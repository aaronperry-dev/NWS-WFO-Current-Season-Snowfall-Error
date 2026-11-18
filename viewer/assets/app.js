/* NWS WFO current-season snowfall forecast error tracker.
 * Shows, per office, the season-to-date mean error and the error of each verified snow event. Loads raw float32 .bin grids (via api.php, or straight from data/ on static hosting)
 * and paints them on a Leaflet map with a custom tile layer that samples the grids per screen pixel (nearest cell), so the cells stay crisp, unsmoothed and correctly placed at every zoom.
 * Grid file format (see manifest.json): little-endian float32, row-major, row 0 = north edge, NaN = no data, -999 = excluded radar sector. */
(() => {
'use strict';
/* Two ways to get the data: through api.php (PHP hosting), or, when the page sets window.NWS.dataDir, straight from static files (GitHub Pages). */
const API = (window.NWS && window.NWS.api) || 'api.php', DATA_DIR = window.NWS && window.NWS.dataDir;
const url = {
  manifest: () => DATA_DIR ? `${DATA_DIR}manifest.json` : `${API}?action=manifest`,
  cwa: () => DATA_DIR ? `${DATA_DIR}cwa.geojson` : `${API}?action=cwa`,
  bin: (season, off, e) => DATA_DIR ? DATA_DIR + e.file.split('/').map(encodeURIComponent).join('/') : `${API}?action=bin&season=${encodeURIComponent(season)}&office=${encodeURIComponent(off)}`,
  event: (season, off, ev) => DATA_DIR ? DATA_DIR + ev.file.split('/').map(encodeURIComponent).join('/') : `${API}?action=event&season=${encodeURIComponent(season)}&office=${encodeURIComponent(off)}&id=${encodeURIComponent(ev.id)}`,
};
const MINUS = '−', EXCLUDED = -999;
const el = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = (v, d = 2) => { if (v == null || !isFinite(v)) return '–'; const r = Number(v.toFixed(d)); return (r < 0 ? MINUS : '+') + Math.abs(r).toFixed(d); };
const pct = (v, d = 0) => (v == null || !isFinite(v)) ? '–' : v.toFixed(d) + '%';

/* ---------- color scale: one diverging ramp; the range is fixed per layer (season mean +/-2 in, single event +/-6 in) ---------- */
const STOPS = ['#1b5fa8', '#6fa3d6', '#cfe0f1', '#f0efec', '#f4d3c4', '#e48a6c', '#b8321f'].map((h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)));
const LUT_N = 512; const LUT = new Uint8ClampedArray(LUT_N * 4);
(function buildLut() {
  for (let k = 0; k < LUT_N; k++) {
    const t = k / (LUT_N - 1) * (STOPS.length - 1), i = Math.min(Math.floor(t), STOPS.length - 2), f = t - i;
    for (let c = 0; c < 3; c++) LUT[k * 4 + c] = STOPS[i][c] + (STOPS[i + 1][c] - STOPS[i][c]) * f;
    LUT[k * 4 + 3] = 255;
  }
})();
const lutIndex = (v, lim) => Math.round((Math.min(Math.max(v, -lim), lim) + lim) / (2 * lim) * (LUT_N - 1));
function colorOf(v, lim = 2) { const k = lutIndex(v, lim) * 4; return [LUT[k], LUT[k + 1], LUT[k + 2]]; }
const inkFor = ([r, g, b]) => (0.299 * r + 0.587 * g + 0.114 * b) < 140 ? '#fff' : '#111';
const chipStyle = (v, lim) => { const c = colorOf(v, lim); return `background:rgb(${c});color:${inkFor(c)}`; };

/* ---------- grid painting ---------- */
function paintTile(canvas, coords, grids) {
  const W = canvas.width, H = canvas.height, S = W / 256, ctx = canvas.getContext('2d'), img = ctx.createImageData(W, H), px = img.data, n = 2 ** coords.z;
  const lons = new Float64Array(W), lats = new Float64Array(H);
  for (let i = 0; i < W; i++) lons[i] = (coords.x + (i + 0.5) / W) / n * 360 - 180;
  for (let j = 0; j < H; j++) lats[j] = Math.atan(Math.sinh(Math.PI * (1 - 2 * (coords.y + (j + 0.5) / H) / n))) * 180 / Math.PI;
  let any = false;
  for (const g of grids) {
    if (lons[0] > g.west + g.nx * g.dx || lons[W - 1] < g.west || lats[0] < g.north - g.ny * g.dy || lats[H - 1] > g.north) continue;
    for (let j = 0; j < H; j++) {
      const iy = Math.floor((g.north - lats[j]) / g.dy); if (iy < 0 || iy >= g.ny) continue;
      const row = iy * g.nx;
      for (let i = 0; i < W; i++) {
        const ix = Math.floor((lons[i] - g.west) / g.dx); if (ix < 0 || ix >= g.nx) continue;
        const v = g.data[row + ix]; if (v !== v) continue;                       // NaN: outside the CWA / too few valid events
        const o = (j * W + i) * 4; any = true;
        if (v === EXCLUDED) {                                                    // radar beam-blockage sector: gray hatch, anchored to the page grid
          const dark = ((((coords.x * W + i) + (coords.y * H + j)) / S) | 0) % 8 < 2;
          px[o] = dark ? 70 : 170; px[o + 1] = dark ? 70 : 170; px[o + 2] = dark ? 70 : 170; px[o + 3] = dark ? 235 : 190;
        } else { const k = lutIndex(v, g.lim) * 4; px[o] = LUT[k]; px[o + 1] = LUT[k + 1]; px[o + 2] = LUT[k + 2]; px[o + 3] = 255; }
      }
    }
  }
  if (any) ctx.putImageData(img, 0, 0);
}
const ErrorLayer = L.GridLayer.extend({
  initialize(opts) { L.GridLayer.prototype.initialize.call(this, opts); this.grids = []; },
  createTile(coords) {
    const c = document.createElement('canvas'), S = this.options.scale; c.width = c.height = 256 * S; c.className = 'leaflet-tile';
    paintTile(c, coords, this.grids); return c;
  }
});
function sample(grids, lat, lon) {
  for (const g of grids) {
    const iy = Math.floor((g.north - lat) / g.dy), ix = Math.floor((lon - g.west) / g.dx);
    if (iy < 0 || iy >= g.ny || ix < 0 || ix >= g.nx) continue;
    const v = g.data[iy * g.nx + ix]; if (v === v) return { off: g.off, v, lim: g.lim, event: !!g.event };
  }
  return null;
}
function toFloat32LE(buf) {
  if (new Uint8Array(new Uint32Array([1]).buffer)[0] === 1) return new Float32Array(buf);       // little-endian platform: view directly
  const dv = new DataView(buf), out = new Float32Array(buf.byteLength / 4);
  for (let i = 0; i < out.length; i++) out[i] = dv.getFloat32(i * 4, true); return out;
}

/* ---------- state ---------- */
const state = { M: null, season: null, offs: [], grids: [], ev: null, evGrid: null, sel: null, token: 0, base: 'dark', opacity: 0.85 };
let waterGeo, prodBox, lineSets = [], map, errLayer, terrainLayer, bm = {}, baseLabelLayer, cwaGeo, cwaCasing, labelLayer, tipBox, legendBox, cwaData;
/* Our own basemap (built by scripts/build_basemap.py): shaded-relief terrain tiles + water / border / state-line vectors + labels, all served from basemap/. */
const BASE_DIR = 'basemap/', LAND_PLAIN = '#eef0ea', WATER_FILL = '#c4d8e4', LAND_DARK = '#06090d', WATER_DARK = '#0a1a29';
const BASEMAPS = { dark: { name: 'Dark (AWIPS-style)' }, terrain: { name: 'Terrain (shaded relief)' }, plain: { name: 'Plain (no terrain)' } };
const LINE_DARK = { coast: { color: '#8ea3b4', weight: 1 }, state: { color: '#8f99a3', weight: 1.2, dashArray: '5 3' }, border: { color: '#d3dbe2', weight: 2.2 } };
const LINE_STYLE = { coast: { color: '#5f7a8a', weight: 1 }, state: { color: '#59626b', weight: 1.1, dashArray: '5 3' }, border: { color: '#2c343b', weight: 2 } };
const STATUS_TEXT = { ongoing: 'In progress', pending_forecast: 'Awaiting forecast archive', no_forecast: 'No forecast found', missing_analysis: 'Analysis missing' };
const STATUS_HELP = { ongoing: 'Snow is still falling, or fell less than 12 h ago, so the event may still grow.', pending_forecast: 'The event is over, but the NCEI forecast archive (about 5 days behind) does not reach it yet. It is verified automatically once it does.',
  no_forecast: 'The forecast archive covers this day, but no NDFD run covers the event.', missing_analysis: 'A NOHRSC analysis period is missing, so the event cannot be verified.' };

const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'], p2 = (n) => String(n).padStart(2, '0');
const zt = (d) => `${MON[d.getUTCMonth()]} ${d.getUTCDate()} ${p2(d.getUTCHours())}Z`;
function dayRange(a, b) {                                       // 'Nov 12', 'Nov 12–14' or 'Nov 30–Dec 2' (UTC; the end is the last moment of snow, so a 00Z end belongs to the day before)
  const s = new Date(a), e = new Date(new Date(b) - 60000), d1 = `${MON[s.getUTCMonth()]} ${s.getUTCDate()}`;
  if (s.getUTCMonth() === e.getUTCMonth()) return s.getUTCDate() === e.getUTCDate() ? d1 : `${d1}–${e.getUTCDate()}`;
  return `${d1}–${MON[e.getUTCMonth()]} ${e.getUTCDate()}`;
}
const evTitle = (ev) => `${zt(new Date(ev.start))} to ${zt(new Date(ev.end))}`;
const localHelp = (e) => `Localized event: about ${Math.round(e.area_pct)}% of the CWA received at least ${state.M.thresholds.local_min_in} in, but the CWA-mean total (${e.obs_cwa_mean.toFixed(2)} in) is below the ${state.M.thresholds.event_total_in} in threshold.`;
const localTag = (e) => e.basis === 'local' ? ` <span class="loc" title="${esc(localHelp(e))}">localized</span>` : '';

function setTitle(label) {                                   // 'NWS WFO Snowfall Forecast Error: 2026–27 Winter Season' (page title and header follow the selected season)
  const t = `NWS WFO Snowfall Forecast Error: ${String(label).replace(/\s*season$/i, '')} Winter Season`; document.title = t; const h = document.querySelector('.topbar h1'); if (h) h.textContent = t;
}

function notice(msg) { const n = el('notice'); n.hidden = !msg; n.textContent = msg || ''; }
async function getJson(url) { const r = await fetch(url, { cache: 'no-cache' }); if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`); return r.json(); }
async function getGrid(url, e) {
  const r = await fetch(url, { cache: 'no-cache' }); if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const buf = await r.arrayBuffer(); if (buf.byteLength !== e.nx * e.ny * 4) throw new Error(`expected ${e.nx * e.ny * 4} bytes, got ${buf.byteLength}`);
  return toFloat32LE(buf);
}
const seasonData = () => state.M.seasons[state.season];
const visibleGrids = () => state.grids.filter((g) => g.visible);
const activeGrids = () => state.ev && state.evGrid ? [state.evGrid] : visibleGrids();
const offEntry = (off) => state.offs.find((o) => o.off === off);
const eventOf = (off, id) => { const o = offEntry(off); return o && o.events.find((e) => e.id === id); };
const LIM = () => ({ season: state.M.layers.season.vmax, event: state.M.layers.event.vmax });

/* ---------- hash state ---------- */
function readHash() { const p = new URLSearchParams(location.hash.slice(1)); return { season: p.get('season'), office: p.get('office'), event: p.get('event'), base: p.get('base'), op: parseFloat(p.get('op')) }; }
function writeHash() {
  const p = new URLSearchParams({ season: state.season, base: state.base, op: state.opacity.toFixed(2) }); if (state.sel) p.set('office', state.sel); if (state.ev) p.set('event', state.ev.off + '-' + state.ev.id);
  history.replaceState(null, '', '#' + p.toString());
}

/* ---------- data loading ---------- */
async function loadSeason(id) {
  const token = ++state.token, s = state.M.seasons[id]; notice('');
  try {
    const offs = state.M.order.filter((o) => s.offices[o]).map((o) => Object.assign({ off: o, visible: true }, s.offices[o]));
    const grids = await Promise.all(offs.filter((o) => o.file).map(async (o) => Object.assign({}, o, { data: await getGrid(url.bin(id, o.off, o), o).catch((e) => { throw new Error(`${o.off}: ${e.message}`); }), lim: LIM().season })));
    if (token !== state.token) return;                      // a newer season was chosen meanwhile
    el('topSeason').textContent = s.label || id; setTitle(s.label || id); state.season = id; state.offs = offs; state.grids = grids; state.ev = null; state.evGrid = null;
    if (state.sel && !s.offices[state.sel]) state.sel = null;
    refresh();
  } catch (err) { notice('Could not load data: ' + err.message); }
}
async function showEvent(off, id) {
  const o = offEntry(off), ev = eventOf(off, id); if (!o || !ev || !ev.file) return;
  const token = ++state.token;
  try {
    const data = await getGrid(url.event(state.season, off, ev), o); if (token !== state.token) return;
    state.ev = { off, id }; state.sel = off; state.evGrid = Object.assign({}, o, { data, lim: LIM().event, event: true, visible: true });
    refresh(); fitOffice(off);
  } catch (err) { notice('Could not load the event grid: ' + err.message); }
}
function clearEvent() { if (!state.ev) return; state.ev = null; state.evGrid = null; refresh(); }

/* ---------- rendering ---------- */
function refresh() {
  errLayer.grids = activeGrids(); errLayer.redraw(); drawStatus(); drawTable(); drawEvents(); drawLog(); drawProduct(); drawLabels(); styleCwa(); drawDetail(); drawLegend(); writeHash();
}
function drawStatus() {
  const s = seasonData(), box = el('status'), total = state.offs.reduce((n, o) => n + o.events.length, 0), ver = state.offs.reduce((n, o) => n + o.events.filter((e) => e.status === 'verified').length, 0);
  const pend = state.offs.reduce((n, o) => n + o.events.filter((e) => e.status === 'pending_forecast').length, 0), on = state.offs.reduce((n, o) => n + o.events.filter((e) => e.status === 'ongoing').length, 0);
  const upd = s.updated ? `Updated ${zt(new Date(s.updated))}` : '', an = s.nohrsc_through ? `NOHRSC analysis through ${zt(new Date(s.nohrsc_through))}` : '', ar = s.ndfd_archive_complete_through ? `forecast archive complete through ${MON[+s.ndfd_archive_complete_through.slice(5, 7) - 1]} ${+s.ndfd_archive_complete_through.slice(8)}` : '';
  const bar = el('statusbar'); if (bar) bar.textContent = [upd, an, ar].filter(Boolean).join('   \u00b7   ');
  let html = '';
  if (!total) {
    const tots = state.offs.filter((o) => o.observed_total_in != null).map((o) => `${esc(o.off)} ${o.observed_total_in.toFixed(2)} in`).join(' &middot; ');
    html += `<div class="empty"><b>No qualifying snow events yet this season.</b> An event needs a CWA-mean total of at least ${state.M.thresholds.event_total_in} in, or at least ${state.M.thresholds.local_area_pct}% of the CWA receiving ${state.M.thresholds.local_min_in} in or more. ${tots ? 'CWA-mean snowfall so far (all small bursts): ' + tots + '.' : ''}</div>`;
  } else html += `<div class="counts"><b>${total}</b> events detected &middot; <b>${ver}</b> verified${pend ? ` &middot; ${pend} awaiting the forecast archive` : ''}${on ? ` &middot; ${on} in progress` : ''}</div>`;
  box.innerHTML = html;
}
function drawTable() {
  const small = state.M.small_sample_events, rows = state.offs.map((o) => {
    const st = o.stats, ver = o.events.filter((e) => e.status === 'verified').length, tot = o.events.length, g = state.grids.find((x) => x.off === o.off), vis = !g || g.visible;
    const cls = ['row', state.sel === o.off ? 'sel' : '', vis ? '' : 'off'].join(' '), c = st ? colorOf(st.mean_error, 2) : null, m = state.M.offices[o.off];
    return `<tr class="${cls}" data-off="${o.off}" tabindex="0">
      <td><input type="checkbox" data-vis="${o.off}" ${vis ? 'checked' : ''} ${g ? '' : 'disabled'} aria-label="Show ${esc(o.off)}"></td>
      <td class="codename"><span class="chip" style="background:${esc(m.color)}"></span><b>${esc(o.off)}</b><small>${esc(m.name)}</small></td>
      <td title="verified events / all detected">${ver}${tot > ver ? ` <small class="of">/ ${tot}</small>` : ''}${st && st.small_sample ? ` <span class="warn" title="Fewer than ${small} verified events: indicative only">&#9888;</span>` : ''}</td>
      <td>${st ? `<span class="val" style="background:rgb(${c});color:${inkFor(c)}">${fmt(st.mean_error)}</span>` : '<span class="na">–</span>'}</td>
      <td>${st ? st.mae.toFixed(2) : '–'}</td><td>${st ? pct(st.hit_rate) : '–'}</td></tr>`;
  }).join('');
  el('offices').innerHTML = `<thead><tr><th></th><th>Office</th><th>Events</th><th>Mean error (in)</th><th>MAE (in)</th><th>Hit</th></tr></thead><tbody>${rows}</tbody>`;
}
function drawEvents() {
  const sec = el('eventsSection'), list = [];
  for (const o of state.offs) if (!state.sel || state.sel === o.off) for (const e of o.events) list.push(Object.assign({ off: o.off }, e));
  const total = state.offs.reduce((n, o) => n + o.events.length, 0); sec.hidden = !total; if (!total) return;
  list.sort((a, b) => b.start.localeCompare(a.start) || a.off.localeCompare(b.off));
  const L = LIM().event;
  el('eventsTitle').textContent = state.sel ? `Events: ${state.sel}` : 'Events, newest first';
  el('events').innerHTML = `<thead><tr><th>Event (UTC)</th><th>Office</th><th class="c-sm">Obs.</th><th class="c-sm">Fcst.</th><th>Error (in)</th><th>MAE</th></tr></thead><tbody>${list.map((e) => {
    const m = state.M.offices[e.off], sel = state.ev && state.ev.off === e.off && state.ev.id === e.id, ver = e.status === 'verified';
    const head = `<td class="evtime"><b>${esc(dayRange(e.start, e.end))}</b><small>${esc(evTitle(e))}${localTag(e)}</small></td><td><span class="chip" style="background:${esc(m.color)}"></span><b>${esc(e.off)}</b></td>`;
    if (!ver) return `<tr class="ev pend" data-off="${e.off}" data-id="${e.id}" title="${esc(STATUS_HELP[e.status] || '')}">${head}<td class="c-sm">${e.obs_cwa_mean.toFixed(1)}</td><td colspan="3" class="badge"><span class="st st-${esc(e.status)}">${esc(STATUS_TEXT[e.status] || e.status)}</span></td></tr>`;
    return `<tr class="ev ${sel ? 'sel' : ''}" data-off="${e.off}" data-id="${e.id}" tabindex="0" title="Show the error map of this event">${head}<td class="c-sm">${e.mean_observed.toFixed(1)}</td><td class="c-sm">${e.mean_forecast.toFixed(1)}</td>
      <td><span class="val" style="${chipStyle(e.mean_error, L)}">${fmt(e.mean_error, 1)}</span></td><td>${e.mae.toFixed(1)}</td></tr>`;
  }).join('')}</tbody>`;
}
function drawLog() {
  const sec = el('logSection'), box = el('log'); if (!state.offs.length) { sec.hidden = true; return; } sec.hidden = false;
  const th = state.M.thresholds, rows = [];
  for (const o of state.offs) if (!state.sel || state.sel === o.off) for (const x of (o.episodes || [])) rows.push({ o, x });
  rows.sort((a, b) => b.x.start.localeCompare(a.x.start) || a.o.off.localeCompare(b.o.off));
  const nev = rows.filter((r) => r.x.event_ids && r.x.event_ids.length).length;
  el('logTitle').textContent = `Snowfall log: ${state.sel || 'all WFOs'} — ${rows.length} measurable episode${rows.length === 1 ? '' : 's'}, ${nev} counted as event${nev === 1 ? '' : 's'}`;
  if (!rows.length) { box.innerHTML = '<p class="hint">No measurable snowfall yet this season.</p>'; return; }
  box.innerHTML = `<table class="events log"><thead><tr><th>Office</th><th>Snowfall (UTC)</th><th>Event?</th><th title="CWA-mean total, inches">Mean (in)</th><th title="Highest cell, inches">Peak (in)</th><th title="Share of the CWA with at least ${th.local_min_in} in">&ge;${th.local_min_in} in</th></tr></thead><tbody>${rows.map(({ o, x }) => {
    const m = state.M.offices[o.off], evs = (x.event_ids || []).map((id) => o.events.find((e) => e.id === id)).filter(Boolean), ver = evs.find((e) => e.status === 'verified'), other = evs[0];
    let tag = '<span class="na">not an event</span>', cls = 'lg', attr = '';
    if (x.ongoing) tag = '<span class="st st-ongoing">In progress</span>';
    else if (ver) { tag = '<span class="st st-verified">Verified event</span>'; cls = 'lg click'; attr = ` data-off="${o.off}" data-id="${ver.id}" tabindex="0" title="Show the error map of this event"`; }
    else if (other) tag = `<span class="st st-${esc(other.status)}">${esc(STATUS_TEXT[other.status] || other.status)}</span>`;
    return `<tr class="${cls}"${attr}><td><span class="chip" style="background:${esc(m.color)}"></span><b>${esc(o.off)}</b></td><td class="evtime"><b>${esc(dayRange(x.start, x.end))}</b><small>${esc(evTitle(x))}</small></td><td>${tag}</td><td>${x.mean_in.toFixed(2)}</td><td>${x.max_in.toFixed(1)}</td><td>${Math.round(x.area_ge1_pct)}%</td></tr>`;
  }).join('')}</tbody></table>
  <p class="hint">Every stretch of measurable snow (at least ${th.measurable_in} in on ${th.measurable_area_pct}% of the CWA in a 6-h period), whether or not it is big enough to verify. Mean = CWA-mean total, Peak = highest cell. Verified events can be clicked.</p>`;
}
function drawProduct() {
  if (!prodBox || !state.M) return; const S = seasonData();
  if (state.ev) { const e = eventOf(state.ev.off, state.ev.id), m = state.M.offices[state.ev.off];
    prodBox.innerHTML = `<div class="pl-h"><i class="chip" style="background:${esc(m.color)}"></i>${esc(state.ev.off)} event error &middot; ${esc(evTitle(e))}</div><div>Forecast &minus; observed snowfall, inches &middot; mean ${fmt(e.mean_error)} in</div>`; return; }
  prodBox.innerHTML = `<div class="pl-h">${esc(S.label)} &middot; season-to-date mean error</div><div>Forecast &minus; observed snowfall, inches per event</div>` + state.offs.map((o) => {
    const g = state.grids.find((x) => x.off === o.off), vis = !g || g.visible, m = state.M.offices[o.off], ver = o.events.filter((e) => e.status === 'verified').length;
    return `<div class="pl-o${vis ? '' : ' off'}"><i class="chip" style="background:${esc(m.color)}"></i>${esc(o.off)}&nbsp; ${o.stats ? fmt(o.stats.mean_error) + ' in &middot; ' + ver + ' event' + (ver === 1 ? '' : 's') : 'no verified events'}</div>`;
  }).join('');
}
function drawLabels() {
  labelLayer.clearLayers(); if (!cwaData) return;
  for (const f of cwaData.features) {
    const off = f.properties.cwa, o = offEntry(off); if (!o) continue; const g = state.grids.find((x) => x.off === off); if (g && !g.visible && !state.ev) continue;
    let sub = '';
    if (state.ev) { if (state.ev.off !== off) continue; sub = `${fmt(eventOf(off, state.ev.id).mean_error)} in`; }
    else sub = o.stats ? `${fmt(o.stats.mean_error)} in` : '<i>no events</i>';
    const [lo, la] = f.properties.label;
    labelLayer.addLayer(L.marker([la, lo], { interactive: false, keyboard: false, icon: L.divIcon({ className: '', iconSize: [0, 0], html: `<div class="cwa-label">${esc(off)}<span>${sub}</span></div>` }) }));
  }
}
function styleCwa() {
  if (!cwaGeo) return;
  cwaGeo.eachLayer((l) => { const off = l.feature.properties.cwa, g = state.grids.find((x) => x.off === off), on = state.ev ? state.ev.off === off : (!g || g.visible), sel = state.sel === off;
    l.setStyle({ color: state.M.offices[off].color, weight: sel ? 4.5 : 2.4, opacity: on ? 1 : 0.35, fill: true, fillOpacity: 0.001 }); });
  cwaCasing.eachLayer((l) => { const off = l.feature.properties.cwa, sel = state.sel === off; const dk = state.base === 'dark'; l.setStyle({ color: dk ? '#000' : '#fff', weight: sel ? 8 : 5, opacity: dk ? 0.7 : 0.85, fill: false }); });
}
function radarNote(off, cells, excl) {
  const rx = state.M.radar_exclusions && state.M.radar_exclusions[off];
  return excl ? `<p>${excl} of ${cells + excl} CWA cells are hatched and excluded from every statistic: radar beam-blockage artifact${rx ? ' (' + esc(rx.radar) + ', azimuth ' + rx.azimuth[0] + '&deg;&ndash;' + rx.azimuth[1] + '&deg;, range &gt; ' + rx.min_range_km + ' km)' : ''}.</p>` : '';
}
function drawDetail() {
  const box = el('detail'), o = offEntry(state.sel);
  if (!o) { box.hidden = true; box.innerHTML = ''; return; }
  const m = state.M.offices[o.off], head = `<h3><span class="chip" style="background:${esc(m.color)}"></span>${esc(o.off)} &mdash; ${esc(m.name)}</h3>`; box.hidden = false;
  if (state.ev) {                                                    // one event
    const e = eventOf(o.off, state.ev.id);
    box.innerHTML = `${head}<div><b>Event ${esc(evTitle(e))}</b> (${e.hours} h)</div>${e.basis === 'local' ? `<p class="note">${esc(localHelp(e))}</p>` : ''}<button id="evBack" class="linkbtn" type="button">&larr; Back to season-to-date mean</button>
      <dl>
        <dt>Mean error (forecast &minus; observed)</dt><dd>${fmt(e.mean_error)} in</dd>
        <dt>Mean forecast / mean observed</dt><dd>${e.mean_forecast.toFixed(2)} / ${e.mean_observed.toFixed(2)} in</dd>
        <dt>Percent bias</dt><dd>${fmt(e.percent_bias, 1)}%</dd>
        <dt>Mean absolute error</dt><dd>${e.mae.toFixed(2)} in</dd>
        <dt>RMSE</dt><dd>${e.rmse.toFixed(2)} in</dd>
        <dt>Hit rate (within max(2 in, 30% of observed))</dt><dd>${pct(e.hit_rate)}</dd>
        <dt>Highest cell: observed / forecast</dt><dd>${e.max_observed.toFixed(1)} / ${e.max_forecast.toFixed(1)} in</dd>
      </dl>
      <p>Forecast: NDFD snowfall run issued ${e.run ? esc(zt(new Date(e.run))) : '–'}${e.lead_hours != null ? ', ' + e.lead_hours + ' h before the event began' : ''}. Observed: NOHRSC analysis. Cells are inches in this event; the map color range is &plusmn;${LIM().event} in.</p>
      ${radarNote(o.off, o.cells, o.cells_excluded_radar)}`;
    const back = el('evBack'); if (back) back.onclick = clearEvent; return;
  }
  const st = o.stats, ci = st && st.mean_error_ci90, ver = o.events.filter((e) => e.status === 'verified').length, other = o.events.length - ver;
  if (!st) { box.innerHTML = `${head}<div>${o.events.length ? `${o.events.length} event${o.events.length > 1 ? 's' : ''} detected, none verified yet.` : 'No qualifying events yet this season.'}</div>${o.observed_total_in != null ? `<p>CWA-mean snowfall so far this season: ${o.observed_total_in.toFixed(2)} in.</p>` : ''}`; return; }
  box.innerHTML = `${head}<div>${esc(seasonData().label)} to date: ${ver} verified event${ver > 1 ? 's' : ''}${other ? `, ${other} more not verified yet` : ''}</div>
    ${st.small_sample ? '<p class="warn">Only ' + ver + ' verified event' + (ver > 1 ? 's' : '') + ': treat the season mean as indicative, not conclusive.</p>' : ''}
    <dl>
      <dt>Mean error (forecast &minus; observed)</dt><dd>${fmt(st.mean_error)} in</dd>
      ${ci ? `<dt>90% event-bootstrap interval</dt><dd>${fmt(ci[0])} to ${fmt(ci[1])} in</dd>` : ''}
      <dt>Percent bias</dt><dd>${fmt(st.percent_bias, 1)}%</dd>
      <dt>Mean absolute error</dt><dd>${st.mae.toFixed(2)} in</dd>
      <dt>RMSE</dt><dd>${st.rmse.toFixed(2)} in</dd>
      <dt>Hit rate (within max(2 in, 30% of observed))</dt><dd>${pct(st.hit_rate)}</dd>
      <dt>Mean forecast / mean observed</dt><dd>${st.mean_forecast.toFixed(2)} / ${st.mean_observed.toFixed(2)} in</dd>
      <dt>CWA area with mean error &gt; +0.5 in</dt><dd>${pct(st.area_pct_gt_p05)}</dd>
      <dt>CWA area with mean error &lt; &minus;0.5 in</dt><dd>${pct(st.area_pct_lt_m05)}</dd>
    </dl>
    ${radarNote(o.off, o.cells, o.cells_excluded_radar)}`;
}
function drawLegend() {
  const lay = state.ev ? state.M.layers.event : state.M.layers.season, L = lay.vmax, anyEx = activeGrids().some((g) => g.cells_excluded_radar > 0);
  const tk = [-L, -L / 2, 0, L / 2, L], units = state.ev ? 'inches of snow in this event' : 'inches of snow per event, season to date';
  legendBox.innerHTML = `<div class="ttl">${esc(lay.label)}</div><div>${esc(units)}</div><canvas width="270" height="14"></canvas>
    <div class="ticks">${tk.map((t, i) => `<span>${i === 0 ? '≤ ' : i === 4 ? '≥ ' : ''}${t === 0 ? '0' : fmt(t, 0)}</span>`).join('')}</div>
    <div class="ends"><span>forecast too low</span><span>forecast too high</span></div>
    ${anyEx ? '<div class="hatch"><i></i>Excluded: radar beam-blockage artifact</div>' : ''}`;
  const c = legendBox.querySelector('canvas'), x = c.getContext('2d');
  for (let i = 0; i < c.width; i++) { const [r, g, b] = colorOf(-L + 2 * L * i / (c.width - 1), L); x.fillStyle = `rgb(${r},${g},${b})`; x.fillRect(i, 0, 1, c.height); }
}

/* ---------- selection ---------- */
function fitOffice(off) { const g = offEntry(off); if (g) map.fitBounds([[g.north - g.ny * g.dy, g.west], [g.north, g.west + g.nx * g.dx]], { padding: [20, 20], maxZoom: 9 }); }
function select(off, zoom = true) {
  state.sel = (state.sel === off && !zoom) ? null : off; if (state.ev && state.ev.off !== state.sel) { state.ev = null; state.evGrid = null; refresh(); } else { styleCwa(); drawTable(); drawEvents(); drawLog(); drawProduct(); drawDetail(); writeHash(); }
  if (zoom && state.sel) fitOffice(off);
}
function deselect() { if (state.ev) { clearEvent(); return; } if (state.sel) { state.sel = null; styleCwa(); drawTable(); drawEvents(); drawLog(); drawProduct(); drawDetail(); writeHash(); } }

/* ---------- setup ---------- */
async function loadBasemapFiles() {
  try {
    const [meta, water, lines, labels] = await Promise.all(['meta.json', 'water.geojson', 'lines.geojson', 'labels.json'].map((f) => getJson(BASE_DIR + f)));
    bm = { meta, water, lines, labels };
  } catch (err) { bm = {}; notice('Basemap files missing: run scripts/build_basemap.py (' + err.message + ')'); }
}
function setBasemap(key) {
  if (!BASEMAPS[key]) key = 'dark'; state.base = key;
  if (terrainLayer) { if (key === 'terrain') { if (!map.hasLayer(terrainLayer)) terrainLayer.addTo(map); } else map.removeLayer(terrainLayer); }
  const dk = key === 'dark'; document.body.classList.toggle('dark-map', dk);
  el('map').style.background = key === 'terrain' ? '#d9dfd0' : dk ? LAND_DARK : LAND_PLAIN; el('basemap').value = key;
  if (waterGeo) waterGeo.setStyle({ fillColor: dk ? WATER_DARK : WATER_FILL });
  for (const t of lineSets) t.l.setStyle(t.casing ? { opacity: dk ? 0 : 0.55 } : Object.assign({ dashArray: null }, dk ? LINE_DARK[t.kind] : LINE_STYLE[t.kind]));
  if (cwaCasing) styleCwa();
  writeHash();
}
function drawBaseLabels() {
  if (!baseLabelLayer || !bm.labels) return; baseLabelLayer.clearLayers(); const z = map.getZoom();
  for (const l of bm.labels) if (z >= l.minzoom)
    baseLabelLayer.addLayer(L.marker([l.lat, l.lon], { pane: 'baseLabels', interactive: false, keyboard: false, icon: L.divIcon({ className: '', iconSize: [0, 0], html: `<div class="bm-label bm-${l.kind}">${esc(l.name)}</div>` }) }));
}
function initMap() {
  const m = bm.meta, opts = { zoomControl: false, zoomSnap: 0.25, worldCopyJump: false, attributionControl: false, minZoom: m ? m.minzoom : 5, maxZoom: m ? m.maxzoom + 1 : 10 };
  if (m) { opts.maxBounds = L.latLngBounds(m.bounds); opts.maxBoundsViscosity = 1.0; }
  map = L.map('map', opts).setView([44.6, -71.3], 7);
  if (window.ResizeObserver) new ResizeObserver(() => map.invalidateSize()).observe(el('map'));      // docks and the log change the map window's size L.control.zoom({ position: 'topright' }).addTo(map);
  if (m) { const fit = () => map.setMinZoom(Math.max(m.minzoom, map.getBoundsZoom(opts.maxBounds, true) - 0.25)); fit(); map.on('resize', fit); }      // never zoom out past the edge of the basemap  // draw order, bottom to top: terrain tiles (tile pane, 200) < water 250 < snowfall-error overlay 300 < borders and coasts 390 < CWA outlines 400 < place names 440 < office labels 600
  for (const [n, z] of [['water', 250], ['data', 300], ['lines', 390], ['baseLabels', 440]]) { const p = map.createPane(n); p.style.zIndex = z; p.style.pointerEvents = 'none'; }
  L.control.attribution({ prefix: false }).addTo(map).addAttribution('Data: NWS NDFD (NCEI), NOHRSC &middot; Terrain: <a href="https://github.com/tilezen/joerd/blob/master/docs/attribution.md" target="_blank" rel="noopener">AWS Terrain Tiles</a> &middot; Boundaries: Natural Earth &middot; CWA: NWS');
  if (m) {
    terrainLayer = L.tileLayer(BASE_DIR + m.tiles, { minNativeZoom: m.minzoom, maxNativeZoom: m.maxzoom, minZoom: m.minzoom, maxZoom: m.maxzoom + 1, bounds: m.bounds, tileSize: 256, zIndex: 1, keepBuffer: 4, errorTileUrl: 'data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==' });
    waterGeo = L.geoJSON(bm.water, { pane: 'water', renderer: L.svg({ pane: 'water' }), interactive: false, style: { stroke: false, fillColor: WATER_FILL, fillOpacity: 1 } }).addTo(map);
    const lineLayer = (kind, extra, casing) => { const l = L.geoJSON(bm.lines, { pane: 'lines', renderer: L.svg({ pane: 'lines' }), interactive: false, filter: (f) => f.properties.kind === kind, style: Object.assign({ lineCap: 'round', lineJoin: 'round' }, LINE_STYLE[kind], extra) }).addTo(map); lineSets.push({ kind, l, casing: !!casing }); };
    lineLayer('state', { color: '#fff', weight: 3, opacity: 0.55, dashArray: null }, true); lineLayer('border', { color: '#fff', weight: 4, opacity: 0.55 }, true);      // pale casings keep the lines readable over the data colours
    lineLayer('coast'); lineLayer('state'); lineLayer('border');
    baseLabelLayer = L.layerGroup().addTo(map); drawBaseLabels(); map.on('zoomend', drawBaseLabels);
  }
  errLayer = new ErrorLayer({ pane: 'data', tileSize: 256, scale: Math.max(2, Math.ceil(window.devicePixelRatio || 1)),   /* cells are only ~4 px wide at zoom 7: paint at 2x so fractional zoom levels do not blur them */ zIndex: 5, opacity: state.opacity, updateWhenIdle: false, keepBuffer: 2 }).addTo(map);
  labelLayer = L.layerGroup().addTo(map);
  legendBox = L.control({ position: 'bottomright' }); legendBox.onAdd = () => L.DomUtil.create('div', 'legend'); legendBox.addTo(map); legendBox = legendBox.getContainer();
  const pc = L.control({ position: 'topleft' }); pc.onAdd = () => L.DomUtil.create('div', 'prodlabel'); pc.addTo(map); prodBox = pc.getContainer();

  // pointer tooltip: follows the cursor over the map
  const tipHtml = (ll) => {
    const s = sample(activeGrids(), ll.lat, ll.lng), pos = `${ll.lat.toFixed(3)}°N ${Math.abs(ll.lng).toFixed(3)}°W`;
    if (!s) return `<div class="tip-none">${activeGrids().length ? 'No data' : 'No verified events yet'}</div><div class="tip-pos">${pos}</div>`;
    const o = state.M.offices[s.off] || {}, head = `<div class="tip-head"><i class="chip" style="background:${esc(o.color || '#888')}"></i><b>${esc(s.off)}</b>${o.name ? ' <span>' + esc(o.name) + '</span>' : ''}</div>`;
    if (s.v === EXCLUDED) return `${head}<div class="tip-none">Excluded: radar beam-blockage artifact</div><div class="tip-pos">${pos}</div>`;
    return `${head}<div class="tip-val"><span class="val" style="${chipStyle(s.v, s.lim)}">${fmt(s.v)} in</span> ${s.event ? 'error in this event' : 'mean error per event'}</div><div class="tip-pos">${pos}</div>`;
  };
  const touch = window.matchMedia('(hover: none)').matches, box = map.getContainer(); let dragging = false, last = null;
  tipBox = L.DomUtil.create('div', 'cursor-tip', box); tipBox.hidden = true;
  const hideTip = () => { tipBox.hidden = true; };
  const showTip = (cp) => {
    last = cp; tipBox.innerHTML = tipHtml(map.containerPointToLatLng(cp)); tipBox.hidden = false;
    const S = map.getSize(), w = tipBox.offsetWidth, h = tipBox.offsetHeight; let x = cp.x + 16, y = cp.y + 18;
    if (x + w > S.x - 6) x = cp.x - w - 12; if (y + h > S.y - 6) y = cp.y - h - 12;
    tipBox.style.transform = `translate(${Math.max(4, Math.round(x))}px, ${Math.max(4, Math.round(y))}px)`;
  };
  if (!touch) {
    box.addEventListener('mousemove', (e) => { if (dragging) return; if (e.target.closest('.leaflet-control, .leaflet-popup')) { last = null; return hideTip(); } showTip(map.mouseEventToContainerPoint(e)); });
    box.addEventListener('mouseleave', () => { last = null; hideTip(); });
    map.on('dragstart', () => { dragging = true; hideTip(); }); map.on('dragend', () => { dragging = false; });
    map.on('zoomstart', hideTip); map.on('moveend', () => { if (last && !dragging) showTip(last); });         // after a zoom or fly-to the cell under a still pointer has changed
    el('offices').addEventListener('change', () => { if (last) showTip(last); });  // (a ticked office changes what is under a still pointer)
  } else map.on('click', (e) => L.popup().setLatLng(e.latlng).setContent(tipHtml(e.latlng)).openOn(map));     // touch screens: tap to read a cell
}
async function initCwa() {
  try {
    cwaData = await getJson(url.cwa());
    cwaCasing = L.geoJSON(cwaData, { interactive: false, style: { color: '#fff', weight: 5, fill: false } }).addTo(map);
    cwaGeo = L.geoJSON(cwaData, { style: { weight: 2.4, fill: true, fillOpacity: 0.001 }, onEachFeature: (f, l) => l.on('click', () => select(f.properties.cwa, false)) }).addTo(map);
    cwaGeo.bringToFront(); styleCwa(); drawLabels();
  } catch (err) { notice('Could not load CWA outlines: ' + err.message); }
}

async function main() {
  const inline = el('manifest'); try { state.M = inline ? JSON.parse(inline.textContent) : await getJson(url.manifest()); } catch (err) { notice('No data found. Run pipeline/update_season.py first.'); return; }
  const h = readHash(), seasons = Object.keys(state.M.seasons).sort().reverse();
  if (isFinite(h.op)) state.opacity = Math.min(1, Math.max(0, h.op));
  el('season').innerHTML = seasons.map((s) => `<option value="${esc(s)}">${esc(state.M.seasons[s].label)}</option>`).join('');
  el('basemap').innerHTML = Object.entries(BASEMAPS).map(([k, b]) => `<option value="${k}">${esc(b.name)}</option>`).join('');
  el('opacity').value = state.opacity; el('opacityOut').textContent = Math.round(state.opacity * 100) + '%';
  await loadBasemapFiles();
  initMap(); window.NWS.map = map; window.NWS.state = state;      // exposed for embedding and testing
  setBasemap(h.base || 'dark');
  const season = seasons.includes(h.season) ? h.season : (state.M.default_season || seasons[0]); el('season').value = season;
  if (h.office) state.sel = h.office.toUpperCase();
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') deselect(); });
  el('season').addEventListener('change', (e) => loadSeason(e.target.value));
  el('basemap').addEventListener('change', (e) => setBasemap(e.target.value));
  el('opacity').addEventListener('input', (e) => { state.opacity = parseFloat(e.target.value); errLayer.setOpacity(state.opacity); el('opacityOut').textContent = Math.round(state.opacity * 100) + '%'; writeHash(); });
  el('offices').addEventListener('change', (e) => { const off = e.target.dataset && e.target.dataset.vis; if (!off) return; state.grids.find((g) => g.off === off).visible = e.target.checked; refresh(); });
  el('offices').addEventListener('click', (e) => { if (e.target.closest('input')) return; const tr = e.target.closest('tr.row'); if (tr) { select(tr.dataset.off, true); toMap(); } });
  el('offices').addEventListener('keydown', (e) => { if (e.key === 'Enter' && e.target.matches('tr.row')) { select(e.target.dataset.off, true); toMap(); } });
  const toMap = () => { const r = el('map').getBoundingClientRect(); if (r.top < 0 || r.bottom > innerHeight + 4) el('map').scrollIntoView({ behavior: 'smooth', block: 'start' }); };
  const pickEvent = (tr) => { if (!tr || tr.classList.contains('pend')) return; const { off, id } = tr.dataset; if (state.ev && state.ev.off === off && state.ev.id === id) clearEvent(); else { showEvent(off, id); toMap(); } };
  el('events').addEventListener('click', (e) => pickEvent(e.target.closest('tr.ev')));
  el('log').addEventListener('click', (e) => pickEvent(e.target.closest('tr.lg.click')));
  el('log').addEventListener('keydown', (e) => { if (e.key === 'Enter' && e.target.matches('tr.lg.click')) pickEvent(e.target); });
  el('events').addEventListener('keydown', (e) => { if (e.key === 'Enter' && e.target.matches('tr.ev')) pickEvent(e.target); });
  await Promise.all([loadSeason(season), initCwa()]);
  map.invalidateSize();
  const ev = h.event && h.event.match(/^([A-Za-z]+)-(\d+)$/);
  if (ev && eventOf(ev[1].toUpperCase(), ev[2])) await showEvent(ev[1].toUpperCase(), ev[2]);
  else if (state.sel && offEntry(state.sel)) select(state.sel, true);
  else if (state.offs.length) map.fitBounds([[Math.min(...state.offs.map((g) => g.north - g.ny * g.dy)), Math.min(...state.offs.map((g) => g.west))],
                                           [Math.max(...state.offs.map((g) => g.north)), Math.max(...state.offs.map((g) => g.west + g.nx * g.dx))]], { padding: [10, 10] });
  drawLabels(); styleCwa();
}
main();
})();
