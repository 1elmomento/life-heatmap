/*
 * Life Heatmap — browser version.
 *
 * This is the same model as heatmap.py, running client-side so the page can be
 * served as static files (GitHub Pages) with no backend. The constants both
 * versions use live in model_params.json, fetched below — tune a radius there
 * and the Python CLI and this page change together.
 *
 * Nothing here talks to a server about your places: they live in localStorage,
 * the heat grid is computed in this tab, and the shareable PNG is composited on
 * a <canvas>. The only outbound requests are basemap tiles and, when you press
 * "Go" in the search box, a single city lookup.
 */

const STORAGE_KEY = 'life-heatmap:places';
const VIEW_KEY = 'life-heatmap:view';

const BASE_TILE_URL = 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}';
const REFERENCE_TILE_URL = 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}';
const TILE_ATTRIBUTION = 'Esri &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors';

const SHARE_CARD_BG = '#efe0c4';
const SHARE_TITLE_COLOR = '#2b2622';
const SHARE_BODY_COLOR = '#8a5a2b';

// matplotlib's "YlOrRd", as its nine anchor colors. Interpolating linearly
// between them reproduces the continuous colormap render_overlay_png uses.
const YLORRD = [
  [255, 255, 204], [255, 237, 160], [254, 217, 118],
  [254, 178,  76], [253, 141,  60], [252,  78,  42],
  [227,  26,  28], [189,   0,  38], [128,   0,  38],
];

let MODEL = null;   // filled in by boot()

/** crypto.randomUUID() only exists in a secure context (https, or localhost).
 *  Serving these files over a plain LAN address is neither, so fall back. */
