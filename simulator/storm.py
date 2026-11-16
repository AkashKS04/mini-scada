#!/usr/bin/env python3
"""
simulator/storm.py - thunderstorm and lightning model for Storm Watch

Part of the simulated "field": storm cells drift over the city and produce
lightning. A lightning location system (LLS) would report each stroke with
position, time, type and peak current; state() is that feed.

Model
    Cells      spawn upwind of a target substation (the feeder-map station 60 %
               of the time, so FLISR gets exercised), move on the storm heading,
               grow, mature and decay. Motion runs TIME_LAPSE x faster than a
               real storm (30 km/h) so one crosses the city in minutes.
    Strokes    Poisson per cell. About 40 % are cloud-to-ground (CG), the rest
               in-cloud (IC). CG: ~88 % negative (-8..-70 kA), else positive
               (+20..+130 kA). Only CG strokes can affect the network.
    Effects    CG stroke within 0.4 km of a feeder-map cable section: fault on
               that section (permanent, or transient with auto-reclose).
               CG stroke within 2.5 km of another substation: transient trip of
               one of its feeders. Faults are rate-limited so a storm stays
               demonstrable instead of tripping everything.

The effect recorded on a stroke is simulation ground truth. The SCADA side
does not use it: the API correlates trips with strokes by time and distance,
the way a utility does with LLS data.
"""

import math
import random
import threading
import time
from collections import deque

import geo

TIME_LAPSE   = 8
REAL_KMH     = 30
MAX_SECONDS  = 20 * 60
KEEP_SECONDS = 15 * 60
SPAWN_KM     = (13, 17)
LEAVE_KM     = 30

INTENSITY = {
    #            cells  strokes/min   seconds between strike-caused faults
    'moderate': {'cells': 2, 'rate': (8, 14),  'fault_gap': 50},
    'severe':   {'cells': 3, 'rate': (18, 30), 'fault_gap': 30},
}


def _poisson(lam):
    if lam <= 0:
        return 0
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= random.random()
        if p <= limit:
            return k
        k += 1


