#!/usr/bin/env python3
"""
lib/topology.py - distribution network model for feeder-map substations (SS-01)

Shared by the simulator (physics), client (telemetry), controller (DNP3
points), API (map + interlocks) and FLISR, so they all agree on switch ids,
addresses and what is energised.

Model (typical urban 11 kV cable network)
    Every load node is a ring main unit (RMU) with an IN switch (towards the
    substation) and, except the last node, an OUT switch (to the next node).
    Cable section k of a feeder runs from the OUT switch of node k-1 (the
    feeder breaker for k = 1) to the IN switch of node k.
    Normally-open tie switches (NOPs, from the config "ties") join nodes of
    different feeders so a section can be backfed.

Switch state: 1 = CLOSED, 0 = OPEN. Normal state: IN/OUT closed, ties open.

Addressing (index = position in Topology.switches)
    Modbus  DA RTU slave (config "da_rtu"), holding register address = index
    DNP3    outstation 100 + substation number, point SWITCH_DNP3_BASE + index
"""

from collections import deque

OPEN, CLOSED = 0, 1
SWITCH_DNP3_BASE = 10


class Topology:
    def __init__(self, ss):
        self.ss_id    = ss['id']
        self.dnp3_addr = 100 + int(ss['id'].split('-')[1])
        self.da_slave = ss['da_rtu']['slave_id']
        self.da_name  = ss['da_rtu']['name']

        self.feeders = {}            # slave_id -> feeder dict
        self.by_number = {}          # feeder_number -> slave_id
        for f in ss['feeders']:
            nodes = f.get('load_nodes') or []
            self.feeders[f['slave_id']] = {
                'slave_id': f['slave_id'], 'number': f['feeder_number'],
                'name': f['name'], 'id': f['id'], 'nodes': nodes,
                'total_kw': sum(n['load_kw'] for n in nodes) or 1,
            }
            self.by_number[f['feeder_number']] = f['slave_id']

        self.switches = []
        for f in self.feeders.values():
            n = len(f['nodes'])
            for i, node in enumerate(f['nodes'], start=1):
                self._add(f"F{f['number']}-N{i}-IN", f"{node['name']} IN", 'IN',
                          f['slave_id'], i, CLOSED)
                if i < n:
                    self._add(f"F{f['number']}-N{i}-OUT", f"{node['name']} OUT", 'OUT',
                              f['slave_id'], i, CLOSED)
        for t in ss.get('ties', []):
            a = (self.by_number[t['a']['feeder']], t['a']['node'])
            b = (self.by_number[t['b']['feeder']], t['b']['node'])
            sw = self._add(t['id'], f"{t['id']} tie F{t['a']['feeder']}-F{t['b']['feeder']}",
                           'TIE', None, None, OPEN)
            sw.update(a=a, b=b, via=t.get('via'))
        self.by_id = {s['id']: s for s in self.switches}

    def _add(self, sid, name, kind, feeder, node, normal):
        sw = {'id': sid, 'name': name, 'kind': kind, 'feeder': feeder, 'node': node,
              'normal': normal, 'index': len(self.switches)}
        self.switches.append(sw)
        return sw

    # ── helpers ─────────────────────────────────────────────────────────
    def dnp3_point(self, switch_id):
        return self.dnp3_addr, SWITCH_DNP3_BASE + self.by_id[switch_id]['index']

    def normal_states(self):
        return {s['id']: s['normal'] for s in self.switches}

    def node_kw(self, fsid, i):
        return self.feeders[fsid]['nodes'][i - 1]['load_kw']

    def switch_of(self, fsid, k, end):
        """Switch at one end of section k: end 'up' = OUT of node k-1, 'down' = IN of node k."""
        num = self.feeders[fsid]['number']
        if end == 'up':
            return f"F{num}-N{k - 1}-OUT" if k >= 2 else None    # k = 1: the breaker
        return f"F{num}-N{k}-IN"

    # ── connectivity ────────────────────────────────────────────────────
    def _neighbours(self, v, sw):
        """Vertices: ('start', f), ('sec', f, k), ('node', f, i)."""
        kind, f = v[0], v[1]
        n = len(self.feeders[f]['nodes'])
        out = []
        if kind == 'start':
            if n:
                out.append(('sec', f, 1))
        elif kind == 'sec':
            k = v[2]
            if k == 1:
                out.append(('start', f))
            elif sw.get(self.switch_of(f, k, 'up'), CLOSED):
                out.append(('node', f, k - 1))
            if sw.get(self.switch_of(f, k, 'down'), CLOSED):
                out.append(('node', f, k))
        else:  # node
            i = v[2]
            if sw.get(self.switch_of(f, i, 'down'), CLOSED):
                out.append(('sec', f, i))
            if i < n and sw.get(self.switch_of(f, i + 1, 'up'), CLOSED):
                out.append(('sec', f, i + 1))
            for t in self.switches:
                if t['kind'] == 'TIE' and sw.get(t['id'], OPEN):
                    if t['a'] == (f, i):
                        out.append(('node',) + t['b'])
                    elif t['b'] == (f, i):
                        out.append(('node',) + t['a'])
        return out

    def energise(self, transformer_live, breakers_closed, switches, faults=None):
        """
        breakers_closed: {feeder slave_id: bool}; switches: {switch_id: 0/1};
        faults: {feeder slave_id: faulted section k (0 = none)}.

        Returns {
          'nodes':    {(f, i): source feeder slave_id},       only energised nodes
          'sections': {(f, k): (source, direction)},           +1 normal flow, -1 backfed
          'faulted':  [(source, (f, k))],                      faulted sections that are live
          'parallel': [(source_a, source_b)],                  feeders paralleled through ties
        }
        """
        faults = faults or {}
        owner, sections, faulted, parallel = {}, {}, [], []
        if transformer_live:
            for src in self.feeders:
                if not breakers_closed.get(src):
                    continue
                start = ('start', src)
                owner[start] = src
                q = deque([start])
                while q:
                    v = q.popleft()
                    for w in self._neighbours(v, switches):
                        if w[0] == 'start' and w[1] != src:
                            continue           # never back into another breaker
                        if w in owner:
                            if owner[w] != src and (owner[w], src) not in parallel:
                                parallel.append((owner[w], src))
                            continue
                        owner[w] = src
                        if w[0] == 'sec':
                            # Entered from the upstream side (start or node k-1)?
                            up = v[0] == 'start' or (v[0] == 'node' and v[1] == w[1] and v[2] == w[2] - 1)
                            sections[(w[1], w[2])] = (src, 1 if up else -1)
                            if faults.get(w[1]) == w[2]:
                                faulted.append((src, (w[1], w[2])))
                                continue       # protection: do not pass through a fault
                        q.append(w)
        nodes = {(v[1], v[2]): s for v, s in owner.items() if v[0] == 'node'}
        return {'nodes': nodes, 'sections': sections, 'faulted': faulted, 'parallel': parallel}

    def supplied_kw(self, result):
        """Connected kW supplied through each feeder breaker: {source: kW}."""
        out = {}
        for (f, i), src in result['nodes'].items():
            out[src] = out.get(src, 0) + self.node_kw(f, i)
        return out


def build(config):
    """{substation_id: Topology} for every substation that has a DA RTU."""
    return {ss['id']: Topology(ss) for ss in config['substations'] if ss.get('da_rtu')}


if __name__ == '__main__':
    import json, os
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = json.load(open(os.path.join(base, 'config', 'substations.json')))
    for ss_id, t in build(cfg).items():
        print(ss_id, 'DA RTU slave', t.da_slave, '|', len(t.switches), 'switches, DNP3',
              t.dnp3_addr, 'points', SWITCH_DNP3_BASE, '-', SWITCH_DNP3_BASE + len(t.switches) - 1)
        allb = {f: True for f in t.feeders}
        r = t.energise(True, allb, t.normal_states())
        print('  normal: nodes live', len(r['nodes']), 'faulted', r['faulted'], 'parallel', r['parallel'])
