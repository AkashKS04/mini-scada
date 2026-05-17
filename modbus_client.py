#!/usr/bin/env python3
"""
Modbus TCP Client — polls RTU, stores to SQLite, raises alarms
Run this in a separate terminal after starting modbus_server.py
"""

import time
import sqlite3
from datetime import datetime
from pymodbus.client import ModbusTcpClient

# --- Config ---
HOST        = "127.0.0.1"
PORT        = 5020
POLL_INTERVAL = 3   # seconds
DB_PATH     = "scada.db"

# --- Alarm thresholds ---
THRESHOLDS = {
    "voltage":     {"min": 210.0, "max": 245.0},
    "current":     {"min":  5.0,  "max":  25.0},
    "temperature": {"min": 0.0,   "max":  90.0},
}

# --- Database setup ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            voltage   REAL,
            current   REAL,
            temperature REAL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS alarms (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            parameter TEXT,
            value     REAL,
            message   TEXT,
            acknowledged INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def save_reading(ts, voltage, current, temperature):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO readings (timestamp, voltage, current, temperature) VALUES (?,?,?,?)",
        (ts, voltage, current, temperature)
    )
    conn.commit()
    conn.close()

def save_alarm(ts, parameter, value, message):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO alarms (timestamp, parameter, value, message) VALUES (?,?,?,?)",
        (ts, parameter, value, message)
    )
    conn.commit()
    conn.close()
    print(f"  ⚠️  ALARM: {message}")

def check_alarms(ts, voltage, current, temperature):
    readings = {
        "voltage": voltage,
        "current": current,
        "temperature": temperature
    }
    for param, val in readings.items():
        lo = THRESHOLDS[param]["min"]
        hi = THRESHOLDS[param]["max"]
        if val > hi:
            save_alarm(ts, param, val,
                f"{param.upper()} HIGH: {val} exceeds max {hi}")
        elif val < lo:
            save_alarm(ts, param, val,
                f"{param.upper()} LOW: {val} below min {lo}")

# --- Main polling loop ---
def main():
    init_db()
    client = ModbusTcpClient(HOST, port=PORT)

    print(f"[CLIENT] Connecting to Modbus server at {HOST}:{PORT}...")
    client.connect()
    print("[CLIENT] Connected. Starting poll loop...\n")

    try:
        while True:
            result = client.read_holding_registers(0, count=3)
            if not result.isError():
                voltage     = result.registers[0] / 10.0
                current     = result.registers[1] / 10.0
                temperature = result.registers[2] / 10.0
                ts          = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                print(f"[{ts}]  V={voltage}V  I={current}A  T={temperature}°C")
                save_reading(ts, voltage, current, temperature)
                check_alarms(ts, voltage, current, temperature)
            else:
                print("[ERROR] Failed to read registers")

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\n[CLIENT] Stopped.")
    finally:
        client.close()

if __name__ == "__main__":
    main()
