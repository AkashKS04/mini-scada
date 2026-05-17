#!/usr/bin/env python3
"""
Flask SCADA Dashboard — live readings, alarms, history
"""

from flask import Flask, render_template, jsonify, request
import sqlite3

app = Flask(__name__)
DB_PATH = "scada.db"

def query_db(sql, args=(), one=False):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(sql, args)
    rows = cur.fetchall()
    conn.close()
    return (rows[0] if rows else None) if one else rows

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/latest")
def api_latest():
    row = query_db(
        "SELECT * FROM readings ORDER BY id DESC LIMIT 1", one=True
    )
    if row:
        return jsonify(dict(row))
    return jsonify({})

@app.route("/api/history")
def api_history():
    rows = query_db(
        "SELECT * FROM readings ORDER BY id DESC LIMIT 50"
    )
    return jsonify([dict(r) for r in rows])

@app.route("/api/alarms")
def api_alarms():
    rows = query_db(
        "SELECT * FROM alarms ORDER BY id DESC LIMIT 20"
    )
    return jsonify([dict(r) for r in rows])

@app.route("/api/acknowledge/<int:alarm_id>", methods=["POST"])
def acknowledge(alarm_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE alarms SET acknowledged=1 WHERE id=?", (alarm_id,)
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8080)
