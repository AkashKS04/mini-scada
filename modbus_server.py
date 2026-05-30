#!/usr/bin/env python3
"""
modbus_server.py — RTU Simulator + Modbus TCP Server
Simulates a field device (RTU) with proper register mapping:

    Register Map (Holding Registers):
    ┌──────────┬─────────────────┬────────────────────────────┐
    │ Address  │ Modbus Ref      │ Description                │
    ├──────────┼─────────────────┼────────────────────────────┤
    │    1     │ 40001           │ Voltage     (x10, e.g 2301 = 230.1 V)  │
    │    2     │ 40002           │ Current     (x10, e.g  153 =  15.3 A)  │
    │    3     │ 40003           │ Temperature (x10, e.g  725 =  72.5 °C) │
    └──────────┴─────────────────┴────────────────────────────┘

Scaling: values are stored as integers (x10) to avoid floats in Modbus.
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
# Register Map — single source of truth
# Change addresses here and it reflects everywhere
# ─────────────────────────────────────────────
REG_VOLTAGE     = 1   # Modbus ref: 40001
REG_CURRENT     = 2   # Modbus ref: 40002
REG_TEMPERATURE = 3   # Modbus ref: 40003

REGISTER_COUNT  = 10  # total holding registers allocated

# ─────────────────────────────────────────────
# Datastore — allocate holding registers
# Starting at address 1 (= 40001 in Modbus convention)
# ─────────────────────────────────────────────
store = ModbusSlaveContext(
    hr=ModbusSequentialDataBlock(REG_VOLTAGE, [0] * REGISTER_COUNT)
)
context = ModbusServerContext(slaves=store, single=True)


def update_sensors():
    """
    Simulate RTU sensor readings and write them into Modbus registers.
    Runs in a background thread, updates every 2 seconds.
    Occasional spikes simulate real-world anomalies that trigger alarms.
    """
    while True:
        # Normal operating range
        voltage     = int(random.uniform(220.0, 240.0) * 10)
        current     = int(random.uniform(10.0,  20.0)  * 10)
        temperature = int(random.uniform(60.0,  90.0)  * 10)

        # ~5% chance of voltage spike (simulates fault condition)
        if random.random() < 0.05:
            voltage = int(random.uniform(250.0, 260.0) * 10)

        # ~5% chance of temperature spike (simulates overheating)
        if random.random() < 0.05:
            temperature = int(random.uniform(95.0, 105.0) * 10)

        # Write each value into its designated register address
        store.setValues(3, REG_VOLTAGE,     [voltage])
        store.setValues(3, REG_CURRENT,     [current])
        store.setValues(3, REG_TEMPERATURE, [temperature])

        print(
            f"[RTU] "
            f"40001=Voltage:{voltage/10}V  "
            f"40002=Current:{current/10}A  "
            f"40003=Temp:{temperature/10}°C"
        )
        time.sleep(2)


async def main():
    # Start sensor simulation in background thread
    t = threading.Thread(target=update_sensors, daemon=True)
    t.start()

    print("[SERVER] Modbus TCP Server starting on 0.0.0.0:5020")
    print("[SERVER] Register Map:")
    print("[SERVER]   40001 → Voltage (V)")
    print("[SERVER]   40002 → Current (A)")
    print("[SERVER]   40003 → Temperature (°C)")
    await StartAsyncTcpServer(context=context, address=("0.0.0.0", 5020))


if __name__ == "__main__":
    asyncio.run(main())
