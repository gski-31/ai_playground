"""Batch convert FLAC / ALAC / WAV / AIFF to high-quality Opus via ffmpeg+libopus."""

import os
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from tkinter import Tk, StringVar, IntVar, BooleanVar, END, DISABLED, NORMAL
from tkinter import filedialog, messagebox, ttk, scrolledtext

SOURCE_EXTS = {".flac", ".wav", ".m4a", ".alac", ".aif", ".aiff"}

# Hide the console window when ffmpeg is launched (Windows only).
if os.name == "nt":
    CREATE_NO_WINDOW = 0x08000000
else:
    CREATE_NO_WINDOW = 0


def find_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def iter_source_files(root: Path):
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in SOURCE_EXTS:
            yield path


def build_ffmpeg_cmd(ffmpeg: str, src: Path, dst: Path, bitrate_kbps: int) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-nostdin",
        "-y",
        "-i", str(src),
        "-map", "0:a",
        "-map_metadata", "0",
        "-c:a", "libopus",
        "-b:a", f"{bitrate_kbps}k",
        "-vbr", "on",
        "-compression_level", "10",
        "-application", "audio",
        str(dst),
    ]


class OpusBatchApp:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("Opus Batch Encoder (libopus VBR)")
        self.root.geometry("780x560")

        self.input_dir = StringVar()
        self.output_dir = StringVar()
        self.bitrate = IntVar(value=256)
        self.skip_existing = BooleanVar(value=True)

        self.ffmpeg_path = find_ffmpeg()
        self.worker: threading.Thread | None = None
        self.cancel_flag = threading.Event()
        self.current_proc: subprocess.Popen | None = None
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

        frm = ttk.Frame(self.root)
        frm.pack(fill="x", **pad)

        ttk.Label(frm, text="Input folder:").grid(row=0, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.input_dir).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(frm, text="Browse...", command=self._pick_input).grid(row=0, column=2)

        ttk.Label(frm, text="Output folder:").grid(row=1, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.output_dir).grid(row=1, column=1, sticky="ew", padx=4)
        ttk.Button(frm, text="Browse...", command=self._pick_output).grid(row=1, column=2)

        frm.columnconfigure(1, weight=1)

        opts = ttk.LabelFrame(self.root, text="Encoding options")
        opts.pack(fill="x", **pad)

        ttk.Label(opts, text="VBR target bitrate (kbps):").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        ttk.Spinbox(
            opts, from_=64, to=510, increment=8, textvariable=self.bitrate, width=8
        ).grid(row=0, column=1, sticky="w")
        ttk.Label(
            opts,
            text="(256 kbps = archive-safe. libopus stereo ceiling is 510 kbps.)",
            foreground="#555",
        ).grid(row=0, column=2, sticky="w", padx=6)

        ttk.Checkbutton(
            opts, text="Skip files that already have a matching .opus output",
            variable=self.skip_existing,
        ).grid(row=1, column=0, columnspan=3, sticky="w", padx=6, pady=2)

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
            args=(in_resolved, out_resolved, int(self.bitrate.get()), bool(self.skip_existing.get())),
            daemon=True,
        )
        self.worker.start()

    def _on_cancel(self) -> None:
        self.cancel_flag.set()
        proc = self.current_proc
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

    def _run(self, in_dir: Path, out_dir: Path, bitrate: int, skip_existing: bool) -> None:
        try:
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
                dst = (out_dir / rel).with_suffix(".opus")
                self._set_status(f"[{idx}/{total}] {rel}")

                if skip_existing and dst.exists() and dst.stat().st_size > 0:
                    skipped += 1
                    self._log(f"SKIP (exists): {rel}")
                    self._set_progress(idx, total)
                    continue

                dst.parent.mkdir(parents=True, exist_ok=True)
                cmd = build_ffmpeg_cmd(self.ffmpeg_path, src, dst, bitrate)

                try:
                    self.current_proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        creationflags=CREATE_NO_WINDOW,
                        text=True,
                    )
                    out, _ = self.current_proc.communicate()
                    rc = self.current_proc.returncode
                finally:
                    self.current_proc = None

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
    OpusBatchApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