function newId() {
  if (globalThis.crypto?.randomUUID) return crypto.randomUUID();
  return `id-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/* ------------------------------------------------------------------ model */

/** Absolute radius in meters and intensity 0-1 for one place. Mirrors
 *  heatmap.py's familiarity(). */
function familiarity(category, durationValue, durationUnit) {
  const params = MODEL.category_params[String(category).trim().toLowerCase()];
  const hoursPer = MODEL.hours_per_unit[String(durationUnit).trim().toLowerCase()];
  if (!params) throw new Error(`unknown category '${category}'`);
  if (!hoursPer) throw new Error(`unknown duration unit '${durationUnit}'`);

  const hours = Math.max(Number(durationValue) || 0, 0) * hoursPer;
  const saturation = 1 - Math.exp(-hours / params.tau_hours);
  return {
    sigma_m: params.r_min + (params.r_max - params.r_min) * saturation,
    intensity: params.i_min + (params.i_max - params.i_min) * saturation,
  };
}

// Equirectangular projection, accurate enough at city scale. lat0/lon0 is the
// centroid of the points themselves rather than a fixed city — the x-scale
// carries cos(lat0), so a fixed origin is only right near that latitude.
function projectMeters(lat, lon, lat0, lon0) {
  return [
    (lon - lon0) * 111320 * Math.cos(lat0 * Math.PI / 180),
    (lat - lat0) * 110540,
  ];
}
function unprojectMeters(x, y, lat0, lon0) {
  return [
    y / 110540 + lat0,
    x / (111320 * Math.cos(lat0 * Math.PI / 180)) + lon0,
  ];
}

/**
 * Sum a Gaussian per place onto a square grid, exactly as build_density_grid
 * does. Each point only touches cells within 5 sigma of it -- past that the
 * Gaussian is below 4e-6, far under alpha_floor, so it cannot affect a pixel --
 * which keeps this fast even with hundreds of places.
 *
 * Returns { grid, bounds: [south, west, north, east], n } with grid[0] as the
 * SOUTH row — flipped to north-up at draw time.
 */
function buildDensityGrid(places) {
  const n = MODEL.grid_resolution;
  const lat0 = places.reduce((s, p) => s + p.lat, 0) / places.length;
  const lon0 = places.reduce((s, p) => s + p.lon, 0) / places.length;

  const pts = places.map((p) => {
    const [x, y] = projectMeters(p.lat, p.lon, lat0, lon0);
    const { sigma_m, intensity } = familiarity(p.category, p.duration_value, p.duration_unit);
    return { x, y, sigma: sigma_m, weight: intensity };
  });

  const pad = Math.max(...pts.map((p) => p.sigma)) * 3.5;
  const xMin = Math.min(...pts.map((p) => p.x)) - pad;
  const xMax = Math.max(...pts.map((p) => p.x)) + pad;
  const yMin = Math.min(...pts.map((p) => p.y)) - pad;
  const yMax = Math.max(...pts.map((p) => p.y)) + pad;

  const dx = (xMax - xMin) / (n - 1);
  const dy = (yMax - yMin) / (n - 1);
  const grid = new Float32Array(n * n);

  for (const p of pts) {
    const reach = p.sigma * 5;
    const i0 = Math.max(0, Math.ceil((p.x - reach - xMin) / dx));
    const i1 = Math.min(n - 1, Math.floor((p.x + reach - xMin) / dx));
    const j0 = Math.max(0, Math.ceil((p.y - reach - yMin) / dy));
    const j1 = Math.min(n - 1, Math.floor((p.y + reach - yMin) / dy));
    const twoSigmaSq = 2 * p.sigma * p.sigma;

    for (let j = j0; j <= j1; j++) {
      const gy = yMin + j * dy;
      const dyy = (gy - p.y) * (gy - p.y);
      const row = j * n;
      for (let i = i0; i <= i1; i++) {
        const gx = xMin + i * dx;
        const dxx = (gx - p.x) * (gx - p.x);
        grid[row + i] += p.weight * Math.exp(-(dxx + dyy) / twoSigmaSq);
      }
    }
  }

  let max = 0;
  for (let k = 0; k < grid.length; k++) if (grid[k] > max) max = grid[k];
  if (max > 0) for (let k = 0; k < grid.length; k++) grid[k] = Math.min(grid[k] / max, 1);

  const [south] = unprojectMeters(0, yMin, lat0, lon0);
  const [north] = unprojectMeters(0, yMax, lat0, lon0);
  const [, west] = unprojectMeters(xMin, 0, lat0, lon0);
  const [, east] = unprojectMeters(xMax, 0, lat0, lon0);

  return { grid, bounds: [south, west, north, east], n };
}

function ylOrRd(t) {
  const s = Math.min(Math.max(t, 0), 1) * (YLORRD.length - 1);
  const i = Math.min(Math.floor(s), YLORRD.length - 2);
  const f = s - i;
  const a = YLORRD[i], b = YLORRD[i + 1];
  return [
    a[0] + (b[0] - a[0]) * f,
    a[1] + (b[1] - a[1]) * f,
    a[2] + (b[2] - a[2]) * f,
  ];
}

/** The heat glow as a canvas, north-up, ready for an ImageOverlay. */
function renderOverlayCanvas({ grid, n }) {
  const canvas = document.createElement('canvas');
  canvas.width = n;
  canvas.height = n;
  const img = canvas.getContext('2d').createImageData(n, n);

  for (let j = 0; j < n; j++) {
    const src = (n - 1 - j) * n;          // flip: row 0 must be the NORTH edge
    const dst = j * n;
    for (let i = 0; i < n; i++) {
      const v = grid[src + i];
      const shaped = Math.pow(v, MODEL.alpha_gamma);
      const [r, g, b] = ylOrRd(shaped);
      const o = (dst + i) * 4;
      img.data[o] = r;
      img.data[o + 1] = g;
      img.data[o + 2] = b;
      img.data[o + 3] = v < MODEL.alpha_floor ? 0 : Math.round(shaped * 255);
    }
  }
  canvas.getContext('2d').putImageData(img, 0, 0);
  return canvas;
}

/* ---------------------------------------------------------------- storage */

function loadPlaces() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}
function savePlaces(places) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(places));
  } catch {
    setStatus(els.generateStatus, "Couldn't save to this browser's storage — export to a file to keep your places.");
  }
}

const CSV_COLUMNS = ['name', 'address', 'lat', 'lon', 'category', 'duration_value', 'duration_unit', 'notes'];

function csvEscape(v) {
  const s = v === undefined || v === null ? '' : String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

/** Same columns as data/locations.csv, so an export drops straight into the
 *  Python CLI: heatmap.py --input that-file.csv --skip-geocode */
function placesToCSV(places) {
  const rows = places.map((p) => CSV_COLUMNS.map((c) => csvEscape(p[c])).join(','));
  return [CSV_COLUMNS.join(','), ...rows].join('\n') + '\n';
}

function parseCSV(text) {
  const rows = [];
  let row = [], field = '', quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') { field += '"'; i++; }
      else if (c === '"') quoted = false;
      else field += c;
    } else if (c === '"') quoted = true;
    else if (c === ',') { row.push(field); field = ''; }
    else if (c === '\n') { row.push(field); rows.push(row); row = []; field = ''; }
    else if (c !== '\r') field += c;
  }
  if (field || row.length) { row.push(field); rows.push(row); }
  if (!rows.length) return [];

  const header = rows[0].map((h) => h.trim());
  return rows.slice(1).filter((r) => r.some((v) => v.trim() !== '')).map((r) => {
    const o = {};
    header.forEach((h, i) => { o[h] = r[i] ?? ''; });
    return o;
  });
}

/** Accepts what we export (JSON or CSV) and drops anything unusable. */
function normalizePlaces(records) {
  const out = [];
  for (const r of records) {
    const lat = Number(r.lat), lon = Number(r.lon);
    const category = String(r.category || '').trim().toLowerCase();
    const unit = String(r.duration_unit || '').trim().toLowerCase();
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
    if (!MODEL.category_params[category] || !MODEL.hours_per_unit[unit]) continue;
    out.push({
      id: newId(),
      name: String(r.name || 'Unnamed place'),
      address: String(r.address || ''),
      lat, lon, category,
      duration_value: Number(r.duration_value) || 0,
      duration_unit: unit,
      notes: String(r.notes || ''),
    });
  }
  return out;
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/* -------------------------------------------------------------- app state */

const els = {};
let map, overlayLayer = null, previewCircle = null, markerLayer;
let places = [];
let pending = null;      // { lat, lng } while the form is open
let selectedCategory = null;

function setStatus(el, msg, timeout = 0) {
  el.textContent = msg;
  el.style.display = msg ? 'block' : 'none';
  if (msg && timeout) setTimeout(() => { if (el.textContent === msg) { el.textContent = ''; el.style.display = 'none'; } }, timeout);
}

function categoryLetter(cat) { return cat.charAt(0).toUpperCase(); }

function formatDuration(value, unit) {
  const v = Number(value);
  const n = Number.isInteger(v) ? v : Math.round(v * 10) / 10;
  return `${n} ${n === 1 ? unit.replace(/s$/, '') : unit}`;
}

/* ------------------------------------------------------------ list + pins */

function renderList() {
  els.list.innerHTML = '';
  if (!places.length) {
    const hint = document.createElement('div');
    hint.id = 'empty-hint';
    hint.textContent = 'No places yet. Click somewhere on the map to add your first one.';
    els.list.appendChild(hint);
  }

  for (const p of places) {
    const color = MODEL.category_color[p.category] || '#8a8a8a';
    const { sigma_m } = familiarity(p.category, p.duration_value, p.duration_unit);

    const card = document.createElement('div');
    card.className = 'loc-card';
    card.title = 'Show on map';

    const badge = document.createElement('div');
    badge.className = 'loc-badge';
    badge.style.background = color;
    badge.textContent = categoryLetter(p.category);

    const info = document.createElement('div');
    info.className = 'loc-info';
    const name = document.createElement('div');
    name.className = 'loc-name';
    name.textContent = p.name;
    const meta = document.createElement('div');
    meta.className = 'loc-meta';
    meta.textContent = `${p.category} · ${formatDuration(p.duration_value, p.duration_unit)} · ${Math.round(sigma_m)} m`;
    info.append(name, meta);

    const del = document.createElement('button');
    del.className = 'loc-del';
    del.textContent = '✕';
    del.title = 'Remove';
    del.addEventListener('click', (e) => {
      e.stopPropagation();
      places = places.filter((q) => q.id !== p.id);
      savePlaces(places);
      renderList();
      renderMarkers();
    });

    card.addEventListener('click', () => map.setView([p.lat, p.lon], Math.max(map.getZoom(), 14)));
    card.append(badge, info, del);
    els.list.appendChild(card);
  }

  els.generateBtn.disabled = places.length === 0;
  els.shareBtn.disabled = places.length === 0;
}

function renderMarkers() {
  markerLayer.clearLayers();
  for (const p of places) {
    const color = MODEL.category_color[p.category] || '#8a8a8a';
    const icon = L.divIcon({
      className: '',
      html: `<div class="cat-marker" style="background:${color}">${categoryLetter(p.category)}</div>`,
      iconSize: [20, 20],
      iconAnchor: [10, 10],
    });
    const { sigma_m } = familiarity(p.category, p.duration_value, p.duration_unit);
    const popup = document.createElement('div');
    const strong = document.createElement('b');
    strong.textContent = p.name;
    popup.append(strong, document.createElement('br'));
    popup.append(`${p.category} — ${formatDuration(p.duration_value, p.duration_unit)} (${Math.round(sigma_m)} m radius)`);
    if (p.notes) { popup.append(document.createElement('br')); popup.append(p.notes); }
    L.marker([p.lat, p.lon], { icon }).bindPopup(popup).addTo(markerLayer);
  }
}

/* ----------------------------------------------------------- the add form */

function openForm(latlng, containerPoint) {
  pending = latlng;
  const panel = els.formPanel;
  panel.style.display = 'block';

  const mapRect = els.map.getBoundingClientRect();
  const w = panel.offsetWidth, h = panel.offsetHeight;
  let left = containerPoint.x + 16;
  let top = containerPoint.y - h / 2;
  if (left + w > mapRect.width - 10) left = containerPoint.x - w - 16;
  panel.style.left = `${Math.max(10, Math.min(left, mapRect.width - w - 10))}px`;
  panel.style.top = `${Math.max(10, Math.min(top, mapRect.height - h - 10))}px`;

  els.formCoords.textContent = `${latlng.lat.toFixed(5)}, ${latlng.lng.toFixed(5)}`;
  els.name.value = '';
  els.notes.value = '';
  els.formError.textContent = '';
  els.name.focus();
  updatePreview();
}

function closeForm() {
  pending = null;
  els.formPanel.style.display = 'none';
  if (previewCircle) { map.removeLayer(previewCircle); previewCircle = null; }
}

/** The dashed circle showing exactly how wide this place will glow. */
function updatePreview() {
  if (!pending || !selectedCategory) return;
  let result;
  try {
    result = familiarity(selectedCategory, els.dur.value, els.unit.value);
  } catch {
    return;
  }
  const color = MODEL.category_color[selectedCategory];
  els.preview.innerHTML = '';
  els.preview.append('Glow radius ');
  const r = document.createElement('b');
  r.textContent = `${Math.round(result.sigma_m)} m`;
  els.preview.append(r, ' · intensity ');
  const it = document.createElement('b');
  it.textContent = result.intensity.toFixed(2);
  els.preview.append(it);

  if (previewCircle) map.removeLayer(previewCircle);
  previewCircle = L.circle([pending.lat, pending.lng], {
    radius: result.sigma_m,
    color,
    weight: 2,
    dashArray: '5,5',
    fillColor: color,
    fillOpacity: 0.12,
  }).addTo(map);
}

function savePending() {
  if (!pending) return;
  if (!selectedCategory) { els.formError.textContent = 'Pick how you know this place.'; return; }
  const name = els.name.value.trim();
  if (!name) { els.formError.textContent = 'Give it a name.'; els.name.focus(); return; }
  const dur = Number(els.dur.value);
  if (!Number.isFinite(dur) || dur < 0) { els.formError.textContent = 'Duration must be a positive number.'; return; }

  places.push({
    id: newId(),
    name,
    address: '',
    lat: pending.lat,
    lon: pending.lng,
    category: selectedCategory,
    duration_value: dur,
    duration_unit: els.unit.value,
    notes: els.notes.value.trim(),
  });
  savePlaces(places);
  closeForm();
  renderList();
  renderMarkers();
}

/* -------------------------------------------------------------- heat draw */

function generateHeatmap() {
  if (!places.length) return;
  const t0 = performance.now();
  const result = buildDensityGrid(places);
  const canvas = renderOverlayCanvas(result);
  const [south, west, north, east] = result.bounds;

  if (overlayLayer) map.removeLayer(overlayLayer);
  overlayLayer = L.imageOverlay(canvas.toDataURL('image/png'), [[south, west], [north, east]], {
    opacity: 0.92,
    interactive: false,
  }).addTo(map);
  map.fitBounds([[south, west], [north, east]]);

  const ms = Math.round(performance.now() - t0);
  setStatus(els.generateStatus, `${places.length} place${places.length === 1 ? '' : 's'} rendered in ${ms} ms.`, 6000);
  return result;
}

/* ------------------------------------------------------------ city search */

async function searchCity(query) {
  const url = `https://nominatim.openstreetmap.org/search?format=json&limit=1&q=${encodeURIComponent(query)}`;
  const res = await fetch(url, { headers: { Accept: 'application/json' } });
  if (!res.ok) throw new Error(`lookup failed (${res.status})`);
  const hits = await res.json();
  if (!hits.length) throw new Error('no match');
  return hits[0];
}

