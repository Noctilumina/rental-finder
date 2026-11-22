/* Rental finder front end. Talks to the local server (server.py). */
'use strict';

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtEur = n => n == null ? '—' : '€' + Math.round(n).toLocaleString('nl-NL');
const hh = h => String(h % 24).padStart(2, '0') + ':00';
const PARK_ANY = 30;

// ---------- state (persisted per browser) ----------
const DEFAULTS = {
  walk: 15, shop: 15, park: PARK_ANY, train: 30, direct: true, dest: 'lg', lastMode: 'bike', day: 'wk', hour: 14,
  lyrAreas: true, lyrHeat: false, lyrStations: true, lyrShops: false, lyrParks: false, lyrListings: true, other: true,
  sort: 'score', listW: 340, sel: null, leftOpen: true, rightOpen: true, dups: false, legend: false,
  f: { rentMin: '', rentMax: '', rentIncl: 'any', m2: '', rooms: '', beds: '', label: 'any', interior: 'any',
       avail: '', listed: 'any', pets: false, gm: [], newOnly: false, minScore: '', ppm2Max: '', list: 'active', state: 'current' },
  tableSort: { key: 'score', asc: false },
};
let S;
try {
  S = { ...DEFAULTS, ...JSON.parse(localStorage.getItem('rf-state') || '{}') };
  S.f = { ...DEFAULTS.f, ...S.f };
  if (S.walk > 60) S.walk = 60;
  S.sel = null;  // a selection doesn't survive a reload
} catch { S = structuredClone(DEFAULTS); }
const save = () => { try { localStorage.setItem('rf-state', JSON.stringify(S)); } catch { /* private mode */ } };

let STATIONS = [], HOURS = [], META = {}, BYCODE = {}, QUAL = new Set(), LISTINGS = [], SHOPS = [], SHOP_COUNTS = {};
let PARKS = null, LAST_REFRESH = null, SCORE_INFO = {}, BANDS = [];
let HOVER = null; // {kind, id} currently previewed on the map
let GROUP = null;  // ids of a clicked group of pins: the list shows only these until you click elsewhere

// ---------- travel-time model ----------
const ARR = () => META.destination?.arrivals || {};
const HUB_NAMES = () => STATIONS_DATA?.hubNames || {};
let STATIONS_DATA = null;
const mode = () => S.direct ? 'direct' : 'any';
const hourIdx = h => HOURS.indexOf(h);
const destName = () => S.dest === 'lg' ? 'Lucas Gasselstraat 2' : 'Den Bosch CS';
const dayName = () => ({ wk: 'weekday', sa: 'Saturday', su: 'Sunday' }[S.day]);
function trainTo(st, hub, h, day) {
  const v = st?.hubs?.[hub]?.hourly?.[day]?.[mode()]?.to?.[hourIdx(h)];
  return v == null ? null : v;
}
/** Quickest trip from a station's platform to the destination at hour h: {min, hub, train, last}. */
function fromStation(st, h = S.hour, day = S.day) {
  if (!st) return null;
  if (S.dest === 'cs') { const v = trainTo(st, 'IFF:ht', h, day); return v == null ? null : { min: v, hub: 'IFF:ht', train: v, last: 0 }; }
  let best = null;
  for (const [hub, leg] of Object.entries(ARR())) {
    const v = trainTo(st, hub, h, day);
    if (v == null || !leg) continue;
    const t = v + leg[S.lastMode];
    if (!best || t < best.min) best = { min: t, hub, train: v, last: leg[S.lastMode] };
  }
  return best;
}
const directMin = destM => {
  if (destM == null) return null;
  const sp = META.destination?.speeds || { walk: 5, bike: 15, bikePark: 3 };
  return Math.round(S.lastMode === 'walk' ? destM / (sp.walk * 1000 / 60) : destM / (sp.bike * 1000 / 60) + sp.bikePark);
};
/** Quickest route for a listing at hour h: via one of its stations, or straight to the destination. */
function route(l, h = S.hour, day = S.day) {
  let best = null;
  for (const [code, w] of (l.options || [[l.station, l.walk]])) {
    const r = fromStation(BYCODE[code], h, day);
    if (!r) continue;
    const t = Math.round(w + r.min);
    if (!best || t < best.min) best = { ...r, min: t, station: code, walk: Math.round(w) };
  }
  if (S.dest === 'lg' && l.destM != null) {
    const d = directMin(l.destM);
    if (!best || d <= best.min) best = { min: d, direct: true };
  }
  return best;
}
const listingTravel = (l, h, day) => route(l, h, day)?.min ?? null;
function routeText(r) {
  if (!r) return 'no train at this hour';
  if (r.direct) return `${S.lastMode === 'walk' ? 'walk' : 'bike'} straight there`;
  const st = BYCODE[r.station];
  const hub = HUB_NAMES()[r.hub] || '';
  return `${r.walk} min walk to ${st?.name || ''}${st?.code === r.hub ? '' : ', train to ' + hub}${S.dest === 'lg' ? `, ${S.lastMode} ${r.last} min` : ''}`;
}
function isOffpeak(h, day) {
  if (day !== 'wk') return true;
  const m = (h % 24) * 60 + 30;
  return m < 390 || (m >= 540 && m < 960) || m >= 1110;
}
function barsHtml(fn, compact = false) {
  const vals = HOURS.map(h => fn(h));
  const max = Math.max(1, ...vals.filter(v => v != null));
  const H = compact ? 48 : 76;
  const bars = HOURS.map((h, i) => {
    const v = vals[i];
    const c = h === S.hour ? 'var(--accent)' : v == null ? 'var(--line)' : isOffpeak(h, S.day) ? 'var(--bar-off)' : 'var(--bar-peak)';
    const lab = v == null ? '' : (i % 3 === 0 || h === S.hour ? v : '');
    const ht = v == null ? 3 : Math.max(4, Math.round(v / max * H));
    return `<button class="b" data-hour="${h}" aria-label="${hh(h)}: ${v == null ? 'no train' : v + ' minutes'}"><span>${compact ? '' : lab}</span><i style="height:${ht}px;background:${c}"></i></button>`;
  }).join('');
  const off = vals.filter((v, i) => v != null && isOffpeak(HOURS[i], S.day));
  const all = vals.filter(v => v != null);
  let summary = 'No trains on this day';
  if (all.length) {
    const worst = Math.max(...all), wi = vals.indexOf(worst);
    summary = (off.length ? `Off-peak ${Math.min(...off)}–${Math.max(...off)} min · ` : '') + `worst ${worst} min at ${hh(HOURS[wi])}`;
  }
  return `<div class="chart"><span class="small muted">Door to door to ${destName()} by departure hour (${dayName()})</span>
    <div class="bars" style="height:${H + 20}px">${bars}</div>
    <div class="axis"><span>06</span><span>09</span><span>12</span><span>15</span><span>18</span><span>21</span><span>00</span></div>
    ${compact ? '' : `<div class="keys"><span><i style="background:var(--bar-off)"></i>Off-peak (free)</span><span><i style="background:var(--bar-peak)"></i>Peak</span><span><i style="background:var(--accent)"></i>${hh(S.hour)}</span></div>`}
    <div class="small">${summary}</div></div>`;
}

// ---------- listing helpers ----------
const now = Date.now();
function ageDays(l) { const t = Date.parse(l.listedAt || l.firstSeen || ''); return isNaN(t) ? null : Math.max(0, Math.floor((now - t) / 864e5)); }
function ageTxt(l) { const d = ageDays(l); return d == null ? '—' : d === 0 ? 'today' : d === 1 ? '1 day ago' : d + ' days ago'; }
const INTERIOR = { furnished: 'Furnished', upholstered: 'Gestoffeerd', bare: 'Kaal', unfurnished: 'Unfurnished' };
const NOT_FURNISHED = new Set(['upholstered', 'bare', 'unfurnished']);
const LABEL_RANK = l => { if (!l) return 99; const m = /^A(\+*)/.exec(l); return m ? -m[1].length : 'ABCDEFG'.indexOf(l[0]); };
const ppm2 = l => l.rent && l.m2 ? l.rent / l.m2 : null;
const ppr = l => l.rent && l.rooms ? l.rent / l.rooms : null;
const isNew = l => LAST_REFRESH && l.firstSeen && l.firstSeen >= LAST_REFRESH;
const user = l => l.user || {};
function rentIncl(l) { return l.rentIncl === false && l.serviceCosts ? l.rent + l.serviceCosts : l.rent; }
const STATUS = [['', 'No status'], ['contacted', 'Contacted'], ['viewing', 'Viewing planned'], ['applied', 'Applied'], ['rejected', 'Rejected'], ['notforme', 'Not for me']];
const STATUS_NAME = Object.fromEntries(STATUS);
const LISTS = [['active', 'All (not hidden)'], ['star', '★ Shortlist'], ['status', 'With a status'], ['contacted', 'Contacted'], ['viewing', 'Viewing planned'], ['applied', 'Applied'], ['rejected', 'Rejected'], ['notforme', 'Not for me'], ['hidden', 'Hidden'], ['any', 'Everything']];
const STATES = [['current', 'Available (incl. not found in last scan)'], ['available', 'Available, seen in last scan'], ['offer', 'Also under offer'], ['removed', 'Removed only'], ['all', 'All, incl. removed']];

function passesFilters(l, f = S.f, skip = null) {
  const r = f.rentIncl === 'incl' ? rentIncl(l) : l.rent;
  if (f.rentMin && r != null && r < +f.rentMin) return false;
  if (f.rentMax && (r == null || r > +f.rentMax)) return false;
  if (f.m2 && (l.m2 == null || l.m2 < +f.m2)) return false;
  if (f.rooms && (l.rooms == null || l.rooms < +f.rooms)) return false;
  if (f.beds && (l.bedrooms == null || l.bedrooms < +f.beds)) return false;
  if (f.label !== 'any') { const lim = { A: 0, B: 1, C: 2, D: 3 }[f.label]; if (LABEL_RANK(l.energyLabel) > lim) return false; }
  if (f.interior === 'notfurnished') { if (!NOT_FURNISHED.has(l.interior)) return false; }
  else if (f.interior !== 'any' && l.interior !== f.interior) return false;
  if (f.avail) { const a = Date.parse(l.availableFrom || ''); if (!isNaN(a) && a > Date.parse(f.avail)) return false; }
  if (f.listed !== 'any') { const d = ageDays(l); if (d == null || d > +f.listed) return false; }
  if (f.pets && l.pets === false) return false;
  if (skip !== 'gm' && f.gm?.length && !f.gm.includes(l.gemeente)) return false;
  if (f.newOnly && !isNew(l)) return false;
  if (f.minScore && (l.score?.total == null || l.score.total < +f.minScore)) return false;
  if (f.ppm2Max && (ppm2(l) == null || ppm2(l) > +f.ppm2Max)) return false;
  const u = user(l);
  if (f.list === 'active' && u.hidden) return false;
  if (f.list === 'star' && !u.star) return false;
  if (f.list === 'hidden' && !u.hidden) return false;
  if (f.list === 'status' && !u.status) return false;
  if (STATUS_NAME[f.list] && f.list && u.status !== f.list) return false;
  const st = l.status || 'available';
  if (f.state === 'current' && st !== 'available') return false;
  if (f.state === 'available' && (st !== 'available' || l.missing)) return false;
  if (f.state === 'offer' && st === 'removed') return false;
  if (f.state === 'removed' && st !== 'removed') return false;
  return true;
}
const num = (v, d) => v == null || isNaN(v) ? d : v;
const SORTS = [
  ['score', 'Score: best first', (a, b) => num(b.score?.total, -1) - num(a.score?.total, -1)],
  ['found', 'Found in latest refresh first', (a, b) => (b.firstSeen || '').localeCompare(a.firstSeen || '')],
  ['newest', 'Newest listed first', (a, b) => num(ageDays(a), 1e9) - num(ageDays(b), 1e9)],
  ['oldest', 'Oldest first', (a, b) => num(ageDays(b), -1) - num(ageDays(a), -1)],
  ['rentAsc', 'Rent: low to high', (a, b) => num(a.rent, 1e9) - num(b.rent, 1e9)],
  ['rentDesc', 'Rent: high to low', (a, b) => num(b.rent, 0) - num(a.rent, 0)],
  ['ppm2', 'Rent per m²: low to high', (a, b) => num(ppm2(a), 1e9) - num(ppm2(b), 1e9)],
  ['ppr', 'Rent per room: low to high', (a, b) => num(ppr(a), 1e9) - num(ppr(b), 1e9)],
  ['drop', 'Biggest price drop first', (a, b) => num(a.priceChange, 0) - num(b.priceChange, 0)],
  ['m2', 'Floor area: large to small', (a, b) => num(b.m2, 0) - num(a.m2, 0)],
  ['rooms', 'Rooms: most first', (a, b) => num(b.rooms, 0) - num(a.rooms, 0)],
  ['label', 'Energy label: best first', (a, b) => LABEL_RANK(a.energyLabel) - LABEL_RANK(b.energyLabel)],
  ['travel', 'Travel time: shortest first', (a, b) => num(listingTravel(a), 1e9) - num(listingTravel(b), 1e9)],
  ['walk', 'Walk to station: shortest first', (a, b) => a.walk - b.walk],
];
let _visible = null;
function visibleListings() {
  if (_visible) return _visible;
  const sort = SORTS.find(s => s[0] === S.sort) || SORTS[0];
  _visible = LISTINGS.filter(l => passesFilters(l)).sort(sort[2]);
  return _visible;
}
const invalidate = () => { _visible = null; };
const byId = id => LISTINGS.find(x => x.id === id);

