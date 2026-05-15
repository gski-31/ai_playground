"""Build & run the ffmpeg HEVC NVENC encode and mkvpropedit tagging pass."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from probe import AudioStream, ProbeResult, SubStream


@dataclass
class MovieMeta:
    title: str = ""
    year: str = ""
    overview: str = ""
    tmdb_id: Optional[int] = None
    poster_path: Optional[Path] = None  # local file


@dataclass
class EncodeJob:
    source: Path
    output: Path
    probe: ProbeResult
    audio: AudioStream
    subs: List[SubStream]
    max_height: int = 1080
    video_kbps: int = 2500
    downmix_stereo: bool = False
    meta: Optional[MovieMeta] = None
    # external .srt files to mux in after the internal subs
    extra_srts: List[Path] = field(default_factory=list)
    # position in the combined output sub list (internal first, then externals) to flag as forced
    forced_sub_idx: Optional[int] = field(default=None)


def _aac_bitrate(channels: int, downmix: bool) -> str:
    if downmix or channels <= 2:
        return "192k"
    if channels <= 6:
        return "384k"
    return "512k"


def build_ffmpeg_cmd(ffmpeg: Path, job: EncodeJob) -> List[str]:
    cmd: List[str] = [
        str(ffmpeg),
        "-hide_banner",
        "-y",
        "-i", str(job.source),
    ]
    # External SRT inputs follow input 0
    for srt in job.extra_srts:
        cmd += ["-sub_charenc", "UTF-8", "-i", str(srt)]

    cmd += [
        "-map", "0:v:0",
        "-map", f"0:{job.audio.index}",
    ]
    for s in job.subs:
        cmd += ["-map", f"0:{s.index}"]
    for i, _ in enumerate(job.extra_srts):
        cmd += ["-map", f"{i + 1}:0"]
    # Preserve chapter markers from the source (Blu-ray rips, etc.)
    cmd += ["-map_chapters", "0"]

    # Video: HEVC NVENC, 10-bit, 2-pass VBR at a target average bitrate.
    maxrate = int(job.video_kbps * 1.6)
    bufsize = maxrate * 2
    cmd += [
        "-c:v", "hevc_nvenc",
        "-preset", "p7",
        "-tune", "hq",
        "-profile:v", "main10",
        "-pix_fmt", "p010le",
        "-rc", "vbr",
        "-multipass", "fullres",
        "-b:v", f"{job.video_kbps}k",
        "-maxrate", f"{maxrate}k",
        "-bufsize", f"{bufsize}k",
        "-spatial-aq", "1",
        "-temporal-aq", "1",
        "-bf", "4",
        "-b_ref_mode", "middle",
        "-refs", "4",
        "-rc-lookahead", "32",
        "-vf", f"scale=-2:'min({job.max_height},ih)'",
    ]

    # Audio: re-encode to AAC; preserve or downmix
    ch = 2 if job.downmix_stereo else max(1, job.audio.channels or 2)
    cmd += [
        "-c:a", "aac",
        "-b:a", _aac_bitrate(job.audio.channels, job.downmix_stereo),
        "-ac", str(ch),
        "-metadata:s:a:0", "language=eng",
    ]

    # Subtitles: per-stream codec. mov_text (MP4) must convert to srt for MKV; others copy.
    total_subs = len(job.subs) + len(job.extra_srts)
    for i in range(total_subs):
        if i < len(job.subs):
            src_codec = job.subs[i].codec.lower()
            out_codec = "srt" if src_codec == "mov_text" else "copy"
        else:
            out_codec = "copy"
        cmd += [f"-c:s:{i}", out_codec]
        cmd += [f"-metadata:s:s:{i}", "language=eng"]
        if job.forced_sub_idx == i:
            cmd += [f"-disposition:s:{i}", "default+forced"]
        else:
            cmd += [f"-disposition:s:{i}", "0"]

    # Container metadata
    if job.meta:
        if job.meta.title:
            title = f"{job.meta.title} ({job.meta.year})" if job.meta.year else job.meta.title
            cmd += ["-metadata", f"title={title}"]
        if job.meta.year:
            cmd += ["-metadata", f"date={job.meta.year}"]
        if job.meta.overview:
            cmd += ["-metadata", f"description={job.meta.overview}"]

    # Progress sink on stdout
    cmd += ["-progress", "pipe:1", "-nostats", str(job.output)]
    return cmd


# keys emitted by ffmpeg's `-progress` reporter; we filter these out of the log so the
# pane only shows real info / warnings / errors.
_PROGRESS_KEYS = (
    "frame=", "fps=", "stream_", "bitrate=", "total_size=",
    "out_time_us=", "out_time_ms=", "out_time=",
    "dup_frames=", "drop_frames=", "speed=", "progress=",
)


def run_encode(
    ffmpeg: Path,
    job: EncodeJob,
    on_progress: Optional[Callable[[float], None]] = None,
    on_log: Optional[Callable[[str], None]] = None,
) -> None:
    """Run ffmpeg with progress on stdout; merge stderr into stdout so we never block on it."""
    cmd = build_ffmpeg_cmd(ffmpeg, job)
    if on_log:
        on_log("$ " + " ".join(f'"{a}"' if " " in a else a for a in cmd))

    duration_us = max(1.0, job.probe.duration_s) * 1_000_000
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip()
            if not line:
                continue
            if line.startswith("out_time_ms="):
                try:
                    out_us = int(line.split("=", 1)[1])
                    if on_progress:
                        on_progress(min(1.0, out_us / duration_us))
                except ValueError:
                    pass
                continue
            if any(line.startswith(k) for k in _PROGRESS_KEYS):
                continue
            if on_log:
                on_log(line)
    finally:
        rc = proc.wait()
        if rc != 0:
            raise RuntimeError(f"ffmpeg exited with code {rc}")


def attach_poster(mkvpropedit: Path, mkv_path: Path, poster_path: Path) -> None:
    """Attach poster image as MKV cover art. Removes any existing cover-* attachments first."""
    # Detect mime from extension
    ext = poster_path.suffix.lower()
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}.get(ext, "image/jpeg")
    # Filename convention recognized by Matroska players as cover art:
    # cover.jpg = small (default), cover_land.jpg = landscape. We use cover.jpg.
    cover_name = "cover.jpg" if mime == "image/jpeg" else "cover.png"
    cmd = [
        str(mkvpropedit),
        str(mkv_path),
        "--attachment-name", cover_name,
        "--attachment-mime-type", mime,
        "--attachment-description", "Movie poster",
        "--add-attachment", str(poster_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def set_container_title(mkvpropedit: Path, mkv_path: Path, title: str) -> None:
    cmd = [str(mkvpropedit), str(mkv_path), "--edit", "info", "--set", f"title={title}"]
    subprocess.run(cmd, check=True, capture_output=True)
