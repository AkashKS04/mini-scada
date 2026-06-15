# Mini SCADA Dashboard — RHEL 10

A Linux-based SCADA simulation system built on Red Hat Enterprise Linux 10,
demonstrating industrial communication protocols, real-time data acquisition,
alarm management, and web-based monitoring.

---

## What This Is

This project simulates a basic SCADA (Supervisory Control and Data Acquisition)
system — the same type of system used in power grids, substations, and
industrial automation.

It uses real industrial protocols and concepts:

- **Modbus TCP** — the most widely used industrial communication protocol
- **RTU simulation** — simulated sensor device generating live voltage, current, and temperature data
- **Alarm engine** — threshold-based detection with ACTIVE / ACKNOWLEDGED states
- **Historian** — time-series data storage in SQLite
- **Web HMI** — live operator dashboard, similar to a real SCADA screen

---

## System Architecture

```
┌─────────────────────┐        ┌─────────────────────┐        ┌─────────────────────┐
│   modbus_server.py  │        │  modbus_client.py   │        │      app.py         │
│                     │        │                     │        │                     │
│   RTU Simulator     │──────▶ │   Data Collector    │──────▶ │   Flask Web HMI     │
│   Modbus TCP Server │        │   Alarm Engine      │        │   Live Dashboard    │
│   Port 5020         │        │   SQLite Logger     │        │   Port 8080         │
└─────────────────────┘        └─────────────────────┘        └─────────────────────┘
         ▲
         │
  Generates simulated
  sensor readings:
  Voltage / Current / Temp
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| Operating System | Red Hat Enterprise Linux 10 |
| Protocol | Modbus TCP (pymodbus 3.6.9) |
| Backend | Python 3.12, Flask |
| Database | SQLite |
| Frontend | HTML, CSS, JavaScript |

---

## Project Structure

```
mini-scada/
├── README.md              # This file
├── requirements.txt       # Python dependencies
├── .gitignore             # Ignores DB and cache files
├── modbus_server.py       # RTU simulator + Modbus TCP server
├── modbus_client.py       # Polls Modbus server, logs to DB, raises alarms
├── app.py                 # Flask web dashboard + REST API
└── templates/
    └── index.html         # Live dashboard UI
```

---

## How to Run

### 1. Clone the repository

```bash
git clone https://github.com/yourusername/mini-scada.git
cd mini-scada
```

### 2. Install dependencies

```bash
pip3 install pymodbus==3.6.9 flask --break-system-packages
```

### 3. Start all three components (open 3 terminals)

```bash
# Terminal 1 — RTU Simulator (Modbus TCP Server)
python3 modbus_server.py

# Terminal 2 — Data Collector + Alarm Engine
python3 modbus_client.py

# Terminal 3 — Web Dashboard
python3 app.py
```

### 4. Open the dashboard in your browser

```
http://localhost:8080
```

---

## Features

- Live readings — voltage (V), current (A), temperature (°C) via real Modbus TCP
- Alarm engine — configurable thresholds, fires alarms on HIGH/LOW conditions
- Alarm acknowledgement — operator can acknowledge active alarms from the dashboard
- Reading history — last 50 readings displayed in a log table
- Auto-refresh — dashboard updates every 3 seconds automatically

---

## Dashboard Preview

> Live voltage, current, temperature cards update in real time.
> Cards turn red when values exceed thresholds.
> Alarm table shows ACTIVE and ACKNOWLEDGED alarms with timestamps.

---

## Alarm Thresholds (configurable in `modbus_client.py`)

| Parameter | Min | Max |
|---|---|---|
| Voltage | 210.0 V | 245.0 V |
| Current | 5.0 A | 25.0 A |
| Temperature | 0.0 °C | 90.0 °C |

---

## Roadmap

### Phase 1 — Fix the Foundation
- [x] Proper Modbus register mapping (40001 → Voltage, 40002 → Current, 40003 → Temperature)
- [x] systemd service files for auto-start on boot (`modbus-server.service`, `modbus-client.service`, `dashboard.service`)

### Phase 2 — Make it SCADA-Like
- [ ] Multi-device simulation (Feeder A, Transformer T1, Substation S1)
- [ ] Smart alarm engine — full lifecycle (ACTIVE → ACKNOWLEDGED → CLEARED)
- [ ] Alarm priority levels (Low / Medium / High)
- [ ] Historian with trend graphs (Chart.js — last 1 hour / 24 hours)

### Phase 3 — Differentiate
- [ ] Failure simulation — device offline, data freeze, communication timeout detection
- [ ] Security hardening — firewalld rules, restricted service user, dashboard login
- [ ] Sequence of Events (SOE) log

### Phase 4 — Real Protocols
- [ ] IEC 60870-5-104 support using lib60870
- [ ] DNP3 basics

---

## Why This Project

Built as part of learning Linux (RHEL 10) in the context of SCADA, DMS, and OMS
systems — the same systems used in power distribution, grid automation, and
substation control.

The goal is to understand not just Linux commands, but how Linux underpins
real industrial systems — from process communication (Modbus TCP) to service
management (systemd) to network security (firewalld).

---

## Author

**Akash** — Intern, ACSIS  
Built on Red Hat Enterprise Linux 10  
May 2026
