// fmap.js - feeder network map, supply impact and FLISR for feeder-map
// stations (SS-01), shown in the "Feeder schematic" panel.

// ─── Feeder network map ───────────────────────────────────────────
// Built once per station, then updated in place so CSS transitions can
// animate: an outage spreads one cable section at a time (--d delay per
// section), and so does restoration. Backfed sections are cyan, with the
// power-flow dashes running the other way.
const SVGNS = 'http://www.w3.org/2000/svg';
const MX = x => 170 + x * 1.6, MY = y => 40 + y * 0.95;
const BUS_X = 120, BRK_X = 140, START_X = 156;
let mapBuilt = null, mapPrev = {}, mapGeom = {}, lastFlisr = null, planArmed = null;
const FMap = {station: null, last: null};

function wrap2(s) {
  if (s.length <= 18) return [s];
  const mid = s.length / 2, sp = [...s.matchAll(/ /g)].map(m => m.index);
  if (!sp.length) return [s];
  const i = sp.reduce((a, b) => Math.abs(b - mid) < Math.abs(a - mid) ? b : a);
  return [s.slice(0, i), s.slice(i + 1)];
}

const ICON = {
  RESIDENTIAL: '<polygon class="body" points="-14,-2 0,-14 14,-2"/><rect class="body" x="-11" y="-2" width="22" height="16"/>'
             + '<rect class="win" x="-7" y="2" width="5" height="5"/><rect class="win" x="2" y="2" width="5" height="5"/>',
  COMMERCIAL:  '<rect class="body" x="-10" y="-16" width="20" height="30"/>'
             + [-12, -5, 2].map(y => `<rect class="win" x="-6" y="${y}" width="5" height="4"/><rect class="win" x="1" y="${y}" width="5" height="4"/>`).join(''),
  INDUSTRIAL:  '<rect class="body" x="9" y="-17" width="5" height="12"/>'
             + '<path class="body" d="M-16,14 V-4 L-8,-10 V-4 L0,-10 V-4 L8,-10 V-4 L16,-4 V14 Z"/>'
             + '<rect class="win" x="-11" y="3" width="5" height="4"/><rect class="win" x="-2.5" y="3" width="5" height="4"/><rect class="win" x="6" y="3" width="5" height="4"/>',
};
ICON.MIXED = `<g transform="translate(-8,3) scale(.75)">${ICON.RESIDENTIAL}</g><g transform="translate(9,1) scale(.7)">${ICON.COMMERCIAL}</g>`;

const lerp = (a, b, t) => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];

function swSym(id, [x, y], title) {
  return `<rect class="swi" id="sw-${id}" data-sw="${id}" x="${(x - 5).toFixed(1)}" y="${(y - 5).toFixed(1)}" width="10" height="10" rx="1.5"><title>${esc(title)} (click to operate)</title></rect>`;
}

