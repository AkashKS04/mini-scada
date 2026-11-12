#!/usr/bin/env python3
"""
engine/modbus_client.py — Phase 4 Data Collector + Alarm Engine

Reads substations.json and polls every Modbus slave device (41 across 5 substations).

Features:
    - 4-level analog limits (high_high, high, low, low_low) per parameter
    - ISA-18.2 alarm rules:
        CRITICAL / HIGH  → manual ACK + manual clear required
        MEDIUM  / LOW    → auto-ACK + auto-clear when condition resolves
    - Breaker state monitoring (OPEN / CLOSED / TRIPPED)
    - Device OFFLINE detection (3 consecutive failures)
    - Data FREEZE detection (5 same-value polls)
    - SOE (Sequence of Events) logging with millisecond timestamps
"""

import os
import sys
import json
import time
import sqlite3
from datetime import datetime, timedelta, timezone

from pymodbus.client import ModbusTcpClient

# ─────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, 'config', 'substations.json')
DB_PATH     = os.path.join(BASE_DIR, 'scada.db')

# ─────────────────────────────────────────────
# Load config
# ─────────────────────────────────────────────
with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

ALARM_RULES = CONFIG['alarm_priority_rules']

# ─────────────────────────────────────────────
# Connection
# ─────────────────────────────────────────────
HOST          = "127.0.0.1"
PORT          = 5020
POLL_INTERVAL = 3

# Historian retention. /api/trends goes back 24 h at most, so 30 h loses nothing
# the HMI can show (24 h trends + margin) while keeping scada.db near 600 MB.
RETENTION_HOURS        = 30
RETENTION_EVERY_SECONDS = 600

# ─────────────────────────────────────────────
# Register addresses
# ─────────────────────────────────────────────
# 0-based wire addresses, Modbus convention: 40001 = address 0, 40100 = 99
REG_VOLTAGE      = 0
REG_CURRENT      = 1
REG_POWER_MW     = 2
REG_POWER_FACTOR = 3
REG_TEMPERATURE  = 4
REG_FREQUENCY    = 5
REG_FAULT_SECTION= 6    # 40007: section a relay located the last fault in, 0 = none
REG_BREAKER      = 99

# Register scale factors (divide raw value by scale to get real value)
REG_SCALES = {
    'voltage':      100.0,
    'current':      10.0,
    'power_mw':     100.0,
    'power_factor': 100.0,
    'temperature':  10.0,
    'frequency':    100.0,
}

# Breaker state names
BREAKER_STATES = {0: 'OPEN', 1: 'CLOSED', 2: 'TRIPPED', 3: 'UNKNOWN'}

# ─────────────────────────────────────────────
# Build device registry from config
# ─────────────────────────────────────────────
device_registry = {}   # {slave_id: device_dict}
substation_map  = {}   # {ss_id: substation_dict}

for ss in CONFIG['substations']:
    substation_map[ss['id']] = ss

    # Transformer
    tr = ss['transformer']
    device_registry[tr['slave_id']] = {
        'type':         'transformer',
        'id':           tr['id'],
        'name':         tr['name'],
        'substation_id':ss['id'],
        'ss_name':      ss['name'],
        'slave_id':     tr['slave_id'],
        'limits':       tr['analog_limits'],
        'parameters':   ['voltage', 'current', 'power_mw',
                          'power_factor', 'temperature', 'frequency'],
        'reg_map': {
            'voltage':      REG_VOLTAGE,
            'current':      REG_CURRENT,
            'power_mw':     REG_POWER_MW,
            'power_factor': REG_POWER_FACTOR,
            'temperature':  REG_TEMPERATURE,
            'frequency':    REG_FREQUENCY,
        }
    }

    # Feeders
    for f in ss['feeders']:
        device_registry[f['slave_id']] = {
            'type':         'feeder',
            'id':           f['id'],
            'name':         f['name'],
            'substation_id':ss['id'],
            'ss_name':      ss['name'],
            'slave_id':     f['slave_id'],
            'load_type':    f['load_type'],
            'limits':       f['analog_limits'],
            'parameters':   ['voltage', 'current', 'power_mw',
                              'power_factor', 'frequency'],
            'reg_map': {
                'voltage':      REG_VOLTAGE,
                'current':      REG_CURRENT,
                'power_mw':     REG_POWER_MW,
                'power_factor': REG_POWER_FACTOR,
                'frequency':    REG_FREQUENCY,
            }
        }

