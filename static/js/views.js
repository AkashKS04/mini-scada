// views.js - single line diagram, station strip, and the alarm / event /
// equipment panels. Each panel has build(body) once and update() per refresh.

// ─── Single line diagram ─────────────────────────────────────────────
function ln(x1, y1, x2, y2, on, col, w) {
  let s = `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${on ? (col || '#3fb950') : '#484f58'}" stroke-width="${w || 3}"/>`;
  if (on) s += `<line class="flow" style="animation-delay:-${((Date.now() % 1200) / 1000).toFixed(2)}s" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="#e6edf3" stroke-opacity=".75" stroke-width="2" stroke-dasharray="4 10"/>`;
  return s;
}

function brk(x, y, e) {
  const st = e.breaker_state;
  const fill = {CLOSED: '#3fb950', OPEN: '#0d1117', TRIPPED: '#f85149'}[st] || '#e3b341';
  return `<rect class="brk${st === 'TRIPPED' ? ' trip' : ''}" data-slave="${e.slave_id}" x="${x - 10}" y="${y - 10}" width="20" height="20" rx="2" fill="${fill}" stroke="${st === 'OPEN' ? '#8b949e' : fill}" stroke-width="2"><title>${esc(e.name)} - ${st} (click to operate)</title></rect>`;
}

function vt(x, y, p, dec, unit, pre) {
  if (!p) return '';
  return `<text class="vt${p.alarm_priority ? ' v-' + p.alarm_priority : ''}" x="${x}" y="${y}">${pre || ''}${p.value.toFixed(dec)} ${unit}</text>`;
}

function badge(x, y, a) {
  if (!a.open) return '';
  return `<circle cx="${x}" cy="${y}" r="9" fill="${PCOL[a.worst]}"/><text x="${x}" y="${y + 4}" text-anchor="middle" style="fill:#0d1117;font-size:11px;font-weight:700">${a.open}</text>`;
}

function renderSLD(el, d) {
  // Feeders keep at least 170 px apart; stations with more than 5 get a wider
  // drawing that scrolls sideways instead of squeezing the text
  const t = d.transformer, tp = t.parameters, n = d.feeders.length;
  const W = Math.max(1000, 300 + (n - 1) * 170), cx = W / 2;
  const xs = d.feeders.map((_, i) => n > 1 ? 150 + i * (W - 300) / (n - 1) : cx);
  let s = `<svg viewBox="0 0 ${W} 500" style="--mw:${W}px" xmlns="http://www.w3.org/2000/svg">`;
  s += `<text class="mu" x="${cx + 15}" y="42">33 kV incomer</text>` + ln(cx, 28, cx, 76, true, '#58a6ff');
  const tc = t.energised ? '#3fb950' : '#484f58';
  s += `<circle cx="${cx}" cy="94" r="18" fill="none" stroke="${tc}" stroke-width="3"/><circle cx="${cx}" cy="114" r="18" fill="none" stroke="${tc}" stroke-width="3"/>`;
  s += `<text class="nm" x="${cx + 40}" y="84">${esc(t.name)}</text>`;
  s += vt(cx + 40, 102, tp.voltage, 2, 'kV', 'V ') + vt(cx + 40, 118, tp.current, 0, 'A', 'I ')
     + vt(cx + 40, 134, tp.power_mw, 2, 'MW', 'P ') + vt(cx + 40, 150, tp.temperature, 1, 'C', 'T ');
  s += ln(cx, 132, cx, 155, t.energised) + brk(cx, 165, t) + ln(cx, 175, cx, 200, t.energised) + badge(cx - 30, 165, t.alarms);
  s += ln(70, 200, W - 70, 200, t.energised, null, 6) + '<text class="mu" x="76" y="190">11 kV busbar</text>';
  d.feeders.forEach((f, i) => {
    const x = xs[i], P = f.parameters, parts = f.name.split(' — ');
    s += ln(x, 200, x, 242, t.energised) + brk(x, 252, f) + ln(x, 262, x, 418, f.energised);
    s += `<polygon points="${x - 8},418 ${x + 8},418 ${x},432" fill="${f.energised ? '#3fb950' : '#484f58'}"/>`;
    s += vt(x + 14, 292, P.voltage, 2, 'kV') + vt(x + 14, 310, P.current, 0, 'A')
       + vt(x + 14, 328, P.power_mw, 2, 'MW') + vt(x + 14, 346, P.power_factor, 2, '', 'PF ');
    s += badge(x + 26, 252, f.alarms);
    s += `<text class="nm" x="${x}" y="456" text-anchor="middle">${esc(parts[0])}</text>`
       + `<text class="vt" x="${x}" y="472" text-anchor="middle">${esc(parts[1] || '')}</text>`
       + `<text class="mu" x="${x}" y="488" text-anchor="middle">${esc(f.load_type)}</text>`;
  });
  el.innerHTML = s + '</svg>';
}