// small pieces of listing markup
function chgHtml(l) {
  if (!l.priceChange) return '';
  const down = l.priceChange < 0;
  return `<span class="chg ${down ? 'down' : 'up'}" title="Rent ${down ? 'lowered' : 'raised'} on ${esc(l.priceChangeAt || '')}">${down ? '▼' : '▲'} ${fmtEur(Math.abs(l.priceChange))}</span>`;
}
function scoreHtml(l, big = false) {
  const t = l.score?.total;
  if (t == null) return '';
  return `<span class="score${big ? ' big' : ''}" title="Score ${t} of 10 · better than ${l.score.totalPct ?? '?'}% of homes">${t.toFixed(1)}</span>`;
}
function warnHtml(l) {
  const s = l.score?.suspicious || [];
  return s.length ? `<span class="warnicon" title="Check carefully: ${esc(s.join('; '))}" aria-label="Possibly suspicious listing">⚠</span>` : '';
}
function stateTags(l) {
  const out = [];
  if (l.status === 'removed') out.push('<span class="tag removed">Removed</span>');
  else if (l.status === 'under_offer') out.push('<span class="tag">Under offer</span>');
  if (l.missing) out.push('<span class="tag missing" title="Not in the last scan\'s search results, but the site hasn\'t marked it removed">Not found in last scan</span>');
  const u = user(l);
  if (u.status) out.push(`<span class="tag st">${esc(STATUS_NAME[u.status])}</span>`);
  if (u.hidden) out.push('<span class="tag">Hidden</span>');
  return out.join('');
}
const metaLine = l => [l.type, l.m2 && l.m2 + ' m²', l.rooms ? l.rooms + (l.rooms === 1 ? ' room' : ' rooms') : l.bedrooms ? l.bedrooms + ' bedroom' + (l.bedrooms > 1 ? 's' : '') : '',
  ppm2(l) && '€' + ppm2(l).toFixed(1) + '/m²', ppr(l) && fmtEur(ppr(l)) + '/room', l.energyLabel && 'label ' + l.energyLabel, INTERIOR[l.interior]].filter(Boolean).join(' · ');
const starBtn = l => `<button class="starbtn${user(l).star ? ' on' : ''}" data-star="${esc(l.homeId || l.id)}" aria-pressed="${!!user(l).star}" title="${user(l).star ? 'Remove from shortlist' : 'Add to shortlist'}">${user(l).star ? '★' : '☆'}</button>`;

async function setUser(hid, patch) {
  const r = await fetch('/api/user/' + encodeURIComponent(hid), { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch) });
  const u = await r.json();
  for (const l of LISTINGS) if ((l.homeId || l.id) === hid) l.user = u;
  invalidate();
  renderAll();
}

// ---------- map ----------
const map = L.map('map', { zoomControl: true, preferCanvas: true }).setView([51.69, 5.3], 10);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(map);
map.createPane('areas'); map.getPane('areas').style.zIndex = 350;
map.createPane('parks'); map.getPane('parks').style.zIndex = 340;
// colour-blind-safe sequential scale (viridis): short trips light, long trips dark
const BAND_COLORS = ['#FDE725', '#C2DF23', '#86D549', '#52C569', '#2AB07F', '#1E9B8A', '#25858E', '#2D708E', '#38588C', '#433E85', '#482173'];
const areaLayer = L.geoJSON(null, {
  pane: 'areas',
  style: f => f.properties.kind === 'band'
    ? { stroke: false, fillColor: BAND_COLORS[f.properties.band] || '#482173', fillOpacity: 0.5 }
    : { color: '#0F766E', weight: 2, fillColor: '#0F766E', fillOpacity: S.lyrHeat ? 0 : 0.28 },
}).addTo(map);
const stationLayer = L.layerGroup().addTo(map);
const shopLayer = L.layerGroup();
const parkLayer = L.geoJSON(null, { pane: 'parks', style: { color: '#3F7D20', weight: 1, fillColor: '#5FA83A', fillOpacity: 0.35 },
  onEachFeature: (f, layer) => layer.bindTooltip(`${esc(f.properties.name || 'Park')} · ${f.properties.ha} ha`) });
const pinLayer = (L.markerClusterGroup ? L.markerClusterGroup({ maxClusterRadius: 40, disableClusteringAtZoom: 15, showCoverageOnHover: false, zoomToBoundsOnClick: false }) : L.layerGroup()).addTo(map);
let fitted = false;
const groupIds = e => e.layer.getAllChildMarkers().map(m => m.options.lid);
// With a side panel open the map is small: stations and groups open on click (single homes still on hover).
// With both panels hidden, everything previews on hover.
const fullMap = () => !S.leftOpen && !S.rightOpen;
pinLayer.on('clustermouseover', e => { if (fullMap()) showHover({ kind: 'group', ids: groupIds(e), x: map.mouseEventToContainerPoint(e.originalEvent).x }); });
pinLayer.on('clustermouseout', () => { if (fullMap()) hideHoverSoon(); });
pinLayer.on('clusterclick', e => {
  const ids = groupIds(e);
  HOVER = null;
  if (S.rightOpen) { GROUP = new Set(ids); S.sel = null; renderCards(); }  // the list shows just this group
  else S.sel = { kind: 'group', ids, x: map.mouseEventToContainerPoint(e.originalEvent).x };
  renderDetailCard();
});
map.on('click', () => {  // click anywhere else: back to the full list, close the card
  if (GROUP) { GROUP = null; renderCards(); }
  if (S.sel) { select(null); drawStations(); }
});

function drawStations() {
  stationLayer.clearLayers();
  if (!S.lyrStations) return;
  for (const st of STATIONS) {
    const ok = QUAL.has(st.code);
    const m = L.circleMarker([st.lat, st.lon], { radius: ok ? 7 : 5, color: '#fff', weight: 2, fillColor: ok ? '#0F766E' : '#A3A39B', fillOpacity: 1, bubblingMouseEvents: false });
    const t = fromStation(st)?.min;
    // no permanent labels (too busy): the name shows on hover, and stays for the selected station
    const selected = S.sel?.kind === 'station' && S.sel.id === st.code;
    m.bindTooltip(`${esc(st.name)} · ${t == null ? 'no train this hour' : t + '′'}`,
      { permanent: selected, direction: 'right', offset: [8, 0], className: 'st-label' + (ok ? '' : ' off') });
    m.on('mouseover', e => { if (fullMap()) showHover({ kind: 'station', id: st.code, x: e.containerPoint?.x }); });
    m.on('mouseout', () => { if (fullMap()) hideHoverSoon(); });
    m.on('click', e => { HOVER = null; select(S.sel?.id === st.code ? null : { kind: 'station', id: st.code, x: e.containerPoint?.x }); drawStations(); });
    stationLayer.addLayer(m);
  }
}
function drawShops() {
  shopLayer.clearLayers();
  if (!S.lyrShops) { map.removeLayer(shopLayer); return; }
  for (const s of SHOPS) {
    L.circleMarker([s.lat, s.lon], { radius: 3.5, color: '#6D28D9', weight: 1, fillColor: '#6D28D9', fillOpacity: 0.85 })
      .bindTooltip(esc(s.name || s.brand || 'Supermarket')).addTo(shopLayer);
  }
  shopLayer.addTo(map);
}
async function drawParks() {
  if (!S.lyrParks) { map.removeLayer(parkLayer); return; }
  if (!PARKS) PARKS = await fetch('/api/parks').then(r => r.json());
  if (!parkLayer.getLayers().length) parkLayer.addData({ type: 'FeatureCollection', features: PARKS.map(p => ({ type: 'Feature', properties: { name: p.name, ha: p.ha }, geometry: p.geometry })) });
  parkLayer.addTo(map);
}
function drawPins(list) {
  pinLayer.clearLayers();
  if (!S.lyrListings) return;
  list.forEach((l, i) => {
    const hov = HOVER?.kind === 'listing' && HOVER.id === l.id;
    const cls = ['pin-icon', hov && 'sel', user(l).star && 'star', l.status === 'removed' && 'gone', l.missing && 'missing'].filter(Boolean).join(' ');
    const icon = L.divIcon({ className: '', html: `<div class="${cls}" data-pin="${esc(l.id)}">${i + 1}</div>`, iconSize: [22, 22], iconAnchor: [11, 11] });
    L.marker([l.lat, l.lon], { icon, zIndexOffset: hov ? 1000 : 0, keyboard: false, lid: l.id })
      .on('mouseover', e => showHover({ kind: 'listing', id: l.id, x: e.containerPoint?.x }))
      .on('mouseout', hideHoverSoon)
      .on('click', () => { location.hash = '#listing/' + encodeURIComponent(l.id); })
      .addTo(pinLayer);
  });
}
function highlightPin(id, on) {
  $$('.pin-icon.sel').forEach(p => p.classList.remove('sel'));
  if (on && id) $$(`.pin-icon[data-pin="${CSS.escape(id)}"]`).forEach(p => p.classList.add('sel'));
}
function renderLegend() {
  const btn = `<button class="legbtn" id="legendBtn" aria-expanded="${S.legend}">${S.legend ? 'Hide legend' : 'Legend'}</button>`;
  if (!S.legend) { $('#legend').innerHTML = btn; return; }
  const parts = [];
  if (S.lyrAreas && S.lyrHeat) {
    const labels = BANDS.length ? [`≤${BANDS[0]}`, ...BANDS.slice(1).map(b => `${b}`), `>${BANDS[BANDS.length - 1]}`] : [];
    parts.push(`<span class="scale" title="Typical off-peak door to door (min)">${labels.map((t, i) => `<span><i style="background:${BAND_COLORS[i]}"></i>${t}</span>`).join('')} min</span>`);
  } else if (S.lyrAreas) parts.push('<span><i class="sw area"></i>Area</span>');
  parts.push('<span><i class="sw dot on"></i>Station</span>');
  if (S.lyrParks) parts.push('<span><i class="sw park"></i>Park</span>');
  parts.push('<span><i class="sw pin star">1</i>Shortlisted</span>');
  $('#legend').innerHTML = parts.join('') + btn;
}

