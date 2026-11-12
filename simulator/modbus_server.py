#!/usr/bin/env python3
"""
simulator/modbus_server.py — RTU Simulator

Reads substations.json and simulates every Modbus slave device
(5 transformers + 36 feeders, slave ids from the config).

Register Map (same for every slave). Wire addresses are 0-based, as in the
Modbus convention: holding register 4xxxx N is protocol address N-1, and
that is the number Wireshark shows.
    40001 (addr 0)  → Voltage      (kV × 100,  e.g. 1100 = 11.00 kV)
    40002 (addr 1)  → Current      (A  × 10,   e.g. 1530 = 153.0 A)
    40003 (addr 2)  → Power MW     (MW × 100,  e.g.  250 = 2.50 MW)
    40004 (addr 3)  → Power Factor (×  100,    e.g.   95 = 0.95)
    40005 (addr 4)  → Temperature  (°C × 10,   e.g.  725 = 72.5 °C) — transformer only
    40006 (addr 5)  → Frequency    (Hz × 100,  e.g. 5000 = 50.00 Hz)
    40100 (addr 99) → Breaker State (0=OPEN, 1=CLOSED, 2=TRIPPED, 3=UNKNOWN)

Electrical model (so the numbers on the SLD add up):
    Feeder      V = its station's 11 kV bus voltage, P from the load curve,
                I = P / (sqrt3 x V x pf)
    Transformer P and Q = sum of its feeders plus losses, I = S / (sqrt3 x V),
                top-oil temperature follows loading with a thermal lag

Failure Injection (automatic, random):
    OFFLINE  — device stops responding for 20-40s (Modbus exception 0x0B)
    FREEZE   — one register value freezes for 15-25s
    TRIP     — breaker state changes to TRIPPED (2)
"""

import os
import sys
import json
import time
import math
import random
import threading
import asyncio
from pymodbus.datastore import (
    ModbusSlaveContext,
    ModbusServerContext,
    ModbusSequentialDataBlock
)
from pymodbus.server import StartAsyncTcpServer
from pymodbus.exceptions import NoSuchSlaveException

# ─────────────────────────────────────────────
# Load config
# ─────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, 'config', 'substations.json')

with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

# ─────────────────────────────────────────────
# Register addresses
# ─────────────────────────────────────────────
# 0-based wire addresses (40001 = address 0)
REG_VOLTAGE      = 0    # 40001
REG_CURRENT      = 1    # 40002
REG_POWER_MW     = 2    # 40003
REG_POWER_FACTOR = 3    # 40004
REG_TEMPERATURE  = 4    # 40005
REG_FREQUENCY    = 5    # 40006
REG_BREAKER      = 99   # 40100

REGISTER_COUNT   = 110  # allocate enough registers

# Normal load never exceeds this fraction of the HIGH limit, so alarms come
# from real events (spikes, faults) and not from ordinary peak demand
LOAD_CEILING     = 0.88

SQRT3            = math.sqrt(3)

# Transformer input = sum of its feeders plus losses
LOSS_P           = 1.01
LOSS_Q           = 1.03

# Top-oil temperature: ambient + rise x loading^1.6, reached with a lag
AMBIENT_C        = 30.0
OIL_RISE_C       = 50.0     # rise above ambient at full rated load
THERMAL_TAU      = 600.0    # seconds
UPDATE_SECONDS   = 2

# Below this the 11 kV bus counts as dead
BUS_LIVE_KV      = 5.0

# Seconds between injected failures. Each feeder trip is a CRITICAL alarm an
# operator must ACK and clear, so this keeps it to roughly 35 trips a day.
# Lower it (e.g. (60, 120)) for a busy demo.
FAILURE_GAP      = (300, 600)

# Breaker states
BREAKER_OPEN    = 0
BREAKER_CLOSED  = 1
BREAKER_TRIPPED = 2
BREAKER_UNKNOWN = 3

