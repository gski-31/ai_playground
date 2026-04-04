# RPCS3 Game Export Tool

Export and import PS3 games for the RPCS3 emulator. Given an RPCS3 installation directory and a game ID, this tool exports **everything** needed to fully restore that game — base game, updates, DLC, saves, patches, and custom config — into a single portable ZIP archive.

## Quick Start

### Windows

```
run.bat
```

### Linux / macOS

```bash
chmod +x run.sh
./run.sh
```

Then open **http://localhost:5000** in your browser.

### Manual Setup

```bash
pip install -r requirements.txt
python app.py
```

## How to Use

### Export

1. Click **Browse** (or type the path) to select your RPCS3 root directory — the folder containing `dev_hdd0`, `patches`, etc.
2. Click **Scan** to discover all installed games. The tool parses `PARAM.SFO` inside each game folder to show human-readable titles, sizes, and available extras.
3. Select a game from the list. Use the search bar to filter by title or game ID (e.g. `BLUS30443`).
4. If the game uses a disc image stored outside the RPCS3 directory, provide the path in the optional **External disc path** field.
5. Click **Browse** to select an output directory, then click **Export Game**.
6. A progress bar tracks the operation in real time. When finished you get a single `.zip` file containing everything.

The tool automatically collects all of the following into one archive:
- Game data, updates, and DLC (`dev_hdd0/game/{GAME_ID}/`)
- Disc content (`dev_hdd0/disc/{GAME_ID}/`) if present
- All save data folders matching the game ID
- Patches from `patch.yml` filtered to only this game's entries
- Per-game custom emulator config if one exists

### Import (Reimport)

1. Switch to the **Import** tab.
2. Browse to your RPCS3 root directory.
3. Browse to the export archive (`.zip`) or extracted export folder.
4. Click **Import Game**. Everything in the archive is restored automatically to the correct locations. Patches are merged into the existing `patch.yml` without overwriting other games' entries.

## What's Inside the ZIP

```
export_{GAME_ID}.zip
  export_{GAME_ID}/
    game/{GAME_ID}/          <- installed game + updates + DLC
    disc/{GAME_ID}/          <- disc content (if applicable)
    savedata/{matching dirs} <- save files
    patches/patch.yml        <- filtered patches for this game only
    custom_config.yml        <- per-game emulator config
    manifest.json            <- metadata (see below)
```

### manifest.json

Contains metadata to make reimporting easier:

- Game ID and title (parsed from `PARAM.SFO`)
- Export date
- RPCS3 version (detected from the installation)
- List of what was included (game data, disc, saves, patches, config)
- Original file paths for reference

## What Gets Collected

| Component | Path | Included |
|---|---|---|
| Game data, updates, DLC | `dev_hdd0/game/{GAME_ID}/` | Always |
| Disc content | `dev_hdd0/disc/{GAME_ID}/` | If present |
| Save data | `dev_hdd0/home/00000001/savedata/{GAME_ID}*` | Always (if saves exist) |
| Patches | `patches/patch.yml` (filtered) | Always (if patches exist) |
| Custom config | `config/custom_configs/{GAME_ID}_config.yml` | Always (if config exists) |

## Requirements

- Python 3.8+
- `flask` and `flask-cors` (installed automatically by the launch scripts)
- `tkinter` (included with most Python installations — used for Browse dialogs)

## Technical Notes

- **PARAM.SFO parsing** is implemented natively in Python (no external library needed). It reads the PS3 binary key-value format directly.
- **Streaming copy** — large game directories (10-50 GB) are copied file-by-file with progress tracking rather than being buffered in memory.
- **Patch extraction** — the tool parses `patch.yml` and extracts only the blocks referencing the target game ID, writing them to a standalone file.
- **One file = one game** — export produces a single ZIP containing everything. Import restores everything from that ZIP. No picking and choosing.
- Supports both Windows and Linux paths.
