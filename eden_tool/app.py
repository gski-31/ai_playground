"""
Eden Game Export Tool — Backend
Flask server that handles NSP discovery, and export/import operations.
"""

import os
import sys
import re
import json
import shutil
import zipfile
import threading
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory
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
# NSP filename parser
# ---------------------------------------------------------------------------

NSP_PATTERN = re.compile(
    r"^(?P<title>.+?)\s*"
    r"\[(?P<title_id>[0-9A-Fa-f]{16})\]"
    r"\[v(?P<version>\d+)\]"
    r"\[(?P<type>Base|Update|DLC)\]"
    r"\.nsp$"
)


def parse_nsp_filename(filename):
    """Parse an NSP filename and return metadata dict, or None if it doesn't match."""
    m = NSP_PATTERN.match(filename)
    if not m:
        return None
    return {
        "filename": filename,
        "title": m.group("title").strip(),
        "title_id": m.group("title_id").upper(),
        "version": int(m.group("version")),
        "type": m.group("type"),  # Base, Update, or DLC
    }


def get_base_title_id(title_id):
    """Derive the base title ID from an update or DLC title ID.

    Nintendo Switch title ID scheme:
    - Base game ends in 000  (e.g. 0100152000022000)
    - Update ends in 800     (e.g. 0100152000022800)
    - DLC starts from 001    (e.g. 0100152000022001, ...002, etc.)

    We mask the last 3 hex digits to get the base ID.
    """
    if len(title_id) != 16:
        return title_id
    prefix = title_id[:13]
    return prefix + "000"


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


def get_eden_appdata():
    """Get the Eden AppData/Roaming path."""
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        return os.path.join(appdata, "eden")
    # Linux fallback
    home = os.path.expanduser("~")
    return os.path.join(home, ".local", "share", "eden")


