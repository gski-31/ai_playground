"""PySide6 GUI for the movie -> HEVC MKV encoder.

Layout:
  Top: queue toolbar
  Middle (splitter): queue list  |  detail panel for selected item
  Bottom: progress + Start/Stop + log
"""
from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtGui import QAction, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSlider,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import config as cfg_mod
import deps
from encode import EncodeJob, MovieMeta
from probe import ProbeResult
from tmdb import MovieMatch, guess_title_and_year
from workers import DepsWorker, EncodeWorker, ProbeWorker, TMDBWorker


VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".mov", ".avi", ".ts", ".m2ts", ".webm", ".wmv"}


@dataclass
class QueueItem:
    source: Path
    status: str = "pending"   # pending/probing/ready/queued/encoding/done/error
    output: Optional[Path] = None
    probe: Optional[ProbeResult] = None
    audio_index: Optional[int] = None         # absolute stream index of chosen audio
    sub_indices: List[int] = field(default_factory=list)   # absolute indices of chosen internal subs
    extra_srts: List[Path] = field(default_factory=list)   # external .srt files appended after internals
    forced_sub_idx: Optional[int] = None      # position within combined output (sub_indices + extra_srts)
    meta: Optional[MovieMeta] = None
    tmdb_results: List[MovieMatch] = field(default_factory=list)
    error: str = ""


class DepDownloadDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Downloading tools")
        self.setModal(True)
        self.resize(420, 140)
        v = QVBoxLayout(self)
        self.label = QLabel("Preparing…")
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        v.addWidget(self.label)
        v.addWidget(self.bar)

        self.thread = QThread(self)
        self.worker = DepsWorker()
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self._progress)
        self.worker.done.connect(self._done)
        self.worker.failed.connect(self._failed)
        self.result_paths: Dict[str, str] = {}

    def exec_download(self) -> bool:
        self.thread.start()
        ok = self.exec() == QDialog.DialogCode.Accepted
        self.thread.quit()
        self.thread.wait(2000)
        return ok

    @Slot(str, int)
    def _progress(self, label: str, pct: int) -> None:
        self.label.setText(label)
        self.bar.setValue(pct)

    @Slot(dict)
    def _done(self, paths: dict) -> None:
        self.result_paths = paths
        self.accept()

    @Slot(str)
    def _failed(self, msg: str) -> None:
        QMessageBox.critical(self, "Download failed", msg)
        self.reject()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Movie → HEVC MKV")
        self.resize(1180, 780)

        self.cfg = cfg_mod.load()
        self.items: Dict[str, QueueItem] = {}     # key = str(source)
        self.encode_thread: Optional[QThread] = None
        self.encode_worker: Optional[EncodeWorker] = None
        self.probe_thread: Optional[QThread] = None
        self.tmdb_thread: Optional[QThread] = None
        self.tools: Dict[str, Path] = {}
        self._tmp_dir = Path(tempfile.mkdtemp(prefix="movie_mkv_"))

        self._build_ui()
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Checking tools…")
        # Defer dep check until the window is shown
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._check_tools)

    # ---------- UI construction ----------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # Top toolbar
        toolbar = QHBoxLayout()
        self.btn_add = QPushButton("Add files…")
        self.btn_remove = QPushButton("Remove")
        self.btn_clear = QPushButton("Clear")
        self.btn_outdir = QPushButton("Output folder…")
        self.lbl_outdir = QLabel(self.cfg.output_dir or "(same as source)")
        self.lbl_outdir.setStyleSheet("color: #888;")
        toolbar.addWidget(self.btn_add)
        toolbar.addWidget(self.btn_remove)
        toolbar.addWidget(self.btn_clear)
        toolbar.addSpacing(20)
        toolbar.addWidget(self.btn_outdir)
        toolbar.addWidget(self.lbl_outdir, 1)
        root.addLayout(toolbar)

        # Splitter: queue | detail
        split = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(split, 1)

        self.queue_list = QListWidget()
        self.queue_list.setMinimumWidth(280)
        split.addWidget(self.queue_list)

        self.detail = self._build_detail_panel()
        split.addWidget(self.detail)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)

        # Bottom: progress + start + log
        bottom = QVBoxLayout()
        row = QHBoxLayout()
        self.btn_start = QPushButton("Start batch")
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setEnabled(False)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress_label = QLabel("Idle")
        row.addWidget(self.btn_start)
        row.addWidget(self.btn_stop)
        row.addWidget(self.progress, 1)
        row.addWidget(self.progress_label)
        bottom.addLayout(row)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(180)
        self.log.setStyleSheet("font-family: Consolas, monospace; font-size: 11px;")
        bottom.addWidget(self.log)
        root.addLayout(bottom)

        # Wire signals
        self.btn_add.clicked.connect(self._on_add)
        self.btn_remove.clicked.connect(self._on_remove)
        self.btn_clear.clicked.connect(self._on_clear)
        self.btn_outdir.clicked.connect(self._on_outdir)
        self.queue_list.currentItemChanged.connect(self._on_select)
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop.clicked.connect(self._on_stop)

    def _build_detail_panel(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        self.lbl_source = QLabel("(no file selected)")
        self.lbl_source.setStyleSheet("font-weight: bold;")
        v.addWidget(self.lbl_source)

        # TMDB metadata box
        meta_box = QGroupBox("Movie metadata (TMDB)")
        meta_layout = QHBoxLayout(meta_box)
        # left col: search form
        meta_form_w = QWidget()
        form = QFormLayout(meta_form_w)
        self.tmdb_title = QLineEdit()
        self.tmdb_year = QLineEdit()
        self.tmdb_year.setPlaceholderText("optional")
        self.btn_tmdb_search = QPushButton("Search")
        srow = QHBoxLayout()
        srow.addWidget(self.tmdb_title)
        srow.addWidget(self.tmdb_year)
        srow.addWidget(self.btn_tmdb_search)
        form.addRow("Title / Year", _wrap(srow))
        self.tmdb_results = QComboBox()
        self.tmdb_results.setMinimumWidth(360)
        form.addRow("Match", self.tmdb_results)
        self.lbl_overview = QLabel("")
        self.lbl_overview.setWordWrap(True)
        self.lbl_overview.setMaximumWidth(420)
        form.addRow("Overview", self.lbl_overview)
        meta_layout.addWidget(meta_form_w, 1)
        # right col: poster
        self.poster = QLabel()
        self.poster.setFixedSize(154, 231)
        self.poster.setStyleSheet("background:#222; border:1px solid #444;")
        self.poster.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.poster.setText("no poster")
        meta_layout.addWidget(self.poster)
        v.addWidget(meta_box)

        # Audio box
        audio_box = QGroupBox("Audio")
        a = QVBoxLayout(audio_box)
        self.audio_combo = QComboBox()
        a.addWidget(self.audio_combo)
        v.addWidget(audio_box)

        # Subtitles box
        sub_box = QGroupBox("Subtitles  —  check to include · Ctrl+click a checked row to mark it forced (●)")
        s = QVBoxLayout(sub_box)
        self.sub_list = QListWidget()
        self.sub_list.setMaximumHeight(180)
        s.addWidget(self.sub_list)
        srt_row = QHBoxLayout()
        self.btn_add_srt = QPushButton("Add SRT file…")
        self.btn_remove_srt = QPushButton("Remove selected external SRT")
        srt_row.addWidget(self.btn_add_srt)
        srt_row.addWidget(self.btn_remove_srt)
        srt_row.addStretch(1)
        s.addLayout(srt_row)
        v.addWidget(sub_box)

        # Output settings
        out_box = QGroupBox("Output")
        o = QFormLayout(out_box)
        self.height_combo = QComboBox()
        for h in (2160, 1440, 1080, 720):
            self.height_combo.addItem(f"{h}p", h)
        self.height_combo.setCurrentText(f"{self.cfg.max_height}p")
        self.bitrate_spin = QSpinBox()
        self.bitrate_spin.setRange(1000, 10000)
        self.bitrate_spin.setSingleStep(250)
        self.bitrate_spin.setSuffix(" kbps")
        self.bitrate_spin.setValue(self.cfg.video_kbps)
        self.bitrate_spin.setToolTip("Average video bitrate. 2500 ≈ 1.1 GB/hr; 2800 ≈ 1.3 GB/hr; 3500 ≈ 1.6 GB/hr.")
        self.downmix = QCheckBox("Downmix surround to stereo (tablet-friendly)")
        self.downmix.setChecked(self.cfg.downmix_stereo)
        o.addRow("Max height", self.height_combo)
        o.addRow("Video bitrate (2-pass VBR)", self.bitrate_spin)
        o.addRow(self.downmix)
        v.addWidget(out_box)

        v.addStretch(1)

        # Disable until a file is selected
        for widget in (
            self.tmdb_title, self.tmdb_year, self.btn_tmdb_search, self.tmdb_results,
            self.audio_combo, self.sub_list, self.height_combo, self.bitrate_spin, self.downmix,
            self.btn_add_srt, self.btn_remove_srt,
        ):
            widget.setEnabled(False)

        # Wire detail-panel signals
        self.btn_tmdb_search.clicked.connect(self._on_tmdb_search)
        self.tmdb_results.currentIndexChanged.connect(self._on_tmdb_pick)
        self.audio_combo.currentIndexChanged.connect(self._on_audio_change)
        self.sub_list.itemChanged.connect(self._on_sub_check_change)
        self.sub_list.itemClicked.connect(self._on_sub_click_forced)
        self.height_combo.currentIndexChanged.connect(self._on_output_change)
        self.bitrate_spin.valueChanged.connect(self._on_output_change)
        self.downmix.stateChanged.connect(self._on_output_change)
        self.btn_add_srt.clicked.connect(self._on_add_srt)
        self.btn_remove_srt.clicked.connect(self._on_remove_srt)
        return w

    # ---------- Dep check ----------

    def _check_tools(self) -> None:
        missing = deps.missing_tools()
        if not missing:
            self.tools = {k: v for k, v in deps.locate_all().items() if v}
            self.statusBar().showMessage("Tools ready.")
            return

        resp = QMessageBox.question(
            self,
            "Missing tools",
            f"Need to download: {', '.join(missing)}.\n\n"
            "ffmpeg (~80 MB) and MKVToolNix (~50 MB) will be saved to your %APPDATA%\\movie_mkv\\tools folder.\n\n"
            "Download now?",
        )
        if resp != QMessageBox.StandardButton.Yes:
            self.statusBar().showMessage("Tools missing — cannot encode until they're installed.")
            return
        dlg = DepDownloadDialog(self)
        if dlg.exec_download():
            self.tools = {k: Path(v) for k, v in dlg.result_paths.items()}
            self.statusBar().showMessage("Tools ready.")
        else:
            self.statusBar().showMessage("Download cancelled or failed.")

    # ---------- Queue actions ----------

    @Slot()
    def _on_add(self) -> None:
        filters = "Video files (*.mkv *.mp4 *.m4v *.mov *.avi *.ts *.m2ts *.webm *.wmv);;All files (*.*)"
        files, _ = QFileDialog.getOpenFileNames(self, "Add video files", "", filters)
        new_paths = [Path(f) for f in files if f]
        if not new_paths:
            return
        added: List[Path] = []
        for p in new_paths:
            key = str(p)
            if key in self.items:
                continue
            self.items[key] = QueueItem(source=p)
            it = QListWidgetItem(f"⏳  {p.name}")
            it.setData(Qt.ItemDataRole.UserRole, key)
            self.queue_list.addItem(it)
            added.append(p)
        if added:
            self._probe_files(added)

    @Slot()
    def _on_remove(self) -> None:
        row = self.queue_list.currentRow()
        if row < 0:
            return
        it = self.queue_list.takeItem(row)
        key = it.data(Qt.ItemDataRole.UserRole)
        self.items.pop(key, None)

    @Slot()
    def _on_clear(self) -> None:
        self.queue_list.clear()
        self.items.clear()

    @Slot()
    def _on_outdir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Choose output folder", self.cfg.output_dir or "")
        if d:
            self.cfg.output_dir = d
            cfg_mod.save(self.cfg)
            self.lbl_outdir.setText(d)

    def _probe_files(self, paths: List[Path]) -> None:
        ffprobe = self.tools.get("ffprobe")
        if not ffprobe:
            self._log("ffprobe not available — install tools first.")
            return
        # one-shot worker per add batch
        worker = ProbeWorker(ffprobe, paths)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_probe_done)
        worker.failed.connect(self._on_probe_failed)
        worker.done.connect(lambda *_: None)  # keep refs

        def _cleanup() -> None:
            thread.quit()
            thread.wait(1000)

        # Stop the thread when all files are probed. Probes are emitted one-per-file,
        # so chain on the worker finishing the loop.
        def _maybe_finish(src: Path, _result) -> None:
            # last file?
            if src == paths[-1]:
                _cleanup()

        worker.done.connect(_maybe_finish)
        worker.failed.connect(lambda src, _msg: _maybe_finish(src, None))
        # hold refs so they don't get GC'd
        thread._worker = worker
        thread.start()
        self.probe_thread = thread

    @Slot(object, object)
    def _on_probe_done(self, source: Path, result: ProbeResult) -> None:
        key = str(source)
        item = self.items.get(key)
        if not item:
            return
        item.probe = result
        # Default selections
        best_audio = result.best_english_audio()
        if best_audio:
            item.audio_index = best_audio.index
        chosen_set = {s.index for s in result.preferred_subs()}
        # Keep sub_indices in probe order so forced-position bookkeeping is stable.
        item.sub_indices = [s.index for s in result.subs if s.index in chosen_set]
        item.forced_sub_idx = None
        for i, abs_idx in enumerate(item.sub_indices):
            src = next((s for s in result.subs if s.index == abs_idx), None)
            if src and src.is_forced_only():
                item.forced_sub_idx = i
                break
        item.status = "ready"
        self._update_queue_row(key)

        # auto-search TMDB
        title, year = guess_title_and_year(source.name)
        if title:
            self._tmdb_search(source, title, year)

        # refresh detail if this file is currently selected
        if self._selected_key() == key:
            self._refresh_detail()

    @Slot(object, str)
    def _on_probe_failed(self, source: Path, msg: str) -> None:
        key = str(source)
        item = self.items.get(key)
        if not item:
            return
        item.status = "error"
        item.error = msg
        self._log(f"[probe error] {source.name}: {msg}")
        self._update_queue_row(key)

    # ---------- TMDB ----------

    def _tmdb_search(self, source: Path, title: str, year: Optional[int]) -> None:
        if not self.cfg.tmdb_access_token:
            self._log("No TMDB access token configured.")
            return
        worker = TMDBWorker(self.cfg.tmdb_access_token, source, title, year)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(self._on_tmdb_done)
        worker.poster_ready.connect(self._on_poster_ready)
        worker.failed.connect(lambda src, m: self._log(f"[tmdb] {src.name}: {m}"))
        thread._worker = worker
        thread.start()
        # don't store — let it GC after emit; keep ref through worker bound to thread

    @Slot()
    def _on_tmdb_search(self) -> None:
        key = self._selected_key()
        if not key:
            return
        item = self.items[key]
        title = self.tmdb_title.text().strip()
        year_text = self.tmdb_year.text().strip()
        year = int(year_text) if year_text.isdigit() else None
        if title:
            self._tmdb_search(item.source, title, year)

    @Slot(object, list)
    def _on_tmdb_done(self, source: Path, matches: List[MovieMatch]) -> None:
        key = str(source)
        item = self.items.get(key)
        if not item:
            return
        item.tmdb_results = matches
        if matches:
            top = matches[0]
            item.meta = MovieMeta(
                title=top.title, year=top.year, overview=top.overview,
                tmdb_id=top.tmdb_id, poster_path=None,
            )
        if self._selected_key() == key:
            self._refresh_tmdb_ui()

    @Slot(object, bytes)
    def _on_poster_ready(self, source: Path, data: bytes) -> None:
        key = str(source)
        item = self.items.get(key)
        if not item or not item.meta:
            return
        # save to disk so ffmpeg/mkvpropedit can use it later
        dest = self._tmp_dir / f"{source.stem}_poster.jpg"
        dest.write_bytes(data)
        item.meta.poster_path = dest
        if self._selected_key() == key:
            self._set_poster_pixmap(data)

    @Slot(int)
    def _on_tmdb_pick(self, idx: int) -> None:
        key = self._selected_key()
        if not key or idx < 0:
            return
        item = self.items[key]
        if idx >= len(item.tmdb_results):
            return
        m = item.tmdb_results[idx]
        item.meta = MovieMeta(title=m.title, year=m.year, overview=m.overview, tmdb_id=m.tmdb_id)
        self.lbl_overview.setText(m.overview)
        # fetch poster for new pick
        if m.poster_path:
            try:
                import requests
                r = requests.get(m.poster_url("w342"), timeout=20)
                if r.ok:
                    self._on_poster_ready(item.source, r.content)
            except Exception as e:
                self._log(f"[poster] {e}")

    # ---------- Selection / detail refresh ----------

    def _selected_key(self) -> Optional[str]:
        it = self.queue_list.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    @Slot()
    def _on_select(self) -> None:
        self._refresh_detail()

    def _refresh_detail(self) -> None:
        key = self._selected_key()
        if not key:
            return
        item = self.items[key]
        self.lbl_source.setText(item.source.name)

        # enable widgets if probe is done
        enabled = item.probe is not None
        for widget in (
            self.tmdb_title, self.tmdb_year, self.btn_tmdb_search, self.tmdb_results,
            self.audio_combo, self.sub_list, self.height_combo, self.bitrate_spin, self.downmix,
            self.btn_add_srt, self.btn_remove_srt,
        ):
            widget.setEnabled(enabled)
        if not enabled:
            return

        # populate title/year guess
        title, year = guess_title_and_year(item.source.name)
        self.tmdb_title.setText(title)
        self.tmdb_year.setText(str(year) if year else "")
        self._refresh_tmdb_ui()

        # audio combo
        self.audio_combo.blockSignals(True)
        self.audio_combo.clear()
        for a in item.probe.audio:
            self.audio_combo.addItem(a.label(), a.index)
        if item.audio_index is not None:
            for i in range(self.audio_combo.count()):
                if self.audio_combo.itemData(i) == item.audio_index:
                    self.audio_combo.setCurrentIndex(i)
                    break
        self.audio_combo.blockSignals(False)

        # subtitles — internal first, external SRTs appended
        self.sub_list.blockSignals(True)
        self.sub_list.clear()
        for s in item.probe.subs:
            li = QListWidgetItem(s.label())
            li.setFlags(li.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = s.index in item.sub_indices
            li.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            li.setData(Qt.ItemDataRole.UserRole, ("internal", s.index))
            self.sub_list.addItem(li)
        for p in item.extra_srts:
            li = QListWidgetItem(f"📄 [external] {p.name}")
            # externals don't carry a checkbox — once added, they're always included
            li.setData(Qt.ItemDataRole.UserRole, ("external", str(p)))
            self.sub_list.addItem(li)
        # mark forced row
        if item.forced_sub_idx is not None:
            row = self._forced_idx_to_row(item)
            if row is not None and 0 <= row < self.sub_list.count():
                li = self.sub_list.item(row)
                li.setText("● " + li.text() + "  [auto-show: foreign dialogue]")
        self.sub_list.blockSignals(False)

    def _refresh_tmdb_ui(self) -> None:
        key = self._selected_key()
        if not key:
            return
        item = self.items[key]
        self.tmdb_results.blockSignals(True)
        self.tmdb_results.clear()
        for m in item.tmdb_results:
            self.tmdb_results.addItem(m.display(), m.tmdb_id)
        self.tmdb_results.blockSignals(False)
        if item.meta:
            self.lbl_overview.setText(item.meta.overview or "")
            if item.meta.poster_path and Path(item.meta.poster_path).exists():
                self._set_poster_pixmap(Path(item.meta.poster_path).read_bytes())
            else:
                self.poster.clear()
                self.poster.setText("no poster")
        else:
            self.lbl_overview.setText("")
            self.poster.clear()
            self.poster.setText("no poster")

    def _set_poster_pixmap(self, data: bytes) -> None:
        img = QImage.fromData(data)
        if not img.isNull():
            pix = QPixmap.fromImage(img).scaled(
                self.poster.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.poster.setPixmap(pix)

    # ---------- Per-stream selection changes ----------

    @Slot()
    def _on_audio_change(self) -> None:
        key = self._selected_key()
        if not key:
            return
        self.items[key].audio_index = self.audio_combo.currentData()

    @Slot()
    def _on_sub_check_change(self) -> None:
        key = self._selected_key()
        if not key:
            return
        item = self.items[key]
        # Snapshot the currently-forced output target so we can re-anchor after the rebuild
        forced_target = self._forced_target(item)
        new_indices: List[int] = []
        for i in range(self.sub_list.count()):
            li = self.sub_list.item(i)
            kind, val = li.data(Qt.ItemDataRole.UserRole)
            if kind == "internal" and li.checkState() == Qt.CheckState.Checked:
                new_indices.append(val)
        item.sub_indices = new_indices
        item.forced_sub_idx = self._forced_target_to_idx(item, forced_target)

    @Slot()
    def _on_sub_click_forced(self, list_item: QListWidgetItem) -> None:
        # Ctrl+Click on any included sub row promotes it to the forced (auto-show) track.
        if not (QApplication.keyboardModifiers() & Qt.KeyboardModifier.ControlModifier):
            return
        key = self._selected_key()
        if not key:
            return
        item = self.items[key]
        kind, val = list_item.data(Qt.ItemDataRole.UserRole)
        if kind == "internal":
            if list_item.checkState() != Qt.CheckState.Checked:
                return
            if val in item.sub_indices:
                item.forced_sub_idx = item.sub_indices.index(val)
        else:  # external
            p = Path(val)
            if p in item.extra_srts:
                item.forced_sub_idx = len(item.sub_indices) + item.extra_srts.index(p)
        self._refresh_detail()

    @Slot()
    def _on_add_srt(self) -> None:
        key = self._selected_key()
        if not key:
            return
        item = self.items[key]
        files, _ = QFileDialog.getOpenFileNames(
            self, "Add external SRT subtitle file(s)", str(item.source.parent),
            "Subtitles (*.srt);;All files (*.*)",
        )
        if not files:
            return
        for f in files:
            p = Path(f)
            if p not in item.extra_srts:
                item.extra_srts.append(p)
        self._refresh_detail()

    @Slot()
    def _on_remove_srt(self) -> None:
        key = self._selected_key()
        if not key:
            return
        item = self.items[key]
        li = self.sub_list.currentItem()
        if not li:
            return
        kind, val = li.data(Qt.ItemDataRole.UserRole)
        if kind != "external":
            return
        forced_target = self._forced_target(item)
        p = Path(val)
        if p in item.extra_srts:
            item.extra_srts.remove(p)
        item.forced_sub_idx = self._forced_target_to_idx(item, forced_target)
        self._refresh_detail()

    # ----- forced-sub bookkeeping helpers -----

    def _forced_target(self, item: QueueItem):
        """Return a stable handle for the currently-forced sub so we can re-resolve after list mutation."""
        if item.forced_sub_idx is None:
            return None
        idx = item.forced_sub_idx
        if idx < len(item.sub_indices):
            return ("internal", item.sub_indices[idx])
        ext_pos = idx - len(item.sub_indices)
        if 0 <= ext_pos < len(item.extra_srts):
            return ("external", item.extra_srts[ext_pos])
        return None

    def _forced_target_to_idx(self, item: QueueItem, target) -> Optional[int]:
        if not target:
            return None
        kind, val = target
        if kind == "internal" and val in item.sub_indices:
            return item.sub_indices.index(val)
        if kind == "external" and val in item.extra_srts:
            return len(item.sub_indices) + item.extra_srts.index(val)
        return None

    def _forced_idx_to_row(self, item: QueueItem) -> Optional[int]:
        """Map forced_sub_idx (position in output order) to the row in the QListWidget."""
        if item.forced_sub_idx is None or not item.probe:
            return None
        idx = item.forced_sub_idx
        if idx < len(item.sub_indices):
            abs_idx = item.sub_indices[idx]
            for row, s in enumerate(item.probe.subs):
                if s.index == abs_idx:
                    return row
            return None
        ext_pos = idx - len(item.sub_indices)
        return len(item.probe.subs) + ext_pos

    @Slot()
    def _on_output_change(self) -> None:
        self.cfg.max_height = self.height_combo.currentData()
        self.cfg.video_kbps = self.bitrate_spin.value()
        self.cfg.downmix_stereo = self.downmix.isChecked()
        cfg_mod.save(self.cfg)

    # ---------- Encode batch ----------

    @Slot()
    def _on_start(self) -> None:
        if not self.tools.get("ffmpeg") or not self.tools.get("mkvpropedit"):
            QMessageBox.warning(self, "Tools missing", "Install tools first (Add a file → it'll prompt).")
            return
        jobs: List[EncodeJob] = []
        for key, item in self.items.items():
            if item.status in ("done",):
                continue
            if not item.probe or item.audio_index is None:
                continue
            audio = next((a for a in item.probe.audio if a.index == item.audio_index), None)
            subs = [s for s in item.probe.subs if s.index in item.sub_indices]
            # preserve sub order according to user-selected order
            subs.sort(key=lambda s: item.sub_indices.index(s.index))
            if not audio:
                continue
            out_dir = Path(self.cfg.output_dir) if self.cfg.output_dir else item.source.parent
            out_path = out_dir / f"{item.source.stem}.mkv"
            # If the output would clobber the source, fall back to a suffixed name.
            try:
                if out_path.resolve() == item.source.resolve():
                    out_path = out_dir / f"{item.source.stem} (HEVC).mkv"
            except OSError:
                pass
            item.output = out_path
            jobs.append(EncodeJob(
                source=item.source,
                output=out_path,
                probe=item.probe,
                audio=audio,
                subs=subs,
                max_height=self.cfg.max_height,
                video_kbps=self.cfg.video_kbps,
                downmix_stereo=self.cfg.downmix_stereo,
                meta=item.meta,
                extra_srts=list(item.extra_srts),
                forced_sub_idx=item.forced_sub_idx,
            ))
            item.status = "queued"
            self._update_queue_row(key)

        if not jobs:
            QMessageBox.information(self, "Nothing to do", "No queued files are ready to encode.")
            return

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress.setValue(0)

        self.encode_worker = EncodeWorker(self.tools["ffmpeg"], self.tools["mkvpropedit"], jobs)
        self.encode_thread = QThread(self)
        self.encode_worker.moveToThread(self.encode_thread)
        self.encode_thread.started.connect(self.encode_worker.run)
        self.encode_worker.file_started.connect(self._on_file_started)
        self.encode_worker.file_progress.connect(self._on_file_progress)
        self.encode_worker.file_done.connect(self._on_file_done)
        self.encode_worker.file_failed.connect(self._on_file_failed)
        self.encode_worker.log.connect(self._log)
        self.encode_worker.all_done.connect(self._on_all_done)
        self.encode_thread.start()
        self._total_jobs = len(jobs)
        self._completed_jobs = 0

    @Slot()
    def _on_stop(self) -> None:
        if self.encode_worker:
            self.encode_worker.stop()
            self._log("Stop requested — finishing current file, then halting.")

    @Slot(object)
    def _on_file_started(self, source: Path) -> None:
        key = str(source)
        if key in self.items:
            self.items[key].status = "encoding"
            self._update_queue_row(key)
        self.progress_label.setText(source.name)

    @Slot(object, float)
    def _on_file_progress(self, source: Path, frac: float) -> None:
        # batch progress = (completed_jobs + current_frac) / total_jobs
        overall = (self._completed_jobs + max(0.0, min(1.0, frac))) / max(1, self._total_jobs)
        self.progress.setValue(int(overall * 1000))

    @Slot(object, object)
    def _on_file_done(self, source: Path, out: Path) -> None:
        key = str(source)
        if key in self.items:
            self.items[key].status = "done"
            self.items[key].output = out
            self._update_queue_row(key)
        self._completed_jobs += 1
        self._log(f"[done] {out}")

    @Slot(object, str)
    def _on_file_failed(self, source: Path, msg: str) -> None:
        key = str(source)
        if key in self.items:
            self.items[key].status = "error"
            self.items[key].error = msg
            self._update_queue_row(key)
        self._completed_jobs += 1
        self._log(f"[error] {source.name}: {msg}")

    @Slot()
    def _on_all_done(self) -> None:
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress.setValue(1000)
        self.progress_label.setText("Done")
        if self.encode_thread:
            self.encode_thread.quit()
            self.encode_thread.wait(2000)
            self.encode_thread = None
            self.encode_worker = None

    # ---------- Helpers ----------

    def _update_queue_row(self, key: str) -> None:
        item = self.items.get(key)
        if not item:
            return
        for i in range(self.queue_list.count()):
            li = self.queue_list.item(i)
            if li.data(Qt.ItemDataRole.UserRole) == key:
                icons = {
                    "pending": "⏳", "probing": "⏳", "ready": "✓",
                    "queued": "→", "encoding": "▶", "done": "✅", "error": "✖",
                }
                ico = icons.get(item.status, "•")
                li.setText(f"{ico}  {item.source.name}")
                break

    def _log(self, msg: str) -> None:
        self.log.append(msg)


def _wrap(layout) -> QWidget:
    w = QWidget()
    w.setLayout(layout)
    layout.setContentsMargins(0, 0, 0, 0)
    return w


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Movie MKV")
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
