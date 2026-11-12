#!/usr/bin/env python3
"""
lib/flisr.py - Fault Location, Isolation and Service Restoration

Pure planning logic plus a loader that reads the live state from scada.db.
Used by the API (plan preview, operator approval) and the DNP3 controller
(automatic mode). Plans are always computed from the live state on the
server, never taken from the browser.

FLISR plan for a tripped feeder with fault section k (from register 40007)
    1. Isolate   open the switches at both ends of section k
    2. Upstream  reclose the feeder breaker (nodes 1..k-1), unless k = 1
    3. Downstream close the normally-open tie that can carry nodes k..n,
                 checked against the supporting feeder's capacity
    The final state is simulated on the topology: a plan that would energise
    a faulted section or parallel two feeders is rejected.

Capacity check
    transfer = faulted feeder's pre-fault MW (historian average of the minute
    before the fault) x share of connected kW being transferred.
    Allowed if supporting feeder MW now + transfer <= TRANSFER_LIMIT x its
    HIGH_HIGH (emergency) power limit.

Restore-normal plan (after the crew has repaired the fault)
    open closed ties touching the feeder (break before make), close its
    section switches from the substation outwards, then its breaker.
"""

from datetime import datetime, timedelta

from topology import OPEN, CLOSED

TRANSFER_LIMIT = 0.90
FMT = "%Y-%m-%d %H:%M:%S"


# ─────────────────────────────────────────────
# State from the database
# ─────────────────────────────────────────────
def load_state(conn, topo, config):
    ss = next(s for s in config['substations'] if s['id'] == topo.ss_id)
    tr_slave = ss['transformer']['slave_id']
    states = {r[0]: r[1] for r in conn.execute("SELECT slave_id, breaker_state FROM devices")}
    cv = {}
    for sid, param, value in conn.execute("SELECT slave_id, parameter, value FROM current_values"):
        cv[(sid, param)] = value

    switches = topo.normal_states()
    for s in topo.switches:
        v = cv.get((topo.da_slave, s['id']))
        if v is not None:
            switches[s['id']] = int(v)

    limits, faults, load, prefault = {}, {}, {}, {}
    for f in ss['feeders']:
        sid = f['slave_id']
        lim = f['analog_limits']['power_mw']
        limits[sid] = (lim['high'], lim['high_high'])
        faults[sid] = int(cv.get((sid, 'fault_section'), 0) or 0)
        load[sid] = cv.get((sid, 'power_mw'), 0.0) or 0.0
        if faults[sid]:
            prefault[sid] = _prefault_mw(conn, f)

    return {
        'transformer_live': states.get(tr_slave, 'CLOSED') == 'CLOSED',
        'breakers': {f['slave_id']: states.get(f['slave_id'], 'CLOSED') for f in ss['feeders']},
        'switches': switches, 'faults': faults, 'load_mw': load,
        'prefault_mw': prefault, 'limits': limits,
    }


def _prefault_mw(conn, feeder):
    """Average feeder MW over the minute before its latest FAULT_LOCATED event."""
    row = conn.execute("SELECT timestamp_ms FROM soe_log WHERE device_id=? AND "
                       "event_type='FAULT_LOCATED' ORDER BY id DESC LIMIT 1",
                       (feeder['id'],)).fetchone()
    if row:
        t = datetime.strptime(row[0][:19], FMT)
        avg = conn.execute(
            "SELECT AVG(value) FROM readings WHERE slave_id=? AND parameter='power_mw' "
            "AND value > 0 AND timestamp BETWEEN ? AND ?",
            (feeder['slave_id'], (t - timedelta(seconds=65)).strftime(FMT),
             (t - timedelta(seconds=4)).strftime(FMT))).fetchone()[0]
        if avg:
            return round(avg, 3)
    # No history: assume the feeder's normal peak (conservative)
    return feeder['normal_operating']['power_mw']['max']


# ─────────────────────────────────────────────
# Planning
# ─────────────────────────────────────────────
def _closed(state):
    return {f: st == 'CLOSED' for f, st in state['breakers'].items()}


def _apply(state, steps):
    """Breaker/switch states after the steps, without touching the input."""
    br, sw = _closed(state), dict(state['switches'])
    for s in steps:
        if s['kind'] == 'breaker':
            br[s['id']] = s['action'] == 'CLOSE'
        else:
            sw[s['id']] = CLOSED if s['action'] == 'CLOSE' else OPEN
    return br, sw


def _step(kind, target, action, label, why):
    return {'kind': kind, 'id': target, 'action': action, 'label': label, 'why': why}


def _nodes(a, b):
    return f"node {a}" if a == b else f"nodes {a}-{b}"


def _consumers(topo, nodes):
    return sum(topo.feeders[f]['nodes'][i - 1]['consumers'] for f, i in nodes)


