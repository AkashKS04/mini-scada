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

Switching plans (FLISR / restore normal, table switching_plans)
    Executed one step at a time. Each step is queued as an ordinary command
    (so it goes through the same DNP3 path and audit trail), and the next
    step only starts once Modbus telemetry confirms the breaker or switch
    really moved. Any failure or missing confirmation stops the plan.
    In automatic mode (settings flisr_auto = 1) a FLISR plan is created for
    every newly located fault on a feeder-map station, once per fault.
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
import topology  # noqa: E402
import flisr     # noqa: E402

CONFIG_PATH = os.path.join(BASE_DIR, "config", "substations.json")
DB_PATH     = os.path.join(BASE_DIR, "scada.db")

HOST         = "127.0.0.1"
PORT         = 20000
MASTER_ADDR  = 1
COMMAND_TTL  = 30      # seconds a queued command stays valid
STEP_CONFIRM = 20      # seconds for telemetry to confirm a plan step
AUTO_EVERY   = 3       # seconds between automatic FLISR checks
POLL_SECONDS = 1
FMT          = "%Y-%m-%d %H:%M:%S"

with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

BY_SLAVE, _ = dnp3lite.point_maps(CONFIG)

# Switch points by switch id (device_id of a switch command)
TOPOLOGIES = topology.build(CONFIG)
BY_DEVICE  = {sw['id']: t.dnp3_point(sw['id']) for t in TOPOLOGIES.values() for sw in t.switches}

# Breaker info by slave id: (device id, name, substation id)
DEVICE_INFO = {}
for _ss in CONFIG['substations']:
    for _d in [_ss['transformer']] + _ss['feeders']:
        DEVICE_INFO[_d['slave_id']] = (_d['id'], _d['name'], _ss['id'])


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
    target = BY_DEVICE.get(cmd["device_id"]) or BY_SLAVE.get(cmd["slave_id"])
    if not target:
        finish(cmd, "FAILED", "no DNP3 control point for this device")
        return
    ok, msg = send(master, target[0], target[1], cmd["command"])
    finish(cmd, "EXECUTED" if ok else "FAILED", msg)


# ─────────────────────────────────────────────
# Switching plans
# ─────────────────────────────────────────────
def plan_soe(conn, plan, event, message):
    t = TOPOLOGIES[plan["substation_id"]]
    f = t.feeders[plan["feeder_slave_id"]]
    conn.execute("""
        INSERT INTO soe_log (timestamp_ms, substation_id, device_id, device_name, event_type,
                             parameter, old_value, new_value, message, operator)
        VALUES (?, ?, ?, ?, ?, 'switching_plan', '', '', ?, ?)
    """, (now_ms(), plan["substation_id"], f["id"], f["name"], event, message, plan["operator"]))


def end_plan(conn, plan, status, result):
    conn.execute("UPDATE switching_plans SET status=?, result=?, finished=? WHERE id=?",
                 (status, result, now_ts(), plan["id"]))
    plan_soe(conn, plan, f"{plan['kind']}_{status}", f"Plan #{plan['id']}: {result}")
    print(f"[PLAN] #{plan['id']} {plan['kind']} -> {status} ({result})")


def confirmed(conn, plan, step):
    """Has telemetry caught up with the step?"""
    want = step["action"] == "CLOSE"
    if step["kind"] == "breaker":
        row = conn.execute("SELECT breaker_state FROM devices WHERE slave_id=?", (step["id"],)).fetchone()
        return bool(row) and (row[0] == "CLOSED") == want
    t = TOPOLOGIES[plan["substation_id"]]
    row = conn.execute("SELECT value FROM current_values WHERE slave_id=? AND parameter=?",
                       (t.da_slave, step["id"])).fetchone()
    return bool(row) and bool(row[0]) == want


def queue_step(conn, plan, step):
    t = TOPOLOGIES[plan["substation_id"]]
    if step["kind"] == "breaker":
        dev_id, name, ss = DEVICE_INFO[step["id"]]
        slave = step["id"]
    else:
        dev_id, name, ss, slave = step["id"], t.by_id[step["id"]]["name"], t.ss_id, t.da_slave
    cur = conn.execute("""
        INSERT INTO control_commands (timestamp, slave_id, device_id, device_name, substation_id,
                                      command, operator, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING')
    """, (now_ts(), slave, dev_id, name, ss, step["action"],
          f"{plan['kind']} #{plan['id']} ({plan['operator']})"))
    return cur.lastrowid


