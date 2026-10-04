#!/usr/bin/env python3
"""
modbus_client.py — Multi-Device Data Collector + Smart Alarm Engine + Failure Detection

Polls all 3 devices and detects the following failure conditions:

    1. DEVICE OFFLINE
       - 3 consecutive failed polls → raises HIGH priority alarm
       - Device responds again → alarm auto-CLEARS

    2. DATA FREEZE
       - Same value for 5 consecutive polls on same register → raises MEDIUM alarm
       - Value changes again → alarm auto-CLEARS

    3. COMMUNICATION TIMEOUT
       - Each failed poll is logged
       - After 3 consecutive failures → escalates to OFFLINE alarm

Alarm lifecycle (same as Phase 2):
    ACTIVE → ACKNOWLEDGED → CLEARED
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
# Register Map
# ─────────────────────────────────────────────
REG_VOLTAGE     = 1     # 40001
REG_CURRENT     = 2     # 40002
REG_TEMPERATURE = 3     # 40003

# ─────────────────────────────────────────────
# Devices
# ─────────────────────────────────────────────
DEVICES = {
    1: "Feeder A",
    2: "Transformer T1",
    3: "Substation S1",
}

# ─────────────────────────────────────────────
# Threshold alarms
# ─────────────────────────────────────────────
THRESHOLDS = {
    "voltage":     {"min": 210.0, "max": 245.0},
    "current":     {"min":   5.0, "max":  25.0},
    "temperature": {"min":   0.0, "max":  90.0},
}

PRIORITY = {
    "voltage":     "HIGH",
    "current":     "MEDIUM",
    "temperature": "LOW",
}

# ─────────────────────────────────────────────
# Failure detection config
# ─────────────────────────────────────────────
OFFLINE_THRESHOLD = 3   # consecutive failed polls before OFFLINE alarm
FREEZE_THRESHOLD  = 5   # consecutive same-value polls before FREEZE alarm

# ─────────────────────────────────────────────
# In-memory state tracking
# ─────────────────────────────────────────────
active_alarms        = {}   # (device_id, parameter) -> alarm_id
consecutive_failures = {dev_id: 0 for dev_id in DEVICES}
offline_alarms       = {}   # device_id -> alarm_id
freeze_tracker       = {}   # (device_id, address) -> {last_value, count}
freeze_alarms        = {}   # (device_id, address) -> alarm_id
device_status        = {dev_id: "online" for dev_id in DEVICES}


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
            timestamp         TEXT,
            device_id         INTEGER,
            device_name       TEXT,
            parameter         TEXT,
            register          TEXT,
            value             REAL,
            message           TEXT,
            priority          TEXT,
            status            TEXT DEFAULT 'ACTIVE',
            alarm_type        TEXT DEFAULT 'THRESHOLD',
            ack_timestamp     TEXT,
            cleared_timestamp TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS device_status (
            device_id   INTEGER PRIMARY KEY,
            device_name TEXT,
            status      TEXT,
            last_seen   TEXT,
            updated_at  TEXT
        )
    """)

    for dev_id, name in DEVICES.items():
        c.execute("""
            INSERT OR IGNORE INTO device_status
            (device_id, device_name, status, last_seen, updated_at)
            VALUES (?, ?, 'online', NULL, ?)
        """, (dev_id, name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

    conn.commit()
    conn.close()
    print("[DB] Database initialised: scada.db")


def load_active_alarms():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, device_id, parameter, alarm_type FROM alarms WHERE status IN ('ACTIVE','ACKNOWLEDGED')"
    ).fetchall()
    conn.close()

    for row in rows:
        if row["alarm_type"] == "OFFLINE":
            offline_alarms[row["device_id"]] = row["id"]
        elif row["alarm_type"] != "FREEZE":
            active_alarms[(row["device_id"], row["parameter"])] = row["id"]

    total = len(active_alarms) + len(offline_alarms)
    if total:
        print(f"[ALARM] Restored {total} open alarm(s) from DB")


# ─────────────────────────────────────────────
# DB helpers
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


def update_device_status_db(device_id, status, last_seen=None):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """UPDATE device_status
           SET status=?, last_seen=?, updated_at=?
           WHERE device_id=?""",
        (status, last_seen or ts, ts, device_id)
    )
    conn.commit()
    conn.close()
    device_status[device_id] = status


def raise_alarm(ts, device_id, device_name, parameter, address,
                value, message, priority, alarm_type="THRESHOLD"):
    register = f"4{str(address).zfill(4)}" if address else "—"
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        """INSERT INTO alarms
           (timestamp, device_id, device_name, parameter, register,
            value, message, priority, status, alarm_type)
           VALUES (?,?,?,?,?,?,?,?,'ACTIVE',?)""",
        (ts, device_id, device_name, parameter, register,
         value, message, priority, alarm_type)
    )
    alarm_id = cur.lastrowid
    conn.commit()
    conn.close()
    print(f"  [ALARM RAISED][{device_name}] [{alarm_type}] {message}")
    return alarm_id


def clear_alarm(alarm_id, ts, device_name, reason):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE alarms SET status='CLEARED', cleared_timestamp=? WHERE id=?",
        (ts, alarm_id)
    )
    conn.commit()
    conn.close()
    print(f"  [ALARM CLEARED][{device_name}] {reason}")