/* ------------------------------------------------------- shareable export */

function degToTile(lat, lon, zoom) {
  const latRad = lat * Math.PI / 180;
  const n = 2 ** zoom;
  return [
    (lon + 180) / 360 * n,
    (1 - Math.log(Math.tan(latRad) + 1 / Math.cos(latRad)) / Math.PI) / 2 * n,
  ];
}

function chooseTileZoom(bounds, maxTiles = 180, maxZoom = 16) {
  const [south, west, north, east] = bounds;
  for (let zoom = maxZoom; zoom > 0; zoom--) {
    const [x0, y0] = degToTile(north, west, zoom);
    const [x1, y1] = degToTile(south, east, zoom);
    if ((Math.abs(x1 - x0) + 1) * (Math.abs(y1 - y0) + 1) <= maxTiles) return zoom;
  }
  return 1;
}

function loadTile(url) {
  return new Promise((resolve) => {
    const img = new Image();
    img.crossOrigin = 'anonymous';   // Esri sends Access-Control-Allow-Origin: *,
    img.onload = () => resolve(img); // so the canvas stays untainted and toBlob works
    img.onerror = () => resolve(null);
    img.src = url;
  });
}

/** Stitch the tiles covering `bounds` and crop precisely to it — the browser's
 *  equivalent of _fetch_tile_mosaic. The browser's HTTP cache stands in for
 *  the Python version's .tile_cache directory. */
