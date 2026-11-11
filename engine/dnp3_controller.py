#!/usr/bin/env python3
"""
engine/dnp3_controller.py - SCADA control layer (DNP3 master)

Takes breaker commands queued by the API (control_commands table) and
executes each one as a DNP3 SELECT then OPERATE to the substation outstation.

Safety rules
    - A command that waited longer than COMMAND_TTL seconds is EXPIRED, never
      executed: nobody wants a breaker operating minutes after the click.
    - A command is claimed (PENDING -> EXECUTING) before anything is sent, so
      it can never run twice.
    - If the controller dies mid-command, that command is marked FAILED on
      the next start instead of being retried.

Status flow:  PENDING -> EXECUTING -> EXECUTED | FAILED      (or EXPIRED)
"""

import os
import sys
import json
import time
import sqlite3
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "lib"))
import dnp3lite  # noqa: E402

CONFIG_PATH = os.path.join(BASE_DIR, "config", "substations.json")
DB_PATH     = os.path.join(BASE_DIR, "scada.db")

HOST         = "127.0.0.1"
PORT         = 20000
MASTER_ADDR  = 1
COMMAND_TTL  = 30      # seconds a queued command stays valid
POLL_SECONDS = 1
FMT          = "%Y-%m-%d %H:%M:%S"

with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

BY_SLAVE, _ = dnp3lite.point_maps(CONFIG)


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def now_ts():
    return datetime.now().strftime(FMT)


def now_ms():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def log_soe(cmd, event_type, message):
    conn = db()
    conn.execute("""
        INSERT INTO soe_log
        (timestamp_ms, substation_id, device_id, device_name, event_type,
         parameter, old_value, new_value, message, operator)
        VALUES (?, ?, ?, ?, ?, 'breaker_state', '', ?, ?, ?)
    """, (now_ms(), cmd["substation_id"], cmd["device_id"], cmd["device_name"],
          event_type, cmd["command"], message, cmd["operator"]))
    conn.commit()
    conn.close()


def claim(cmd_id):
    """PENDING -> EXECUTING. Returns False if someone else already took it."""
    conn = db()
    cur = conn.execute("UPDATE control_commands SET status='EXECUTING' "
                       "WHERE id=? AND status='PENDING'", (cmd_id,))
    conn.commit()
    conn.close()
    return cur.rowcount == 1


def finish(cmd, status, result):
    conn = db()
    conn.execute("UPDATE control_commands SET status=?, result=?, executed_timestamp=? WHERE id=?",
                 (status, result, now_ts(), cmd["id"]))
    conn.commit()
    conn.close()
    event = {"EXECUTED": "CONTROL_EXECUTED", "FAILED": "CONTROL_FAILED",
             "EXPIRED": "CONTROL_EXPIRED"}[status]
    log_soe(cmd, event, f"{cmd['command']} {cmd['device_name']}: {result}")
    print(f"[CONTROL] #{cmd['id']} {cmd['command']} {cmd['device_name']} "
          f"by {cmd['operator']} -> {status} ({result})")


def send(master, addr, index, command):
    """One retry on a broken connection, then give up with a clear message."""
    last = "unknown error"
    for _ in range(2):
        try:
            if not master.connected:
                master.connect()
            return master.operate(addr, index, command)
        except (OSError, dnp3lite.ProtocolError) as e:
            master.close()
            last = f"communication error: {e}"
    return False, last


def handle(master, cmd):
    age = (datetime.now() - datetime.strptime(cmd["timestamp"], FMT)).total_seconds()
    if age > COMMAND_TTL:
        finish(cmd, "EXPIRED", f"not executed within {COMMAND_TTL}s")
        return
    if not claim(cmd["id"]):
        return
    target = BY_SLAVE.get(cmd["slave_id"])
    if not target:
        finish(cmd, "FAILED", "no DNP3 control point for this device")
        return
    ok, msg = send(master, target[0], target[1], cmd["command"])
    finish(cmd, "EXECUTED" if ok else "FAILED", msg)


def main():
    master = dnp3lite.Master(HOST, PORT, MASTER_ADDR)
    print(f"[CONTROL] DNP3 control layer started (master {MASTER_ADDR} -> {HOST}:{PORT})")

    recovered = False
    while True:
        try:
            if not recovered:
                conn = db()
                conn.execute("UPDATE control_commands SET status='FAILED', "
                             "result='controller restarted during execution' "
                             "WHERE status='EXECUTING'")
                conn.commit()
                conn.close()
                recovered = True

            conn = db()
            pending = [dict(r) for r in conn.execute(
                "SELECT * FROM control_commands WHERE status='PENDING' ORDER BY id")]
            conn.close()
            for cmd in pending:
                handle(master, cmd)

        except sqlite3.OperationalError as e:
            print(f"[CONTROL] waiting for database: {e}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