def run_plans():
    """Advance every EXECUTING plan by at most one step."""
    conn = db()
    for plan in [dict(r) for r in conn.execute(
            "SELECT * FROM switching_plans WHERE status='EXECUTING' ORDER BY id")]:
        steps = json.loads(plan["steps"])
        i = plan["current_step"]
        if i >= len(steps):
            end_plan(conn, plan, "COMPLETED", f"all {len(steps)} steps confirmed")
            continue
        step = steps[i]
        if not plan["step_cmd_id"]:
            cmd_id = queue_step(conn, plan, step)
            conn.execute("UPDATE switching_plans SET step_cmd_id=?, step_started=? WHERE id=?",
                         (cmd_id, now_ts(), plan["id"]))
            print(f"[PLAN] #{plan['id']} step {i + 1}/{len(steps)}: {step['label']}")
            continue
        cmd = conn.execute("SELECT status, result FROM control_commands WHERE id=?",
                           (plan["step_cmd_id"],)).fetchone()
        if cmd and cmd[0] in ("FAILED", "EXPIRED"):
            end_plan(conn, plan, "FAILED", f"step {i + 1} ({step['label']}): {cmd[1]}")
        elif cmd and cmd[0] == "EXECUTED" and confirmed(conn, plan, step):
            conn.execute("UPDATE switching_plans SET current_step=?, step_cmd_id=NULL WHERE id=?",
                         (i + 1, plan["id"]))
            if i + 1 == len(steps):
                plan["current_step"] = i + 1
                end_plan(conn, plan, "COMPLETED", f"all {len(steps)} steps confirmed")
        else:
            started = datetime.strptime(plan["step_started"], FMT)
            if (datetime.now() - started).total_seconds() > STEP_CONFIRM:
                end_plan(conn, plan, "FAILED",
                         f"step {i + 1} ({step['label']}) not confirmed by telemetry "
                         f"within {STEP_CONFIRM}s")
    conn.commit()
    conn.close()


def auto_flisr():
    """Automatic mode: one FLISR plan per newly located fault."""
    conn = db()
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='flisr_auto'").fetchone()
        if not row or row[0] != "1":
            return
        for t in TOPOLOGIES.values():
            if conn.execute("SELECT 1 FROM switching_plans WHERE status='EXECUTING' "
                            "AND substation_id=?", (t.ss_id,)).fetchone():
                continue                          # one plan at a time per station
            state = flisr.load_state(conn, t, CONFIG)
            for fsid, f in t.feeders.items():
                if not state["faults"][fsid] or state["breakers"][fsid] != "TRIPPED":
                    continue
                ev = conn.execute("SELECT timestamp_ms FROM soe_log WHERE device_id=? AND "
                                  "event_type='FAULT_LOCATED' ORDER BY id DESC LIMIT 1",
                                  (f["id"],)).fetchone()
                if not ev or conn.execute(
                        "SELECT 1 FROM switching_plans WHERE feeder_slave_id=? AND kind='FLISR' "
                        "AND created >= ?", (fsid, ev[0][:19])).fetchone():
                    continue                      # not located yet, or already handled
                plan = flisr.plan_flisr(t, state, fsid)
                pid = flisr.create_plan(conn, t, plan, "AUTO", "FLISR-AUTO")
                print(f"[PLAN] #{pid} automatic FLISR for {f['name']}: "
                      f"{'EXECUTING' if plan['feasible'] else 'REJECTED'}")
                break
    finally:
        conn.close()


def main():
    master = dnp3lite.Master(HOST, PORT, MASTER_ADDR)
    print(f"[CONTROL] DNP3 control layer started (master {MASTER_ADDR} -> {HOST}:{PORT})")

    recovered = False
    last_auto = 0
    while True:
        try:
            if not recovered:
                conn = db()
                conn.execute("UPDATE control_commands SET status='FAILED', "
                             "result='controller restarted during execution' "
                             "WHERE status='EXECUTING'")
                conn.execute("UPDATE switching_plans SET status='FAILED', finished=?, "
                             "result='controller restarted during execution' "
                             "WHERE status='EXECUTING'", (now_ts(),))
                conn.commit()
                conn.close()
                recovered = True

            if time.time() - last_auto >= AUTO_EVERY:
                auto_flisr()
                last_auto = time.time()
            run_plans()

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