// ---------- "what's here" while hovering the map ----------
let hereTimer, hereSeq = 0;
map.on('mousemove', e => {
  clearTimeout(hereTimer);
  $('#hereTip').hidden = true;
  if (e.originalEvent.target.closest?.('.leaflet-marker-icon, .marker-cluster, .detailcard')) return;
  if (S.lyrStations && STATIONS.some(st => map.latLngToContainerPoint([st.lat, st.lon]).distanceTo(e.containerPoint) < 14)) return;
  hereTimer = setTimeout(() => showHere(e.latlng, e.containerPoint), 450);
});
map.on('mouseout movestart zoomstart', () => { clearTimeout(hereTimer); $('#hereTip').hidden = true; });
async function showHere(ll, pt) {
  const seq = ++hereSeq;
  const r = await fetch(`/api/point?lat=${ll.lat.toFixed(5)}&lon=${ll.lng.toFixed(5)}&${q()}&dest=${S.dest}&last=${S.lastMode}`).then(r => r.json()).catch(() => null);
  if (!r || seq !== hereSeq) return;
  const p = r.place || {};
  const town = p.woonplaatsnaam || '';
  const title = [p.straatnaam, p.buurtnaam].filter(Boolean).join(' · ') || town || 'Somewhere';
  const sub = [town, p.gemeentenaam && p.gemeentenaam !== town ? 'municipality ' + p.gemeentenaam : ''].filter(Boolean).join(', ');
  const st = (r.stations || [])[0];
  const lines = [];
  if (r.inside) lines.push('<span class="badge">Inside the areas</span>');
  if (st) lines.push(`${st.walk} min walk to ${esc(st.name)}`);
  else if (r.stations) lines.push('No qualifying station within an hour\'s walk');
  if (r.door != null) lines.push(`${r.door} min door to door to ${destName()} <span class="muted">(typical off-peak${r.direct != null && r.door === r.direct ? ', ' + S.lastMode + ' straight there' : ''})</span>`);
  lines.push(`Supermarket ${r.shop ?? '30+'} min · park ${r.park ?? '30+'} min`);
  const tip = $('#hereTip');
  tip.innerHTML = `<b>${esc(title)}</b>${sub ? `<span class="muted">${esc(sub)}</span>` : ''}${lines.map(x => `<span>${x}</span>`).join('')}`;
  const box = $('.mapwrap').getBoundingClientRect();
  tip.style.left = Math.min(pt.x + 14, box.width - 290) + 'px';
  tip.style.top = Math.min(pt.y + 14, box.height - 140) + 'px';
  tip.hidden = false;
}

// ---------- data loading ----------
let areaTimer, areaSeq = 0;
function q() { return `walk=${S.walk}&shop=${S.shop}&park=${S.park}&train=${S.train}&direct=${S.direct}`; }
async function loadAreas(listings = true) {
  const seq = ++areaSeq;
  const [a, l] = await Promise.all([
    fetch(`/api/areas?${q()}&dest=${S.dest}&last=${S.lastMode}&heat=${S.lyrAreas && S.lyrHeat}`).then(r => r.json()),
    listings ? fetch(`/api/listings?${q()}&dest=${S.dest}&last=${S.lastMode}&dups=${S.dups}`).then(r => r.json()) : null,
  ]);
  if (seq !== areaSeq) return;
  QUAL = new Set(a.stations);
  SHOP_COUNTS = a.shopBrands || {};
  BANDS = a.bands || [];
  areaLayer.clearLayers();
  if (S.lyrAreas) areaLayer.addData(a.areas);
  const outline = a.areas.features.find(f => f.properties.kind === 'outline');
  $('#areaStats').textContent = `${QUAL.size} stations qualify · ${outline?.properties?.km2 ?? 0} km² of area`;
  if (!fitted && outline) { fitted = true; map.fitBounds(L.geoJSON(outline).getBounds(), { padding: [20, 20] }); }
  if (l) {
    LISTINGS = l.items;
    LAST_REFRESH = l.lastRefresh?.at || l.updated;
    SCORE_INFO = l.scoreInfo || {};
    $('#updated').textContent = l.updated ? `Listings updated ${relTime(l.updated)}` : 'No listings fetched yet';
  }
  invalidate();
  renderAll();
}
const scheduleAreas = (listings = true) => { clearTimeout(areaTimer); areaTimer = setTimeout(() => loadAreas(listings), 150); };
function relTime(iso) {
  const m = Math.round((Date.now() - Date.parse(iso)) / 60000);
  return m < 1 ? 'just now' : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} days ago`;
}

// ---------- rendering ----------
function setCollapsed(el, collapsed) {
  if (el.classList.contains('collapsed') === collapsed) return;
  el.classList.toggle('collapsed', collapsed);
  el.inert = collapsed;
  // keep the map's size in step while the panel slides
  const t0 = performance.now();
  const step = () => { map.invalidateSize({ pan: false }); if (performance.now() - t0 < 320) requestAnimationFrame(step); };
  requestAnimationFrame(step);
}
// restart a short fade on an element whose content just changed
function fade(el) { el.classList.remove('fadein'); void el.offsetWidth; el.classList.add('fadein'); }
function renderControls() {
  $('#walk').value = S.walk; $('#walkOut').textContent = S.walk + ' min';
  $('#shop').value = S.shop; $('#shopOut').textContent = S.shop + ' min';
  $('#park').value = S.park; $('#parkOut').textContent = S.park >= PARK_ANY ? 'Any' : S.park + ' min';
  $('#train').value = S.train; $('#trainOut').textContent = S.train + ' min';
  $('#hour').value = S.hour; $('#hourOut').textContent = hh(S.hour);
  const op = isOffpeak(S.hour, S.day);
  $('#periodOut').textContent = op ? 'Off-peak: you travel free' : 'Peak hours';
  $('#periodOut').style.color = op ? 'var(--accent-dark)' : 'var(--warn)';
  $('#direct').checked = S.direct;
  $('#lastMode').value = S.lastMode;
  $('#lastRow').hidden = $('#lastHint').hidden = S.dest !== 'lg';
  const arr = ARR(), names = HUB_NAMES();
  $('#lastHint').textContent = Object.entries(arr).filter(([, v]) => v).map(([c, v]) => `${names[c] || c}: ${v[S.lastMode]} min`).join(' · ');
  $$('[data-dest]').forEach(b => b.classList.toggle('on', b.dataset.dest === S.dest));
  $$('[data-day]').forEach(b => b.classList.toggle('on', b.dataset.day === S.day));
  for (const k of ['lyrAreas', 'lyrHeat', 'lyrStations', 'lyrShops', 'lyrParks', 'lyrListings']) $('#' + k).checked = S[k];
  $('#lyrHeat').disabled = !S.lyrAreas;
  $('#otherBody').hidden = !S.other; $('#otherChevron').textContent = S.other ? '▴' : '▾';
  $('#otherToggle').setAttribute('aria-expanded', S.other);
  const sort = SORTS.find(s => s[0] === S.sort) || SORTS[0];
  $('#sortBtn').textContent = `Sort: ${sort[1]} ▾`;
  $('#sortMenu').innerHTML = SORTS.map(([k, label]) => `<button role="menuitem" data-sort="${k}" class="${k === S.sort ? 'on' : ''}">${label}</button>`).join('');
  setCollapsed($('#criteria'), !S.leftOpen);
  setCollapsed($('#listPanel'), !S.rightOpen);
  $('#toggleLeft').textContent = S.leftOpen ? '‹' : '›';
  $('#toggleLeft').setAttribute('aria-label', S.leftOpen ? 'Hide the criteria panel' : 'Show the criteria panel');
  $('#toggleRight').textContent = S.rightOpen ? '›' : '‹';
  $('#toggleRight').setAttribute('aria-label', S.rightOpen ? 'Hide the listing panel' : 'Show the listing panel');
  $('#listPanel').style.flexBasis = S.listW + 'px';
  $('#quickFilters').hidden = S.listW < 520;
  $('#cards').style.gridTemplateColumns = S.listW >= 620 ? 'repeat(2,minmax(0,1fr))' : 'minmax(0,1fr)';
  renderLegend();
}

// small photo; Rentola's image service can resize, so ask for a small one
function thumbHtml(l, cls) {
  if (!l.photo) return `<span class="${cls} nophoto" aria-hidden="true">⌂</span>`;
  const src = /[?&]width=\d+/.test(l.photo) ? l.photo.replace(/([?&]width=)\d+/, '$1240') : l.photo;
  return `<img class="${cls}" src="${esc(src)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'${cls} nophoto',textContent:'⌂'}))">`;
}
function cardHtml(l, i) {
  const r = route(l);
  const u = user(l);
  return `<div class="lcard${u.hidden ? ' dim' : ''}" data-listing="${esc(l.id)}" tabindex="0" role="link" aria-label="${esc(l.address)}, ${fmtEur(l.rent)}">
    <span class="lhead">${thumbHtml(l, 'lthumb')}<span class="lbody">
    <span class="top1"><span class="row" style="gap:6px"><span class="rank">${i + 1}</span><span class="price">${fmtEur(l.rent)}</span>${chgHtml(l)}${warnHtml(l)}</span>
      <span class="row" style="gap:6px">${scoreHtml(l)}${starBtn(l)}</span></span>
    <span>${esc(l.address)}${l.city ? ', ' + esc(l.city) : ''}</span>
    <span class="small muted">${esc(metaLine(l))}</span></span></span>
    <span class="travel"><span><b>${r?.min ?? '—'} min</b> to ${S.dest === 'lg' ? 'Lucas Gasselstraat' : 'Den Bosch CS'} at ${hh(S.hour)}</span><span class="muted">${esc(r?.direct ? routeText(r) : (BYCODE[r?.station]?.name || '') + (r ? ', ' + r.walk + ' min walk' : ''))}</span></span>
    <span class="tags">${stateTags(l)}${(l.sources || []).slice(0, 3).map(s => `<span class="tag">${esc(s.site)}</span>`).join('')}${(l.sources || []).length > 3 ? `<span class="tag">+${l.sources.length - 3}</span>` : ''}<span class="small muted" style="margin-left:auto">${ageTxt(l)}</span></span>
  </div>`;
}

function activeFilters() {
  const f = S.f, d = DEFAULTS.f;
  return Object.keys(d).filter(k => JSON.stringify(f[k]) !== JSON.stringify(d[k])).length;
}
function renderList() {
  const list = visibleListings();
  const n = activeFilters();
  $('#filterNote').hidden = !n;
  $('#filterNote').innerHTML = n ? `${n} listing filter${n > 1 ? 's' : ''} active (${LISTINGS.length - list.length} hidden) · <a href="#" id="clearFilters">Clear filters</a>` : '';
  const stars = LISTINGS.filter(l => user(l).star).length;
  $('#listTabs').innerHTML = [['active', 'All'], ['star', `★ Shortlist (${stars})`], ['status', 'With status'], ['hidden', 'Hidden']]
    .map(([k, t]) => `<button class="chip${S.f.list === k ? ' on' : ''}" data-list="${k}">${t}</button>`).join('') +
    `<label class="check small" style="margin-left:auto"><input type="checkbox" data-f="newOnly" ${S.f.newOnly ? 'checked' : ''}> New in latest refresh</label>`;
  renderCards();
  drawPins(list);
}
function renderCards() {
  const all = visibleListings();
  const list = GROUP ? all.filter(l => GROUP.has(l.id)) : all;
  $('#listCount').textContent = GROUP ? `${list.length} listing${list.length === 1 ? '' : 's'} in this group` : `${all.length} listing${all.length === 1 ? '' : 's'} in these areas`;
  $('#groupNote').hidden = !GROUP;
  if (renderCards.lastGroup !== GROUP) { renderCards.lastGroup = GROUP; fade($('#cards')); }
  const rank = new Map(all.map((l, i) => [l.id, i]));
  $('#cards').innerHTML = list.length ? list.slice(0, 400).map(l => cardHtml(l, rank.get(l.id))).join('') + (list.length > 400 ? `<div class="empty">Showing the first 400 of ${list.length}. Narrow the filters or open the full table.</div>` : '') :
    `<div class="empty">${LISTINGS.length ? 'No listings match these filters.' : 'No listings inside these areas yet. Press “Refresh listings” to search the rental sites.'}</div>`;
}

// ---------- hover / pinned card on the map ----------
let hideTimer;
function showHover(sel) { clearTimeout(hideTimer); HOVER = sel; renderDetailCard(); if (sel?.kind === 'listing') highlightPin(sel.id, true); }
function hideHoverSoon() { clearTimeout(hideTimer); hideTimer = setTimeout(() => { HOVER = null; highlightPin(null, false); renderDetailCard(); }, 250); }
$('#detailCard').addEventListener('mouseenter', () => clearTimeout(hideTimer));
$('#detailCard').addEventListener('mouseleave', () => { if (HOVER) hideHoverSoon(); });

function renderDetailCard() {
  const box = $('#detailCard');
  const sel = HOVER || S.sel;
  if (!sel) { box.hidden = true; return; }
  const pinned = !HOVER && !!S.sel;
  let html = '';
  if (sel.kind === 'group') {
    const rank = new Map(visibleListings().map((l, i) => [l.id, i]));
    const ls = sel.ids.map(byId).filter(Boolean).sort((a, b) => rank.get(a.id) - rank.get(b.id));
    html = `<div class="cardtitle"><b style="font-size:15px">${ls.length} listings</b></div>
      <div class="glist">${ls.slice(0, 15).map(l => `<a href="#listing/${encodeURIComponent(l.id)}" class="grow">
        <span class="rank">${rank.get(l.id) + 1}</span>${thumbHtml(l, 'gthumb')}<span class="gmain"><b>${fmtEur(l.rent)}</b> ${chgHtml(l)} <span class="muted">${l.m2 ? l.m2 + ' m²' : ''}</span><br><span class="small">${esc(l.address)}</span></span>${scoreHtml(l)}</a>`).join('')}</div>
      ${ls.length > 15 ? `<span class="small muted">+${ls.length - 15} more</span>` : ''}`;
  } else if (sel.kind === 'station') {
    const st = BYCODE[sel.id];
    if (!st) { box.hidden = true; return; }
    const ok = QUAL.has(st.code), d = st.direct, a = st.any;
    const via = HUB_NAMES()[(d || a)?.via] || 'Den Bosch';
    const r = fromStation(st);
    html = `<div class="cardtitle"><b style="font-size:15px">${esc(st.name)}</b><span class="badge ${ok ? '' : 'no'}">${ok ? 'Qualifies' : 'Outside criteria'}</span></div>
      <div class="small muted">${d ? `Direct trains to ${esc(via)}` : a ? `Reachable with ${a.transfers || 1} transfer${(a.transfers || 1) > 1 ? 's' : ''}` : ''}</div>
      <div class="facts">
        <div><div>To ${esc(via)}</div><b>${d ? d.to + ' min direct' : a ? a.to + ' min' : '—'}</b></div>
        <div><div>Back</div><b>${d ? d.from + ' min direct' : a ? a.from + ' min' : '—'}</b></div>
        <div><div>Direct trains</div><b>${d && d.q ? `${st.freq.offpeak}/h, evening ${st.freq.evening}/h` : d ? '—' : 'none'}</b></div>
        <div><div>Last direct home</div><b>${st.lastHome || '—'}</b></div>
      </div>
      <div class="small">At ${hh(S.hour)}: <b>${r?.min ?? '—'} min</b> from the platform to ${destName()}${r && S.dest === 'lg' ? ` via ${esc(HUB_NAMES()[r.hub] || '')}` : ''}</div>
      ${barsHtml(h => fromStation(st, h)?.min ?? null, true)}`;
  } else {
    const l = byId(sel.id);
    if (!l) { box.hidden = true; return; }
    const r = route(l);
    const rank = visibleListings().indexOf(l) + 1;
    html = `${l.photo ? `<div class="cardphoto" style="background-image:url('${esc(l.photo)}')"></div>` : ''}
      <div class="cardtitle"><b style="font-size:15px">${fmtEur(l.rent)} · ${l.m2 ?? '?'} m² ${chgHtml(l)}</b><span class="row" style="gap:6px">${scoreHtml(l)}<span class="badge pinb">${rank ? 'Listing ' + rank : 'Filtered out'}</span></span></div>
      <div class="small muted">${esc(l.address)}${l.city ? ', ' + esc(l.city) : ''}${l.buurt ? ' · ' + esc(l.buurt) : ''}</div>
      <div class="small">${esc(metaLine(l))}</div>
      <div class="facts">
        <div><div>At ${hh(S.hour)}</div><b>${r?.min ?? '—'} min door to door</b></div>
        <div><div>Route</div><b class="small">${esc(routeText(r))}</b></div>
        <div><div>Supermarket / park</div><b>${l.shop} / ${l.park ?? '30+'} min</b></div>
        <div><div>Listed</div><b>${ageTxt(l)}</b></div>
      </div>${barsHtml(h => listingTravel(l, h), true)}`;
  }
  box.innerHTML = (pinned ? `<button class="close" aria-label="Close">×</button>` : '') + html;
  box.classList.toggle('pinned', pinned);
  box.classList.toggle('left', sel.x != null && sel.x > map.getSize().x / 2);
  box.hidden = false;
}

function renderPops() {
  const l = HOVER?.kind === 'listing' ? byId(HOVER.id) : LAST_HOVERED;
  const pop = $('#shopPop');
  if (l) {
    const near = SHOPS.map(s => ({ s, d: L.latLng(l.lat, l.lon).distanceTo([s.lat, s.lon]) }))
      .filter(x => x.d <= S.shop * 83).sort((a, b) => a.d - b.d).slice(0, 6);
    pop.innerHTML = `<span class="small muted">Supermarkets near ${esc(l.address)}</span>
      <div class="r"><span>Nearest by walking route</span><span>${l.shop} min</span></div>
      ${near.map(x => `<div class="r"><span>${esc(x.s.name || x.s.brand || 'Supermarket')}</span><span>${Math.round(x.d)} m</span></div>`).join('') || '<span class="small">None within this distance.</span>'}
      <span class="foot">Distances in a straight line; walk time along streets from OpenStreetMap.</span>`;
  } else {
    const rows = Object.entries(SHOP_COUNTS).sort((a, b) => b[1] - a[1]);
    pop.innerHTML = `<span class="small muted">Supermarkets inside the current areas</span>
      ${rows.slice(0, 7).map(([b, n]) => `<div class="r"><span>${esc(b)}</span><span>${n}</span></div>`).join('') || '<span class="small">None.</span>'}
      <span class="foot">Hover a listing to see its nearest ones and how far they are.</span>`;
  }
  const pp = $('#parkPop');
  const nearParks = l && PARKS ? PARKS.map(p => ({ p, d: L.latLng(l.lat, l.lon).distanceTo([p.lat, p.lon]) })).sort((a, b) => a.d - b.d).slice(0, 5) : [];
  pp.innerHTML = `<span class="small muted">${l ? 'Parks near ' + esc(l.address) : 'Parks'}</span>
    ${l ? `<div class="r"><span>Nearest by walking route</span><span>${l.park ?? '30+'} min</span></div>` : ''}
    ${nearParks.map(x => `<div class="r"><span>${esc(x.p.name || 'Park')} (${x.p.ha} ha)</span><span>${Math.round(x.d)} m</span></div>`).join('')}
    <span class="foot">Parks of 0.5 ha or more in OpenStreetMap; walk time along streets to the park's edge. At the slider's end (Any) parks don't limit the areas. ${PARKS ? '' : 'Turn on "Parks" under Show on map to list them.'}</span>`;
}
let LAST_HOVERED = null;

