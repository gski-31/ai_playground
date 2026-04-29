"""
Date estimation via Claude vision. For images with no EXIF date, ask the model
to estimate when they were taken — using the album's name and the date range
of dated photos in the same album as priors.

Returns dict mapping filename → {date, confidence, reason} or {error}.
"""
from __future__ import annotations

import asyncio
import base64
import json
from io import BytesIO
from pathlib import Path

from PIL import Image

CHRONOLOGY_SYSTEM = """You are dating personal photographs by visual evidence.

CRITICAL — visual evidence ALWAYS overrides album context for era markers.
Personal albums routinely mix scanned old prints with recent digital photos.
The album's anchor date range tells you when the DIGITAL photos were taken;
it does NOT bound when older scanned photos in the same album could be from.

If a photo is unmistakably:
  - black & white film / sepia toned
  - on Polaroid stock (square, white border)
  - a faded color print with characteristic 1970s-1980s cast
  - showing pre-digital subject matter (rotary phones, period cars, vintage clothing)
  - clearly a scanned physical print (visible paper texture, dust, scratches, deckle edges)
...then date it from THAT visual era, even if other photos in the album are
dated decades later. Don't anchor it to the digital range.

Use the anchor range only when the photo has no strong era markers of its own.

Use these signals (in rough order of reliability):

PHOTO MEDIUM
- Daguerreotype/tintype: 1840s-1870s
- Cabinet card, sepia: 1880s-1910s
- Black & white film: 1900s-1960s
- Color film with warm/orange cast: 1950s-1980s
- Polaroid (white border, square): 1960s-1990s
- Sharp glossy color print: 1980s-2000s
- Digital (sharp, no grain): late 1990s onward
- Smartphone aesthetic (high res, square crops): 2007 onward

CLOTHING & HAIR
- Beehive, mod: 1960s
- Bell bottoms, wide collars, long hair: 1970s
- Big hair, shoulder pads, neon: 1980s
- Grunge, flannel, baggy jeans: 1990s
- Low-rise jeans, layered tees, frosted tips: early 2000s
- Skinny jeans, hipster aesthetic: 2010s
- Athleisure, oversized fit: 2020s

VISIBLE TECHNOLOGY
- CRT TVs, rotary phones, boomboxes: pre-1995
- Boxy beige PCs, VHS: 1990s
- Flip phones, iPods: 2000s
- iPhones/smartphones in hand: 2007+
- Specific car body styles in background

PRINT / PHYSICAL
- Round-corner prints with white border: 1940s-1960s
- Date stamp on print edge: very useful if visible (often white text in corner)
- Wallet photo serrated edges: 1970s-1990s

ALWAYS respond with one line of valid JSON, nothing else:
{"date": "YYYY-MM-DD", "confidence": 0.0-1.0, "reason": "brief"}

When you only know the year, use mid-year (YYYY-06-15).
When you only know the decade, use mid-decade mid-year (1985-06-15 for "1980s").

Confidence guide:
- 0.9+: visible date stamp or extremely clear cues with tight year
- 0.7-0.9: confident decade + narrowed by visual + album context
- 0.5-0.7: confident decade only
- 0.3-0.5: rough era (e.g., "70s vs 80s")
- below 0.3: wild guess"""

CHRONOLOGY_USER = """Album: "{album}"
{anchor_context}

Estimate when this photo was taken."""

SUSPICIOUS_DATE_NOTE = (
    "\n\nNOTE: This image has an EXIF date of {date} ({reason}). This date is "
    "untrustworthy — likely when the photo was scanned or assigned by software, "
    "not when it was actually taken. Use it as a possible UPPER bound (the photo "
    "can't be from after that scan date) but rely primarily on visual evidence "
    "to estimate the original capture date."
)

SCAN_SOURCE_NOTE = (
    "\n\nClassification: this image is a SCANNED PRINT — an older physical "
    "photograph that was digitized later. Estimate when the original photo "
    "was taken (not when it was scanned). Scanned prints in mixed albums "
    "are usually significantly older than the digital photos."
)


def _image_to_b64(path: Path, max_edge: int = 1024) -> str:
    with Image.open(path) as img:
        img.thumbnail((max_edge, max_edge))
        buf = BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=85)
        return base64.standard_b64encode(buf.getvalue()).decode()


def _parse_response(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    return json.loads(text)


async def _estimate_one(client, sem, model, image_path, album, anchor_context,
                          suspicious_date=None, suspicious_reason=None,
                          source_type=None):
    async with sem:
        try:
            img_b64 = await asyncio.to_thread(_image_to_b64, image_path)
        except Exception as e:
            return {"error": f"image read: {type(e).__name__}: {e}"}

        user_text = CHRONOLOGY_USER.format(album=album, anchor_context=anchor_context)
        if source_type == "scan":
            user_text += SCAN_SOURCE_NOTE
        if suspicious_date:
            user_text += SUSPICIOUS_DATE_NOTE.format(
                date=suspicious_date, reason=suspicious_reason or "untrusted",
            )

        try:
            msg = await client.messages.create(
                model=model,
                max_tokens=200,
                system=[{
                    "type": "text",
                    "text": CHRONOLOGY_SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": img_b64,
                        }},
                        {"type": "text", "text": user_text},
                    ],
                }],
            )
            return _parse_response(msg.content[0].text)
        except json.JSONDecodeError as e:
            return {"error": f"json parse: {e}", "raw": msg.content[0].text[:200]}
        except Exception as e:
            return {"error": f"api: {type(e).__name__}: {e}"}


async def _estimate_album_async(album_name, targets, dated_with_dates,
                                  model, concurrency, progress_cb=None):
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        raise RuntimeError("anthropic SDK not installed. Run: pip install anthropic")

    client = AsyncAnthropic()
    sem = asyncio.Semaphore(concurrency)

    if dated_with_dates:
        dates = sorted(d for _, d in dated_with_dates)
        anchor_context = (
            f"Other photos in this album are dated between {dates[0][:10]} "
            f"and {dates[-1][:10]} ({len(dated_with_dates)} trusted dated photos)."
        )
    else:
        anchor_context = "No other photos in this album have trustworthy dates."

    async def wrapped(target):
        path, suspicious_date, suspicious_reason, source_type = target
        result = await _estimate_one(
            client, sem, model, path, album_name, anchor_context,
            suspicious_date=suspicious_date, suspicious_reason=suspicious_reason,
            source_type=source_type,
        )
        if progress_cb:
            progress_cb()
        return result

    results = await asyncio.gather(*(wrapped(t) for t in targets))
    return {t[0].name: r for t, r in zip(targets, results)}


def estimate_album(
    album_name: str,
    targets: list[tuple[Path, str | None, str | None, str | None]],
    dated_with_dates: list[tuple[Path, str]],
    model: str = "claude-sonnet-4-6",
    concurrency: int = 5,
    progress_cb=None,
) -> dict[str, dict]:
    """Synchronous wrapper for the async estimator.

    `targets` is a list of 4-tuples: (path, suspicious_exif_date,
    suspicious_reason, source_type).
    - source_type: "scan" | "digital" | "unknown" | None — tells the model
      whether this image is a scanned print or a digital camera photo.
    - suspicious_exif_date: the untrusted date string, or None.
    - suspicious_reason: why we don't trust it, or None.
    """
    if not targets:
        return {}
    return asyncio.run(_estimate_album_async(
        album_name, targets, dated_with_dates,
        model, concurrency, progress_cb,
    ))
