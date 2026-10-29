#!/usr/bin/env python3
"""
api/app.py - Phase 4 REST API

Reads substations.json (structure + limits) and scada.db (live data).

Endpoints
    GET  /                              placeholder page (HMI comes later)
    GET  /api/health                    service + database check
    GET  /api/substations               all substations with alarm/comms summary
    GET  /api/substations/<ss_id>       full detail: transformer, feeders, live values
    GET  /api/feedermap/<ss_id>         load nodes + energised state (SS-01)
    GET  /api/alarms                    open alarms (ACTIVE / ACKNOWLEDGED)
    GET  /api/alarms/summary            counts for header chips
    GET  /api/alarms/history            CLEARED alarms
    POST /api/alarms/<id>/ack           acknowledge an alarm
    POST /api/alarms/<id>/clear         clear (delete) an alarm
    GET  /api/soe                       Sequence of Events log
    GET  /api/trends                    historian data  ?slave_id=2&hours=1
    POST /api/control/breaker           queue a breaker OPEN/CLOSE command
    GET  /api/control/commands          recent control commands
    GET  /api/control/commands/<id>     status of one command

Alarm rules (ISA-18.2 style)
    CRITICAL / HIGH  must be ACKNOWLEDGED, then CLEARED by the operator.
                     They never disappear on their own, even after the value
                     returns to normal (they show as "returned to normal").
    MEDIUM / LOW     clear automatically when the condition resolves.
"""

import os
import json
import sqlite3
from datetime import datetime, timedelta

from flask import Flask, jsonify, request, render_template, render_template_string

# ─────────────────────────────────────────────
# Paths + app
# ─────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, 'config', 'substations.json')
DB_PATH     = os.path.join(BASE_DIR, 'scada.db')

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, 'templates'),
    static_folder=os.path.join(BASE_DIR, 'static'),
)

with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

PRIORITY_RANK     = {'CRITICAL': 1, 'HIGH': 2, 'MEDIUM': 3, 'LOW': 4}
QUALITY_BY_STATUS = {'online': 'GOOD', 'frozen': 'SUSPECT', 'offline': 'BAD'}

# ─────────────────────────────────────────────
# Device index built from config
# ─────────────────────────────────────────────
SUBSTATIONS = {ss['id']: ss for ss in CONFIG['substations']}
DEVICES = {}

for _ss in CONFIG['substations']:
    _tr = _ss['transformer']
    DEVICES[_tr['slave_id']] = {
        'slave_id':      _tr['slave_id'],
        'id':            _tr['id'],
        'name':          _tr['name'],
        'type':          'transformer',
        'substation_id': _ss['id'],
        'rating_mva':    _tr['rating_mva'],
        'limits':        _tr['analog_limits'],
    }
    for _f in _ss['feeders']:
        DEVICES[_f['slave_id']] = {
            'slave_id':      _f['slave_id'],
            'id':            _f['id'],
            'name':          _f['name'],
            'type':          'feeder',
            'substation_id': _ss['id'],
            'feeder_number': _f['feeder_number'],
            'load_type':     _f['load_type'],
            'consumers':     _f.get('estimated_consumers', 0),
            'connected_kw':  _f.get('connected_load_kw', 0),
            'limits':        _f['analog_limits'],
        }


# ─────────────────────────────────────────────
# Database helpers
# ─────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def query(sql, args=()):
    conn = get_db()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def execute(sql, args=()):
    """Run a write. Returns (rowcount, lastrowid)."""
    conn = get_db()
    try:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.rowcount, cur.lastrowid
    finally:
        conn.close()