async function fetchTileMosaic(bounds, tileUrl, zoom, onProgress, tileSize = 256) {
  const [south, west, north, east] = bounds;
  const [x0f, y0f] = degToTile(north, west, zoom);
  const [x1f, y1f] = degToTile(south, east, zoom);
  const x0 = Math.floor(Math.min(x0f, x1f)), x1 = Math.floor(Math.max(x0f, x1f));
  const y0 = Math.floor(Math.min(y0f, y1f)), y1 = Math.floor(Math.max(y0f, y1f));

  const canvas = document.createElement('canvas');
  canvas.width = (x1 - x0 + 1) * tileSize;
  canvas.height = (y1 - y0 + 1) * tileSize;
  const ctx = canvas.getContext('2d');

  const coords = [];
  for (let xt = x0; xt <= x1; xt++) for (let yt = y0; yt <= y1; yt++) coords.push([xt, yt]);

  let done = 0;
  const CONCURRENCY = 12;
  for (let i = 0; i < coords.length; i += CONCURRENCY) {
    const batch = coords.slice(i, i + CONCURRENCY);
    const imgs = await Promise.all(batch.map(([xt, yt]) =>
      loadTile(tileUrl.replace('{z}', zoom).replace('{x}', xt).replace('{y}', yt))));
    batch.forEach(([xt, yt], k) => {
      if (imgs[k]) ctx.drawImage(imgs[k], (xt - x0) * tileSize, (yt - y0) * tileSize);
      done++;
    });
    if (onProgress) onProgress(done, coords.length);
  }

  // crop to the exact extent
  const cropX = (x0f - x0) * tileSize, cropY = (y0f - y0) * tileSize;
  const cropW = (x1f - x0) * tileSize - cropX, cropH = (y1f - y0) * tileSize - cropY;
  const out = document.createElement('canvas');
  out.width = Math.max(1, Math.round(cropW));
  out.height = Math.max(1, Math.round(cropH));
  out.getContext('2d').drawImage(canvas, cropX, cropY, cropW, cropH, 0, 0, out.width, out.height);
  return out;
}

