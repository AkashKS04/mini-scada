#!/usr/bin/env python3
"""
app.py — Flask Web Dashboard + REST API (Multi-Device)

Serves the SCADA HMI dashboard and exposes API endpoints
that return data grouped by device.

Endpoints:
    GET  /                        → dashboard UI
    GET  /api/devices             → list of devices
    GET  /api/latest              → latest reading per device, per parameter
    GET  /api/history             → last 50 readings (all devices)
    GET  /api/alarms              → last 20 alarms (all devices)
    POST /api/acknowledge/<id>    → acknowledge an alarm
    GET  /api/register_map        → register map reference
"""

from flask import Flask, render_template, jsonify, request
import sqlite3

app = Flask(__name__)
DB_PATH = "scada.db"

# ─────────────────────────────────────────────
# Devices — must match modbus_server.py / modbus_client.py
# ─────────────────────────────────────────────
DEVICES = {
    1: "Feeder A",
    2: "Transformer T1",
    3: "Substation S1",
}

PARAMETERS = ["voltage", "current", "temperature"]

# ─────────────────────────────────────────────
# Register Map — for API responses
# ─────────────────────────────────────────────
REGISTER_MAP = {
    "voltage":     "40001",
    "current":     "40002",
    "temperature": "40003",
}


def query_db(sql, args=(), one=False):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(sql, args)
    rows = cur.fetchall()
    conn.close()
    return (rows[0] if rows else None) if one else rows


# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/devices")
def api_devices():
    """Returns the list of known devices."""
    return jsonify([
        {"device_id": dev_id, "device_name": name}
        for dev_id, name in DEVICES.items()
    ])


@app.route("/api/latest")
def api_latest():
    """
    Returns the latest reading for each (device, parameter) pair.

    Response shape:
    {
        "1": {
            "device_id": 1,
            "device_name": "Feeder A",
            "voltage":     {"value": 230.1, "register": "40001", "timestamp": "..."},
            "current":     {"value": 14.5,  "register": "40002", "timestamp": "..."},
            "temperature": {"value": 72.3,  "register": "40003", "timestamp": "..."}
        },
        "2": { ... },
        "3": { ... }
    }
    """
    # Fetch enough recent rows to cover the latest value
    # for every (device, parameter) combination
    limit = len(DEVICES) * len(PARAMETERS) * 5
    rows = query_db(
        """SELECT device_id, device_name, parameter, value, register, timestamp
           FROM readings ORDER BY id DESC LIMIT ?""",
        (limit,)
    )

    result = {}
    seen = set()
    total_needed = len(DEVICES) * len(PARAMETERS)

    for row in rows:
        key = (row["device_id"], row["parameter"])
        if key in seen:
            continue
        seen.add(key)

        dev_key = str(row["device_id"])
        if dev_key not in result:
            result[dev_key] = {
                "device_id":   row["device_id"],
                "device_name": row["device_name"],
            }

        result[dev_key][row["parameter"]] = {
            "value":     row["value"],
            "register":  row["register"],
            "timestamp": row["timestamp"],
        }

        if len(seen) == total_needed:
            break

    return jsonify(result)


@app.route("/api/history")
def api_history():
    """Returns last 50 readings across all devices."""
    rows = query_db(
        "SELECT * FROM readings ORDER BY id DESC LIMIT 50"
    )
    return jsonify([dict(r) for r in rows])


@app.route("/api/alarms")
def api_alarms():
    """Returns last 20 alarms across all devices."""
    rows = query_db(
        "SELECT * FROM alarms ORDER BY id DESC LIMIT 20"
    )
    return jsonify([dict(r) for r in rows])


@app.route("/api/acknowledge/<int:alarm_id>", methods=["POST"])
def acknowledge(alarm_id):
    """Acknowledge an alarm by ID."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE alarms SET acknowledged=1 WHERE id=?", (alarm_id,)
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "alarm_id": alarm_id})


@app.route("/api/register_map")
def register_map():
    """Returns the register map — useful for documentation."""
    return jsonify(REGISTER_MAP)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8080)
