# Movie → HEVC MKV

A Windows GUI that batch-converts videos to H.265 MKV files tuned for an 11" tablet and 1080p TV playback. Uses NVIDIA NVENC for fast HEVC encoding, picks the best English audio and subtitle tracks automatically, and tags movies with poster art and metadata from TMDB.

## Features

- **Hardware HEVC encode** via `hevc_nvenc` (10-bit Main10, CQ-driven VBR) — sized for NVIDIA RTX cards.
- **Smart audio pick**: filters to English tracks, ranks by codec quality (TrueHD/DTS-HD MA > FLAC > E-AC3 > DTS > AC3 > AAC) and channel count.
- **Smart subtitles**: keeps the full English track (toggleable, off by default) and the forced English track (auto-shows for foreign dialogue) when both exist.
- **TMDB metadata**: auto-guesses title/year from the filename, fetches the matching poster, and embeds title, year, overview, and cover art into the MKV.
- **Batch queue** with per-file detail editing and live progress.
- **Self-installs ffmpeg + MKVToolNix** on first run.

## Requirements

- **Windows 10/11**
- **Python 3.10+** on PATH (3.12 or 3.13 recommended; 3.14 works if PySide6 has a matching wheel)
- An **NVIDIA GPU with NVENC HEVC** support (Turing/Ampere/Ada/Blackwell)
- Internet access on first launch (to download ffmpeg + MKVToolNix, ~130 MB)

## Running it

From the [movie_mkv](.) folder, in PowerShell:

```powershell
.\run.bat
```

Or from `cmd`:

```cmd
run.bat
```

What happens:

1. The script creates `.venv\` and installs PySide6, requests, and py7zr (one-time, ~1 min).
2. The GUI opens. Status bar says "Checking tools…".
3. Click **Add files…** and pick one or more video files (`.mkv`, `.mp4`, `.m4v`, `.mov`, `.avi`, `.ts`, `.m2ts`, `.webm`, `.wmv`).
4. **First add only**: a prompt appears offering to download ffmpeg and MKVToolNix into `%APPDATA%\movie_mkv\tools\`. Click **Yes**. Tools are cached for next time.
5. Each file gets probed; the detail panel fills in with auto-picked audio/subs and a TMDB match guess.
6. Review per-file: swap the audio track if needed, tick/untick subtitle tracks, change the TMDB match (or search again).
7. (Optional) Click **Output folder…** to set a destination (otherwise output goes next to the source).
8. Click **Start batch**.

Output files are named `<original_name>.mkv` (or `<original_name> (HEVC).mkv` if that would overwrite the source).

## The detail panel

- **TMDB row**: title/year is guessed from the filename. Click **Search** to re-run; pick a different match from the dropdown.
- **Audio**: dropdown of all detected audio tracks; the best English track is preselected.
- **Subtitles**: checklist of all subtitle tracks (internal + external SRTs).
  - Checked = included in the output.
  - A `●` marks the track flagged as "forced" — it auto-displays for foreign dialogue.
  - **Ctrl-click** any included sub row to promote it to the forced track.
  - **Add SRT file…** to mux in an external `.srt` alongside the source (always included once added; **Remove selected external SRT** drops it).
- **Output**: max height (1080p default), target video bitrate (2-pass VBR, default 2800 kbps ≈ 1.3 GB/hr), and an optional "downmix surround to stereo" checkbox for tablet-friendly files.

## Output settings explained

| Setting       | Default          | Effect                                                                                   |
|---------------|------------------|------------------------------------------------------------------------------------------|
| Codec         | `hevc_nvenc`     | NVIDIA hardware HEVC. Far faster than software x265 with near-equivalent quality.        |
| Profile       | Main10 (10-bit)  | Better banding/gradients at same bitrate. Modern TVs and tablets handle it fine.         |
| Rate control  | 2-pass VBR       | Target average bitrate with maxrate at 1.6× and bufsize at 3.2×. Yields predictable file sizes (~1.1 GB/hr at 2500 kbps). |
| Max height    | 1080p            | Scales 4K down to 1080p, leaves 1080p alone, never upscales.                             |
| Audio         | AAC, channels=as-source | 192k stereo / 384k 5.1 / 512k 7.1. Downmix-to-stereo for tablets is a checkbox.   |
| Subtitles     | `copy`           | Source subs (PGS/SRT/ASS) are kept as soft subs. `mov_text` from MP4 is converted to SRT. |

## Config

Settings, your TMDB key, and tool paths live in:

```
%APPDATA%\movie_mkv\config.json
```

Tools (ffmpeg, MKVToolNix) live in:

```
%APPDATA%\movie_mkv\tools\
```

`config.json` is gitignored locally; the TMDB key is currently hardcoded as a default in [config.py](config.py).

## File layout

| File              | Purpose |
|-------------------|---------|
| [app.py](app.py)             | PySide6 main window + queue/detail UI |
| [workers.py](workers.py)     | QThread workers for probe / TMDB / encode / dep-download |
| [probe.py](probe.py)         | `ffprobe` JSON parsing + audio/subtitle picking heuristics |
| [tmdb.py](tmdb.py)           | TMDB client + filename → title/year guesser |
| [encode.py](encode.py)       | ffmpeg command builder + mkvpropedit poster/title pass |
| [deps.py](deps.py)           | Locate or auto-download ffmpeg + MKVToolNix |
| [config.py](config.py)       | Settings dataclass + load/save |
| [requirements.txt](requirements.txt) | PySide6, requests, py7zr |
| [run.bat](run.bat)           | venv bootstrap + launcher |

## Troubleshooting

**"Python not found on PATH"** — install Python from [python.org](https://www.python.org/downloads/) and tick "Add Python to PATH" during setup.

**`pip install` fails on PySide6** — Python 3.14 may not yet have a matching wheel. Install Python 3.12 or 3.13 alongside and delete `.venv\` before re-running.

**No NVENC** — the encoder will fail with a clear ffmpeg error if your GPU lacks HEVC NVENC support. You'd need to edit [encode.py](encode.py) and swap `hevc_nvenc` for `libx265` (much slower).

**Forced subs not auto-detected** — many rips only ship a single full English subtitle track with no "forced" companion track. There's no reliable way to auto-extract the foreign-dialogue portion from a full sub track; the app includes the full track as toggleable instead. If your source ships a separate forced track, it's detected automatically.

**HDR sources** — the app passes color metadata through but does not tonemap HDR → SDR. If your input is HDR and your TV isn't, the result may look washed out.

## Tools used

- [ffmpeg](https://ffmpeg.org/) (gyan.dev essentials build) — encoding and muxing
- [MKVToolNix](https://mkvtoolnix.download/) — poster attachments and container metadata
- [The Movie Database (TMDB) API](https://www.themoviedb.org/) — movie metadata and posters