function buildMap(fm, det) {
  const byF = {}, at = {};
  fm.load_nodes.forEach(n => {
    (byF[n.feeder_slave_id] = byF[n.feeder_slave_id] || []).push(n);
    at[n.feeder_slave_id + ':' + n.seq] = [MX(n.x), MY(n.y)];
  });
  const ys = fm.feeders.map(f => MY((byF[f.slave_id] || [{y: 250}])[0].y));
  const busTop = Math.min(...ys) - 22, busBot = Math.max(...ys) + 22, y0 = (busTop + busBot) / 2;
  const tr = det.transformer;
  mapGeom = {};

  let s = `<svg viewBox="0 0 1200 545" xmlns="${SVGNS}">`;
  s += `<text class="ttl" x="10" y="22">${esc(fm.substation.name)}</text>`;
  s += `<text class="mu" x="10" y="${y0 - 40}">33 kV</text><line x1="6" y1="${y0}" x2="38" y2="${y0}" stroke="#58a6ff" stroke-width="3"/>`;
  s += `<circle cx="50" cy="${y0}" r="12" fill="none" stroke="#58a6ff" stroke-width="2.5"/>`
     + `<circle class="seg" data-f="bus" cx="66" cy="${y0}" r="12" fill="none" style="stroke-width:2.5"/>`;
  s += `<text class="mu" x="40" y="${y0 + 32}">${esc(tr.name.split(' ')[0])}</text>`;
  s += `<line class="seg" data-f="bus" x1="78" y1="${y0}" x2="${BUS_X}" y2="${y0}"/>`;
  s += `<rect class="brk" data-slave="${tr.slave_id}" id="mbrk-${tr.slave_id}" x="89" y="${y0 - 8}" width="16" height="16" rx="2"><title>${esc(tr.name)} (click to operate)</title></rect>`;
  s += `<line class="seg" data-f="bus" x1="${BUS_X}" y1="${busTop}" x2="${BUS_X}" y2="${busBot}" style="stroke-width:7"/>`;
  s += `<text class="mu" x="${BUS_X - 6}" y="${busTop - 8}" text-anchor="middle">11 kV bus</text>`;

  let lines = '', ties = '', syms = '', nodesSvg = '', faults = '';
  fm.feeders.forEach(f => {
    const nodes = byF[f.slave_id] || [];
    const pts = [[START_X, MY(nodes.length ? nodes[0].y : 250)]].concat(nodes.map(n => [MX(n.x), MY(n.y)]));
    mapGeom[f.slave_id] = pts;
    const fy = pts[0][1];
    lines += `<line class="seg" data-stub="${f.slave_id}" x1="${BUS_X}" y1="${fy}" x2="${BRK_X - 8}" y2="${fy}"/>`;
    syms  += `<rect class="brk" data-slave="${f.slave_id}" id="mbrk-${f.slave_id}" x="${BRK_X - 8}" y="${fy - 8}" width="16" height="16" rx="2"><title>${esc(f.name)} (click to operate)</title></rect>`
           + `<text class="nl" x="${START_X + 4}" y="${fy - 9}" style="font-weight:700">F${f.feeder_number}</text>`;
    for (let k = 1; k < pts.length; k++) {
      const [x1, y1] = pts[k - 1], [x2, y2] = pts[k], d = `--d:${(0.4 + 0.35 * k).toFixed(2)}s`, c = `x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"`;
      lines += `<line class="seg" id="sec-${f.slave_id}-${k}" style="${d}" ${c}/>`
             + `<line class="segflow" id="secf-${f.slave_id}-${k}" style="${d}" ${c}/>`
             + `<line class="seghit" data-f="${f.slave_id}" data-k="${k}" ${c}><title>F${f.feeder_number} section ${k}</title></line>`;
    }
    nodes.forEach((n, i) => {
      const lines2 = wrap2(n.name);
      nodesSvg += `<g class="node" id="node-${f.slave_id}-${n.seq}" style="--d:${(0.4 + 0.35 * (i + 1)).toFixed(2)}s" transform="translate(${MX(n.x)},${MY(n.y)})">`
        + `<title>${esc(n.name)} - ${n.type}, ${n.consumers} consumers, ${n.load_kw} kW connected</title>`
        + (ICON[n.type] || ICON.MIXED)
        + lines2.map((l, j) => `<text class="nl" y="${28 + 12 * j}" text-anchor="middle">${esc(l)}</text>`).join('')
        + `<text class="ni" y="${28 + 12 * lines2.length}" text-anchor="middle">${n.consumers.toLocaleString()} cons | ${n.load_kw} kW</text></g>`;
    });
    faults += `<g class="fault" id="flt-${f.slave_id}"><circle class="ring" r="13"/><circle class="ring r2" r="13"/>`
            + '<polygon class="bolt" points="2,-15 -7,2 -1,2 -4,15 7,-3 1,-3"/>'
            + `<text id="fltt-${f.slave_id}" x="12" y="-12">FAULT</text></g>`;
  });

  // RMU switches sit at the ends of each cable section, ties at their curve midpoint
  fm.switches.forEach(w => {
    const pts = mapGeom[w.feeder_slave_id];
    if (w.kind === 'IN')  syms += swSym(w.id, lerp(pts[w.node - 1], pts[w.node], 0.8), w.name);
    if (w.kind === 'OUT') syms += swSym(w.id, lerp(pts[w.node], pts[w.node + 1], 0.2), w.name);
    if (w.kind === 'TIE') {
      const A = at[w.a.feeder_slave_id + ':' + w.a.node], B = at[w.b.feeder_slave_id + ':' + w.b.node];
      const C = w.via ? [MX(w.via[0]), MY(w.via[1])] : lerp(A, B, 0.5);
      const d = `M${A[0]},${A[1]} Q${C[0]},${C[1]} ${B[0]},${B[1]}`;
      ties += `<path class="tie" id="tie-${w.id}" d="${d}"/><path class="tieflow" id="tief-${w.id}" d="${d}"/>`;
      syms += swSym(w.id, [0.25 * A[0] + 0.5 * C[0] + 0.25 * B[0], 0.25 * A[1] + 0.5 * C[1] + 0.25 * B[1]], w.name)
            + `<text class="ni" x="${(0.25 * A[0] + 0.5 * C[0] + 0.25 * B[0] + 8).toFixed(1)}" y="${(0.25 * A[1] + 0.5 * C[1] + 0.25 * B[1] - 7).toFixed(1)}">${w.id}</text>`;
    }
  });

  $('map').innerHTML = s + lines + ties + nodesSvg + syms + faults + '</svg>';
  mapBuilt = fm.substation.id; mapPrev = {};
}