print(f"[CONFIG] Loaded {len(device_registry)} devices")

# {substation_id: transformer slave id}, used to tell if a device has supply
TRANSFORMER_OF = {ss['id']: ss['transformer']['slave_id'] for ss in CONFIG['substations']}

# Feeder-map stations: DA RTU with one status register per RMU / tie switch
sys.path.insert(0, os.path.join(BASE_DIR, 'lib'))
import topology  # noqa: E402
TOPOLOGIES = topology.build(CONFIG)

# ─────────────────────────────────────────────
# In-memory state tracking
# ─────────────────────────────────────────────
# Analog alarms: {(slave_id, parameter, limit_type): alarm_id}
active_analog_alarms = {}

# Offline alarms: {slave_id: alarm_id}
offline_alarms = {}

# Freeze alarms: {(slave_id, parameter): alarm_id}
freeze_alarms = {}

# Consecutive failure counts: {slave_id: int}
consecutive_failures = {sid: 0 for sid in device_registry}

# Freeze tracker: {(slave_id, parameter): {last_value, count}}
freeze_tracker = {}

# Breaker states: {slave_id: int}
breaker_states = {}

# Breaker trip alarms: {slave_id: alarm_id}
breaker_trip_alarms = {}

# Breaker open alarms: {slave_id: alarm_id}
breaker_open_alarms = {}

# Last fault section seen per feeder: {slave_id: int}
fault_sections = {}

# Last switch states per DA RTU: {da_slave: {switch_id: 0/1}}
switch_states = {}
da_failed = {}

# Device comms status: {slave_id: 'online'|'offline'|'frozen'}
device_comms_status = {sid: 'online' for sid in device_registry}

OFFLINE_THRESHOLD = 3
FREEZE_THRESHOLD  = 5

# Only values that change on every sample of a live feeder are checked for
# freeze. Power factor and frequency at 0.01 resolution, and transformer oil
# temperature, can legitimately hold one value for 15 s on a steady load.
FREEZE_PARAMETERS = ('voltage', 'current', 'power_mw')


# ─────────────────────────────────────────────
# Timestamp helpers
# ─────────────────────────────────────────────
def now_ts():
    """Current timestamp string for DB storage."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def now_ms():
    """Current timestamp with milliseconds for SOE log."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