function wrapText(ctx, text, maxWidth) {
  const lines = [];
  let current = '';
  for (const word of text.split(/\s+/)) {
    const trial = current ? `${current} ${word}` : word;
    if (ctx.measureText(trial).width <= maxWidth) current = trial;
    else { if (current) lines.push(current); current = word; }
  }
  if (current) lines.push(current);
  return lines;
}

/** One self-contained high-resolution PNG: basemap + glow + caption card.
 *  The layout mirrors render_share_image() in heatmap.py. */
async function renderShareImage(title, description, onProgress) {
  const result = buildDensityGrid(places);
  const { bounds } = result;
  const zoom = chooseTileZoom(bounds);

  const base = await fetchTileMosaic(bounds, BASE_TILE_URL, zoom, (d, t) => onProgress(`Fetching map tiles… ${d}/${t}`));
  const reference = await fetchTileMosaic(bounds, REFERENCE_TILE_URL, zoom, (d, t) => onProgress(`Fetching labels… ${d}/${t}`));
  onProgress('Compositing…');

  let mapW = base.width, mapH = base.height;
  const scale = Math.max(1, 1600 / mapW);      // upscale small extents for social media
  mapW = Math.round(mapW * scale);
  mapH = Math.round(mapH * scale);

  const overlay = renderOverlayCanvas(result);

  // --- caption metrics (font sizes are fractions of the map width, as in Python)
  const innerPad = Math.round(mapW * 0.045);
  const gap = Math.round(mapW * 0.014);
  const measure = document.createElement('canvas').getContext('2d');
  const maxTextW = mapW - 2 * innerPad;

  let titleSize = Math.round(mapW * 0.034);
  measure.font = `bold ${titleSize}px Georgia, "Times New Roman", serif`;
  while (titleSize > 14 && measure.measureText(title).width > maxTextW) {
    titleSize -= 2;
    measure.font = `bold ${titleSize}px Georgia, "Times New Roman", serif`;
  }
  const bodySize = Math.round(mapW * 0.022);
  measure.font = `${bodySize}px Georgia, "Times New Roman", serif`;
  const descLines = wrapText(measure, description, maxTextW).slice(0, 2);

  const titleH = Math.round(titleSize * 1.3);
  const lineH = Math.round(bodySize * 1.4);
  const cardH = titleH + gap + descLines.length * lineH + 2 * innerPad;
  const overlap = Math.round(cardH * 0.3);

  const canvas = document.createElement('canvas');
  canvas.width = mapW;
  canvas.height = mapH + cardH - overlap;
  const ctx = canvas.getContext('2d');
  ctx.imageSmoothingQuality = 'high';

  ctx.fillStyle = SHARE_CARD_BG;
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(base, 0, 0, mapW, mapH);
  ctx.drawImage(reference, 0, 0, mapW, mapH);
  ctx.drawImage(overlay, 0, 0, mapW, mapH);

  // Esri's tiles require the attribution to stay visible; it sits top-right,
  // away from the caption card so the two never fight for space.
  const attribution = 'Esri, HERE, Garmin, OpenStreetMap contributors';
  const attrSize = Math.round(mapW * 0.011);
  ctx.font = `${attrSize}px -apple-system, "Segoe UI", Roboto, sans-serif`;
  const attrPad = Math.round(mapW * 0.01);
  const attrBarH = Math.round(mapW * 0.026);
  const attrW = ctx.measureText(attribution).width;
  ctx.fillStyle = 'rgba(255,255,255,0.67)';
  ctx.fillRect(mapW - attrW - 2 * attrPad, 0, attrW + 2 * attrPad, attrBarH);
  ctx.fillStyle = '#4a463f';
  ctx.textBaseline = 'middle';
  ctx.fillText(attribution, mapW - attrW - attrPad, attrBarH / 2);

  // caption card, with a soft shadow along its top edge
  const cardTop = mapH - overlap;
  ctx.save();
  ctx.shadowColor = 'rgba(0,0,0,0.35)';
  ctx.shadowBlur = Math.round(mapW * 0.03);
  ctx.shadowOffsetY = Math.round(mapW * -0.004);
  ctx.fillStyle = SHARE_CARD_BG;
  ctx.fillRect(0, cardTop, mapW, cardH);
  ctx.restore();
  ctx.fillStyle = SHARE_CARD_BG;
  ctx.fillRect(0, cardTop, mapW, cardH);

  ctx.textBaseline = 'top';
  ctx.fillStyle = SHARE_TITLE_COLOR;
  ctx.font = `bold ${titleSize}px Georgia, "Times New Roman", serif`;
  let y = cardTop + innerPad;
  ctx.fillText(title, innerPad, y);
  y += titleH + gap;
  ctx.fillStyle = SHARE_BODY_COLOR;
  ctx.font = `${bodySize}px Georgia, "Times New Roman", serif`;
  for (const line of descLines) {
    ctx.fillText(line, innerPad, y);
    y += lineH;
  }

  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error('could not encode the image'))), 'image/png');
  });
}