function boomAt(x, y) {
  const c = document.createElementNS(SVGNS, 'circle');
  c.setAttribute('class', 'boom'); c.setAttribute('cx', x); c.setAttribute('cy', y); c.setAttribute('r', 12);
  $('map').querySelector('svg').appendChild(c);
  setTimeout(() => c.remove(), 1200);
  $('fmapwrap').classList.remove('flash'); void $('fmapwrap').offsetWidth; $('fmapwrap').classList.add('flash');
}

function paintBreaker(id, st) {
  const r = $('mbrk-' + id);
  if (!r) return;
  const fill = {CLOSED: '#3fb950', OPEN: '#0d1117', TRIPPED: '#f85149'}[st] || '#e3b341';
  r.setAttribute('fill', fill); r.setAttribute('stroke', st === 'OPEN' ? '#8b949e' : fill); r.setAttribute('stroke-width', 2);
  r.setAttribute('class', 'brk' + (st === 'TRIPPED' ? ' trip' : ''));
}

function renderMap(fm, det) {
  if (mapBuilt !== fm.substation.id) buildMap(fm, det);
  FMap.last = fm;
  const svg = $('map').querySelector('svg');
  svg.querySelectorAll('[data-f="bus"]').forEach(e => e.classList.toggle('dead', !fm.substation.transformer_energised));
  paintBreaker(det.transformer.slave_id, det.transformer.breaker_state);

  fm.sections.forEach(c => {
    const back = c.energised && c.source !== c.feeder_slave_id;
    [$(`sec-${c.feeder_slave_id}-${c.section}`), $(`secf-${c.feeder_slave_id}-${c.section}`)].forEach(e => {
      e.classList.toggle('dead', !c.energised); e.classList.toggle('back', back);
    });
    $(`secf-${c.feeder_slave_id}-${c.section}`).classList.toggle('rev', c.direction < 0);
  });
  const nodeAt = {};
  fm.load_nodes.forEach(n => {
    nodeAt[n.feeder_slave_id + ':' + n.seq] = n;
    const e = $(`node-${n.feeder_slave_id}-${n.seq}`);
    e.classList.toggle('dead', !n.energised);
    e.classList.toggle('back', n.energised && n.source !== n.feeder_slave_id);
  });
  fm.switches.forEach(w => {
    $('sw-' + w.id).setAttribute('class', 'swi ' + w.state.toLowerCase() + (w.state !== w.normal ? ' abn' : ''));
    if (w.kind === 'TIE') {
      const A = nodeAt[w.a.feeder_slave_id + ':' + w.a.node];
      $('tie-' + w.id).setAttribute('class', 'tie' + (w.state === 'CLOSED' ? ' closed' : '') + (w.energised ? ' live' : ''));
      // dashes run from the side that has its own supply towards the backfed side
      $('tief-' + w.id).setAttribute('class', 'tieflow' + (w.energised ? ' on' : '')
        + (w.energised && A && A.source !== A.feeder_slave_id ? ' rev' : ''));
    }
  });

  fm.feeders.forEach(f => {
    const sid = f.slave_id, prev = mapPrev[sid], sec = f.fault_section || 0;
    svg.querySelectorAll(`[data-stub="${sid}"]`).forEach(e => e.classList.toggle('dead', !f.energised));
    paintBreaker(sid, f.breaker_state);
    const g = $('flt-' + sid), pts = mapGeom[sid];
    if (sec > 0 && pts && sec < pts.length) {
      const x = (pts[sec - 1][0] + pts[sec][0]) / 2, y = (pts[sec - 1][1] + pts[sec][1]) / 2;
      g.setAttribute('transform', `translate(${x},${y})`);
      $('fltt-' + sid).textContent = 'FAULT S' + sec;
      g.classList.add('on');
      if (prev && prev.sec !== sec) boomAt(x, y);       // new fault since the last refresh
    } else {
      g.classList.remove('on');
    }
    mapPrev[sid] = {sec};
  });
  renderImpact(fm);
}