# ─────────────────────────────────────────────
# Threshold alarm state machine
# ─────────────────────────────────────────────
def check_threshold_alarm(ts, device_id, device_name, parameter, address, value):
    lo  = THRESHOLDS[parameter]["min"]
    hi  = THRESHOLDS[parameter]["max"]
    key = (device_id, parameter)
    register  = f"4{str(address).zfill(4)}"
    in_breach = value > hi or value < lo

    if in_breach:
        if key not in active_alarms:
            msg = (f"{register} {parameter.upper()} HIGH: {value} exceeds max {hi}"
                   if value > hi else
                   f"{register} {parameter.upper()} LOW: {value} below min {lo}")
            alarm_id = raise_alarm(ts, device_id, device_name, parameter,
                                   address, value, msg, PRIORITY[parameter])
            active_alarms[key] = alarm_id
    else:
        if key in active_alarms:
            clear_alarm(active_alarms[key], ts, device_name,
                        f"{parameter.upper()} back to normal")
            del active_alarms[key]


# ─────────────────────────────────────────────
# Failure detection: OFFLINE
# ─────────────────────────────────────────────
def handle_poll_failure(ts, device_id, device_name):
    consecutive_failures[device_id] += 1
    count = consecutive_failures[device_id]

    print(f"  [TIMEOUT] Slave {device_id} ({device_name}) — failed poll #{count}")

    if count == OFFLINE_THRESHOLD and device_id not in offline_alarms:
        msg = (f"Slave {device_id} ({device_name}) OFFLINE — "
               f"{count} consecutive poll failures")
        alarm_id = raise_alarm(
            ts, device_id, device_name,
            "communication", None, 0.0, msg, "HIGH", "OFFLINE"
        )
        offline_alarms[device_id] = alarm_id
        update_device_status_db(device_id, "offline")


def handle_poll_success(ts, device_id, device_name):
    was_offline = consecutive_failures[device_id] >= OFFLINE_THRESHOLD
    consecutive_failures[device_id] = 0

    if was_offline and device_id in offline_alarms:
        clear_alarm(offline_alarms[device_id], ts, device_name,
                    f"Slave {device_id} ({device_name}) back ONLINE")
        del offline_alarms[device_id]
        update_device_status_db(device_id, "online", ts)


# ─────────────────────────────────────────────
# Failure detection: DATA FREEZE
# ─────────────────────────────────────────────
def check_freeze(ts, device_id, device_name, parameter, address, value):
    key = (device_id, address)

    if key not in freeze_tracker:
        freeze_tracker[key] = {"last_value": value, "count": 1}
        return

    tracker = freeze_tracker[key]

    if value == tracker["last_value"]:
        tracker["count"] += 1
    else:
        tracker["last_value"] = value
        tracker["count"] = 1

        if key in freeze_alarms:
            clear_alarm(freeze_alarms[key], ts, device_name,
                        f"{parameter.upper()} register unfrozen")
            del freeze_alarms[key]
            remaining = [k for k in freeze_alarms if k[0] == device_id]
            if not remaining and device_id not in offline_alarms:
                update_device_status_db(device_id, "online", ts)
        return

    if tracker["count"] >= FREEZE_THRESHOLD and key not in freeze_alarms:
        register = f"4{str(address).zfill(4)}"
        msg = (f"{register} {parameter.upper()} DATA FREEZE — "
               f"value stuck at {value} for {tracker['count']} polls")
        alarm_id = raise_alarm(
            ts, device_id, device_name,
            parameter, address, value, msg, "MEDIUM", "FREEZE"
        )
        freeze_alarms[key] = alarm_id
        update_device_status_db(device_id, "frozen")


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
    print(f"[CLIENT] Failure detection: ENABLED")
    print(f"[CLIENT]   Offline threshold : {OFFLINE_THRESHOLD} consecutive failures")
    print(f"[CLIENT]   Freeze threshold  : {FREEZE_THRESHOLD} same-value polls")
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
                all_ok  = True

                for address, parameter in registers:
                    result = client.read_holding_registers(
                        address, count=1, slave=slave_id
                    )

                    if not result.isError():
                        value = result.registers[0] / 10.0
                        results[parameter] = (address, value)
                        save_reading(ts, slave_id, device_name, parameter, address, value)
                        check_threshold_alarm(ts, slave_id, device_name, parameter, address, value)
                        check_freeze(ts, slave_id, device_name, parameter, address, value)
                    else:
                        all_ok = False

                if all_ok:
                    handle_poll_success(ts, slave_id, device_name)
                    v = results["voltage"][1]
                    i = results["current"][1]
                    t = results["temperature"][1]
                    status_tag = " [FROZEN]" if device_status.get(slave_id) == "frozen" else ""
                    print(
                        f"[{ts}] [Slave {slave_id}: {device_name}]{status_tag} "
                        f"40001=V:{v}V  40002=I:{i}A  40003=T:{t}C"
                    )
                else:
                    handle_poll_failure(ts, slave_id, device_name)

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\n[CLIENT] Stopped by user.")
    finally:
        client.close()
        print("[CLIENT] Connection closed.")


if __name__ == "__main__":
    main()