# ─────────────────────────────────────────────
# Failure state — shared across threads
# ─────────────────────────────────────────────
offline_devices  = set()          # slave_ids currently offline
frozen_registers = {}             # {slave_id: {reg_addr: frozen_value}}
failure_lock     = threading.Lock()

# ─────────────────────────────────────────────
# Build device registry from config
# ─────────────────────────────────────────────
# device_registry: {slave_id: device_info_dict}
device_registry = {}

for ss in CONFIG['substations']:
    # Transformer
    tr = ss['transformer']
    device_registry[tr['slave_id']] = {
        'type':        'transformer',
        'id':          tr['id'],
        'name':        tr['name'],
        'substation':  ss['id'],
        'ss_name':     ss['name'],
        'slave_id':    tr['slave_id'],
        'normal':      tr['normal_operating'],
        'limits':      tr['analog_limits'],
        'has_temp':    True,
        'rating_mva':  tr['rating_mva'],
    }
    # Feeders
    for f in ss['feeders']:
        device_registry[f['slave_id']] = {
            'type':        'feeder',
            'id':          f['id'],
            'name':        f['name'],
            'substation':  ss['id'],
            'ss_name':     ss['name'],
            'slave_id':    f['slave_id'],
            'load_type':   f['load_type'],
            'normal':      f['normal_operating'],
            'limits':      f['analog_limits'],
            'has_temp':    False,
            'consumers':   f.get('estimated_consumers', 0),
        }

print(f"[CONFIG] Loaded {len(device_registry)} devices from substations.json")

# {substation_id: transformer slave_id}, used to work out if a feeder has supply
transformer_of = {ss["id"]: ss["transformer"]["slave_id"] for ss in CONFIG["substations"]}

# {substation_id: [feeder slave ids]}, summed by the transformer
feeders_of = {ss["id"]: [f["slave_id"] for f in ss["feeders"]] for ss in CONFIG["substations"]}

# {substation_id: 11 kV bus voltage}. Written by the transformer thread,
# read by the feeders, which all measure the same busbar.
bus_voltage = {ss["id"]: 11.0 for ss in CONFIG["substations"]}

# {transformer slave_id: top-oil temperature}, carried between updates
oil_temp = {}

# ─────────────────────────────────────────────
# Build one ModbusSlaveContext per device
# ─────────────────────────────────────────────
class RtuContext(ModbusSlaveContext):
    """
    A slave that can go OFFLINE. Every Modbus read and write is validated
    first; refusing it here makes pymodbus answer with exception 0x0B
    (Gateway Target Device Failed to Respond), like a real gateway whose
    field device has gone silent. Removing the slave from the server instead
    would only give a 3 s client timeout per request, because pymodbus drops
    frames for unknown unit ids. The simulator's own threads use
    getValues/setValues directly, so they keep running.
    """
    offline = False

    def validate(self, fc_as_hex, address, count=1):
        if self.offline:
            raise NoSuchSlaveException("device offline (simulated)")
        return super().validate(fc_as_hex, address, count)


device_contexts = {}
for slave_id in device_registry:
    device_contexts[slave_id] = RtuContext(
        hr=ModbusSequentialDataBlock(0, [0] * REGISTER_COUNT)
    )
    # All breakers start CLOSED
    device_contexts[slave_id].setValues(3, REG_BREAKER, [BREAKER_CLOSED])

context = ModbusServerContext(slaves=device_contexts, single=False)


# ─────────────────────────────────────────────
# Realistic value generation
# ─────────────────────────────────────────────
def get_time_factor():
    """
    Returns a load factor (0.4 - 1.0) based on time of day.
    Simulates realistic daily load curve:
    - Night (0-5h): low load 0.4
    - Morning peak (7-9h): high 0.9
    - Afternoon (12-14h): medium 0.7
    - Evening peak (18-21h): high 1.0
    - Late evening (22-24h): medium 0.6
    """
    hour = time.localtime().tm_hour + time.localtime().tm_min / 60
    # Smooth sinusoidal approximation of daily load curve
    base = 0.65 + 0.25 * math.sin(math.pi * (hour - 6) / 12)
    return max(0.4, min(1.0, base + random.uniform(-0.05, 0.05)))


