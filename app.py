#!/usr/bin/env python3
"""
app.py — Flask Web Dashboard + REST API

Serves the SCADA HMI dashboard and exposes API endpoints
that include Modbus register addresses in every response.

Endpoints:
    GET  /                        → dashboard UI
    GET  /api/latest              → latest reading with register info
    GET  /api/history             → last 50 readings
    GET  /api/alarms              → last 20 alarms
    POST /api/acknowledge/<id>    → acknowledge an alarm
"""

from flask import Flask, render_template, jsonify, request
import sqlite3

app = Flask(__name__)
DB_PATH = "scada.db"

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


@app.route("/api/latest")
def api_latest():
    """
    Returns the latest reading for each parameter,
    with its Modbus register address included.
    """
    rows = query_db(
        "SELECT parameter, value, register, timestamp FROM readings ORDER BY id DESC LIMIT 10"
    )

    result = {}
    seen = set()

    for row in rows:
        param = row["parameter"]
        if param not in seen:
            result[param] = {
                "value":     row["value"],
                "register":  row["register"],
                "timestamp": row["timestamp"],
            }
            seen.add(param)

        if len(seen) == 3:
            break

    return jsonify(result)


@app.route("/api/history")
def api_history():
    """Returns last 50 readings with register addresses."""
    rows = query_db(
        "SELECT * FROM readings ORDER BY id DESC LIMIT 50"
    )
    return jsonify([dict(r) for r in rows])


@app.route("/api/alarms")
def api_alarms():
    """Returns last 20 alarms with register addresses."""
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