def _kw(topo, nodes):
    return sum(topo.node_kw(f, i) for f, i in nodes)


def plan_flisr(topo, state, fsid):
    f = topo.feeders[fsid]
    k, n = state['faults'].get(fsid, 0), len(f['nodes'])
    name = f"F{f['number']}"
    plan = {'kind': 'FLISR', 'feeder': fsid, 'feeder_name': f['name'], 'fault_section': k,
            'steps': [], 'notes': [], 'feasible': False}
    if not k:
        plan['notes'].append('No fault located on this feeder.')
        return plan
    if state['breakers'][fsid] == 'CLOSED':
        plan['notes'].append('Feeder breaker is closed: nothing to restore.')
        return plan
    if not state['transformer_live']:
        plan['notes'].append('Station transformer is not energised: no source to restore from.')
        return plan

    steps, sw = [], state['switches']
    up, down = topo.switch_of(fsid, k, 'up'), topo.switch_of(fsid, k, 'down')

    # 1. Isolation
    if up and sw[up] == CLOSED:
        steps.append(_step('switch', up, 'OPEN', f"Open {up} ({topo.by_id[up]['name']})", 'isolate'))
    if sw[down] == CLOSED:
        steps.append(_step('switch', down, 'OPEN', f"Open {down} ({topo.by_id[down]['name']})", 'isolate'))

    # 2. Upstream restoration through the feeder's own breaker
    upstream = [(fsid, i) for i in range(1, k)]
    if upstream:
        steps.append(_step('breaker', fsid, 'CLOSE', f"Close {name} breaker", 'restore upstream'))
    else:
        plan['notes'].append(f'Fault is in section 1, next to the breaker: {name} breaker stays open.')

    # 3. Downstream restoration through a tie. Try the whole island beyond
    #    the fault first; if no tie can carry it, split it and restore the
    #    largest tail part that fits (partial restoration).
    br, sw_after = _apply(state, steps)
    pre = topo.energise(state['transformer_live'], br, sw_after, state['faults'])
    island_full = [(fsid, i) for i in range(k, n + 1)]
    chosen, first = None, None
    for j in range(k, n + 1):
        island = [(fsid, i) for i in range(j, n + 1)]
        transfer_kw = _kw(topo, island)
        transfer_mw = round(state['prefault_mw'].get(fsid, 0) * transfer_kw / f['total_kw'], 3)
        candidates = []
        for t in topo.switches:
            if t['kind'] != 'TIE' or sw_after[t['id']] == CLOSED:
                continue
            for mine, other in ((t['a'], t['b']), (t['b'], t['a'])):
                if mine in island and other not in island_full and other in pre['nodes']:
                    src = pre['nodes'][other]
                    now = state['load_mw'].get(src, 0)
                    cap = round(TRANSFER_LIMIT * state['limits'][src][1], 3)
                    candidates.append({'tie': t['id'], 'source': src, 'now_mw': round(now, 3),
                                       'after_mw': round(now + transfer_mw, 3), 'limit_mw': cap,
                                       'ok': now + transfer_mw <= cap,
                                       'source_name': f"F{topo.feeders[src]['number']}"})
        candidates.sort(key=lambda c: c['limit_mw'] - c['after_mw'], reverse=True)
        if first is None:
            first = {'mw': transfer_mw, 'kw_connected': transfer_kw,
                     'prefault_mw': state['prefault_mw'].get(fsid), 'candidates': candidates}
        ok = next((c for c in candidates if c['ok']), None)
        if ok:
            chosen = (j, ok, transfer_mw)
            break
    plan['transfer'] = first

    if chosen:
        j, c, mw = chosen
        if j > k:
            split = topo.switch_of(fsid, j, 'down')
            if sw_after[split] == CLOSED:
                steps.append(_step('switch', split, 'OPEN',
                                   f"Open {split} ({topo.by_id[split]['name']}) to split the island",
                                   'partial restore'))
            plan['notes'].append(f"Partial restoration: {c['source_name']} can carry "
                                 f"{_nodes(j, n)} only; {_nodes(k, j - 1)} {'stays' if k == j - 1 else 'stay'} off supply.")
        steps.append(_step('switch', c['tie'], 'CLOSE',
                           f"Close {c['tie']} (backfeed {mw} MW from {c['source_name']})",
                           'restore downstream'))
    elif island_full:
        plan['notes'].append('No tie with enough capacity: nodes beyond the fault stay off supply.'
                             if first and first['candidates'] else
                             'No usable tie for the nodes beyond the fault.')

    # Safety: simulate the final state
    br, sw_final = _apply(state, steps)
    final = topo.energise(state['transformer_live'], br, sw_final, state['faults'])
    if final['faulted'] or final['parallel']:
        plan['notes'].append('Rejected: final state would energise a fault or parallel feeders.')
        return plan

    restored = [nd for nd in upstream + island_full if nd in final['nodes']]
    off = [(fsid, i) for i in range(1, n + 1) if (fsid, i) not in final['nodes']]
    plan.update(steps=steps, feasible=bool(steps), restored={
        'consumers': _consumers(topo, restored), 'kw': _kw(topo, restored), 'nodes': len(restored)},
        still_off={'consumers': _consumers(topo, off), 'kw': _kw(topo, off), 'nodes': len(off)})
    return plan


