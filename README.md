# AI Playground

A collection of independent utility tools for managing game emulator exports and a random restaurant picker.

## Tools

### RPCS3 Game Export Tool (`rpcs3_tool/`)

Export and import complete PS3 game installations for the RPCS3 emulator. Scans your RPCS3 install, parses PS3 binary metadata (PARAM.SFO), and bundles games into single ZIP archives including game data, updates, DLC, saves, patches, and per-game config.

```bash
cd rpcs3_tool
pip install -r requirements.txt
python app.py
# Opens http://localhost:5000
```

Or use `run.bat` (Windows) / `run.sh` (Linux) to auto-install dependencies and launch.

### Eden Game Export Tool (`eden_tool/`)

Export and import Nintendo Switch game bundles for the Eden emulator. Parses NSP filenames to identify base games, updates, and DLC, then bundles everything (saves, mods, config) into a single ZIP.

```bash
cd eden_tool
pip install -r requirements.txt
python app.py
# Opens http://localhost:5001
```

Or use `run.bat` to auto-install and launch.

### Restaurant Roulette (`rest_roulette/`)

A web app that randomly picks a restaurant near you based on distance, rating, price level, and cuisine filters. Uses Google Maps Places API with a slot-machine style reveal animation.

1. Copy `js/config.example.js` to `js/config.js`
2. Add your Google Maps API key (with Places API enabled)
3. Open `index.html` in a browser and allow location access

### Shell Scripts (`shells/`)

PowerShell scripts for copying large game libraries with SLC cache-aware pacing. Monitors write speed and pauses automatically when cache exhaustion is detected.

- `Eden.ps1` -- Copy Eden game library
- `RPCS3.ps1` -- Copy RPCS3 game library

## Tech Stack

| Component | Stack |
|---|---|
| RPCS3 Tool | Python, Flask, tkinter |
| Eden Tool | Python, Flask, tkinter |
| Restaurant Roulette | Vanilla JS, Google Maps API |
| Shell Scripts | PowerShell |

## Project Structure

```
ai_playground/
├── rpcs3_tool/          # PS3 game export/import tool
│   ├── app.py
│   ├── static/index.html
│   ├── requirements.txt
│   ├── run.bat / run.sh
│   └── README.md
├── eden_tool/           # Switch game export/import tool
│   ├── app.py
│   ├── static/index.html
│   ├── requirements.txt
│   ├── run.bat
│   └── README.md
├── rest_roulette/       # Random restaurant picker
│   ├── index.html
│   ├── js/
│   └── css/
└── shells/              # Game library copy scripts
    ├── Eden.ps1
    └── RPCS3.ps1
```
