#!/usr/bin/env python3
"""
simulator/modbus_server.py — Phase 4 RTU Simulator

Reads substations.json and simulates all 18 Modbus slave devices:
    3 transformers  (Slave IDs 1, 7, 13)
    15 feeders      (Slave IDs 2-6, 8-12, 14-18)

Register Map (same for every slave):
    40001 → Voltage      (kV × 100,  e.g. 1100 = 11.00 kV)
    40002 → Current      (A  × 10,   e.g. 1530 = 153.0 A)
    40003 → Power MW     (MW × 100,  e.g.  250 = 2.50 MW)
    40004 → Power Factor (×  100,    e.g.   95 = 0.95)
    40005 → Temperature  (°C × 10,   e.g.  725 = 72.5 °C) — transformer only
    40006 → Frequency    (Hz × 100,  e.g. 5000 = 50.00 Hz)
    40100 → Breaker State (0=OPEN, 1=CLOSED, 2=TRIPPED, 3=UNKNOWN)

Failure Injection (automatic, random):
    OFFLINE  — device stops responding for 20-40s
    FREEZE   — one register value freezes for 15-25s
    TRIP     — breaker state changes to TRIPPED (2)
"""

import os
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
REG_VOLTAGE      = 1    # 40001
REG_CURRENT      = 2    # 40002
REG_POWER_MW     = 3    # 40003
REG_POWER_FACTOR = 4    # 40004
REG_TEMPERATURE  = 5    # 40005
REG_FREQUENCY    = 6    # 40006
REG_BREAKER      = 100  # 40100

REGISTER_COUNT   = 110  # allocate enough registers

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

# ─────────────────────────────────────────────
# Build one ModbusSlaveContext per device
# ─────────────────────────────────────────────
device_contexts = {}
for slave_id in device_registry:
    device_contexts[slave_id] = ModbusSlaveContext(
        hr=ModbusSequentialDataBlock(REG_VOLTAGE, [0] * REGISTER_COUNT)
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


def generate_feeder_values(device):
    """Generate realistic feeder analog values."""
    normal  = device['normal']
    ltype   = device.get('load_type', 'MIXED')
    factor  = load_type_factor(ltype)

    v_min, v_max = normal['voltage_kv']['min'], normal['voltage_kv']['max']
    i_min, i_max = normal['current_a']['min'],  normal['current_a']['max']
    p_min, p_max = normal['power_mw']['min'],   normal['power_mw']['max']
    pf_min,pf_max= normal['power_factor']['min'],normal['power_factor']['max']

    # Voltage is relatively stable, small variation
    voltage     = v_min + (v_max - v_min) * (0.5 + random.uniform(-0.15, 0.15))
    # Current and power scale with load factor
    current     = i_min + (i_max - i_min) * factor + random.uniform(-5, 5)
    power_mw    = p_min + (p_max - p_min) * factor + random.uniform(-0.05, 0.05)
    power_factor= pf_min + (pf_max - pf_min) * random.uniform(0.8, 1.0)
    frequency   = 50.0 + random.uniform(-0.1, 0.1)

    # Occasional anomalies (~3% chance)
    if random.random() < 0.03:
        voltage += random.uniform(0.4, 0.8)   # voltage surge
    if random.random() < 0.03:
        current += random.uniform(30, 60)      # current spike
    if random.random() < 0.02:
        power_factor -= random.uniform(0.08, 0.15)  # PF drop

    return {
        'voltage':      max(0, voltage),
        'current':      max(0, current),
        'power_mw':     max(0, power_mw),
        'power_factor': max(0.5, min(1.0, power_factor)),
        'frequency':    frequency,
    }


def generate_transformer_values(device):
    """Generate realistic transformer analog values."""
    normal = device['normal']

    lv_min = normal['lv_voltage_kv']['min']
    lv_max = normal['lv_voltage_kv']['max']
    i_min  = normal['current_a']['min']
    i_max  = normal['current_a']['max']
    p_min  = normal['power_mw']['min']
    p_max  = normal['power_mw']['max']
    t_min  = normal['temperature_degc']['min']
    t_max  = normal['temperature_degc']['max']

    factor = get_time_factor()

    voltage     = lv_min + (lv_max - lv_min) * (0.5 + random.uniform(-0.1, 0.1))
    current     = i_min  + (i_max - i_min)   * factor + random.uniform(-10, 10)
    power_mw    = p_min  + (p_max - p_min)   * factor + random.uniform(-0.1, 0.1)
    temperature = t_min  + (t_max - t_min)   * (0.4 + factor * 0.5)
    frequency   = 50.0   + random.uniform(-0.08, 0.08)
    power_factor= 0.88   + random.uniform(-0.05, 0.05)

    # Occasional temperature spike
    if random.random() < 0.04:
        temperature += random.uniform(8, 20)

    # Occasional overload
    if random.random() < 0.03:
        current  += random.uniform(50, 120)
        power_mw += random.uniform(0.5, 2.0)

    return {
        'voltage':      max(0, voltage),
        'current':      max(0, current),
        'power_mw':     max(0, power_mw),
        'power_factor': max(0.5, min(1.0, power_factor)),
        'temperature':  max(20, temperature),
        'frequency':    frequency,
    }


# ─────────────────────────────────────────────
# Scale floats to integer register values
# ─────────────────────────────────────────────
def to_reg(value, scale):
    """Convert float to scaled integer register value."""
    return max(0, int(round(value * scale)))


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
            vals = generate_feeder_values(device)

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

        time.sleep(2)


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
            print(f"\n[FAILURE] ⚠  Slave {slave_id} ({name}) → OFFLINE for {duration}s\n")
            time.sleep(duration)
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
        wait = random.randint(60, 120)
        time.sleep(wait)


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

    print("\n" + "="*60)
    print(" Phase 4 SCADA RTU Simulator")
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
    print("   40001 → Voltage (kV × 100)")
    print("   40002 → Current (A × 10)")
    print("   40003 → Power MW (MW × 100)")
    print("   40004 → Power Factor (× 100)")
    print("   40005 → Temperature (°C × 10) — transformer only")
    print("   40006 → Frequency (Hz × 100)")
    print("   40100 → Breaker State (0=OPEN 1=CLOSED 2=TRIPPED)")
    print()
    print(" Failure injection: ENABLED (first failure in ~45s)")
    print("="*60 + "\n")

    await StartAsyncTcpServer(context=context, address=("0.0.0.0", 5020))


if __name__ == "__main__":
    asyncio.run(main())