function renderImpact(fm) {
  const im = fm.impact, pc = im.total_consumers ? 100 * im.affected_consumers / im.total_consumers : 0;
  const cons = {}, count = {};
  fm.load_nodes.forEach(n => {
    cons[n.feeder_slave_id] = (cons[n.feeder_slave_id] || 0) + n.consumers;
    count[n.feeder_slave_id] = (count[n.feeder_slave_id] || 0) + 1;
  });
  let h = `<div class="stats"><div><div class="sub">Consumers off supply</div>
    <div class="big${im.affected_consumers ? ' bad' : ''}">${im.affected_consumers.toLocaleString()} <span class="sub">/ ${im.total_consumers.toLocaleString()}</span></div>
    <div class="meter"><div style="width:${pc.toFixed(1)}%"></div></div></div>
    <div><div class="sub">Connected load interrupted</div>
    <div class="big${im.affected_load_kw ? ' bad' : ''}">${im.affected_load_kw.toLocaleString()} kW <span class="sub">/ ${im.total_load_kw.toLocaleString()}</span></div>
    <div class="meter"><div style="width:${(im.total_load_kw ? 100 * im.affected_load_kw / im.total_load_kw : 0).toFixed(1)}%"></div></div></div></div>
    <div class="flist">`;
  fm.feeders.forEach(f => {
    const total = count[f.slave_id] || 0, live = f.nodes_live;
    const st = live === total && f.energised ? 'LIVE' : live > 0 ? 'PARTIAL'
             : (f.breaker_state === 'CLOSED' ? 'NO SUPPLY' : f.breaker_state);
    const cls = st === 'LIVE' ? 'live' : st === 'PARTIAL' ? 'part' : 'dead';
    const canFault = f.energised && f.breaker_state === 'CLOSED' && f.comms_status !== 'offline' && !f.fault_section;
    h += `<div class="frow"><div class="fh"><span><b>F${f.feeder_number}</b> ${esc(f.name.split(' — ')[1] || f.name)}</span>
        <span class="${cls}">${st}</span></div>
      <div class="fh"><span class="sub">${live}/${total} nodes live | ${(cons[f.slave_id] || 0).toLocaleString()} consumers${f.abnormal ? ' | <span class="abntag">abnormal config</span>' : ''}</span>
        <button data-fault="${f.slave_id}" ${canFault ? '' : 'disabled'}>Simulate fault</button></div>`;
    if (f.fault_section) {
      const b = f.fault_between;
      h += `<div class="ff">Fault in section ${f.fault_section}${b ? ': ' + esc(b[0]) + ' → ' + esc(b[1]) : ''}.
        <button data-repair="${f.slave_id}">Repair (crew)</button></div>`;
    }
    h += '</div>';
  });
  $('impact').innerHTML = h + '</div>';
}

const PLAN_CHIP = {EXECUTING: 'MEDIUM', COMPLETED: 'OK', FAILED: 'CRITICAL', REJECTED: 'CRITICAL', CANCELLED: 'HIGH'};

