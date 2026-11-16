// workspace.js - console switching and the System Operator Console (SOC).
// The SOC has two zones: substation displays on top, panels below. A zone
// shows 1 item full width, 2 side by side, and 3 or more as Chrome-style
// tabs. Items are dragged in from the Displays / Panels menus (or clicked),
// tabs and pane headers can be dragged to reorder, and x closes them.
// Panes are kept alive while open, so the feeder map keeps its animation
// state across tab switches. The layout is remembered in this browser.

const PANELS = {alarms: AlarmPanel, events: EventPanel, feeder: FeederPanel, equipment: EquipmentPanel};
const PANEL_INFO = {
  alarms:    'Alarm list: acknowledge, clear, bulk clear',
  events:    'Sequence of events (SOE log)',
  feeder:    'Feeder network map, supply impact, FLISR',
  equipment: 'Equipment table of the focused substation',
};
const LAYOUT_KEY = 'mini-scada-soc-v1';
const WS = {displays: ['SS-01'], panels: ['alarms'], active: {displays: 'SS-01', panels: 'alarms'}, focus: 'SS-01'};
const panes = {displays: {}, panels: {}};      // key -> {el, body, built}
let dragItem = null;                           // {zone, key, from: 'menu' | 'tab'}

function loadLayout() {
  try {
    const s = JSON.parse(localStorage.getItem(LAYOUT_KEY));
    if (s && Array.isArray(s.displays) && Array.isArray(s.panels)) Object.assign(WS, s);
  } catch (e) { /* private window or blocked storage: keep the default layout */ }
  WS.panels = WS.panels.filter(k => PANELS[k]);
}

function saveLayout() {
  try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(WS)); } catch (e) { /* not persisted */ }
}

function itemTitle(zone, key) {
  if (zone === 'panels') return PANELS[key].title();
  const s = App.stations.find(x => x.id === key);
  return s ? `${s.short_name} ${s.id}` : key;
}

const visibleItems = zone => WS[zone].length <= 2 ? WS[zone] : [WS.active[zone]];

// ─── Items ───────────────────────────────────────────────────────────
function addItem(zone, key, beforeKey) {
  if (!WS[zone].includes(key)) {
    const i = beforeKey ? WS[zone].indexOf(beforeKey) : -1;
    WS[zone].splice(i < 0 ? WS[zone].length : i, 0, key);
  }
  WS.active[zone] = key;
  if (zone === 'displays') WS.focus = key;
  saveLayout(); renderZones(); refresh();
}

function closeItem(zone, key) {
  WS[zone] = WS[zone].filter(k => k !== key);
  const p = panes[zone][key];
  if (p) { p.el.remove(); delete panes[zone][key]; }
  if (WS.active[zone] === key) WS.active[zone] = WS[zone][0] || null;
  if (zone === 'displays' && WS.focus === key) WS.focus = WS.displays[0] || null;
  saveLayout(); renderZones(); refresh();
}

function moveItem(zone, key, beforeKey) {
  if (key === beforeKey) return;
  const list = WS[zone].filter(k => k !== key);
  const i = beforeKey ? list.indexOf(beforeKey) : -1;
  list.splice(i < 0 ? list.length : i, 0, key);
  WS[zone] = list;
  saveLayout(); renderZones();
}

function activate(zone, key) {
  WS.active[zone] = key;
  if (zone === 'displays') WS.focus = key;
  saveLayout(); renderZones(); refresh();
}

function paneFor(zone, key) {
  if (panes[zone][key]) return panes[zone][key];
  const el = document.createElement('div');
  el.className = 'pane';
  el.dataset.key = key;
  el.innerHTML = `<div class="ph" draggable="true"><span class="ptitle"></span><button class="x" title="Close">&times;</button></div><div class="pb"></div>`;
  const p = {el, body: el.querySelector('.pb'), built: false};
  el.querySelector('.x').addEventListener('click', () => closeItem(zone, key));
  el.querySelector('.ph').addEventListener('dragstart', e => startDrag(e, {zone, key, from: 'tab'}));
  if (zone === 'displays') {
    p.body.innerHTML = '<div class="stn"></div><div class="sld"><div class="empty">Loading diagram...</div></div>';
    p.built = true;
    el.addEventListener('mousedown', () => {
      if (WS.focus !== key) { WS.focus = key; saveLayout(); updateTitles(); refresh(); }
    });
  }
  panes[zone][key] = p;
  return p;
}