# ─────────────────────────────────────────────
# Database initialisation
# ─────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Substations table
    c.execute("""
        CREATE TABLE IF NOT EXISTS substations (
            id          TEXT PRIMARY KEY,
            name        TEXT,
            short_name  TEXT,
            location    TEXT,
            voltage_level TEXT,
            status      TEXT DEFAULT 'IN_SERVICE'
        )
    """)

    # Devices table (transformers + feeders)
    c.execute("""
        CREATE TABLE IF NOT EXISTS devices (
            id              TEXT PRIMARY KEY,
            slave_id        INTEGER,
            substation_id   TEXT,
            device_type     TEXT,
            name            TEXT,
            load_type       TEXT,
            breaker_state   TEXT DEFAULT 'CLOSED',
            comms_status    TEXT DEFAULT 'online',
            last_seen       TEXT,
            updated_at      TEXT
        )
    """)

    # WAL lets the API read while the client writes, without "database is locked"
    c.execute("PRAGMA journal_mode=WAL")

    # Historian (time-series). Kept slim on purpose: device names and units
    # live in the config, and rows are deleted after RETENTION_HOURS.
    c.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            id              INTEGER PRIMARY KEY,
            timestamp       TEXT,
            slave_id        INTEGER,
            parameter       TEXT,
            value           REAL
        )
    """)

    # Real-time database: one row per (device, parameter), overwritten every
    # poll. The HMI reads live values here instead of scanning the historian.
    c.execute("""
        CREATE TABLE IF NOT EXISTS current_values (
            slave_id        INTEGER,
            parameter       TEXT,
            value           REAL,
            timestamp       TEXT,
            PRIMARY KEY (slave_id, parameter)
        )
    """)

    # Alarms (ISA-18.2 lifecycle)
    c.execute("""
        CREATE TABLE IF NOT EXISTS alarms (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp         TEXT,
            slave_id          INTEGER,
            device_id         TEXT,
            device_name       TEXT,
            substation_id     TEXT,
            parameter         TEXT,
            value             REAL,
            limit_value       REAL,
            message           TEXT,
            alarm_type        TEXT,
            priority          TEXT,
            status            TEXT DEFAULT 'ACTIVE',
            ack_required      INTEGER DEFAULT 1,
            ack_timestamp     TEXT,
            ack_by            TEXT,
            cleared_timestamp TEXT,
            rtn_timestamp     TEXT,
            auto_clear        INTEGER DEFAULT 0
        )
    """)

    # SOE log (Sequence of Events)
    c.execute("""
        CREATE TABLE IF NOT EXISTS soe_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp_ms    TEXT,
            substation_id   TEXT,
            device_id       TEXT,
            device_name     TEXT,
            event_type      TEXT,
            parameter       TEXT,
            old_value       TEXT,
            new_value       TEXT,
            message         TEXT,
            operator        TEXT DEFAULT 'SYSTEM'
        )
    """)

    # Device comms status
    c.execute("""
        CREATE TABLE IF NOT EXISTS device_status (
            slave_id        INTEGER PRIMARY KEY,
            device_id       TEXT,
            device_name     TEXT,
            substation_id   TEXT,
            status          TEXT DEFAULT 'online',
            last_seen       TEXT,
            updated_at      TEXT
        )
    """)

    # Breaker control commands (queued by the API, executed by the control layer)
    c.execute("""
        CREATE TABLE IF NOT EXISTS control_commands (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp          TEXT,
            slave_id           INTEGER,
            device_id          TEXT,
            device_name        TEXT,
            substation_id      TEXT,
            command            TEXT,
            operator           TEXT,
            status             TEXT DEFAULT 'PENDING',
            result             TEXT,
            executed_timestamp TEXT
        )
    """)

    # Switching plans (FLISR / restore normal), executed step by step by the
    # control layer, each step confirmed by telemetry before the next
    c.execute("""
        CREATE TABLE IF NOT EXISTS switching_plans (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            created           TEXT,
            substation_id     TEXT,
            feeder_slave_id   INTEGER,
            kind              TEXT,
            mode              TEXT,
            operator          TEXT,
            steps             TEXT,
            summary           TEXT,
            status            TEXT DEFAULT 'EXECUTING',
            current_step      INTEGER DEFAULT 0,
            step_cmd_id       INTEGER,
            step_started      TEXT,
            result            TEXT,
            finished          TEXT
        )
    """)

    # Key/value settings shared by the API and the control layer (e.g. FLISR auto)
    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # Indexes so trend and latest-value queries stay fast as readings grow
    c.execute("CREATE INDEX IF NOT EXISTS idx_readings_dev_ts ON readings(slave_id, timestamp)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_alarms_status ON alarms(status, id)")

    conn.commit()

    # Seed substations
    for ss in CONFIG['substations']:
        c.execute("""
            INSERT OR IGNORE INTO substations (id, name, short_name, location, voltage_level)
            VALUES (?, ?, ?, ?, ?)
        """, (ss['id'], ss['name'], ss['short_name'], ss['location'], ss['voltage_level']))

    # Seed devices
    ts = now_ts()
    for sid, dev in device_registry.items():
        c.execute("""
            INSERT OR IGNORE INTO devices
            (id, slave_id, substation_id, device_type, name, load_type, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (dev['id'], sid, dev['substation_id'], dev['type'],
              dev['name'], dev.get('load_type', ''), ts))
        c.execute("""
            INSERT OR IGNORE INTO device_status
            (slave_id, device_id, device_name, substation_id, status, updated_at)
            VALUES (?, ?, ?, ?, 'online', ?)
        """, (sid, dev['id'], dev['name'], dev['substation_id'], ts))

    conn.commit()
    conn.close()
    print("[DB] Database initialised")