def load_type_factor(load_type):
    """
    Different load types have different demand profiles.
    INDUSTRIAL loads are more constant, RESIDENTIAL peaks evenings.
    """
    hour = time.localtime().tm_hour
    if load_type == 'INDUSTRIAL':
        # Industrial: high during day, low at night
        if 8 <= hour <= 20:
            return random.uniform(0.75, 0.95)
        return random.uniform(0.30, 0.50)
    elif load_type == 'RESIDENTIAL':
        # Residential: peaks morning and evening
        if 7 <= hour <= 9 or 18 <= hour <= 22:
            return random.uniform(0.80, 1.00)
        elif 0 <= hour <= 5:
            return random.uniform(0.25, 0.40)
        return random.uniform(0.50, 0.70)
    elif load_type == 'COMMERCIAL':
        # Commercial: high during office hours
        if 9 <= hour <= 18:
            return random.uniform(0.70, 0.90)
        return random.uniform(0.20, 0.35)
    else:  # MIXED
        return get_time_factor()


def generate_feeder_values(device, bus_kv):
    """Feeder analog values. Current follows from P, V and pf."""
    normal  = device['normal']
    ltype   = device.get('load_type', 'MIXED')
    factor  = min(load_type_factor(ltype), LOAD_CEILING)

    p_min, p_max = normal['power_mw']['min'],   normal['power_mw']['max']
    pf_min,pf_max= normal['power_factor']['min'],normal['power_factor']['max']

    # Measured at the feeder breaker, i.e. on the station bus
    voltage     = bus_kv + random.uniform(-0.03, 0.03)
    power_mw    = p_min + (p_max - p_min) * factor + random.uniform(-0.03, 0.03)
    power_factor= pf_min + (pf_max - pf_min) * random.uniform(0.8, 1.0)
    frequency   = 50.0 + random.uniform(-0.1, 0.1)

    # Occasional anomalies (0.1% chance each, per 2 s update)
    if random.random() < 0.001:
        voltage += random.uniform(0.4, 0.8)          # voltage surge
    if random.random() < 0.001:
        power_factor -= random.uniform(0.08, 0.15)   # PF drop

    power_mw     = max(0, power_mw)
    power_factor = max(0.5, min(1.0, power_factor))
    current = power_mw * 1000 / (SQRT3 * voltage * power_factor) if voltage > 0 else 0.0

    # Current transient (e.g. motor starting): a measurement spike, not load
    if random.random() < 0.001:
        current *= random.uniform(1.25, 1.4)

    return {
        'voltage':      max(0, voltage),
        'current':      current,
        'power_mw':     power_mw,
        'power_factor': power_factor,
        'frequency':    frequency,
    }


def feeder_totals(substation):
    """P and Q drawn by a station's feeders, read from their registers (MW, Mvar)."""
    p_total = q_total = 0.0
    for sid in feeders_of[substation]:
        regs = device_contexts[sid].getValues(3, REG_VOLTAGE, count=6)
        p  = regs[REG_POWER_MW] / 100
        pf = min(1.0, max(0.01, regs[REG_POWER_FACTOR] / 100))
        p_total += p
        q_total += p * math.tan(math.acos(pf))
    return p_total, q_total