function renderQuick() {
  $('#quickFilters').innerHTML = `
    <label>Max rent (€)<input type="number" data-f="rentMax" value="${esc(S.f.rentMax)}" placeholder="1500"></label>
    <label>Min m²<input type="number" data-f="m2" value="${esc(S.f.m2)}" placeholder="50"></label>
    <label>Min rooms<input type="number" data-f="rooms" value="${esc(S.f.rooms)}" placeholder="2"></label>
    <label>Min score<input type="number" step="0.5" min="0" max="10" data-f="minScore" value="${esc(S.f.minScore)}" placeholder="6"></label>
    ${gmPickerHtml('q')}
    ${selectHtml('label', 'Energy label', LABELS)}
    ${selectHtml('interior', 'Interior', INTERIOR_OPTS)}
    ${selectHtml('listed', 'Listed within', LISTED)}
    ${selectHtml('state', 'Availability', STATES)}
    <a href="#listings" class="small" style="align-self:end;padding-bottom:8px">All filters</a>`;
}
const LABELS = [['any', 'Any'], ['A', 'A or better'], ['B', 'B or better'], ['C', 'C or better'], ['D', 'D or better']];
const INTERIOR_OPTS = [['any', 'Any'], ['furnished', 'Furnished'], ['notfurnished', 'Not furnished'], ['upholstered', 'Gestoffeerd'], ['bare', 'Kaal']];
const LISTED = [['any', 'Any time'], ['1', 'Last 24 hours'], ['3', 'Last 3 days'], ['7', 'Last week'], ['14', 'Last 2 weeks'], ['31', 'Last month']];
function selectHtml(key, label, opts) {
  return `<label>${label}<select data-f="${key}">${opts.map(([v, t]) => `<option value="${v}" ${String(S.f[key]) === v ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select></label>`;
}

// municipality multi-select (built from the listings themselves)
function gmCounts() {
  const c = {};
  for (const l of LISTINGS) if (l.gemeente && passesFilters(l, S.f, 'gm')) c[l.gemeente] = (c[l.gemeente] || 0) + 1;
  for (const g of S.f.gm) c[g] = c[g] || 0;
  return Object.entries(c).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}
function gmLabel() { const g = S.f.gm; return !g.length ? 'All' : g.length === 1 ? g[0] : `${g.length} selected`; }
function gmPickerHtml(id) {
  return `<label>Municipalities<span class="gmwrap"><button type="button" class="btn gmbtn" data-gmopen="${id}" aria-haspopup="listbox">${esc(gmLabel())} ▾</button>
    <div class="menu gmmenu" id="gm-${id}" hidden></div></span></label>`;
}
function renderGmMenu(id, filter = '') {
  const m = $('#gm-' + id);
  if (!m) return;
  const rows = gmCounts().filter(([g]) => g.toLowerCase().includes(filter.toLowerCase()));
  m.innerHTML = `<input type="text" class="gmsearch" data-gmsearch="${id}" placeholder="Search" value="${esc(filter)}">
    <div class="gmlist">${rows.map(([g, n]) => `<label class="check"><input type="checkbox" data-gm="${esc(g)}" ${S.f.gm.includes(g) ? 'checked' : ''}> <span style="flex:1">${esc(g)}</span><span class="muted small">${n}</span></label>`).join('') || '<span class="small muted">No municipalities</span>'}</div>
    <div class="row" style="justify-content:space-between"><button type="button" class="btn" data-gmclear="1">Clear</button><button type="button" class="btn primary" data-gmdone="1">Done</button></div>`;
}

// ---------- full table ----------
const COLS = [
  ['star', '★', l => user(l).star ? 0 : 1, l => starBtn(l)],
  ['address', 'Address', l => l.address, l => `<a href="#listing/${encodeURIComponent(l.id)}" style="color:inherit;text-decoration:none;font-weight:500">${esc(l.address)}</a> ${warnHtml(l)}<div class="sub2">${esc(l.city || '')}${l.buurt ? ' · ' + esc(l.buurt) : ''}</div>`],
  ['gemeente', 'Municipality', l => l.gemeente || 'zz', l => esc(l.gemeente || '—')],
  ['score', 'Score', l => num(l.score?.total, -1), l => l.score?.total != null ? `${scoreHtml(l)}<div class="sub2">top ${100 - (l.score.totalPct ?? 0)}%</div>` : '—'],
  ['rent', 'Rent', l => num(l.rent, 1e9), l => `<b>${fmtEur(l.rent)}</b> ${chgHtml(l)}`],
  ['ppm2', '€/m²', l => num(ppm2(l), 1e9), l => ppm2(l) ? '€' + ppm2(l).toFixed(1) : '—'],
  ['ppr', '€/room', l => num(ppr(l), 1e9), l => ppr(l) ? fmtEur(ppr(l)) : '—'],
  ['m2', 'm²', l => num(l.m2, 0), l => l.m2 ?? '—'],
  ['rooms', 'Rooms / beds', l => num(l.rooms, 0), l => `${l.rooms ?? '—'} / ${l.bedrooms ?? '—'}`],
  ['label', 'Energy label', l => LABEL_RANK(l.energyLabel), l => l.energyLabel ? `<span class="lbl ${esc(l.energyLabel[0])}" title="${esc(l.energySource || '')}">${esc(l.energyLabel)}</span>` : '—'],
  ['interior', 'Interior', l => l.interior || 'z', l => INTERIOR[l.interior] || '—'],
  ['avail', 'Available', l => l.availableFrom || 'zz', l => esc(l.availableFrom || '—')],
  ['listed', 'Listed', l => num(ageDays(l), 1e9), l => ageTxt(l)],
  ['travel', 'To destination', l => num(listingTravel(l), 1e9), l => { const r = route(l); return `<b style="color:var(--accent-dark)">${r?.min ?? '—'} min</b><div class="sub2">${esc(r?.direct ? routeText(r) : (BYCODE[r?.station]?.name || '') + ', ' + (r?.walk ?? '') + ' min walk')}</div>`; }],
  ['status', 'Status', l => (user(l).status || 'zz') + (l.status || ''), l => stateTags(l) || '—'],
  ['sources', 'Sources', l => (l.sources || []).length, l => (l.sources || []).map(s => `<a class="tag" href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.site)}</a>`).join(' ')],
];
function tableRows() {
  const c = COLS.find(c => c[0] === S.tableSort.key) || COLS[3];
  return LISTINGS.filter(l => passesFilters(l)).sort((a, b) => {
    const x = c[2](a), y = c[2](b);
    return (x > y ? 1 : x < y ? -1 : 0) * (S.tableSort.asc ? 1 : -1);
  });
}
function renderTable() {
  const rows = tableRows();
  $('#tableTitle').textContent = `${rows.length} listings inside the qualifying areas`;
  $('#tableCriteria').innerHTML = `Walk ≤ ${S.walk} min · supermarket ≤ ${S.shop} min${S.park < PARK_ANY ? ` · park ≤ ${S.park} min` : ''} · ${S.direct ? 'direct ' : ''}train ≤ ${S.train} min off-peak · <a href="#map">change on map</a>`;
  $('#table thead').innerHTML = '<tr>' + COLS.map(([k, label]) => {
    const on = S.tableSort.key === k;
    return `<th><button data-col="${k}" class="${on ? 'on' : ''}">${label} ${on ? (S.tableSort.asc ? '↑' : '↓') : ''}</button></th>`;
  }).join('') + '</tr>';
  renderTableBodyOnly(rows);
  $('#fullFilters').innerHTML = `
    <label>Rent (€ / month)<span class="pair"><input type="number" data-f="rentMin" value="${esc(S.f.rentMin)}" placeholder="min"><input type="number" data-f="rentMax" value="${esc(S.f.rentMax)}" placeholder="max"></span></label>
    ${selectHtml('rentIncl', 'Rent includes', [['any', 'Any'], ['incl', 'Incl. service costs'], ['excl', 'Excl. service costs']])}
    <label>Max rent per m²<input type="number" data-f="ppm2Max" value="${esc(S.f.ppm2Max)}" placeholder="25"></label>
    <label>Min floor area (m²)<input type="number" data-f="m2" value="${esc(S.f.m2)}" placeholder="50"></label>
    <label>Min rooms / bedrooms<span class="pair"><input type="number" data-f="rooms" value="${esc(S.f.rooms)}" placeholder="rooms"><input type="number" data-f="beds" value="${esc(S.f.beds)}" placeholder="beds"></span></label>
    <label>Min score (0–10)<input type="number" step="0.5" min="0" max="10" data-f="minScore" value="${esc(S.f.minScore)}" placeholder="6"></label>
    ${gmPickerHtml('t')}
    ${selectHtml('label', 'Energy label', LABELS)}
    ${selectHtml('interior', 'Interior', INTERIOR_OPTS)}
    <label>Available by<input type="date" data-f="avail" value="${esc(S.f.avail)}"></label>
    ${selectHtml('listed', 'Listed within', LISTED)}
    ${selectHtml('list', 'My lists', LISTS)}
    ${selectHtml('state', 'Availability', STATES)}
    <label>More<span class="pair" style="padding-top:6px;flex-wrap:wrap;gap:10px">
      <span class="check" style="color:var(--ink)"><input type="checkbox" data-f="pets" ${S.f.pets ? 'checked' : ''}> Pets allowed</span>
      <span class="check" style="color:var(--ink)"><input type="checkbox" data-f="newOnly" ${S.f.newOnly ? 'checked' : ''}> New in latest refresh</span>
      <span class="check" style="color:var(--ink)"><input type="checkbox" id="dups" ${S.dups ? 'checked' : ''}> Show duplicate ads</span></span></label>`;
}
function renderTableBodyOnly(rows = tableRows()) {
  $('#tableTitle').textContent = `${rows.length} listings inside the qualifying areas`;
  $('#table tbody').innerHTML = rows.map(l => `<tr class="${user(l).hidden ? 'dim' : ''}">` + COLS.map(c => `<td>${c[3](l)}</td>`).join('') + '</tr>').join('')
    || `<tr><td colspan="${COLS.length}" class="muted" style="padding:24px;text-align:center">No listings match.</td></tr>`;
}
function exportCsv() {
  const head = ['address', 'city', 'municipality', 'neighbourhood', 'postcode', 'score', 'rent', 'price_change', 'rent_per_m2', 'rent_per_room', 'service_costs', 'm2', 'rooms',
    'bedrooms', 'energy_label', 'interior', 'available_from', 'listed', 'first_seen', 'status', 'my_status', 'starred', 'station', 'walk_to_station_min', 'walk_to_supermarket_min',
    'walk_to_park_min', 'door_to_door_min', 'sources'];
  const rows = tableRows().map(l => { const r = route(l); return [l.address, l.city, l.gemeente, l.buurt, l.postcode, l.score?.total, l.rent, l.priceChange, ppm2(l)?.toFixed(1), ppr(l)?.toFixed(0),
    l.serviceCosts, l.m2, l.rooms, l.bedrooms, l.energyLabel, INTERIOR[l.interior] || '', l.availableFrom, l.listedAt, l.firstSeen, l.missing ? 'not found in last scan' : l.status,
    STATUS_NAME[user(l).status] || '', user(l).star ? 'yes' : '', r?.direct ? '(straight there)' : BYCODE[r?.station]?.name, r?.walk, l.shop, l.park, r?.min,
    (l.sources || []).map(s => s.url).join(' ')]; });
  const csv = [head, ...rows].map(r => r.map(v => `"${String(v ?? '').replace(/"/g, '""')}"`).join(',')).join('\r\n');
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob(['﻿' + csv], { type: 'text/csv' }));
  a.download = `rentals-${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
}

// ---------- detail page ----------
let photoIdx = 0, detailId = null;
function renderDetail(id) {
  const l = byId(id);
  const el = $('#view-detail');
  if (detailId !== id) { photoIdx = 0; detailId = id; }
  if (!l) { el.innerHTML = `<a href="#listings">← Back to listings</a><div class="empty">This listing isn't inside the current areas or no longer exists.</div>`; return; }
  const r = route(l);
  const sc = l.score || {};
  const photos = (l.photos && l.photos.length ? l.photos : l.photo ? [l.photo] : []);
  const u = user(l);
  const hid = l.homeId || l.id;
  const st = BYCODE[r?.station];
  el.innerHTML = `<a href="#listings">← Back to listings</a>
  ${l.status === 'removed' ? `<div class="banner">This home was removed from ${esc((l.sources || []).map(s => s.site).join(', '))}${l.removedAt ? ' around ' + esc(l.removedAt.slice(0, 10)) : ''}.</div>` :
    l.missing ? '<div class="banner">Not found in the last scan\'s search results, though the site hasn\'t marked it removed. It may be let or paused.</div>' : ''}
  ${sc.suspicious?.length ? `<div class="banner warn">⚠ Check carefully: ${esc(sc.suspicious.join('; '))}. Never pay before a viewing or a signed contract.</div>` : ''}
  <div class="detail">
    <div class="main">
      <div class="photo" style="${photos.length ? `background-image:url('${esc(photos[photoIdx] || photos[0])}')` : ''}">${photos.length ? '' : 'No photo'}
        ${photos.length > 1 ? `<button class="pnav prev" data-photo="-1" aria-label="Previous photo">‹</button><button class="pnav next" data-photo="1" aria-label="Next photo">›</button><span class="pcount">${photoIdx + 1} / ${photos.length}</span>` : ''}</div>
      ${photos.length > 1 ? `<div class="thumbs">${photos.map((p, i) => `<button class="thumb${i === photoIdx ? ' on' : ''}" data-photoidx="${i}" style="background-image:url('${esc(p)}')" aria-label="Photo ${i + 1}"></button>`).join('')}</div>` : ''}
      <section class="card">
        <div class="pagehead"><h1>${esc(l.address)}${l.city ? ', ' + esc(l.city) : ''} ${warnHtml(l)}</h1>
          <span style="font-size:20px;font-weight:600">${fmtEur(l.rent)} ${chgHtml(l)} <span class="small muted" style="font-weight:400">/ month${l.serviceCosts ? ' + ' + fmtEur(l.serviceCosts) + ' service' : l.rentIncl ? ' incl.' : ''}</span></span></div>
        <span class="small muted">Listed ${ageTxt(l)} · first seen here ${esc((l.firstSeen || '').slice(0, 10))} · on ${(l.sources || []).map(s => esc(s.site)).join(' and ')}${l.gemeente ? ' · ' + esc(l.buurt ? l.buurt + ', ' : '') + 'municipality ' + esc(l.gemeente) : ''}</span>
        <div class="props">
          ${prop('Floor area', l.m2 ? l.m2 + ' m²' : '—')}${prop('Rooms / bedrooms', `${l.rooms ?? '—'} / ${l.bedrooms ?? '—'}`)}
          ${prop('Rent per m²', ppm2(l) ? '€' + ppm2(l).toFixed(1) : '—')}${prop('Rent per room', ppr(l) ? fmtEur(ppr(l)) : '—')}
          ${prop('Energy label', l.energyLabel ? l.energyLabel + (l.energySource ? ` (${l.energySource})` : '') : '—')}${prop('Interior', INTERIOR[l.interior] || '—')}
          ${prop('Available', l.availableFrom || '—')}${prop('Deposit', l.deposit ? fmtEur(l.deposit) + (l.depositMonths ? ` (${l.depositMonths} mo)` : '') : '—')}
          ${prop('Pets', l.pets == null ? 'Not stated' : l.pets ? 'Allowed' : 'Not allowed')}${prop('Outdoor space', (l.outdoor || []).join(', ') || 'Not stated')}
          ${prop('Postcode', l.postcode || '—')}${prop('Type', l.type || '—')}
        </div>
        ${(l.flags || []).length ? `<div class="flags">${l.flags.map(f => `<span class="tag ${f.penalty >= 2 ? 'removed' : ''}">${esc(f.label)}</span>`).join('')}</div>` : ''}
        ${(l.priceHistory || []).length > 1 ? `<div class="small"><b>Rent history:</b> ${l.priceHistory.map(([d, p]) => `${esc(d)} ${fmtEur(p)}`).join(' → ')}</div>` : ''}
      </section>
      ${scoreCard(l)}
      ${l.description ? `<section class="card"><h2>Description</h2><p class="desc">${esc(l.description)}</p></section>` : ''}
    </div>
    <div class="side">
      <section class="card">
        <h2>My notes</h2>
        <div class="notesrow">
          <button class="btn${u.star ? ' primary' : ''}" data-star="${esc(hid)}">${u.star ? '★ On shortlist' : '☆ Add to shortlist'}</button>
          <select data-ustatus="${esc(hid)}" aria-label="Status">${STATUS.map(([v, t]) => `<option value="${v}" ${(u.status || '') === v ? 'selected' : ''}>${t}</option>`).join('')}</select>
        </div>
        <textarea data-unote="${esc(hid)}" rows="3" placeholder="Notes: contact person, viewing date, why rejected…">${esc(u.note || '')}</textarea>
        <label class="check small"><input type="checkbox" data-uhidden="${esc(hid)}" ${u.hidden ? 'checked' : ''}> Hide this home from the lists</label>
      </section>
      <section class="card">
        <h2>Travel time</h2>
        <div class="seg" role="group" aria-label="Destination"><button data-dest="cs" class="${S.dest === 'cs' ? 'on' : ''}">Den Bosch CS</button><button data-dest="lg" class="${S.dest === 'lg' ? 'on' : ''}">Lucas Gasselstraat 2</button></div>
        <div class="seg small" role="group" aria-label="Day"><button data-day="wk" class="${S.day === 'wk' ? 'on' : ''}">Weekday</button><button data-day="sa" class="${S.day === 'sa' ? 'on' : ''}">Saturday</button><button data-day="su" class="${S.day === 'su' ? 'on' : ''}">Sunday</button></div>
        <div class="breakdown">
          ${r?.direct ? `<div><span>${S.lastMode === 'walk' ? 'Walk' : 'Bike'} straight to Lucas Gasselstraat 2 (${(l.destM / 1000).toFixed(1)} km)</span><span>${r.min} min</span></div>` : r ? `
          <div><span>Walk to ${esc(st?.name || 'station')}</span><span>${r.walk} min</span></div>
          ${st?.code === r.hub ? '' : `<div><span>Wait + ${S.direct ? 'direct ' : ''}train to ${esc(HUB_NAMES()[r.hub] || '')} at ${hh(S.hour)}</span><span>${r.train} min</span></div>`}
          ${S.dest === 'lg' ? `<div><span>${S.lastMode === 'walk' ? 'Walk' : 'Bike'} from ${esc(HUB_NAMES()[r.hub] || '')} to Lucas Gasselstraat 2</span><span>${r.last} min</span></div>` : ''}` : '<div><span>No train at this hour</span><span>—</span></div>'}
          ${S.dest === 'lg' && !r?.direct && l.destM != null ? `<div><span>Or ${S.lastMode} straight there (${(l.destM / 1000).toFixed(1)} km)</span><span>${directMin(l.destM)} min</span></div>` : ''}
          <div><span>Walk to nearest supermarket / park</span><span>${l.shop} / ${l.park ?? '30+'} min</span></div>
          ${st && !st.hub ? `<div><span>Last direct train back from Den Bosch</span><span>${st.lastHome || '—'}</span></div>` : ''}
        </div>
        ${barsHtml(h => listingTravel(l, h))}
        <div class="small"><b>Leaving at ${hh(S.hour)}: ${r?.min ?? '—'} min</b> door to door. Click a bar to pick another hour.</div>
      </section>
      <section class="card"><h2>Sources</h2>
        ${(l.sources || []).map(s => `<div class="row" style="justify-content:space-between"><a class="btn" href="${esc(s.url)}" target="_blank" rel="noopener">View on ${esc(s.site)} ↗</a><span class="small muted">${fmtEur(s.rent)}${s.listedAt ? ' · listed ' + esc(s.listedAt) : ''}</span></div>`).join('')}
        ${(l.sources || []).length > 1 ? `<span class="small muted">${l.sources.length} ads merged into this home${l.rentRange ? ` (rent ${fmtEur(l.rentRange[0])}–${fmtEur(l.rentRange[1])}; lowest shown)` : ''}.</span>` : ''}
        ${findLinks(l)}
      </section>
      ${nbCard(l)}
      <div id="miniMap" style="height:220px;border-radius:10px;border:1px solid var(--line)"></div>
    </div>
  </div>`;
  const mm = L.map('miniMap', { zoomControl: false, attributionControl: false }).setView([l.lat, l.lon], 15);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19 }).addTo(mm);
  L.marker([l.lat, l.lon], { icon: L.divIcon({ className: '', html: '<div class="pin-icon sel">⌂</div>', iconSize: [22, 22], iconAnchor: [11, 11] }) }).addTo(mm);
  if (st) { L.circleMarker([st.lat, st.lon], { radius: 7, color: '#fff', weight: 2, fillColor: '#0F766E', fillOpacity: 1 }).bindTooltip(esc(st.name)).addTo(mm); mm.fitBounds([[l.lat, l.lon], [st.lat, st.lon]], { padding: [30, 30], maxZoom: 16 }); }
}
function scoreCard(l) {
  const sc = l.score;
  if (!sc || sc.total == null) return '';
  const order = Object.keys(SCORE_INFO);
  const rows = order.filter(k => sc.parts[k]).map(k => {
    const p = sc.parts[k], info = SCORE_INFO[k];
    const pct = p.pct;
    return `<div class="srow" title="${esc(info.how)}">
      <span class="sname">${esc(info.label)}</span>
      <span class="sbar"><i style="width:${(p.score ?? 0) * 10}%"></i></span>
      <span class="sval">${p.score == null ? '—' : p.score.toFixed(1)}</span>
      <span class="spct">${p.score == null ? 'not counted' : pct == null ? '' : pct >= 50 ? `top ${Math.max(1, 100 - pct)}%` : `bottom ${Math.max(1, pct)}%`}</span>
      <span class="stext">${esc(p.text || '')}</span></div>`;
  }).join('');
  const missing = order.filter(k => !sc.parts[k]).map(k => SCORE_INFO[k].label);
  return `<section class="card"><div class="cardhead"><h2>Score ${scoreHtml(l, true)}</h2><span class="small muted">better than ${sc.totalPct ?? '?'}% of all homes in the data</span></div>
    <div class="scores">${rows}</div>
    ${missing.length ? `<span class="small muted">Not scored (no data): ${esc(missing.join(', '))}.</span>` : ''}
    <span class="small muted">Each part is 0–10 on a fixed scale; hover a row for how it's worked out. Weights in the total: ${order.filter(k => SCORE_INFO[k].weight > 0).sort((a, b) => SCORE_INFO[b].weight - SCORE_INFO[a].weight).map(k => `${esc(SCORE_INFO[k].label.toLowerCase())} ×${SCORE_INFO[k].weight}`).join(', ')} (<a href="#data">change</a>).</span></section>${legalCard(l)}`;
}
function legalCard(l) {
  const w = l.score?.wws;
  if (!w) return '';
  const fmtP = p => (Math.round(p * 10) / 10).toLocaleString('nl-NL');
  const verdict = w.sector === 'free' ? `<b>Free sector</b>: about ${w.points} points, so no legal maximum rent.`
    : w.over > 0 ? `<b>The rent is about ${fmtEur(w.over)} above the estimated legal maximum of ${fmtEur(w.maxRent)}.</b> If a proper check confirms it, the Huurcommissie can lower the rent.`
    : `<b>The rent is within the estimated legal maximum of ${fmtEur(w.maxRent)}</b> (${fmtEur(-w.over)} below it).`;
  return `<section class="card"><div class="cardhead"><h2>Rent vs. legal maximum</h2><span class="small muted">points system (${esc(w.table)})</span></div>
    <div class="small">${verdict}</div>
    <table class="pts"><thead><tr><th>Part</th><th>How</th><th>Points</th></tr></thead><tbody>
      ${w.items.map(it => `<tr><td>${esc(it.what)}${it.known ? '' : ' <span class="muted" title="Estimated or assumed: not in the listing">*</span>'}</td><td>${esc(it.how)}</td><td>${fmtP(it.points)}</td></tr>`).join('')}
      ${w.capped ? `<tr><td>WOZ cap</td><td>WOZ points may be at most 33% of the total</td><td>−${fmtP(w.capped)}</td></tr>` : ''}
      <tr class="tot"><td>Total</td><td></td><td>${w.points}</td></tr>
      <tr><td>Maximum rent</td><td colspan="2">${esc(w.maxHow)}</td></tr>
      ${w.maxRent ? `<tr><td>Asked rent</td><td></td><td>${fmtEur(l.rent)}</td></tr>` : ''}
    </tbody></table>
    <span class="small muted">* estimated or assumed: not in the listing. A rough estimate to keep things honest, not a valuation; the official check is the Huurcommissie's <a href="https://www.huurcommissie.nl/support/huurprijscheck" target="_blank" rel="noopener">huurprijscheck</a>.</span></section>`;
}
function nbCard(l) {
  const nb = l.score?.nb;
  if (!nb && !l.buurt) return '';
  const fmtK = v => v == null ? '—' : '€' + Math.round(v / 1000) + 'k';
  return `<section class="card"><h2>Neighbourhood${l.buurt ? ': ' + esc(l.buurt) : ''}</h2>
    <div class="breakdown">
      <div><span>Liveability (Leefbaarometer 2024)</span><span>${esc(nb?.lbmLabel || '—')}</span></div>
      <div><span>Registered crimes per 1000 inhabitants (2025)</span><span>${nb?.crimePer1000 ?? '—'}</span></div>
      <div><span>Average WOZ value of homes</span><span>${fmtK(nb?.woz)}</span></div>
      <div><span>Average income per inhabitant</span><span>${fmtK(nb?.income)}</span></div>
      <div><span>Inhabitants</span><span>${nb?.inhabitants?.toLocaleString('nl-NL') ?? '—'}</span></div>
    </div>
    <a href="#municipalities" data-gmgo="${esc(l.gemeente || '')}" class="small">Compare with other neighbourhoods in ${esc(l.gemeente || 'this municipality')}</a></section>`;
}
function findLinks(l) {
  const town = (l.city || l.gemeente || '').replace(/'/g, '');
  const slug = /^s-?hertogenbosch$/i.test(town) || /den bosch/i.test(town) ? 'den-bosch' : town.toLowerCase().replace(/[^a-z0-9]+/g, '-');
  const pc4 = (l.postcode || '').slice(0, 4);
  const lo = l.rent ? Math.floor(l.rent * 0.9 / 50) * 50 : null, hi = l.rent ? Math.ceil(l.rent * 1.1 / 50) * 50 : null;
  const m2lo = l.m2 ? Math.max(0, l.m2 - 5) : null, m2hi = l.m2 ? l.m2 + 5 : null;
  const fArea = pc4 ? `%5B%22${pc4}%22%5D` : `%5B%22${slug}%22%5D`;
  const funda = `https://www.funda.nl/zoeken/huur?selected_area=${fArea}${lo ? `&price=%22${lo}-${hi}%22` : ''}${m2lo ? `&floor_area=%22${m2lo}-${m2hi}%22` : ''}`;
  const pararius = `https://www.pararius.nl/huurwoningen/${slug}${lo ? `/${lo}-${hi}` : ''}${l.m2 ? `/${Math.max(10, Math.floor((l.m2 - 5) / 5) * 5)}m2` : ''}`;
  const g = encodeURIComponent(`"${l.address}" ${l.city || ''} huur`);
  const aggregator = (l.sources || []).every(s => s.site === 'Rentola');
  return `<div style="border-top:1px solid var(--line2);padding-top:10px;display:flex;flex-direction:column;gap:6px">
    <span class="small muted">${aggregator ? 'Rentola re-lists homes from Pararius, Funda and others. Look for the original ad (same area, rent ±10% and size ±5 m²):' : 'Also look for this home on:'}</span>
    <span class="row small"><a class="btn" href="${funda}" target="_blank" rel="noopener">Funda ↗</a><a class="btn" href="${pararius}" target="_blank" rel="noopener">Pararius ↗</a><a class="btn" href="https://www.google.com/search?q=${g}" target="_blank" rel="noopener">Search the address ↗</a></span>
    <span class="small muted">Funda searches postcode ${esc(pc4 || town)}; no results there usually means the ad is on Pararius or a landlord's site instead.</span>
  </div>`;
}
const prop = (k, v) => `<div><span>${k}</span><b>${esc(v)}</b></div>`;

// ---------- municipalities page ----------
let MUNI = null, muniOpen = new Set(), muniSort = { key: 'n', asc: false };
async function renderMunicipalities() {
  const el = $('#view-municipalities');
  if (!MUNI) { el.innerHTML = '<div class="empty">Loading…</div>'; MUNI = await fetch('/api/municipalities').then(r => r.json()); }
  const fmtK = v => v == null ? '—' : '€' + Math.round(v / 1000) + 'k';
  const cols = [
    ['name', 'Municipality', m => m.name],
    ['n', 'Homes', m => m.n],
    ['rent', 'Median rent', m => m.rent ?? 1e9],
    ['ppm2', '€/m²', m => m.ppm2 ?? 1e9],
    ['m2', 'Median m²', m => m.m2 ?? 0],
    ['score', 'Median score', m => m.score ?? -1],
    ['lbm', 'Liveability', m => m.stats?.lbm ?? 0],
    ['crime', 'Crimes / 1000', m => m.stats?.crimePer1000 ?? 1e9],
    ['woz', 'Avg WOZ', m => m.stats?.woz ?? 0],
    ['income', 'Income / inhabitant', m => m.stats?.income ?? 0],
    ['inh', 'Inhabitants', m => m.stats?.inhabitants ?? 0],
  ];
  const c = cols.find(x => x[0] === muniSort.key) || cols[1];
  const items = [...MUNI.items].sort((a, b) => { const x = c[2](a), y = c[2](b); return (x > y ? 1 : x < y ? -1 : 0) * (muniSort.asc ? 1 : -1); });
  const cells = (m, sub) => `<td>${sub ? `<span class="sub2">${esc(m.wijk ? m.wijk + ' · ' : '')}</span>${esc(m.name)}` :
    `<button class="linkbtn" data-muni="${esc(m.name)}" aria-expanded="${muniOpen.has(m.name)}">${muniOpen.has(m.name) ? '▾' : '▸'} ${esc(m.name)}</button>`}</td>
    <td>${sub ? m.n : `<a href="#map" data-gmgo="${esc(m.name)}">${m.n}</a>`}</td><td>${fmtEur(m.rent)}</td><td>${m.ppm2 ? '€' + m.ppm2 : '—'}</td><td>${m.m2 ?? '—'}</td>
    <td>${m.score ?? '—'}</td><td>${esc(m.stats?.lbmLabel || '—')}</td><td>${m.stats?.crimePer1000 ?? '—'}</td><td>${fmtK(m.stats?.woz)}</td><td>${fmtK(m.stats?.income)}</td>
    <td>${m.stats?.inhabitants?.toLocaleString('nl-NL') ?? '—'}</td>`;
  el.innerHTML = `<div class="pagehead"><h1>Municipalities</h1><span class="muted small">All homes currently in the data (not limited by the map criteria). Click a name for its neighbourhoods, a count to see those homes on the map.</span></div>
    <div class="tablewrap"><table class="muni"><thead><tr>${cols.map(([k, t]) => `<th><button data-mcol="${k}" class="${muniSort.key === k ? 'on' : ''}">${t} ${muniSort.key === k ? (muniSort.asc ? '↑' : '↓') : ''}</button></th>`).join('')}</tr></thead>
    <tbody>${items.map(m => `<tr>${cells(m)}</tr>` + (muniOpen.has(m.name) ? m.buurten.map(b => `<tr class="subrow">${cells(b, true)}</tr>`).join('') : '')).join('')}</tbody></table></div>
    <p class="hint">Medians of the listings in each area. Liveability: Leefbaarometer 2024 class. Crimes per 1000 inhabitants: registered crimes 2025 (Politie/CBS), not shown for areas under 300 inhabitants (city centres and business areas inflate it). WOZ and income: CBS 2024. Sources: ${esc(MUNI.sources || '')}.</p>`;
}

// ---------- your travel times (Data screen) ----------
function renderTravel() {
  const d = META.destination;
  if (!d) return;
  const names = HUB_NAMES();
  const arr = Object.entries(d.arrivals || {}).filter(([, v]) => v);
  const cell = (code, v, m) => `<input type="number" min="0" max="180" step="1" class="tnum${v.custom?.includes(m) ? ' custom' : ''}" data-leg="${code}" data-mode="${m}" value="${v[m]}" aria-label="${esc(names[code] || code)}, ${m} minutes">
    ${v.custom?.includes(m) ? `<span class="small muted">computed ${v.default[m]}</span>` : ''}`;
  $('#legTable tbody').innerHTML = arr.map(([code, v]) => `<tr><td>${esc(names[code] || code)}</td><td>${(v.m / 1000).toFixed(1)} km</td><td>${cell(code, v, 'bike')}</td><td>${cell(code, v, 'walk')}</td></tr>`).join('');
  const sp = d.speeds, ds = d.defaultSpeeds;
  const fld = (k, label, unit, step) => `<label class="field">${label}<span class="keyrow"><input type="number" step="${step}" data-speed="${k}" value="${sp[k]}" class="${+sp[k] !== +ds[k] ? 'custom' : ''}"><span class="unit">${unit}</span></span>
    ${+sp[k] !== +ds[k] ? `<span class="small muted">default ${ds[k]} ${unit}</span>` : ''}</label>`;
  $('#speeds').innerHTML = fld('walk', 'Walking speed', 'km/h', 0.5) + fld('bike', 'Cycling speed', 'km/h', 0.5) + fld('bikePark', 'Picking up / parking the bike', 'min', 1);
  const custom = arr.some(([, v]) => v.custom?.length) || Object.keys(ds).some(k => +sp[k] !== +ds[k]);
  $('#resetTravel').disabled = !custom;
}
async function saveTravel(reset = false) {
  const body = { arrivals: {}, speeds: {} };
  if (!reset) {
    for (const i of $$('[data-leg]')) {
      const v = META.destination.arrivals[i.dataset.leg];
      if (+i.value !== +v.default[i.dataset.mode]) (body.arrivals[i.dataset.leg] ||= {})[i.dataset.mode] = +i.value;
    }
    for (const i of $$('[data-speed]')) if (+i.value !== +META.destination.defaultSpeeds[i.dataset.speed]) body.speeds[i.dataset.speed] = +i.value;
  }
  $('#travelStatus').textContent = 'Saving…';
  const r = await fetch('/api/travel', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!r.ok) { $('#travelStatus').textContent = 'Could not save: use numbers.'; return; }
  await boot(true);  // new times flow into the map, listings and scores
  renderTravel();
  $('#travelStatus').textContent = reset ? 'Back to the computed times.' : 'Saved. Travel times and scores are updated.';
}
let travelTimer;
document.addEventListener('change', e => { if (e.target.dataset?.leg || e.target.dataset?.speed) { clearTimeout(travelTimer); travelTimer = setTimeout(() => saveTravel(), 200); } });
$('#resetTravel').addEventListener('click', () => saveTravel(true));

// ---------- score weights (Data screen) ----------
let WEIGHTS = null, WDEFAULTS = null, wUndo = [], wTimer;
const wEqual = (a, b) => Object.keys(b).every(k => +a[k] === +b[k]);
async function renderWeights() {
  if (!WEIGHTS) {
    const r = await fetch('/api/weights').then(r => r.json());
    WDEFAULTS = r.defaults;
    WEIGHTS = Object.fromEntries(Object.entries(r.info).map(([k, v]) => [k, v.weight]));
    renderWeights.info = r.info;
  }
  const info = renderWeights.info;
  const sum = Object.values(WEIGHTS).reduce((a, b) => a + +b, 0) || 1;
  $('#weights').innerHTML = Object.keys(info).map(k => `<div class="wrow">
      <label for="w-${k}" title="${esc(info[k].how)}"><span class="dotted">${esc(info[k].label)}</span></label>
      <input type="range" id="w-${k}" data-w="${k}" min="0" max="5" step="0.5" value="${WEIGHTS[k]}">
      <b>×${WEIGHTS[k]}</b>
      <span class="small muted">${Math.round(WEIGHTS[k] / sum * 100)}% of the total${+WEIGHTS[k] !== +WDEFAULTS[k] ? ` · default ×${WDEFAULTS[k]}` : ''}</span></div>`).join('');
  $('#undoWeights').disabled = !wUndo.length;
  $('#resetWeights').disabled = wEqual(WEIGHTS, WDEFAULTS);
}
async function saveWeights(next, pushUndo = true) {
  if (pushUndo) wUndo.push({ ...WEIGHTS });
  WEIGHTS = { ...next };
  renderWeights();
  $('#weightsStatus').textContent = 'Saving…';
  const r = await fetch('/api/weights', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ weights: WEIGHTS }) });
  if (!r.ok) { $('#weightsStatus').textContent = 'Could not save the weights.'; return; }
  await loadAreas();  // scores are recomputed by the server
  $('#weightsStatus').textContent = 'Saved. Scores and percentiles are updated.';
}
document.addEventListener('input', e => {
  const k = e.target.dataset?.w;
  if (!k) return;
  const row = e.target.closest('.wrow');
  row.querySelector('b').textContent = '×' + e.target.value;
  clearTimeout(wTimer);
  const prev = { ...WEIGHTS };
  wTimer = setTimeout(() => { wUndo.push(prev); saveWeights({ ...WEIGHTS, [k]: +e.target.value }, false); }, 400);
});
$('#undoWeights').addEventListener('click', () => { if (wUndo.length) saveWeights(wUndo.pop(), false); });
$('#resetWeights').addEventListener('click', () => saveWeights({ ...WDEFAULTS }));