def load_active_alarms():
    """Restore open alarms from DB on startup."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM alarms WHERE status IN ('ACTIVE','ACKNOWLEDGED')"
    ).fetchall()
    conn.close()

    for row in rows:
        alarm_type = row['alarm_type']
        sid        = row['slave_id']
        param      = row['parameter']

        if alarm_type == 'OFFLINE':
            offline_alarms[sid] = row['id']
        elif alarm_type == 'FREEZE':
            freeze_alarms[(sid, param)] = row['id']
        elif alarm_type == 'BREAKER_TRIP':
            breaker_trip_alarms[sid] = row['id']
        elif alarm_type in ('BREAKER_OPEN', 'UNAUTHORISED_CONTROL'):
            breaker_open_alarms[sid] = row['id']
        elif alarm_type in ('ANALOG_HIGH_HIGH', 'ANALOG_HIGH',
                             'ANALOG_LOW', 'ANALOG_LOW_LOW'):
            active_analog_alarms[(sid, param, alarm_type)] = row['id']

    total = (len(active_analog_alarms) + len(offline_alarms) + len(freeze_alarms) +
             len(breaker_trip_alarms) + len(breaker_open_alarms))
    if total:
        print(f"[ALARM] Restored {total} open alarm(s) from DB")


# ─────────────────────────────────────────────
# DB write helpers
# ─────────────────────────────────────────────
def save_readings(rows, status_rows=()):
    """
    Write one poll cycle in a single transaction. rows: (ts, slave_id, parameter, value).
    rows go to the historian and the real-time table; status_rows (e.g. the
    fault section) only to the real-time table.
    """
    if not rows and not status_rows:
        return
    conn = sqlite3.connect(DB_PATH, timeout=10)
    with conn:
        conn.executemany(
            "INSERT INTO readings (timestamp, slave_id, parameter, value) VALUES (?, ?, ?, ?)",
            rows)
        conn.executemany("""
            INSERT INTO current_values (timestamp, slave_id, parameter, value) VALUES (?, ?, ?, ?)
            ON CONFLICT(slave_id, parameter) DO UPDATE
            SET value=excluded.value, timestamp=excluded.timestamp
        """, list(rows) + list(status_rows))
    conn.close()


def poll_da_rtu(client, t, ts, status):
    """Read every switch status of a DA RTU; SOE entry for each change."""
    result = client.read_holding_registers(0, count=len(t.switches), slave=t.da_slave)
    if result.isError():
        da_failed[t.da_slave] = da_failed.get(t.da_slave, 0) + 1
        if da_failed[t.da_slave] == OFFLINE_THRESHOLD:
            print(f"  [TIMEOUT] {t.da_name} not responding")
        return
    da_failed[t.da_slave] = 0
    prev = switch_states.get(t.da_slave)
    now  = {}
    for sw in t.switches:
        v = result.registers[sw['index']]
        now[sw['id']] = v
        status.append((ts, t.da_slave, sw['id'], v))
        if prev is not None and prev.get(sw['id']) != v:
            word = 'CLOSED' if v else 'OPEN'
            conn = sqlite3.connect(DB_PATH)
            conn.execute("""
                INSERT INTO soe_log (timestamp_ms, substation_id, device_id, device_name,
                                     event_type, parameter, old_value, new_value, message)
                VALUES (?, ?, ?, ?, ?, 'switch_state', ?, ?, ?)
            """, (now_ms(), t.ss_id, sw['id'], sw['name'], f'SWITCH_{word}',
                  'CLOSED' if prev.get(sw['id']) else 'OPEN', word,
                  f"{sw['name']} ({sw['id']}) {word}"))
            conn.commit()
            conn.close()
            print(f"  [SWITCH][{sw['id']}] → {word}")
    switch_states[t.da_slave] = now


def check_fault_section(dev, section):
    """SOE entry when the relay reports a new fault location, or it is cleared."""
    sid  = dev['slave_id']
    prev = fault_sections.get(sid, 0)
    if section != prev:
        if section:
            log_soe(dev, 'FAULT_LOCATED', 'fault_section', prev, section,
                    f"{dev['name']}: fault located in section {section}")
        else:
            log_soe(dev, 'FAULT_CLEARED', 'fault_section', prev, 0,
                    f"{dev['name']}: fault indication reset")
        print(f"  [FAULT][{dev['name']}] section {prev} → {section}")
    fault_sections[sid] = section


def purge_old_readings():
    """
    Delete historian rows older than RETENTION_HOURS. Ids grow with time, so
    find the first id inside the window and delete everything below it: a
    rowid range delete, cheap even on millions of rows.
    """
    cutoff = (datetime.now() - timedelta(hours=RETENTION_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_PATH, timeout=10)
    row = conn.execute("SELECT id FROM readings WHERE timestamp >= ? ORDER BY id LIMIT 1",
                       (cutoff,)).fetchone()
    if row:
        with conn:
            n = conn.execute("DELETE FROM readings WHERE id < ?", (row[0],)).rowcount
        if n:
            print(f"[HISTORIAN] Purged {n} readings older than {RETENTION_HOURS} h")
    conn.close()


def update_comms_status(dev, status, last_seen=None):
    ts = now_ts()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        UPDATE device_status
        SET status=?, last_seen=?, updated_at=?
        WHERE slave_id=?
    """, (status, last_seen or ts, ts, dev['slave_id']))
    conn.commit()
    conn.close()
    device_comms_status[dev['slave_id']] = status


