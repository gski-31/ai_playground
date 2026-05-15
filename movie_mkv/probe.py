"""ffprobe wrapper + heuristics for picking the best English audio and the right English subs."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


# Audio codec quality tiers — higher beats lower when picking "best English audio".
AUDIO_TIER = {
    "truehd": 100,
    "dts-hd ma": 95,
    "mlp": 92,
    "flac": 90,
    "pcm_s16le": 88,
    "pcm_s24le": 88,
    "eac3": 70,
    "dts": 65,
    "ac3": 60,
    "opus": 55,
    "aac": 50,
    "mp3": 30,
    "vorbis": 25,
}


@dataclass
class AudioStream:
    index: int           # absolute stream index in the source
    codec: str
    channels: int
    language: str
    title: str
    default: bool
    bit_rate: int = 0    # bps, 0 if unknown

    def quality_score(self) -> int:
        return AUDIO_TIER.get(self.codec.lower(), 0) * 100 + self.channels * 50 + (1 if self.default else 0)

    def label(self) -> str:
        bits = [self.codec.upper(), f"{self.channels}ch"]
        if self.language:
            bits.append(f"[{self.language}]")
        if self.title:
            bits.append(f'"{self.title}"')
        return " ".join(bits)


@dataclass
class SubStream:
    index: int
    codec: str
    language: str
    title: str
    default: bool
    forced: bool
    hearing_impaired: bool = False

    def is_forced_only(self) -> bool:
        if self.forced:
            return True
        t = self.title.lower()
        # heuristic: title says "forced" but track may not have the disposition flag set
        return "forced" in t and "non-forced" not in t

    def is_sdh(self) -> bool:
        if self.hearing_impaired:
            return True
        t = self.title.lower()
        return "sdh" in t or "hearing" in t

    def label(self) -> str:
        bits = [self.codec.upper()]
        if self.language:
            bits.append(f"[{self.language}]")
        if self.title:
            bits.append(f'"{self.title}"')
        tags = []
        if self.is_forced_only():
            tags.append("forced")
        if self.is_sdh():
            tags.append("SDH")
        if self.default:
            tags.append("default")
        if tags:
            bits.append(f"({', '.join(tags)})")
        return " ".join(bits)


@dataclass
class ProbeResult:
    path: Path
    duration_s: float = 0.0
    audio: List[AudioStream] = field(default_factory=list)
    subs: List[SubStream] = field(default_factory=list)

    def best_english_audio(self) -> Optional[AudioStream]:
        eng = [a for a in self.audio if a.language.lower().startswith("eng")]
        pool = eng or self.audio
        if not pool:
            return None
        return max(pool, key=lambda a: a.quality_score())

    def preferred_subs(self) -> List[SubStream]:
        """Pick the subs to embed: full English (toggleable) + forced English (auto-show foreign)."""
        eng = [s for s in self.subs if s.language.lower().startswith("eng")]
        if not eng:
            return []
        forced = [s for s in eng if s.is_forced_only()]
        full = [s for s in eng if not s.is_forced_only() and not s.is_sdh()]
        chosen: List[SubStream] = []
        if full:
            chosen.append(full[0])
        if forced:
            chosen.append(forced[0])
        # Fallback: if no "full" non-SDH track, take any non-forced English (incl. SDH)
        if not chosen:
            chosen = eng[:1]
        return chosen


def probe(ffprobe: Path, video_path: Path) -> ProbeResult:
    cmd = [
        str(ffprobe),
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(video_path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True, encoding="utf-8")
    data = json.loads(out.stdout)
    result = ProbeResult(path=video_path)

    fmt = data.get("format", {})
    try:
        result.duration_s = float(fmt.get("duration", 0) or 0)
    except (TypeError, ValueError):
        result.duration_s = 0.0

    for s in data.get("streams", []):
        codec_type = s.get("codec_type")
        tags = s.get("tags", {}) or {}
        disp = s.get("disposition", {}) or {}
        idx = s.get("index", -1)
        codec = s.get("codec_name", "") or ""
        lang = tags.get("language", "") or ""
        title = tags.get("title", "") or ""
        default = bool(disp.get("default", 0))

        if codec_type == "audio":
            try:
                br = int(s.get("bit_rate", 0) or 0)
            except (TypeError, ValueError):
                br = 0
            result.audio.append(AudioStream(
                index=idx,
                codec=codec,
                channels=int(s.get("channels", 0) or 0),
                language=lang,
                title=title,
                default=default,
                bit_rate=br,
            ))
        elif codec_type == "subtitle":
            result.subs.append(SubStream(
                index=idx,
                codec=codec,
                language=lang,
                title=title,
                default=default,
                forced=bool(disp.get("forced", 0)),
                hearing_impaired=bool(disp.get("hearing_impaired", 0)),
            ))

    return result
