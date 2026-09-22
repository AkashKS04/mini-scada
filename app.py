#!/usr/bin/env python3
"""
app.py — Flask Web Dashboard + REST API (with Historian Trends)

Endpoints:
    GET  /                            → dashboard UI
    GET  /api/devices                 → list of devices
    GET  /api/latest                  → latest reading per device per parameter
    GET  /api/history                 → last 50 readings (all devices)
    GET  /api/alarms                  → ACTIVE + ACKNOWLEDGED alarms
    GET  /api/alarms/history          → CLEARED alarms (alarm history)
    POST /api/acknowledge/<id>        → acknowledge an alarm
    GET  /api/trends                  → time-series data for historian charts
                                        ?device_id=1&hours=1
    GET  /api/register_map            → register map reference
"""

from flask import Flask, render_template, jsonify, request
import sqlite3
from datetime import datetime, timedelta

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
    rows = query_db(
        "SELECT * FROM readings ORDER BY id DESC LIMIT 50"
    )
    return jsonify([dict(r) for r in rows])


@app.route("/api/alarms")
def api_alarms():
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
    rows = query_db(
        """SELECT * FROM alarms
           WHERE status = 'CLEARED'
           ORDER BY id DESC
           LIMIT 30"""
    )
    return jsonify([dict(r) for r in rows])


@app.route("/api/acknowledge/<int:alarm_id>", methods=["POST"])
def acknowledge(alarm_id):
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


@app.route("/api/trends")
def api_trends():
    """
    Returns time-series historian data for a given device over
    the last N hours. Used to power the Chart.js trend graphs.

    Query params:
        device_id : int  (1, 2, or 3)
        hours     : int  (1 or 24, default 1)

    Response shape:
    {
        "device_id": 1,
        "device_name": "Feeder A",
        "hours": 1,
        "labels": ["2026-06-15 10:00:00", ...],   <- timestamps (x-axis)
        "voltage":     [230.1, 229.8, ...],         <- values (y-axis)
        "current":     [14.5, 15.2, ...],
        "temperature": [72.3, 71.9, ...]
    }

    For 1 hour: returns every reading (sampled ~every 3s, max ~1200 points)
    For 24 hours: buckets into 1-minute averages to keep response small
    """
    device_id = request.args.get("device_id", 1, type=int)
    hours     = request.args.get("hours", 1, type=int)

    # Clamp hours to valid values
    if hours not in (1, 24):
        hours = 1

    device_name = DEVICES.get(device_id, "Unknown")
    since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    if hours == 1:
        # Return raw readings for the last hour
        rows = query_db(
            """SELECT timestamp, parameter, value
               FROM readings
               WHERE device_id=? AND timestamp >= ?
               ORDER BY timestamp ASC""",
            (device_id, since)
        )

        # Build per-parameter lists aligned by timestamp
        data = {}
        labels_set = {}

        for row in rows:
            ts    = row["timestamp"]
            param = row["parameter"]
            val   = row["value"]

            if param not in data:
                data[param]       = {}
            data[param][ts] = val

        # Get union of all timestamps
        all_ts = sorted(set(
            ts for param_data in data.values() for ts in param_data
        ))

        result = {
            "device_id":   device_id,
            "device_name": device_name,
            "hours":       hours,
            "labels":      all_ts,
            "voltage":     [data.get("voltage",     {}).get(ts) for ts in all_ts],
            "current":     [data.get("current",     {}).get(ts) for ts in all_ts],
            "temperature": [data.get("temperature", {}).get(ts) for ts in all_ts],
        }

    else:
        # 24 hours: bucket into 1-minute averages to reduce data points
        rows = query_db(
            """SELECT
                   strftime('%Y-%m-%d %H:%M', timestamp) AS minute,
                   parameter,
                   ROUND(AVG(value), 1) AS avg_value
               FROM readings
               WHERE device_id=? AND timestamp >= ?
               GROUP BY minute, parameter
               ORDER BY minute ASC""",
            (device_id, since)
        )

        data = {}
        for row in rows:
            minute = row["minute"]
            param  = row["parameter"]
            val    = row["avg_value"]

            if param not in data:
                data[param] = {}
            data[param][minute] = val

        all_ts = sorted(set(
            ts for param_data in data.values() for ts in param_data
        ))

        result = {
            "device_id":   device_id,
            "device_name": device_name,
            "hours":       hours,
            "labels":      all_ts,
            "voltage":     [data.get("voltage",     {}).get(ts) for ts in all_ts],
            "current":     [data.get("current",     {}).get(ts) for ts in all_ts],
            "temperature": [data.get("temperature", {}).get(ts) for ts in all_ts],
        }

    return jsonify(result)


@app.route("/api/register_map")
def register_map():
    return jsonify(REGISTER_MAP)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8080)
