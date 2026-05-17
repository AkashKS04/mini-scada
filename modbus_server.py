#!/usr/bin/env python3
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

# --- Shared datastore ---
store = ModbusSlaveContext(
    hr=ModbusSequentialDataBlock(0, [0] * 10)
)
context = ModbusServerContext(slaves=store, single=True)

def update_sensors():
    """Simulate RTU sensor data in background thread."""
    while True:
        voltage     = int(random.uniform(220.0, 240.0) * 10)
        current     = int(random.uniform(10.0,  20.0)  * 10)
        temperature = int(random.uniform(60.0,  90.0)  * 10)

        # Occasional spikes to trigger alarms
        if random.random() < 0.05:
            voltage     = int(random.uniform(250.0, 260.0) * 10)
        if random.random() < 0.05:
            temperature = int(random.uniform(95.0, 105.0)  * 10)

        store.setValues(3, 0, [voltage, current, temperature])
        print(f"[RTU] V={voltage/10}V  I={current/10}A  T={temperature/10}°C")
        time.sleep(2)

async def main():
    t = threading.Thread(target=update_sensors, daemon=True)
    t.start()
    print("[SERVER] Modbus TCP Server starting on port 5020...")
    await StartAsyncTcpServer(context=context, address=("0.0.0.0", 5020))

if __name__ == "__main__":
    asyncio.run(main())
