// storm.js - Storm Watch console. A canvas map of the city with the simulated
// thunderstorm (radar-style cells), lightning strokes from the LLS feed
// (/api/storm), lightning proximity warnings per substation, the live SS-01
// feeder network, and each breaker trip linked to the stroke it correlates
// with. Wheel = zoom, drag = pan, double-click = reset, hover = stroke details.

const SW = {
  built: false, geo: null, st: null, fm: null, auto: false,
  strikes: [], lastId: 0, anims: [], polledAt: 0, serverSkew: 0,
  view: null, drag: null, hover: null, raf: null, timer: null,
};
const KM_LAT = 110.57;
const kmLon = lat => 111.32 * Math.cos(lat * Math.PI / 180);

// ─── DOM ─────────────────────────────────────────────────────────────
function buildStorm() {
  $('c-storm').innerHTML = `<div class="stormwrap">
    <div class="stormmap"><canvas id="sc"></canvas><div class="sov" id="sov"></div>
      <div class="snote" id="snote"></div></div>
    <aside class="stormside">
      <div class="impact"><h2>Weather simulation</h2>
        <div class="pbar"><button data-storm="moderate">Moderate storm</button><button data-storm="severe">Severe storm</button>
          <button data-storm="stop">Stop</button></div>
        <label class="ts"><input type="checkbox" id="stauto"> Self-healing: automatic FLISR on RJN</label>
        <div id="stmsg" class="note"></div>
        <div class="sub">The storm is simulated in the RTU simulator. Its motion runs in time-lapse; stroke times,
          currents and the faults they cause are real-time events in the SCADA chain.</div></div>
      <div class="impact"><h2>Lightning proximity (last 5 min)</h2><div id="stwarn"></div>
        <div class="sub">Amber: cloud-to-ground stroke within 8 km. Red: within 3 km.</div></div>
      <div class="impact"><h2>Breaker trips and lightning (last 20 min)</h2><div id="stcorr"></div>
        <div class="sub">Matched by time and distance from the stroke feed, as with a lightning location system.
          Most lightning faults are transient: the recloser restores supply within seconds, so the breaker is
          closed again by the time you look at the SLD. The Events panel keeps the full sequence.</div></div>
      <div class="impact"><h2>Latest cloud-to-ground strokes</h2><div class="scroll"><table class="tleg">
        <thead><tr><th>Time</th><th>kA</th><th>Nearest substation</th></tr></thead><tbody id="ststr"></tbody></table></div></div>
    </aside></div>`;

  document.querySelector('.stormside').addEventListener('click', async e => {
    const b = e.target.closest('button[data-storm]');
    if (!b) return;
    const a = b.dataset.storm;
    try {
      const r = await postJSON('/api/sim/storm', a === 'stop' ? {action: 'stop'} : {action: 'start', intensity: a});
      $('stmsg').textContent = r.message + '.';
    } catch (err) { $('stmsg').textContent = 'Refused: ' + err.message; }
    pollStorm();
  });
  $('stauto').addEventListener('change', async () => {
    try { await postJSON('/api/flisr/settings', {auto: $('stauto').checked}); }
    catch (err) { $('stmsg').textContent = err.message; }
    refresh();
  });

  const cv = $('sc');
  cv.addEventListener('wheel', e => {
    e.preventDefault();
    const v = SW.view, r = cv.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
    const before = unproject(mx, my);
    v.kmpp = Math.min(0.14, Math.max(0.006, v.kmpp * (e.deltaY > 0 ? 1.15 : 0.87)));
    const after = unproject(mx, my);                 // keep the point under the cursor fixed
    v.lat += before[0] - after[0]; v.lon += before[1] - after[1];
  }, {passive: false});
  cv.addEventListener('mousedown', e => { SW.drag = {x: e.clientX, y: e.clientY}; });
  window.addEventListener('mouseup', () => { SW.drag = null; });
  cv.addEventListener('mousemove', e => {
    const r = cv.getBoundingClientRect();
    SW.hover = {x: e.clientX - r.left, y: e.clientY - r.top};
    if (SW.drag) {
      const v = SW.view;
      v.lon -= (e.clientX - SW.drag.x) * v.kmpp / kmLon(v.lat);
      v.lat += (e.clientY - SW.drag.y) * v.kmpp / KM_LAT;
      SW.drag = {x: e.clientX, y: e.clientY};
    }
  });
  cv.addEventListener('mouseleave', () => { SW.hover = null; });
  cv.addEventListener('dblclick', () => { SW.view = null; });
  SW.built = true;
}

