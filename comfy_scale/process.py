"""
Single entry point: enhance + chronologically order all album subfolders.

Layout:
    <input_root>/
        wedding/
        hawaii_trip/
        ...

Outputs:
    processed/
        wedding/
            wedding_0001.jpg             oldest photo, renamed chronologically
            wedding_0002.jpg             ...
            chronological_order.csv      maps original → renamed names
            .renamed_complete            marker — album is fully done
        hawaii_trip/
            ...
        manifest.csv                     all albums combined

Re-runs: albums with .renamed_complete are skipped entirely. To re-process an
album, delete its processed/<album>/ folder.

Defaults:
    upscale model:  4x_NMKD-Siax_200k    most reputable photo upscaler
    blend:          0.5                  50/50 AI / Lanczos to keep authentic detail
    target-min:     2000                 upscale only if long edge below this
    target-max:     4000                 cap long edge to keep file sizes sane
    JPEG quality:   92

Run:
    # one command, does everything (downloads models if needed):
    python process.py path/to/photos

    # if you don't want vision API (uses EXIF dates only):
    python process.py path/to/photos --skip-chronology

    # set ANTHROPIC_API_KEY env var to enable date estimation:
    set ANTHROPIC_API_KEY=sk-ant-...        (Windows)
    export ANTHROPIC_API_KEY=sk-ant-...     (Mac/Linux)
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from tqdm import tqdm

from pipeline.enhance import (
    JPEG_QUALITY, TARGET_MAX, TARGET_MIN,
    find_realesrgan, process_album,
)
from pipeline.metadata import (
    IMAGE_EXTS, VIDEO_EXTS, date_trust_score, read_metadata,
    register_format_openers,
)

DEFAULT_MODEL = "4x_NMKD-Siax_200k"
DEFAULT_SCAN_MODEL = "uniscale_restore"   # restoration-focused model for scans
DEFAULT_BLEND = 0.6         # 60% AI upscale over Lanczos-upscaled original
DEFAULT_SATURATION = 1.10   # subtle color pop to counter AI desaturation
DEFAULT_AUTO_COLOR = 0.6    # 60% blend of Photoshop-style auto-color
DEFAULT_DESCRATCH = 0.5     # vertical-line descratch at 50% over original (scans only)
DEFAULT_POLISH = 0.35       # picsart-style cleanup polish (final step, all images)
DEFAULT_API_MODEL = "claude-sonnet-4-6"
DEFAULT_TRUST_THRESHOLD = 0.5   # below this, EXIF date is treated as suspicious


def ensure_models(*models: str) -> Path:
    """Make sure realesrgan binary + each requested model file are present.
    Auto-downloads on first run. Empty / None model names are skipped."""
    binary = find_realesrgan()
    if not binary:
        print("Real-ESRGAN binary not found. Downloading (~50MB)...")
        subprocess.run([sys.executable, "scripts/download_models.py"], check=True)
        binary = find_realesrgan()
        if not binary:
            raise SystemExit("Binary still missing after download. "
                             "See scripts/download_models.py output.")

    for model in models:
        if not model:
            continue
        if (binary.parent / f"{model}.bin").exists():
            continue
        print(f"Model '{model}' not found. Downloading...")
        subprocess.run(
            [sys.executable, "scripts/download_extra_models.py", model], check=True,
        )
        if not (binary.parent / f"{model}.bin").exists():
            raise SystemExit(f"Model '{model}' still missing after download.")

    return binary


ALBUM_FIELDS = ["sequence", "filename", "original_filename", "source_type",
                "best_date", "date_source", "confidence", "exif_date",
                "exif_trust", "exif_trust_reason",
                "estimated_date", "estimate_reason"]


def _chronological_sort_key(row: dict, prefix: tuple = ()) -> tuple:
    """Sort key: scans (older) before digital/video (modern), then by best date,
    then by original filename. 'unknown' sorts with scans."""
    sort_group = 1 if row.get("source_type") in ("digital", "video") else 0
    date = row.get("best_date") or "9999"
    fn = row.get("original_filename") or row.get("filename", "")
    return prefix + (sort_group, date, fn)


def write_album_manifest(out_dir: Path, rows: list[dict]) -> None:
    if not rows:
        return
    rows_sorted = sorted(rows, key=_chronological_sort_key)
    with (out_dir / "chronological_order.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ALBUM_FIELDS)
        w.writeheader()
        for i, r in enumerate(rows_sorted, 1):
            r["sequence"] = i
            w.writerow({k: r.get(k, "") for k in ALBUM_FIELDS})


def read_existing_album_manifest(out_dir: Path, album_name: str) -> list[dict]:
    """Read chronological_order.csv back into row dicts (for re-run skip)."""
    csv_path = out_dir / "chronological_order.csv"
    if not csv_path.exists():
        return []
    rows = []
    with csv_path.open("r", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            r["album"] = album_name
            rows.append(r)
    return rows


def _safe_album_prefix(album_name: str) -> str:
    """Filename-safe form of an album name. Whitespace runs collapse to a single
    underscore so 'Family Photos' becomes 'Family_Photos'."""
    return re.sub(r"\s+", "_", album_name.strip())


def rename_album_chronologically(out_dir: Path, album_name: str,
                                  rows: list[dict]) -> None:
    """Rename files in out_dir to <prefix>_NNNN.jpg in chronological order,
    where <prefix> is the album name with whitespace turned into underscores.
    Updates each row in-place: sets 'original_filename' and overwrites 'filename'.
    Writes .renamed_complete marker on success.

    Two-step temp rename so any single-name collision can't lose data."""
    prefix = _safe_album_prefix(album_name)
    # Refuse to proceed if a prior run left half-renamed temp files.
    stale = list(out_dir.glob("*.tmprename"))
    if stale:
        raise RuntimeError(
            f"{out_dir} has {len(stale)} stale .tmprename files from a prior "
            f"failed rename. Inspect and remove manually before re-running."
        )

    sorted_rows = sorted(rows, key=_chronological_sort_key)

    # Filter to rows whose files actually exist on disk BEFORE numbering, so the
    # final sequence is gap-free even if some rows reference missing files.
    present_rows = [r for r in sorted_rows if (out_dir / r["filename"]).exists()]

    plan = []
    for i, row in enumerate(present_rows, 1):
        # Preserve original extension so videos stay videos, JPGs stay JPGs.
        ext = Path(row["filename"]).suffix.lower() or ".jpg"
        final_name = f"{prefix}_{i:04d}{ext}"
        plan.append({
            "current": out_dir / row["filename"],
            "temp": out_dir / f"{final_name}.tmprename",
            "final": out_dir / final_name,
            "row": row,
            "final_name": final_name,
        })

    # Phase 1: move every original to its .tmprename name.
    for p in plan:
        p["current"].rename(p["temp"])

    # Phase 2: rename temps to final names; record original_filename on the row.
    for p in plan:
        p["temp"].rename(p["final"])
        p["row"]["original_filename"] = p["row"]["filename"]
        p["row"]["filename"] = p["final_name"]

    marker_lines = [f"{p['row']['original_filename']} -> {p['final_name']}"
                    for p in plan]
    (out_dir / ".renamed_complete").write_text(
        "\n".join(marker_lines) + "\n", encoding="utf-8",
    )