// Status strip above a substation display: replaces the old overview tiles
function renderStationStrip(el, id) {
  const s = App.stations.find(x => x.id === id);
  if (!s) return;
  const a = s.alarms_by_priority;
  el.innerHTML = `<span class="tn"><b>${esc(s.short_name)}</b> <span class="sub">${esc(s.id)} | ${esc(s.name)} | ${esc(s.location)}</span></span>
    <span class="chips">${chipsFor(a)}</span>
    <span class="tm">Unacked ${s.unacked_alarms} | Comms issues ${s.comms_issues} | Breakers open/tripped ${s.feeders_open_or_tripped}</span>`;
}

// ─── Alarm panel ─────────────────────────────────────────────────────
let rtnCount = 0, bulkArmed = false;

function drawBulk() {
  const b = $('bulk');
  if (!rtnCount) bulkArmed = false;
  b.disabled = !rtnCount;
  b.className = bulkArmed ? 'armed' : '';
  b.textContent = (bulkArmed ? 'CONFIRM clear ' : 'Clear returned-to-normal (') + rtnCount + (bulkArmed ? '' : ')');
}

const AlarmPanel = {
  title: () => 'Alarms',
  build(body) {
    body.innerHTML = `<div class="pbar"><select id="filter"><option value="">All substations</option></select>
        <button id="bulk" disabled>Clear returned-to-normal (0)</button></div>
      <div class="scroll"><table>
        <thead><tr><th>Raised</th><th>Station</th><th>Device</th><th>Priority</th>
          <th>Message</th><th>State</th><th>Action</th></tr></thead>
        <tbody id="alarms"></tbody></table></div>`;
    App.stations.forEach(s => $('filter').add(new Option(s.id + ' - ' + s.short_name, s.id)));
    $('alarms').addEventListener('click', async e => {
      const b = e.target.closest('button');
      if (!b || b.disabled) return;
      try { await postJSON('/api/alarms/' + b.dataset.id + '/' + b.dataset.act, {}); }
      catch (err) { showErr(err.message, true); }
      refresh();
    });
    $('bulk').addEventListener('click', async () => {
      if (!bulkArmed) { bulkArmed = true; drawBulk(); return; }   // second click confirms
      bulkArmed = false;
      const f = $('filter').value;
      try { await postJSON('/api/alarms/clear-rtn' + (f ? '?substation=' + encodeURIComponent(f) : ''), {}); }
      catch (err) { showErr(err.message, true); }
      refresh();
    });
    $('filter').addEventListener('change', () => { bulkArmed = false; refresh(); });
  },
  async update() {
    if ($('filter').options.length <= 1) App.stations.forEach(s => $('filter').add(new Option(s.id + ' - ' + s.short_name, s.id)));
    const f = $('filter').value;
    const list = await cached('/api/alarms' + (f ? '?substation=' + encodeURIComponent(f) : ''));
    rtnCount = list.filter(a => a.state === 'RTN_UNACK' || a.state === 'RTN_ACK').length;
    drawBulk();
    if (!list.length) { $('alarms').innerHTML = '<tr><td colspan="7" class="empty">No open alarms</td></tr>'; return; }
    $('alarms').innerHTML = list.map(a => {
      const hot = a.can_ack && (a.priority === 'CRITICAL' || a.priority === 'HIGH') ? 'hot' : '';
      return `<tr class="${hot}">
        <td>${esc(a.timestamp.slice(5))}</td><td>${esc(a.substation_id)}</td><td>${esc(a.device_name)}</td>
        <td><span class="chip ${a.priority}">${a.priority}</span></td>
        <td class="msg">${esc(a.message)}</td>
        <td class="st ${a.state}">${STATE[a.state] || a.state}</td>
        <td><button data-act="ack" data-id="${a.id}" ${a.can_ack ? '' : 'disabled'}>Ack</button>
            <button data-act="clear" data-id="${a.id}" ${a.can_clear ? '' : 'disabled'}>Clear</button></td>
      </tr>`;
    }).join('');
  },
};

// ─── Event panel (Sequence of Events) ────────────────────────────────
let evLastId = 0;

function evClass(t) {
  if (/FAULT|TRIP|UNAUTHORISED|FAILED|REJECTED/.test(t)) return 'ev-fault';
  if (/^SWITCH/.test(t)) return 'ev-sw';
  if (/FLISR|RESTORE/.test(t)) return 'ev-plan';
  if (/CONTROL/.test(t)) return 'ev-ctl';
  if (/ALARM_RAISED/.test(t)) return 'ev-alarm';
  if (/CLEARED|CLOSED|RESTORED|REPAIRED|COMPLETED/.test(t)) return 'ev-ok';
  return '';
}

