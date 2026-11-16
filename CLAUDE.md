# CLAUDE.md - Mini SCADA (project handoff)

Read this first. It is the project's memory: what exists, how it fits together,
how to deploy safely and what went wrong before. It was written from the design
conversation, so verify anything important against the code before relying on it.

## What this is
A Linux SCADA simulation on RHEL 10, built by Akash (ACSIS, SCADA/DMS/OMS domain)
to show real utility concepts: Modbus TCP monitoring, DNP3 control, a four-level
alarm engine, historian, failure detection, SOE log and a station HMI with a
single-line diagram, and FLISR on a feeder network map. Five 33/11kV Bengaluru
substations, 41 simulated devices (5 transformers, 36 feeders: 5/6/7/8/10 per station),
plus a distribution-automation RTU for SS-01's 33 RMU/tie switches.
GitHub: github.com/AkashKS04/mini-scada. It is a learning/portfolio project that
uses no real utility data. Keep it that way.

## Architecture
simulator/modbus_server.py   41 Modbus slaves (RTU simulator) on :5020, plus a DNP3
                             outstation on 127.0.0.1:20000, a failure injector, and a
                             test-set port on 127.0.0.1:20001 (inject a feeder fault on demand)
engine/modbus_client.py      polls all slaves (one block read of 40001-40006 + one breaker read each;
                             ~3.1 s cycle), writes SQLite once per cycle, runs the alarm engine
engine/dnp3_controller.py    DNP3 master: executes queued breaker commands (SELECT then OPERATE)
api/app.py                   Flask REST API on 127.0.0.1:8080, serves templates/hmi.html
lib/dnp3lite.py              pure-Python DNP3 subset (master + outstation), shared
lib/topology.py              SS-01 network model (RMU switches, ties, energisation), shared by all
lib/flisr.py                 FLISR / restore-normal planner + state loader + create_plan
config/substations.json      SINGLE SOURCE OF TRUTH: devices, limits, priorities, load nodes
lib/geo.py                   Storm Watch geography: substations at config lat/lon, SS-01 feeder
                             routes placed around the substation from the schematic (illustrative)
simulator/storm.py           thunderstorm + lightning model, run inside the simulator
templates/hmi.html           page shell only: header, console nav, Displays/Panels menus, dialog
static/css/hmi.css           all styles
static/js/core.js            helpers, shared state (App), breaker/switch dialog, refresh loop
static/js/views.js           SLD, station strip, Alarm / Event (SOE) / Equipment panels
static/js/fmap.js            Feeder schematic panel: network map, supply impact, FLISR
static/js/workspace.js       System Operator Console: zones, drag and drop, split/tabs, layout memory
static/js/trend.js           Trending console (canvas, real-time pens)
static/js/storm.js           Storm Watch console (canvas map, lightning animation)
                             Script load order matters: core, views, fmap, trend, storm, workspace.
templates/index.html         OLD Phase 3 dashboard, unused
systemd/                     copies of the service unit files

Data flow: monitoring = simulator -(Modbus)-> client -> scada.db -> API -> HMI.
FLISR = fault -> client logs FAULT_LOCATED -> plan (API on operator approval, or controller in
auto mode) -> switching_plans row -> controller queues one control_commands row per step,
waits for Modbus telemetry to confirm it, then the next step.
Control = HMI -> API (queues a row in control_commands) -> dnp3_controller
-(DNP3)-> outstation -> apply_breaker_command() -> breaker register -> next Modbus poll.

## HMI consoles (one page, three consoles in the header nav)
- System Operator Console: displays zone (substation SLDs) on top, panels zone below. Each zone
  shows 1 item full width, 2 side by side, 3+ as Chrome-style tabs (zone classes m-one / m-split /
  m-tabs). Items come from the Displays / Panels menus by drag and drop or click; tabs and pane
  headers drag to reorder; x closes. Panels: alarms, events, feeder, equipment. Default layout:
  SS-01 + alarms. Layout is kept in localStorage (mini-scada-soc-v1). Panes are kept alive while
  open and only visible ones fetch data. Equipment follows WS.focus (display last clicked).
- Trending: pens from /api/trends (history) + /api/live (every refresh), one strip per unit,
  SOE markers for the trended devices, windows 5 min - 24 h, pens saved in localStorage.
- Storm Watch: /api/storm/geo once, /api/storm every second (LLS-style stroke feed, proximity
  warnings, trip/stroke correlation), SS-01 network coloured from /api/feedermap.
- Canvas consoles draw on requestAnimationFrame AND on every data update (background tabs and
  headless Firefox throttle animation frames).
- CSS class names are global: `.fault` (map marker, hidden unless .on) once hid the SOE event
  labels, and a zone class `tabs` once collided with the tab strip. Prefix new classes.