def now_ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def now_ms():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def log_soe(substation_id, device_id, device_name, event_type,
            parameter='', old='', new='', message='', operator='SYSTEM'):
    execute("""
        INSERT INTO soe_log
        (timestamp_ms, substation_id, device_id, device_name, event_type,
         parameter, old_value, new_value, message, operator)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (now_ms(), substation_id, device_id, device_name, event_type,
          parameter, str(old), str(new), message, operator))


def get_operator():
    data = request.get_json(silent=True) or {}
    return str(data.get('operator') or 'operator')[:40]


# ─────────────────────────────────────────────
# Live data builders
# ─────────────────────────────────────────────
def latest_values():
    """{slave_id: {parameter: {value, timestamp}}} - newest reading of each."""
    rows = query("""
        SELECT r.slave_id, r.parameter, r.value, r.timestamp
        FROM readings r
        JOIN (SELECT MAX(id) AS mid FROM readings
              GROUP BY slave_id, parameter) m ON r.id = m.mid
    """)
    out = {}
    for r in rows:
        out.setdefault(r['slave_id'], {})[r['parameter']] = {
            'value': r['value'], 'timestamp': r['timestamp']
        }
    return out


def device_states():
    """{slave_id: {breaker_state, comms_status, last_seen}}"""
    rows = query("""
        SELECT d.slave_id, d.breaker_state,
               s.status AS comms_status, s.last_seen
        FROM devices d
        LEFT JOIN device_status s ON s.slave_id = d.slave_id
    """)
    return {r['slave_id']: r for r in rows}


def empty_summary():
    return {'count': 0, 'unacked': 0, 'worst': None,
            'by_priority': {p: 0 for p in PRIORITY_RANK}, 'by_param': {}}


def open_alarm_summary():
    """Per-device summary of open alarms."""
    rows = query("""
        SELECT slave_id, parameter, priority, status, ack_required, rtn_timestamp
        FROM alarms WHERE status IN ('ACTIVE', 'ACKNOWLEDGED')
    """)
    out = {}
    for r in rows:
        s = out.setdefault(r['slave_id'], empty_summary())
        s['count'] += 1
        s['by_priority'][r['priority']] += 1
        if r['status'] == 'ACTIVE' and r['ack_required']:
            s['unacked'] += 1
        if s['worst'] is None or PRIORITY_RANK[r['priority']] < PRIORITY_RANK[s['worst']]:
            s['worst'] = r['priority']
        # Only conditions still in breach colour a value; "returned to
        # normal" alarms do not
        if not r['rtn_timestamp']:
            cur = s['by_param'].get(r['parameter'])
            if cur is None or PRIORITY_RANK[r['priority']] < PRIORITY_RANK[cur]:
                s['by_param'][r['parameter']] = r['priority']
    return out


def build_device(dev, values, states, alarms):
    sid   = dev['slave_id']
    st    = states.get(sid, {})
    comms = st.get('comms_status') or 'online'
    summ  = alarms.get(sid) or empty_summary()

    params = {}
    for p, info in values.get(sid, {}).items():
        lim = dev['limits'].get(p, {})
        params[p] = {
            'value':          round(info['value'], 3),
            'timestamp':      info['timestamp'],
            'unit':           lim.get('unit', ''),
            'limits':         {k: lim[k] for k in
                               ('high_high', 'high', 'low', 'low_low') if k in lim},
            'alarm_priority': summ['by_param'].get(p),
        }

    out = {
        'slave_id':      sid,
        'id':            dev['id'],
        'name':          dev['name'],
        'type':          dev['type'],
        'breaker_state': st.get('breaker_state') or 'UNKNOWN',
        'comms_status':  comms,
        'quality':       QUALITY_BY_STATUS.get(comms, 'BAD'),
        'last_seen':     st.get('last_seen'),
        'alarms': {
            'open':        summ['count'],
            'unacked':     summ['unacked'],
            'worst':       summ['worst'],
            'by_priority': summ['by_priority'],
        },
        'parameters': params,
    }
    if dev['type'] == 'transformer':
        out['rating_mva'] = dev['rating_mva']
    else:
        out['feeder_number'] = dev['feeder_number']
        out['load_type']     = dev['load_type']
        out['consumers']     = dev['consumers']
        out['connected_kw']  = dev['connected_kw']
    return out


def decorate_alarm(a):
    in_rtn = bool(a.get('rtn_timestamp'))
    if a['status'] == 'ACTIVE':
        a['state'] = 'RTN_UNACK' if in_rtn else 'ACTIVE_UNACK'
    elif a['status'] == 'ACKNOWLEDGED':
        a['state'] = 'RTN_ACK' if in_rtn else 'ACTIVE_ACK'
    else:
        a['state'] = 'CLEARED'
    a['can_ack']   = a['status'] == 'ACTIVE' and bool(a['ack_required'])
    a['can_clear'] = (a['status'] != 'CLEARED' and
                      (a['status'] == 'ACKNOWLEDGED' or not a['ack_required']))
    return a


# ─────────────────────────────────────────────
# Pages
# ─────────────────────────────────────────────
PLACEHOLDER = """
<!DOCTYPE html><html><head><meta charset="utf-8"><title>Mini SCADA API</title>
<style>body{font-family:sans-serif;background:#0d1117;color:#e6edf3;padding:40px}
a{color:#58a6ff}code{background:#161b22;padding:2px 6px;border-radius:4px}</style></head>
<body><h1>Mini SCADA - Phase 4 API</h1>
<p>The station HMI is not built yet. The API is live:</p><ul>
<li><a href="/api/health">/api/health</a></li>
<li><a href="/api/substations">/api/substations</a></li>
<li><a href="/api/substations/SS-01">/api/substations/SS-01</a></li>
<li><a href="/api/feedermap/SS-01">/api/feedermap/SS-01</a></li>
<li><a href="/api/alarms">/api/alarms</a></li>
<li><a href="/api/soe">/api/soe</a></li>
<li><a href="/api/trends?slave_id=2&hours=1">/api/trends?slave_id=2&amp;hours=1</a></li>
</ul></body></html>
"""


@app.route('/')
def index():
    if os.path.exists(os.path.join(BASE_DIR, 'templates', 'hmi.html')):
        return render_template('hmi.html')
    return render_template_string(PLACEHOLDER)


@app.route('/api/health')
def api_health():
    try:
        last = query("SELECT id, timestamp FROM readings ORDER BY id DESC LIMIT 1")
        open_n = query("SELECT COUNT(*) AS n FROM alarms "
                       "WHERE status IN ('ACTIVE','ACKNOWLEDGED')")[0]['n']
        return jsonify({
            'status':             'ok',
            'devices_configured': len(DEVICES),
            'last_reading':       last[0]['timestamp'] if last else None,
            'open_alarms':        open_n,
        })
    except sqlite3.Error as e:
        return jsonify({'status': 'error', 'detail': str(e)}), 503


# ─────────────────────────────────────────────
# Substations
# ─────────────────────────────────────────────
@app.route('/api/substations')
def api_substations():
    states = device_states()
    alarms = open_alarm_summary()
    out = []

    for ss in CONFIG['substations']:
        sids = [ss['transformer']['slave_id']] + [f['slave_id'] for f in ss['feeders']]
        by_priority = {p: 0 for p in PRIORITY_RANK}
        unacked = 0
        comms_issues = 0
        for sid in sids:
            summ = alarms.get(sid) or empty_summary()
            for p, n in summ['by_priority'].items():
                by_priority[p] += n
            unacked += summ['unacked']
            if (states.get(sid, {}).get('comms_status') or 'online') != 'online':
                comms_issues += 1

        feeder_states = [states.get(f['slave_id'], {}).get('breaker_state')
                         for f in ss['feeders']]
        out.append({
            'id':                   ss['id'],
            'name':                 ss['name'],
            'short_name':           ss['short_name'],
            'location':             ss['location'],
            'voltage_level':        ss['voltage_level'],
            'is_feeder_map':        ss.get('is_feeder_map_substation', False),
            'feeder_count':         len(ss['feeders']),
            'feeders_open_or_tripped': sum(1 for b in feeder_states
                                           if b in ('OPEN', 'TRIPPED')),
            'comms_issues':         comms_issues,
            'alarms_by_priority':   by_priority,
            'unacked_alarms':       unacked,
        })
    return jsonify(out)


@app.route('/api/substations/<ss_id>')
def api_substation_detail(ss_id):
    ss = SUBSTATIONS.get(ss_id)
    if not ss:
        return jsonify({'error': f'unknown substation {ss_id}'}), 404

    values = latest_values()
    states = device_states()
    alarms = open_alarm_summary()

    tr = build_device(DEVICES[ss['transformer']['slave_id']], values, states, alarms)
    tr['energised'] = tr['breaker_state'] == 'CLOSED'

    feeders = []
    for f in ss['feeders']:
        fd = build_device(DEVICES[f['slave_id']], values, states, alarms)
        # Simple connectivity rule for now: a feeder is live when the
        # transformer breaker and its own breaker are both CLOSED.
        # A full topology processor replaces this later.
        fd['energised'] = tr['energised'] and fd['breaker_state'] == 'CLOSED'
        feeders.append(fd)

    return jsonify({
        'id':            ss['id'],
        'name':          ss['name'],
        'short_name':    ss['short_name'],
        'location':      ss['location'],
        'voltage_level': ss['voltage_level'],
        'is_feeder_map': ss.get('is_feeder_map_substation', False),
        'transformer':   tr,
        'feeders':       feeders,
    })


@app.route('/api/feedermap/<ss_id>')
def api_feedermap(ss_id):
    ss = SUBSTATIONS.get(ss_id)
    if not ss:
        return jsonify({'error': f'unknown substation {ss_id}'}), 404
    if not any(f.get('load_nodes') for f in ss['feeders']):
        return jsonify({'error': f'{ss_id} has no feeder map'}), 404

    states = device_states()
    xfmr_ok = (states.get(ss['transformer']['slave_id'], {})
               .get('breaker_state') or 'CLOSED') == 'CLOSED'

    feeders, nodes = [], []
    total_consumers = affected_consumers = 0
    total_kw = affected_kw = 0

    for f in ss['feeders']:
        breaker  = states.get(f['slave_id'], {}).get('breaker_state') or 'CLOSED'
        energised = xfmr_ok and breaker == 'CLOSED'
        feeders.append({
            'slave_id':      f['slave_id'],
            'id':            f['id'],
            'name':          f['name'],
            'feeder_number': f['feeder_number'],
            'load_type':     f['load_type'],
            'breaker_state': breaker,
            'energised':     energised,
        })
        for n in f.get('load_nodes', []):
            nodes.append({
                'id':        n['id'],
                'name':      n['name'],
                'type':      n['type'],
                'consumers': n['consumers'],
                'load_kw':   n['load_kw'],
                'x':         n['x'],
                'y':         n['y'],
                'feeder_slave_id': f['slave_id'],
                'energised': energised,
            })
            total_consumers += n['consumers']
            total_kw        += n['load_kw']
            if not energised:
                affected_consumers += n['consumers']
                affected_kw        += n['load_kw']

    return jsonify({
        'substation':   {'id': ss['id'], 'name': ss['name'],
                         'transformer_energised': xfmr_ok},
        'feeders':      feeders,
        'load_nodes':   nodes,
        'impact': {
            'total_consumers':    total_consumers,
            'affected_consumers': affected_consumers,
            'total_load_kw':      total_kw,
            'affected_load_kw':   affected_kw,
        },
    })


# ─────────────────────────────────────────────
# Alarms
# ─────────────────────────────────────────────
@app.route('/api/alarms')
def api_alarms():
    ss_id = request.args.get('substation')
    sql = """
        SELECT * FROM alarms
        WHERE status IN ('ACTIVE', 'ACKNOWLEDGED')
    """
    args = []
    if ss_id:
        sql += " AND substation_id = ?"
        args.append(ss_id)
    sql += """
        ORDER BY
            CASE priority WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2
                          WHEN 'MEDIUM' THEN 3 ELSE 4 END,
            id DESC
        LIMIT 200
    """
    return jsonify([decorate_alarm(a) for a in query(sql, args)])


@app.route('/api/alarms/summary')
def api_alarm_summary():
    rows = query("""
        SELECT priority, status, COUNT(*) AS n FROM alarms
        WHERE status IN ('ACTIVE', 'ACKNOWLEDGED')
        GROUP BY priority, status
    """)
    by_priority = {p: 0 for p in PRIORITY_RANK}
    active = acknowledged = 0
    for r in rows:
        by_priority[r['priority']] += r['n']
        if r['status'] == 'ACTIVE':
            active += r['n']
        else:
            acknowledged += r['n']
    unacked = query("SELECT COUNT(*) AS n FROM alarms "
                    "WHERE status='ACTIVE' AND ack_required=1")[0]['n']
    return jsonify({
        'by_priority':  by_priority,
        'active':       active,
        'acknowledged': acknowledged,
        'unacked_manual_alarms': unacked,
    })


@app.route('/api/alarms/history')
def api_alarm_history():
    limit = min(request.args.get('limit', 50, type=int), 500)
    rows = query("SELECT * FROM alarms WHERE status='CLEARED' "
                 "ORDER BY id DESC LIMIT ?", (limit,))
    return jsonify([decorate_alarm(a) for a in rows])


@app.route('/api/alarms/<int:alarm_id>/ack', methods=['POST'])
def api_ack(alarm_id):
    operator = get_operator()
    rows = query("SELECT * FROM alarms WHERE id=?", (alarm_id,))
    if not rows:
        return jsonify({'error': 'alarm not found'}), 404
    a = rows[0]
    if a['status'] != 'ACTIVE':
        return jsonify({'error': f"alarm is {a['status']}, only ACTIVE alarms can be acknowledged"}), 409

    execute("UPDATE alarms SET status='ACKNOWLEDGED', ack_timestamp=?, ack_by=? "
            "WHERE id=? AND status='ACTIVE'", (now_ts(), operator, alarm_id))
    log_soe(a['substation_id'], a['device_id'], a['device_name'],
            'ALARM_ACK', a['parameter'], 'ACTIVE', 'ACKNOWLEDGED',
            f"Alarm #{alarm_id} acknowledged: {a['message']}", operator)
    return jsonify({'status': 'ok', 'alarm_id': alarm_id, 'ack_by': operator})


@app.route('/api/alarms/<int:alarm_id>/clear', methods=['POST'])
def api_clear(alarm_id):
    operator = get_operator()
    rows = query("SELECT * FROM alarms WHERE id=?", (alarm_id,))
    if not rows:
        return jsonify({'error': 'alarm not found'}), 404
    a = rows[0]
    if a['status'] == 'CLEARED':
        return jsonify({'error': 'alarm already cleared'}), 409
    if a['ack_required'] and a['status'] != 'ACKNOWLEDGED':
        return jsonify({'error': f"{a['priority']} alarms must be acknowledged before they can be cleared"}), 409

    execute("UPDATE alarms SET status='CLEARED', cleared_timestamp=? WHERE id=?",
            (now_ts(), alarm_id))
    log_soe(a['substation_id'], a['device_id'], a['device_name'],
            'ALARM_CLEARED', a['parameter'], a['status'], 'CLEARED',
            f"Alarm #{alarm_id} cleared by operator", operator)
    return jsonify({'status': 'ok', 'alarm_id': alarm_id, 'cleared_by': operator})


# ─────────────────────────────────────────────
# SOE log
# ─────────────────────────────────────────────
@app.route('/api/soe')
def api_soe():
    limit = min(request.args.get('limit', 100, type=int), 1000)
    ss_id = request.args.get('substation')
    sql, args = "SELECT * FROM soe_log", []
    if ss_id:
        sql += " WHERE substation_id = ?"
        args.append(ss_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return jsonify(query(sql, args))


# ─────────────────────────────────────────────
# Historian trends
# ─────────────────────────────────────────────
@app.route('/api/trends')
def api_trends():
    slave_id = request.args.get('slave_id', type=int)
    hours    = request.args.get('hours', 1, type=int)
    param    = request.args.get('parameter')

    dev = DEVICES.get(slave_id)
    if not dev:
        return jsonify({'error': 'unknown slave_id'}), 404
    if hours not in (1, 6, 24):
        hours = 1

    since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    args  = [slave_id, since]
    pfilter = ""
    if param:
        pfilter = " AND parameter = ?"
        args.append(param)

    if hours == 1:
        rows = query(
            "SELECT timestamp AS t, parameter, value FROM readings "
            "WHERE slave_id=? AND timestamp>=?" + pfilter + " ORDER BY timestamp",
            args)
    else:
        # longer ranges are averaged per minute to keep responses small
        rows = query(
            "SELECT strftime('%Y-%m-%d %H:%M', timestamp) AS t, parameter, "
            "ROUND(AVG(value), 3) AS value FROM readings "
            "WHERE slave_id=? AND timestamp>=?" + pfilter +
            " GROUP BY t, parameter ORDER BY t", args)

    pivot, params = {}, set()
    for r in rows:
        pivot.setdefault(r['t'], {})[r['parameter']] = r['value']
        params.add(r['parameter'])

    labels = sorted(pivot)
    return jsonify({
        'slave_id':    slave_id,
        'device_name': dev['name'],
        'hours':       hours,
        'labels':      labels,
        'series':      {p: [pivot[t].get(p) for t in labels] for p in sorted(params)},
    })


# ─────────────────────────────────────────────
# Breaker control (queued)
# ─────────────────────────────────────────────
@app.route('/api/control/breaker', methods=['POST'])
def api_control_breaker():
    data     = request.get_json(silent=True) or {}
    operator = get_operator()
    slave_id = data.get('slave_id')
    command  = str(data.get('command', '')).upper()

    dev = DEVICES.get(slave_id)
    if not dev:
        return jsonify({'error': 'unknown slave_id'}), 404
    if command not in ('OPEN', 'CLOSE'):
        return jsonify({'error': "command must be OPEN or CLOSE"}), 400

    st = device_states().get(slave_id, {})
    if (st.get('comms_status') or 'online') == 'offline':
        return jsonify({'error': f"{dev['name']} is offline, command refused"}), 409

    current = st.get('breaker_state') or 'UNKNOWN'
    if command == 'OPEN' and current == 'OPEN':
        return jsonify({'error': 'breaker is already OPEN'}), 409
    if command == 'CLOSE' and current == 'CLOSED':
        return jsonify({'error': 'breaker is already CLOSED'}), 409

    _, cmd_id = execute("""
        INSERT INTO control_commands
        (timestamp, slave_id, device_id, device_name, substation_id,
         command, operator, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING')
    """, (now_ts(), slave_id, dev['id'], dev['name'],
          dev['substation_id'], command, operator))

    log_soe(dev['substation_id'], dev['id'], dev['name'], 'CONTROL_ISSUED',
            'breaker_state', current, command,
            f"{operator} issued {command} on {dev['name']}", operator)

    return jsonify({'status': 'queued', 'command_id': cmd_id,
                    'slave_id': slave_id, 'command': command}), 202


@app.route('/api/control/commands')
def api_control_commands():
    limit = min(request.args.get('limit', 20, type=int), 200)
    return jsonify(query("SELECT * FROM control_commands ORDER BY id DESC LIMIT ?",
                         (limit,)))


@app.route('/api/control/commands/<int:cmd_id>')
def api_control_command(cmd_id):
    rows = query("SELECT * FROM control_commands WHERE id=?", (cmd_id,))
    if not rows:
        return jsonify({'error': 'command not found'}), 404
    return jsonify(rows[0])


if __name__ == '__main__':
    # debug=False: Flask's debugger allows remote code execution.
    # Bound to localhost to match the firewalld rules.
    app.run(host='127.0.0.1', port=8080, debug=False)
