"""
Download additional community upscale models from Upscayl's custom-models repo
into the realesrgan-ncnn-vulkan models folder.

These are higher-quality alternatives to the default `realesrgan-x4plus`:
  - RealESRGAN_General_x4_v3   newer Real-ESRGAN, less plasticky than x4plus
  - 4x_NMKD-Siax_200k          popular general-purpose photo upscaler
  - 4xLSDIRCompactC3           modern LSDIR-trained, fast and clean
  - 4xNomos8kSC                Nomos dataset, alternative general-purpose
  - 4x_NMKD-Superscale-SP      another good photo option
  - uniscale_restore           restoration-focused

After install, use with enhance.py:
    python -m pipeline.enhance <input> --model 4x_NMKD-Siax_200k

Run:
    python scripts/download_extra_models.py
    python scripts/download_extra_models.py --all       # grab everything in the list
    python scripts/download_extra_models.py 4xLSDIRCompactC3   # one specific model
"""
from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

REPO_BASE = "https://github.com/upscayl/custom-models/raw/main/models"

# Curated list — names mirror the .bin/.param filenames in the repo.
DEFAULT_MODELS = [
    "RealESRGAN_General_x4_v3",
    "4x_NMKD-Siax_200k",
    "4xLSDIRCompactC3",
    "4xNomos8kSC",
]

ALL_MODELS = DEFAULT_MODELS + [
    "4x_NMKD-Superscale-SP_178000_G",
    "uniscale_restore",
    "4xLSDIRplusC",
    "4xLSDIR",
]

INSTALL_DIR = Path("models/realesrgan-ncnn-vulkan")


def download(url: str, dest: Path) -> None:
    if dest.exists():
        print(f"  already present: {dest.name}")
        return
    print(f"  downloading {dest.name}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(url) as r, open(tmp, "wb") as f:
            while chunk := r.read(64 * 1024):
                f.write(chunk)
        tmp.rename(dest)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise


def fetch_model(name: str) -> bool:
    """Download both .bin and .param for a model. Returns True on success."""
    print(f"\n[{name}]")
    try:
        for ext in ("param", "bin"):
            download(f"{REPO_BASE}/{name}.{ext}", INSTALL_DIR / f"{name}.{ext}")
        return True
    except Exception as e:
        print(f"  FAILED: {type(e).__name__}: {e}")
        return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("models", nargs="*",
                    help="Specific model names to download (default: curated set)")
    ap.add_argument("--all", action="store_true",
                    help="Download every model in the known list")
    args = ap.parse_args()

    if not INSTALL_DIR.exists():
        raise SystemExit(
            f"Upscaler not installed yet — run download_models.py first.\n"
            f"Expected dir: {INSTALL_DIR}"
        )

    if args.models:
        targets = args.models
    elif args.all:
        targets = ALL_MODELS
    else:
        targets = DEFAULT_MODELS

    print(f"Installing into: {INSTALL_DIR}")
    print(f"Models: {', '.join(targets)}")

    ok = sum(fetch_model(m) for m in targets)
    print(f"\nDone: {ok}/{len(targets)} models installed.")
    if ok < len(targets):
        sys.exit(1)


if __name__ == "__main__":
    main()
