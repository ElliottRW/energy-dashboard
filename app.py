"""Energy dashboard – the small web server that runs on your computer.

Start it with:   python app.py
Then open:       http://localhost:5080

With no API key in .env it shows made-up demo data so you can look around.
"""
import os
import threading
import time
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, jsonify, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

import analysis  # noqa: E402
from db import DB  # noqa: E402
from sync import SyncManager  # noqa: E402

PORT = int(os.environ.get("PORT", 5080))
API_KEY = os.environ.get("OCTOPUS_API_KEY", "").strip()
ACCOUNT = os.environ.get("OCTOPUS_ACCOUNT", "").strip()
GAS_UNITS = os.environ.get("GAS_UNITS", "auto").strip()
DEMO = os.environ.get("DEMO", "").strip() == "1" or not (API_KEY and ACCOUNT)
AUTO_SYNC_HOURS = float(os.environ.get("AUTO_SYNC_HOURS", 6))

if DEMO:
    import demo
    if not os.path.exists(demo.PATH) or os.path.getmtime(demo.PATH) < time.time() - 86400:
        print("Building demo data…", flush=True)
        demo.build()
    db = DB(demo.PATH)
    sync_manager = None
else:
    db = DB(os.path.join(HERE, "data", "energy.db"))
    sync_manager = SyncManager(db, API_KEY, ACCOUNT, GAS_UNITS)

app = Flask(__name__, static_folder=None)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

_cache = {"key": None, "data": None}
_cache_lock = threading.Lock()


def dashboard_data():
    key = (db.data_version(), datetime.now().strftime("%Y-%m-%d %H"))
    with _cache_lock:
        if _cache["key"] != key:
            _cache["data"] = analysis.build(db, demo=DEMO)
            _cache["key"] = key
        return _cache["data"]


@app.get("/")
def index():
    return send_from_directory(os.path.join(HERE, "static"), "index.html")


@app.get("/static/<path:name>")
def static_files(name):
    return send_from_directory(os.path.join(HERE, "static"), name)


@app.get("/api/dashboard")
def api_dashboard():
    if not DEMO and not db.fuels():
        return jsonify({"empty": True, "sync": sync_manager.status})
    return jsonify(dashboard_data())


@app.get("/api/day/<ds>")
def api_day(ds):
    try:
        datetime.strptime(ds, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "bad date"}), 400
    return jsonify(analysis.day_detail(db, ds))


@app.get("/api/sync")
def api_sync_status():
    if DEMO:
        return jsonify({"demo": True, "running": False})
    return jsonify(sync_manager.status)


@app.post("/api/sync")
def api_sync_start():
    if DEMO:
        return jsonify({"demo": True, "running": False})
    sync_manager.start()
    return jsonify(sync_manager.status)


def auto_sync():
    """Sync on start-up, then every few hours while the app is open."""
    while True:
        last = db.get("last_sync") or 0
        if time.time() - last > AUTO_SYNC_HOURS * 3600:
            sync_manager.start()
        time.sleep(600)


if __name__ == "__main__":
    if DEMO:
        print("No OCTOPUS_API_KEY / OCTOPUS_ACCOUNT in .env – showing DEMO data.", flush=True)
    else:
        threading.Thread(target=auto_sync, daemon=True).start()
    print(f"Open http://localhost:{PORT}", flush=True)
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
