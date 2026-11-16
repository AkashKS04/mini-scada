// trend.js - Trending console: real-time pens drawn on a canvas in stacked
// strips (one per engineering unit), a smoothly scrolling time axis, hover
// crosshair, limit lines, SOE event markers and a min/max/avg legend.
// History comes from /api/trends, live points from /api/live every refresh.

const PEN_COLORS  = ['#58a6ff', '#3fb950', '#f0883e', '#bc8cff', '#39c5cf', '#e3b341', '#ff7b72', '#a5d6ff'];
const PARAM_LABEL = {voltage: 'Voltage', current: 'Current', power_mw: 'Power', power_factor: 'PF',
                     temperature: 'Oil temp', frequency: 'Frequency'};
const UNIT_LABEL  = {kV: 'kV', A: 'A', MW: 'MW', degC: '°C', Hz: 'Hz', '': 'PF'};
const WINDOWS     = [[5, '5 min'], [15, '15 min'], [60, '1 h'], [360, '6 h'], [1440, '24 h']];
const TREND_KEY   = 'mini-scada-trend-v1';
const MAX_PENS    = 8;

const Trend = {
  pens: [], minutes: 15, paused: false, pausedAt: 0,
  hover: null, events: [], ready: false, built: false, raf: null, open: new Set(['SS-01']),
};

const parseTs = s => Date.parse(s.replace(' ', 'T'));
const bucket  = m => m <= 60 ? 1 : m <= 360 ? 6 : 24;

function devShort(e) {
  if (e.type === 'transformer') return 'T1';
  return `F${e.feeder_number} ${e.name.split(' — ')[1] || ''}`.trim();
}

function findDevice(slave) {
  for (const s of App.stations) {
    const d = App.details[s.id];
    if (!d) continue;
    const e = [d.transformer].concat(d.feeders).find(x => x.slave_id === slave);
    if (e) return {st: s, e};
  }
  return null;
}

function saveTrend() {
  try { localStorage.setItem(TREND_KEY, JSON.stringify({pens: Trend.pens.map(p => p.key), minutes: Trend.minutes})); }
  catch (e) { /* not persisted */ }
}

// ─── Pens ────────────────────────────────────────────────────────────
async function loadHistory(pen) {
  const r = await api(`/api/trends?slave_id=${pen.slave}&hours=${bucket(Trend.minutes)}&parameter=${pen.param}`);
  const vals = r.series[pen.param] || [];
  pen.data = r.labels.map((l, i) => [parseTs(l), vals[i]]).filter(p => p[1] != null);
}

async function addPen(key, quiet) {
  const old = Trend.pens.findIndex(p => p.key === key);
  if (old >= 0) { Trend.pens.splice(old, 1); afterPens(); return; }       // toggle off
  if (Trend.pens.length >= MAX_PENS) { $('tmsg').textContent = `At most ${MAX_PENS} pens.`; return; }
  const [slave, param] = [+key.split(':')[0], key.split(':')[1]];
  const f = findDevice(slave);
  if (!f || !f.e.parameters[param]) return;
  const used = new Set(Trend.pens.map(p => p.color));
  const pen = {key, slave, param, devId: f.e.id, station: f.st.id,
               name: `${f.st.short_name} ${devShort(f.e)} ${PARAM_LABEL[param]}`,
               unit: f.e.parameters[param].unit || '', limits: f.e.parameters[param].limits || {},
               color: PEN_COLORS.find(c => !used.has(c)) || PEN_COLORS[0], data: []};
  Trend.pens.push(pen);
  if (!quiet) afterPens();
  await loadHistory(pen);
}

function afterPens() { saveTrend(); renderPicker(); renderLegend(); }