- Static files are versioned by mtime (?v=) from the template, so no stale JS after a deploy.

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
Wire addresses are 0-based (Modbus convention, what Wireshark shows): 40001-40006 = 0-5,
40100 = 99. pymodbus adds 1 internally on both reads and writes; leave the datastore at 0.
Values are clamped to 0-65535: one out-of-range register makes the whole read fail.
An OFFLINE device answers Modbus exception 0x0B (Gateway Target Device Failed to Respond).
40007 (addr 6) = fault section from the relay fault locator: 0 none, N = cable section feeding
load node N (feeders without load_nodes have 3 sections). Kept in current_values only, not the
historian; changes are logged in the SOE as FAULT_LOCATED / FAULT_CLEARED.
DNP3: master address 1, outstation address 100 + substation number (SS-01 = 101 ... SS-05 = 105).
Point index 0 = transformer breaker, N = feeder N breaker. CROB code 0x81 = trip/OPEN,
0x41 = close. Select-before-operate, select timeout 10 s.
SS-01 RMU/tie switches: DNP3 outstation 101 points 10-42 (10 + switch index); status on Modbus
slave 42 (DA RTU), register address = switch index, 1 = CLOSED. Switch ids F{n}-N{i}-IN/OUT and
NOP-1..4 (ties from config "ties"). Section k of a feeder = OUT of node k-1 (breaker for k=1) to
IN of node k. Normal: IN/OUT closed, ties open.
It is a SUBSET: no class polls, events, unsolicited responses, time sync, file transfer,
confirmed link frames or Secure Authentication. The outstation also accepts DIRECT OPERATE (0x05);
the controller always uses SELECT then OPERATE.
Never describe it as a full DNP3 stack. It was checked against Wireshark's DNP3 decoder.

## Database (scada.db, created by modbus_client.init_db)
substations, devices, readings, current_values, alarms, soe_log, device_status, control_commands,
switching_plans (FLISR/RESTORE plans, steps JSON, status EXECUTING/COMPLETED/FAILED/REJECTED/
CANCELLED), settings (flisr_auto). New tables are created on the existing db; no delete needed.
Switch states live in current_values under slave_id 42, parameter = switch id.
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
- FREEZE is checked only on voltage, current and power_mw (FREEZE_PARAMETERS). PF and
  frequency at 0.01 resolution and transformer oil temperature can legitimately hold steady.

## FLISR (lib/flisr.py, executed by dnp3_controller.py)
- Plan for a tripped feeder with fault section k: open both ends of section k, reclose the
  breaker (not for k = 1), close a tie for nodes k..n if the supporting feeder can carry it:
  now MW + transfer <= 0.9 x its power HIGH_HIGH (emergency rating). Transfer = pre-fault MW
  (historian, minute before FAULT_LOCATED) x connected-kW share. If not, partial: open IN of
  node j and backfeed only j..n. The final state is simulated; any energised fault or parallel
  feeders rejects the plan. Backfeeding can push the supporting feeder above HIGH (alarm): real.
- Restore normal (only after Repair): open ties touching the feeder (break before make), close
  its switches from the substation outwards, then the breaker. Restore proposals use
  abnormal(ties=False): a feeder that only backfeeds through a tie must not get one.
- Manual mode: operator approves in the HMI (plan recomputed on the server). Auto mode
  (settings flisr_auto=1): controller makes one plan per FAULT_LOCATED event. One executing
  plan per station. Step confirm timeout 20 s; controller restart fails executing plans.
- DMS interlocks in the API: refuse a CLOSE (breaker or switch) that would energise a faulted
  section or parallel two feeders. The simulator still trips a breaker that closes onto a fault.

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
- The HMI is served from templates/hmi.html. Flask (debug=False) caches the template, so after
  editing it RESTART scada-dashboard, then hard-refresh the browser (Ctrl+Shift+R).

