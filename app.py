#!/usr/bin/env python3
"""
app.py — Flask Web Dashboard + REST API (Smart Alarm Engine)

Endpoints:
    GET  /                            → dashboard UI
    GET  /api/devices                 → list of devices
    GET  /api/latest                  → latest reading per device per parameter
    GET  /api/history                 → last 50 readings (all devices)
    GET  /api/alarms                  → ACTIVE + ACKNOWLEDGED alarms
    GET  /api/alarms/history          → CLEARED alarms (alarm history)
    POST /api/acknowledge/<id>        → acknowledge an alarm (ACTIVE → ACKNOWLEDGED)
    GET  /api/register_map            → register map reference
"""

from flask import Flask, render_template, jsonify
import sqlite3
from datetime import datetime

app = Flask(__name__)
DB_PATH = "scada.db"

# ─────────────────────────────────────────────
# Devices + Register Map
# ─────────────────────────────────────────────
DEVICES = {
    1: "Feeder A",
    2: "Transformer T1",
    3: "Substation S1",
}

PARAMETERS = ["voltage", "current", "temperature"]

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
    return jsonify([
        {"device_id": dev_id, "device_name": name}
        for dev_id, name in DEVICES.items()
    ])


@app.route("/api/latest")
def api_latest():
    """Latest reading per device per parameter."""
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
    """Last 50 readings across all devices."""
    rows = query_db(
        "SELECT * FROM readings ORDER BY id DESC LIMIT 50"
    )
    return jsonify([dict(r) for r in rows])


@app.route("/api/alarms")
def api_alarms():
    """
    Returns ACTIVE and ACKNOWLEDGED alarms only.
    These are the alarms needing operator attention.
    Ordered by priority then timestamp.
    """
    rows = query_db(
        """SELECT * FROM alarms
           WHERE status IN ('ACTIVE', 'ACKNOWLEDGED')
           ORDER BY
               CASE priority
                   WHEN 'HIGH'   THEN 1
                   WHEN 'MEDIUM' THEN 2
                   WHEN 'LOW'    THEN 3
               END,
               id DESC
           LIMIT 20"""
    )
    return jsonify([dict(r) for r in rows])


@app.route("/api/alarms/history")
def api_alarms_history():
    """
    Returns CLEARED alarms — the alarm history log.
    Shows resolved events for post-fault analysis (like SOE in real OMS).
    """
    rows = query_db(
        """SELECT * FROM alarms
           WHERE status = 'CLEARED'
           ORDER BY id DESC
           LIMIT 30"""
    )
    return jsonify([dict(r) for r in rows])


@app.route("/api/acknowledge/<int:alarm_id>", methods=["POST"])
def acknowledge(alarm_id):
    """
    Acknowledges an ACTIVE alarm — moves it to ACKNOWLEDGED.
    Records the timestamp of acknowledgement.
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """UPDATE alarms
           SET status='ACKNOWLEDGED', ack_timestamp=?
           WHERE id=? AND status='ACTIVE'""",
        (ts, alarm_id)
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "alarm_id": alarm_id, "ack_timestamp": ts})


@app.route("/api/register_map")
def register_map():
    return jsonify(REGISTER_MAP)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8080)
