"""
RPCS3 Game Export Tool — Backend
Flask server that handles game discovery, SFO parsing, and export/import operations.
"""

import os
import sys
import struct
import json
import shutil
import zipfile
import threading
import time
import re
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory, send_file
from flask_cors import CORS

app = Flask(__name__, static_folder="static")
CORS(app)

# ---------------------------------------------------------------------------
# Global progress tracking
# ---------------------------------------------------------------------------
export_progress = {
    "running": False,
    "percent": 0,
    "status": "",
    "done": False,
    "error": None,
    "output_path": None,
}


def reset_progress():
    export_progress.update(
        running=False, percent=0, status="", done=False, error=None, output_path=None
    )


# ---------------------------------------------------------------------------
# PARAM.SFO parser
# ---------------------------------------------------------------------------

def parse_sfo(path):
    """Parse a PS3 PARAM.SFO file and return a dict of key-value pairs."""
    result = {}
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
            if magic != b"\x00PSF":
                return result

            version = struct.unpack("<I", f.read(4))[0]
            key_table_start = struct.unpack("<I", f.read(4))[0]
            data_table_start = struct.unpack("<I", f.read(4))[0]
            tables_entries = struct.unpack("<I", f.read(4))[0]

            entries = []
            for _ in range(tables_entries):
                key_offset = struct.unpack("<H", f.read(2))[0]
                data_fmt = struct.unpack("<H", f.read(2))[0]
                data_len = struct.unpack("<I", f.read(4))[0]
                data_max_len = struct.unpack("<I", f.read(4))[0]
                data_offset = struct.unpack("<I", f.read(4))[0]
                entries.append((key_offset, data_fmt, data_len, data_max_len, data_offset))

            for key_offset, data_fmt, data_len, data_max_len, data_offset in entries:
                f.seek(key_table_start + key_offset)
                key = b""
                while True:
                    c = f.read(1)
                    if c == b"\x00" or c == b"":
                        break
                    key += c
                key = key.decode("utf-8", errors="replace")

                f.seek(data_table_start + data_offset)
                if data_fmt == 0x0404:  # integer
                    value = struct.unpack("<I", f.read(4))[0]
                elif data_fmt == 0x0204:  # utf-8 string
                    value = f.read(data_len).rstrip(b"\x00").decode("utf-8", errors="replace")
                else:  # raw bytes
                    value = f.read(data_len).hex()

                result[key] = value
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_dir_size(path):
    """Return total size in bytes of a directory tree."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def copy_tree_streaming(src, dst, progress_callback=None):
    """Copy a directory tree with optional progress callback."""
    if not os.path.isdir(src):
        return
    total = get_dir_size(src)
    copied = 0
    for dirpath, dirnames, filenames in os.walk(src):
        rel = os.path.relpath(dirpath, src)
        dest_dir = os.path.join(dst, rel)
        os.makedirs(dest_dir, exist_ok=True)
        for fn in filenames:
            src_file = os.path.join(dirpath, fn)
            dst_file = os.path.join(dest_dir, fn)
            try:
                sz = os.path.getsize(src_file)
                shutil.copy2(src_file, dst_file)
                copied += sz
                if progress_callback and total > 0:
                    progress_callback(copied / total)
            except OSError:
                pass


def extract_game_patches(patch_yml_path, game_id):
    """Extract patch entries for a specific game ID from patch.yml.

    RPCS3 patch.yml uses a nested YAML structure. We do a simplified text-based
    extraction: grab all top-level keys whose block references the game ID.
    """
    if not os.path.isfile(patch_yml_path):
        return None

    try:
        with open(patch_yml_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception:
        return None

    # patch.yml has a structure where game title hashes are top-level keys.
    # Each key block may contain the game ID string. We extract full blocks.
    lines = content.split("\n")
    header_lines = []
    blocks = []
    current_block = []
    current_is_top = False

    for line in lines:
        # Version/metadata lines at the very top (before first key)
        if not blocks and not current_block and (line.startswith("Version:") or line.strip() == "" or line.startswith("#")):
            header_lines.append(line)
            continue

        # A top-level key starts at column 0 and is not a comment/blank
        if line and not line[0].isspace() and not line.startswith("#"):
            if current_block:
                blocks.append(current_block)
            current_block = [line]
        else:
            current_block.append(line)

    if current_block:
        blocks.append(current_block)

    matched = []
    for block in blocks:
        block_text = "\n".join(block)
        if game_id in block_text:
            matched.append(block_text)

    if not matched:
        return None

    return "\n".join(header_lines) + "\n" + "\n\n".join(matched) + "\n"


def detect_rpcs3_version(rpcs3_root):
    """Try to detect RPCS3 version from the installation."""
    # Try reading from rpcs3.exe file version (Windows)
    exe_path = os.path.join(rpcs3_root, "rpcs3.exe")
    if os.path.isfile(exe_path):
        try:
            # Try to read PE version info — simplified approach
            with open(exe_path, "rb") as f:
                data = f.read(min(os.path.getsize(exe_path), 4 * 1024 * 1024))
                # Look for version string pattern
                for pattern in [b"ProductVersion", b"FileVersion"]:
                    idx = data.find(pattern)
                    if idx != -1:
                        # Skip past the key and padding
                        start = idx + len(pattern)
                        # Scan forward for version-like string
                        chunk = data[start:start + 200]
                        # Find printable version string
                        ver = ""
                        for b in chunk:
                            if 0x20 <= b < 0x7F:
                                ver += chr(b)
                            elif ver and b == 0:
                                if re.match(r"[\d.]+", ver.strip()):
                                    return ver.strip()
                                ver = ""
        except Exception:
            pass

    # Try GuiConfigs
    gui_cfg = os.path.join(rpcs3_root, "GuiConfigs", "CurrentSettings.ini")
    if os.path.isfile(gui_cfg):
        try:
            with open(gui_cfg, "r", errors="replace") as f:
                for line in f:
                    if "version" in line.lower():
                        return line.strip()
        except Exception:
            pass

    return "Unknown"


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

@app.route("/api/browse", methods=["POST"])
def browse():
    """Open a native OS folder/file picker dialog and return the selected path."""
    data = request.json or {}
    mode = data.get("mode", "folder")  # "folder" or "file"
    title = data.get("title", "Select a folder")
    initial_dir = data.get("initial_dir", "").strip() or None
    filetypes = data.get("filetypes", None)  # e.g. [["ZIP files", "*.zip"]]

    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        # Bring dialog to front on Windows
        root.attributes("-topmost", True)

        if mode == "folder":
            path = filedialog.askdirectory(title=title, initialdir=initial_dir)
        else:
            ft = []
            if filetypes:
                ft = [tuple(f) for f in filetypes]
            ft.append(("All files", "*.*"))
            path = filedialog.askopenfilename(title=title, initialdir=initial_dir, filetypes=ft)

        root.destroy()

        if not path:
            return jsonify({"path": None, "cancelled": True})
        return jsonify({"path": path, "cancelled": False})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


def find_disc_game_folders(rpcs3_root):
    """Scan the games/ folder (disc game storage) and build a map of TITLE_ID -> folder path.

    RPCS3 stores extracted disc games in {rpcs3_root}/games/ with human-readable
    folder names. Each contains PS3_GAME/PARAM.SFO with the TITLE_ID that links
    it to the matching entry in dev_hdd0/game/ (updates/DLC).
    """
    disc_map = {}  # title_id -> full path to disc folder
    games_folder = os.path.join(rpcs3_root, "games")
    if not os.path.isdir(games_folder):
        return disc_map

    for entry in os.listdir(games_folder):
        entry_path = os.path.join(games_folder, entry)
        if not os.path.isdir(entry_path):
            continue
        # Standard disc game structure: PS3_GAME/PARAM.SFO
        sfo_path = os.path.join(entry_path, "PS3_GAME", "PARAM.SFO")
        if os.path.isfile(sfo_path):
            sfo = parse_sfo(sfo_path)
            title_id = sfo.get("TITLE_ID", "")
            if title_id:
                disc_map[title_id] = entry_path
    return disc_map


@app.route("/api/scan", methods=["POST"])
def scan_games():
    """Scan an RPCS3 root directory and return a list of installed games."""
    data = request.json or {}
    rpcs3_root = data.get("rpcs3_root", "").strip()

    if not rpcs3_root or not os.path.isdir(rpcs3_root):
        return jsonify({"error": "Invalid RPCS3 directory path"}), 400

    game_dir = os.path.join(rpcs3_root, "dev_hdd0", "game")
    if not os.path.isdir(game_dir):
        return jsonify({"error": "Could not find dev_hdd0/game/ in the specified directory"}), 400

    # Build mapping of TITLE_ID -> disc game folder from games/ directory
    disc_map = find_disc_game_folders(rpcs3_root)

    games = []
    seen_ids = set()
    try:
        # Scan dev_hdd0/game/ (updates, DLC, digital titles)
        for entry in sorted(os.listdir(game_dir)):
            entry_path = os.path.join(game_dir, entry)
            if not os.path.isdir(entry_path):
                continue

            sfo_path = os.path.join(entry_path, "PARAM.SFO")
            title = entry  # fallback
            category = ""
            version = ""

            if os.path.isfile(sfo_path):
                sfo = parse_sfo(sfo_path)
                title = sfo.get("TITLE", entry)
                category = sfo.get("CATEGORY", "")
                version = sfo.get("APP_VER", sfo.get("VERSION", ""))

            # Calculate total size: dev_hdd0/game + disc folder if present
            size = get_dir_size(entry_path)
            disc_folder = disc_map.get(entry)
            if disc_folder:
                size += get_dir_size(disc_folder)

            # Check for disc content in dev_hdd0/disc/ as well
            has_disc_hdd = os.path.isdir(os.path.join(rpcs3_root, "dev_hdd0", "disc", entry))
            has_disc = bool(disc_folder) or has_disc_hdd

            save_dir = os.path.join(rpcs3_root, "dev_hdd0", "home", "00000001", "savedata")
            has_saves = False
            save_count = 0
            if os.path.isdir(save_dir):
                for sd in os.listdir(save_dir):
                    if sd.startswith(entry):
                        has_saves = True
                        save_count += 1
            has_config = os.path.isfile(
                os.path.join(rpcs3_root, "config", "custom_configs", f"{entry}_config.yml")
            )

            games.append({
                "id": entry,
                "title": title,
                "category": category,
                "version": version,
                "size": size,
                "has_disc": has_disc,
                "disc_folder": disc_folder or "",
                "has_saves": has_saves,
                "save_count": save_count,
                "has_config": has_config,
            })
            seen_ids.add(entry)

        # Also pick up any disc games that DON'T have a dev_hdd0/game/ entry
        for title_id, disc_folder in disc_map.items():
            if title_id in seen_ids:
                continue
            sfo_path = os.path.join(disc_folder, "PS3_GAME", "PARAM.SFO")
            sfo = parse_sfo(sfo_path) if os.path.isfile(sfo_path) else {}
            title = sfo.get("TITLE", title_id)
            category = sfo.get("CATEGORY", "DG")
            version = sfo.get("APP_VER", sfo.get("VERSION", ""))
            size = get_dir_size(disc_folder)

            save_dir = os.path.join(rpcs3_root, "dev_hdd0", "home", "00000001", "savedata")
            has_saves = False
            save_count = 0
            if os.path.isdir(save_dir):
                for sd in os.listdir(save_dir):
                    if sd.startswith(title_id):
                        has_saves = True
                        save_count += 1
            has_config = os.path.isfile(
                os.path.join(rpcs3_root, "config", "custom_configs", f"{title_id}_config.yml")
            )

            games.append({
                "id": title_id,
                "title": title,
                "category": category,
                "version": version,
                "size": size,
                "has_disc": True,
                "disc_folder": disc_folder,
                "has_saves": has_saves,
                "save_count": save_count,
                "has_config": has_config,
            })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Sort by title
    games.sort(key=lambda g: g["title"].lower())

    # Detect RPCS3 version
    rpcs3_version = detect_rpcs3_version(rpcs3_root)

    return jsonify({"games": games, "rpcs3_version": rpcs3_version})


@app.route("/api/export", methods=["POST"])
def start_export():
    """Start an export operation in a background thread.
    Always includes everything: game data, disc, saves, patches, config — one file."""
    if export_progress["running"]:
        return jsonify({"error": "An export is already in progress"}), 409

    data = request.json or {}
    rpcs3_root = data.get("rpcs3_root", "").strip()
    game_id = data.get("game_id", "").strip()
    output_dir = data.get("output_dir", "").strip()
    disc_folder = data.get("disc_folder", "").strip()  # auto-detected from scan
    game_title = data.get("game_title", "").strip()

    if not rpcs3_root or not os.path.isdir(rpcs3_root):
        return jsonify({"error": "Invalid RPCS3 directory"}), 400
    if not game_id:
        return jsonify({"error": "No game ID specified"}), 400
    if not output_dir:
        return jsonify({"error": "No output directory specified"}), 400

    # Auto-detect disc folder if not provided
    if not disc_folder:
        disc_map = find_disc_game_folders(rpcs3_root)
        disc_folder = disc_map.get(game_id, "")

    reset_progress()
    export_progress["running"] = True

    thread = threading.Thread(
        target=run_export,
        args=(rpcs3_root, game_id, output_dir, disc_folder, game_title),
        daemon=True,
    )
    thread.start()
    return jsonify({"status": "started"})


def sanitize_filename(name):
    """Remove characters that are invalid in file/folder names."""
    sanitized = re.sub(r'[<>:"/\\|?*]', '', name)
    sanitized = sanitized.replace('\u00ae', '').replace('\u00a9', '').replace('\u2122', '')
    # Replace the special trademark-like chars from PS3 titles
    sanitized = re.sub(r'[^\x20-\x7E]', '', sanitized)
    return sanitized.strip().rstrip('.')


def run_export(rpcs3_root, game_id, output_dir, disc_folder, game_title_hint=""):
    """Export everything for a game into a single ZIP archive."""
    try:
        os.makedirs(output_dir, exist_ok=True)

        # Determine game title for the export folder/zip name
        game_path = os.path.join(rpcs3_root, "dev_hdd0", "game", game_id)
        title = game_title_hint or game_id
        if not game_title_hint:
            if disc_folder and os.path.isdir(disc_folder):
                disc_sfo = os.path.join(disc_folder, "PS3_GAME", "PARAM.SFO")
                if os.path.isfile(disc_sfo):
                    sfo = parse_sfo(disc_sfo)
                    title = sfo.get("TITLE", game_id)
            if title == game_id:
                sfo_path = os.path.join(game_path, "PARAM.SFO")
                if os.path.isfile(sfo_path):
                    sfo = parse_sfo(sfo_path)
                    title = sfo.get("TITLE", game_id)

        safe_title = sanitize_filename(title) or game_id
        export_name = safe_title
        export_root = os.path.join(output_dir, export_name)

        # Clean previous export if exists
        if os.path.isdir(export_root):
            shutil.rmtree(export_root)
        os.makedirs(export_root, exist_ok=True)

        manifest = {
            "game_id": game_id,
            "game_title": title,
            "export_date": datetime.now().isoformat(),
            "rpcs3_version": detect_rpcs3_version(rpcs3_root),
            "included": [],
            "original_paths": {},
        }

        # Count steps for progress: disc_game + updates_dlc + saves + patches + config + zip
        total_steps = 6
        current_step = 0

        def update(step_name, sub_progress=None):
            base = (current_step / total_steps) * 100
            step_range = 100 / total_steps
            if sub_progress is not None:
                export_progress["percent"] = int(base + step_range * sub_progress)
            else:
                export_progress["percent"] = int(base)
            export_progress["status"] = step_name

        # --- Disc game folder (the big one from games/) ---
        if disc_folder and os.path.isdir(disc_folder):
            update("Copying disc game files...")
            dest = os.path.join(export_root, "disc_game")
            os.makedirs(dest, exist_ok=True)
            copy_tree_streaming(disc_folder, dest, lambda p: update("Copying disc game files...", p))
            manifest["included"].append("disc_game")
            manifest["original_paths"]["disc_game"] = disc_folder
        current_step += 1

        # --- Updates + DLC from dev_hdd0/game/ ---
        if os.path.isdir(game_path):
            update("Copying updates + DLC...")
            dest = os.path.join(export_root, "game", game_id)
            os.makedirs(dest, exist_ok=True)
            copy_tree_streaming(game_path, dest, lambda p: update("Copying updates + DLC...", p))
            manifest["included"].append("game_data")
            manifest["original_paths"]["game_data"] = game_path
        current_step += 1

        # --- Save data ---
        update("Copying save data...")
        save_dir = os.path.join(rpcs3_root, "dev_hdd0", "home", "00000001", "savedata")
        saves_found = []
        if os.path.isdir(save_dir):
            for sd in sorted(os.listdir(save_dir)):
                if sd.startswith(game_id):
                    src = os.path.join(save_dir, sd)
                    dst = os.path.join(export_root, "savedata", sd)
                    os.makedirs(dst, exist_ok=True)
                    copy_tree_streaming(src, dst)
                    saves_found.append(sd)
        if saves_found:
            manifest["included"].append("save_data")
            manifest["original_paths"]["save_data"] = save_dir
            manifest["save_folders"] = saves_found
        current_step += 1

        # --- Patches ---
        update("Extracting patches...")
        patch_path = os.path.join(rpcs3_root, "patches", "patch.yml")
        patch_content = extract_game_patches(patch_path, game_id)
        if patch_content:
            patches_dir = os.path.join(export_root, "patches")
            os.makedirs(patches_dir, exist_ok=True)
            with open(os.path.join(patches_dir, "patch.yml"), "w", encoding="utf-8") as f:
                f.write(patch_content)
            manifest["included"].append("patches")
            manifest["original_paths"]["patches"] = patch_path
        current_step += 1

        # --- Custom config ---
        update("Copying custom config...")
        config_path = os.path.join(rpcs3_root, "config", "custom_configs", f"{game_id}_config.yml")
        if os.path.isfile(config_path):
            shutil.copy2(config_path, os.path.join(export_root, "custom_config.yml"))
            manifest["included"].append("custom_config")
            manifest["original_paths"]["custom_config"] = config_path
        current_step += 1

        # --- Write manifest ---
        with open(os.path.join(export_root, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

        # --- Create ZIP archive ---
        update("Creating ZIP archive...")
        zip_path = export_root + ".zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            file_list = []
            for dirpath, dirnames, filenames in os.walk(export_root):
                for fn in filenames:
                    file_list.append(os.path.join(dirpath, fn))

            for i, fpath in enumerate(file_list):
                arcname = os.path.relpath(fpath, output_dir)
                zf.write(fpath, arcname)
                if file_list:
                    update("Creating ZIP archive...", (i + 1) / len(file_list))

        # Remove uncompressed folder — only the .zip remains
        shutil.rmtree(export_root)
        current_step += 1

        export_progress["percent"] = 100
        export_progress["status"] = "Complete"
        export_progress["done"] = True
        export_progress["output_path"] = zip_path

    except Exception as e:
        export_progress["error"] = str(e)
        export_progress["status"] = f"Error: {e}"
        export_progress["done"] = True
    finally:
        export_progress["running"] = False


@app.route("/api/progress")
def get_progress():
    return jsonify(export_progress)


@app.route("/api/import", methods=["POST"])
def start_import():
    """Reimport an exported archive — restores everything in the archive automatically."""
    data = request.json or {}
    rpcs3_root = data.get("rpcs3_root", "").strip()
    archive_path = data.get("archive_path", "").strip()

    if not rpcs3_root or not os.path.isdir(rpcs3_root):
        return jsonify({"error": "Invalid RPCS3 directory"}), 400
    if not archive_path or not os.path.exists(archive_path):
        return jsonify({"error": "Archive not found"}), 400

    try:
        # If it's a zip, extract to temp
        if archive_path.endswith(".zip"):
            import tempfile
            tmp = tempfile.mkdtemp(prefix="rpcs3_import_")
            with zipfile.ZipFile(archive_path, "r") as zf:
                zf.extractall(tmp)
            # Find the export directory — look for manifest.json
            export_dir = None
            for entry in os.listdir(tmp):
                candidate = os.path.join(tmp, entry)
                if os.path.isdir(candidate) and os.path.isfile(os.path.join(candidate, "manifest.json")):
                    export_dir = candidate
                    break
            if not export_dir:
                if os.path.isfile(os.path.join(tmp, "manifest.json")):
                    export_dir = tmp
                else:
                    return jsonify({"error": "Could not find export directory in archive"}), 400
        elif os.path.isdir(archive_path):
            export_dir = archive_path
            tmp = None
        else:
            return jsonify({"error": "Unsupported archive format"}), 400

        # Read manifest
        manifest_path = os.path.join(export_dir, "manifest.json")
        if not os.path.isfile(manifest_path):
            return jsonify({"error": "No manifest.json found in export"}), 400

        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        game_id = manifest["game_id"]
        results = []

        # Restore disc game (the large game folder -> games/ directory)
        disc_game_src = os.path.join(export_dir, "disc_game")
        if os.path.isdir(disc_game_src):
            # Use original path from manifest if available, otherwise use game title
            orig_path = manifest.get("original_paths", {}).get("disc_game", "")
            if orig_path:
                folder_name = os.path.basename(orig_path)
            else:
                folder_name = manifest.get("game_title", game_id)
            disc_dst = os.path.join(rpcs3_root, "games", folder_name)
            os.makedirs(os.path.join(rpcs3_root, "games"), exist_ok=True)
            copy_tree_streaming(disc_game_src, disc_dst)
            results.append(f"Disc game restored to games/{folder_name}")

        # Restore updates + DLC (-> dev_hdd0/game/)
        game_src = os.path.join(export_dir, "game", game_id)
        if os.path.isdir(game_src):
            game_dst = os.path.join(rpcs3_root, "dev_hdd0", "game", game_id)
            copy_tree_streaming(game_src, game_dst)
            results.append("Updates + DLC restored")

        # Restore saves
        save_src = os.path.join(export_dir, "savedata")
        if os.path.isdir(save_src):
            save_dst = os.path.join(rpcs3_root, "dev_hdd0", "home", "00000001", "savedata")
            os.makedirs(save_dst, exist_ok=True)
            for sd in os.listdir(save_src):
                copy_tree_streaming(
                    os.path.join(save_src, sd),
                    os.path.join(save_dst, sd),
                )
            results.append("Save data restored")

        # Merge patches
        patch_src = os.path.join(export_dir, "patches", "patch.yml")
        if os.path.isfile(patch_src):
            patch_dst = os.path.join(rpcs3_root, "patches", "patch.yml")
            with open(patch_src, "r", encoding="utf-8") as f:
                new_patches = f.read()

            if os.path.isfile(patch_dst):
                with open(patch_dst, "r", encoding="utf-8") as f:
                    existing = f.read()
                if game_id not in existing:
                    with open(patch_dst, "a", encoding="utf-8") as f:
                        f.write("\n" + new_patches)
                    results.append("Patches merged")
                else:
                    results.append("Patches already present (skipped)")
            else:
                os.makedirs(os.path.dirname(patch_dst), exist_ok=True)
                shutil.copy2(patch_src, patch_dst)
                results.append("Patches installed")

        # Restore config
        config_src = os.path.join(export_dir, "custom_config.yml")
        if os.path.isfile(config_src):
            config_dst = os.path.join(rpcs3_root, "config", "custom_configs", f"{game_id}_config.yml")
            os.makedirs(os.path.dirname(config_dst), exist_ok=True)
            shutil.copy2(config_src, config_dst)
            results.append("Custom config restored")

        # Cleanup temp
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

        return jsonify({"status": "success", "results": results, "game_id": game_id, "game_title": manifest.get("game_title", "")})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Serve static files
# ---------------------------------------------------------------------------

@app.route("/<path:path>")
def static_files(path):
    return send_from_directory("static", path)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    print(f"RPCS3 Export Tool running at http://localhost:{port}")
    app.run(host="127.0.0.1", port=port, debug=True)