function renderFlisr(fl) {
  lastFlisr = fl;
  $('flisrauto').checked = fl.auto;
  const running = fl.plans.find(p => p.status === 'EXECUTING');
  let h = '';
  if (!fl.proposals.length) h += '<div class="sub">No located faults, and every feeder is in its normal configuration.</div>';
  fl.proposals.forEach(p => {
    const key = p.kind + ':' + p.feeder, armedHere = planArmed === key;
    h += `<div class="plan"><div class="ph">${p.kind === 'FLISR'
        ? `FLISR: ${esc(p.feeder_name)}, fault in section ${p.fault_section}` : `Restore normal: ${esc(p.feeder_name)}`}</div>`;
    if (p.steps.length) h += '<ol>' + p.steps.map(s => `<li><span class="why">${esc(s.why)}</span>${esc(s.label)}</li>`).join('') + '</ol>';
    if (p.restored) h += `<div class="sub">Restores <b>${p.restored.consumers.toLocaleString()}</b> consumers (${p.restored.kw.toLocaleString()} kW connected); still off ${p.still_off.consumers.toLocaleString()} (${p.still_off.kw.toLocaleString()} kW)</div>`;
    if (p.transfer && p.transfer.candidates.length) {
      h += `<div class="sub">Tie check for ${p.transfer.mw} MW (pre-fault ${p.transfer.prefault_mw} MW x share of connected load): `
         + p.transfer.candidates.map(c => `${c.tie} from ${c.source_name}: ${c.now_mw} → ${c.after_mw} MW, limit ${c.limit_mw} `
           + (c.ok ? '<span class="okc">OK</span>' : '<span class="noc">over</span>')).join('; ') + '</div>';
    }
    p.notes.forEach(n => h += `<div class="note">${esc(n)}</div>`);
    h += `<button class="${armedHere ? 'armed' : ''}" data-plan="${key}" ${p.feasible && !running ? '' : 'disabled'}>${armedHere ? 'CONFIRM execute' : 'Execute plan'}</button></div>`;
  });
  if (fl.plans.length) h += '<div class="sub" style="margin-top:10px">Recent switching plans</div>';
  fl.plans.slice(0, 4).forEach(p => {
    h += `<div class="prow"><span class="chip ${PLAN_CHIP[p.status] || 'LOW'}">${p.status}</span>
      #${p.id} ${p.kind === 'FLISR' ? 'FLISR' : 'Restore'} ${esc((p.summary.feeder_name || '').split(' — ')[0])}
      <span class="sub">${p.mode.toLowerCase()}, ${esc(p.operator)}, ${esc(p.created.slice(11))}</span>`;
    if (p.status === 'EXECUTING') {
      const st = p.steps[p.current_step];
      h += `<div class="sub">Step ${Math.min(p.current_step + 1, p.steps.length)}/${p.steps.length}: ${esc(st ? st.label : 'finishing')}
        <button data-cancel="${p.id}">Cancel</button></div>`;
    } else if (p.result) {
      h += `<div class="sub">${esc(p.result)}</div>`;
    }
    h += '</div>';
  });
  $('flisr').innerHTML = h;

  // Pulse the breaker or switch the running plan is operating
  document.querySelectorAll('#map .busy').forEach(e => e.classList.remove('busy'));
  const st = running && running.steps[running.current_step];
  if (st) { const e = $(st.kind === 'breaker' ? 'mbrk-' + st.id : 'sw-' + st.id); if (e) e.classList.add('busy'); }
}

async function injectFault(slave, section) {
  try {
    const r = await postJSON('/api/sim/fault', section ? {slave_id: slave, section} : {slave_id: slave});
    $('tsmsg').textContent = 'Test set: ' + r.message + '. Protection response appears on the next poll.';
  } catch (e) { $('tsmsg').textContent = 'Test set refused: ' + e.message; }
  refresh();
}

