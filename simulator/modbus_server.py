#!/usr/bin/env python3
"""
modbus_server.py — Multi-Device RTU Simulator + Failure Injection

Simulates 3 field devices on Modbus TCP with realistic failure scenarios:

    Register Map (same for every device):
        Address 1 → 40001 → Voltage     (x10)
        Address 2 → 40002 → Current     (x10)
        Address 3 → 40003 → Temperature (x10)

    Devices:
        Slave ID 1 → Feeder A
        Slave ID 2 → Transformer T1
        Slave ID 3 → Substation S1

    Failure Injection (automatic, random):
        - Device OFFLINE: server stops responding for that Slave ID
          (simulates RTU power loss / cable disconnect)
        - Data FREEZE: register value stops changing
          (simulates stuck sensor / frozen register)

    Failure schedule (to make demo visible):
        - Every ~60s, one random device goes OFFLINE for ~20s
        - Every ~45s, one random device gets a FREEZE on one register for ~15s
"""

import time
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
# Register Map
# ─────────────────────────────────────────────
REG_VOLTAGE     = 1   # 40001
REG_CURRENT     = 2   # 40002
REG_TEMPERATURE = 3   # 40003
REGISTER_COUNT  = 10

# ─────────────────────────────────────────────
# Device definitions
# ─────────────────────────────────────────────
DEVICES = {
    1: {
        "name": "Feeder A",
        "voltage_range":     (220.0, 240.0),
        "current_range":     (10.0,  20.0),
        "temperature_range": (60.0,  90.0),
        "voltage_spike":     (250.0, 260.0),
        "temp_spike":        (95.0,  105.0),
    },
    2: {
        "name": "Transformer T1",
        "voltage_range":     (215.0, 235.0),
        "current_range":     (15.0,  30.0),
        "temperature_range": (75.0,  100.0),
        "voltage_spike":     (245.0, 255.0),
        "temp_spike":        (105.0, 115.0),
    },
    3: {
        "name": "Substation S1",
        "voltage_range":     (230.0, 250.0),
        "current_range":     (5.0,   15.0),
        "temperature_range": (50.0,  80.0),
        "voltage_spike":     (260.0, 270.0),
        "temp_spike":        (90.0,  100.0),
    },
}

# ─────────────────────────────────────────────
# Failure state — shared across threads
# ─────────────────────────────────────────────
# offline_devices: set of slave_ids currently offline
# frozen_registers: dict of {slave_id: set of register addresses}
offline_devices  = set()
frozen_registers = {}   # {slave_id: {address: frozen_value}}
failure_lock     = threading.Lock()

# ─────────────────────────────────────────────
# Datastore — one context per device
# ─────────────────────────────────────────────
device_contexts = {}
for slave_id in DEVICES:
    device_contexts[slave_id] = ModbusSlaveContext(
        hr=ModbusSequentialDataBlock(REG_VOLTAGE, [0] * REGISTER_COUNT)
    )

# single=False → route by Slave ID
context = ModbusServerContext(slaves=device_contexts, single=False)


# ─────────────────────────────────────────────
# Sensor update thread — one per device
# ─────────────────────────────────────────────
def update_device(slave_id, config):
    store = device_contexts[slave_id]
    name  = config["name"]

    while True:
        with failure_lock:
            is_offline = slave_id in offline_devices
            frozen     = frozen_registers.get(slave_id, {})

        if is_offline:
            # Device offline — don't update registers, don't print
            # Modbus server will still accept connections but slave ID
            # won't respond (handled by not updating context)
            time.sleep(2)
            continue

        # Normal operating ranges
        v_lo, v_hi = config["voltage_range"]
        c_lo, c_hi = config["current_range"]
        t_lo, t_hi = config["temperature_range"]

        voltage     = int(random.uniform(v_lo, v_hi) * 10)
        current     = int(random.uniform(c_lo, c_hi) * 10)
        temperature = int(random.uniform(t_lo, t_hi) * 10)

        # ~5% chance of spikes
        if random.random() < 0.05:
            sv_lo, sv_hi = config["voltage_spike"]
            voltage = int(random.uniform(sv_lo, sv_hi) * 10)
        if random.random() < 0.05:
            st_lo, st_hi = config["temp_spike"]
            temperature = int(random.uniform(st_lo, st_hi) * 10)

        # Apply freeze if active — use frozen value instead of new random
        if REG_VOLTAGE in frozen:
            voltage = frozen[REG_VOLTAGE]
        if REG_CURRENT in frozen:
            current = frozen[REG_CURRENT]
        if REG_TEMPERATURE in frozen:
            temperature = frozen[REG_TEMPERATURE]

        store.setValues(3, REG_VOLTAGE,     [voltage])
        store.setValues(3, REG_CURRENT,     [current])
        store.setValues(3, REG_TEMPERATURE, [temperature])

        freeze_tag = " [FROZEN]" if frozen else ""
        print(
            f"[RTU][Slave {slave_id}: {name}]{freeze_tag} "
            f"40001=V:{voltage/10}V  "
            f"40002=I:{current/10}A  "
            f"40003=T:{temperature/10}C"
        )
        time.sleep(2)