def generate_transformer_values(device):
    """Transformer analog values: the sum of its feeders plus losses."""
    normal = device['normal']
    lv_min = normal['lv_voltage_kv']['min']
    lv_max = normal['lv_voltage_kv']['max']

    p_feed, q_feed = feeder_totals(device['substation'])
    power_mw = p_feed * LOSS_P
    q_mvar   = q_feed * LOSS_Q
    s_mva    = math.hypot(power_mw, q_mvar)

    voltage      = lv_min + (lv_max - lv_min) * (0.5 + random.uniform(-0.1, 0.1))
    current      = s_mva * 1000 / (SQRT3 * voltage)
    power_factor = power_mw / s_mva if s_mva else 1.0
    frequency    = 50.0 + random.uniform(-0.08, 0.08)

    # Top-oil temperature moves towards its steady state for this loading
    loading = s_mva / device['rating_mva']
    target  = AMBIENT_C + OIL_RISE_C * loading ** 1.6
    sid     = device['slave_id']
    if sid not in oil_temp and s_mva == 0:
        temp = target              # startup, feeders not written yet: don't seed cold
    else:
        temp  = oil_temp.get(sid, target)
        temp += (target - temp) * min(1.0, UPDATE_SECONDS / THERMAL_TAU)
        oil_temp[sid] = temp
    temperature = temp + random.uniform(-0.2, 0.2)

    # Occasional hot-spot reading
    if random.random() < 0.001:
        temperature += random.uniform(8, 20)

    return {
        'voltage':      voltage,
        'current':      current,
        'power_mw':     power_mw,
        'power_factor': max(0.5, min(1.0, power_factor)),
        'temperature':  temperature,
        'frequency':    frequency,
    }


# ─────────────────────────────────────────────
# Scale floats to integer register values
# ─────────────────────────────────────────────
def to_reg(value, scale):
    """Convert float to scaled integer register value (16-bit, 0-65535).
    An out-of-range value would make the whole Modbus read of the device fail."""
    return max(0, min(65535, int(round(value * scale))))


# ─────────────────────────────────────────────
# Breaker control (called by the DNP3 outstation)
# ─────────────────────────────────────────────
def apply_breaker_command(slave_id, command):
    """
    Execute an OPEN or CLOSE command on a breaker.
    Returns (ok, message). Never raises: the caller reports the message
    back to the master.
    """
    device = device_registry.get(slave_id)
    if not device:
        return False, "unknown device"

    with failure_lock:
        if slave_id in offline_devices:
            return False, "device offline"

    store = device_contexts[slave_id]
    state = store.getValues(3, REG_BREAKER, count=1)[0]

    if command == "OPEN":
        if state == BREAKER_OPEN:
            return False, "already open"
        new_state = BREAKER_OPEN
    elif command == "CLOSE":
        if state == BREAKER_CLOSED:
            return False, "already closed"
        new_state = BREAKER_CLOSED
    else:
        return False, "unsupported command"

    store.setValues(3, REG_BREAKER, [new_state])
    print(f"\n[CONTROL] Slave {slave_id} ({device['name']}) breaker -> {command}\n")
    return True, "executed"