const FeederPanel = {
  title: () => 'Feeder schematic' + (FMap.station ? ' - ' + FMap.station : ''),
  build(body) {
    mapBuilt = null; mapPrev = {}; planArmed = null;
    body.innerHTML = `<div id="fmapwrap"><div class="mapwrap">
      <div class="sld map" id="map"><div class="empty">Loading network...</div></div>
      <div class="side">
        <div class="impact">
          <h2>Supply impact</h2>
          <div id="impact"></div>
          <div class="ts"><input type="checkbox" id="tsmode">
            <label for="tsmode">Test set: click a cable section to put a fault on it</label></div>
          <div id="tsmsg"></div>
          <div class="sub">Test set faults are simulation tools, logged in the SOE as TEST_FAULT_INJECTED.
            Load figures are connected load of the nodes; live MW is on the SLD.</div>
        </div>
        <div class="impact">
          <div class="bar"><h2>FLISR</h2>
            <label class="ts" style="margin:0 0 10px auto"><input type="checkbox" id="flisrauto"> Automatic (self-healing)</label></div>
          <div id="flisr"></div>
          <div id="flisrmsg"></div>
          <div class="sub">Fault location, isolation and service restoration. Plans are computed by the server from live
            telemetry; each step is a DNP3 command, and the next step waits for Modbus telemetry to confirm it.</div>
        </div>
      </div></div></div>`;

    $('map').addEventListener('click', e => {
      const r = e.target.closest('.brk');
      if (r) { openModal(r.dataset.slave); return; }
      const w = e.target.closest('.swi');
      if (w) { openSwitch(w.dataset.sw); return; }
      const h = e.target.closest('.seghit');
      if (h && $('tsmode').checked) injectFault(+h.dataset.f, +h.dataset.k);
    });
    $('impact').addEventListener('click', async e => {
      const b = e.target.closest('button[data-fault]');
      if (b && !b.disabled) injectFault(+b.dataset.fault);
      const r = e.target.closest('button[data-repair]');
      if (r) {
        try { const x = await postJSON('/api/sim/repair', {slave_id: +r.dataset.repair}); $('tsmsg').textContent = 'Test set: ' + x.message + '.'; }
        catch (err) { $('tsmsg').textContent = 'Test set refused: ' + err.message; }
        refresh();
      }
    });
    $('flisr').addEventListener('click', async e => {
      const b = e.target.closest('button[data-plan]');
      if (b && !b.disabled) {
        if (planArmed !== b.dataset.plan) { planArmed = b.dataset.plan; renderFlisr(lastFlisr); return; }
        planArmed = null;
        const [kind, feeder] = b.dataset.plan.split(':');
        try { const r = await postJSON(`/api/flisr/${FMap.station}/execute`, {kind, feeder_slave_id: +feeder});
              $('flisrmsg').textContent = `Plan #${r.plan_id} executing (${r.steps} steps).`; }
        catch (err) { $('flisrmsg').textContent = 'Refused: ' + err.message; }
        refresh();
      }
      const c = e.target.closest('button[data-cancel]');
      if (c) { try { await postJSON(`/api/flisr/${FMap.station}/cancel`, {plan_id: +c.dataset.cancel}); } catch (err) { $('flisrmsg').textContent = err.message; } refresh(); }
    });
    $('flisrauto').addEventListener('change', async () => {
      try { await postJSON('/api/flisr/settings', {auto: $('flisrauto').checked});
            $('flisrmsg').textContent = $('flisrauto').checked ? 'Automatic FLISR on: new faults are isolated and restored without approval.' : 'Manual FLISR: plans wait for operator approval.'; }
      catch (err) { $('flisrmsg').textContent = err.message; }
      refresh();
    });
    $('tsmode').addEventListener('change', () => $('map').classList.toggle('tsmode', $('tsmode').checked));
  },
  async update() {
    const st = App.stations.find(s => s.is_feeder_map);
    if (!st) { $('map').innerHTML = '<div class="empty">No substation has a feeder network model.</div>'; return; }
    FMap.station = st.id;
    const [det, fm, fl] = await Promise.all([detail(st.id), cached('/api/feedermap/' + st.id), cached('/api/flisr/' + st.id)]);
    renderMap(fm, det);
    renderFlisr(fl);
  },
};
