"""
Triage: walk an album tree, read EXIF, classify each image, write a per-album
manifest CSV. Pure metadata — no GPU, no API. Useful as a preview before enhance.

Layout:
    <input_root>/
        wedding/
            *.jpg
        hawaii_trip/
            *.jpg

Run:
    python -m pipeline.triage <input_root> --output processed
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
from pathlib import Path

from tqdm import tqdm

from pipeline.metadata import (
    IMAGE_EXTS,
    ImageMetadata,
    read_metadata,
    register_format_openers,
)


def triage_album(album_dir: Path, out_dir: Path) -> list[ImageMetadata]:
    out_dir.mkdir(parents=True, exist_ok=True)
    images = sorted(p for p in album_dir.iterdir() if p.suffix.lower() in IMAGE_EXTS)
    rows = [read_metadata(p) for p in tqdm(images, desc=album_dir.name, unit="img", leave=False)]

    if rows:
        manifest = out_dir / "manifest.csv"
        fieldnames = ["album", "filename"] + [
            k for k in asdict(rows[0]).keys() if k != "path"
        ]
        with manifest.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in rows:
                d = asdict(r)
                d["album"] = album_dir.name
                d["filename"] = r.path.name
                d.pop("path")
                writer.writerow(d)

    return rows


def _summary(label: str, rows: list[ImageMetadata]) -> None:
    if not rows:
        print(f"  [{label}] (empty)")
        return
    n = len(rows)
    digital = sum(1 for r in rows if r.type == "digital")
    scan = sum(1 for r in rows if r.type == "scan")
    dated = sum(1 for r in rows if r.exif_date)
    errors = sum(1 for r in rows if r.error)
    pct = lambda x: f"{100 * x // n}%"
    print(
        f"  [{label}] {n:>5} | digital {digital:>5} ({pct(digital)})  "
        f"scan {scan:>5} ({pct(scan)})  dated {dated:>5} ({pct(dated)})"
        + (f"  errors {errors}" if errors else "")
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", type=Path)
    ap.add_argument("--output", type=Path, default=Path("processed"))
    args = ap.parse_args()

    register_format_openers()

    if not args.input.is_dir():
        raise SystemExit(f"Input not a directory: {args.input}")

    albums = sorted(p for p in args.input.iterdir() if p.is_dir()) or [args.input]
    print(f"Triaging {len(albums)} album(s)\n")

    all_rows: list[ImageMetadata] = []
    for album in albums:
        rows = triage_album(album, args.output / album.name)
        _summary(album.name, rows)
        all_rows.extend(rows)

    print()
    _summary("TOTAL", all_rows)

    needs_vision = sum(1 for r in all_rows if not r.exif_date and not r.error)
    if needs_vision:
        est = needs_vision * 0.004
        print(f"\n~{needs_vision} images lack EXIF dates → vision API candidates "
              f"(rough Sonnet+Batch est: ${est:.2f})")


if __name__ == "__main__":
    main()