# ─────────────────────────────────────────────
# Failure injection thread
# Randomly takes devices offline or freezes registers
# ─────────────────────────────────────────────
def failure_injector():
    """
    Injects realistic failures at random intervals.
    Runs as a background daemon thread.

    Offline cycle:  every 60-90s, pick a random device, take offline for 15-25s
    Freeze cycle:   every 45-75s, pick a random device+register, freeze for 10-20s
    """
    time.sleep(10)  # Wait 30s on startup before first failure

    while True:
        # ── OFFLINE injection ──
        offline_interval = random.randint(60, 90)
        time.sleep(offline_interval)

        slave_id = random.choice(list(DEVICES.keys()))
        name     = DEVICES[slave_id]["name"]
        duration = random.randint(40, 60)

        with failure_lock:
            offline_devices.add(slave_id)

        print(f"\n[FAILURE INJECTOR] ⚠ Slave {slave_id} ({name}) → OFFLINE for {duration}s\n")
        time.sleep(duration)

        with failure_lock:
            offline_devices.discard(slave_id)

        print(f"\n[FAILURE INJECTOR] ✓ Slave {slave_id} ({name}) → RESTORED\n")

        # ── FREEZE injection ──
        freeze_interval = random.randint(45, 75)
        time.sleep(freeze_interval)

        slave_id  = random.choice(list(DEVICES.keys()))
        name      = DEVICES[slave_id]["name"]
        reg_addr  = random.choice([REG_VOLTAGE, REG_CURRENT, REG_TEMPERATURE])
        reg_names = {REG_VOLTAGE: "Voltage", REG_CURRENT: "Current", REG_TEMPERATURE: "Temperature"}
        duration  = random.randint(10, 20)

        # Capture current value to freeze it
        store      = device_contexts[slave_id]
        frozen_val = store.getValues(3, reg_addr, count=1)[0]

        with failure_lock:
            if slave_id not in frozen_registers:
                frozen_registers[slave_id] = {}
            frozen_registers[slave_id][reg_addr] = frozen_val

        print(
            f"\n[FAILURE INJECTOR] ❄ Slave {slave_id} ({name}) "
            f"→ {reg_names[reg_addr]} FROZEN at {frozen_val/10} for {duration}s\n"
        )
        time.sleep(duration)

        with failure_lock:
            if slave_id in frozen_registers:
                frozen_registers[slave_id].pop(reg_addr, None)
                if not frozen_registers[slave_id]:
                    del frozen_registers[slave_id]

        print(f"\n[FAILURE INJECTOR] ✓ Slave {slave_id} ({name}) → {reg_names[reg_addr]} UNFROZEN\n")


# ─────────────────────────────────────────────
# Expose failure state for client to query
# (client imports this to check offline/frozen state)
# ─────────────────────────────────────────────
def get_failure_state():
    with failure_lock:
        return {
            "offline":  set(offline_devices),
            "frozen":   dict(frozen_registers),
        }


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
async def main():
    # Start one sensor thread per device
    for slave_id, config in DEVICES.items():
        t = threading.Thread(
            target=update_device,
            args=(slave_id, config),
            daemon=True
        )
        t.start()

    # Start failure injector thread
    fi = threading.Thread(target=failure_injector, daemon=True)
    fi.start()

    print("[SERVER] Modbus TCP Server starting on 0.0.0.0:5020")
    print("[SERVER] Register Map:")
    print("[SERVER]   40001 -> Voltage (V)")
    print("[SERVER]   40002 -> Current (A)")
    print("[SERVER]   40003 -> Temperature (C)")
    print("[SERVER] Devices:")
    for slave_id, config in DEVICES.items():
        print(f"[SERVER]   Slave ID {slave_id} -> {config['name']}")
    print("[SERVER] Failure injection: ENABLED (first failure in ~30s)")

    await StartAsyncTcpServer(context=context, address=("0.0.0.0", 5020))


if __name__ == "__main__":
    asyncio.run(main())
