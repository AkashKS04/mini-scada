# CLAUDE.md - Mini SCADA (project handoff)

Read this first. It is the project's memory: what exists, how it fits together,
how to deploy safely and what went wrong before. It was written from the design
conversation, so verify anything important against the code before relying on it.

## What this is
A Linux SCADA simulation on RHEL 10, built by Akash (ACSIS, SCADA/DMS/OMS domain)
to show real utility concepts: Modbus TCP monitoring, DNP3 control, a four-level
alarm engine, historian, failure detection, SOE log and a station HMI with a
single-line diagram. Three 33/11kV Bengaluru substations, 18 simulated devices.
GitHub: github.com/AkashKS04/mini-scada. It is a learning/portfolio project that
uses no real utility data. Keep it that way.

## Architecture
simulator/modbus_server.py   18 Modbus slaves (RTU simulator) on :5020, plus a DNP3
                             outstation on 127.0.0.1:20000, plus a failure injector
engine/modbus_client.py      polls all slaves every ~3 s, writes SQLite, runs the alarm engine
engine/dnp3_controller.py    DNP3 master: executes queued breaker commands (SELECT then OPERATE)
api/app.py                   Flask REST API on 127.0.0.1:8080, serves templates/hmi.html
lib/dnp3lite.py              pure-Python DNP3 subset (master + outstation), shared
config/substations.json      SINGLE SOURCE OF TRUTH: devices, limits, priorities, load nodes
templates/hmi.html           station HMI (tiles, SVG single-line diagram, equipment table, alarms)
templates/index.html         OLD Phase 3 dashboard, unused
systemd/                     copies of the service unit files

Data flow: monitoring = simulator -(Modbus)-> client -> scada.db -> API -> HMI.
Control = HMI -> API (queues a row in control_commands) -> dnp3_controller
-(DNP3)-> outstation -> apply_breaker_command() -> breaker register -> next Modbus poll.

## Stack and pins
RHEL 10, Python 3.12, Flask, SQLite. **pymodbus must stay at 3.6.9.** Version 3.13 renamed
classes and broke the datastore API. Packages are installed system-wide with
`sudo pip3 install ... --break-system-packages` so the `scada` user can import them.

## Addressing
Modbus slave ids: SS-01 transformer 1, feeders 2-6. SS-02: 7, 8-12. SS-03: 13, 14-18.
Registers (same for every slave): 40001 voltage kV x100, 40002 current A x10,
40003 power MW x100, 40004 power factor x100, 40005 temperature C x10 (transformer only),
40006 frequency Hz x100, 40100 breaker state (0 OPEN, 1 CLOSED, 2 TRIPPED, 3 UNKNOWN).
DNP3: master address 1, outstation address 100 + substation number (SS-01 = 101).
Point index 0 = transformer breaker, N = feeder N breaker. CROB code 0x81 = trip/OPEN,
0x41 = close. Select-before-operate, select timeout 10 s.
It is a SUBSET: no class polls, events, unsolicited responses or Secure Authentication.
Never describe it as a full DNP3 stack. It was checked against Wireshark's DNP3 decoder.

## Database (scada.db, created by modbus_client.init_db)
substations, devices, readings, alarms, soe_log, device_status, control_commands.
CREATE TABLE IF NOT EXISTS does not migrate. After a schema change, delete scada.db.
Readings grow ~2M rows/day, so a retention job is still to do.

## Alarm rules (core requirement from the owner)
- Four limits per parameter (high_high, high, low, low_low) with a priority each, from the JSON.
- CRITICAL and HIGH: the operator must acknowledge, then clear. They never auto-clear.
  When the value recovers they stay open as "returned to normal" (rtn_timestamp set).
- MEDIUM and LOW: auto-clear when the condition resolves.
- One alarm per (device, parameter, limit type). The client tracks open ones in memory and
  calls sync_open_alarms() each cycle so alarms cleared in the HMI can re-raise if still in breach.
- Types: ANALOG_HIGH_HIGH/HIGH/LOW/LOW_LOW, OFFLINE (3 failed polls), FREEZE (5 identical
  polls, skipped when the device is de-energised), BREAKER_TRIP, BREAKER_OPEN (operator
  command found in control_commands) and UNAUTHORISED_CONTROL (breaker opened with no command:
  Modbus has no authentication, so this is detection, not prevention).
- API enforces: clear is refused with 409 until an ack_required alarm is acknowledged.

## Services (all User=scada)
scada-server -> scada-client -> scada-dashboard, and scada-control requires server + client.
Restarting scada-server restarts the dependants. Unit files are in /etc/systemd/system/.
Firewalld: rich rules allow 5020 and 8080 from 127.0.0.1 only.

## Deploy rules (these caused most of the pain)
- Files live in /home/akash/mini-scada owned scada:scada, mode 770. After copying or
  editing files, re-run `sudo chown -R scada:scada /home/akash/mini-scada` and
  `sudo chmod -R 770 /home/akash/mini-scada`, then restart the affected service.
- Running scripts by hand as akash can leave root- or akash-owned files (scada.db) that the
  scada user cannot write. Chown again before restarting services.
- Flask must be started with debug=False (the debugger allows remote code execution).
- `git config --global --add safe.directory /home/akash/mini-scada` is needed after the
  ownership change.
- The HMI is served from templates/hmi.html. Hard-refresh the browser (Ctrl+Shift+R) after changes.

## Simulator behaviour to know
LOAD_CEILING 0.88 keeps normal peak load below the HIGH limits. FAILURE_GAP (300-600 s) sets
how often offline / freeze / trip failures are injected. Values follow breaker state: open
feeder = 0 A and 0 MW; open transformer = whole bus dead. Spike probability is 0.001.

## Mistakes already made (do not repeat)
- Clearing logic once dropped tracking for manual alarms, so breaches raised duplicates. Fixed.
- Constant zeros on a de-energised feeder tripped the freeze detector. Fixed (is_energised).
- pymodbus 3.13 incompatibility. Pinned to 3.6.9.
- Claiming something "works" from a first look. Show test output or a screenshot first.

## Status
Done: Modbus monitoring, 4-level alarms, historian + /api/trends, failure detection, SOE,
station HMI with SLD, DNP3 control with confirm step, unauthorised-control detection,
bulk "clear returned-to-normal" button, firewalld + restricted service user.
Next: SS-01 feeder map (/api/feedermap/SS-01 already returns nodes and impact; needs the
HMI view with fault animation). Later ideas: state-based alarm suppression for out-of-service
equipment, SOE tab in the HMI, historian retention, README refresh, login for the HMI.
Known gaps: operator name is free text (no real authentication), DNP3 is a subset.

## Working style that suits the owner
Complete, copy-ready files; explain the why briefly; test before claiming; be honest about
limits. The owner presents this project to colleagues, so accuracy about what it is matters.
