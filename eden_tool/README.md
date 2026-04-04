# Eden Game Export Tool

Export and import NSP games for the Eden emulator. This tool bundles **everything** for a game — base NSP, updates, DLC, saves, mods, and custom config — into a single portable ZIP archive.

## Quick Start

### Windows

```
run.bat
```

Your browser will open automatically to **http://localhost:5001**.

### Manual Setup

```bash
pip install -r requirements.txt
python app.py
```

## How to Use

### Export

1. Click **Browse** to select your Eden root directory (the folder containing `eden.exe` and `NSPs/`).
2. Click **Scan** to discover all installed games. The tool parses NSP filenames to identify base games, updates, and DLC.
3. Select a game from the list. Use the search bar to filter by title or title ID.
4. Click **Browse** to select an output directory, then click **Export**.
5. A progress bar tracks the operation. When finished you get a single `.zip` file containing everything.

The tool automatically collects all of the following into one archive:
- Base game NSP
- All update NSPs for that title
- All DLC NSPs for that title
- Save data from `%APPDATA%/eden/nand/user/save/`
- Mods from `%APPDATA%/eden/sdmc/atmosphere/contents/{TitleID}/`
- Load patches from `%APPDATA%/eden/load/{TitleID}/`
- Per-game custom config from `%APPDATA%/eden/config/custom/{TitleID}.ini`

### Import (Reimport)

1. Switch to the **Import** tab.
2. Browse to your Eden root directory.
3. Browse to the export archive (`.zip`) or extracted export folder.
4. Click **Import Game**. Everything is restored automatically to the correct locations.

## What's Inside the ZIP

```
export_{TitleID}.zip
  export_{TitleID}/
    NSPs/                        <- base game + updates + DLC (.nsp files)
    saves/{uid}/{hash}/{id}/     <- save data
    mods/{TitleID}/              <- atmosphere mods
    load/{TitleID}/              <- load patches
    config/{TitleID}.ini         <- per-game emulator config
    manifest.json                <- metadata
```

## NSP Naming Convention

The tool expects NSP files to follow this naming convention:

```
Game Title [TitleID][vN][Type].nsp
```

Where:
- `TitleID` is the 16-character hex title ID (e.g. `0100152000022000`)
- `vN` is the version number (e.g. `v0` for base, `v131072` for updates)
- `Type` is `Base`, `Update`, or `DLC`

Example: `Mario Kart 8 Deluxe [0100152000022000][v0][Base].nsp`

## Requirements

- Python 3.8+
- `flask` and `flask-cors` (installed automatically by `run.bat`)
- `tkinter` (included with most Python installations — used for Browse dialogs)

## Technical Notes

- **NSP files are copied with streaming** (16 MB chunks) to handle large files without buffering in memory.
- **ZIP uses STORED compression** (no compression) since NSP files are already compressed. This makes archiving much faster.
- **Title ID grouping** uses the Nintendo Switch scheme: base games end in `000`, updates end in `800`, DLC uses `001`-`7FF`. The tool masks the last 3 hex digits to group related content.
- Runs on port **5001** by default (to avoid conflicts with the RPCS3 tool on 5000).
