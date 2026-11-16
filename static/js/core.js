// core.js - shared helpers, state, the breaker/switch dialog and the refresh loop.
// Every console registers a function in CONSOLES; refresh() runs the active one.

const PRIO  = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'];
const SHORT = {CRITICAL: 'CRIT', HIGH: 'HIGH', MEDIUM: 'MED', LOW: 'LOW'};
const STATE = {ACTIVE_UNACK: 'ACTIVE / UNACK', ACTIVE_ACK: 'ACTIVE / ACK',
               RTN_UNACK: 'NORMAL / UNACK', RTN_ACK: 'NORMAL / ACK'};
const PCOL  = {CRITICAL: '#f85149', HIGH: '#f0883e', MEDIUM: '#e3b341', LOW: '#58a6ff'};

const $   = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g,
  c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));

const App = {
  console:  'soc',
  stations: [],          // /api/substations
  details:  {},          // station id -> last /api/substations/<id>
  cycle:    {},          // promises fetched during the current refresh
  errUntil: 0,
};
const CONSOLES = {};     // name -> async function run on every refresh
const CONSOLE_HOOKS = {}; // name -> {show(), hide()} when a console is shown / left

async function api(url, opts) {
  const r = await fetch(url, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}

function postJSON(url, body) {
  return api(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                   body: JSON.stringify(Object.assign({operator: $('op').value || 'operator'}, body))});
}

// One fetch per URL per refresh cycle, shared by every view that needs it
function cached(url) {
  if (!App.cycle[url]) App.cycle[url] = api(url);
  return App.cycle[url];
}

async function detail(id) {
  const d = await cached('/api/substations/' + id);
  App.details[id] = d;
  return d;
}

function showErr(msg, hold) {
  $('err').textContent = msg; $('err').hidden = false;
  if (hold) App.errUntil = Date.now() + 4000;
}

function chipsFor(by) {
  return PRIO.map(p => `<span class="chip ${p}${by[p] ? '' : ' zero'}">${SHORT[p]} ${by[p]}</span>`).join('');
}

function renderSummary(s) {
  let h = chipsFor(s.by_priority);
  if (s.unacked_manual_alarms) h += `<span class="chip UNACK">UNACK ${s.unacked_manual_alarms}</span>`;
  $('summary').innerHTML = h;
}

// ─── Breaker / switch dialog ─────────────────────────────────────────
let mdev = null, armed = null;

const allDevices = () => Object.values(App.details).flatMap(d => [d.transformer].concat(d.feeders));

function openModal(slave) {
  mdev = allDevices().find(e => String(e.slave_id) === String(slave));
  if (!mdev) return;
  armed = null; $('m-msg').textContent = ''; drawModal(); $('modal').hidden = false;
}

function switchDev(id) {
  const w = FMap.last && FMap.last.switches.find(x => x.id === id);
  return w && {switch_id: w.id, name: `${w.name} (${w.id})`, breaker_state: w.state, normal: w.normal};
}

function openSwitch(id) {
  mdev = switchDev(id);
  if (!mdev) return;
  armed = null; $('m-msg').textContent = ''; drawModal(); $('modal').hidden = false;
}

function drawModal() {
  $('m-title').textContent = mdev.name;
  $('m-state').innerHTML = mdev.switch_id
    ? `Switch <span class="bk ${mdev.breaker_state}">${mdev.breaker_state}</span> | Normal state ${mdev.normal}`
    : `Breaker <span class="bk ${mdev.breaker_state}">${mdev.breaker_state}</span> | Supply ${mdev.energised ? 'LIVE' : 'DEAD'}`;
  ['OPEN', 'CLOSE'].forEach(c => {
    const b = $('m-' + c.toLowerCase());
    b.disabled = (c === 'OPEN' && mdev.breaker_state === 'OPEN') || (c === 'CLOSE' && mdev.breaker_state === 'CLOSED');
    b.className = armed === c ? 'armed' : '';
    b.textContent = armed === c ? 'CONFIRM ' + c : c;
  });
}

async function sendCmd(c) {
  if (armed !== c) { armed = c; drawModal(); return; }   // first click selects, second confirms
  try {
    const r = mdev.switch_id
      ? await postJSON('/api/control/switch', {switch_id: mdev.switch_id, command: c})
      : await postJSON('/api/control/breaker', {slave_id: mdev.slave_id, command: c});
    $('m-msg').textContent = 'Command #' + r.command_id + ' (' + c + ') sent...';
    trackCommand(r.command_id, c);
  } catch (e) { $('m-msg').textContent = 'Refused: ' + e.message; }
  armed = null; drawModal();
}

async function trackCommand(id, c) {
  for (let i = 0; i < 12; i++) {
    await new Promise(r => setTimeout(r, 1000));
    try {
      const st = await api('/api/control/commands/' + id);
      if (st.status === 'EXECUTED') {
        $('m-msg').textContent = 'Command #' + id + ' ' + c + ' executed (DNP3 SELECT + OPERATE confirmed). The diagram updates on the next poll.';
        return;
      }
      if (st.status === 'FAILED' || st.status === 'EXPIRED') {
        $('m-msg').textContent = 'Command #' + id + ' ' + st.status + ': ' + (st.result || '');
        return;
      }
    } catch (e) { return; }
  }
  $('m-msg').textContent = 'Command #' + id + ' still pending. Is the control service running?';
}

function syncModal() {
  if ($('modal').hidden || !mdev) return;
  const n = mdev.switch_id ? switchDev(mdev.switch_id)
          : allDevices().find(e => e.slave_id === mdev.slave_id);
  if (n) { mdev = n; drawModal(); }
}

// ─── Refresh loop ────────────────────────────────────────────────────
let refreshing = false, again = false;

async function refresh() {
  if (refreshing) { again = true; return; }
  refreshing = true;
  try {
    App.cycle = {};
    const [sum, subs] = await Promise.all([cached('/api/alarms/summary'), cached('/api/substations')]);
    App.stations = subs;
    renderSummary(sum);
    if (CONSOLES[App.console]) await CONSOLES[App.console]();
    syncModal();
    if (Date.now() > App.errUntil) $('err').hidden = true;
    $('upd').textContent = new Date().toLocaleTimeString();
    $('upd').title = 'Last successful update';
  } catch (e) {
    showErr('API not reachable: ' + e.message);
  } finally {
    refreshing = false;
    if (again) { again = false; refresh(); }
  }
}

function startCore() {
  $('m-open').addEventListener('click', () => sendCmd('OPEN'));
  $('m-close').addEventListener('click', () => sendCmd('CLOSE'));
  $('m-cancel').addEventListener('click', () => { $('modal').hidden = true; });
  $('modal').addEventListener('click', e => { if (e.target.id === 'modal') $('modal').hidden = true; });
  // Any breaker symbol in any SLD opens the dialog
  document.addEventListener('click', e => {
    const r = e.target.closest('.sld:not(.map) .brk');
    if (r) openModal(r.dataset.slave);
  });
  refresh();
  setInterval(refresh, 3000);
}