// ─── Layout ──────────────────────────────────────────────────────────
function renderZones() {
  ['displays', 'panels'].forEach(zone => {
    const z = $('z-' + zone), list = WS[zone];
    const mode = list.length <= 1 ? 'one' : list.length === 2 ? 'split' : 'tabs';
    z.className = 'zone m-' + mode;
    if (!list.includes(WS.active[zone])) WS.active[zone] = list[0] || null;
    const strip = z.querySelector('.tabs'), box = z.querySelector('.panes');
    strip.hidden = mode !== 'tabs';
    strip.innerHTML = mode !== 'tabs' ? '' : list.map(k =>
      `<div class="tab${k === WS.active[zone] ? ' on' : ''}" draggable="true" data-key="${esc(k)}">
         <span class="ttl2">${esc(itemTitle(zone, k))}</span><button class="x" data-close="${esc(k)}" title="Close">&times;</button></div>`).join('');
    box.querySelectorAll(':scope > .hint').forEach(h => h.remove());
    list.forEach(k => {
      const p = paneFor(zone, k);
      box.appendChild(p.el);                      // moves, never rebuilds
      p.el.hidden = mode === 'tabs' && k !== WS.active[zone];
    });
    if (!list.length) {
      box.insertAdjacentHTML('beforeend', `<div class="hint">${zone === 'displays'
        ? 'Drag a substation here from Displays'
        : 'Drag panels here from Panels: alarms, events, feeder schematic, equipment'}</div>`);
    }
  });
  if (WS.focus && !WS.displays.includes(WS.focus)) WS.focus = WS.displays[0] || null;
  updateTitles();
  renderMenus();
}

function updateTitles() {
  ['displays', 'panels'].forEach(zone => {
    WS[zone].forEach(k => {
      const p = panes[zone][k], t = itemTitle(zone, k);
      if (p) p.el.querySelector('.ptitle').textContent = t + (zone === 'displays' && k === WS.focus && WS.displays.length > 1 ? '  (focus)' : '');
    });
    $('z-' + zone).querySelectorAll('.tab').forEach(tab => { tab.querySelector('.ttl2').textContent = itemTitle(zone, tab.dataset.key); });
  });
}

function renderMenus() {
  $('m-displays').innerHTML = '<div class="lh">Substations: drag onto the console, or click</div>' + App.stations.map(s => {
    const a = s.alarms_by_priority;
    const worst = PRIO.find(p => a[p]);
    return `<div class="mi" draggable="true" data-zone="displays" data-key="${esc(s.id)}"><span class="grip">&#8942;&#8942;</span>
      <span><b>${esc(s.short_name)}</b> ${esc(s.id)}<div class="sub2">${esc(s.name)}</div></span>
      ${worst ? `<span class="chip ${worst}" style="margin-left:auto">${SHORT[worst]} ${a[worst]}</span>` : ''}
      ${WS.displays.includes(s.id) ? '<span class="isopen">open</span>' : ''}</div>`;
  }).join('');
  $('m-panels').innerHTML = '<div class="lh">Panels: drag onto the console, or click</div>' + Object.keys(PANELS).map(k =>
    `<div class="mi" draggable="true" data-zone="panels" data-key="${k}"><span class="grip">&#8942;&#8942;</span>
      <span><b>${esc(PANELS[k].title().split(' - ')[0])}</b><div class="sub2">${esc(PANEL_INFO[k])}</div></span>
      ${WS.panels.includes(k) ? '<span class="isopen">open</span>' : ''}</div>`).join('');
}

// ─── Drag and drop ───────────────────────────────────────────────────
function startDrag(e, item) {
  dragItem = item;
  e.dataTransfer.setData('text/plain', item.zone + ':' + item.key);    // Firefox needs data to start a drag
  e.dataTransfer.effectAllowed = 'move';
  document.querySelectorAll('.menu .list').forEach(l => l.classList.add('dragging'));
}

function endDrag() {
  dragItem = null;
  document.querySelectorAll('.zone.drop').forEach(z => z.classList.remove('drop'));
  document.querySelectorAll('.dragover').forEach(x => x.classList.remove('dragover'));
  document.querySelectorAll('.menu .list').forEach(l => { l.classList.remove('dragging'); l.hidden = true; });
}

function dropKeyAt(target, zone) {
  // Dropping onto a tab or pane header of the same zone inserts before it
  const t = target.closest('.tab, .pane');
  return t && t.closest('.zone') === $('z-' + zone) ? t.dataset.key : null;
}