/* -------------------------------------------------------------------- app */

function wireUp() {
  // category buttons, built from the shared params so adding a category to
  // model_params.json is enough to make it appear here
  for (const [cat, color] of Object.entries(MODEL.category_color)) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'cat-btn';
    btn.style.setProperty('--cat-color', color);
    const swatch = document.createElement('span');
    swatch.className = 'swatch';
    const label = document.createElement('span');
    label.textContent = cat;
    btn.append(swatch, label);
    btn.addEventListener('click', () => {
      selectedCategory = cat;
      els.catRow.querySelectorAll('.cat-btn').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      els.formError.textContent = '';
      updatePreview();
    });
    els.catRow.appendChild(btn);
  }

  map.on('click', (e) => {
    if (els.formPanel.style.display === 'block') { closeForm(); return; }
    openForm(e.latlng, e.containerPoint);
  });

  els.dur.addEventListener('input', updatePreview);
  els.unit.addEventListener('change', updatePreview);
  els.save.addEventListener('click', savePending);
  els.cancel.addEventListener('click', closeForm);
  els.name.addEventListener('keydown', (e) => { if (e.key === 'Enter') savePending(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeForm(); });

  els.generateBtn.addEventListener('click', generateHeatmap);

  // --- search
  els.searchForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const q = els.searchInput.value.trim();
    if (!q) return;
    els.searchBtn.disabled = true;
    setStatus(els.searchStatus, 'Searching…');
    try {
      const hit = await searchCity(q);
      map.setView([Number(hit.lat), Number(hit.lon)], 12);
      setStatus(els.searchStatus, hit.display_name, 4000);
      els.searchInput.select();
    } catch (err) {
      setStatus(els.searchStatus, err.message === 'no match'
        ? `No place found for "${q}".`
        : "Couldn't reach the search service — pan the map instead.", 6000);
    } finally {
      els.searchBtn.disabled = false;
    }
  });

  // --- export / import / clear
  els.exportBtn.addEventListener('click', () => {
    if (!places.length) return;
    downloadBlob(new Blob([placesToCSV(places)], { type: 'text/csv' }), 'life-heatmap-places.csv');
    setStatus(els.generateStatus, 'Saved as CSV — the same format the Python CLI reads.', 6000);
  });

  els.importBtn.addEventListener('click', () => els.importFile.click());
  els.importFile.addEventListener('change', async () => {
    const file = els.importFile.files[0];
    if (!file) return;
    try {
      const text = await file.text();
      const records = file.name.toLowerCase().endsWith('.json') ? JSON.parse(text) : parseCSV(text);
      const imported = normalizePlaces(Array.isArray(records) ? records : []);
      if (!imported.length) { setStatus(els.generateStatus, 'Nothing usable in that file.', 6000); return; }
      places = places.concat(imported);
      savePlaces(places);
      renderList();
      renderMarkers();
      setStatus(els.generateStatus, `Imported ${imported.length} place${imported.length === 1 ? '' : 's'}.`, 6000);
    } catch {
      setStatus(els.generateStatus, "Couldn't read that file — expected the CSV or JSON this page exports.", 6000);
    } finally {
      els.importFile.value = '';
    }
  });

  els.clearBtn.addEventListener('click', () => {
    if (!places.length) return;
    if (!confirm(`Remove all ${places.length} places? Export first if you want to keep them.`)) return;
    places = [];
    savePlaces(places);
    if (overlayLayer) { map.removeLayer(overlayLayer); overlayLayer = null; }
    renderList();
    renderMarkers();
  });

  // --- shareable image
  els.shareBtn.addEventListener('click', () => {
    els.shareTitle.value = els.shareTitle.value || 'My Life Familiarity Map';
    els.shareDesc.value = els.shareDesc.value
      || `${places.length} places I've lived, worked, traveled to, and visited.`;
    els.shareDialog.showModal();
  });

  els.shareDialog.addEventListener('close', async () => {
    if (els.shareDialog.returnValue !== 'ok') return;
    const btn = els.shareBtn;
    const label = btn.querySelector('.label');
    btn.disabled = true;
    btn.classList.add('loading');
    label.textContent = 'Rendering…';
    setStatus(els.shareStatus, '');
    try {
      const blob = await renderShareImage(
        els.shareTitle.value.trim() || 'My Life Familiarity Map',
        els.shareDesc.value.trim(),
        (msg) => { label.textContent = msg; },
      );
      downloadBlob(blob, 'life-heatmap-share.png');
    } catch (err) {
      setStatus(els.shareStatus, `Could not render the image (${err.message}).`, 8000);
    } finally {
      btn.disabled = false;
      btn.classList.remove('loading');
      label.textContent = 'Download shareable image';
    }
  });

  // remember where the map was left, so you only pan to your city once
  map.on('moveend', () => {
    try {
      const c = map.getCenter();
      localStorage.setItem(VIEW_KEY, JSON.stringify({ lat: c.lat, lon: c.lng, zoom: map.getZoom() }));
    } catch { /* private mode: not worth surfacing */ }
  });
}

