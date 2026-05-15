"""Locate or download ffmpeg + MKVToolNix on Windows.

ffmpeg comes from gyan.dev's release-essentials zip (always-latest URL).
MKVToolNix comes from the official 7z portable build.
"""
from __future__ import annotations

import shutil
import zipfile
from pathlib import Path
from typing import Callable, Optional, Tuple

import requests

from config import TOOLS_DIR


FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
# Bumping note: this URL pattern is stable; update version if a newer release is desired.
MKVTOOLNIX_VERSION = "88.0"
MKVTOOLNIX_URL = (
    f"https://mkvtoolnix.download/windows/releases/{MKVTOOLNIX_VERSION}/"
    f"mkvtoolnix-64-bit-{MKVTOOLNIX_VERSION}.7z"
)


ProgressFn = Callable[[str, int], None]  # (label, percent)


def _find(name: str) -> Optional[Path]:
    p = shutil.which(name)
    if p:
        return Path(p)
    if TOOLS_DIR.exists():
        for match in TOOLS_DIR.rglob(f"{name}.exe"):
            return match
    return None


def locate_all() -> dict:
    return {
        "ffmpeg": _find("ffmpeg"),
        "ffprobe": _find("ffprobe"),
        "mkvmerge": _find("mkvmerge"),
        "mkvpropedit": _find("mkvpropedit"),
    }


def missing_tools() -> list[str]:
    return [name for name, path in locate_all().items() if path is None]


def _download(url: str, dest: Path, label: str, progress: Optional[ProgressFn]) -> None:
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0))
        got = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                got += len(chunk)
                if progress and total:
                    progress(label, int(100 * got / total))
        if progress:
            progress(label, 100)


def ensure_ffmpeg(progress: Optional[ProgressFn] = None) -> Tuple[Path, Path]:
    ff = _find("ffmpeg")
    fp = _find("ffprobe")
    if ff and fp:
        return ff, fp

    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = TOOLS_DIR / "ffmpeg.zip"
    _download(FFMPEG_URL, zip_path, "Downloading ffmpeg", progress)

    if progress:
        progress("Extracting ffmpeg", 0)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(TOOLS_DIR)
    zip_path.unlink(missing_ok=True)
    if progress:
        progress("Extracting ffmpeg", 100)

    ff, fp = _find("ffmpeg"), _find("ffprobe")
    if not (ff and fp):
        raise RuntimeError("ffmpeg/ffprobe not found after extraction")
    return ff, fp


def ensure_mkvtoolnix(progress: Optional[ProgressFn] = None) -> Tuple[Path, Path]:
    mm = _find("mkvmerge")
    mp = _find("mkvpropedit")
    if mm and mp:
        return mm, mp

    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    sz_path = TOOLS_DIR / "mkvtoolnix.7z"
    _download(MKVTOOLNIX_URL, sz_path, "Downloading MKVToolNix", progress)

    if progress:
        progress("Extracting MKVToolNix", 0)
    import py7zr
    with py7zr.SevenZipFile(sz_path, "r") as z:
        z.extractall(TOOLS_DIR)
    sz_path.unlink(missing_ok=True)
    if progress:
        progress("Extracting MKVToolNix", 100)

    mm, mp = _find("mkvmerge"), _find("mkvpropedit")
    if not (mm and mp):
        raise RuntimeError("mkvmerge/mkvpropedit not found after extraction")
    return mm, mp


def ensure_all(progress: Optional[ProgressFn] = None) -> dict:
    ff, fp = ensure_ffmpeg(progress)
    mm, mpe = ensure_mkvtoolnix(progress)
    return {"ffmpeg": ff, "ffprobe": fp, "mkvmerge": mm, "mkvpropedit": mpe}