## Simulator behaviour to know
Electrical model (the SLD numbers add up): feeder V = its station's 11 kV bus, P from the
load curve, I = P/(sqrt3 V pf). Transformer P, Q = sum of feeders x losses (1.01, 1.03),
I = S/(sqrt3 V), pf = P/S, oil temp = 30 + 50 x loading^1.6 reached with a 600 s lag.
Transformers 12.5/16/20/20/20 MVA; feeder loads are scaled so each station peaks at ~75%.
Feeder current limits = their MW limits converted at 11 kV, pf 0.9. A feeder treats its bus as
dead until the transformer has published a live bus voltage (avoids I = P/0 after a reclose).
LOAD_CEILING 0.88 keeps normal peak load below the HIGH limits. FAILURE_GAP (300-600 s) sets
how often offline / freeze / trip failures are injected. Injected trips auto-reclose after 25-50 s;
freeze injection only hits voltage, current and MW. Values follow breaker state: open
feeder = 0 A and 0 MW; open transformer = whole bus dead. Spike probability is 0.001.
Storm (simulator/storm.py, started/stopped through the test set, POST /api/sim/storm): cells
spawn upwind of a target (the feeder-map station's network centre 60 % of the time), move at
30 km/h x TIME_LAPSE 8, live 5-8 min, storm ends after 20 min. Strokes: ~40 % cloud-to-ground
(88 % negative), rest in-cloud. A CG stroke within 0.4 km of an SS-01 section causes a fault
there (60 % permanent, else transient_trip with 5-9 s auto-reclose); within 2.5 km of another
substation a transient trip. Faults are rate-limited (fault_gap). The stroke's `effect` is
ground truth for debugging; the API correlation (time <= 12 s before the trip was seen,
distance <= 3 km) never uses it. Auto FLISR waits RECLOSE_WAIT = 12 s after FAULT_LOCATED so a
transient fault can reclose first.
Faults: trip_feeder() sets 40007 and trips the breaker. A fault stays until repaired (test set
"repair", POST /api/sim/repair, SOE TEST_FAULT_REPAIRED); a breaker CLOSE no longer clears it,
and protection() trips any breaker that feeds a faulted section. Random injector trips are
transient: they clear 40007 and auto-reclose after 25-50 s unless the breaker was operated.
POST /api/sim/fault forwards to the test set and logs TEST_FAULT_INJECTED with the operator.
SS-01 feeder MW = demand of the nodes its breaker supplies now (incl. nodes backfed through a
tie) + meter noise added AFTER the split (else a part-fed feeder reads constant and FREEZEs).
OFFLINE sets RtuContext.offline, so validate() refuses requests; never remove a slave from the
server context (pymodbus drops unknown unit ids silently -> 3 s client timeouts, 9 s cycles).

## Mistakes already made (do not repeat)
- Clearing logic once dropped tracking for manual alarms, so breaches raised duplicates. Fixed.
- Constant zeros on a de-energised feeder tripped the freeze detector. Fixed (is_energised).
- pymodbus 3.13 incompatibility. Pinned to 3.6.9.
- Claiming something "works" from a first look. Show test output or a screenshot first.
- .gitignore (Python template) ignored lib/, so lib/dnp3lite.py was never committed. Fixed.
- Files edited by Claude Code end up owned by akash. Always run the chown/chmod lines before restarting.
  On 2026-11-09 this had left scada-server crash-looping (Permission denied) for a week.
- Until 2026-11-11 the OFFLINE failure only stopped register updates, so the server kept
  answering with stale values: OFFLINE alarms never fired and outages showed up as FREEZE.
- ModbusServerContext keeps a reference to the slaves dict; deleting from it deleted the
  simulator's own context too and crashed every thread. Caught in a scratchpad test.
- 2026-11-11: Claude opened scada.db read-only as akash to check it, recreated the WAL side
  files as akash, and crash-looped scada-client for ~40 s. Fixed with wal_keeper + the rule above.
- 2026-11-12: FLISR first offered "restore normal" for the feeder supplying a backfeed, which
  would have opened the tie and dropped the backfed customers. Fixed (abnormal(ties=False)).
- 2026-11-12: a screenshot looked unchanged after a CSS edit because Flask had cached the
  template. Restart the API process before judging an HMI change.
- The old live-value query (MAX(id) GROUP BY over readings) took 2 s at 322k rows. Never read live
  values from the historian; use current_values.

## Status
Done: Modbus monitoring, 4-level alarms, historian + /api/trends, failure detection, SOE,
station HMI with SLD, DNP3 control with confirm step, unauthorised-control detection,
bulk "clear returned-to-normal" button, firewalld + restricted service user,
5 substations / 41 devices, real-time table, historian retention, 0-based Modbus addressing,
electrically consistent simulator (transformer = sum of feeders), real OFFLINE failures,
SS-01 feeder network map (fault location, outage/restore wave, supply impact, test-set faults),
FLISR with RMU/tie switches, partial restoration, manual + automatic modes, restore normal,
operator console workspace (drag/drop, split, tabs), Event (SOE) panel, Trending console,
Storm Watch (simulated lightning causing faults, proximity warnings, trip/stroke correlation).
Next ideas: README refresh, load-flow based capacity check (today: MW
sums vs limits), state-based alarm suppression for out-of-service equipment, login for the HMI,
DNP3 event polling (outstation-timestamped SOE; today SOE times are poll times, ~3 s resolution).
Known gaps: operator name is free text (no real authentication), DNP3 is a subset, FLISR
capacity is a MW-sum check (no voltage-drop/load-flow), unauthorised-control detection covers
breakers only (not RMU switches).

## Testing the HMI without a person
Run a copy of the stack in the scratchpad on other ports (sed 5020/20000/20001/8080), add to the
COPY of hmi.html a window.onerror trap and <img src="/slow.gif?t=12"> (a test-only Flask route
that sleeps) so the load event waits for data; the same injected script can preset
localStorage (layout, console) from the query string, since headless cannot drag. Then:
firefox --headless --no-remote --profile <fresh dir> --window-size 1400,2400 --screenshot shot.png URL
Use a fresh profile each time (cache). Screenshots show end states, not the transitions.

## Working style that suits the owner
Complete, copy-ready files; explain the why briefly; test before claiming; be honest about
limits. The owner presents this project to colleagues, so accuracy about what it is matters.