// ---------- data page ----------
let statusTimer;
async function renderData() {
  const s = await fetch('/api/status').then(r => r.json());
  const meta = s.meta || {}, lst = s.listings;
  $('#dsListUpdated').textContent = lst.updated ? 'Updated ' + relTime(lst.updated) : 'Never run';
  const en = lst.energy || {};
  $('#dsSources').innerHTML = Object.entries(lst.sources || {}).map(([site, v]) =>
    `<div><span>${esc(site)}</span><span class="${v.ok ? 'ok' : 'bad'}">${v.ok ? `${v.found} found · ${v.kept ?? 0} in areas` : esc(v.error || "failed")}</span></div>`).join('')
    + `<div><span>Homes stored</span><b>${lst.total}</b></div><div><span>Inside the current areas</span><b>${LISTINGS.length}</b></div>
       <div><span>Energy labels</span><span>${en.epOnline != null ? `${en.epOnline} official, ${en.fromText} from listing texts` : '—'}</span></div>`;
  $('#keyStatus').innerHTML = s.energyKey ? '<span class="ok">✓ Key saved.</span> Paste a new one to replace it.'
    : '<span class="bad">No key yet.</span> Until there is one, labels only come from listing texts.';

  $('#dsMapBuilt').textContent = meta.mapBuilt ? 'Built ' + meta.mapBuilt : 'Not built';
  const dates = s.timetable.dates || {};
  const arr = meta.destination?.arrivals || {};
  $('#dsMap').innerHTML = `<div><span>Timetable</span><span>generated ${esc(s.timetable.generated || '—')}</span></div>
    <div><span>Sample days used</span><span>${Object.values(dates).map(d => d.slice(6, 8) + '-' + d.slice(4, 6)).join(', ')}</span></div>
    <div><span>Stations within 60 min</span><span>${s.timetable.stations}</span></div>
    <div><span>Stations with walking areas</span><span>${meta.stationsWithGrid ?? 0}</span></div>
    <div><span>Supermarkets / parks</span><span>${meta.supermarkets ?? 0} / ${meta.parks ?? 0}</span></div>
    <div><span>Destination</span><span>${esc(meta.destination?.label || '—')}</span></div>
    ${Object.entries(arr).filter(([, v]) => v).map(([c, v]) => `<div><span>From ${esc(HUB_NAMES()[c] || c)}</span><span>${(v.m / 1000).toFixed(1)} km · ${v.bike} min bike, ${v.walk} min walk</span></div>`).join('')}`;
  const j = s.job;
  const running = j.name && !j.finished;
  const last = (j.log || []).slice().reverse().find(l => /ACTION:|passed, continuing/.test(l)) || '';
  const action = running && last.includes('ACTION:') ? last.replace(/^.*ACTION:\s*/, '') : '';
  $('#actionBar').hidden = !action;
  $('#actionBar').textContent = action;
  $('#jobState').textContent = !j.name ? 'Nothing run since the server started' : running ? `${j.name === 'map' ? 'Rebuilding map data' : j.name === 'energy' ? 'Looking up energy labels' : 'Refreshing listings'} since ${j.started.slice(11)}` : `${j.name} finished ${j.finished.slice(11)} ${j.code === 0 ? '' : '(failed)'}`;
  $('#jobLog').textContent = (j.log || []).join('\n') || '—';
  $('#jobLog').scrollTop = 1e9;
  for (const b of ['#refreshListings', '#rebuildMap', '#refreshListingsTop', '#lookupEnergy']) $(b).disabled = !!running;
  if (!s.energyKey) $('#lookupEnergy').disabled = true;
  $('#lookupEnergy').title = s.energyKey ? '' : 'Save an API key first';
  $('#epKey').placeholder = s.energyKey ? 'Paste a new key to replace the saved one' : 'Paste your EP-Online API key';
  $('#refreshListingsTop').textContent = running && j.name === 'listings' ? 'Refreshing…' : 'Refresh listings';
  clearTimeout(statusTimer);
  if (running) statusTimer = setTimeout(renderData, 3000);
  else if (j.finished && renderData.lastFinished !== j.finished) { renderData.lastFinished = j.finished; MUNI = null; await boot(true); }
}
async function startJob(name) {
  const r = await fetch('/api/refresh/' + name, { method: 'POST' });
  if (!r.ok) alert((await r.json()).detail || 'Could not start');
  renderData();
}