def log_soe(dev, event_type, parameter='', old_val='', new_val='', message=''):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO soe_log
        (timestamp_ms, substation_id, device_id, device_name,
         event_type, parameter, old_value, new_value, message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (now_ms(), dev['substation_id'], dev['id'], dev['name'],
          event_type, parameter, str(old_val), str(new_val), message))
    conn.commit()
    conn.close()


def raise_alarm(dev, parameter, value, limit_value, alarm_type, priority, message):
    """Create a new ACTIVE alarm. Returns alarm_id."""
    rules       = ALARM_RULES[priority]
    ack_required= 1 if rules['manual_ack_required'] else 0
    auto_clear  = 1 if rules['auto_clear'] else 0

    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("""
        INSERT INTO alarms
        (timestamp, slave_id, device_id, device_name, substation_id,
         parameter, value, limit_value, message, alarm_type, priority,
         status, ack_required, auto_clear)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
    """, (now_ts(), dev['slave_id'], dev['id'], dev['name'],
          dev['substation_id'], parameter, value, limit_value,
          message, alarm_type, priority, ack_required, auto_clear))
    alarm_id = cur.lastrowid
    conn.commit()
    conn.close()

    print(f"  [ALARM][{dev['name']}][{priority}][{alarm_type}] {message}")

    # SOE entry
    log_soe(dev, 'ALARM_RAISED', parameter, '', str(value), message)

    return alarm_id


def clear_alarm(alarm_id, dev, reason, auto=False):
    """
    Returns True if the alarm is closed (CLEARED), False if it stays open.

    auto=True means the condition has returned to normal:
        MEDIUM/LOW (auto_clear)   -> CLEARED automatically
        CRITICAL/HIGH (manual)    -> stays open, marked "returned to normal"
                                     (rtn_timestamp). The operator must still
                                     ACK and clear it.
    """
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT auto_clear FROM alarms WHERE id=?", (alarm_id,)
    ).fetchone()

    if not row:
        conn.close()
        return True   # alarm no longer exists, stop tracking it

    if auto and not row[0]:
        cur = conn.execute(
            "UPDATE alarms SET rtn_timestamp=? "
            "WHERE id=? AND rtn_timestamp IS NULL "
            "AND status IN ('ACTIVE','ACKNOWLEDGED')",
            (now_ts(), alarm_id)
        )
        conn.commit()
        changed = cur.rowcount
        conn.close()
        if changed:
            print(f"  [RTN][{dev['name']}] {reason} - awaiting operator ACK/clear")
            log_soe(dev, 'ALARM_RTN', message=reason)
        return False

    conn.execute(
        "UPDATE alarms SET status='CLEARED', cleared_timestamp=? WHERE id=?",
        (now_ts(), alarm_id)
    )
    conn.commit()
    conn.close()

    print(f"  [CLEARED][{dev['name']}] {reason}")
    log_soe(dev, 'ALARM_CLEARED', message=reason)
    return True