// ─── DOM ─────────────────────────────────────────────────────────────
function buildTrend() {
  $('c-trend').innerHTML = `<div class="trendwrap">
    <aside class="tpick"><h2>Point browser</h2><input id="tq" placeholder="Search points" style="width:100%">
      <div id="tlist"></div></aside>
    <div class="tmain">
      <div class="pbar" id="twin">${WINDOWS.map(([m, l]) => `<button data-min="${m}">${l}</button>`).join('')}
        <button id="tpause">Pause</button><button id="tclear">Clear pens</button>
        <span class="sub" id="tmsg">Click points on the left to add pens. Hover the chart for values.</span></div>
      <div class="tcanvas"><canvas id="tc"></canvas></div>
      <div class="scroll"><table class="tleg"><thead><tr><th></th><th>Pen</th><th>Now</th><th>Min</th><th>Max</th>
        <th>Avg</th><th>Limits (H / HH)</th><th></th></tr></thead><tbody id="tleg"></tbody></table></div>
    </div></div>`;
  $('tq').addEventListener('input', renderPicker);
  $('tlist').addEventListener('click', e => {
    const t = e.target.closest('.tp');
    if (t) addPen(t.dataset.key);
  });
  $('tlist').addEventListener('toggle', e => {
    const d = e.target.closest('details');
    if (d) { if (d.open) Trend.open.add(d.dataset.st); else Trend.open.delete(d.dataset.st); }
  }, true);
  $('twin').addEventListener('click', async e => {
    const b = e.target.closest('button[data-min]');
    if (!b) return;
    const before = bucket(Trend.minutes);
    Trend.minutes = +b.dataset.min;
    markWindow(); saveTrend();
    if (bucket(Trend.minutes) !== before) await Promise.all(Trend.pens.map(loadHistory));
    renderLegend();
  });
  $('tpause').addEventListener('click', () => {
    Trend.paused = !Trend.paused; Trend.pausedAt = Date.now();
    $('tpause').textContent = Trend.paused ? 'Resume' : 'Pause';
    $('tpause').className = Trend.paused ? 'armed' : '';
  });
  $('tclear').addEventListener('click', () => { Trend.pens = []; afterPens(); });
  $('tleg').addEventListener('click', e => {
    const b = e.target.closest('[data-rm]');
    if (b) addPen(b.dataset.rm);
  });
  const cv = $('tc');
  cv.addEventListener('mousemove', e => { const r = cv.getBoundingClientRect(); Trend.hover = {x: e.clientX - r.left, y: e.clientY - r.top}; });
  cv.addEventListener('mouseleave', () => { Trend.hover = null; });
  Trend.built = true;
  markWindow();
}

function markWindow() {
  document.querySelectorAll('#twin button[data-min]').forEach(b => b.classList.toggle('on', +b.dataset.min === Trend.minutes));
}

function renderPicker() {
  if (!$('tlist')) return;
  const q = $('tq').value.trim().toLowerCase();
  $('tlist').innerHTML = App.stations.map(s => {
    const d = App.details[s.id];
    if (!d) return '';
    const inner = [d.transformer].concat(d.feeders).map(e => {
      const items = Object.keys(e.parameters).filter(p => PARAM_LABEL[p])
        .filter(p => !q || `${s.short_name} ${devShort(e)} ${PARAM_LABEL[p]}`.toLowerCase().includes(q))
        .map(p => {
          const key = `${e.slave_id}:${p}`, pen = Trend.pens.find(x => x.key === key);
          return `<span class="tp${pen ? ' on' : ''}" data-key="${key}"${pen ? ` style="border-color:${pen.color};color:${pen.color}"` : ''}>${PARAM_LABEL[p]}</span>`;
        }).join('');
      return items ? `<div class="tdev"><div class="sub2">${esc(devShort(e))}</div>${items}</div>` : '';
    }).join('');
    return inner ? `<details data-st="${s.id}"${q || Trend.open.has(s.id) ? ' open' : ''}><summary><b>${esc(s.short_name)}</b> ${esc(s.id)}</summary>${inner}</details>` : '';
  }).join('');
}

function windowStats(pen) {
  const t0 = (Trend.paused ? Trend.pausedAt : Date.now()) - Trend.minutes * 60000;
  const v = pen.data.filter(p => p[0] >= t0).map(p => p[1]);
  if (!v.length) return null;
  return {now: v[v.length - 1], min: Math.min(...v), max: Math.max(...v), avg: v.reduce((a, b) => a + b, 0) / v.length};
}

function renderLegend() {
  if (!$('tleg')) return;
  const f = x => x == null ? '-' : (Math.abs(x) >= 100 ? x.toFixed(0) : x.toFixed(2));
  $('tleg').innerHTML = Trend.pens.map(p => {
    const s = windowStats(p) || {};
    return `<tr><td><span class="sw" style="background:${p.color}"></span></td><td>${esc(p.name)}</td>
      <td class="num"><b>${f(s.now)}</b> ${UNIT_LABEL[p.unit] || p.unit}</td><td class="num">${f(s.min)}</td><td class="num">${f(s.max)}</td>
      <td class="num">${f(s.avg)}</td><td class="num sub">${p.limits.high != null ? `${p.limits.high} / ${p.limits.high_high}` : '-'}</td>
      <td><button class="x" data-rm="${p.key}" title="Remove pen">&times;</button></td></tr>`;
  }).join('') || '<tr><td colspan="8" class="empty">No pens</td></tr>';
}

