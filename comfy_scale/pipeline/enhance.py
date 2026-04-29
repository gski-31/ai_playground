"""
Enhance: upscale + clean + re-encode every image in an album tree.

Policy:
- If long edge < TARGET_MIN: upscale 4x via realesrgan-ncnn-vulkan
- Cap long edge at TARGET_MAX after upscale (keeps file sizes sane)
- Output: JPEG quality 92 with original EXIF/GPS preserved
- Idempotent: skips files that already exist in the output tree

Layout (input one level deep):
    <input_root>/
        wedding/*.jpg
        hawaii_trip/*.heic
        ...

Run:
    python -m pipeline.enhance <input_root> --output processed
    python -m pipeline.enhance <input_root> --target-min 3000 --target-max 6000

Tuning detail / over-smoothing:
    --blend 0.5            blend AI result 50/50 with Lanczos-upscaled original
                           (recovers grain/texture the AI smoothed out)
    --model 4x_NMKD-Siax_200k    use a different model
                                 (run: python scripts/download_extra_models.py)
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageEnhance, ImageOps
from tqdm import tqdm

from pipeline.metadata import (
    IMAGE_EXTS, VIDEO_EXTS, read_metadata, register_format_openers,
)

TARGET_MIN = 2000     # upscale anything with a long edge smaller than this
TARGET_MAX = 4000     # cap long edge after upscale to keep file sizes sane
JPEG_QUALITY = 92
UPSCALE_MODEL = "realesrgan-x4plus"
UPSCALE_FACTOR = 4
BLEND_DEFAULT = 1.0   # 1.0 = pure AI, 0.5 = 50/50 with Lanczos-upscaled original, 0.0 = no AI
SATURATION_DEFAULT = 1.0   # 1.0 = unchanged, 1.1 = +10% saturation
AUTO_COLOR_DEFAULT = 0.0   # 0.0 = off, 0.5 = 50% blend of auto-corrected, 1.0 = full
AUTO_COLOR_CUTOFF = 0.5    # ignore top/bottom 0.5% of histogram (clip outliers)
DESCRATCH_DEFAULT = 0.0    # 0.0 = off, 0.7 recommended for scans
POLISH_DEFAULT = 0.0       # 0.0 = off, 0.5 recommended for cleaned-up final look


def find_realesrgan() -> Path | None:
    """Look in common locations + PATH for the realesrgan-ncnn-vulkan binary."""
    exe = "realesrgan-ncnn-vulkan.exe" if sys.platform == "win32" else "realesrgan-ncnn-vulkan"
    candidates = [
        Path("models/realesrgan-ncnn-vulkan") / exe,
        Path("models") / exe,
        Path(exe),
    ]
    for p in candidates:
        if p.exists():
            return p.resolve()
    on_path = shutil.which(exe)
    return Path(on_path) if on_path else None


def upscale_with_binary(src: Path, dst: Path, binary: Path, model: str) -> None:
    cmd = [
        str(binary),
        "-i", str(src),
        "-o", str(dst),
        "-n", model,
        "-s", str(UPSCALE_FACTOR),
        "-m", str(binary.parent),  # explicit models dir = the binary's folder
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"realesrgan failed: {result.stderr.strip() or result.stdout.strip()}")


def fit_max_edge(img: Image.Image, max_edge: int) -> Image.Image:
    longest = max(img.size)
    if longest <= max_edge:
        return img
    scale = max_edge / longest
    return img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)


def descratch_scan(img: Image.Image, strength: float) -> Image.Image:
    """Detect long thin near-vertical lines (typical scanner scratches and
    print scratches) and inpaint them.

    Designed for scans — DO NOT call on digital photos or you'll inpaint
    over real vertical features (door frames, buildings, lampposts).

    strength: 0.0 = no effect, 1.0 = full inpaint. The inpainted result is
    blended over the original at this opacity, so values around 0.7 fix
    obvious scratches while limiting damage from false-positive detections."""
    if strength <= 0:
        return img
    try:
        import cv2
        import numpy as np
    except ImportError:
        raise RuntimeError(
            "Descratch requires opencv-python and numpy. "
            "Install with: pip install opencv-python numpy"
        )

    rgb = np.array(img.convert("RGB"))
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape

    # Edges, then probabilistic Hough lines.
    edges = cv2.Canny(gray, 50, 150)
    min_line_length = max(50, int(h * 0.15))
    lines = cv2.HoughLinesP(
        edges, rho=1, theta=np.pi / 360,
        threshold=80,
        minLineLength=min_line_length,
        maxLineGap=10,
    )
    if lines is None:
        return img

    # Keep only near-vertical line segments (within 5° of vertical).
    mask = np.zeros((h, w), dtype=np.uint8)
    n_drawn = 0
    for line in lines:
        x1, y1, x2, y2 = line[0]
        dy = y2 - y1
        if abs(dy) < 1:
            continue  # horizontal — skip
        angle_from_vertical = abs(np.degrees(np.arctan2(x2 - x1, abs(dy))))
        if angle_from_vertical > 5:
            continue
        cv2.line(mask, (x1, y1), (x2, y2), 255, thickness=2)
        n_drawn += 1

    if n_drawn == 0:
        return img

    # Dilate so the inpaint mask covers the line plus a pixel of padding.
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    mask = cv2.dilate(mask, kernel, iterations=1)

    inpainted_bgr = cv2.inpaint(bgr, mask, inpaintRadius=3, flags=cv2.INPAINT_TELEA)
    inpainted = Image.fromarray(cv2.cvtColor(inpainted_bgr, cv2.COLOR_BGR2RGB))

    if strength >= 1.0:
        return inpainted
    return Image.blend(img, inpainted, alpha=strength)


def polish_image(img: Image.Image, strength: float) -> Image.Image:
    """Final-step picsart-style cleanup: edge-preserving bilateral smoothing
    plus unsharp mask, blended over the original at `strength`.

    Bilateral filter smooths flat areas (skin, sky, walls) while keeping edges
    sharp. The unsharp mask brings back fine detail that the smoothing nibbled.
    The blend keeps the result from looking plastic.

    strength: 0.0 = no effect, 1.0 = full polish, 0.5 = subtle cleaned-up look."""
    if strength <= 0:
        return img
    try:
        import cv2
        import numpy as np
    except ImportError:
        raise RuntimeError(
            "Polish requires opencv-python and numpy. "
            "Install with: pip install opencv-python numpy"
        )

    rgb = np.array(img.convert("RGB"))
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)

    # Bilateral: smooth flat areas while preserving edges. Mild parameters so
    # the blend at <1.0 produces a subtle effect, not a plastic look.
    smoothed = cv2.bilateralFilter(bgr, d=9, sigmaColor=50, sigmaSpace=50)

    # Unsharp mask on the smoothed image: subtract a blurred copy to re-add detail.
    blurred = cv2.GaussianBlur(smoothed, (0, 0), sigmaX=2)
    sharpened = cv2.addWeighted(smoothed, 1.4, blurred, -0.4, 0)

    polished = Image.fromarray(cv2.cvtColor(sharpened, cv2.COLOR_BGR2RGB))

    if strength >= 1.0:
        return polished
    return Image.blend(img, polished, alpha=strength)


def apply_auto_color(img: Image.Image, alpha: float,
                     cutoff: float = AUTO_COLOR_CUTOFF) -> Image.Image:
    """Photoshop-style auto-color: independently stretch each channel's histogram,
    then blend the corrected version over the original at `alpha` opacity.
    Removes color casts and boosts contrast in one pass."""
    if alpha <= 0.0:
        return img
    if img.mode == "RGB":
        r, g, b = img.split()
        corrected = Image.merge("RGB", (
            ImageOps.autocontrast(r, cutoff=cutoff),
            ImageOps.autocontrast(g, cutoff=cutoff),
            ImageOps.autocontrast(b, cutoff=cutoff),
        ))
    elif img.mode == "L":
        corrected = ImageOps.autocontrast(img, cutoff=cutoff)
    else:
        return img
    if alpha >= 1.0:
        return corrected
    return Image.blend(img, corrected, alpha=alpha)


def process_image(
    src: Path,
    dst: Path,
    realesrgan: Path | None,
    target_min: int,
    target_max: int,
    quality: int,
    model: str,
    blend: float,
    saturation: float = SATURATION_DEFAULT,
    auto_color: float = AUTO_COLOR_DEFAULT,
    scan_model: str | None = None,
    descratch: float = DESCRATCH_DEFAULT,
    polish: float = POLISH_DEFAULT,
) -> str:
    """Returns a one-word action: 'upscaled' | 'kept' | 'passthrough'.

    Routing — when `scan_model` is set, images classified as scans use that
    model instead of `model`. Descratch (when > 0) applies to scans only."""
    # Read metadata once if we'll need it for scan-routing or descratch.
    meta = None
    if scan_model or descratch > 0:
        meta = read_metadata(src)
    is_scan = bool(meta and meta.type == "scan")

    active_model = scan_model if (is_scan and scan_model) else model

    with Image.open(src) as src_img:
        # Capture color profile + EXIF before any mode/format conversion.
        # Without this, phone photos shot in Display P3 lose their wide-gamut
        # tag on save and look noticeably duller in any color-aware viewer.
        icc_profile = src_img.info.get("icc_profile")
        exif = src_img.getexif()

        if src_img.mode not in ("RGB", "L"):
            src_img = src_img.convert("RGB")
        long_edge = max(src_img.size)
        needs_upscale = long_edge < target_min

        if needs_upscale and realesrgan:
            with tempfile.TemporaryDirectory() as td:
                tmp_in = Path(td) / "in.png"
                tmp_out = Path(td) / "out.png"
                src_img.save(tmp_in, "PNG")
                upscale_with_binary(tmp_in, tmp_out, realesrgan, active_model)
                with Image.open(tmp_out) as up:
                    ai = up.convert("RGB")
                    if blend < 1.0:
                        # Lanczos-upscale original to AI's size, blend toward authentic detail.
                        baseline = src_img.resize(ai.size, Image.LANCZOS)
                        ai = Image.blend(baseline, ai, alpha=blend)
                    out_img = fit_max_edge(ai, target_max).copy()
            action = "upscaled"
        else:
            out_img = fit_max_edge(src_img, target_max).copy()
            action = "passthrough" if needs_upscale else "kept"

        if descratch > 0 and is_scan:
            out_img = descratch_scan(out_img, strength=descratch)
        if auto_color > 0.0:
            out_img = apply_auto_color(out_img, auto_color)
        if saturation != 1.0:
            out_img = ImageEnhance.Color(out_img).enhance(saturation)
        if polish > 0.0:
            out_img = polish_image(out_img, strength=polish)

    dst.parent.mkdir(parents=True, exist_ok=True)
    save_kwargs = {"quality": quality, "exif": exif, "optimize": True}
    if icc_profile:
        save_kwargs["icc_profile"] = icc_profile
    out_img.save(dst, "JPEG", **save_kwargs)
    return action


def process_album(
    album_dir: Path,
    out_dir: Path,
    realesrgan: Path | None,
    target_min: int,
    target_max: int,
    quality: int,
    model: str,
    blend: float,
    saturation: float = SATURATION_DEFAULT,
    auto_color: float = AUTO_COLOR_DEFAULT,
    scan_model: str | None = None,
    descratch: float = DESCRATCH_DEFAULT,
    polish: float = POLISH_DEFAULT,
) -> dict[str, int]:
    images = sorted(p for p in album_dir.iterdir() if p.suffix.lower() in IMAGE_EXTS)
    videos = sorted(p for p in album_dir.iterdir() if p.suffix.lower() in VIDEO_EXTS)
    out_dir.mkdir(parents=True, exist_ok=True)

    counts = {"upscaled": 0, "kept": 0, "passthrough": 0, "skipped": 0, "error": 0,
              "video_copied": 0, "video_skipped": 0}
    for src in tqdm(images, desc=album_dir.name, unit="img", leave=False):
        dst = out_dir / (src.stem + ".jpg")
        if dst.exists():
            counts["skipped"] += 1
            continue
        try:
            action = process_image(src, dst, realesrgan, target_min, target_max,
                                   quality, model, blend, saturation, auto_color,
                                   scan_model, descratch, polish)
            counts[action] += 1
        except Exception as e:
            counts["error"] += 1
            tqdm.write(f"  ERROR {album_dir.name}/{src.name}: {type(e).__name__}: {e}")

    # Videos: copy as-is, preserve mtime + ctime via copy2. No editing.
    for src in tqdm(videos, desc=f"{album_dir.name} (video)", unit="vid", leave=False):
        dst = out_dir / src.name
        if dst.exists():
            counts["video_skipped"] += 1
            continue
        try:
            shutil.copy2(src, dst)
            counts["video_copied"] += 1
        except Exception as e:
            counts["error"] += 1
            tqdm.write(f"  ERROR {album_dir.name}/{src.name}: copy failed: {e}")

    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", type=Path, help="Root folder containing album subfolders")
    ap.add_argument("--output", type=Path, default=Path("processed"))
    ap.add_argument("--target-min", type=int, default=TARGET_MIN,
                    help=f"Upscale if long edge below this (default: {TARGET_MIN})")
    ap.add_argument("--target-max", type=int, default=TARGET_MAX,
                    help=f"Cap long edge at this (default: {TARGET_MAX})")
    ap.add_argument("--quality", type=int, default=JPEG_QUALITY,
                    help=f"JPEG quality (default: {JPEG_QUALITY})")
    ap.add_argument("--realesrgan", type=Path, default=None,
                    help="Path to realesrgan-ncnn-vulkan binary (auto-detected if omitted)")
    ap.add_argument("--model", default=UPSCALE_MODEL,
                    help=f"Upscale model name (default: {UPSCALE_MODEL}). "
                         "Run scripts/download_extra_models.py for better photo models, "
                         "then try: RealESRGAN_General_x4_v3, 4x_NMKD-Siax_200k, "
                         "4xLSDIRCompactC3, 4xNomos8kSC")
    ap.add_argument("--blend", type=float, default=BLEND_DEFAULT,
                    help="Blend AI result with Lanczos-upscaled original. "
                         "1.0 = pure AI (default), 0.5 = 50/50, 0.0 = no AI. "
                         "Lower values recover authentic grain/texture the AI smoothed away.")
    ap.add_argument("--saturation", type=float, default=SATURATION_DEFAULT,
                    help="Color saturation multiplier. 1.0 = unchanged (default), "
                         "1.1 = +10%% (subtle pop), 1.2 = +20%% (more vivid), "
                         "0.0 = grayscale. Try 1.10–1.15 if outputs look dull.")
    ap.add_argument("--auto-color", type=float, default=AUTO_COLOR_DEFAULT,
                    help="Photoshop-style auto-color (per-channel histogram stretch) "
                         "blended at this opacity. 0.0 = off (default), 0.5 = subtle "
                         "50%% blend, 1.0 = full. Fixes color casts on old scans.")
    args = ap.parse_args()

    if not 0.0 <= args.blend <= 1.0:
        raise SystemExit(f"--blend must be between 0.0 and 1.0, got {args.blend}")
    if args.saturation < 0.0:
        raise SystemExit(f"--saturation must be >= 0.0, got {args.saturation}")
    if not 0.0 <= args.auto_color <= 1.0:
        raise SystemExit(f"--auto-color must be 0.0-1.0, got {args.auto_color}")

    register_format_openers()

    if not args.input.is_dir():
        raise SystemExit(f"Input not a directory: {args.input}")

    realesrgan = args.realesrgan or find_realesrgan()
    if realesrgan:
        print(f"Upscaler: {realesrgan}")
        print(f"  model: {args.model}  blend: {args.blend:.2f}  "
              f"saturation: {args.saturation:.2f}  "
              f"auto-color: {args.auto_color:.2f}")
        # Sanity check the requested model exists.
        if not (realesrgan.parent / f"{args.model}.bin").exists():
            print(f"  WARNING: {args.model}.bin not found in {realesrgan.parent}")
            print(f"  Run: python scripts/download_extra_models.py")
    else:
        print(f"WARNING: realesrgan-ncnn-vulkan not found.")
        print(f"  Images below {args.target_min}px will pass through without upscale.")
        print(f"  To install: python scripts/download_models.py")

    albums = sorted(p for p in args.input.iterdir() if p.is_dir()) or [args.input]
    print(f"Processing {len(albums)} album(s) → {args.output}\n")

    totals = {"upscaled": 0, "kept": 0, "passthrough": 0, "skipped": 0, "error": 0}
    for album in albums:
        c = process_album(album, args.output / album.name, realesrgan,
                          args.target_min, args.target_max, args.quality,
                          args.model, args.blend, args.saturation, args.auto_color)
        print(
            f"  [{album.name}]  upscaled {c['upscaled']:>4}  "
            f"kept {c['kept']:>4}  pass {c['passthrough']:>3}  "
            f"skip {c['skipped']:>4}  err {c['error']:>3}"
        )
        for k, v in c.items():
            totals[k] += v

    print(
        f"\nTotals: upscaled {totals['upscaled']}  kept {totals['kept']}  "
        f"passthrough {totals['passthrough']}  skipped {totals['skipped']}  "
        f"errors {totals['error']}"
    )


if __name__ == "__main__":
    main()