def reopen_alarm(alarm_id):
    """Condition is back in breach: drop the 'returned to normal' mark."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE alarms SET rtn_timestamp=NULL "
        "WHERE id=? AND rtn_timestamp IS NOT NULL",
        (alarm_id,)
    )
    conn.commit()
    conn.close()


def sync_open_alarms():
    """
    Forget tracked alarms that the operator has already cleared through the
    API, so a condition that is still in breach raises a fresh alarm.
    """
    conn = sqlite3.connect(DB_PATH)
    open_ids = {r[0] for r in conn.execute(
        "SELECT id FROM alarms WHERE status IN ('ACTIVE','ACKNOWLEDGED')"
    )}
    conn.close()

    for table in (active_analog_alarms, offline_alarms,
                  freeze_alarms, breaker_trip_alarms, breaker_open_alarms):
        for key in [k for k, v in table.items() if v not in open_ids]:
            del table[key]


# ─────────────────────────────────────────────
# 4-level analog alarm state machine
# ─────────────────────────────────────────────
def check_analog_alarm(dev, parameter, value):
    """
    Checks value against 4 limit levels.
    Raises alarm if breached, clears if returned to normal.
    One alarm per (device, parameter, limit_type) at a time.
    """
    limits = dev['limits'].get(parameter)
    if not limits:
        return

    sid = dev['slave_id']

    # Determine which limit is breached (if any)
    # high_high takes priority over high, low_low over low
    breached_type  = None
    breached_limit = None

    if 'high_high' in limits and value >= limits['high_high']:
        breached_type  = 'ANALOG_HIGH_HIGH'
        breached_limit = limits['high_high']
        priority       = limits.get('priority_high_high', 'CRITICAL')
    elif 'high' in limits and value >= limits['high']:
        breached_type  = 'ANALOG_HIGH'
        breached_limit = limits['high']
        priority       = limits.get('priority_high', 'HIGH')
    elif 'low_low' in limits and limits['low_low'] >= 0 and value <= limits['low_low']:
        breached_type  = 'ANALOG_LOW_LOW'
        breached_limit = limits['low_low']
        priority       = limits.get('priority_low_low', 'CRITICAL')
    elif 'low' in limits and value <= limits['low']:
        breached_type  = 'ANALOG_LOW'
        breached_limit = limits['low']
        priority       = limits.get('priority_low', 'HIGH')

    unit = limits.get('unit', '')

    # Which limits are violated right now?
    def violated(atype):
        if atype == 'ANALOG_HIGH_HIGH':
            return 'high_high' in limits and value >= limits['high_high']
        if atype == 'ANALOG_HIGH':
            return 'high' in limits and value >= limits['high']
        if atype == 'ANALOG_LOW_LOW':
            return ('low_low' in limits and limits['low_low'] >= 0
                    and value <= limits['low_low'])
        return 'low' in limits and value <= limits['low']

    labels = {
        'ANALOG_HIGH_HIGH': 'HIGH-HIGH', 'ANALOG_HIGH': 'HIGH',
        'ANALOG_LOW_LOW':   'LOW-LOW',   'ANALOG_LOW':  'LOW',
    }

    for atype in ('ANALOG_HIGH_HIGH', 'ANALOG_HIGH',
                  'ANALOG_LOW_LOW', 'ANALOG_LOW'):
        key = (sid, parameter, atype)

        if violated(atype):
            if key in active_analog_alarms:
                # Still (or again) in breach: keep the existing alarm,
                # never raise a duplicate
                reopen_alarm(active_analog_alarms[key])
            elif atype == breached_type:
                msg = (f"{parameter.upper()} {labels[atype]}: "
                       f"{value:.3f}{unit} (limit {breached_limit}{unit})")
                active_analog_alarms[key] = raise_alarm(
                    dev, parameter, value, breached_limit,
                    atype, priority, msg)
        elif key in active_analog_alarms:
            if clear_alarm(active_analog_alarms[key], dev,
                           f"{parameter.upper()} returned to normal",
                           auto=True):
                del active_analog_alarms[key]


# ─────────────────────────────────────────────
# Breaker state monitoring
# ─────────────────────────────────────────────
def load_breaker_states():
    """Start from the last known breaker state so a restart does not look like a change."""
    codes = {'OPEN': 0, 'CLOSED': 1, 'TRIPPED': 2}
    conn = sqlite3.connect(DB_PATH)
    for sid, state in conn.execute("SELECT slave_id, breaker_state FROM devices"):
        if state in codes:
            breaker_states[sid] = codes[state]
    conn.close()


def find_operator_command(sid):
    """An OPEN sent through the control layer in the last 60 s, if there is one."""
    since = (datetime.now() - timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, operator FROM control_commands "
        "WHERE slave_id=? AND command='OPEN' AND status IN ('EXECUTING','EXECUTED') "
        "AND timestamp>=? ORDER BY id DESC LIMIT 1", (sid, since)).fetchone()
    conn.close()
    return {'id': row[0], 'operator': row[1]} if row else None


def check_breaker_state(dev, raw_state):
    """Detect breaker state changes and raise TRIP alarms."""
    sid       = dev['slave_id']
    state_str = BREAKER_STATES.get(raw_state, 'UNKNOWN')
    prev_raw  = breaker_states.get(sid)
    prev_str  = BREAKER_STATES.get(prev_raw, 'UNKNOWN') if prev_raw is not None else 'UNKNOWN'

    if prev_raw == raw_state:
        return  # No change

    # Update DB breaker state
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE devices SET breaker_state=?, updated_at=? WHERE slave_id=?",
        (state_str, now_ts(), sid)
    )
    conn.commit()
    conn.close()

    # SOE entry for every state change
    log_soe(dev, f'BREAKER_{state_str}', 'breaker_state',
            prev_str, state_str,
            f"{dev['name']} breaker changed: {prev_str} → {state_str}")

    print(f"  [BREAKER][{dev['name']}] {prev_str} → {state_str}")

    # TRIP alarm
    if raw_state == 2:  # TRIPPED
        if sid in breaker_trip_alarms:
            reopen_alarm(breaker_trip_alarms[sid])
        else:
            msg = (f"BREAKER TRIP: {dev['name']} breaker has TRIPPED - "
                   f"feeder de-energised")
            breaker_trip_alarms[sid] = raise_alarm(
                dev, 'breaker_state', raw_state, 2,
                'BREAKER_TRIP', 'CRITICAL', msg)

    # Breaker closed again: trip alarm returns to normal, operator still
    # has to ACK and clear it
    elif raw_state == 1 and sid in breaker_trip_alarms:
        if clear_alarm(breaker_trip_alarms[sid], dev,
                       f"{dev['name']} breaker reclosed - feeder restored",
                       auto=True):
            del breaker_trip_alarms[sid]

    # OPEN: an operator command, or a change nobody asked for
    if raw_state == 0:
        if sid in breaker_open_alarms:
            reopen_alarm(breaker_open_alarms[sid])
        else:
            cmd = find_operator_command(sid)
            if cmd:
                atype = 'BREAKER_OPEN'
                priority = 'CRITICAL' if dev['type'] == 'transformer' else 'HIGH'
                msg = (f"BREAKER OPEN: {dev['name']} opened by {cmd['operator']} "
                       f"(command #{cmd['id']})")
            else:
                atype, priority = 'UNAUTHORISED_CONTROL', 'CRITICAL'
                msg = (f"BREAKER OPEN: {dev['name']} opened WITHOUT an operator "
                       f"command - possible unauthorised or local operation")
            breaker_open_alarms[sid] = raise_alarm(
                dev, 'breaker_state', raw_state, 0, atype, priority, msg)
    elif raw_state == 1 and sid in breaker_open_alarms:
        if clear_alarm(breaker_open_alarms[sid], dev,
                       f"{dev['name']} breaker closed", auto=True):
            del breaker_open_alarms[sid]

    breaker_states[sid] = raw_state


# ─────────────────────────────────────────────
# Offline detection
# ─────────────────────────────────────────────
def handle_poll_failure(dev):
    sid = dev['slave_id']
    consecutive_failures[sid] += 1
    count = consecutive_failures[sid]

    print(f"  [TIMEOUT] Slave {sid} ({dev['name']}) — failed poll #{count}")

    if count == OFFLINE_THRESHOLD:
        if sid in offline_alarms:
            reopen_alarm(offline_alarms[sid])
        else:
            msg = (f"COMMS FAIL: {dev['name']} OFFLINE - "
                   f"{count} consecutive poll failures")
            offline_alarms[sid] = raise_alarm(
                dev, 'communication', 0, 0, 'OFFLINE', 'HIGH', msg)
        update_comms_status(dev, 'offline')


def handle_poll_success(dev):
    sid = dev['slave_id']
    was_offline = consecutive_failures[sid] >= OFFLINE_THRESHOLD
    consecutive_failures[sid] = 0

    if was_offline:
        update_comms_status(dev, 'online', now_ts())
        log_soe(dev, 'COMMS_RESTORED', message=f"{dev['name']} back online")
        if sid in offline_alarms:
            if clear_alarm(offline_alarms[sid], dev,
                           f"{dev['name']} comms restored", auto=True):
                del offline_alarms[sid]


# ─────────────────────────────────────────────
# Data freeze detection
# ─────────────────────────────────────────────
def is_energised(dev):
    """Own breaker and the substation transformer breaker are both CLOSED."""
    return (breaker_states.get(dev['slave_id'], 1) == 1 and
            breaker_states.get(TRANSFORMER_OF[dev['substation_id']], 1) == 1)


def check_freeze(dev, parameter, value):
    sid = dev['slave_id']
    key = (sid, parameter)

    # A de-energised device legitimately reads constant zeros, so it is never
    # "frozen": reset the tracker and drop any freeze alarm raised earlier.
    # Same for parameters that are not freeze-monitored at all.
    monitored = parameter in FREEZE_PARAMETERS
    if not monitored or not is_energised(dev):
        freeze_tracker.pop(key, None)
        if key in freeze_alarms:
            why = "device de-energised" if monitored else "not freeze-monitored"
            clear_alarm(freeze_alarms[key], dev,
                        f"{parameter.upper()} {why}", auto=True)
            del freeze_alarms[key]
            if (not [k for k in freeze_alarms if k[0] == sid]
                    and device_comms_status.get(sid) == 'frozen'):
                update_comms_status(dev, 'online', now_ts())
        return

    if key not in freeze_tracker:
        freeze_tracker[key] = {'last_value': value, 'count': 1}
        return

    tracker = freeze_tracker[key]

    if value == tracker['last_value']:
        tracker['count'] += 1
    else:
        tracker['last_value'] = value
        tracker['count'] = 1
        if key in freeze_alarms:
            clear_alarm(freeze_alarms[key], dev,
                        f"{parameter.upper()} register unfrozen", auto=True)
            del freeze_alarms[key]
            # Check if any freezes remain for this device
            remaining = [k for k in freeze_alarms if k[0] == sid]
            if not remaining and device_comms_status.get(sid) == 'frozen':
                update_comms_status(dev, 'online', now_ts())
        return

    if tracker['count'] >= FREEZE_THRESHOLD and key not in freeze_alarms:
        msg = (f"DATA FREEZE: {parameter.upper()} stuck at "
               f"{value} for {tracker['count']} consecutive polls")
        alarm_id = raise_alarm(dev, parameter, value, value,
                               'FREEZE', 'MEDIUM', msg)
        freeze_alarms[key] = alarm_id
        update_comms_status(dev, 'frozen')


# ─────────────────────────────────────────────
# Main polling loop
# ─────────────────────────────────────────────
def main():
    init_db()

    # Held open for the life of the client and never used. SQLite deletes the
    # WAL side files when the last connection closes; if anyone else (e.g.
    # akash running a check) then opens scada.db, they recreate scada.db-wal/-shm
    # owned by themselves and this service fails with "readonly database".
    # It must run one read: an idle connection is not attached to the WAL yet.
    wal_keeper = sqlite3.connect(DB_PATH)
    wal_keeper.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()

    load_active_alarms()
    load_breaker_states()

    client = ModbusTcpClient(HOST, port=PORT)
    print(f"[CLIENT] Connecting to {HOST}:{PORT} ...")

    connected = False
    while not connected:
        connected = client.connect()
        if not connected:
            print("[CLIENT] Waiting for server...")
            time.sleep(3)

    print("[CLIENT] Connected.")
    print(f"[CLIENT] Polling {len(device_registry)} devices every {POLL_INTERVAL}s")
    print()

    last_purge = 0
    try:
        while True:
            ts = now_ts()
            sync_open_alarms()
            batch, status = [], []

            for sid in sorted(device_registry.keys()):
                dev    = device_registry[sid]

                # ── Read registers 40001-40007 in one request ──
                result = client.read_holding_registers(
                    REG_VOLTAGE, count=REG_FAULT_SECTION - REG_VOLTAGE + 1, slave=sid
                )
                all_ok = not result.isError()

                if all_ok:
                    for param, reg_addr in dev['reg_map'].items():
                        value = result.registers[reg_addr - REG_VOLTAGE] / REG_SCALES[param]
                        batch.append((ts, sid, param, value))

                        # Check 4-level analog alarms
                        check_analog_alarm(dev, param, value)

                        # Check freeze
                        check_freeze(dev, param, value)

                    if dev['type'] == 'feeder':
                        section = result.registers[REG_FAULT_SECTION - REG_VOLTAGE]
                        status.append((ts, sid, 'fault_section', section))
                        check_fault_section(dev, section)

                # ── Read breaker state ──
                br_result = client.read_holding_registers(
                    REG_BREAKER, count=1, slave=sid
                )
                if not br_result.isError():
                    check_breaker_state(dev, br_result.registers[0])

                # ── Offline detection ──
                if all_ok:
                    handle_poll_success(dev)
                else:
                    handle_poll_failure(dev)

            # Historian + real-time table, one transaction per cycle
            for t in TOPOLOGIES.values():
                poll_da_rtu(client, t, ts, status)

            save_readings(batch, status)

            if time.time() - last_purge > RETENTION_EVERY_SECONDS:
                purge_old_readings()
                last_purge = time.time()

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\n[CLIENT] Stopped.")
    finally:
        client.close()


if __name__ == "__main__":
    main()
