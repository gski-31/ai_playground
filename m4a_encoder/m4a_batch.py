"""Batch convert FLAC / ALAC / WAV / AIFF to high-quality AAC (.m4a) via qaac (Apple CoreAudio).

Pipeline per file:
  1. ffprobe reads tags + sample rate.
  2. ffmpeg decodes to a temp WAV (24-bit signed PCM, resampled if > 48 kHz).
  3. qaac encodes the temp WAV -> .m4a with True VBR + tags.
  4. Temp file is auto-deleted.
"""

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from tkinter import Tk, StringVar, IntVar, BooleanVar, END, DISABLED, NORMAL
from tkinter import filedialog, messagebox, ttk, scrolledtext

SOURCE_EXTS = {".flac", ".wav", ".m4a", ".alac", ".aif", ".aiff"}

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

QAAC_TAG_FLAGS = {
    "title": "--title",
    "artist": "--artist",
    "album_artist": "--band",
    "albumartist": "--band",
    "album": "--album",
    "genre": "--genre",
    "date": "--date",
    "year": "--date",
    "composer": "--composer",
    "grouping": "--grouping",
    "comment": "--comment",
    "lyrics": "--lyrics",
}


def find_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def find_ffprobe() -> str | None:
    p = shutil.which("ffprobe")
    if p:
        return p
    ff = find_ffmpeg()
    if ff:
        guess = Path(ff).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if guess.exists():
            return str(guess)
    return None


def find_qaac() -> str | None:
    for name in ("qaac64", "qaac"):
        p = shutil.which(name)
        if p:
            return p
    here = Path(__file__).parent
    candidates = [
        Path(r"C:\Program Files\qaac\qaac64.exe"),
        Path(r"C:\Program Files (x86)\qaac\qaac64.exe"),
        here / "qaac64.exe",
        here / "qaac" / "qaac64.exe",
        here.parent / "ogg_encoder" / "qaac" / "qaac64.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def iter_source_files(root: Path):
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in SOURCE_EXTS:
            yield path


def probe_audio(ffprobe: str, src: Path) -> tuple[dict[str, str], int]:
    """Return (tags, sample_rate). sample_rate is 0 if unknown."""
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(src)],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
        )
        if proc.returncode != 0:
            return {}, 0
        data = json.loads(proc.stdout or "{}")
    except (OSError, json.JSONDecodeError):
        return {}, 0

    tags: dict[str, str] = {}
    fmt_tags = (data.get("format") or {}).get("tags") or {}
    for k, v in fmt_tags.items():
        if v is None:
            continue
        tags[k.lower()] = str(v)
    sample_rate = 0
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "audio" and not sample_rate:
            try:
                sample_rate = int(stream.get("sample_rate") or 0)
            except (TypeError, ValueError):
                sample_rate = 0
        for k, v in (stream.get("tags") or {}).items():
            if v is None:
                continue
            tags.setdefault(k.lower(), str(v))
    return tags, sample_rate


def pick_aac_rate(src_rate: int) -> int:
    """Apple's AAC encoder maxes at 48 kHz. Match the source's rate family."""
    if src_rate <= 0:
        return 48000
    if src_rate <= 48000:
        return 0  # pass through
    if src_rate % 44100 == 0:
        return 44100
    return 48000


def tags_to_qaac_args(tags: dict[str, str]) -> list[str]:
    args: list[str] = []
    used_band = False

    for key, value in tags.items():
        if not value:
            continue
        if key in ("track", "tracknumber"):
            total = tags.get("totaltracks") or tags.get("tracktotal")
            args += ["--track", f"{value}/{total}" if total else value]
            continue
        if key in ("disc", "discnumber"):
            total = tags.get("totaldiscs") or tags.get("disctotal")
            args += ["--disk", f"{value}/{total}" if total else value]
            continue
        if key == "compilation":
            if value.strip() in ("1", "true", "yes"):
                args.append("--compilation")
            continue
        if key in ("totaltracks", "tracktotal", "totaldiscs", "disctotal"):
            continue
        flag = QAAC_TAG_FLAGS.get(key)
        if flag == "--band":
            if used_band:
                continue
            used_band = True
        if flag:
            args += [flag, value]
        else:
            if len(value) <= 1024 and not key.startswith("itunes"):
                args += ["--long-tag", f"{key}:{value}"]
    return args