// ---------- routing and events ----------
function routeView() {
  const h = location.hash || '#map';
  const view = h.startsWith('#listing/') ? 'detail' : h.slice(1);
  for (const v of ['map', 'listings', 'municipalities', 'detail', 'data']) $('#view-' + v).hidden = v !== view;
  $$('.top nav a').forEach(a => a.classList.toggle('active', a.dataset.view === (view === 'detail' ? 'listings' : view)));
  if (view === 'map') setTimeout(() => map.invalidateSize(), 0);
  if (view === 'listings') renderTable();
  if (view === 'municipalities') renderMunicipalities();
  if (view === 'detail') renderDetail(decodeURIComponent(h.slice(9)));
  if (view === 'data') { renderData(); renderWeights(); renderTravel(); }
}
function renderAll() {
  invalidate();
  renderControls(); drawStations(); renderList(); renderDetailCard(); renderQuick(); renderPops();
  if (!$('#view-listings').hidden) renderTable();
  if (!$('#view-detail').hidden) renderDetail(decodeURIComponent(location.hash.slice(9)));
}
function select(sel) { S.sel = sel; save(); renderDetailCard(); }
function set(patch, reload = false) { Object.assign(S, patch); save(); reload ? (renderControls(), scheduleAreas()) : renderAll(); }