# ─────────────────────────────────────────────
# Device simulation thread
# ─────────────────────────────────────────────
def simulate_device(slave_id, device):
    """
    Background thread: generates sensor values for one device
    and writes them into its Modbus registers every 2 seconds.
    Respects offline and freeze failure states.
    """
    store = device_contexts[slave_id]
    name  = device['name']
    dtype = device['type']

    while True:
        with failure_lock:
            is_offline = slave_id in offline_devices
            frozen     = frozen_registers.get(slave_id, {})

        if is_offline:
            time.sleep(2)
            continue

        # Generate values
        if dtype == 'transformer':
            vals = generate_transformer_values(device)
        else:
            vals = generate_feeder_values(device, bus_voltage[device['substation']])

        # Values follow breaker state. An open feeder breaker carries no
        # current or power; an open transformer breaker kills the whole bus.
        own_state = store.getValues(3, REG_BREAKER, count=1)[0]
        tr_state  = device_contexts[transformer_of[device['substation']]] \
                        .getValues(3, REG_BREAKER, count=1)[0]
        bus_dead  = tr_state != BREAKER_CLOSED
        # A feeder follows the bus voltage the transformer last measured. Right
        # after the transformer recloses that is still 0 for up to one update;
        # treat the bus as dead until then rather than divide by ~0 kV.
        if dtype == 'feeder' and bus_voltage[device['substation']] < BUS_LIVE_KV:
            bus_dead = True
        if bus_dead:
            vals['voltage'] = 0.0
        if bus_dead or own_state != BREAKER_CLOSED:
            vals['current']      = 0.0
            vals['power_mw']     = 0.0
            vals['power_factor'] = 1.0
        if dtype == 'transformer':
            bus_voltage[device['substation']] = vals['voltage']

        # Apply freeze overrides
        voltage     = frozen.get(REG_VOLTAGE,      to_reg(vals['voltage'],      100))
        current     = frozen.get(REG_CURRENT,      to_reg(vals['current'],      10))
        power_mw    = frozen.get(REG_POWER_MW,     to_reg(vals['power_mw'],     100))
        pf          = frozen.get(REG_POWER_FACTOR, to_reg(vals['power_factor'], 100))
        frequency   = frozen.get(REG_FREQUENCY,    to_reg(vals['frequency'],    100))

        # Write registers
        store.setValues(3, REG_VOLTAGE,      [voltage])
        store.setValues(3, REG_CURRENT,      [current])
        store.setValues(3, REG_POWER_MW,     [power_mw])
        store.setValues(3, REG_POWER_FACTOR, [pf])
        store.setValues(3, REG_FREQUENCY,    [frequency])

        if dtype == 'transformer':
            temperature = frozen.get(REG_TEMPERATURE,
                          to_reg(vals.get('temperature', 25), 10))
            store.setValues(3, REG_TEMPERATURE, [temperature])

        # Console output
        freeze_tag = " [FROZEN]" if frozen else ""
        if dtype == 'transformer':
            temp_val = vals.get('temperature', 0)
            print(
                f"[RTU][Slave {slave_id:2d}: {name[:30]:<30}]{freeze_tag} "
                f"V:{vals['voltage']:.2f}kV  "
                f"I:{vals['current']:.0f}A  "
                f"P:{vals['power_mw']:.2f}MW  "
                f"T:{temp_val:.1f}°C"
            )
        else:
            print(
                f"[RTU][Slave {slave_id:2d}: {name[:30]:<30}]{freeze_tag} "
                f"V:{vals['voltage']:.2f}kV  "
                f"I:{vals['current']:.0f}A  "
                f"P:{vals['power_mw']:.2f}MW  "
                f"PF:{vals['power_factor']:.2f}"
            )

        time.sleep(UPDATE_SECONDS)