// ─── Canvas ──────────────────────────────────────────────────────────
function niceStep(range, n) {
  const raw = range / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  return [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw);
}

const TIME_STEPS = [60e3, 120e3, 300e3, 600e3, 900e3, 1800e3, 3600e3, 7200e3, 10800e3];

function nearest(data, t) {
  let lo = 0, hi = data.length - 1;
  if (hi < 0) return null;
  while (lo < hi) { const m = (lo + hi) >> 1; if (data[m][0] < t) lo = m + 1; else hi = m; }
  const c = data[lo], p = data[lo - 1];
  return p && Math.abs(p[0] - t) < Math.abs(c[0] - t) ? p : c;
}

function drawTrend() {
  const cv = $('tc');
  if (!cv) return;
  const box = cv.parentElement, dpr = window.devicePixelRatio || 1;
  const W = box.clientWidth, H = box.clientHeight;
  if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) {
    cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
    cv.style.width = W + 'px'; cv.style.height = H + 'px';
  }
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  ctx.font = '11px Segoe UI, system-ui, sans-serif';

  // Right edge = now minus a little lag, so a fresh poll lands inside the plot
  const now = Trend.paused ? Trend.pausedAt : Date.now() - 1500;
  const span = Trend.minutes * 60000, t0 = now - span;
  const L = 70, R = 18, T = 14, B = 26, gap = 12;
  const X = t => L + (t - t0) / span * (W - L - R);

  const groups = [];
  Trend.pens.forEach(p => {
    let g = groups.find(x => x.unit === p.unit);
    if (!g) groups.push(g = {unit: p.unit, pens: []});
    g.pens.push(p);
  });
  if (!groups.length) {
    ctx.fillStyle = '#8b949e'; ctx.textAlign = 'center';
    ctx.fillText('Add pens from the point browser', W / 2, H / 2);
    return;
  }
  const sh = (H - T - B - gap * (groups.length - 1)) / groups.length;
  const gapMs = Trend.minutes <= 60 ? 30000 : 180000;
  const pulse = 2.5 + 1.5 * Math.sin(Date.now() / 250);
  const strips = [];

  groups.forEach((g, gi) => {
    const y0 = T + gi * (sh + gap);
    let lo = Infinity, hi = -Infinity;
    g.pens.forEach(p => p.data.forEach(([t, v]) => { if (t >= t0 - gapMs) { lo = Math.min(lo, v); hi = Math.max(hi, v); } }));
    if (!isFinite(lo)) { lo = 0; hi = 1; }
    const pad = (hi - lo) * 0.18 || Math.max(Math.abs(hi) * 0.05, 0.1);
    lo -= pad; hi += pad;
    const Y = v => y0 + sh - (v - lo) / (hi - lo) * sh;
    strips.push({y0, Y, g});

    ctx.fillStyle = '#0b0f14'; ctx.fillRect(L, y0, W - L - R, sh);
    ctx.strokeStyle = '#21262d'; ctx.lineWidth = 1; ctx.strokeRect(L + .5, y0 + .5, W - L - R - 1, sh - 1);
    const step = niceStep(hi - lo, Math.max(2, Math.floor(sh / 45)));
    ctx.fillStyle = '#8b949e'; ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
    for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
      const y = Math.round(Y(v)) + .5;
      ctx.strokeStyle = '#1b2129'; ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(W - R, y); ctx.stroke();
      ctx.fillText(+v.toFixed(3), L - 8, y);
    }
    ctx.save(); ctx.translate(14, y0 + sh / 2); ctx.rotate(-Math.PI / 2); ctx.textAlign = 'center';
    ctx.fillStyle = '#c9d1d9'; ctx.fillText(UNIT_LABEL[g.unit] || g.unit, 0, 0); ctx.restore();

    ctx.save();
    ctx.beginPath(); ctx.rect(L, y0, W - L - R, sh); ctx.clip();
    // Limit lines when the strip shows a single point and the limit is in view
    if (g.pens.length === 1) {
      const lim = g.pens[0].limits;
      [['high', '#f0883e'], ['high_high', '#f85149'], ['low', '#f0883e'], ['low_low', '#f85149']].forEach(([k, c]) => {
        if (lim[k] == null || lim[k] < lo || lim[k] > hi) return;
        const y = Math.round(Y(lim[k])) + .5;
        ctx.setLineDash([6, 5]); ctx.strokeStyle = c; ctx.globalAlpha = .7;
        ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(W - R, y); ctx.stroke();
        ctx.setLineDash([]); ctx.globalAlpha = 1; ctx.fillStyle = c; ctx.textAlign = 'left';
        ctx.fillText(k.replace('_', '-').toUpperCase() + ' ' + lim[k], L + 6, y - 7);
      });
    }
    g.pens.forEach((p, pi) => {
      const pts = p.data.filter(([t]) => t >= t0 - gapMs);
      if (!pts.length) return;
      // soft area fill under the first pen of a strip
      if (pi === 0) {
        const grad = ctx.createLinearGradient(0, y0, 0, y0 + sh);
        grad.addColorStop(0, p.color + '38'); grad.addColorStop(1, p.color + '00');
        ctx.fillStyle = grad; ctx.beginPath();
        let open = false, sx = 0;
        pts.forEach(([t, v], i) => {
          if (!open) { ctx.moveTo(X(t), y0 + sh); sx = X(t); open = true; }
          ctx.lineTo(X(t), Y(v));
          const nx = pts[i + 1];
          if (!nx || nx[0] - t > gapMs) { ctx.lineTo(X(t), y0 + sh); ctx.lineTo(sx, y0 + sh); open = false; }
        });
        ctx.fill();
      }
      ctx.strokeStyle = p.color; ctx.lineWidth = 1.8; ctx.shadowColor = p.color; ctx.shadowBlur = 6;
      ctx.beginPath();
      pts.forEach(([t, v], i) => {
        if (i === 0 || t - pts[i - 1][0] > gapMs) ctx.moveTo(X(t), Y(v)); else ctx.lineTo(X(t), Y(v));
      });
      ctx.stroke(); ctx.shadowBlur = 0;
      const [lt, lv] = pts[pts.length - 1];
      ctx.fillStyle = p.color; ctx.beginPath(); ctx.arc(X(lt), Y(lv), pulse, 0, 7); ctx.fill();
    });
    ctx.restore();
  });

  // Time axis
  const step = TIME_STEPS.find(s => span / s <= 8) || 10800e3;
  ctx.fillStyle = '#8b949e'; ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  for (let t = Math.ceil(t0 / step) * step; t <= now; t += step) {
    const x = Math.round(X(t)) + .5;
    ctx.strokeStyle = '#161b22'; ctx.beginPath(); ctx.moveTo(x, T); ctx.lineTo(x, H - B); ctx.stroke();
    const d = new Date(t);
    ctx.fillText(d.toTimeString().slice(0, 5), x, H - B + 7);
  }

  // SOE markers for the devices being trended
  ctx.textBaseline = 'alphabetic';
  Trend.events.forEach(ev => {
    if (ev.t < t0 || ev.t > now) return;
    const x = Math.round(X(ev.t)) + .5;
    ctx.strokeStyle = ev.color; ctx.globalAlpha = .55; ctx.setLineDash([3, 4]);
    ctx.beginPath(); ctx.moveTo(x, T); ctx.lineTo(x, H - B); ctx.stroke();
    ctx.setLineDash([]); ctx.globalAlpha = 1; ctx.fillStyle = ev.color;
    ctx.beginPath(); ctx.moveTo(x - 5, T - 2); ctx.lineTo(x + 5, T - 2); ctx.lineTo(x, T + 6); ctx.fill();
  });

  // Hover crosshair + tooltip
  const hv = Trend.hover;
  if (hv && hv.x > L && hv.x < W - R) {
    const t = t0 + (hv.x - L) / (W - L - R) * span;
    ctx.strokeStyle = '#e6edf3'; ctx.globalAlpha = .45;
    ctx.beginPath(); ctx.moveTo(hv.x + .5, T); ctx.lineTo(hv.x + .5, H - B); ctx.stroke(); ctx.globalAlpha = 1;
    const lines = [new Date(t).toTimeString().slice(0, 8)];
    const near = Trend.events.filter(ev => Math.abs(X(ev.t) - hv.x) < 6);
    near.forEach(ev => lines.push('▼ ' + ev.label));
    strips.forEach(({Y, g}) => g.pens.forEach(p => {
      const n = nearest(p.data, t);
      if (!n || Math.abs(n[0] - t) > gapMs) return;
      lines.push([p.color, `${p.name}: ${+n[1].toFixed(3)} ${UNIT_LABEL[p.unit] || p.unit}`]);
      ctx.fillStyle = p.color; ctx.beginPath(); ctx.arc(X(n[0]), Y(n[1]), 4, 0, 7); ctx.fill();
    }));
    const tw = Math.max(...lines.map(l => ctx.measureText(Array.isArray(l) ? l[1] : l).width)) + 24;
    const th = lines.length * 16 + 10;
    let bx = hv.x + 14; if (bx + tw > W - R) bx = hv.x - tw - 14;
    const by = Math.min(Math.max(T, hv.y - th / 2), H - B - th);
    ctx.fillStyle = 'rgba(13,17,23,.94)'; ctx.strokeStyle = '#30363d';
    ctx.fillRect(bx, by, tw, th); ctx.strokeRect(bx + .5, by + .5, tw - 1, th - 1);
    ctx.textAlign = 'left';
    lines.forEach((l, i) => {
      const y = by + 18 + i * 16;
      if (Array.isArray(l)) { ctx.fillStyle = l[0]; ctx.fillRect(bx + 8, y - 8, 8, 8); ctx.fillStyle = '#e6edf3'; ctx.fillText(l[1], bx + 20, y); }
      else { ctx.fillStyle = i === 0 ? '#8b949e' : '#e3b341'; ctx.fillText(l, bx + 8, y); }
    });
  }
}