async function boot() {
  Object.assign(els, {
    map: document.getElementById('map'),
    list: document.getElementById('loc-list'),
    generateBtn: document.getElementById('generate-btn'),
    generateStatus: document.getElementById('generate-status'),
    exportBtn: document.getElementById('export-btn'),
    importBtn: document.getElementById('import-btn'),
    importFile: document.getElementById('import-file'),
    clearBtn: document.getElementById('clear-btn'),
    formPanel: document.getElementById('form-panel'),
    formCoords: document.getElementById('form-coords'),
    catRow: document.getElementById('cat-row'),
    name: document.getElementById('f-name'),
    dur: document.getElementById('f-dur'),
    unit: document.getElementById('f-unit'),
    notes: document.getElementById('f-notes'),
    preview: document.getElementById('preview-readout'),
    formError: document.getElementById('form-error'),
    save: document.getElementById('save-btn'),
    cancel: document.getElementById('cancel-btn'),
    searchForm: document.getElementById('search-form'),
    searchInput: document.getElementById('search-input'),
    searchBtn: document.getElementById('search-btn'),
    searchStatus: document.getElementById('search-status'),
    shareBtn: document.getElementById('share-btn'),
    shareStatus: document.getElementById('share-status'),
    shareDialog: document.getElementById('share-dialog'),
    shareTitle: document.getElementById('share-title'),
    shareDesc: document.getElementById('share-desc'),
  });

  try {
    const res = await fetch('model_params.json');
    if (!res.ok) throw new Error(res.status);
    MODEL = await res.json();
  } catch {
    document.body.innerHTML = '<p style="padding:40px;font:15px/1.5 -apple-system,sans-serif">'
      + 'Could not load <code>model_params.json</code>. If you opened this file directly from disk, '
      + 'browsers block that read — serve the folder instead, e.g. <code>python3 -m http.server</code> '
      + 'from inside <code>docs/</code>.</p>';
    return;
  }

  let view = { lat: MODEL.default_center[0], lon: MODEL.default_center[1], zoom: MODEL.default_zoom };
  try {
    const saved = JSON.parse(localStorage.getItem(VIEW_KEY) || 'null');
    if (saved && Number.isFinite(saved.lat) && Number.isFinite(saved.lon)) view = saved;
  } catch { /* fall back to the default view */ }

  map = L.map(els.map.querySelector('#map-canvas'), { zoomControl: true })
    .setView([view.lat, view.lon], view.zoom);
  // crossOrigin matters beyond the live map: the shareable-image export re-fetches
  // these same tiles with crossOrigin='anonymous' so the canvas stays untainted and
  // toBlob() works. If Leaflet cached them under a different CORS mode, that second
  // fetch can be served the cached opaque response and fail -- so both use the same.
  L.tileLayer(BASE_TILE_URL, { attribution: TILE_ATTRIBUTION, maxZoom: 16, crossOrigin: 'anonymous' }).addTo(map);
  L.tileLayer(REFERENCE_TILE_URL, { maxZoom: 16, crossOrigin: 'anonymous' }).addTo(map);
  markerLayer = L.layerGroup().addTo(map);

  places = normalizePlaces(loadPlaces());
  selectedCategory = Object.keys(MODEL.category_color)[0];

  wireUp();
  els.catRow.querySelector('.cat-btn').classList.add('active');
  renderList();
  renderMarkers();
}

boot();