for (const [id, key] of [['walk', 'walk'], ['shop', 'shop'], ['park', 'park'], ['train', 'train']])
  $('#' + id).addEventListener('input', e => set({ [key]: +e.target.value }, true));
$('#hour').addEventListener('input', e => set({ hour: +e.target.value }));
$('#lastMode').addEventListener('change', e => { set({ lastMode: e.target.value }); if (S.lyrHeat) scheduleAreas(false); });
$('#direct').addEventListener('change', e => set({ direct: e.target.checked }, true));
$('#otherToggle').addEventListener('click', () => set({ other: !S.other }));
for (const k of ['lyrAreas', 'lyrHeat', 'lyrStations', 'lyrListings']) $('#' + k).addEventListener('change', e => { S[k] = e.target.checked; save(); renderControls(); loadAreas(k === 'lyrListings'); });
$('#lyrShops').addEventListener('change', e => { S.lyrShops = e.target.checked; save(); drawShops(); renderLegend(); });
$('#lyrParks').addEventListener('change', e => { S.lyrParks = e.target.checked; save(); drawParks().then(renderPops); renderLegend(); });
$('#toggleLeft').addEventListener('click', () => { S.leftOpen = !S.leftOpen; save(); renderControls(); map.invalidateSize(); });
$('#toggleRight').addEventListener('click', () => { S.rightOpen = !S.rightOpen; save(); renderControls(); map.invalidateSize(); });

