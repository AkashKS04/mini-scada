#!/usr/bin/env python3
"""
modbus_client.py — Data Collector + Alarm Engine

Polls the Modbus TCP server using the correct register addresses:
    40001 (address 1) → Voltage
    40002 (address 2) → Current
    40003 (address 3) → Temperature

Stores readings to SQLite and raises alarms when thresholds are breached.
"""

import time
import sqlite3
from datetime import datetime
from pymodbus.client import ModbusTcpClient

# ─────────────────────────────────────────────
# Connection config
# ─────────────────────────────────────────────
HOST          = "127.0.0.1"
PORT          = 5020
POLL_INTERVAL = 3        # seconds between each poll
DB_PATH       = "scada.db"

# ─────────────────────────────────────────────
# Register Map — must match modbus_server.py
# ─────────────────────────────────────────────
REG_VOLTAGE     = 1     # 40001
REG_CURRENT     = 2     # 40002
REG_TEMPERATURE = 3     # 40003

# ─────────────────────────────────────────────
# Alarm thresholds
# ─────────────────────────────────────────────
THRESHOLDS = {
    "voltage":     {"min": 210.0, "max": 245.0},
    "current":     {"min":   5.0, "max":  25.0},
    "temperature": {"min":   0.0, "max":  90.0},
}


# ─────────────────────────────────────────────
# Database setup
# ─────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT,
            register    TEXT,
            address     INTEGER,
            parameter   TEXT,
            value       REAL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS alarms (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp    TEXT,
            parameter    TEXT,
            register     TEXT,
            value        REAL,
            message      TEXT,
            acknowledged INTEGER DEFAULT 0
        )
    """)

    conn.commit()
    conn.close()
    print("[DB] Database initialised: scada.db")


def save_reading(ts, parameter, address, value):
    """Save a single register reading to the DB."""
    register = f"4{str(address).zfill(4)}"   # e.g. address 1 → "40001"
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO readings (timestamp, register, address, parameter, value) VALUES (?,?,?,?,?)",
        (ts, register, address, parameter, value)
    )
    conn.commit()
    conn.close()


def save_alarm(ts, parameter, address, value, message):
    """Save an alarm event to the DB."""
    register = f"4{str(address).zfill(4)}"
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO alarms (timestamp, parameter, register, value, message) VALUES (?,?,?,?,?)",
        (ts, parameter, register, value, message)
    )
    conn.commit()
    conn.close()
    print(f"  [ALARM] {message}")


def check_alarms(ts, parameter, address, value):
    """Check if a reading breaches its threshold and raise alarm if so."""
    lo = THRESHOLDS[parameter]["min"]
    hi = THRESHOLDS[parameter]["max"]
    register = f"4{str(address).zfill(4)}"

    if value > hi:
        save_alarm(ts, parameter, address, value,
            f"{register} {parameter.upper()} HIGH: {value} exceeds max {hi}")
    elif value < lo:
        save_alarm(ts, parameter, address, value,
            f"{register} {parameter.upper()} LOW: {value} below min {lo}")


# ─────────────────────────────────────────────
# Main polling loop
# ─────────────────────────────────────────────
def main():
    init_db()

    client = ModbusTcpClient(HOST, port=PORT)
    print(f"[CLIENT] Connecting to Modbus server at {HOST}:{PORT} ...")
    client.connect()
    print("[CLIENT] Connected.")
    print("[CLIENT] Polling registers:")
    print("[CLIENT]   40001 → Voltage")
    print("[CLIENT]   40002 → Current")
    print("[CLIENT]   40003 → Temperature")
    print()

    try:
        while True:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Read each register individually by its named address
            readings = [
                (REG_VOLTAGE,     "voltage"),
                (REG_CURRENT,     "current"),
                (REG_TEMPERATURE, "temperature"),
            ]

            results = {}
            all_ok = True

            for address, parameter in readings:
                result = client.read_holding_registers(address, count=1)
                if not result.isError():
                    value = result.registers[0] / 10.0
                    results[parameter] = (address, value)
                    save_reading(ts, parameter, address, value)
                    check_alarms(ts, parameter, address, value)
                else:
                    print(f"  [ERROR] Failed to read register 4{str(address).zfill(4)}")
                    all_ok = False

            if all_ok:
                v = results["voltage"][1]
                i = results["current"][1]
                t = results["temperature"][1]
                print(
                    f"[{ts}] "
                    f"40001=Voltage:{v}V  "
                    f"40002=Current:{i}A  "
                    f"40003=Temp:{t}C"
                )

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\n[CLIENT] Stopped by user.")
    finally:
        client.close()
        print("[CLIENT] Connection closed.")


if __name__ == "__main__":
    main()