const EventPanel = {
  title: () => 'Events (SOE)',
  build(body) {
    body.innerHTML = `<div class="pbar"><select id="evss"><option value="">All substations</option></select>
        <input id="evq" placeholder="Filter type or text" style="width:200px">
        <label class="sub"><input type="checkbox" id="evpause" style="width:auto"> Pause</label>
        <span class="sub">Timestamps are poll times (about 3 s resolution), not device time stamps.</span></div>
      <div class="scroll" style="max-height:460px;overflow-y:auto"><table class="ev">
        <thead><tr><th>Time</th><th>Station</th><th>Device</th><th>Event</th><th>Message</th><th>By</th></tr></thead>
        <tbody id="events"></tbody></table></div>`;
    App.stations.forEach(s => $('evss').add(new Option(s.id + ' - ' + s.short_name, s.id)));
    $('evss').addEventListener('change', refresh);
    $('evq').addEventListener('input', () => EventPanel.draw());
    evLastId = 0;
  },
  rows: [],
  async update() {
    if ($('evss').options.length <= 1) App.stations.forEach(s => $('evss').add(new Option(s.id + ' - ' + s.short_name, s.id)));
    if ($('evpause').checked) return;
    const ss = $('evss').value;
    this.rows = await cached('/api/soe?limit=300' + (ss ? '&substation=' + encodeURIComponent(ss) : ''));
    this.draw();
  },
  draw() {
    const q = $('evq').value.trim().toUpperCase();
    const rows = q ? this.rows.filter(e => (e.event_type + ' ' + e.message + ' ' + e.device_name).toUpperCase().includes(q)) : this.rows;
    const prev = evLastId;
    $('events').innerHTML = rows.slice(0, 200).map(e => `<tr class="${prev && e.id > prev ? 'new' : ''}">
        <td class="t">${esc(e.timestamp_ms.slice(5))}</td><td>${esc(e.substation_id)}</td><td>${esc(e.device_name)}</td>
        <td><span class="et ${evClass(e.event_type)}">${esc(e.event_type)}</span></td>
        <td class="msg">${esc(e.message)}</td><td class="sub">${esc(e.operator)}</td></tr>`).join('')
      || '<tr><td colspan="6" class="empty">No events</td></tr>';
    if (this.rows.length) evLastId = Math.max(evLastId, this.rows[0].id);
  },
};

// ─── Equipment panel (follows the focused substation display) ────────
function val(p, dec) {
  if (!p) return '<td class="num">-</td>';
  return `<td class="num${p.alarm_priority ? ' v-' + p.alarm_priority : ''}">${p.value.toFixed(dec)}</td>`;
}

const EquipmentPanel = {
  title: () => 'Equipment' + (WS.focus ? ' - ' + WS.focus : ''),
  build(body) {
    body.innerHTML = `<div class="sub" id="eqhint" style="margin-bottom:8px"></div><div class="scroll"><table>
      <thead><tr><th>Equipment</th><th>Breaker</th><th>Supply</th><th>Data quality</th>
        <th>Voltage kV</th><th>Current A</th><th>Power MW</th><th>PF</th><th>Temp C</th><th>Alarms</th></tr></thead>
      <tbody id="detail"></tbody></table></div>`;
  },
  async update() {
    if (!WS.focus) { $('detail').innerHTML = '<tr><td colspan="10" class="empty">Open a substation display</td></tr>'; return; }
    const d = await detail(WS.focus);
    $('eqhint').textContent = `${d.name}: follows the substation display you last clicked.`;
    $('detail').innerHTML = [d.transformer].concat(d.feeders).map(e => {
      const P = e.parameters;
      return `<tr>
        <td>${esc(e.name)}<div class="sub">${e.type === 'transformer' ? 'Transformer ' + e.rating_mva + ' MVA' : esc(e.load_type)}</div></td>
        <td><span class="bk ${e.breaker_state}">${e.breaker_state}</span></td>
        <td>${e.energised ? '<span class="live">LIVE</span>' : '<span class="dead">DEAD</span>'}</td>
        <td><span class="q ${e.quality}">${e.quality}</span></td>
        ${val(P.voltage, 2)}${val(P.current, 0)}${val(P.power_mw, 2)}${val(P.power_factor, 2)}${val(P.temperature, 1)}
        <td>${e.alarms.open ? `<span class="chip ${e.alarms.worst}">${e.alarms.open}</span>` : '-'}</td>
      </tr>`;
    }).join('');
  },
};