class Storm:
    def __init__(self, config, trip_permanent, trip_transient, log=print):
        self.net = geo.network(config)
        self.trip_permanent, self.trip_transient, self.log = trip_permanent, trip_transient, log
        self.stations = [{'id': ss['id'], 'pos': (ss['latitude'], ss['longitude']),
                          'feeders': [f['slave_id'] for f in ss['feeders']],
                          'has_net': ss['id'] in self.net} for ss in config['substations']]
        for st in self.stations:
            # A storm aimed at a feeder-map station should cross its cables,
            # which run away from the substation: aim at the network's centre
            pts = [p for route in self.net.get(st['id'], {}).get('feeders', {}).values() for p in route]
            st['aim'] = ((sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
                         if pts else st['pos'])
        self.center = (sum(s['pos'][0] for s in self.stations) / len(self.stations),
                       sum(s['pos'][1] for s in self.stations) / len(self.stations))
        self.lock = threading.Lock()
        self.active, self.intensity, self.started = False, 'moderate', 0.0
        self.heading = 0.0
        self.target = self.center
        self.cells, self.strikes = [], deque()
        self.next_cell = self.next_strike = 1
        self.last_fault = 0.0

    # ── control ─────────────────────────────────────────────────────────
    def start(self, intensity='moderate'):
        if intensity not in INTENSITY:
            return False, "intensity must be moderate or severe"
        with self.lock:
            self.active, self.intensity, self.started = True, intensity, time.time()
            self.heading = random.uniform(0, 360)          # direction the storm moves towards
            netted = [s for s in self.stations if s['has_net']]
            self.target = (random.choice(netted) if netted and random.random() < 0.6
                           else random.choice(self.stations))['aim']
            self.cells = []
            for _ in range(INTENSITY[intensity]['cells']):
                self._spawn()
        self.log(f"\n[STORM] {intensity} storm started, heading {self.heading:.0f} deg\n")
        return True, f"{intensity} storm started"

    def stop(self):
        with self.lock:
            was = self.active
            self.active, self.cells = False, []
        if was:
            self.log("\n[STORM] storm ended\n")
        return True, "storm stopped" if was else "no storm active"

    # ── model ───────────────────────────────────────────────────────────
    def _spawn(self):
        klat, klon = geo.km_per_deg(self.center[0])
        back = math.radians(self.heading + 180 + random.uniform(-14, 14))   # upwind of the target
        r = random.uniform(*SPAWN_KM)
        lo, hi = INTENSITY[self.intensity]['rate']
        self.cells.append({
            'id': self.next_cell,
            'lat': self.target[0] + r * math.cos(back) / klat,
            'lon': self.target[1] + r * math.sin(back) / klon,
            'heading': self.heading + random.uniform(-6, 6),
            'speed_kmh': REAL_KMH * random.uniform(0.8, 1.25),
            'radius_km': random.uniform(3.0, 5.5),
            'peak': random.uniform(lo, hi), 'age': 0.0, 'life': random.uniform(300, 480),
        })
        self.next_cell += 1

    @staticmethod
    def _profile(x):
        """Activity over a cell's life: grows for 25 %, mature, decays over the last 30 %."""
        if x < 0.25:
            return x / 0.25
        if x > 0.70:
            return max(0.0, (1 - x) / 0.30)
        return 1.0

    def step(self, dt):
        if not self.active:
            return
        if time.time() - self.started > MAX_SECONDS:
            self.stop()
            return
        new = []
        with self.lock:
            klat, klon = geo.km_per_deg(self.center[0])
            for c in self.cells:
                km = c['speed_kmh'] * TIME_LAPSE * dt / 3600
                h = math.radians(c['heading'])
                c['lat'] += km * math.cos(h) / klat
                c['lon'] += km * math.sin(h) / klon
                c['age'] += dt
                c['rate'] = c['peak'] * self._profile(c['age'] / c['life'])
                for _ in range(_poisson(c['rate'] * dt / 60)):
                    new.append(self._stroke(c))
            self.cells = [c for c in self.cells if c['age'] < c['life']
                          and geo.dist_km((c['lat'], c['lon']), self.center) < LEAVE_KM]
            while len(self.cells) < INTENSITY[self.intensity]['cells']:
                self._spawn()
            cut = time.time() - KEEP_SECONDS
            while self.strikes and self.strikes[0]['t'] < cut * 1000:
                self.strikes.popleft()
        for s in new:                      # effects outside the lock: they call into the simulator
            if s['type'] == 'CG':
                s['effect'] = self._effect(s)
            with self.lock:
                self.strikes.append(s)

    def _stroke(self, c):
        klat, klon = geo.km_per_deg(c['lat'])
        sigma = c['radius_km'] * 0.45
        cg = random.random() < 0.40
        if not cg:
            ka = random.choice((-1, 1)) * random.uniform(3, 15)
        elif random.random() < 0.88:
            ka = -min(70.0, random.lognormvariate(math.log(20), 0.55) + 8)
        else:
            ka = min(130.0, random.lognormvariate(math.log(40), 0.6) + 20)
        s = {'id': self.next_strike, 't': int(time.time() * 1000),
             'lat': c['lat'] + random.gauss(0, sigma) / klat,
             'lon': c['lon'] + random.gauss(0, sigma) / klon,
             'ka': round(ka, 1), 'type': 'CG' if cg else 'IC', 'cell': c['id'], 'effect': None}
        self.next_strike += 1
        return s

    def _effect(self, s):
        now = time.time()
        if now - self.last_fault < INTENSITY[self.intensity]['fault_gap']:
            return None
        p = (s['lat'], s['lon'])
        near = geo.nearest_section(self.net, p)
        if near and near[0] <= 0.4:
            d, _, sid, k = near
            if random.random() < (0.85 if d <= 0.15 else 0.45):
                permanent = random.random() < 0.6
                ok, _ = (self.trip_permanent if permanent else self.trip_transient)(sid, k)
                if ok:
                    self.last_fault = now
                    return f"{'fault' if permanent else 'transient trip'}: slave {sid} section {k} ({d * 1000:.0f} m)"
            return None
        for st in self.stations:
            if st['has_net']:
                continue
            d = geo.dist_km(p, st['pos'])
            if d <= 2.5 and random.random() < 0.3:
                sid = random.choice(st['feeders'])
                ok, _ = self.trip_transient(sid, None)
                if ok:
                    self.last_fault = now
                    return f"transient trip: {st['id']} slave {sid} ({d:.1f} km)"
        return None

    # ── LLS feed ────────────────────────────────────────────────────────
    def state(self, since=0):
        with self.lock:
            return {
                'active': self.active, 'intensity': self.intensity, 'time_lapse': TIME_LAPSE,
                'started': int(self.started * 1000) if self.active else None,
                'heading': round(self.heading), 'now': int(time.time() * 1000),
                'cells': [{'id': c['id'], 'lat': round(c['lat'], 5), 'lon': round(c['lon'], 5),
                           'radius_km': round(c['radius_km'], 2), 'rate': round(c.get('rate', 0), 1),
                           'heading': round(c['heading']), 'speed_kmh': round(c['speed_kmh']),
                           'life': round(c['age'] / c['life'], 2)} for c in self.cells],
                'strikes': [dict(s, lat=round(s['lat'], 5), lon=round(s['lon'], 5))
                            for s in self.strikes if s['id'] > since],
            }

    def run(self):
        while True:
            try:
                self.step(0.5)
            except Exception as e:                     # never let weather kill the simulator
                self.log(f"[STORM] error: {e}")
            time.sleep(0.5)
