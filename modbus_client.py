#!/usr/bin/env python3
"""
modbus_client.py — Multi-Device Data Collector + Smart Alarm Engine

Polls all 3 simulated devices (by Slave ID) using the same register map:
    40001 (address 1) → Voltage
    40002 (address 2) → Current
    40003 (address 3) → Temperature

Devices:
    Slave ID 1 → Feeder A
    Slave ID 2 → Transformer T1
    Slave ID 3 → Substation S1

Alarm Lifecycle (real OMS-style state machine):

    Value breaches threshold
            |
        [ACTIVE]   <- alarm row created ONCE
            |
    Operator acknowledges (via dashboard)
            |
    [ACKNOWLEDGED]   <- operator aware, condition may still be bad
            |
    Value returns to normal range
            |
        [CLEARED]   <- condition resolved automatically

One alarm = one event. Repeated breaches while ACTIVE/ACKNOWLEDGED
do NOT create duplicate rows.

Priority levels (assigned per parameter):
    Voltage     -> HIGH
    Current     -> MEDIUM
    Temperature -> LOW
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
# Alarm priority — assigned per parameter
# ─────────────────────────────────────────────
PRIORITY = {
    "voltage":     "HIGH",
    "current":     "MEDIUM",
    "temperature": "LOW",
}

# ─────────────────────────────────────────────
# In-memory tracking of currently open alarms
# key: (device_id, parameter) -> alarm_id
# Loaded from DB on startup so restarts don't lose state
# ─────────────────────────────────────────────
active_alarms = {}


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
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp         TEXT,    -- when alarm was raised (became ACTIVE)
            device_id         INTEGER,
            device_name       TEXT,
            parameter         TEXT,
            register          TEXT,
            value             REAL,
            message           TEXT,
            priority          TEXT,    -- HIGH / MEDIUM / LOW
            status            TEXT DEFAULT 'ACTIVE',  -- ACTIVE / ACKNOWLEDGED / CLEARED
            ack_timestamp     TEXT,
            cleared_timestamp TEXT
        )
    """)

    conn.commit()
    conn.close()
    print("[DB] Database initialised: scada.db")


def load_active_alarms():
    """
    On startup, load any alarms that are still ACTIVE or ACKNOWLEDGED
    into memory, so we don't raise duplicate alarms after a restart.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, device_id, parameter FROM alarms WHERE status IN ('ACTIVE','ACKNOWLEDGED')"
    ).fetchall()
    conn.close()

    for row in rows:
        active_alarms[(row["device_id"], row["parameter"])] = row["id"]

    if active_alarms:
        print(f"[ALARM] Restored {len(active_alarms)} open alarm(s) from previous run")


# ─────────────────────────────────────────────
# Readings
# ─────────────────────────────────────────────
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


# ─────────────────────────────────────────────
# Alarm lifecycle: raise / clear
# ─────────────────────────────────────────────
def raise_alarm(ts, device_id, device_name, parameter, address, value, message):
    """Create a new ACTIVE alarm and return its ID."""
    register = f"4{str(address).zfill(4)}"
    priority = PRIORITY[parameter]

    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        """INSERT INTO alarms
           (timestamp, device_id, device_name, parameter, register, value, message, priority, status)
           VALUES (?,?,?,?,?,?,?,?, 'ACTIVE')""",
        (ts, device_id, device_name, parameter, register, value, message, priority)
    )
    alarm_id = cur.lastrowid
    conn.commit()
    conn.close()

    print(f"  [ALARM RAISED][{device_name}] {message} (Priority: {priority})")
    return alarm_id


def clear_alarm(alarm_id, ts, device_name, parameter):
    """Mark an alarm as CLEARED — condition has returned to normal."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE alarms SET status='CLEARED', cleared_timestamp=? WHERE id=?",
        (ts, alarm_id)
    )
    conn.commit()
    conn.close()

    print(f"  [ALARM CLEARED][{device_name}] {parameter.upper()} back to normal")


def check_alarms(ts, device_id, device_name, parameter, address, value):
    """
    Alarm state machine for one (device, parameter) pair.

    - Breach + no open alarm  -> raise ONE new ACTIVE alarm
    - Breach + alarm already open (ACTIVE/ACKNOWLEDGED) -> do nothing
    - Normal + alarm open     -> mark CLEARED
    - Normal + no alarm open  -> do nothing
    """
    lo = THRESHOLDS[parameter]["min"]
    hi = THRESHOLDS[parameter]["max"]
    register = f"4{str(address).zfill(4)}"
    key = (device_id, parameter)

    in_breach = value > hi or value < lo

    if in_breach:
        if key not in active_alarms:
            if value > hi:
                message = f"{register} {parameter.upper()} HIGH: {value} exceeds max {hi}"
            else:
                message = f"{register} {parameter.upper()} LOW: {value} below min {lo}"

            alarm_id = raise_alarm(ts, device_id, device_name, parameter, address, value, message)
            active_alarms[key] = alarm_id
        # else: alarm already open, don't create a duplicate
    else:
        if key in active_alarms:
            clear_alarm(active_alarms[key], ts, device_name, parameter)
            del active_alarms[key]


# ─────────────────────────────────────────────
# Main polling loop
# ─────────────────────────────────────────────
def main():
    init_db()
    load_active_alarms()

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