def check_qaac(qaac: str) -> tuple[bool, str]:
    """Run `qaac --check`; returns (ok, output)."""
    try:
        proc = subprocess.run(
            [qaac, "--check"],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        ok = proc.returncode == 0 and "CoreAudioToolbox" in out
        return ok, out.strip()
    except OSError as e:
        return False, str(e)


class M4aBatchApp:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("M4A Batch Encoder — qaac / Apple CoreAudio AAC")
        self.root.geometry("820x620")

        self.input_dir = StringVar()
        self.output_dir = StringVar()
        self.tvbr = IntVar(value=127)  # ~320 kbps, max quality
        self.qaac_path = StringVar(value=find_qaac() or "")
        self.skip_existing = BooleanVar(value=True)

        self.ffmpeg_path = find_ffmpeg()
        self.ffprobe_path = find_ffprobe()
        self.worker: threading.Thread | None = None
        self.cancel_flag = threading.Event()
        self.current_procs: list[subprocess.Popen] = []
        self.log_queue: queue.Queue[str] = queue.Queue()

        self._build_ui()
        self._poll_log()

        if not self.ffmpeg_path:
            messagebox.showerror(
                "ffmpeg not found",
                "ffmpeg.exe was not found on PATH.\n"
                "Install it (e.g. `winget install Gyan.FFmpeg`) and restart.",
            )

    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}

        folders = ttk.Frame(self.root)
        folders.pack(fill="x", **pad)
        ttk.Label(folders, text="Input folder:").grid(row=0, column=0, sticky="w")
        ttk.Entry(folders, textvariable=self.input_dir).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(folders, text="Browse...", command=self._pick_input).grid(row=0, column=2)
        ttk.Label(folders, text="Output folder:").grid(row=1, column=0, sticky="w")
        ttk.Entry(folders, textvariable=self.output_dir).grid(row=1, column=1, sticky="ew", padx=4)
        ttk.Button(folders, text="Browse...", command=self._pick_output).grid(row=1, column=2)
        folders.columnconfigure(1, weight=1)

        opts = ttk.LabelFrame(self.root, text="qaac / AAC options")
        opts.pack(fill="x", **pad)
        ttk.Label(opts, text="qaac64.exe:").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(opts, textvariable=self.qaac_path).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(opts, text="Browse...", command=self._pick_qaac).grid(row=0, column=2)
        ttk.Label(opts, text="True VBR quality (0–127):").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        ttk.Spinbox(
            opts, from_=0, to=127, increment=1, textvariable=self.tvbr, width=8,
        ).grid(row=1, column=1, sticky="w")
        ttk.Label(
            opts,
            text="(91≈192k · 109≈256k iTunes Plus · 127≈320k max. Needs Apple Application Support.)",
            foreground="#555",
        ).grid(row=1, column=2, sticky="w", padx=6)
        opts.columnconfigure(1, weight=1)

        common = ttk.Frame(self.root)
        common.pack(fill="x", **pad)
        ttk.Checkbutton(
            common, text="Skip files that already have a matching .m4a output",
            variable=self.skip_existing,
        ).pack(anchor="w")

        controls = ttk.Frame(self.root)
        controls.pack(fill="x", **pad)
        self.start_btn = ttk.Button(controls, text="Start", command=self._on_start)
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(controls, text="Cancel", command=self._on_cancel, state=DISABLED)
        self.cancel_btn.pack(side="left", padx=6)

        self.progress = ttk.Progressbar(self.root, mode="determinate")
        self.progress.pack(fill="x", **pad)

        self.status = StringVar(value="Idle.")
        ttk.Label(self.root, textvariable=self.status, anchor="w").pack(fill="x", padx=8)

        self.log = scrolledtext.ScrolledText(self.root, height=18, state=DISABLED, wrap="none")
        self.log.pack(fill="both", expand=True, **pad)

    def _pick_input(self) -> None:
        d = filedialog.askdirectory(title="Choose input folder (recursively scanned)")
        if d:
            self.input_dir.set(d)

    def _pick_output(self) -> None:
        d = filedialog.askdirectory(title="Choose output folder (mirrors input structure)")
        if d:
            self.output_dir.set(d)

    def _pick_qaac(self) -> None:
        f = filedialog.askopenfilename(
            title="Locate qaac64.exe",
            filetypes=[("qaac executable", "qaac64.exe qaac.exe"), ("All files", "*.*")],
        )
        if f:
            self.qaac_path.set(f)

    def _log(self, msg: str) -> None:
        self.log_queue.put(msg)

    def _poll_log(self) -> None:
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log.configure(state=NORMAL)
                self.log.insert(END, msg + "\n")
                self.log.see(END)
                self.log.configure(state=DISABLED)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_log)

    def _on_start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not self.ffmpeg_path:
            messagebox.showerror("ffmpeg missing", "ffmpeg is not on PATH.")
            return

        qaac = self.qaac_path.get().strip()
        if not qaac or not Path(qaac).is_file():
            messagebox.showerror(
                "qaac missing",
                "Pick qaac64.exe.\n\n"
                "Download: https://github.com/nu774/qaac/releases\n"
                "Also requires Apple Application Support DLLs (from iTunes).",
            )
            return

        in_dir = Path(self.input_dir.get()).expanduser()
        out_dir = Path(self.output_dir.get()).expanduser()
        if not in_dir.is_dir():
            messagebox.showerror("Bad input", "Pick a valid input folder.")
            return
        if not self.output_dir.get():
            messagebox.showerror("Bad output", "Pick an output folder.")
            return
        try:
            in_resolved = in_dir.resolve()
            out_resolved = out_dir.resolve()
        except OSError as e:
            messagebox.showerror("Path error", str(e))
            return
        if out_resolved == in_resolved or in_resolved in out_resolved.parents:
            if not messagebox.askyesno(
                "Overlapping folders",
                "Output folder is the same as or inside the input folder. "
                "Continue anyway?",
            ):
                return

        out_dir.mkdir(parents=True, exist_ok=True)
        self.cancel_flag.clear()
        self.start_btn.configure(state=DISABLED)
        self.cancel_btn.configure(state=NORMAL)

        self.worker = threading.Thread(
            target=self._run,
            args=(
                in_resolved, out_resolved,
                int(self.tvbr.get()), qaac,
                bool(self.skip_existing.get()),
            ),
            daemon=True,
        )
        self.worker.start()

    def _on_cancel(self) -> None:
        self.cancel_flag.set()
        for proc in list(self.current_procs):
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        self._log("Cancellation requested...")

    def _set_status(self, text: str) -> None:
        self.root.after(0, lambda: self.status.set(text))

    def _set_progress(self, value: float, maximum: float) -> None:
        def apply():
            self.progress.configure(maximum=max(maximum, 1), value=value)
        self.root.after(0, apply)

    def _finish(self) -> None:
        def apply():
            self.start_btn.configure(state=NORMAL)
            self.cancel_btn.configure(state=DISABLED)
        self.root.after(0, apply)

    def _encode_one(self, src: Path, dst: Path, qaac: str, tvbr: int) -> tuple[int, str]:
        """Decode src to a temp WAV with ffmpeg, then encode that WAV to dst with qaac."""
        if self.ffprobe_path:
            tags, src_rate = probe_audio(self.ffprobe_path, src)
        else:
            tags, src_rate = {}, 0
        tag_args = tags_to_qaac_args(tags)
        target_rate = pick_aac_rate(src_rate)

        with tempfile.TemporaryDirectory(prefix="m4a_enc_") as td:
            tmp_wav = Path(td) / "decoded.wav"

            ff_cmd = [
                self.ffmpeg_path,
                "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-i", str(src),
                "-map", "0:a:0",
            ]
            if target_rate:
                ff_cmd += [
                    "-af", "aresample=resampler=soxr:precision=28",
                    "-ar", str(target_rate),
                ]
            # Force 24-bit signed PCM — CoreAudio AAC rejects 32-bit float WAV.
            ff_cmd += ["-c:a", "pcm_s24le", str(tmp_wav)]

            if target_rate:
                self._log(f"       resampling {src_rate or '?'} -> {target_rate} Hz")

            ff = subprocess.Popen(
                ff_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW, text=True,
            )
            self.current_procs = [ff]
            try:
                _, ff_err = ff.communicate()
            finally:
                self.current_procs = []
            if self.cancel_flag.is_set():
                return -1, "cancelled"
            if ff.returncode != 0:
                return ff.returncode, f"ffmpeg: {(ff_err or '').strip()}"
            if not tmp_wav.exists() or tmp_wav.stat().st_size == 0:
                return 1, "ffmpeg produced no output WAV"

            qa_cmd = [
                qaac,
                "--tvbr", str(tvbr),
                "--threading",
                *tag_args,
                "-o", str(dst),
                str(tmp_wav),
            ]
            qa = subprocess.Popen(
                qa_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW, text=True,
            )
            self.current_procs = [qa]
            try:
                qa_out, _ = qa.communicate()
            finally:
                self.current_procs = []
            if self.cancel_flag.is_set():
                return -1, "cancelled"
            if qa.returncode != 0:
                return qa.returncode, f"qaac: {(qa_out or '').strip()}"
            return 0, ""

    def _run(
        self,
        in_dir: Path, out_dir: Path,
        tvbr: int, qaac: str, skip_existing: bool,
    ) -> None:
        try:
            self._log("Checking qaac / Apple CoreAudio...")
            ok, info = check_qaac(qaac)
            self._log(info or "(no qaac --check output)")
            if not ok:
                self._log("ABORT: qaac cannot load CoreAudioToolbox. Install Apple "
                          "Application Support (e.g. via iTunes) and try again.")
                self._set_status("qaac not ready — see log.")
                return

            self._log(f"Scanning {in_dir} ...")
            files = sorted(iter_source_files(in_dir))
            total = len(files)
            self._log(f"Found {total} source file(s).")
            if total == 0:
                self._set_status("No source files found.")
                return

            self._set_progress(0, total)
            done = skipped = failed = 0

            for idx, src in enumerate(files, start=1):
                if self.cancel_flag.is_set():
                    self._log("Cancelled.")
                    break

                rel = src.relative_to(in_dir)
                dst = (out_dir / rel).with_suffix(".m4a")
                self._set_status(f"[{idx}/{total}] {rel}")

                if skip_existing and dst.exists() and dst.stat().st_size > 0:
                    skipped += 1
                    self._log(f"SKIP (exists): {rel}")
                    self._set_progress(idx, total)
                    continue

                dst.parent.mkdir(parents=True, exist_ok=True)

                try:
                    rc, out = self._encode_one(src, dst, qaac, tvbr)
                except Exception as e:
                    rc, out = 1, str(e)

                if self.cancel_flag.is_set():
                    if dst.exists():
                        try:
                            dst.unlink()
                        except OSError:
                            pass
                    self._log("Cancelled mid-encode.")
                    break

                if rc == 0:
                    done += 1
                    self._log(f"OK   : {rel}")
                else:
                    failed += 1
                    if dst.exists():
                        try:
                            dst.unlink()
                        except OSError:
                            pass
                    self._log(f"FAIL : {rel}\n       {(out or '').strip()}")

                self._set_progress(idx, total)

            summary = f"Done. Encoded {done}, skipped {skipped}, failed {failed}, total {total}."
            self._set_status(summary)
            self._log(summary)
        except Exception as e:
            self._log(f"ERROR: {e}")
            self._set_status("Error — see log.")
        finally:
            self._finish()


def main() -> int:
    root = Tk()
    try:
        ttk.Style().theme_use("vista" if os.name == "nt" else "clam")
    except Exception:
        pass
    M4aBatchApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