// ─── Projection ──────────────────────────────────────────────────────
function fitView(W, H) {
  const s = SW.geo.substations;
  const lats = s.map(x => x.lat), lons = s.map(x => x.lon);
  const lat = (Math.min(...lats) + Math.max(...lats)) / 2, lon = (Math.min(...lons) + Math.max(...lons)) / 2;
  const hkm = (Math.max(...lats) - Math.min(...lats)) * KM_LAT + 16, wkm = (Math.max(...lons) - Math.min(...lons)) * kmLon(lat) + 16;
  SW.view = {lat, lon, kmpp: Math.max(hkm / H, wkm / W), lat0: lat, lon0: lon};
}

let SCW = 0, SCH = 0;
const project = (lat, lon) => [SCW / 2 + (lon - SW.view.lon) * kmLon(SW.view.lat) / SW.view.kmpp,
                               SCH / 2 - (lat - SW.view.lat) * KM_LAT / SW.view.kmpp];
const unproject = (x, y) => [SW.view.lat - (y - SCH / 2) * SW.view.kmpp / KM_LAT,
                             SW.view.lon + (x - SCW / 2) * SW.view.kmpp / kmLon(SW.view.lat)];
const distKm = (a, b) => Math.hypot((a[0] - b[0]) * KM_LAT, (a[1] - b[1]) * kmLon(a[0]));

// ─── Data ────────────────────────────────────────────────────────────
function makeBolt(seed) {
  // A jagged channel from the cloud down to the strike point, in px relative to it
  let r = seed;
  const rnd = () => { r = (r * 9301 + 49297) % 233280; return r / 233280; };
  const top = [-40 + rnd() * 80, -(150 + rnd() * 90)], pts = [top], n = 9;
  for (let i = 1; i <= n; i++) {
    const f = i / n;
    pts.push([top[0] * (1 - f) + (i < n ? (rnd() - .5) * 34 * (1 - f * .5) : 0), top[1] * (1 - f)]);
  }
  const branches = [];
  for (let b = 0; b < 2; b++) {
    const i = 2 + Math.floor(rnd() * 4), [bx, by] = pts[i], dir = rnd() < .5 ? -1 : 1, br = [[bx, by]];
    for (let j = 1; j <= 3; j++) br.push([bx + dir * j * (10 + rnd() * 10), by + j * (12 + rnd() * 8)]);
    branches.push(br);
  }
  return {pts, branches};
}

async function pollStorm() {
  if (!SW.geo) return;
  let st;
  try { st = await api('/api/storm?since=' + SW.lastId); }
  catch (e) { $('sov').innerHTML = `<b style="color:var(--red)">Lightning feed unavailable</b><br>${esc(e.message)}`; return; }
  const perf = performance.now();
  SW.serverSkew = st.now - Date.now();
  const fresh = st.strikes.filter(x => st.now - x.t < 4000);
  const base = fresh.length ? Math.min(...fresh.map(x => x.t)) : 0;
  st.strikes.forEach(x => {
    SW.strikes.push(x);
    SW.lastId = Math.max(SW.lastId, x.id);
    // replay new strokes in their own order instead of all at once
    if (st.now - x.t < 4000) SW.anims.push({s: x, at: perf + (x.t - base), bolt: x.type === 'CG' ? makeBolt(x.id * 7919) : null});
  });
  const cut = st.now - 15 * 60000;
  SW.strikes = SW.strikes.filter(x => x.t >= cut);
  if (st.last_id < SW.lastId) { SW.lastId = 0; SW.strikes = []; }     // feed restarted: start over
  SW.st = st; SW.polledAt = perf;
  renderStormSide();
  drawStorm();          // also when animation frames are throttled
}