document.addEventListener('click', e => {
  const t = e.target.closest('button, a, [data-listing]');
  if (!e.target.closest('.gmwrap')) $$('.gmmenu').forEach(m => m.hidden = true);
  if (!t) { $('#sortMenu').hidden = true; return; }
  if (t.dataset.star) { e.preventDefault(); e.stopPropagation(); const l = LISTINGS.find(x => (x.homeId || x.id) === t.dataset.star); setUser(t.dataset.star, { star: !user(l || {}).star }); return; }
  if (t.dataset.dest) { set({ dest: t.dataset.dest }); if (S.lyrHeat) scheduleAreas(false); }
  else if (t.dataset.day) set({ day: t.dataset.day });
  else if (t.dataset.hour) set({ hour: +t.dataset.hour });
  else if (t.dataset.sort) { $('#sortMenu').hidden = true; set({ sort: t.dataset.sort }); }
  else if (t.dataset.list) { S.f.list = t.dataset.list; save(); renderAll(); }
  else if (t.dataset.listing && !t.dataset.star) { location.hash = '#listing/' + encodeURIComponent(t.dataset.listing); }
  else if (t.dataset.col) { const k = t.dataset.col; S.tableSort = { key: k, asc: S.tableSort.key === k ? !S.tableSort.asc : !['score', 'm2', 'rooms'].includes(k) }; save(); renderTable(); }
  else if (t.dataset.mcol) { const k = t.dataset.mcol; muniSort = { key: k, asc: muniSort.key === k ? !muniSort.asc : k === 'name' || k === 'rent' || k === 'ppm2' || k === 'crime' }; renderMunicipalities(); }
  else if (t.dataset.muni) { const n = t.dataset.muni; muniOpen.has(n) ? muniOpen.delete(n) : muniOpen.add(n); renderMunicipalities(); }
  else if (t.dataset.gmgo !== undefined) { if (t.dataset.gmgo) { e.preventDefault(); S.f.gm = [t.dataset.gmgo]; save(); invalidate(); location.hash = t.getAttribute('href') === '#municipalities' ? '#municipalities' : '#map'; renderAll(); } }
  else if (t.dataset.gmopen) { const m = $('#gm-' + t.dataset.gmopen); const open = m.hidden; $$('.gmmenu').forEach(x => x.hidden = true); if (open) { renderGmMenu(t.dataset.gmopen); m.hidden = false; m.querySelector('input')?.focus(); } }
  else if (t.dataset.gmclear) { S.f.gm = []; save(); invalidate(); renderList(); if (!$('#view-listings').hidden) renderTableBodyOnly(); const id = t.closest('.gmmenu').id.slice(3); renderGmMenu(id); $$('.gmbtn').forEach(b => b.textContent = gmLabel() + ' ▾'); }
  else if (t.dataset.gmdone) { t.closest('.gmmenu').hidden = true; }
  else if (t.dataset.photo) { const l = byId(detailId); const n = (l.photos || []).length || 1; photoIdx = (photoIdx + +t.dataset.photo + n) % n; renderDetail(detailId); }
  else if (t.dataset.photoidx) { photoIdx = +t.dataset.photoidx; renderDetail(detailId); }
  else if (t.classList.contains('close')) { select(null); drawStations(); }
  else if (t.id === 'legendBtn') { S.legend = !S.legend; save(); renderLegend(); }
  else if (t.id === 'clearGroup') { e.preventDefault(); GROUP = null; renderCards(); }
  else if (t.id === 'clearFilters') { e.preventDefault(); S.f = structuredClone(DEFAULTS.f); save(); renderAll(); }
  if (t.id !== 'sortBtn' && !t.dataset.sort) $('#sortMenu').hidden = true;
});
document.addEventListener('keydown', e => { if (e.key === 'Enter' && e.target.dataset?.listing) location.hash = '#listing/' + encodeURIComponent(e.target.dataset.listing); });
// hovering a list card previews it on the map
$('#cards').addEventListener('mouseover', e => { const c = e.target.closest('[data-listing]'); if (c && HOVER?.id !== c.dataset.listing) { LAST_HOVERED = byId(c.dataset.listing); showHover({ kind: 'listing', id: c.dataset.listing }); } });
$('#cards').addEventListener('mouseleave', hideHoverSoon);
$('#sortBtn').addEventListener('click', () => { const m = $('#sortMenu'); m.hidden = !m.hidden; $('#sortBtn').setAttribute('aria-expanded', !m.hidden); });
document.addEventListener('input', e => {
  const t = e.target;
  if (t.dataset?.gmsearch) { renderGmMenu(t.dataset.gmsearch, t.value); const i = $('#gm-' + t.dataset.gmsearch + ' .gmsearch'); i.focus(); i.setSelectionRange(t.value.length, t.value.length); return; }
  if (t.dataset?.gm) {
    const g = t.dataset.gm;
    S.f.gm = t.checked ? [...new Set([...S.f.gm, g])] : S.f.gm.filter(x => x !== g); save(); invalidate();
    $$('.gmbtn').forEach(b => b.textContent = gmLabel() + ' ▾');
    renderList(); if (!$('#view-listings').hidden) renderTableBodyOnly();
    return;
  }
  const k = t.dataset?.f;
  if (!k) return;
  S.f[k] = t.type === 'checkbox' ? t.checked : t.value; save(); invalidate();
  renderList(); renderDetailCard();
  if (!$('#view-listings').hidden) renderTableBodyOnly();
});
let noteTimer;
document.addEventListener('input', e => {
  if (e.target.dataset?.unote) { clearTimeout(noteTimer); const hid = e.target.dataset.unote, v = e.target.value; noteTimer = setTimeout(() => fetch('/api/user/' + encodeURIComponent(hid), { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ note: v }) }).then(r => r.json()).then(u => { for (const l of LISTINGS) if ((l.homeId || l.id) === hid) l.user = u; }), 600); }
});
document.addEventListener('change', e => {
  const t = e.target;
  if (t.id === 'dups') { S.dups = t.checked; save(); loadAreas(); }
  // rejected / not for me also hides the home (you can untick "Hide" again afterwards)
  if (t.dataset?.ustatus) setUser(t.dataset.ustatus, ['rejected', 'notforme'].includes(t.value) ? { status: t.value, hidden: true } : { status: t.value });
  if (t.dataset?.uhidden) setUser(t.dataset.uhidden, { hidden: t.checked });
});
for (const [row, pop] of [['#shopRow', '#shopPop'], ['#parkRow', '#parkPop']]) {
  ['mouseenter', 'focusin'].forEach(ev => $(row).addEventListener(ev, () => { renderPops(); $(pop).hidden = false; }));
  ['mouseleave', 'focusout'].forEach(ev => $(row).addEventListener(ev, () => { $(pop).hidden = true; }));
}
$('#exportCsv').addEventListener('click', exportCsv);
$('#refreshListings').addEventListener('click', () => startJob('listings'));
$('#lookupEnergy').addEventListener('click', () => startJob('energy'));
$('#saveKey').addEventListener('click', async () => {
  const key = $('#epKey').value.trim();
  if (!key) return;
  const r = await fetch('/api/energy-key', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key }) });
  $('#epKey').value = '';
  if (!r.ok) { $('#keyStatus').innerHTML = `<span class="bad">${esc((await r.json()).detail || 'Could not save')}</span>`; return; }
  renderData();
});
$('#epKey').addEventListener('keydown', e => { if (e.key === 'Enter') $('#saveKey').click(); });

$('#refreshListingsTop').addEventListener('click', () => startJob('listings'));
$('#rebuildMap').addEventListener('click', () => { if (confirm('Rebuild map and timetable data? This downloads the timetable and map extracts again and takes a few minutes.')) startJob('map'); });

// drag the listing panel's left edge
const grip = $('#grip');
grip.addEventListener('pointerdown', e => {
  e.preventDefault();
  const x0 = e.clientX, w0 = S.listW;
  document.body.classList.add('dragging');
  const move = ev => { S.listW = Math.max(280, Math.min(900, Math.round(w0 + (x0 - ev.clientX)))); renderControls(); map.invalidateSize(); };
  const up = () => { document.body.classList.remove('dragging'); window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); save(); renderQuick(); };
  window.addEventListener('pointermove', move); window.addEventListener('pointerup', up);
});
grip.addEventListener('keydown', e => {
  if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
    e.preventDefault(); S.listW = Math.max(280, Math.min(900, S.listW + (e.key === 'ArrowLeft' ? 40 : -40)));
    save(); renderControls(); map.invalidateSize();
  }
});
window.addEventListener('hashchange', routeView);

async function boot(reloadOnly = false) {
  const st = await fetch('/api/stations').then(r => r.json());
  STATIONS_DATA = st;
  STATIONS = st.stations; HOURS = st.hours || []; META = st.meta || {};
  BYCODE = Object.fromEntries(STATIONS.map(s => [s.code, s]));
  if (!reloadOnly) { SHOPS = await fetch('/api/supermarkets').then(r => r.json()); drawShops(); drawParks(); }
  await loadAreas();
  if (!reloadOnly) routeView();
  setTimeout(() => map.invalidateSize(), 300);
}
boot();
