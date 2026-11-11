# CLAUDE.md - Mini SCADA (project handoff)

Read this first. It is the project's memory: what exists, how it fits together,
how to deploy safely and what went wrong before. It was written from the design
conversation, so verify anything important against the code before relying on it.

## What this is
A Linux SCADA simulation on RHEL 10, built by Akash (ACSIS, SCADA/DMS/OMS domain)
to show real utility concepts: Modbus TCP monitoring, DNP3 control, a four-level
alarm engine, historian, failure detection, SOE log and a station HMI with a
single-line diagram. Five 33/11kV Bengaluru substations, 41 simulated devices
(5 transformers, 36 feeders: 5/6/7/8/10 per station).
GitHub: github.com/AkashKS04/mini-scada. It is a learning/portfolio project that
uses no real utility data. Keep it that way.

## Architecture
simulator/modbus_server.py   41 Modbus slaves (RTU simulator) on :5020, plus a DNP3
                             outstation on 127.0.0.1:20000, plus a failure injector
engine/modbus_client.py      polls all slaves (one block read of 40001-40006 + one breaker read each;
                             ~3.1 s cycle), writes SQLite once per cycle, runs the alarm engine
engine/dnp3_controller.py    DNP3 master: executes queued breaker commands (SELECT then OPERATE)
api/app.py                   Flask REST API on 127.0.0.1:8080, serves templates/hmi.html
lib/dnp3lite.py              pure-Python DNP3 subset (master + outstation), shared
config/substations.json      SINGLE SOURCE OF TRUTH: devices, limits, priorities, load nodes
templates/hmi.html           station HMI (tiles, SVG single-line diagram, equipment table, alarms).
                             The SLD lays out any feeder count; >5 feeders widens it and it scrolls.
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
Modbus slave ids are contiguous, transformer first then its feeders:
SS-01 Rajajinagar 1, 2-6 | SS-02 Jayanagar 7, 8-13 | SS-03 Whitefield 14, 15-21 |
SS-04 Hebbal 22, 23-30 | SS-05 Electronic City 31, 32-41.
Adding a feeder renumbers every later station: edit the JSON, then delete scada.db.
Registers (same for every slave): 40001 voltage kV x100, 40002 current A x10,
40003 power MW x100, 40004 power factor x100, 40005 temperature C x10 (transformer only),
40006 frequency Hz x100, 40100 breaker state (0 OPEN, 1 CLOSED, 2 TRIPPED, 3 UNKNOWN).
DNP3: master address 1, outstation address 100 + substation number (SS-01 = 101 ... SS-05 = 105).
Point index 0 = transformer breaker, N = feeder N breaker. CROB code 0x81 = trip/OPEN,
0x41 = close. Select-before-operate, select timeout 10 s.
It is a SUBSET: no class polls, events, unsolicited responses, time sync, file transfer,
confirmed link frames or Secure Authentication. The outstation also accepts DIRECT OPERATE (0x05);
the controller always uses SELECT then OPERATE.
Never describe it as a full DNP3 stack. It was checked against Wireshark's DNP3 decoder.

## Database (scada.db, created by modbus_client.init_db)
substations, devices, readings, current_values, alarms, soe_log, device_status, control_commands.
CREATE TABLE IF NOT EXISTS does not migrate. After a schema change, delete scada.db.
WAL journal mode (scada.db-wal / -shm files sit next to it and need the same ownership).
The client holds one idle "wal_keeper" connection so those files always exist while it runs.
NEVER open scada.db as akash (python, sqlite3) while the services run: if the side files are
missing you recreate them owned by akash and scada-client dies with "attempt to write a
readonly database". Check data through the API (curl localhost:8080/api/...) or
`sudo -u scada python3 ...`.
current_values = real-time table, one row per (slave_id, parameter); the API reads live values here.
readings = historian, slim (timestamp, slave_id, parameter, value), ~5.9M rows/day at 82 bytes/row.
The client purges rows older than RETENTION_HOURS = 30 every 10 min (~600 MB steady state).
/api/trends goes back 24 h at most. Free disk: 4.1 GB on 2026-11-09, 2.7 GB on 2026-11-11
(the drop was outside this project). Lower RETENTION_HOURS before raising anything else.

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
- POST /api/alarms/clear-rtn acks + clears only alarms with rtn_timestamp set (bulk HMI button).
- BREAKER_OPEN counts as authorised only if an OPEN command was EXECUTING/EXECUTED in the last 60 s.
  A CLOSE with no command is not detected.
