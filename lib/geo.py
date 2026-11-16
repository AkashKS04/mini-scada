#!/usr/bin/env python3
"""
lib/geo.py - geographic placement for Storm Watch

Substations sit at their latitude/longitude from the config. The SS-01
feeder network has no surveyed routes, so its load nodes are placed around
the substation from their schematic coordinates (x grows east, y grows
south). These routes are ILLUSTRATIVE: good enough to decide which cable a
simulated lightning strike landed near, not a real GIS.

Distances use a local equirectangular projection (accurate to well under 1 %
over a city).
"""

import math

KM_PER_UNIT = 0.0095        # schematic unit -> km (the SS-01 network spans ~5.5 km)
SCHEMATIC_Y0 = 250          # schematic y of the substation's east-west axis


def km_per_deg(lat):
    return 110.57, 111.32 * math.cos(math.radians(lat))


def node_geo(ss, x, y):
    """Schematic (x, y) of a load node -> (lat, lon) around substation ss."""
    klat, klon = km_per_deg(ss['latitude'])
    return (ss['latitude'] - (y - SCHEMATIC_Y0) * KM_PER_UNIT / klat,
            ss['longitude'] + x * KM_PER_UNIT / klon)


def dist_km(a, b):
    """Great-circle-ish distance between two (lat, lon) points in km."""
    klat, klon = km_per_deg((a[0] + b[0]) / 2)
    return math.hypot((a[0] - b[0]) * klat, (a[1] - b[1]) * klon)


def dist_to_segment_km(p, a, b):
    """Shortest distance from point p to the segment a-b, in km."""
    klat, klon = km_per_deg(p[0])
    px, py = 0.0, 0.0
    ax, ay = (a[1] - p[1]) * klon, (a[0] - p[0]) * klat
    bx, by = (b[1] - p[1]) * klon, (b[0] - p[0]) * klat
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0 if L2 == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(ax + t * dx - px, ay + t * dy - py)


def network(config):
    """
    Geographic layout of every feeder-map station:
    {ss_id: {'feeders': {slave_id: [(lat, lon) substation, node 1, ...]},
             'ties': [(id, (lat, lon), (lat, lon))]}}
    Section k of a feeder is the segment between points k-1 and k.
    """
    out = {}
    for ss in config['substations']:
        if not any(f.get('load_nodes') for f in ss['feeders']):
            continue
        origin = (ss['latitude'], ss['longitude'])
        feeders, at = {}, {}
        for f in ss['feeders']:
            pts = [origin] + [node_geo(ss, n['x'], n['y']) for n in f.get('load_nodes', [])]
            feeders[f['slave_id']] = pts
            for i, p in enumerate(pts[1:], start=1):
                at[(f['feeder_number'], i)] = p
        ties = [(t['id'], at[(t['a']['feeder'], t['a']['node'])], at[(t['b']['feeder'], t['b']['node'])])
                for t in ss.get('ties', [])]
        out[ss['id']] = {'feeders': feeders, 'ties': ties}
    return out


def nearest_section(net, p):
    """(distance km, ss_id, feeder slave_id, section k) of the cable section closest to p."""
    best = None
    for ss_id, n in net.items():
        for sid, pts in n['feeders'].items():
            for k in range(1, len(pts)):
                d = dist_to_segment_km(p, pts[k - 1], pts[k])
                if best is None or d < best[0]:
                    best = (d, ss_id, sid, k)
    return best