function trendLoop() {
  drawTrend();
  Trend.raf = requestAnimationFrame(trendLoop);
}

// ─── Console registration ────────────────────────────────────────────
const EVENT_COLORS = [[/TRIP|FAULT|UNAUTHORISED/, '#f85149'], [/SWITCH/, '#39c5cf'], [/FLISR|RESTORE/, '#bc8cff'],
                      [/BREAKER_(OPEN|CLOSED)/, '#58a6ff']];

CONSOLES.trend = async () => {
  if (!Trend.built) buildTrend();
  if (!Trend.ready) {
    await Promise.all(App.stations.map(s => detail(s.id)));
    let keys = ['1:power_mw', '1:temperature', '3:current', '5:current'], saved = null;
    try { saved = JSON.parse(localStorage.getItem(TREND_KEY)); } catch (e) { /* default pens */ }
    if (saved && Array.isArray(saved.pens)) { keys = saved.pens; Trend.minutes = saved.minutes || 15; }
    markWindow();
    await Promise.all(keys.map(k => addPen(k, true)));
    Trend.ready = true;
    renderPicker();
  }
  if (Trend.pens.length) {
    const rows = await api('/api/live?points=' + Trend.pens.map(p => p.key).join(','));
    rows.forEach(r => {
      const p = Trend.pens.find(x => x.slave === r.slave_id && x.param === r.parameter);
      const t = parseTs(r.timestamp), last = p && p.data[p.data.length - 1];
      if (p && (!last || t > last[0])) p.data.push([t, r.value]);
    });
    const cut = Date.now() - 25 * 3600e3;
    Trend.pens.forEach(p => { while (p.data.length && p.data[0][0] < cut) p.data.shift(); });
    const devs = new Set(Trend.pens.map(p => p.devId));
    const soe = await cached('/api/soe?limit=400');
    Trend.events = soe.filter(e => devs.has(e.device_id) && /TRIP|FAULT|BREAKER|FLISR|RESTORE|UNAUTHORISED/.test(e.event_type))
      .map(e => ({t: parseTs(e.timestamp_ms.slice(0, 19)), label: `${e.event_type} ${e.device_name}`,
                  color: (EVENT_COLORS.find(([re]) => re.test(e.event_type)) || [0, '#8b949e'])[1]}));
  }
  renderLegend();
  drawTrend();          // also when animation frames are throttled (background tab)
};

CONSOLE_HOOKS.trend = {
  show() { if (!Trend.built) buildTrend(); if (!Trend.raf) trendLoop(); },
  hide() { cancelAnimationFrame(Trend.raf); Trend.raf = null; },
};
