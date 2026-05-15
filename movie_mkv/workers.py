"""Qt worker objects for off-thread probe / TMDB search / encode / dependency download."""
from __future__ import annotations

import tempfile
import traceback
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QObject, Signal, Slot

import deps
from encode import EncodeJob, MovieMeta, attach_poster, run_encode, set_container_title
from probe import ProbeResult, probe
from tmdb import MovieMatch, TMDB, guess_title_and_year


class DepsWorker(QObject):
    progress = Signal(str, int)
    done = Signal(dict)
    failed = Signal(str)

    @Slot()
    def run(self) -> None:
        try:
            tools = deps.ensure_all(progress=lambda label, pct: self.progress.emit(label, pct))
            self.done.emit({k: str(v) for k, v in tools.items()})
        except Exception as e:
            self.failed.emit(f"{e}\n\n{traceback.format_exc()}")


class ProbeWorker(QObject):
    done = Signal(object, object)   # (Path source, ProbeResult)
    failed = Signal(object, str)    # (Path source, error msg)

    def __init__(self, ffprobe_path: Path, files: List[Path]) -> None:
        super().__init__()
        self.ffprobe_path = ffprobe_path
        self.files = files

    @Slot()
    def run(self) -> None:
        for f in self.files:
            try:
                result = probe(self.ffprobe_path, f)
                self.done.emit(f, result)
            except Exception as e:
                self.failed.emit(f, str(e))


class TMDBWorker(QObject):
    done = Signal(object, list)        # (Path source, List[MovieMatch])
    poster_ready = Signal(object, bytes)  # (Path source, image bytes)
    failed = Signal(object, str)

    def __init__(self, access_token: str, source: Path, query: str, year: Optional[int]) -> None:
        super().__init__()
        self.access_token = access_token
        self.source = source
        self.query = query
        self.year = year

    @Slot()
    def run(self) -> None:
        try:
            t = TMDB(self.access_token)
            results = t.search_movie(self.query, year=self.year)
            self.done.emit(self.source, results)
            if results and results[0].poster_path:
                url = results[0].poster_url("w342")
                if url:
                    import requests
                    r = requests.get(url, timeout=20)
                    if r.ok:
                        self.poster_ready.emit(self.source, r.content)
        except Exception as e:
            self.failed.emit(self.source, str(e))


class EncodeWorker(QObject):
    file_started = Signal(object)        # Path
    file_progress = Signal(object, float)  # (Path, 0..1)
    file_done = Signal(object, object)   # (Path, output Path)
    file_failed = Signal(object, str)
    log = Signal(str)
    all_done = Signal()

    def __init__(self, ffmpeg: Path, mkvpropedit: Path, jobs: List[EncodeJob]) -> None:
        super().__init__()
        self.ffmpeg = ffmpeg
        self.mkvpropedit = mkvpropedit
        self.jobs = jobs
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    @Slot()
    def run(self) -> None:
        for job in self.jobs:
            if self._stop:
                break
            self.file_started.emit(job.source)
            try:
                run_encode(
                    self.ffmpeg,
                    job,
                    on_progress=lambda p, src=job.source: self.file_progress.emit(src, p),
                    on_log=lambda s: self.log.emit(s),
                )
                # Post-encode: attach poster + container title via mkvpropedit
                if job.meta:
                    title = f"{job.meta.title} ({job.meta.year})" if job.meta.year else job.meta.title
                    if title.strip():
                        try:
                            set_container_title(self.mkvpropedit, job.output, title)
                        except Exception as e:
                            self.log.emit(f"[warn] set container title failed: {e}")
                    if job.meta.poster_path and Path(job.meta.poster_path).exists():
                        try:
                            attach_poster(self.mkvpropedit, job.output, Path(job.meta.poster_path))
                        except Exception as e:
                            self.log.emit(f"[warn] attach poster failed: {e}")
                self.file_done.emit(job.source, job.output)
            except Exception as e:
                self.file_failed.emit(job.source, str(e))
        self.all_done.emit()