function renderStormSide() {
  const st = SW.st;
  $('sov').innerHTML = st.active
    ? `<b class="son">STORM ACTIVE</b> ${st.intensity} | ${st.cells.length} cells | ${st.stats.per_min} strokes/min (${st.stats.cg_per_min} to ground)
       | moving towards ${st.heading}° | time-lapse ×${st.time_lapse}`
    : '<b>No storm</b> | start a weather simulation on the right';
  $('stwarn').innerHTML = st.warnings.map(w => `<div class="fh" style="padding:3px 0"><span><b>${esc(w.short_name)}</b> <span class="sub">${esc(w.substation_id)}</span></span>
      <span class="sub">${w.nearest_km == null ? 'no strokes' : `nearest ${w.nearest_km} km | ${w.within_8km} within 8 km`}</span>
      <span class="wl ${w.level}">${w.level}</span></div>`).join('');
  const corr = st.correlations.slice(0, 8);
  const outcome = o => !o ? '' : o.state === 'AUTO_RECLOSED' ? `<span class="okc">auto-reclosed after ${o.after_s} s</span> (transient fault)`
    : o.state === 'CLOSED_BY_COMMAND' ? `<span class="okc">closed after ${o.after_s} s</span> by ${esc(o.by)}`
    : o.state === 'STILL_TRIPPED' ? '<span class="noc">still tripped</span> (permanent fault)'
    : esc(o.state.replace('STILL_', 'now ').toLowerCase());
  $('stcorr').innerHTML = corr.length ? corr.map(c => `<div class="prow"><b>${esc(c.device_name.split(' — ')[0])}</b>
      <span class="sub">${esc(c.substation_id)} tripped ${esc(c.time.slice(11, 19))}, ${outcome(c.outcome)}</span><br>` + (c.strike
      ? `<span class="okc">⚡ ${c.strike.ka} kA stroke</span> <span class="sub">${c.strike.distance_km} km away, ${c.strike.seconds_before} s before</span>`
      : '<span class="sub">no stroke nearby: other cause</span>') + '</div>').join('')
    : '<div class="sub">No breaker trips in the last 20 minutes.</div>';
  const subs = SW.geo.substations;
  $('ststr').innerHTML = SW.strikes.filter(x => x.type === 'CG').slice(-9).reverse().map(x => {
    const near = subs.map(s => [distKm([x.lat, x.lon], [s.lat, s.lon]), s.short_name]).sort((a, b) => a[0] - b[0])[0];
    return `<tr><td>${new Date(x.t - SW.serverSkew).toTimeString().slice(0, 8)}</td>
      <td class="num" style="color:${x.ka > 0 ? '#ff7b72' : '#a5d6ff'}">${x.ka > 0 ? '+' : ''}${x.ka}</td>
      <td>${near[0].toFixed(1)} km from ${esc(near[1])}</td></tr>`;
  }).join('') || '<tr><td colspan="3" class="empty">No strokes</td></tr>';
}

// ─── Drawing ─────────────────────────────────────────────────────────
function ageColor(ageMs) {
  const m = ageMs / 60000;
  if (m < 1) return [255, 255, 255, 1];
  if (m < 4) return [255, 221, 87, .95];
  if (m < 9) return [255, 140, 60, .8];
  return [248, 81, 73, Math.max(0, .7 * (1 - (m - 9) / 6))];
}