# ─────────────────────────────────────────────
# Failure injection thread
# ─────────────────────────────────────────────
def failure_injector():
    """
    Injects realistic failures at random intervals.
    Three failure types:
        OFFLINE  — device stops responding
        FREEZE   — register value freezes
        TRIP     — breaker changes to TRIPPED state
    """
    time.sleep(45)   # Wait 45s before first failure

    slave_ids = list(device_registry.keys())

    while True:

        # ── Random failure type ──
        failure_type = random.choices(
            ['OFFLINE', 'FREEZE', 'TRIP'],
            weights=[40, 40, 20],
            k=1
        )[0]

        slave_id = random.choice(slave_ids)
        device   = device_registry[slave_id]
        name     = device['name']

        if failure_type == 'OFFLINE':
            duration = random.randint(20, 40)
            with failure_lock:
                offline_devices.add(slave_id)
            device_contexts[slave_id].offline = True     # Modbus now answers 0x0B
            print(f"\n[FAILURE] ⚠  Slave {slave_id} ({name}) → OFFLINE for {duration}s\n")
            time.sleep(duration)
            device_contexts[slave_id].offline = False
            with failure_lock:
                offline_devices.discard(slave_id)
            print(f"\n[FAILURE] ✓  Slave {slave_id} ({name}) → RESTORED\n")

        elif failure_type == 'FREEZE':
            reg_choices = [REG_VOLTAGE, REG_CURRENT, REG_POWER_MW]
            reg_addr    = random.choice(reg_choices)
            reg_names   = {
                REG_VOLTAGE:   'Voltage',
                REG_CURRENT:   'Current',
                REG_POWER_MW:  'Power MW',
            }
            duration  = random.randint(15, 25)
            store     = device_contexts[slave_id]
            frozen_val = store.getValues(3, reg_addr, count=1)[0]

            with failure_lock:
                if slave_id not in frozen_registers:
                    frozen_registers[slave_id] = {}
                frozen_registers[slave_id][reg_addr] = frozen_val

            print(
                f"\n[FAILURE] ❄  Slave {slave_id} ({name}) "
                f"→ {reg_names[reg_addr]} FROZEN at {frozen_val} for {duration}s\n"
            )
            time.sleep(duration)

            with failure_lock:
                if slave_id in frozen_registers:
                    frozen_registers[slave_id].pop(reg_addr, None)
                    if not frozen_registers[slave_id]:
                        del frozen_registers[slave_id]

            print(f"\n[FAILURE] ✓  Slave {slave_id} ({name}) → {reg_names[reg_addr]} UNFROZEN\n")

        elif failure_type == 'TRIP':
            # Only trip feeders, not transformers
            feeder_ids = [sid for sid, d in device_registry.items()
                          if d['type'] == 'feeder']
            slave_id  = random.choice(feeder_ids)
            device    = device_registry[slave_id]
            name      = device['name']
            store     = device_contexts[slave_id]

            print(f"\n[FAILURE] ⚡ Slave {slave_id} ({name}) → BREAKER TRIPPED\n")
            store.setValues(3, REG_BREAKER, [BREAKER_TRIPPED])

            # Feeder values drop to near zero when tripped
            store.setValues(3, REG_CURRENT,  [0])
            store.setValues(3, REG_POWER_MW, [0])

            duration = random.randint(25, 50)
            time.sleep(duration)

            # Auto-reclose after duration
            store.setValues(3, REG_BREAKER, [BREAKER_CLOSED])
            print(f"\n[FAILURE] ✓  Slave {slave_id} ({name}) → BREAKER AUTO-RECLOSED\n")

        # Wait before next failure
        time.sleep(random.randint(*FAILURE_GAP))


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
async def main():
    # Start one simulation thread per device
    for slave_id, device in device_registry.items():
        t = threading.Thread(
            target=simulate_device,
            args=(slave_id, device),
            daemon=True
        )
        t.start()

    # Start failure injector
    fi = threading.Thread(target=failure_injector, daemon=True)
    fi.start()

    # DNP3 outstation: the control path for breaker OPEN / CLOSE
    sys.path.insert(0, os.path.join(BASE_DIR, 'lib'))
    import dnp3lite
    _, by_point = dnp3lite.point_maps(CONFIG)
    dnp3lite.Outstation(by_point, apply_breaker_command).start()

    print("\n" + "="*60)
    print(" Mini SCADA RTU Simulator")
    print("="*60)
    print(f" Config: {CONFIG_PATH}")
    print(f" Devices: {len(device_registry)} total")
    print()
    for ss in CONFIG['substations']:
        tr = ss['transformer']
        print(f" {ss['id']} — {ss['name']}")
        print(f"   Slave {tr['slave_id']:2d}: {tr['name']}")
        for f in ss['feeders']:
            print(f"   Slave {f['slave_id']:2d}: {f['name']} [{f['load_type']}]")
        print()
    print(" Register Map:")
    print("   40001 (addr 0)  → Voltage (kV × 100)")
    print("   40002 (addr 1)  → Current (A × 10)")
    print("   40003 (addr 2)  → Power MW (MW × 100)")
    print("   40004 (addr 3)  → Power Factor (× 100)")
    print("   40005 (addr 4)  → Temperature (°C × 10) — transformer only")
    print("   40006 (addr 5)  → Frequency (Hz × 100)")
    print("   40100 (addr 99) → Breaker State (0=OPEN 1=CLOSED 2=TRIPPED)")
    print()
    print(" Failure injection: ENABLED (first failure in ~45s)")
    print("="*60 + "\n")

    await StartAsyncTcpServer(context=context, address=("0.0.0.0", 5020))


if __name__ == "__main__":
    asyncio.run(main())
