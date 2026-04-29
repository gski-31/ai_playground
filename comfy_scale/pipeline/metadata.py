"""Shared library: read image metadata and classify scan vs digital.

Used by both triage.py (reporting) and enhance.py (routing decisions).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import ExifTags, Image

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".heic", ".heif"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".mpg", ".mpeg",
              ".wmv", ".flv", ".webm", ".3gp", ".mts", ".m2ts"}
TAG_BY_NAME = {v: k for k, v in ExifTags.TAGS.items()}

_OPENERS_REGISTERED = False


def register_format_openers() -> None:
    """Register HEIF/HEIC opener with Pillow. Idempotent."""
    global _OPENERS_REGISTERED
    if _OPENERS_REGISTERED:
        return
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    _OPENERS_REGISTERED = True


@dataclass
class ImageMetadata:
    path: Path
    type: str               # "digital" | "scan" | "unknown"
    confidence: float
    reason: str
    exif_date: str | None
    camera: str
    has_gps: bool
    width: int | None
    height: int | None
    size_kb: int
    error: str


# EXIF date sentinels: when we see these we know the date is junk, not a real
# capture timestamp. Common offenders are software defaults and OS epochs.
SENTINEL_DATES = {
    "1900-01-01T00:00:00",
    "1601-01-01T00:00:00",   # Windows FILETIME epoch
    "1904-01-01T00:00:00",   # classic Mac epoch
    "1970-01-01T00:00:00",   # Unix epoch
    "1980-01-01T00:00:00",   # FAT filesystem epoch
    "2000-01-01T00:00:00",
}

# Earliest plausible year for a real camera EXIF date. Consumer digital cameras
# with EXIF DateTimeOriginal don't predate ~1995; we set the bar a bit earlier
# for safety.
EARLIEST_PLAUSIBLE_YEAR = 1990


def date_trust_score(meta: "ImageMetadata") -> tuple[float, str]:
    """Returns (trust 0..1, reason).

    Trust < ~0.5 means the EXIF date is suspicious — likely the scan date,
    a default from broken software, or a sentinel value — and the chronology
    step should re-estimate via vision rather than trusting the EXIF.

    Signals (in priority order):
    - No date                       → 0.0
    - Sentinel value                → 0.0
    - Future date                   → 0.0
    - Year impossibly early         → 0.1
    - Classified as scan            → 0.2  (scanner software / no camera tags)
    - Date present, no camera tags  → 0.4
    - Camera Make/Model present     → 0.95 (real camera EXIF)
    """
    if not meta.exif_date:
        return 0.0, "no date"

    if meta.exif_date in SENTINEL_DATES:
        return 0.0, f"sentinel date {meta.exif_date[:10]}"

    try:
        year = int(meta.exif_date[:4])
    except (ValueError, TypeError):
        return 0.0, "unparseable date"

    current_year = datetime.now().year
    if year > current_year:
        return 0.0, f"future date ({year})"

    if year < EARLIEST_PLAUSIBLE_YEAR:
        return 0.1, f"too old for camera EXIF ({year})"

    if meta.type == "scan":
        return 0.2, "scanner software or no camera tags"

    if meta.camera and len(meta.camera.strip()) > 1:
        return 0.95, f"camera: {meta.camera}"

    return 0.4, "date but no camera tags"


def _exif_dict(img: Image.Image) -> dict:
    raw = img.getexif()
    if not raw:
        return {}
    out: dict = {}
    for tag_id, val in raw.items():
        out[ExifTags.TAGS.get(tag_id, tag_id)] = val
    # DateTimeOriginal lives in the SubIFD; pull it via the offset pointer.
    sub_ifd_id = TAG_BY_NAME.get("ExifOffset")
    if sub_ifd_id and sub_ifd_id in raw:
        sub = raw.get_ifd(sub_ifd_id)
        for tag_id, val in sub.items():
            out[ExifTags.TAGS.get(tag_id, tag_id)] = val
    return out


def parse_exif_date(s) -> str | None:
    if not s:
        return None
    if isinstance(s, bytes):
        s = s.decode("ascii", errors="ignore")
    s = str(s).strip().rstrip("\x00")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).isoformat()
        except ValueError:
            continue
    return None


def classify(exif: dict) -> tuple[str, float, str]:
    has_camera = bool(exif.get("Make") or exif.get("Model"))
    has_date = bool(exif.get("DateTimeOriginal") or exif.get("DateTime"))
    software = str(exif.get("Software", "")).lower()
    if any(kw in software for kw in ("scan", "epson", "canoscan", "vuescan", "silverfast")):
        return "scan", 0.95, f"scanner software: {software}"
    if has_camera and has_date:
        return "digital", 0.95, "camera + date"
    if has_camera and not has_date:
        return "digital", 0.7, "camera, no date"
    if has_date and not has_camera:
        return "scan", 0.7, "date but no camera"
    return "scan", 0.8, "no EXIF (likely scan)"


def read_metadata(path: Path) -> ImageMetadata:
    register_format_openers()
    err = ""
    exif: dict = {}
    width = height = None
    try:
        with Image.open(path) as img:
            width, height = img.size
            exif = _exif_dict(img)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"

    kind, conf, reason = classify(exif) if not err else ("unknown", 0.0, "open failed")
    camera = " ".join(
        str(x).strip() for x in (exif.get("Make"), exif.get("Model")) if x
    ).strip()

    return ImageMetadata(
        path=path,
        type=kind,
        confidence=conf,
        reason=reason,
        exif_date=parse_exif_date(exif.get("DateTimeOriginal") or exif.get("DateTime")),
        camera=camera,
        has_gps="GPSInfo" in exif,
        width=width,
        height=height,
        size_kb=path.stat().st_size // 1024 if path.exists() else 0,
        error=err,
    )