- Analog alarms still fire on de-energised equipment (only FREEZE is suppressed).
- Power factor varies so little in the simulator that it repeats 5 polls by chance and raises
  MEDIUM FREEZE alarms (about half of all freeze alarms). Pre-existing, not yet fixed.

## Control safety (dnp3_controller.py)
Commands expire after COMMAND_TTL = 30 s. Status: PENDING -> EXECUTING -> EXECUTED | FAILED | EXPIRED.
A command is claimed before sending, so it never runs twice; one left EXECUTING after a controller
restart is marked FAILED, never retried. The API refuses (409) commands to offline devices and
commands for the state the breaker is already in.

## Services (all User=scada)
scada-server -> scada-client -> scada-dashboard, and scada-control requires server + client.
Restarting scada-server restarts the dependants. Unit files are in /etc/systemd/system/.
Firewalld: rich rules allow 5020 and 8080 from 127.0.0.1 only. The API (8080) and DNP3 (20000)
bind 127.0.0.1, but Modbus binds 0.0.0.0:5020, so firewalld is the only thing keeping it local.
systemd/ holds copies of all four installed units; re-copy them after changing /etc/systemd/system.

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
how often offline / freeze / trip failures are injected. Injected trips auto-reclose after 25-50 s;
freeze injection only hits voltage, current and MW. Values follow breaker state: open
feeder = 0 A and 0 MW; open transformer = whole bus dead. Spike probability is 0.001.
Transformer values are generated on their own, NOT as the sum of its feeders, so the SLD
transformer MW does not equal the feeder total. Known realism gap.

## Mistakes already made (do not repeat)
- Clearing logic once dropped tracking for manual alarms, so breaches raised duplicates. Fixed.
- Constant zeros on a de-energised feeder tripped the freeze detector. Fixed (is_energised).
- pymodbus 3.13 incompatibility. Pinned to 3.6.9.
- Claiming something "works" from a first look. Show test output or a screenshot first.
- .gitignore (Python template) ignored lib/, so lib/dnp3lite.py was never committed. Fixed.
- Files edited by Claude Code end up owned by akash. Always run the chown/chmod lines before restarting.
  On 2026-11-09 this had left scada-server crash-looping (Permission denied) for a week.
- 2026-11-11: Claude opened scada.db read-only as akash to check it, recreated the WAL side
  files as akash, and crash-looped scada-client for ~40 s. Fixed with wal_keeper + the rule above.
- The old live-value query (MAX(id) GROUP BY over readings) took 2 s at 322k rows. Never read live
  values from the historian; use current_values.

## Status
Done: Modbus monitoring, 4-level alarms, historian + /api/trends, failure detection, SOE,
station HMI with SLD, DNP3 control with confirm step, unauthorised-control detection,
bulk "clear returned-to-normal" button, firewalld + restricted service user,
5 substations / 41 devices, real-time table, historian retention.
Next: SS-01 feeder map (/api/feedermap/SS-01 already returns nodes and impact; needs the
HMI view with fault animation). Later ideas: state-based alarm suppression for out-of-service
equipment, SOE tab in the HMI, README refresh, login for the HMI, transformer = sum of feeders,
fix power-factor false freezes.
Known gaps: operator name is free text (no real authentication), DNP3 is a subset.

## Working style that suits the owner
Complete, copy-ready files; explain the why briefly; test before claiming; be honest about
limits. The owner presents this project to colleagues, so accuracy about what it is matters.