GLOBAL_FIELDS = ["album", "sequence_in_album", "filename", "original_filename",
                 "source_type", "best_date", "date_source", "confidence",
                 "exif_date", "exif_trust", "exif_trust_reason",
                 "estimated_date", "estimate_reason"]


def write_global_manifest(output_root: Path, all_rows: list[dict]) -> None:
    if not all_rows:
        return
    all_rows.sort(key=lambda r: _chronological_sort_key(r, prefix=(r["album"],)))
    seq: dict[str, int] = {}
    for r in all_rows:
        seq[r["album"]] = seq.get(r["album"], 0) + 1
        r["sequence_in_album"] = seq[r["album"]]
    with (output_root / "manifest.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=GLOBAL_FIELDS)
        w.writeheader()
        for r in all_rows:
            w.writerow({k: r.get(k, "") for k in GLOBAL_FIELDS})


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("input", type=Path, help="Root folder containing album subfolders")
    ap.add_argument("--output", type=Path, default=Path("processed"))
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"Upscale model for digital photos (default: {DEFAULT_MODEL})")
    ap.add_argument("--scan-model", default=DEFAULT_SCAN_MODEL,
                    help=f"Model used for images classified as scans "
                         f"(default: {DEFAULT_SCAN_MODEL}). Restoration-focused "
                         "model that handles scratches/dust better than the upscale "
                         "model. Set to '' to use the same model for everything.")
    ap.add_argument("--blend", type=float, default=DEFAULT_BLEND,
                    help="Blend AI with Lanczos-upscaled original. "
                         f"1.0=pure AI, 0.5=50/50 (default), 0.0=no AI")
    ap.add_argument("--saturation", type=float, default=DEFAULT_SATURATION,
                    help=f"Color saturation multiplier (default {DEFAULT_SATURATION}). "
                         "Try 1.10–1.15 if outputs look dull, 1.0 to disable.")
    ap.add_argument("--auto-color", type=float, default=DEFAULT_AUTO_COLOR,
                    help=f"Photoshop-style auto-color blend opacity (default {DEFAULT_AUTO_COLOR}). "
                         "Per-channel histogram stretch, blended over the image. "
                         "0.0 to disable, 1.0 for full effect.")
    ap.add_argument("--descratch", type=float, default=DEFAULT_DESCRATCH,
                    help=f"Vertical scratch removal strength for scans (default {DEFAULT_DESCRATCH}). "
                         "Detects long thin near-vertical lines (scanner artifacts and print "
                         "scratches) and inpaints them. ONLY applied to images classified as "
                         "scans — digital photos are never descratched. 0.0 to disable.")
    ap.add_argument("--polish", type=float, default=DEFAULT_POLISH,
                    help=f"Picsart-style final cleanup polish blend opacity (default {DEFAULT_POLISH}). "
                         "Edge-preserving smoothing + unsharp mask, applied as the LAST step "
                         "to give a clean polished look. 0.0 to disable, 1.0 for full effect.")
    ap.add_argument("--target-min", type=int, default=TARGET_MIN)
    ap.add_argument("--target-max", type=int, default=TARGET_MAX)
    ap.add_argument("--quality", type=int, default=JPEG_QUALITY)
    ap.add_argument("--skip-chronology", action="store_true",
                    help="Skip Claude vision date estimation (use EXIF only)")
    ap.add_argument("--api-model", default=DEFAULT_API_MODEL,
                    help=f"Claude model for chronology (default: {DEFAULT_API_MODEL})")
    ap.add_argument("--concurrency", type=int, default=5,
                    help="Concurrent Claude API calls (default: 5)")
    ap.add_argument("--trust-threshold", type=float, default=DEFAULT_TRUST_THRESHOLD,
                    help=f"EXIF date trust threshold (default: {DEFAULT_TRUST_THRESHOLD}). "
                         "Below this, an EXIF date is treated as suspicious "
                         "(scanner stamp, sentinel value, etc.) and re-estimated "
                         "via vision. Use 0.0 to trust every EXIF date as-is.")
    ap.add_argument("--force-rename", action="store_true",
                    help="Re-run chronology + rename on albums already marked "
                         "complete. Ignores .renamed_complete markers. Useful if "
                         "you added new photos or want to reprocess dates.")
    args = ap.parse_args()

    if not args.input.is_dir():
        raise SystemExit(f"Input not a directory: {args.input}")
    if not 0.0 <= args.blend <= 1.0:
        raise SystemExit(f"--blend must be 0.0-1.0, got {args.blend}")
    if args.saturation < 0.0:
        raise SystemExit(f"--saturation must be >= 0.0, got {args.saturation}")
    if not 0.0 <= args.auto_color <= 1.0:
        raise SystemExit(f"--auto-color must be 0.0-1.0, got {args.auto_color}")
    if not 0.0 <= args.descratch <= 1.0:
        raise SystemExit(f"--descratch must be 0.0-1.0, got {args.descratch}")
    if not 0.0 <= args.polish <= 1.0:
        raise SystemExit(f"--polish must be 0.0-1.0, got {args.polish}")

    register_format_openers()

    # ---- STEP 1: models ----
    print("=" * 64)
    print("STEP 1/3  Verify upscale models")
    print("=" * 64)
    binary = ensure_models(args.model, args.scan_model)
    print(f"  binary:     {binary}")
    print(f"  model:      {args.model}  (digital photos)")
    if args.scan_model and args.scan_model != args.model:
        print(f"  scan-model: {args.scan_model}  (auto-routed for scans)")
    print(f"  blend:      {args.blend}")
    print(f"  saturation: {args.saturation}")
    print(f"  auto-color: {args.auto_color}")
    print(f"  descratch:  {args.descratch}  (scans only)")
    print(f"  polish:     {args.polish}  (final cleanup pass)")

    albums = sorted(p for p in args.input.iterdir() if p.is_dir()) or [args.input]
    print(f"  found:  {len(albums)} album(s)")

    # ---- STEP 2: enhance ----
    print("\n" + "=" * 64)
    print("STEP 2/3  Upscale + clean + preserve metadata (slow part)")
    print("=" * 64)

    totals = {"upscaled": 0, "kept": 0, "passthrough": 0, "skipped": 0, "error": 0,
              "video_copied": 0, "video_skipped": 0}
    for album in albums:
        out_dir = args.output / album.name
        marker = out_dir / ".renamed_complete"
        if marker.exists() and not args.force_rename:
            print(f"  [{album.name}]  (already complete, skipping enhance)")
            continue
        c = process_album(album, out_dir, binary,
                          args.target_min, args.target_max, args.quality,
                          args.model, args.blend, args.saturation, args.auto_color,
                          args.scan_model or None, args.descratch, args.polish)
        vid_summary = ""
        if c.get("video_copied") or c.get("video_skipped"):
            vid_summary = (f"  vid+{c.get('video_copied', 0)}/"
                           f"~{c.get('video_skipped', 0)}")
        print(
            f"  [{album.name}]  upscaled {c['upscaled']:>4}  kept {c['kept']:>4}  "
            f"pass {c['passthrough']:>3}  skip {c['skipped']:>4}  err {c['error']:>3}"
            f"{vid_summary}"
        )
        for k, v in c.items():
            totals[k] = totals.get(k, 0) + v
    print(f"\n  totals: upscaled {totals['upscaled']}  kept {totals['kept']}  "
          f"passthrough {totals['passthrough']}  skipped {totals['skipped']}  "
          f"errors {totals['error']}  "
          f"videos copied {totals.get('video_copied', 0)}")

    # ---- STEP 3: chronology ----
    print("\n" + "=" * 64)
    print("STEP 3/3  Date estimation + chronological ordering")
    print("=" * 64)

    has_api_key = bool(os.getenv("ANTHROPIC_API_KEY"))
    do_chronology = has_api_key and not args.skip_chronology
    if not has_api_key:
        print("  ANTHROPIC_API_KEY not set — using EXIF dates only.")
        print("  (Set the env var and re-run to estimate dates for undated photos.)")
    elif args.skip_chronology:
        print("  --skip-chronology — using EXIF dates only.")
    else:
        print(f"  Will estimate dates via {args.api_model} (concurrency {args.concurrency})")

    all_rows: list[dict] = []
    for album in albums:
        out_dir = args.output / album.name
        if not out_dir.exists():
            continue

        marker = out_dir / ".renamed_complete"
        # Already complete? Just load its existing manifest (unless --force-rename).
        if marker.exists() and not args.force_rename:
            existing = read_existing_album_manifest(out_dir, album.name)
            all_rows.extend(existing)
            print(f"  [{album.name}]  (already complete, {len(existing)} images)")
            continue
        # If forcing, drop the marker so rename logic will run again.
        if marker.exists() and args.force_rename:
            marker.unlink()
            print(f"  [{album.name}]  --force-rename: dropping marker, re-doing rename")

        images = sorted(p for p in out_dir.iterdir() if p.suffix.lower() in IMAGE_EXTS)
        videos = sorted(p for p in out_dir.iterdir() if p.suffix.lower() in VIDEO_EXTS)
        rows: list[dict] = []
        # targets sent to vision: (path, suspicious_date_or_None, reason_or_None)
        vision_targets: list[tuple[Path, str | None, str | None]] = []
        dated_with_dates: list[tuple[Path, str]] = []
        n_suspicious = 0

        # Videos: no EXIF, no vision — use file mtime as the chronological anchor.
        for v in videos:
            mtime = datetime.fromtimestamp(v.stat().st_mtime).isoformat(timespec="seconds")
            rows.append({
                "album": album.name,
                "filename": v.name,
                "original_filename": v.name,
                "source_type": "video",
                "exif_date": "",
                "exif_trust": "",
                "exif_trust_reason": "video — uses file mtime",
                "estimated_date": "",
                "estimate_reason": "",
                "confidence": 0.7,
                "date_source": "file_mtime",
                "best_date": mtime,
            })

        for p in images:
            meta = read_metadata(p)
            trust, trust_reason = date_trust_score(meta)
            trusted = bool(meta.exif_date) and trust >= args.trust_threshold

            row = {
                "album": album.name,
                "filename": p.name,
                "original_filename": p.name,   # will stay equal until rename step
                "source_type": meta.type,      # "scan" | "digital" | "unknown"
                "exif_date": meta.exif_date or "",
                "exif_trust": f"{trust:.2f}",
                "exif_trust_reason": trust_reason,
                "estimated_date": "",
                "estimate_reason": "",
                "confidence": trust if trusted else "",
                "date_source": "exif" if trusted else "",
                "best_date": meta.exif_date if trusted else "",
            }
            rows.append(row)

            if trusted:
                dated_with_dates.append((p, meta.exif_date))
            else:
                # Send to vision. Include suspicious EXIF date + source classification.
                if meta.exif_date:
                    n_suspicious += 1
                    vision_targets.append(
                        (p, meta.exif_date[:10], trust_reason, meta.type)
                    )
                else:
                    vision_targets.append((p, None, None, meta.type))

        if n_suspicious:
            print(f"  [{album.name}]  {n_suspicious} suspicious EXIF date(s) "
                  f"will be re-estimated via vision")

        # Estimate dates for undated + suspicious images.
        if do_chronology and vision_targets:
            try:
                from pipeline.chronology import estimate_album
                with tqdm(total=len(vision_targets),
                          desc=f"  {album.name} (vision)", unit="img") as pbar:
                    results = estimate_album(
                        album.name, vision_targets, dated_with_dates,
                        model=args.api_model, concurrency=args.concurrency,
                        progress_cb=pbar.update,
                    )
                for row in rows:
                    if row["filename"] in results:
                        r = results[row["filename"]]
                        if "error" in r:
                            row["estimate_reason"] = r["error"]
                        else:
                            row["estimated_date"] = r.get("date", "")
                            row["estimate_reason"] = r.get("reason", "")
                            row["confidence"] = r.get("confidence", "")
                            if r.get("date"):
                                row["date_source"] = "ai_estimate"
                                row["best_date"] = r["date"]
            except Exception as e:
                print(f"  WARNING: chronology failed for {album.name}: "
                      f"{type(e).__name__}: {e}")

        # Rename to <album>_NNNN.jpg in chronological order, then write manifest.
        try:
            rename_album_chronologically(out_dir, album.name, rows)
        except Exception as e:
            print(f"  WARNING: rename failed for {album.name}: "
                  f"{type(e).__name__}: {e}  (manifest still written)")

        write_album_manifest(out_dir, rows)
        all_rows.extend(rows)

        n_dated = sum(1 for r in rows if r["best_date"])
        print(f"  [{album.name}]  total {len(rows):>4}  with date {n_dated:>4}  "
              f"({100*n_dated//max(len(rows),1)}%)  → renamed "
              f"{_safe_album_prefix(album.name)}_NNNN.jpg")

    write_global_manifest(args.output, all_rows)

    n_total = len(all_rows)
    n_dated = sum(1 for r in all_rows if r["best_date"])
    n_exif = sum(1 for r in all_rows if r["date_source"] == "exif")
    n_ai = sum(1 for r in all_rows if r["date_source"] == "ai_estimate")
    print(f"\n  global: {n_total} images  |  dated {n_dated}  "
          f"(exif {n_exif}, ai {n_ai})")
    print(f"\n  Manifest: {args.output / 'manifest.csv'}")
    print("\nDONE.")


if __name__ == "__main__":
    main()
