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
    POST /api/alarms/clear-rtn          ack + clear every returned-to-normal alarm
                                        ?substation=SS-01 limits it to one station
    GET  /api/soe                       Sequence of Events log
    GET  /api/trends                    historian data  ?slave_id=2&hours=1
    GET  /api/live                      latest values   ?points=2:current,31:power_mw
    POST /api/control/breaker           queue a breaker OPEN/CLOSE command
    GET  /api/control/commands          recent control commands
    GET  /api/control/commands/<id>     status of one command
    POST /api/control/switch            queue an RMU / tie switch OPEN/CLOSE (DMS interlocked)
    GET  /api/flisr/<ss_id>             live FLISR / restore-normal proposals + recent plans
    POST /api/flisr/<ss_id>/execute     operator approves {"feeder_slave_id", "kind"}
    POST /api/flisr/<ss_id>/cancel      stop an executing plan {"plan_id"}
    POST /api/flisr/settings            {"auto": true|false}
    POST /api/sim/fault                 TEST SET: inject a feeder fault in the simulator
                                        {"slave_id": 3, "section": 2 (optional)}
    POST /api/sim/repair                TEST SET: crew repaired the fault {"slave_id": 3}
    POST /api/sim/storm                 WEATHER SIM: {"action": "start"|"stop", "intensity": "moderate"|"severe"}
    GET  /api/storm/geo                 Storm Watch map: substations, feeder routes, bounds
    GET  /api/storm                     lightning feed, proximity warnings, trip/strike correlation

Alarm rules (ISA-18.2 style)
    CRITICAL / HIGH  must be ACKNOWLEDGED, then CLEARED by the operator.
                     They never disappear on their own, even after the value
                     returns to normal (they show as "returned to normal").
    MEDIUM / LOW     clear automatically when the condition resolves.
"""

import os
import sys
import json
import socket
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

sys.path.insert(0, os.path.join(BASE_DIR, 'lib'))
import topology  # noqa: E402
import flisr     # noqa: E402
import geo       # noqa: E402

TOPOLOGIES = topology.build(CONFIG)      # feeder-map stations
GEO_NET    = geo.network(CONFIG)         # illustrative geographic routes of those stations

PRIORITY_RANK     = {'CRITICAL': 1, 'HIGH': 2, 'MEDIUM': 3, 'LOW': 4}
QUALITY_BY_STATUS = {'online': 'GOOD', 'frozen': 'SUSPECT', 'offline': 'BAD'}

# Simulator test-set port (fault injection for demonstrations)
TEST_SET_ADDR     = ('127.0.0.1', 20001)

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


def network(t, state, breakers=None, switches=None):
    """Energisation of a feeder-map station, optionally for a proposed state."""
    br = breakers or {f: st == 'CLOSED' for f, st in state['breakers'].items()}
    return t.energise(state['transformer_live'], br, switches or state['switches'], state['faults'])


def interlock(t, breaker=None, switch=None, command='CLOSE'):
    """
    DMS safety check before a CLOSE on a feeder-map station. Returns an error
    message, or None if the operation is safe: it must not energise a faulted
    section, nor parallel two feeders through a tie.
    """
    conn = get_db()
    try:
        state = flisr.load_state(conn, t, CONFIG)
    finally:
        conn.close()
    br = {f: st == 'CLOSED' for f, st in state['breakers'].items()}
    sw = dict(state['switches'])
    if breaker is not None:
        br[breaker] = command == 'CLOSE'
    if switch is not None:
        sw[switch] = topology.CLOSED if command == 'CLOSE' else topology.OPEN
    before = network(t, state)
    after  = network(t, state, br, sw)
    if after['faulted']:
        src, (f, k) = after['faulted'][0]
        return (f"DMS interlock: this would energise the faulted section {k} of "
                f"F{t.feeders[f]['number']}. Isolate it or repair the fault first.")
    new_par = [p for p in after['parallel'] if p not in before['parallel']]
    if new_par:
        a, b = new_par[0]
        return (f"DMS interlock: this would parallel F{t.feeders[a]['number']} and "
                f"F{t.feeders[b]['number']} through a tie. Open the other side first.")
    return None


def get_operator():
    data = request.get_json(silent=True) or {}
    return str(data.get('operator') or 'operator')[:40]


# ─────────────────────────────────────────────
# Live data builders
# ─────────────────────────────────────────────
def latest_values():
    """{slave_id: {parameter: {value, timestamp}}} from the real-time table."""
    rows = query("SELECT slave_id, parameter, value, timestamp FROM current_values")
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


def static_version():
    """Newest modification time of the HMI's CSS/JS, so browsers never run a stale copy."""
    newest = 0
    for root, _, files in os.walk(os.path.join(BASE_DIR, 'static')):
        for name in files:
            newest = max(newest, os.path.getmtime(os.path.join(root, name)))
    return int(newest)


