#!/usr/bin/env python3
"""
modbus_server.py — Multi-Device RTU Simulator + Modbus TCP Server

Simulates 3 field devices on the same Modbus TCP line, each with its
own Slave ID (Unit ID). All devices share the same register map:

    Register Map (Holding Registers) — same for every device:
        Address 1 → 40001 → Voltage     (x10)
        Address 2 → 40002 → Current     (x10)
        Address 3 → 40003 → Temperature (x10)

    Devices:
        Slave ID 1 → Feeder A        (normal voltage range)
        Slave ID 2 → Transformer T1  (runs hotter)
        Slave ID 3 → Substation S1   (higher voltage range)

A real Modbus master talks to all 3 over ONE TCP connection,
distinguishing devices by Slave ID — exactly like a real RTU network.
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
# Register Map — same for every device
# ─────────────────────────────────────────────
REG_VOLTAGE     = 1   # 40001
REG_CURRENT     = 2   # 40002
REG_TEMPERATURE = 3   # 40003
REGISTER_COUNT  = 10

# ─────────────────────────────────────────────
# Device definitions
# Each device gets its own Slave ID, name,
# and its own normal operating ranges
# ─────────────────────────────────────────────
DEVICES = {
    1: {
        "name": "Feeder A",
        "voltage_range":     (220.0, 240.0),
        "current_range":     (10.0,  20.0),
        "temperature_range": (60.0,  90.0),
        "voltage_spike":     (250.0, 260.0),
        "temp_spike":        (95.0, 105.0),
    },
    2: {
        "name": "Transformer T1",
        "voltage_range":     (215.0, 235.0),
        "current_range":     (15.0,  30.0),
        "temperature_range": (75.0, 100.0),   # runs hotter than Feeder A
        "voltage_spike":     (245.0, 255.0),
        "temp_spike":        (105.0, 115.0),
    },
    3: {
        "name": "Substation S1",
        "voltage_range":     (230.0, 250.0),  # higher voltage range
        "current_range":     (5.0,   15.0),
        "temperature_range": (50.0,  80.0),
        "voltage_spike":     (260.0, 270.0),
        "temp_spike":        (90.0, 100.0),
    },
}

# ─────────────────────────────────────────────
# Build one ModbusSlaveContext per device
# ─────────────────────────────────────────────
device_contexts = {}
for slave_id in DEVICES:
    device_contexts[slave_id] = ModbusSlaveContext(
        hr=ModbusSequentialDataBlock(REG_VOLTAGE, [0] * REGISTER_COUNT)
    )

# single=False -> server uses the slave_id dict to route requests
context = ModbusServerContext(slaves=device_contexts, single=False)


def update_device(slave_id, config):
    """
    Background thread — continuously generates simulated sensor
    data for ONE device and writes it into that device's registers.
    """
    store = device_contexts[slave_id]
    name  = config["name"]

    while True:
        v_lo, v_hi = config["voltage_range"]
        c_lo, c_hi = config["current_range"]
        t_lo, t_hi = config["temperature_range"]

        voltage     = int(random.uniform(v_lo, v_hi) * 10)
        current     = int(random.uniform(c_lo, c_hi) * 10)
        temperature = int(random.uniform(t_lo, t_hi) * 10)

        # ~5% chance of voltage spike
        if random.random() < 0.05:
            sv_lo, sv_hi = config["voltage_spike"]
            voltage = int(random.uniform(sv_lo, sv_hi) * 10)

        # ~5% chance of temperature spike
        if random.random() < 0.05:
            st_lo, st_hi = config["temp_spike"]
            temperature = int(random.uniform(st_lo, st_hi) * 10)

        store.setValues(3, REG_VOLTAGE,     [voltage])
        store.setValues(3, REG_CURRENT,     [current])
        store.setValues(3, REG_TEMPERATURE, [temperature])

        print(
            f"[RTU][Slave {slave_id}: {name}] "
            f"40001=V:{voltage/10}V  "
            f"40002=I:{current/10}A  "
            f"40003=T:{temperature/10}C"
        )
        time.sleep(2)


async def main():
    # Start one background thread per device
    for slave_id, config in DEVICES.items():
        t = threading.Thread(
            target=update_device,
            args=(slave_id, config),
            daemon=True
        )
        t.start()

    print("[SERVER] Modbus TCP Server starting on 0.0.0.0:5020")
    print("[SERVER] Register Map (same for every device):")
    print("[SERVER]   40001 -> Voltage (V)")
    print("[SERVER]   40002 -> Current (A)")
    print("[SERVER]   40003 -> Temperature (C)")
    print("[SERVER] Devices:")
    for slave_id, config in DEVICES.items():
        print(f"[SERVER]   Slave ID {slave_id} -> {config['name']}")

    await StartAsyncTcpServer(context=context, address=("0.0.0.0", 5020))


if __name__ == "__main__":
    asyncio.run(main())