function drawStorm() {
  const cv = $('sc');
  if (!cv || !SW.geo) return;
  const box = cv.parentElement, dpr = window.devicePixelRatio || 1;
  const W = box.clientWidth, H = box.clientHeight;
  if (!W || !H) return;
  if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) {
    cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
    cv.style.width = W + 'px'; cv.style.height = H + 'px';
  }
  SCW = W; SCH = H;
  if (!SW.view) fitView(W, H);
  const ctx = cv.getContext('2d'), v = SW.view, perf = performance.now(), tnow = Date.now() + SW.serverSkew;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.font = '11px Segoe UI, system-ui, sans-serif';

  // Night sky
  const bg = ctx.createRadialGradient(W / 2, H / 2, 40, W / 2, H / 2, Math.max(W, H) * .75);
  bg.addColorStop(0, '#0c1728'); bg.addColorStop(1, '#04070d');
  ctx.fillStyle = bg; ctx.fillRect(0, 0, W, H);

  // Lat/lon grid
  ctx.strokeStyle = 'rgba(88,166,255,.08)'; ctx.fillStyle = 'rgba(139,148,158,.6)'; ctx.lineWidth = 1;
  const step = v.kmpp > 0.06 ? 0.1 : v.kmpp > 0.02 ? 0.05 : 0.01;
  const [latT, lonL] = unproject(0, 0), [latB, lonR] = unproject(W, H);
  ctx.textAlign = 'left'; ctx.textBaseline = 'top';
  for (let la = Math.ceil(latB / step) * step; la <= latT; la += step) {
    const y = Math.round(project(la, v.lon)[1]) + .5;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke(); ctx.fillText(la.toFixed(2) + '°N', 6, y + 3);
  }
  for (let lo = Math.ceil(lonL / step) * step; lo <= lonR; lo += step) {
    const x = Math.round(project(v.lat, lo)[0]) + .5;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke(); ctx.fillText(lo.toFixed(2) + '°E', x + 4, H - 60);
  }

  // Range rings and radar sweep around the centre of the network
  const [cx, cy] = project(v.lat0, v.lon0);
  ctx.textAlign = 'center';
  for (let km = 5; km <= 25; km += 5) {
    ctx.strokeStyle = 'rgba(63,185,80,.10)'; ctx.beginPath(); ctx.arc(cx, cy, km / v.kmpp, 0, 7); ctx.stroke();
    ctx.fillStyle = 'rgba(63,185,80,.35)'; ctx.fillText(km + ' km', cx, cy - km / v.kmpp + 3);
  }
  const sweep = (perf / 4500) * Math.PI * 2, rr = 26 / v.kmpp;
  for (let i = 0; i < 40; i++) {
    ctx.fillStyle = `rgba(63,185,80,${0.07 * (1 - i / 40)})`;
    ctx.beginPath(); ctx.moveTo(cx, cy); ctx.arc(cx, cy, rr, sweep - (i + 1) * 0.02, sweep - i * 0.02); ctx.fill();
  }
  ctx.strokeStyle = 'rgba(63,185,80,.28)'; ctx.beginPath(); ctx.moveTo(cx, cy);
  ctx.lineTo(cx + rr * Math.cos(sweep), cy + rr * Math.sin(sweep)); ctx.stroke();

  // Storm cells as radar reflectivity: overlapping lobes, green rim to magenta core
  const st = SW.st, dtPoll = (perf - SW.polledAt) / 1000;
  if (st && st.active) {
    ctx.save(); ctx.globalCompositeOperation = 'screen';
    st.cells.forEach(c => {
      const km = c.speed_kmh * st.time_lapse * dtPoll / 3600, h = c.heading * Math.PI / 180;
      const lat = c.lat + km * Math.cos(h) / KM_LAT, lon = c.lon + km * Math.sin(h) / kmLon(c.lat);
      const [x, y] = project(lat, lon), R = c.radius_km / v.kmpp, power = Math.min(1, c.rate / 22);
      c._x = x; c._y = y; c._R = R;
      // rain area (green) > heavy rain (yellow) > intense (orange-red) > hail core (magenta)
      const layers = [[1.35, '40,170,70', .20], [.95, '225,185,60', .10 + .12 * power], [.6, '240,95,45', .08 + .16 * power],
                      [.3, '214,60,200', .22 * power]];
      layers.forEach(([f, rgb, a], li) => {
        for (let k = 0; k < 5; k++) {
          const ang = k * 1.2566 + c.id + perf / (9000 + li * 2500) * (li % 2 ? -1 : 1);
          const off = R * f * .5, lx = x + Math.cos(ang) * off, ly = y + Math.sin(ang) * off;
          const lr = R * f * (.6 + .1 * Math.sin(perf / 1700 + k + c.id));
          const g = ctx.createRadialGradient(lx, ly, 0, lx, ly, lr);
          g.addColorStop(0, `rgba(${rgb},${a})`); g.addColorStop(.6, `rgba(${rgb},${a * .55})`); g.addColorStop(1, `rgba(${rgb},0)`);
          ctx.fillStyle = g; ctx.beginPath(); ctx.arc(lx, ly, lr, 0, 7); ctx.fill();
        }
      });
    });
    ctx.restore();
    st.cells.forEach(c => {
      const h = c.heading * Math.PI / 180, ax = Math.sin(h), ay = -Math.cos(h);
      ctx.strokeStyle = 'rgba(230,237,243,.55)'; ctx.fillStyle = 'rgba(230,237,243,.8)'; ctx.lineWidth = 1.5;
      const x1 = c._x + ax * (c._R + 8), y1 = c._y + ay * (c._R + 8), x2 = x1 + ax * 26, y2 = y1 + ay * 26;
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(x2 + ax * 7, y2 + ay * 7); ctx.lineTo(x2 - ay * 5, y2 + ax * 5); ctx.lineTo(x2 + ay * 5, y2 - ax * 5); ctx.fill();
      ctx.textAlign = 'center'; ctx.fillText(`Cell ${c.id} | ${c.rate}/min | ${c.speed_kmh} km/h`, c._x, c._y - c._R - 14);
    });
  }

  // SS-01 feeder network, coloured from live telemetry
  const secState = {};
  if (SW.fm) SW.fm.sections.forEach(s => { secState[s.feeder_slave_id + ':' + s.section] = s; });
  const blink = Math.sin(perf / 160) > 0;
  ctx.lineCap = 'round';
  SW.geo.ties.forEach(t => {
    const a = project(...t.a), b = project(...t.b);
    ctx.setLineDash([4, 5]); ctx.strokeStyle = 'rgba(139,148,158,.45)'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(...a); ctx.lineTo(...b); ctx.stroke(); ctx.setLineDash([]);
  });
  SW.geo.feeders.forEach(f => {
    for (let k = 1; k < f.points.length; k++) {
      const s = secState[f.slave_id + ':' + k], a = project(...f.points[k - 1]), b = project(...f.points[k]);
      let col = '#3fb950';
      if (s && !s.energised) col = '#484f58';
      else if (s && s.source !== f.slave_id) col = '#39c5cf';
      if (s && s.faulted) col = blink ? '#f85149' : '#7d2a26';
      ctx.strokeStyle = col; ctx.lineWidth = 2.2; ctx.shadowColor = col; ctx.shadowBlur = s && s.energised ? 6 : 0;
      ctx.beginPath(); ctx.moveTo(...a); ctx.lineTo(...b); ctx.stroke(); ctx.shadowBlur = 0;
      ctx.fillStyle = col; ctx.beginPath(); ctx.arc(b[0], b[1], 2.6, 0, 7); ctx.fill();
    }
    if (v.kmpp < 0.03) {
      const p = project(...f.points[1]);
      ctx.fillStyle = 'rgba(230,237,243,.7)'; ctx.textAlign = 'left'; ctx.fillText('F' + f.feeder_number, p[0] + 5, p[1] - 6);
    }
  });

  // Substations with proximity warning rings
  const warn = {};
  if (st) st.warnings.forEach(w => { warn[w.substation_id] = w.level; });
  SW.geo.substations.forEach(s => {
    const [x, y] = project(s.lat, s.lon), info = App.stations.find(a => a.id === s.id) || {};
    const tripped = info.feeders_open_or_tripped > 0, lvl = warn[s.id];
    if (lvl === 'AMBER' || lvl === 'RED') {
      const col = lvl === 'RED' ? '248,81,73' : '227,179,65', ph = (perf / 1400) % 1;
      ctx.strokeStyle = `rgba(${col},${.7 * (1 - ph)})`; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(x, y, 10 + ph * 26, 0, 7); ctx.stroke();
      ctx.setLineDash([3, 6]); ctx.strokeStyle = `rgba(${col},.28)`; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.arc(x, y, (lvl === 'RED' ? 3 : 8) / v.kmpp, 0, 7); ctx.stroke(); ctx.setLineDash([]);
    }
    const col = tripped ? '#f85149' : '#3fb950';
    ctx.shadowColor = col; ctx.shadowBlur = 14; ctx.fillStyle = col;
    ctx.beginPath(); ctx.arc(x, y, 6, 0, 7); ctx.fill(); ctx.shadowBlur = 0;
    ctx.strokeStyle = '#e6edf3'; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.arc(x, y, 9, 0, 7); ctx.stroke();
    ctx.fillStyle = '#e6edf3'; ctx.textAlign = 'left'; ctx.font = 'bold 12px Segoe UI, system-ui, sans-serif';
    ctx.fillText(s.short_name, x + 14, y - 12); ctx.font = '11px Segoe UI, system-ui, sans-serif';
    ctx.fillStyle = 'rgba(139,148,158,.9)'; ctx.fillText(s.id + (tripped ? ' | breaker tripped' : ''), x + 14, y + 2);
  });

  // Stroke markers: polarity sign for ground strokes, fading with age
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  let hoverHit = null;
  SW.strikes.forEach(x => {
    const age = tnow - x.t, [px, py] = project(x.lat, x.lon);
    if (px < -20 || py < -20 || px > W + 20 || py > H + 20) return;
    if (x.type === 'IC') {
      if (age < 20000) { ctx.fillStyle = `rgba(188,140,255,${.5 * (1 - age / 20000)})`; ctx.beginPath(); ctx.arc(px, py, 2, 0, 7); ctx.fill(); }
      return;
    }
    const [r, g, b, a] = ageColor(age);
    ctx.strokeStyle = `rgba(${r},${g},${b},${a})`; ctx.lineWidth = 1.8;
    ctx.beginPath(); ctx.moveTo(px - 4, py); ctx.lineTo(px + 4, py);
    if (x.ka > 0) { ctx.moveTo(px, py - 4); ctx.lineTo(px, py + 4); }
    ctx.stroke();
    if (SW.hover && Math.hypot(SW.hover.x - px, SW.hover.y - py) < 8) hoverHit = [x, px, py, age];
  });

  // Links from correlated trips to their stroke
  if (st) st.correlations.forEach(c => {
    if (!c.strike || tnow - c.t > 240000) return;
    const f = SW.geo.feeders.find(x => x.slave_id === c.slave_id), sub = SW.geo.substations.find(s => s.id === c.substation_id);
    const to = f ? project(...f.points[Math.ceil((f.points.length - 1) / 2)]) : project(sub.lat, sub.lon);
    const from = project(c.strike.lat, c.strike.lon);
    ctx.setLineDash([5, 4]); ctx.strokeStyle = 'rgba(227,179,65,.85)'; ctx.lineWidth = 1.2;
    ctx.beginPath(); ctx.moveTo(...from); ctx.lineTo(...to); ctx.stroke(); ctx.setLineDash([]);
    ctx.beginPath(); ctx.arc(from[0], from[1], 9, 0, 7); ctx.stroke();
    ctx.fillStyle = '#e3b341'; ctx.textAlign = 'left';
    ctx.fillText(`${c.device_name.split(' — ')[0]} trip: ${c.strike.ka} kA, ${c.strike.distance_km} km`, from[0] + 13, from[1] - 10);
  });

  // New strokes: bolt, ground flash, ripples
  let flash = 0;
  SW.anims = SW.anims.filter(a => perf - a.at < 1800);
  SW.anims.forEach(a => {
    const age = (perf - a.at) / 1000;
    if (age < 0) return;
    const [px, py] = project(a.s.lat, a.s.lon);
    if (a.s.type === 'IC') {
      if (age < .45) {
        const g = ctx.createRadialGradient(px, py, 0, px, py, 46);
        g.addColorStop(0, `rgba(210,190,255,${.55 * (1 - age / .45)})`); g.addColorStop(1, 'rgba(210,190,255,0)');
        ctx.fillStyle = g; ctx.beginPath(); ctx.arc(px, py, 46, 0, 7); ctx.fill();
      }
      return;
    }
    const strong = Math.abs(a.s.ka) >= 40;
    if (age < .32 && ((age * 1000) % 90) < 62) {
      ctx.save(); ctx.translate(px, py);
      ctx.shadowColor = '#9cc9ff'; ctx.shadowBlur = 22; ctx.strokeStyle = '#ffffff'; ctx.lineWidth = strong ? 3 : 2; ctx.lineJoin = 'round';
      ctx.beginPath(); a.bolt.pts.forEach((p, i) => i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])); ctx.stroke();
      ctx.lineWidth = 1.2; ctx.globalAlpha = .8;
      a.bolt.branches.forEach(br => { ctx.beginPath(); br.forEach((p, i) => i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])); ctx.stroke(); });
      ctx.restore();
    }
    if (age < .5) {
      const g = ctx.createRadialGradient(px, py, 0, px, py, 60);
      g.addColorStop(0, `rgba(255,255,255,${.85 * (1 - age / .5)})`); g.addColorStop(.3, `rgba(156,201,255,${.4 * (1 - age / .5)})`);
      g.addColorStop(1, 'rgba(156,201,255,0)');
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(px, py, 60, 0, 7); ctx.fill();
    }
    [0, .28].forEach(d => {
      const t = age - d;
      if (t < 0 || t > 1.5) return;
      ctx.strokeStyle = `rgba(200,225,255,${.8 * (1 - t / 1.5)})`; ctx.lineWidth = 1.6;
      ctx.beginPath(); ctx.arc(px, py, 6 + t * 46, 0, 7); ctx.stroke();
    });
    if (strong && age < .22) flash = Math.max(flash, .2 * (1 - age / .22));
  });
  if (flash) { ctx.fillStyle = `rgba(220,235,255,${flash})`; ctx.fillRect(0, 0, W, H); }

  // Scale bar and north arrow
  const barKm = [1, 2, 5, 10].find(k => k / v.kmpp >= 60) || 20, bw = barKm / v.kmpp;
  ctx.strokeStyle = '#8b949e'; ctx.fillStyle = '#8b949e'; ctx.lineWidth = 1.5; ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic';
  ctx.beginPath(); ctx.moveTo(20, H - 34); ctx.lineTo(20, H - 28); ctx.lineTo(20 + bw, H - 28); ctx.lineTo(20 + bw, H - 34); ctx.stroke();
  ctx.fillText(barKm + ' km', 20 + bw / 2, H - 38);
  ctx.beginPath(); ctx.moveTo(W - 30, 62); ctx.lineTo(W - 36, 78); ctx.lineTo(W - 30, 74); ctx.lineTo(W - 24, 78); ctx.fill();
  ctx.fillText('N', W - 30, 56);

  if (hoverHit) {
    const [x, px, py, age] = hoverHit;
    const txt = [`${x.ka > 0 ? '+' : ''}${x.ka} kA cloud-to-ground`, `${new Date(x.t - SW.serverSkew).toTimeString().slice(0, 8)}, ${Math.round(age / 1000)} s ago`];
    const tw = Math.max(...txt.map(t => ctx.measureText(t).width)) + 16;
    const bx = Math.min(px + 12, W - tw - 6), by = Math.max(6, py - 44);
    ctx.fillStyle = 'rgba(13,17,23,.94)'; ctx.strokeStyle = '#30363d'; ctx.fillRect(bx, by, tw, 38); ctx.strokeRect(bx + .5, by + .5, tw - 1, 37);
    ctx.fillStyle = '#e6edf3'; ctx.textAlign = 'left'; ctx.fillText(txt[0], bx + 8, by + 15);
    ctx.fillStyle = '#8b949e'; ctx.fillText(txt[1], bx + 8, by + 30);
  }
}

function stormLoop() {
  drawStorm();
  SW.raf = requestAnimationFrame(stormLoop);
}

// ─── Console registration ────────────────────────────────────────────
CONSOLES.storm = async () => {
  if (!SW.built) buildStorm();
  if (!SW.geo) {
    SW.geo = await api('/api/storm/geo');
    $('snote').textContent = SW.geo.note;
    await pollStorm();
  }
  const st = App.stations.find(s => s.is_feeder_map);
  if (st) {
    const [fm, fl] = await Promise.all([cached('/api/feedermap/' + st.id), cached('/api/flisr/' + st.id)]);
    SW.fm = fm; SW.auto = fl.auto;
    $('stauto').checked = fl.auto;
  }
  drawStorm();          // also when animation frames are throttled
};

CONSOLE_HOOKS.storm = {
  show() {
    if (!SW.built) buildStorm();
    if (!SW.raf) stormLoop();
    if (!SW.timer) SW.timer = setInterval(pollStorm, 1000);
  },
  hide() {
    cancelAnimationFrame(SW.raf); SW.raf = null;
    clearInterval(SW.timer); SW.timer = null;
  },
};