@app.route('/')
def index():
    if os.path.exists(os.path.join(BASE_DIR, 'templates', 'hmi.html')):
        return render_template('hmi.html', v=static_version())
    return render_template_string(PLACEHOLDER)


@app.route('/api/health')
def api_health():
    try:
        last = query("SELECT MAX(timestamp) AS timestamp FROM current_values")
        open_n = query("SELECT COUNT(*) AS n FROM alarms "
                       "WHERE status IN ('ACTIVE','ACKNOWLEDGED')")[0]['n']
        return jsonify({
            'status':             'ok',
            'devices_configured': len(DEVICES),
            'last_reading':       last[0]['timestamp'],
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
    t  = TOPOLOGIES.get(ss_id)
    if not ss or not t:
        return jsonify({'error': f'{ss_id} has no feeder map'}), 404

    conn = get_db()
    try:
        state = flisr.load_state(conn, t, CONFIG)
        comms = {r['slave_id']: r['status'] for r in conn.execute(
            "SELECT slave_id, status FROM device_status")}
    finally:
        conn.close()
    net = network(t, state)

    feeders, nodes, sections = [], [], []
    total_consumers = affected_consumers = total_kw = affected_kw = 0
    for f in ss['feeders']:
        sid, f_nodes = f['slave_id'], f.get('load_nodes', [])
        section = state['faults'].get(sid, 0)
        live_nodes = sum(1 for i in range(1, len(f_nodes) + 1) if (sid, i) in net['nodes'])
        feeders.append({
            'slave_id':      sid,
            'id':            f['id'],
            'name':          f['name'],
            'feeder_number': f['feeder_number'],
            'load_type':     f['load_type'],
            'breaker_state': state['breakers'][sid],
            'energised':     state['transformer_live'] and state['breakers'][sid] == 'CLOSED',
            'comms_status':  comms.get(sid, 'online'),
            'load_mw':       round(state['load_mw'].get(sid, 0), 2),
            'nodes_live':    live_nodes,
            'abnormal':      flisr.abnormal(t, state, sid),
            # Cable section the relay located the fault in (section N feeds
            # load node N); 0 = no fault
            'fault_section': section,
            'fault_between': ([f_nodes[section - 2]['name'] if section > 1 else 'Substation',
                               f_nodes[section - 1]['name']]
                              if 0 < section <= len(f_nodes) else None),
        })
        for seq, n in enumerate(f_nodes, start=1):
            src = net['nodes'].get((sid, seq))
            nodes.append({
                'id': n['id'], 'name': n['name'], 'type': n['type'],
                'consumers': n['consumers'], 'load_kw': n['load_kw'],
                'x': n['x'], 'y': n['y'], 'feeder_slave_id': sid, 'seq': seq,
                'energised': src is not None, 'source': src,
            })
            total_consumers += n['consumers']
            total_kw        += n['load_kw']
            if src is None:
                affected_consumers += n['consumers']
                affected_kw        += n['load_kw']
            sec = net['sections'].get((sid, seq))
            sections.append({'feeder_slave_id': sid, 'section': seq,
                             'energised': sec is not None,
                             'source': sec[0] if sec else None,
                             'direction': sec[1] if sec else 0,
                             'faulted': section == seq})

    switches = []
    for sw in t.switches:
        d = {'id': sw['id'], 'name': sw['name'], 'kind': sw['kind'],
             'state': 'CLOSED' if state['switches'][sw['id']] else 'OPEN',
             'normal': 'CLOSED' if sw['normal'] else 'OPEN',
             'feeder_slave_id': sw['feeder'], 'node': sw['node']}
        if sw['kind'] == 'TIE':
            d.update(a={'feeder_slave_id': sw['a'][0], 'node': sw['a'][1]},
                     b={'feeder_slave_id': sw['b'][0], 'node': sw['b'][1]}, via=sw['via'],
                     energised=sw['a'] in net['nodes'] and bool(state['switches'][sw['id']]))
        switches.append(d)

    return jsonify({
        'substation':   {'id': ss['id'], 'name': ss['name'],
                         'transformer_energised': state['transformer_live'],
                         'da_rtu': t.da_name},
        'feeders':      feeders,
        'load_nodes':   nodes,
        'sections':     sections,
        'switches':     switches,
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


@app.route('/api/alarms/clear-rtn', methods=['POST'])
def api_clear_rtn():
    """
    Bulk housekeeping: acknowledge and clear alarms whose condition has
    already gone away. Anything still in breach (rtn_timestamp NULL) is left
    alone. Each UPDATE re-checks rtn_timestamp, so an alarm the client
    re-opens at the same moment is not cleared by mistake.
    """
    operator = get_operator()
    ss_id    = request.args.get('substation')
    ts, ms   = now_ts(), now_ms()

    sql = ("SELECT * FROM alarms WHERE status IN ('ACTIVE', 'ACKNOWLEDGED') "
           "AND rtn_timestamp IS NOT NULL")
    args = []
    if ss_id:
        sql += " AND substation_id = ?"
        args.append(ss_id)

    conn = get_db()
    cleared = []
    try:
        for a in conn.execute(sql, args).fetchall():
            cur = conn.execute("""
                UPDATE alarms SET status='CLEARED', cleared_timestamp=?,
                       ack_timestamp=COALESCE(ack_timestamp, ?),
                       ack_by=COALESCE(ack_by, ?)
                WHERE id=? AND status IN ('ACTIVE', 'ACKNOWLEDGED')
                  AND rtn_timestamp IS NOT NULL
            """, (ts, ts, operator, a['id']))
            if cur.rowcount:
                cleared.append(a['id'])
                conn.execute("""
                    INSERT INTO soe_log
                    (timestamp_ms, substation_id, device_id, device_name, event_type,
                     parameter, old_value, new_value, message, operator)
                    VALUES (?, ?, ?, ?, 'ALARM_CLEARED', ?, ?, 'CLEARED', ?, ?)
                """, (ms, a['substation_id'], a['device_id'], a['device_name'],
                      a['parameter'], a['status'],
                      f"Alarm #{a['id']} cleared (bulk, returned to normal)", operator))
        conn.commit()
    finally:
        conn.close()

    return jsonify({'status': 'ok', 'cleared': len(cleared), 'alarm_ids': cleared,
                    'cleared_by': operator})


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


@app.route('/api/live')
def api_live():
    """Latest value of each requested point: ?points=2:current,31:power_mw (real-time table)."""
    pts = []
    for item in request.args.get('points', '').split(','):
        sid, _, param = item.partition(':')
        if sid.isdigit() and param:
            pts.append((int(sid), param))
    if not pts or len(pts) > 40:
        return jsonify({'error': 'points: 1-40 entries of slave_id:parameter'}), 400
    rows = query("SELECT slave_id, parameter, value, timestamp FROM current_values WHERE "
                 + " OR ".join(["(slave_id=? AND parameter=?)"] * len(pts)),
                 [v for p in pts for v in p])
    return jsonify(rows)


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

    t = TOPOLOGIES.get(dev['substation_id'])
    if t and command == 'CLOSE' and slave_id in t.feeders:
        why = interlock(t, breaker=slave_id)
        if why:
            return jsonify({'error': why}), 409

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


# ─────────────────────────────────────────────
# Test set (simulation only)
# ─────────────────────────────────────────────
@app.route('/api/sim/fault', methods=['POST'])
def api_sim_fault():
    """
    Ask the simulator's test set to put a fault on a feeder. This is NOT an
    operator control: in the field it is the event the protection reacts to.
    Logged in the SOE as TEST_FAULT_INJECTED so the audit trail stays honest.
    """
    data     = request.get_json(silent=True) or {}
    operator = get_operator()
    slave_id = data.get('slave_id')
    section  = data.get('section')
    dev = DEVICES.get(slave_id)
    if not dev or dev['type'] != 'feeder':
        return jsonify({'error': 'slave_id must be a feeder'}), 404

    req = {'cmd': 'trip', 'slave_id': slave_id}
    if section is not None:
        req['section'] = section
    try:
        with socket.create_connection(TEST_SET_ADDR, timeout=5) as sock:
            sock.sendall((json.dumps(req) + '\n').encode())
            reply = json.loads(sock.makefile().readline())
    except (OSError, ValueError) as e:
        return jsonify({'error': f'simulator test set not reachable: {e}'}), 503

    if not reply.get('ok'):
        return jsonify({'error': reply.get('message', 'refused')}), 409
    log_soe(dev['substation_id'], dev['id'], dev['name'], 'TEST_FAULT_INJECTED',
            'fault_section', '', section or 'random',
            f"Test set: fault injected on {dev['name']} ({reply['message']})", operator)
    return jsonify({'status': 'ok', 'message': reply['message']})


@app.route('/api/control/switch', methods=['POST'])
def api_control_switch():
    """Queue an RMU or tie switch OPEN/CLOSE (DNP3 point on the station outstation)."""
    data      = request.get_json(silent=True) or {}
    operator  = get_operator()
    switch_id = str(data.get('switch_id', ''))
    command   = str(data.get('command', '')).upper()
    t = next((t for t in TOPOLOGIES.values() if switch_id in t.by_id), None)
    if not t:
        return jsonify({'error': 'unknown switch'}), 404
    if command not in ('OPEN', 'CLOSE'):
        return jsonify({'error': "command must be OPEN or CLOSE"}), 400
    sw = t.by_id[switch_id]

    conn = get_db()
    try:
        state = flisr.load_state(conn, t, CONFIG)
    finally:
        conn.close()
    current = 'CLOSED' if state['switches'][switch_id] else 'OPEN'
    if command == ('OPEN' if current == 'OPEN' else 'CLOSE'):
        return jsonify({'error': f'switch is already {current}'}), 409
    if command == 'CLOSE':
        why = interlock(t, switch=switch_id)
        if why:
            return jsonify({'error': why}), 409

    _, cmd_id = execute("""
        INSERT INTO control_commands
        (timestamp, slave_id, device_id, device_name, substation_id, command, operator, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING')
    """, (now_ts(), t.da_slave, switch_id, sw['name'], t.ss_id, command, operator))
    log_soe(t.ss_id, switch_id, sw['name'], 'CONTROL_ISSUED', 'switch_state', current, command,
            f"{operator} issued {command} on {sw['name']}", operator)
    return jsonify({'status': 'queued', 'command_id': cmd_id, 'switch_id': switch_id,
                    'command': command}), 202


# ─────────────────────────────────────────────
# FLISR
# ─────────────────────────────────────────────
def plan_rows(ss_id, limit=6):
    rows = query("SELECT * FROM switching_plans WHERE substation_id=? ORDER BY id DESC LIMIT ?",
                 (ss_id, limit))
    for r in rows:
        r['steps'] = json.loads(r['steps'] or '[]')
        r['summary'] = json.loads(r['summary'] or '{}')
    return rows


@app.route('/api/flisr/<ss_id>')
def api_flisr(ss_id):
    """Live FLISR proposals (computed now) and recent plans."""
    t = TOPOLOGIES.get(ss_id)
    if not t:
        return jsonify({'error': f'{ss_id} has no FLISR network'}), 404
    conn = get_db()
    try:
        state = flisr.load_state(conn, t, CONFIG)
        auto = conn.execute("SELECT value FROM settings WHERE key='flisr_auto'").fetchone()
    finally:
        conn.close()

    proposals = []
    for fsid in t.feeders:
        if state['faults'][fsid] and state['breakers'][fsid] != 'CLOSED':
            proposals.append(flisr.plan_flisr(t, state, fsid))
        elif not state['faults'][fsid] and (flisr.abnormal(t, state, fsid, ties=False)
                                            or state['breakers'][fsid] == 'TRIPPED'):
            proposals.append(flisr.plan_restore(t, state, fsid))
    return jsonify({'auto': bool(auto and auto['value'] == '1'),
                    'proposals': proposals, 'plans': plan_rows(ss_id)})


@app.route('/api/flisr/<ss_id>/execute', methods=['POST'])
def api_flisr_execute(ss_id):
    """Operator approves a plan. It is recomputed here from the live state."""
    t = TOPOLOGIES.get(ss_id)
    if not t:
        return jsonify({'error': f'{ss_id} has no FLISR network'}), 404
    data = request.get_json(silent=True) or {}
    operator = get_operator()
    fsid, kind = data.get('feeder_slave_id'), data.get('kind', 'FLISR')
    if fsid not in t.feeders or kind not in ('FLISR', 'RESTORE'):
        return jsonify({'error': 'feeder_slave_id and kind FLISR|RESTORE required'}), 400

    conn = get_db()
    try:
        if conn.execute("SELECT 1 FROM switching_plans WHERE status='EXECUTING' AND "
                        "substation_id=?", (ss_id,)).fetchone():
            return jsonify({'error': 'another switching plan is executing'}), 409
        state = flisr.load_state(conn, t, CONFIG)
        plan = (flisr.plan_flisr if kind == 'FLISR' else flisr.plan_restore)(t, state, fsid)
        if not plan['feasible']:
            return jsonify({'error': ' '.join(plan['notes']) or 'nothing to do'}), 409
        pid = flisr.create_plan(conn, t, plan, 'MANUAL', operator)
    finally:
        conn.close()
    return jsonify({'status': 'executing', 'plan_id': pid, 'steps': len(plan['steps'])}), 202


@app.route('/api/flisr/<ss_id>/cancel', methods=['POST'])
def api_flisr_cancel(ss_id):
    data = request.get_json(silent=True) or {}
    operator = get_operator()
    n, _ = execute("UPDATE switching_plans SET status='CANCELLED', finished=?, result=? "
                   "WHERE id=? AND substation_id=? AND status='EXECUTING'",
                   (now_ts(), f'cancelled by {operator} (switches stay where they are)',
                    data.get('plan_id'), ss_id))
    if not n:
        return jsonify({'error': 'no executing plan with that id'}), 409
    return jsonify({'status': 'cancelled'})


@app.route('/api/flisr/settings', methods=['POST'])
def api_flisr_settings():
    data = request.get_json(silent=True) or {}
    operator = get_operator()
    auto = bool(data.get('auto'))
    execute("INSERT INTO settings (key, value) VALUES ('flisr_auto', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", ('1' if auto else '0',))
    for t in TOPOLOGIES.values():
        log_soe(t.ss_id, 'FLISR', 'FLISR', 'FLISR_MODE', 'flisr_auto', '', 'AUTO' if auto else 'MANUAL',
                f"FLISR set to {'automatic' if auto else 'manual (operator approval)'} by {operator}",
                operator)
    return jsonify({'status': 'ok', 'auto': auto})


@app.route('/api/sim/repair', methods=['POST'])
def api_sim_repair():
    """TEST SET: the crew has repaired the faulted cable (fault indication resets)."""
    data     = request.get_json(silent=True) or {}
    operator = get_operator()
    dev = DEVICES.get(data.get('slave_id'))
    if not dev or dev['type'] != 'feeder':
        return jsonify({'error': 'slave_id must be a feeder'}), 404
    try:
        with socket.create_connection(TEST_SET_ADDR, timeout=5) as sock:
            sock.sendall((json.dumps({'cmd': 'repair', 'slave_id': dev['slave_id']}) + '\n').encode())
            reply = json.loads(sock.makefile().readline())
    except (OSError, ValueError) as e:
        return jsonify({'error': f'simulator test set not reachable: {e}'}), 503
    if not reply.get('ok'):
        return jsonify({'error': reply.get('message', 'refused')}), 409
    log_soe(dev['substation_id'], dev['id'], dev['name'], 'TEST_FAULT_REPAIRED', 'fault_section',
            '', 0, f"Test set: crew repaired the fault on {dev['name']}", operator)
    return jsonify({'status': 'ok', 'message': reply['message']})


# ─────────────────────────────────────────────
# Storm Watch
# ─────────────────────────────────────────────
CORR_BEFORE_S = 12      # a stroke may precede the trip we saw by up to this (poll delay)
CORR_AFTER_S  = 1
CORR_KM       = 3.0
WARN_KM       = (3.0, 8.0)    # RED inside 3 km, AMBER inside 8 km
WARN_SECONDS  = 300


def test_set(req):
    """One request to the simulator's test-set port. Raises OSError/ValueError if unreachable."""
    with socket.create_connection(TEST_SET_ADDR, timeout=5) as sock:
        sock.sendall((json.dumps(req) + '\n').encode())
        return json.loads(sock.makefile().readline())


def feeder_distance_km(dev, p):
    """Distance from point p to a feeder: its cable route if known, else its substation."""
    net = GEO_NET.get(dev['substation_id'])
    if net and dev['slave_id'] in net['feeders']:
        pts = net['feeders'][dev['slave_id']]
        return min(geo.dist_to_segment_km(p, pts[k - 1], pts[k]) for k in range(1, len(pts)))
    ss = SUBSTATIONS[dev['substation_id']]
    return geo.dist_km(p, (ss['latitude'], ss['longitude']))


def correlate_trips(strikes):
    """
    Match recent breaker trips with cloud-to-ground strokes, by time and
    distance only (the simulator's own cause record is not used): a stroke
    up to CORR_BEFORE_S before the trip was seen, within CORR_KM of the feeder.
    """
    since = (datetime.now() - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
    by_id = {d['id']: d for d in DEVICES.values()}
    cg = [x for x in strikes if x['type'] == 'CG']
    # What happened after each trip: when the breaker closed again, and whether
    # a command closed it (operator / FLISR) or the auto-recloser did
    closes = {}
    for r in query("SELECT timestamp_ms, device_id FROM soe_log WHERE event_type='BREAKER_CLOSED' "
                   "AND timestamp_ms >= ? ORDER BY id", (since,)):
        closes.setdefault(r['device_id'], []).append(r['timestamp_ms'])
    commands = {}
    for r in query("SELECT executed_timestamp, device_id, operator FROM control_commands WHERE "
                   "command='CLOSE' AND status='EXECUTED' AND executed_timestamp >= ?", (since,)):
        commands.setdefault(r['device_id'], []).append((r['executed_timestamp'], r['operator']))
    states = device_states()
    out = []
    for ev in query("SELECT timestamp_ms, device_id, device_name, substation_id FROM soe_log "
                    "WHERE event_type='BREAKER_TRIPPED' AND timestamp_ms >= ? ORDER BY id DESC LIMIT 30",
                    (since,)):
        dev = by_id.get(ev['device_id'])
        if not dev:
            continue
        t_trip = datetime.strptime(ev['timestamp_ms'], "%Y-%m-%d %H:%M:%S.%f").timestamp() * 1000
        best = None
        for x in cg:
            dt = (t_trip - x['t']) / 1000
            if -CORR_AFTER_S <= dt <= CORR_BEFORE_S:
                d = feeder_distance_km(dev, (x['lat'], x['lon']))
                if d <= CORR_KM and (best is None or d < best[0]):
                    best = (d, dt, x)
        row = {'time': ev['timestamp_ms'], 't': int(t_trip), 'device_name': ev['device_name'],
               'slave_id': dev['slave_id'], 'substation_id': ev['substation_id'], 'strike': None}
        closed_at = next((c for c in closes.get(ev['device_id'], []) if c > ev['timestamp_ms']), None)
        if closed_at:
            secs = (datetime.strptime(closed_at, "%Y-%m-%d %H:%M:%S.%f").timestamp() * 1000 - t_trip) / 1000
            by = next((op for ts, op in commands.get(ev['device_id'], [])
                       if ev['timestamp_ms'][:19] <= ts <= closed_at[:19]), None)
            row['outcome'] = {'state': 'CLOSED_BY_COMMAND' if by else 'AUTO_RECLOSED',
                              'after_s': round(secs, 1), 'by': by}
        else:
            row['outcome'] = {'state': 'STILL_' + (states.get(dev['slave_id'], {}).get('breaker_state') or 'UNKNOWN')}
        if best:
            row['strike'] = {'id': best[2]['id'], 'lat': best[2]['lat'], 'lon': best[2]['lon'],
                             'ka': best[2]['ka'], 'distance_km': round(best[0], 2),
                             'seconds_before': round(best[1], 1)}
        out.append(row)
    return out


@app.route('/api/storm/geo')
def api_storm_geo():
    subs = [{'id': ss['id'], 'short_name': ss['short_name'], 'name': ss['name'],
             'lat': ss['latitude'], 'lon': ss['longitude']} for ss in CONFIG['substations']]
    feeders, ties = [], []
    for ss_id, net in GEO_NET.items():
        for sid, pts in net['feeders'].items():
            feeders.append({'substation_id': ss_id, 'slave_id': sid,
                            'feeder_number': DEVICES[sid]['feeder_number'], 'points': pts})
        ties += [{'id': tid, 'a': a, 'b': b} for tid, a, b in net['ties']]
    return jsonify({'substations': subs, 'feeders': feeders, 'ties': ties,
                    'note': 'Substations are at their configured coordinates. Feeder routes are '
                            'illustrative, laid out around the substation from the schematic.'})


@app.route('/api/storm')
def api_storm():
    since = request.args.get('since', 0, type=int)
    try:
        reply = test_set({'cmd': 'storm_state', 'since': 0})
    except (OSError, ValueError) as e:
        return jsonify({'error': f'simulator not reachable: {e}'}), 503
    st = reply['storm']
    strikes, now = st['strikes'], st['now']

    recent = [x for x in strikes if x['type'] == 'CG' and now - x['t'] <= WARN_SECONDS * 1000]
    warnings = []
    for ss in CONFIG['substations']:
        pos = (ss['latitude'], ss['longitude'])
        ds = sorted(geo.dist_km(pos, (x['lat'], x['lon'])) for x in recent)
        level = 'CLEAR' if not ds or ds[0] > WARN_KM[1] else 'RED' if ds[0] <= WARN_KM[0] else 'AMBER'
        warnings.append({'substation_id': ss['id'], 'short_name': ss['short_name'], 'level': level,
                         'nearest_km': round(ds[0], 1) if ds else None,
                         'within_8km': sum(1 for d in ds if d <= WARN_KM[1])})

    minute = [x for x in strikes if now - x['t'] <= 60000]
    st['last_id'] = max((x['id'] for x in strikes), default=0)
    st['strikes'] = [x for x in strikes if x['id'] > since]
    st.update(warnings=warnings, correlations=correlate_trips(strikes), stats={
        'per_min': len(minute), 'cg_per_min': sum(1 for x in minute if x['type'] == 'CG'),
        'max_ka': max((abs(x['ka']) for x in recent), default=0), 'total': len(strikes)})
    return jsonify(st)


@app.route('/api/sim/storm', methods=['POST'])
def api_sim_storm():
    """WEATHER SIM: start or stop the simulated thunderstorm."""
    data     = request.get_json(silent=True) or {}
    operator = get_operator()
    action   = data.get('action')
    if action not in ('start', 'stop'):
        return jsonify({'error': 'action must be start or stop'}), 400
    try:
        reply = test_set({'cmd': 'storm', 'action': action, 'intensity': data.get('intensity', 'moderate')})
    except (OSError, ValueError) as e:
        return jsonify({'error': f'simulator not reachable: {e}'}), 503
    if not reply.get('ok'):
        return jsonify({'error': reply.get('message', 'refused')}), 409
    ss = CONFIG['substations'][0]
    log_soe(ss['id'], 'WEATHER', 'Weather simulation', f"SIM_STORM_{'STARTED' if action == 'start' else 'STOPPED'}",
            'storm', '', data.get('intensity', '') if action == 'start' else '',
            f"Weather simulation: {reply['message']}", operator)
    return jsonify({'status': 'ok', 'message': reply['message']})


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
