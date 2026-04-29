"""
Download the realesrgan-ncnn-vulkan binary + bundled models into ./models/.

After running, enhance.py will auto-detect the binary at:
    models/realesrgan-ncnn-vulkan/realesrgan-ncnn-vulkan(.exe)

Run:
    python scripts/download_models.py
"""
from __future__ import annotations

import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

# v0.2.5.0 — the latest tagged release with prebuilt binaries.
RELEASES = {
    "win32": "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-windows.zip",
    "darwin": "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-macos.zip",
    "linux": "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-ubuntu.zip",
}

INSTALL_DIR = Path("models/realesrgan-ncnn-vulkan")


def download(url: str, dest: Path) -> None:
    print(f"Downloading {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def main() -> None:
    platform = sys.platform if sys.platform in RELEASES else (
        "linux" if sys.platform.startswith("linux") else None
    )
    if not platform:
        raise SystemExit(f"No prebuilt binary for platform: {sys.platform}")

    exe_name = "realesrgan-ncnn-vulkan.exe" if platform == "win32" else "realesrgan-ncnn-vulkan"
    if (INSTALL_DIR / exe_name).exists():
        print(f"Already installed: {INSTALL_DIR / exe_name}")
        return

    zip_path = Path("models/_realesrgan.zip")
    download(RELEASES[platform], zip_path)

    print(f"Extracting → {INSTALL_DIR}")
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(INSTALL_DIR)
    zip_path.unlink()

    binary = INSTALL_DIR / exe_name
    if not binary.exists():
        # Some release zips put files under a nested folder; flatten it.
        nested = next((d for d in INSTALL_DIR.iterdir() if d.is_dir()), None)
        if nested:
            for f in nested.iterdir():
                f.rename(INSTALL_DIR / f.name)
            nested.rmdir()
    if not binary.exists():
        raise SystemExit(f"Binary missing after extract: {binary}")

    if platform != "win32":
        binary.chmod(0o755)

    print(f"\nInstalled: {binary}")
    print("Bundled models:")
    for m in sorted(INSTALL_DIR.glob("*.bin")):
        print(f"  - {m.stem}")


if __name__ == "__main__":
    main()