def plan_restore(topo, state, fsid):
    """Return the feeder to its normal configuration once the fault is repaired."""
    f = topo.feeders[fsid]
    name = f"F{f['number']}"
    plan = {'kind': 'RESTORE', 'feeder': fsid, 'feeder_name': f['name'], 'steps': [],
            'notes': [], 'feasible': False}
    if state['faults'].get(fsid):
        plan['notes'].append('Fault not repaired yet: closing back would energise it.')
        return plan

    steps, sw = [], state['switches']
    mine = {(fsid, i) for i in range(1, len(f['nodes']) + 1)}
    for t in topo.switches:                      # break before make
        if t['kind'] == 'TIE' and sw[t['id']] == CLOSED and (t['a'] in mine or t['b'] in mine):
            steps.append(_step('switch', t['id'], 'OPEN', f"Open {t['id']} (end backfeed)", 'open tie'))
    for k in range(1, len(f['nodes']) + 1):     # from the substation outwards
        for end in ('up', 'down'):
            s = topo.switch_of(fsid, k, end)
            if s and sw[s] == OPEN:
                steps.append(_step('switch', s, 'CLOSE', f"Close {s} ({topo.by_id[s]['name']})",
                                   'normal configuration'))
    if state['breakers'][fsid] != 'CLOSED':
        steps.append(_step('breaker', fsid, 'CLOSE', f"Close {name} breaker", 'normal configuration'))
    if not steps:
        plan['notes'].append('Feeder is already in its normal configuration.')
        return plan

    br, sw_final = _apply(state, steps)
    final = topo.energise(state['transformer_live'], br, sw_final, state['faults'])
    if final['faulted'] or final['parallel']:
        plan['notes'].append('Rejected: final state would energise a fault or parallel feeders.')
        return plan
    plan.update(steps=steps, feasible=True)
    return plan


def abnormal(topo, state, fsid, ties=True):
    """
    True if a switch of the feeder is out of its normal state. ties=False
    ignores ties: a feeder that is only backfeeding a neighbour through a tie
    must not get its own restore plan, which would drop the backfed nodes.
    """
    mine = {(fsid, i) for i in range(1, len(topo.feeders[fsid]['nodes']) + 1)}
    for s in topo.switches:
        touches = s['feeder'] == fsid or (ties and s['kind'] == 'TIE' and (s['a'] in mine or s['b'] in mine))
        if touches and state['switches'][s['id']] != s['normal']:
            return True
    return False


# ─────────────────────────────────────────────
# Recording a plan for execution
# ─────────────────────────────────────────────
def create_plan(conn, topo, plan, mode, operator):
    """
    Store a plan for the control layer to execute (status EXECUTING), or as
    REJECTED if it has nothing safe to do. Logs the start in the SOE.
    Returns the plan id.
    """
    import json
    now = datetime.now().strftime(FMT)
    status = 'EXECUTING' if plan['feasible'] else 'REJECTED'
    summary = {k: plan.get(k) for k in ('feeder_name', 'fault_section', 'restored',
                                        'still_off', 'transfer', 'notes')}
    cur = conn.execute("""
        INSERT INTO switching_plans (created, substation_id, feeder_slave_id, kind, mode,
                                     operator, steps, summary, status, finished)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (now, topo.ss_id, plan['feeder'], plan['kind'], mode, operator,
          json.dumps(plan['steps']), json.dumps(summary), status,
          now if status == 'REJECTED' else None))
    pid = cur.lastrowid
    f = topo.feeders[plan['feeder']]
    what = 'FLISR' if plan['kind'] == 'FLISR' else 'Restore-normal'
    msg = (f"{what} plan #{pid} ({mode.lower()}) started: {len(plan['steps'])} steps"
           if status == 'EXECUTING' else
           f"{what} plan #{pid} rejected: {' '.join(plan['notes']) or 'nothing to do'}")
    conn.execute("""
        INSERT INTO soe_log (timestamp_ms, substation_id, device_id, device_name, event_type,
                             parameter, old_value, new_value, message, operator)
        VALUES (?, ?, ?, ?, ?, 'switching_plan', '', ?, ?, ?)
    """, (datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], topo.ss_id, f['id'], f['name'],
          f"{plan['kind']}_{'STARTED' if status == 'EXECUTING' else 'REJECTED'}",
          status, msg, operator))
    conn.commit()
    return pid
