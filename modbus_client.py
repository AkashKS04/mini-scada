#!/usr/bin/env python3
"""
modbus_client.py — Multi-Device Data Collector + Alarm Engine

Polls all 3 simulated devices (by Slave ID) using the same register map:
    40001 (address 1) → Voltage
    40002 (address 2) → Current
    40003 (address 3) → Temperature

Devices:
    Slave ID 1 → Feeder A
    Slave ID 2 → Transformer T1
    Slave ID 3 → Substation S1

Stores readings to SQLite (tagged with device_id/device_name) and
raises alarms when thresholds are breached, per device.
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
POLL_INTERVAL = 3
DB_PATH       = "scada.db"

# ─────────────────────────────────────────────
# Register Map — must match modbus_server.py
# ─────────────────────────────────────────────
REG_VOLTAGE     = 1     # 40001
REG_CURRENT     = 2     # 40002
REG_TEMPERATURE = 3     # 40003

# ─────────────────────────────────────────────
# Devices — must match modbus_server.py
# ─────────────────────────────────────────────
DEVICES = {
    1: "Feeder A",
    2: "Transformer T1",
    3: "Substation S1",
}

# ─────────────────────────────────────────────
# Alarm thresholds — applied to every device
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
            device_id   INTEGER,
            device_name TEXT,
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
            device_id    INTEGER,
            device_name  TEXT,
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


def save_reading(ts, device_id, device_name, parameter, address, value):
    register = f"4{str(address).zfill(4)}"
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO readings
           (timestamp, device_id, device_name, register, address, parameter, value)
           VALUES (?,?,?,?,?,?,?)""",
        (ts, device_id, device_name, register, address, parameter, value)
    )
    conn.commit()
    conn.close()


def save_alarm(ts, device_id, device_name, parameter, address, value, message):
    register = f"4{str(address).zfill(4)}"
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO alarms
           (timestamp, device_id, device_name, parameter, register, value, message)
           VALUES (?,?,?,?,?,?,?)""",
        (ts, device_id, device_name, parameter, register, value, message)
    )
    conn.commit()
    conn.close()
    print(f"  [ALARM][{device_name}] {message}")


def check_alarms(ts, device_id, device_name, parameter, address, value):
    """Check if a reading breaches its threshold and raise alarm if so."""
    lo = THRESHOLDS[parameter]["min"]
    hi = THRESHOLDS[parameter]["max"]
    register = f"4{str(address).zfill(4)}"

    if value > hi:
        save_alarm(ts, device_id, device_name, parameter, address, value,
            f"{register} {parameter.upper()} HIGH: {value} exceeds max {hi}")
    elif value < lo:
        save_alarm(ts, device_id, device_name, parameter, address, value,
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
    print("[CLIENT] Devices to poll:")
    for slave_id, name in DEVICES.items():
        print(f"[CLIENT]   Slave {slave_id} -> {name}")
    print()

    registers = [
        (REG_VOLTAGE,     "voltage"),
        (REG_CURRENT,     "current"),
        (REG_TEMPERATURE, "temperature"),
    ]

    try:
        while True:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Poll each device in turn, using its Slave ID
            for slave_id, device_name in DEVICES.items():
                results = {}
                all_ok = True

                for address, parameter in registers:
                    result = client.read_holding_registers(
                        address, count=1, slave=slave_id
                    )
                    if not result.isError():
                        value = result.registers[0] / 10.0
                        results[parameter] = value
                        save_reading(ts, slave_id, device_name, parameter, address, value)
                        check_alarms(ts, slave_id, device_name, parameter, address, value)
                    else:
                        print(
                            f"  [ERROR] Slave {slave_id} ({device_name}): "
                            f"failed to read register 4{str(address).zfill(4)}"
                        )
                        all_ok = False

                if all_ok:
                    print(
                        f"[{ts}] [Slave {slave_id}: {device_name}] "
                        f"40001=V:{results['voltage']}V  "
                        f"40002=I:{results['current']}A  "
                        f"40003=T:{results['temperature']}C"
                    )

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\n[CLIENT] Stopped by user.")
    finally:
        client.close()
        print("[CLIENT] Connection closed.")


if __name__ == "__main__":
    main()
