"""TMDB movie search + poster download."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import requests


API_BASE = "https://api.themoviedb.org/3"
IMG_BASE = "https://image.tmdb.org/t/p"


@dataclass
class MovieMatch:
    tmdb_id: int
    title: str
    year: str
    overview: str
    poster_path: str  # like "/abc.jpg", needs IMG_BASE prefix
    vote_average: float = 0.0

    def display(self) -> str:
        return f"{self.title} ({self.year})" if self.year else self.title

    def poster_url(self, size: str = "w500") -> Optional[str]:
        if not self.poster_path:
            return None
        return f"{IMG_BASE}/{size}{self.poster_path}"


class TMDB:
    def __init__(self, access_token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        })

    def search_movie(self, query: str, year: Optional[int] = None, limit: int = 8) -> List[MovieMatch]:
        params = {"query": query, "include_adult": "false", "language": "en-US"}
        if year:
            params["year"] = str(year)
        r = self.session.get(f"{API_BASE}/search/movie", params=params, timeout=15)
        r.raise_for_status()
        results = r.json().get("results", [])[:limit]
        return [
            MovieMatch(
                tmdb_id=m["id"],
                title=m.get("title") or m.get("original_title", ""),
                year=(m.get("release_date") or "")[:4],
                overview=m.get("overview", ""),
                poster_path=m.get("poster_path") or "",
                vote_average=float(m.get("vote_average") or 0),
            )
            for m in results
        ]

    def download_poster(self, match: MovieMatch, dest: Path, size: str = "w500") -> Optional[Path]:
        url = match.poster_url(size)
        if not url:
            return None
        r = self.session.get(url, timeout=30)
        r.raise_for_status()
        dest.write_bytes(r.content)
        return dest


_TITLE_CLEAN_RE = re.compile(
    r"\b(1080p|720p|2160p|4k|uhd|bluray|bdrip|webrip|web-dl|x264|x265|hevc|h264|h265|aac|ac3|dts|atmos|remux|extended|directors?[. ]cut)\b",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_SEPARATORS = re.compile(r"[._]+")


def guess_title_and_year(filename: str) -> tuple[str, Optional[int]]:
    """Extract a likely movie title and year from a messy filename like 'The.Matrix.1999.1080p.BluRay.x265.mkv'."""
    stem = Path(filename).stem
    stem = _SEPARATORS.sub(" ", stem)
    year_match = _YEAR_RE.search(stem)
    year = int(year_match.group(0)) if year_match else None
    if year_match:
        stem = stem[: year_match.start()]
    stem = _TITLE_CLEAN_RE.sub(" ", stem)
    title = " ".join(stem.split()).strip(" -[]()")
    return title, year