def parse_installed_title_ids(eden_data_dir):
    """Parse qt-config.ini to find all installed title IDs (updates, DLC).

    Returns a dict mapping base_title_id -> list of installed addon title IDs.
    """
    from collections import defaultdict
    config_path = os.path.join(eden_data_dir, "config", "qt-config.ini")
    addons = defaultdict(list)  # base_id -> [(title_id, kind)]

    if not os.path.isfile(config_path):
        return addons

    try:
        in_section = False
        with open(config_path, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line == "[DisabledAddOns]":
                    in_section = True
                    continue
                if line.startswith("[") and in_section:
                    break
                if in_section and "title_id=" in line and "default" not in line:
                    val = line.split("=", 1)[1].strip()
                    try:
                        dec = int(val)
                        hex_id = format(dec, "016X")
                        suffix = hex_id[-3:]
                        if suffix == "000":
                            kind = "Base"
                        elif suffix == "800":
                            kind = "Update"
                        else:
                            kind = "DLC"
                        base_id = hex_id[:13] + "000"
                        if kind != "Base":
                            addons[base_id].append({"id": hex_id, "type": kind})
                    except ValueError:
                        pass
    except Exception:
        pass

    return addons


def find_save_dirs_for_title(eden_data_dir, title_id):
    """Find all save directories for a given title ID.

    Saves live in: {eden_data}/nand/user/save/0000000000000000/{hash}/{title_id}/
    """
    saves = []
    save_root = os.path.join(eden_data_dir, "nand", "user", "save")
    if not os.path.isdir(save_root):
        return saves

    for uid in os.listdir(save_root):
        uid_path = os.path.join(save_root, uid)
        if not os.path.isdir(uid_path):
            continue
        for hash_dir in os.listdir(uid_path):
            hash_path = os.path.join(uid_path, hash_dir)
            if not os.path.isdir(hash_path):
                continue
            title_path = os.path.join(hash_path, title_id)
            if os.path.isdir(title_path):
                saves.append({
                    "path": title_path,
                    "uid": uid,
                    "hash": hash_dir,
                })
    return saves


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


def copy_file_streaming(src, dst, progress_callback=None):
    """Copy a single large file with progress callback."""
    total = os.path.getsize(src)
    copied = 0
    buf_size = 16 * 1024 * 1024  # 16 MB chunks
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        while True:
            buf = fsrc.read(buf_size)
            if not buf:
                break
            fdst.write(buf)
            copied += len(buf)
            if progress_callback and total > 0:
                progress_callback(copied / total)
    # Preserve timestamps
    shutil.copystat(src, dst)


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------

@app.route("/api/browse", methods=["POST"])
def browse():
    """Open a native OS folder/file picker dialog."""
    data = request.json or {}
    mode = data.get("mode", "folder")
    title = data.get("title", "Select a folder")
    initial_dir = data.get("initial_dir", "").strip() or None
    filetypes = data.get("filetypes", None)

    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
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


@app.route("/api/scan", methods=["POST"])
def scan_games():
    """Scan an Eden root directory and return a list of games with all related files."""
    data = request.json or {}
    eden_root = data.get("eden_root", "").strip()

    if not eden_root or not os.path.isdir(eden_root):
        return jsonify({"error": "Invalid Eden directory path"}), 400

    nsp_dir = os.path.join(eden_root, "NSPs")
    if not os.path.isdir(nsp_dir):
        return jsonify({"error": "Could not find NSPs/ folder in the specified directory"}), 400

    eden_data = get_eden_appdata()

    # Parse all NSP files
    all_nsps = []
    for fn in os.listdir(nsp_dir):
        if not fn.lower().endswith(".nsp"):
            continue
        meta = parse_nsp_filename(fn)
        if meta:
            meta["file_path"] = os.path.join(nsp_dir, fn)
            try:
                meta["file_size"] = os.path.getsize(meta["file_path"])
            except OSError:
                meta["file_size"] = 0
            all_nsps.append(meta)

    # Group by base title ID
    games = {}
    for nsp in all_nsps:
        if nsp["type"] == "Base":
            base_id = nsp["title_id"]
        else:
            base_id = get_base_title_id(nsp["title_id"])

        if base_id not in games:
            games[base_id] = {
                "id": base_id,
                "title": "",
                "base_nsp": None,
                "updates": [],
                "dlc": [],
                "total_size": 0,
            }

        g = games[base_id]
        g["total_size"] += nsp["file_size"]

        if nsp["type"] == "Base":
            g["base_nsp"] = nsp
            g["title"] = nsp["title"]
        elif nsp["type"] == "Update":
            g["updates"].append(nsp)
        elif nsp["type"] == "DLC":
            g["dlc"].append(nsp)

    # Fill in titles for games that only have updates/DLC but no base (edge case)
    for gid, g in games.items():
        if not g["title"]:
            # Use title from first update or DLC
            for nsp in g["updates"] + g["dlc"]:
                if nsp["title"]:
                    g["title"] = nsp["title"]
                    break
            if not g["title"]:
                g["title"] = gid

    # Parse installed updates/DLC from config
    installed_addons = parse_installed_title_ids(eden_data)

    # Check size of registered content (shared across all games)
    registered_path = os.path.join(eden_data, "nand", "user", "Contents", "registered")
    has_registered = os.path.isdir(registered_path)

    # Check for saves, mods, load patches, custom config
    result = []
    for gid, g in sorted(games.items(), key=lambda x: x[1]["title"].lower()):
        # Saves
        saves = find_save_dirs_for_title(eden_data, gid)

        # Installed updates/DLC (from config, stored in registered/)
        addons = installed_addons.get(gid, [])
        installed_updates = [a for a in addons if a["type"] == "Update"]
        installed_dlc = [a for a in addons if a["type"] == "DLC"]

        # Mods (atmosphere)
        mods_path = os.path.join(eden_data, "sdmc", "atmosphere", "contents", gid)
        has_mods = os.path.isdir(mods_path) and any(os.listdir(mods_path))

        # Load dir
        load_path = os.path.join(eden_data, "load", gid)
        has_load = os.path.isdir(load_path) and any(os.listdir(load_path))

        # Custom config
        config_path = os.path.join(eden_data, "config", "custom", f"{gid}.ini")
        has_config = os.path.isfile(config_path)

        # Total size includes NSPs + registered content can't be split per-game
        total_size = g["total_size"]

        result.append({
            "id": gid,
            "title": g["title"],
            "base_nsp": g["base_nsp"]["filename"] if g["base_nsp"] else None,
            "update_count": len(g["updates"]) + len(installed_updates),
            "dlc_count": len(g["dlc"]) + len(installed_dlc),
            "total_size": total_size,
            "save_count": len(saves),
            "has_mods": has_mods,
            "has_load": has_load,
            "has_config": has_config,
            "has_registered_content": has_registered and len(addons) > 0,
            # Pass file lists for export
            "_nsps": [n["filename"] for n in ([g["base_nsp"]] if g["base_nsp"] else []) + g["updates"] + g["dlc"]],
        })

    return jsonify({"games": result, "eden_data": eden_data})


@app.route("/api/export", methods=["POST"])
def start_export():
    """Start an export — bundles all NSPs, saves, mods, config for a game into one ZIP."""
    if export_progress["running"]:
        return jsonify({"error": "An export is already in progress"}), 409

    data = request.json or {}
    eden_root = data.get("eden_root", "").strip()
    title_id = data.get("title_id", "").strip()
    output_dir = data.get("output_dir", "").strip()
    nsp_files = data.get("nsp_files", [])  # list of filenames from scan
    game_title = data.get("game_title", "").strip()

    if not eden_root or not os.path.isdir(eden_root):
        return jsonify({"error": "Invalid Eden directory"}), 400
    if not title_id:
        return jsonify({"error": "No game selected"}), 400
    if not output_dir:
        return jsonify({"error": "No output directory specified"}), 400

    reset_progress()
    export_progress["running"] = True

    thread = threading.Thread(
        target=run_export,
        args=(eden_root, title_id, output_dir, nsp_files, game_title),
        daemon=True,
    )
    thread.start()
    return jsonify({"status": "started"})


def sanitize_filename(name):
    """Remove characters that are invalid in file/folder names."""
    # Replace characters not allowed in Windows filenames
    sanitized = re.sub(r'[<>:"/\\|?*]', '', name)
    # Replace special unicode chars that cause issues
    sanitized = sanitized.replace('\u00ae', '').replace('\u00a9', '').replace('\u2122', '')
    return sanitized.strip().rstrip('.')


def run_export(eden_root, title_id, output_dir, nsp_files, game_title_hint=""):
    """Export everything for a game into a single ZIP."""
    try:
        os.makedirs(output_dir, exist_ok=True)
        eden_data = get_eden_appdata()
        nsp_dir = os.path.join(eden_root, "NSPs")

        # Determine game title for naming
        game_title = game_title_hint or title_id
        if not game_title_hint:
            for fn in nsp_files:
                meta = parse_nsp_filename(fn)
                if meta and meta["type"] == "Base":
                    game_title = meta["title"]
                    break

        safe_title = sanitize_filename(game_title) or title_id
        export_name = safe_title
        export_root = os.path.join(output_dir, export_name)

        if os.path.isdir(export_root):
            shutil.rmtree(export_root)
        os.makedirs(export_root, exist_ok=True)

        manifest = {
            "title_id": title_id,
            "game_title": game_title,
            "export_date": datetime.now().isoformat(),
            "included": [],
            "nsp_files": [],
        }

        # Steps: NSPs (each file) + registered + saves + mods + load + config + zip
        total_steps = len(nsp_files) + 6
        current_step = 0

        def update(step_name, sub_progress=None):
            base = (current_step / total_steps) * 100
            step_range = 100 / total_steps
            if sub_progress is not None:
                export_progress["percent"] = min(99, int(base + step_range * sub_progress))
            else:
                export_progress["percent"] = min(99, int(base))
            export_progress["status"] = step_name

        # --- Copy NSP files ---
        nsps_dest = os.path.join(export_root, "NSPs")
        os.makedirs(nsps_dest, exist_ok=True)
        for fn in nsp_files:
            src = os.path.join(nsp_dir, fn)
            if os.path.isfile(src):
                update(f"Copying {fn[:50]}...")
                dst = os.path.join(nsps_dest, fn)
                copy_file_streaming(src, dst, lambda p: update(f"Copying {fn[:50]}...", p))
                manifest["nsp_files"].append(fn)
                manifest["included"].append(f"nsp:{fn}")
            current_step += 1

        # --- Installed updates + DLC (registered content) ---
        update("Copying installed updates + DLC...")
        registered_path = os.path.join(eden_data, "nand", "user", "Contents", "registered")
        if os.path.isdir(registered_path):
            dest = os.path.join(export_root, "registered")
            os.makedirs(dest, exist_ok=True)
            copy_tree_streaming(registered_path, dest,
                                lambda p: update("Copying installed updates + DLC...", p))
            manifest["included"].append("registered_content")
        current_step += 1

        # --- Save data ---
        update("Copying save data...")
        saves = find_save_dirs_for_title(eden_data, title_id)
        if saves:
            for sv in saves:
                rel_path = os.path.join("saves", sv["uid"], sv["hash"], title_id)
                dest = os.path.join(export_root, rel_path)
                os.makedirs(dest, exist_ok=True)
                copy_tree_streaming(sv["path"], dest)
            manifest["included"].append("save_data")
            manifest["save_count"] = len(saves)
        current_step += 1

        # --- Mods (atmosphere) ---
        update("Copying mods...")
        mods_path = os.path.join(eden_data, "sdmc", "atmosphere", "contents", title_id)
        if os.path.isdir(mods_path) and any(os.listdir(mods_path)):
            dest = os.path.join(export_root, "mods", title_id)
            os.makedirs(dest, exist_ok=True)
            copy_tree_streaming(mods_path, dest)
            manifest["included"].append("mods")
        current_step += 1

        # --- Load patches ---
        update("Copying load patches...")
        load_path = os.path.join(eden_data, "load", title_id)
        if os.path.isdir(load_path) and any(os.listdir(load_path)):
            dest = os.path.join(export_root, "load", title_id)
            os.makedirs(dest, exist_ok=True)
            copy_tree_streaming(load_path, dest)
            manifest["included"].append("load_patches")
        current_step += 1

        # --- Custom config ---
        update("Copying custom config...")
        config_path = os.path.join(eden_data, "config", "custom", f"{title_id}.ini")
        if os.path.isfile(config_path):
            dest = os.path.join(export_root, "config")
            os.makedirs(dest, exist_ok=True)
            shutil.copy2(config_path, os.path.join(dest, f"{title_id}.ini"))
            manifest["included"].append("custom_config")
        current_step += 1

        # --- Write manifest ---
        with open(os.path.join(export_root, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

        # --- Create ZIP ---
        update("Creating ZIP archive...")
        zip_path = export_root + ".zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            file_list = []
            for dirpath, dirnames, filenames in os.walk(export_root):
                for fn in filenames:
                    file_list.append(os.path.join(dirpath, fn))

            for i, fpath in enumerate(file_list):
                arcname = os.path.relpath(fpath, output_dir)
                zf.write(fpath, arcname)
                if file_list:
                    update("Creating ZIP archive...", (i + 1) / len(file_list))

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
    """Reimport an exported archive — restores everything automatically."""
    data = request.json or {}
    eden_root = data.get("eden_root", "").strip()
    archive_path = data.get("archive_path", "").strip()

    if not eden_root or not os.path.isdir(eden_root):
        return jsonify({"error": "Invalid Eden directory"}), 400
    if not archive_path or not os.path.exists(archive_path):
        return jsonify({"error": "Archive not found"}), 400

    try:
        import tempfile

        if archive_path.endswith(".zip"):
            tmp = tempfile.mkdtemp(prefix="eden_import_")
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
                # Maybe manifest.json is directly in tmp (flat zip)
                if os.path.isfile(os.path.join(tmp, "manifest.json")):
                    export_dir = tmp
                else:
                    return jsonify({"error": "Could not find export directory in archive"}), 400
        elif os.path.isdir(archive_path):
            export_dir = archive_path
            tmp = None
        else:
            return jsonify({"error": "Unsupported archive format"}), 400

        manifest_path = os.path.join(export_dir, "manifest.json")
        if not os.path.isfile(manifest_path):
            return jsonify({"error": "No manifest.json found in export"}), 400

        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        title_id = manifest["title_id"]
        eden_data = get_eden_appdata()
        results = []

        # Restore NSP files
        nsps_src = os.path.join(export_dir, "NSPs")
        if os.path.isdir(nsps_src):
            nsp_dst = os.path.join(eden_root, "NSPs")
            os.makedirs(nsp_dst, exist_ok=True)
            count = 0
            for fn in os.listdir(nsps_src):
                if fn.endswith(".nsp"):
                    src = os.path.join(nsps_src, fn)
                    dst = os.path.join(nsp_dst, fn)
                    if not os.path.exists(dst):
                        copy_file_streaming(src, dst)
                        count += 1
                    else:
                        count += 1  # already exists
            results.append(f"{count} NSP file(s) restored")

        # Restore registered content (updates + DLC)
        reg_src = os.path.join(export_dir, "registered")
        if os.path.isdir(reg_src):
            reg_dst = os.path.join(eden_data, "nand", "user", "Contents", "registered")
            os.makedirs(reg_dst, exist_ok=True)
            copy_tree_streaming(reg_src, reg_dst)
            results.append("Installed updates + DLC restored")

        # Restore saves
        saves_src = os.path.join(export_dir, "saves")
        if os.path.isdir(saves_src):
            save_dst_root = os.path.join(eden_data, "nand", "user", "save")
            os.makedirs(save_dst_root, exist_ok=True)
            copy_tree_streaming(saves_src, save_dst_root)
            results.append("Save data restored")

        # Restore mods
        mods_src = os.path.join(export_dir, "mods", title_id)
        if os.path.isdir(mods_src):
            mods_dst = os.path.join(eden_data, "sdmc", "atmosphere", "contents", title_id)
            os.makedirs(mods_dst, exist_ok=True)
            copy_tree_streaming(mods_src, mods_dst)
            results.append("Mods restored")

        # Restore load patches
        load_src = os.path.join(export_dir, "load", title_id)
        if os.path.isdir(load_src):
            load_dst = os.path.join(eden_data, "load", title_id)
            os.makedirs(load_dst, exist_ok=True)
            copy_tree_streaming(load_src, load_dst)
            results.append("Load patches restored")

        # Restore custom config
        config_src = os.path.join(export_dir, "config", f"{title_id}.ini")
        if os.path.isfile(config_src):
            config_dst = os.path.join(eden_data, "config", "custom", f"{title_id}.ini")
            os.makedirs(os.path.dirname(config_dst), exist_ok=True)
            shutil.copy2(config_src, config_dst)
            results.append("Custom config restored")

        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

        return jsonify({
            "status": "success",
            "results": results,
            "title_id": title_id,
            "game_title": manifest.get("game_title", ""),
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/<path:path>")
def static_files(path):
    return send_from_directory("static", path)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5001
    print(f"Eden Export Tool running at http://localhost:{port}")
    app.run(host="127.0.0.1", port=port, debug=True)