function wireConsoleDnD() {
  const c = $('c-soc');
  c.addEventListener('dragover', e => {
    if (!dragItem) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    $('z-' + dragItem.zone).classList.add('drop');
    document.querySelectorAll('.dragover').forEach(x => x.classList.remove('dragover'));
    const k = dropKeyAt(e.target, dragItem.zone);
    if (k) {
      const el = e.target.closest('.tab') || e.target.closest('.pane').querySelector('.ph');
      el.classList.add('dragover');
    }
  });
  c.addEventListener('drop', e => {
    if (!dragItem) return;
    e.preventDefault();
    const {zone, key, from} = dragItem, before = dropKeyAt(e.target, zone);
    endDrag();
    if (from === 'menu') addItem(zone, key, before);
    else moveItem(zone, key, before);
  });
  document.addEventListener('dragend', endDrag);

  ['displays', 'panels'].forEach(zone => {
    const strip = $('z-' + zone).querySelector('.tabs');
    strip.addEventListener('click', e => {
      const x = e.target.closest('[data-close]');
      if (x) { closeItem(zone, x.dataset.close); return; }
      const t = e.target.closest('.tab');
      if (t) activate(zone, t.dataset.key);
    });
    strip.addEventListener('dragstart', e => {
      const t = e.target.closest('.tab');
      if (t) startDrag(e, {zone, key: t.dataset.key, from: 'tab'});
    });
  });
}

function wireMenus() {
  ['displays', 'panels'].forEach(name => {
    const btn = $('b-' + name), list = $('m-' + name);
    btn.addEventListener('click', e => {
      e.stopPropagation();
      const open = list.hidden;
      document.querySelectorAll('.menu .list').forEach(l => { l.hidden = true; });
      list.hidden = !open;
      if (open) renderMenus();
    });
    list.addEventListener('dragstart', e => {
      const mi = e.target.closest('.mi');
      if (mi) startDrag(e, {zone: mi.dataset.zone, key: mi.dataset.key, from: 'menu'});
    });
    list.addEventListener('click', e => {
      const mi = e.target.closest('.mi');
      if (mi) { list.hidden = true; addItem(mi.dataset.zone, mi.dataset.key); }
    });
  });
  document.addEventListener('click', e => {
    if (!e.target.closest('.menu')) document.querySelectorAll('.menu .list').forEach(l => { l.hidden = true; });
  });
}

// ─── Consoles ────────────────────────────────────────────────────────
function showConsole(name) {
  if (CONSOLE_HOOKS[App.console] && CONSOLE_HOOKS[App.console].hide) CONSOLE_HOOKS[App.console].hide();
  App.console = name;
  document.querySelectorAll('.nav button').forEach(b => b.classList.toggle('on', b.dataset.console === name));
  document.querySelectorAll('.console').forEach(c => { c.hidden = c.id !== 'c-' + name; });
  $('soc-tools').hidden = name !== 'soc';
  if (CONSOLE_HOOKS[name] && CONSOLE_HOOKS[name].show) CONSOLE_HOOKS[name].show();
  try { localStorage.setItem('mini-scada-console', name); } catch (e) { /* not persisted */ }
  refresh();
}

CONSOLES.soc = async () => {
  renderMenus();
  const jobs = visibleItems('displays').map(async id => {
    const d = await detail(id), p = paneFor('displays', id);
    renderStationStrip(p.body.querySelector('.stn'), id);
    renderSLD(p.body.querySelector('.sld'), d);
  });
  visibleItems('panels').forEach(k => {
    const p = paneFor('panels', k);
    if (!p.built) { PANELS[k].build(p.body); p.built = true; }
    jobs.push(PANELS[k].update());
  });
  const res = await Promise.allSettled(jobs);
  const bad = res.find(r => r.status === 'rejected');
  if (bad) showErr('Display update failed: ' + bad.reason.message, true);
  updateTitles();
};

function startWorkspace() {
  loadLayout();
  wireMenus();
  wireConsoleDnD();
  document.querySelectorAll('.nav button').forEach(b => b.addEventListener('click', () => showConsole(b.dataset.console)));
  renderZones();
  let start = 'soc';
  try { start = localStorage.getItem('mini-scada-console') || 'soc'; } catch (e) { /* default */ }
  showConsole(CONSOLES[start] ? start : 'soc');
}
