"""User config: API key + tool paths + output preferences. Stored in %APPDATA%/movie_mkv/config.json."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional


APP_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "movie_mkv"
CONFIG_PATH = APP_DIR / "config.json"
TOOLS_DIR = APP_DIR / "tools"


@dataclass
class Config:
    tmdb_api_key: str = "beaead7e577424744663d4769288e2cb"
    tmdb_access_token: str = (
        "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJiZWFlYWQ3ZTU3NzQyNDc0NDY2M2Q0NzY5Mjg4ZTJjYiIsIm5iZiI6MTc3O"
        "DYxMDg1MS44NDE5OTk4LCJzdWIiOiI2YTAzNzJhMzZmOWZhNTUzNjYxZTQ5OTIiLCJzY29wZXMiOlsiYXBpX3JlYW"
        "QiXSwidmVyc2lvbiI6MX0.5i-KivzVAG8nDBKFOrG2qdJRmAOhSJuOiOUGqNKyWDw"
    )
    output_dir: str = ""
    max_height: int = 1080
    video_kbps: int = 2800
    downmix_stereo: bool = False
    ffmpeg_path: str = ""
    ffprobe_path: str = ""
    mkvmerge_path: str = ""
    mkvpropedit_path: str = ""


def load() -> Config:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        cfg = Config(**{k: v for k, v in data.items() if k in Config.__dataclass_fields__})
    else:
        cfg = Config()
        save(cfg)
    return cfg


def save(cfg: Config) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(asdict(cfg), indent=2), encoding="utf-8")
