/* New Roads Planner - web front end. Talks to webapp.py's JSON API on the same origin. */
"use strict";

const MODES = {
  ride: { distance: 40, min: 5, max: 200, step: 1, plural: "rides", kmMarks: [5, 10, 20, 50, 100] },
  run: { distance: 10, min: 2, max: 60, step: 0.5, plural: "runs", kmMarks: [1, 2, 5, 10, 20] },
};
const DEFAULT_OPTIONS = {
  alternatives: 3, avoidMain: 1, eagerness: 0.6, effort: "normal", countAll: false,
  bikeOn: true, bikeStrength: 0.5, // favour a local bike network where one is known (rides only)
};
const STORE_KEY = "new-roads-planner:v1";
const POLL_MS = 1000;
const STAGES = { // what the planner is doing, from its progress reports
  network: "Loading the road network…",
  waiting: "Waiting for another download of this area to finish…",
  build: "Building the road network (a one-off for this area)…",
  coverage: "Working out which roads you've done…",
  search: "Searching for the route with the most new road…",
  bikenet: "Getting the bike network ready (a one-off for this area)…",
  elevation: "Adding elevation (a one-off download for this area)…",
};

const $ = (sel) => document.querySelector(sel);
const rootStyle = getComputedStyle(document.documentElement);
const color = (name) => rootStyle.getPropertyValue(name).trim();
const nf0 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
const nf1 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
const km = (v) => nf1.format(v);
const dateFormat = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "numeric" });
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

const state = {
  mode: "ride",
  routeType: "loop",            // "loop" | "oneway"
  distance: { ride: MODES.ride.distance, run: MODES.run.distance },
  start: null,                  // { lat, lon, label, source: "usual" | "search" | "map" | "gps" }
  end: null,
  options: { ...DEFAULT_OPTIONS },
  show: { untraveled: true, traveled: false, areas: true, key: true, aerial: false, bikenet: false },
  config: null,                 // from /api/config
  pick: null,                   // "start" | "end" while the next map click sets it
  job: null,                    // id of the search in progress
  lastJob: null,                // id of the latest search, to show its routes again after a reload
  result: null,                 // the routes on show: a finished search, or a saved route
  context: null,                // roads around the routes, for the background layers
  selected: 0,                  // which route is highlighted
  saved: [],                    // saved route summaries
  renaming: null,               // id of the saved route whose name is being edited
};

// ---------- small helpers ----------

function el(tag, props = {}) {
  return Object.assign(document.createElement(tag), props);
}

function icon(name) {
  return `<svg class="i" aria-hidden="true"><use href="#i-${name}"/></svg>`;
}

function miniButton(text, iconName, onClick, title) {
  const b = el("button", { type: "button", className: "mini" });
  if (title) b.title = title;
  b.innerHTML = icon(iconName);
  b.append(document.createTextNode(text));
  b.addEventListener("click", onClick);
  return b;
}

function gpxLink(href) {
  const a = el("a", { className: "mini", href, title: "Download as GPX, for a bike computer, watch or route app" });
  a.setAttribute("download", "");
  a.innerHTML = `${icon("download")}GPX`;
  return a;
}

function duration(seconds) {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${Math.max(5, Math.round(s / 5) * 5)} seconds`;
  const m = Math.round(s / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
}

function distanceKm(a, b) {
  const rad = Math.PI / 180;
  const h = Math.sin((b.lat - a.lat) * rad / 2) ** 2 +
    Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin((b.lon - a.lon) * rad / 2) ** 2;
  return 12742 * Math.asin(Math.min(1, Math.sqrt(h)));
}

// ---------- saving settings between visits (this browser only) ----------

function save() {
  const { mode, routeType, distance, start, end, options, show, lastJob } = state;
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify({ mode, routeType, distance, start, end, options, show, lastJob }));
  } catch (_) { /* storage unavailable: settings just aren't remembered */ }
}

function restore() {
  let s = null;
  try { s = JSON.parse(localStorage.getItem(STORE_KEY) || "null"); } catch (_) { return; }
  if (!s || typeof s !== "object") return;
  if (s.mode in MODES) state.mode = s.mode;
  if (s.routeType === "loop" || s.routeType === "oneway") state.routeType = s.routeType;
  for (const m in MODES) {
    const d = Number(s.distance && s.distance[m]);
    if (d >= 0.5 && d <= 400) state.distance[m] = d;
  }
  if (isPlace(s.start)) state.start = s.start;
  if (isPlace(s.end)) state.end = s.end;
  const o = s.options || {};
  if ([1, 2, 3].includes(o.alternatives)) state.options.alternatives = o.alternatives;
  if ([0, 1, 2].includes(o.avoidMain)) state.options.avoidMain = o.avoidMain;
  if (o.eagerness >= 0 && o.eagerness <= 0.95) state.options.eagerness = o.eagerness;
  if (["quick", "normal", "thorough"].includes(o.effort)) state.options.effort = o.effort;
  state.options.countAll = o.countAll === true;
  if (typeof o.bikeOn === "boolean") state.options.bikeOn = o.bikeOn;
  if (o.bikeStrength >= 0.1 && o.bikeStrength <= 1) state.options.bikeStrength = o.bikeStrength;
  for (const k in state.show) if (typeof (s.show || {})[k] === "boolean") state.show[k] = s.show[k];
  if (typeof s.lastJob === "string" && /^[0-9a-f]{12}$/.test(s.lastJob)) state.lastJob = s.lastJob;
}

function isPlace(p) {
  return Boolean(p) && Number.isFinite(p.lat) && Number.isFinite(p.lon) && Math.abs(p.lat) <= 90 && Math.abs(p.lon) <= 180;
}

// ---------- talking to webapp.py ----------

async function api(path, { method = "GET", body } = {}) {
  let res;
  try {
    res = await fetch(path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (_) {
    throw new Error("Can't reach the planner. Is webapp.py still running in your terminal?");
  }
  let data = null;
  try { data = await res.json(); } catch (_) { /* not JSON */ }
  if (!res.ok) throw new Error((data && data.error) || `The planner answered ${res.status}.`);
  return data;
}

// ---------- the map ----------

const map = L.map("map", { zoomControl: false, worldCopyJump: true }).setView([20, 0], 2);
L.control.zoom({ position: "topright" }).addTo(map);
L.control.scale({ imperial: false, position: "bottomright" }).addTo(map);
const bases = {
  map: L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, className: "base-osm",
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }),
  aerial: L.layerGroup([
    L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
      maxZoom: 19, maxNativeZoom: 18,
      attribution: "Imagery &copy; Esri, Maxar, Earthstar Geographics and the GIS User Community",
    }),
    L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}", {
      maxZoom: 19, maxNativeZoom: 18, opacity: 0.85,
    }),
  ]),
};

// draw order: downloaded areas < nearby roads < other suggestions < chosen route < arrows < pins
for (const [pane, z] of [["areas", 350], ["context", 380], ["bikenet", 385], ["alts", 390], ["marks", 450]]) {
  map.createPane(pane).style.zIndex = z;
}
map.getPane("marks").style.pointerEvents = "none";
const contextRenderer = L.canvas({ pane: "context", padding: 0.3 }); // thousands of lines: canvas is much faster
const bikenetRenderer = L.canvas({ pane: "bikenet", padding: 0.3 });
const layers = {
  areas: L.layerGroup(),
  bikenet: L.layerGroup(),
  untraveled: L.layerGroup(),
  traveled: L.layerGroup(),
  alts: L.layerGroup().addTo(map),
  route: L.layerGroup().addTo(map),
  marks: L.layerGroup().addTo(map),
  places: L.layerGroup().addTo(map),
  hover: L.layerGroup().addTo(map),
};
const markers = {};

function setBase(kind) {
  state.show.aerial = kind === "aerial";
  for (const [name, layer] of Object.entries(bases)) {
    if (name === kind) layer.addTo(map); else layer.remove();
  }
  document.documentElement.classList.toggle("aerial", state.show.aerial);
  document.querySelectorAll("[data-base]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.base === kind)));
  restyle();
  save();
}

// colours depend on the theme and the base map, so redraw everything that uses them
function restyle() {
  drawBikeNetwork();
  renderAreas();
  drawRoutes();
  drawContext();
  renderProfile();
}

function applyLayerToggles() {
  for (const k of ["untraveled", "traveled", "areas"]) {
    $(`#show-${k}`).checked = state.show[k];
    if (state.show[k]) layers[k].addTo(map); else layers[k].remove();
  }
}

function renderAreas() {
  layers.areas.clearLayers();
  for (const a of (state.config && state.config.areas) || []) {
    if (a.mode !== state.mode) continue;
    L.circle([a.lat, a.lon], {
      radius: a.radius_m, pane: "areas", interactive: false,
      color: color("--area"), weight: 1.5, opacity: 0.6, dashArray: "6 8", fillOpacity: 0.025,
    }).addTo(layers.areas);
  }
}

function fitMap(bounds, maxZoom = 16) {
  const key = $("#legend");
  const roomy = $("#map").clientWidth > 640 && key.open && key.offsetParent !== null;
  map.fitBounds(bounds, { paddingTopLeft: [roomy ? key.offsetWidth + 28 : 40, 56], paddingBottomRight: [40, 40], maxZoom });
}

map.on("click", (e) => {
  const ll = e.latlng.wrap();
  const place = { lat: ll.lat, lon: ll.lng, label: "Dropped pin", source: "map" };
  if (state.pick) {
    const which = state.pick;
    setPick(null);
    setPlace(which, place, { pan: false });
    return;
  }
  const box = el("div", { className: "popup-actions" });
  const add = (text, which) => {
    const b = el("button", { type: "button", textContent: text });
    b.addEventListener("click", () => { map.closePopup(); setPlace(which, place, { pan: false }); });
    box.append(b);
  };
  add("Start here", "start");
  if (state.routeType === "oneway") add("Finish here", "end");
  L.popup({ closeButton: false, offset: [0, -2] }).setLatLng(e.latlng).setContent(box).openOn(map);
});
map.on("zoomend", () => drawMarks());

// ---------- start and finish ----------

function pinIcon(which) {
  return L.divIcon({ className: `pin pin-${which}`, html: which === "start" ? "S" : "F", iconSize: [30, 30], iconAnchor: [15, 15] });
}

function renderPlaces() {
  for (const which of ["start", "end"]) {
    const p = state[which];
    const visible = p && (which === "start" || state.routeType === "oneway");
    if (!visible) {
      if (markers[which]) { layers.places.removeLayer(markers[which]); delete markers[which]; }
    } else if (markers[which]) {
      markers[which].setLatLng([p.lat, p.lon]);
    } else {
      markers[which] = L.marker([p.lat, p.lon], {
        icon: pinIcon(which), draggable: true, autoPan: true, zIndexOffset: which === "start" ? 100 : 0,
        title: which === "start" ? "Start (drag to move)" : "Finish (drag to move)",
      }).on("dragend", (e) => {
        const ll = e.target.getLatLng().wrap();
        setPlace(which, { lat: ll.lat, lon: ll.lng, label: "Dropped pin", source: "map" }, { pan: false });
      }).addTo(layers.places);
    }
  }
  placeLabel("start", "Not set. Search, use a shortcut, or click the map.");
  placeLabel("end", "Not set. Search, or click the map.");
}

function placeLabel(which, emptyText) {
  const label = $(`#${which}-label`);
  const p = state[which];
  if (!p) { label.textContent = emptyText; return; }
  label.replaceChildren(el("strong", { textContent: p.label || "Dropped pin" }), ` · ${p.lat.toFixed(5)}, ${p.lon.toFixed(5)}`);
}

function setPlace(which, place, { pan = true } = {}) {
  state[which] = { lat: Number(place.lat), lon: Number(place.lon), label: place.label || "Dropped pin", source: place.source || "map" };
  renderPlaces();
  updateHints();
  markStale();
  save();
  if (pan) map.panTo([state[which].lat, state[which].lon]);
}

function setPick(which) {
  state.pick = which;
  document.body.classList.toggle("picking", Boolean(which));
  $("#pick-banner").hidden = !which;
  $("#pick-banner-text").textContent = which === "end" ? "Click the map to set the finish" : "Click the map to set the start";
  document.querySelectorAll("[data-pick]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.pick === which)));
}

function usualStart(mode = state.mode) {
  const u = state.config && state.config.usual_start && state.config.usual_start[mode];
  return u ? { lat: u.lat, lon: u.lon, label: `Your usual ${mode} start`, source: "usual" } : null;
}

function useUsualStart() {
  const p = usualStart();
  if (!p) {
    showError(`There are no ${MODES[state.mode].plural} in your Strava history to find a usual start from.`);
    return;
  }
  setPlace("start", p, { pan: false });
  map.setView([p.lat, p.lon], Math.max(map.getZoom(), 13));
}

function locate() {
  if (!navigator.geolocation) { showError("This browser can't share your location."); return; }
  const btn = $("#locate");
  btn.disabled = true;
  navigator.geolocation.getCurrentPosition((pos) => {
    btn.disabled = false;
    const p = { lat: pos.coords.latitude, lon: pos.coords.longitude, label: "Your location", source: "gps" };
    setPlace("start", p, { pan: false });
    map.setView([p.lat, p.lon], Math.max(map.getZoom(), 14));
  }, (err) => {
    btn.disabled = false;
    showError(err.code === err.PERMISSION_DENIED
      ? "Location access is blocked. Allow it for this page, or search instead."
      : "Couldn't get your location. Search or click the map instead.");
  }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 });
}

async function search(which) {
  const input = $(`#${which}-q`);
  const list = $(`#${which}-results`);
  const q = input.value.trim();
  if (!q) { input.focus(); return; }
  const params = new URLSearchParams({ q });
  if (map.getZoom() >= 8) { // prefer places in view, without ruling out others
    const b = map.getBounds();
    params.set("viewbox", [b.getWest(), b.getNorth(), b.getEast(), b.getSouth()].map((v) => v.toFixed(4)).join(","));
  }
  showList(list, [{ text: "Searching…" }]);
  try {
    const { results } = await api(`/api/geocode?${params}`);
    if (!results.length) { showList(list, [{ text: "Nothing found. Try adding a suburb or town." }]); return; }
    showList(list, results.map((r) => ({
      text: r.name,
      pick: () => {
        list.hidden = true;
        input.value = "";
        setPlace(which, { lat: r.lat, lon: r.lon, label: shortName(r.name), source: "search" }, { pan: false });
        map.setView([r.lat, r.lon], Math.max(map.getZoom(), 14));
      },
    })));
  } catch (err) {
    showList(list, [{ text: err.message, error: true }]);
  }
}

// "Town Hall, 12, High Street, Springfield, ..." -> "Town Hall, High Street"
function shortName(name) {
  const parts = name.split(",").map((s) => s.trim()).filter((s) => s && !/^\d+[a-z]?$/i.test(s));
  return parts.slice(0, 2).join(", ") || name;
}

function showList(list, items) {
  list.replaceChildren(...items.map((it) => {
    const li = el("li");
    if (it.pick) {
      const b = el("button", { type: "button", textContent: it.text });
      b.addEventListener("click", it.pick);
      li.append(b);
    } else {
      li.className = it.error ? "error" : "muted";
      li.textContent = it.text;
    }
    return li;
  }));
  list.hidden = false;
}

// ---------- activity, route type and distance ----------

function setMode(mode) {
  state.mode = mode;
  setDistanceRange();
  showDistance();
  if (state.start && state.start.source === "usual") { // "usual start" follows the activity
    const p = usualStart(mode);
    if (p) { state.start = p; renderPlaces(); }
  }
  renderAreas();
  updateHints();
  markStale();
  save();
}

function setRouteType(type) {
  state.routeType = type;
  $("#end-field").hidden = type !== "oneway";
  if (type !== "oneway" && state.pick === "end") setPick(null);
  renderPlaces();
  updateHints();
  markStale();
  save();
}

function setDistanceRange() {
  const m = MODES[state.mode];
  const range = $("#distance-range");
  range.min = String(m.min);
  range.max = String(m.max);
  range.step = String(m.step);
}

function setDistance(value) {
  const v = Math.round(Math.min(400, Math.max(0.5, Number(value))) * 2) / 2;
  if (Number.isFinite(v)) {
    state.distance[state.mode] = v;
    updateHints();
    markStale();
    save();
  }
  showDistance();
}

function showDistance() {
  const m = MODES[state.mode];
  const d = state.distance[state.mode];
  $("#distance-range").value = String(clamp(d, m.min, m.max));
  $("#distance").value = String(d);
}

function showEagerness() {
  const v = state.options.eagerness;
  $("#eagerness").value = String(v);
  $("#eagerness-value").textContent = `${v < 0.3 ? "a little" : v < 0.75 ? "balanced" : "keenly"} · ${v.toFixed(2)}`;
}

function updateHints() {
  const d = state.distance[state.mode];
  const hint = $("#distance-hint");
  hint.className = "hint";
  if (state.routeType === "loop") {
    hint.textContent = `A loop of about ${km(d)} km, back to the start.`;
  } else if (state.start && state.end) {
    const gap = distanceKm(state.start, state.end);
    if (gap * 1.05 >= d) {
      hint.classList.add("warn");
      hint.textContent = `The finish is ${km(gap)} km away in a straight line, so a ${km(d)} km route can't reach it. Try ${Math.ceil(gap * 1.3)} km or more.`;
    } else if (gap * 1.3 > d) {
      hint.classList.add("warn");
      hint.textContent = `The finish is ${km(gap)} km away in a straight line, which leaves little room for detours. ${Math.ceil(gap * 1.5)} km or more finds more new road.`;
    } else {
      hint.textContent = `About ${km(d)} km, finishing ${km(gap)} km from the start as the crow flies.`;
    }
  } else {
    hint.textContent = `About ${km(d)} km from start to finish.`;
  }
  scheduleEstimate();
  updateBikeField();
  updateGenerate();
}

// ---------- the local bike network ----------

function covers(net, p) {
  return Boolean(p) && p.lat >= net.bbox[1] && p.lat <= net.bbox[3] && p.lon >= net.bbox[0] && p.lon <= net.bbox[2];
}

// the bike network a ride from here would use (the planner uses the one covering the start or finish)
function routeNetwork() {
  if (state.mode !== "ride" || !state.config) return null;
  const nets = state.config.bike_networks || [];
  return nets.find((n) => covers(n, state.start) || (state.routeType === "oneway" && covers(n, state.end))) || null;
}

function updateBikeField() {
  const net = routeNetwork();
  $("#bike-field").hidden = !net;
  if (!net) return;
  const on = state.options.bikeOn;
  $("#bike-on").checked = on;
  $("#bike-on-label").textContent = `Prefer ${net.short}`;
  $("#bike-strength-row").classList.toggle("off", !on);
  $("#bike-strength").disabled = !on;
  showBikeStrength();
  $("#bike-hint").textContent = on
    ? "Routes keep to the council's cycle routes and the RiverWalk where they can, and new road on them counts " +
      "for more. It's a planning map, so a few routes may be planned rather than built."
    : "Off: routes go wherever the most new road is, whatever the road.";
}

function showBikeStrength() {
  const v = state.options.bikeStrength;
  $("#bike-strength").value = String(v);
  $("#bike-strength-value").textContent = `${v < 0.35 ? "a little" : v < 0.75 ? "balanced" : "strongly"} · ${v.toFixed(2)}`;
}

const bikeData = {}; // network id -> lines by class, fetched once
let noticeTimer = 0;

function showNotice(message, seconds = 5) {
  const box = $("#map-notice");
  box.textContent = message;
  box.hidden = !message;
  clearTimeout(noticeTimer);
  if (message && seconds) noticeTimer = setTimeout(() => { box.hidden = true; }, seconds * 1000);
}

async function setBikeNetworkShown(on) {
  state.show.bikenet = on;
  $("#bikenet-toggle").setAttribute("aria-pressed", String(on));
  $("#bikenet-legend").hidden = !on;
  save();
  const nets = (state.config && state.config.bike_networks) || [];
  for (const net of nets) map.attributionControl.removeAttribution(net.attribution);
  if (!on) {
    layers.bikenet.remove();
    showNotice("");
    return;
  }
  layers.bikenet.addTo(map);
  try {
    for (const net of nets) {
      if (!bikeData[net.id]) {
        showNotice(`Loading ${net.short}…`, 0);
        bikeData[net.id] = await api(`/api/bike-network/${net.id}`);
      }
    }
  } catch (err) {
    showNotice(err.message, 8);
    if (state.show.bikenet) setBikeNetworkShown(false);
    return;
  }
  if (!state.show.bikenet) return; // switched off again while loading
  for (const net of nets) map.attributionControl.addAttribution(net.attribution);
  drawBikeNetwork();
  const view = map.getBounds();
  const inView = nets.filter((n) => view.intersects(L.latLngBounds([[n.bbox[1], n.bbox[0]], [n.bbox[3], n.bbox[2]]])));
  showNotice(inView.length ? "" : `The bike network overlay covers ${nets.map((n) => n.short.replace(/'s bike network$/, "")).join(", ")}.`);
  if (inView.length === 1 && nets.length) $("#bikenet-legend-title").textContent = inView[0].name;
}

function drawBikeNetwork() {
  layers.bikenet.clearLayers();
  if (!state.show.bikenet) return;
  const line = { renderer: bikenetRenderer, interactive: false, lineJoin: "round", lineCap: "round", opacity: 0.9 };
  const styles = { // drawn in this order, so the more important routes sit on top
    local: { color: color("--bn-local"), weight: 2, opacity: 0.85, dashArray: "4 4" },
    secondary: { color: color("--bn-secondary"), weight: 3.2 },
    primary: { color: color("--bn-primary"), weight: 5 },
    riverwalk: { color: color("--bn-riverwalk"), weight: 5, dashArray: "9 5" },
  };
  for (const data of Object.values(bikeData)) {
    for (const [cls, style] of Object.entries(styles)) {
      const lines = data.classes[cls] || [];
      if (lines.length) L.polyline(lines, { ...line, ...style }).addTo(layers.bikenet);
    }
  }
}

// does this area's road data need downloading first? (asked of webapp.py, which knows the cache)
let estimateTimer = 0;
let estimateKey = "";

function scheduleEstimate() {
  clearTimeout(estimateTimer);
  estimateTimer = setTimeout(updateEstimate, 250);
}

async function updateEstimate() {
  const area = $("#area-hint");
  const oneway = state.routeType === "oneway";
  if (!state.start || !state.config || (oneway && !state.end)) { area.hidden = true; estimateKey = ""; return; }
  const params = new URLSearchParams({
    mode: state.mode,
    distance_km: String(state.distance[state.mode]),
    start: `${state.start.lat.toFixed(6)},${state.start.lon.toFixed(6)}`,
  });
  if (oneway) params.set("end", `${state.end.lat.toFixed(6)},${state.end.lon.toFixed(6)}`);
  const key = params.toString();
  estimateKey = key;
  let e;
  try { e = await api(`/api/estimate?${params}`); } catch (_) { area.hidden = true; return; }
  if (key !== estimateKey) return; // the settings have changed since
  area.hidden = false;
  if (e.ready) {
    area.className = "hint ok";
    area.textContent = "✓ Road data for this area is downloaded, so this takes seconds.";
  } else {
    const done = e.tiles - e.tiles_left;
    area.className = "hint warn";
    area.textContent = `The first route here downloads this area's road data from OpenStreetMap: ` +
      `${e.tiles} part${e.tiles === 1 ? "" : "s"}${done ? ` (${done} already done)` : ""}, ` +
      `taking about ${duration(e.minutes * 60)}. It's a one-off, and you can cancel it.`;
  }
}

function updateGenerate() {
  const btn = $("#generate");
  const needEnd = state.routeType === "oneway" && !state.end;
  btn.disabled = Boolean(state.job) || !state.start || needEnd || !state.config;
  btn.textContent = state.job ? "Planning…" : !state.start ? "Choose a start" : needEnd ? "Choose a finish" : "Generate routes";
  $("#clear").disabled = Boolean(state.job);
}

function showError(message) {
  const box = $("#form-error");
  box.textContent = message;
  box.hidden = false;
  $("#form-note").hidden = true;
}

function showNote(message) {
  const box = $("#form-note");
  box.textContent = message;
  box.hidden = false;
}

function clearMessages() {
  $("#form-error").hidden = true;
  $("#form-note").hidden = true;
}

function markStale() {
  if (state.result && !state.result.saved) $("#results-stale").hidden = false;
}

// ---------- searching ----------

async function generate(e) {
  e.preventDefault();
  const oneway = state.routeType === "oneway";
  if (state.job || !state.start || (oneway && !state.end)) return;
  clearMessages();
  setPick(null);
  const o = state.options;
  const body = {
    mode: state.mode,
    distance_km: state.distance[state.mode],
    start: { lat: state.start.lat, lon: state.start.lon },
    end: oneway ? { lat: state.end.lat, lon: state.end.lon } : null,
    alternatives: o.alternatives,
    avoid_main_roads: o.avoidMain,
    eagerness: o.eagerness,
    effort: o.effort,
    count_all_activities: o.countAll,
    bike_network: routeNetwork() && o.bikeOn ? o.bikeStrength : 0,
  };
  state.job = "starting";
  updateGenerate();
  showProgress({ status: "queued", log: [], elapsed_s: 0, progress: null });
  try {
    const { job } = await api("/api/plan", { method: "POST", body });
    state.lastJob = job;
    save();
    follow(job);
  } catch (err) {
    state.job = null;
    $("#progress").hidden = true;
    updateGenerate();
    showError(err.message);
  }
}

function follow(id) {
  state.job = id;
  updateGenerate();
  const tick = async () => {
    if (state.job !== id) return;
    let s;
    try {
      s = await api(`/api/jobs/${id}`);
    } catch (err) {
      finish(id, { status: "error", error: err.message });
      return;
    }
    if (s.status === "queued" || s.status === "running") {
      showProgress(s);
      setTimeout(tick, POLL_MS);
    } else {
      finish(id, s);
    }
  };
  tick();
}

function finish(id, s) {
  if (state.job !== id) return;
  state.job = null;
  $("#progress").hidden = true;
  resetCancel();
  updateGenerate();
  if (s.status === "done") {
    showResult(s.result);
    refreshConfig();
  } else if (s.status === "cancelled") {
    showNote(s.stage === "download" || s.stage === "waiting"
      ? "Search cancelled. The road data already downloaded is kept, so generating here again carries on from there."
      : "Search cancelled.");
    refreshConfig();
  } else {
    showError(s.error || "Something went wrong.");
  }
}

async function cancelSearch() {
  const id = state.job;
  if (!id || id === "starting") return;
  const btn = $("#cancel");
  btn.disabled = true;
  btn.textContent = "Cancelling…";
  try {
    await api(`/api/jobs/${id}/cancel`, { method: "POST" });
  } catch (err) {
    resetCancel();
    showError(err.message);
  }
}

function resetCancel() {
  const btn = $("#cancel");
  btn.disabled = false;
  btn.textContent = "Cancel";
}

function showProgress(s) {
  $("#progress").hidden = false;
  const p = s.progress;
  let text;
  if (s.status === "queued") text = "Waiting for the planner…";
  else if (p && p.stage === "download" && p.total) text = `Downloading road data: part ${Math.min(p.done + 1, p.total)} of ${p.total}`;
  else if (p && STAGES[p.stage]) text = STAGES[p.stage];
  else text = friendly(s.log.length ? s.log[s.log.length - 1] : "") || "Starting…";
  $("#progress-text").textContent = text;
  const bar = $("#progress-bar");
  bar.hidden = !(p && p.total);
  if (p && p.total) $("#progress-fill").style.width = `${clamp(100 * p.done / p.total, 0, 100)}%`;
  $("#progress-eta").textContent = p && p.eta_s != null ? `About ${duration(p.eta_s)} left` : "";
  const t = Math.round(s.elapsed_s || 0);
  $("#progress-time").textContent = `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`;
  $("#progress-log").textContent = s.log.join("\n");
}

// the planner's terminal messages, in plainer words (used before its first progress report)
function friendly(line) {
  if (/^Downloading OpenStreetMap/.test(line)) return "Downloading road data (a one-off for this area)…";
  if (/trying again in/.test(line)) return "OpenStreetMap's servers are busy. Retrying…";
  if (/^Unzipping/.test(line)) return "Unzipping your Strava export…";
  if (/^Parsing \d+ new activity/.test(line)) return "Reading new activities from your export…";
  if (/^Road network:|^Within|activities count towards|^Start:/.test(line)) return "Loading the road network…";
  return line;
}

async function refreshConfig() {
  try { state.config = await api("/api/config"); } catch (_) { return; }
  renderAreas();
  updateHints();
}

// ---------- results ----------

function showResult(result) {
  state.result = result;
  state.selected = 0;
  state.context = null;
  $("#results-stale").hidden = true;
  $("#results-error").hidden = true;
  $("#results-note").hidden = true;
  layers.untraveled.clearLayers();
  layers.traveled.clearLayers();
  drawRoutes();
  renderResults();
  renderSaved();
  const all = L.latLngBounds(result.routes[0].bounds);
  for (const r of result.routes) all.extend(r.bounds);
  fitMap(all);
  loadContext(result);
}

function select(i) {
  if (!state.result || i === state.selected) return;
  state.selected = i;
  drawRoutes();
  renderResults();
}

function routeLine(route) {
  const out = [];
  route.segments.forEach((run, k) => { for (const p of (k === 0 ? run.coords : run.coords.slice(1))) out.push(p); });
  return out;
}

function drawRoutes() {
  layers.alts.clearLayers();
  layers.route.clearLayers();
  const res = state.result;
  if (res) {
    res.routes.forEach((r, i) => {
      if (i === state.selected) return;
      L.polyline(r.segments.map((s) => s.coords), {
        pane: "alts", color: color("--alt"), weight: 5, opacity: 0.5, lineJoin: "round", bubblingMouseEvents: false,
      })
        .bindTooltip(`Option ${i + 1}: ${km(r.distance_km)} km, ${km(r.new_km)} km new. Click to show it.`, { sticky: true })
        .on("click", () => select(i))
        .on("mouseover", (e) => e.target.setStyle({ opacity: 0.85 }))
        .on("mouseout", (e) => e.target.setStyle({ opacity: 0.5 }))
        .addTo(layers.alts);
    });
    const sel = res.routes[state.selected];
    const line = { interactive: false, lineJoin: "round", lineCap: "round" };
    L.polyline(sel.segments.map((s) => s.coords), { ...line, color: state.show.aerial ? "#fff" : color("--panel"), weight: 9, opacity: 0.9 })
      .addTo(layers.route);
    for (const s of sel.segments) {
      L.polyline(s.coords, { ...line, color: s.new ? color("--new") : color("--done"), weight: 5, opacity: 1 }).addTo(layers.route);
    }
    if (res.saved) { // a saved route's own start and finish (the S/F pins are the form's)
      const pts = routeLine(sel);
      const dot = (p, fill) => L.circleMarker(p, { radius: 6, color: "#fff", weight: 2.5, fillColor: fill, fillOpacity: 1, interactive: false });
      dot(pts[0], "#188038").addTo(layers.route);
      if (res.route_type === "oneway") dot(pts[pts.length - 1], "#1f2328").addTo(layers.route);
    }
  }
  drawMarks();
}

// direction arrows along the chosen route, and distance markers at round numbers of km
function drawMarks() {
  layers.marks.clearLayers();
  const res = state.result;
  if (!res) return;
  const pts = routeLine(res.routes[state.selected]);
  if (pts.length < 2) return;
  const cum = [0];
  for (let i = 1; i < pts.length; i++) {
    cum.push(cum[i - 1] + 1000 * distanceKm({ lat: pts[i - 1][0], lon: pts[i - 1][1] }, { lat: pts[i][0], lon: pts[i][1] }));
  }
  const total = cum[cum.length - 1];
  const mpp = 156543.03392 * Math.cos(pts[0][0] * Math.PI / 180) / 2 ** map.getZoom(); // metres per pixel
  const arrowEvery = 90 * mpp;
  for (let s = arrowEvery / 2; s < total; s += arrowEvery) {
    const at = pointAt(pts, cum, s);
    L.marker(at.latlng, { icon: arrowIcon(at.angle), pane: "marks", interactive: false, keyboard: false }).addTo(layers.marks);
  }
  const steps = MODES[res.mode].kmMarks;
  const every = (steps.find((k) => (k * 1000) / mpp >= 70) || steps[steps.length - 1]) * 1000;
  for (let s = every; s < total - every * 0.25; s += every) {
    const at = pointAt(pts, cum, s);
    L.marker(at.latlng, { icon: kmIcon(Math.round(s / 1000)), pane: "marks", interactive: false, keyboard: false }).addTo(layers.marks);
  }
}

function pointAt(pts, cum, s) {
  let a = 0;
  let b = cum.length - 1;
  while (b - a > 1) {
    const mid = (a + b) >> 1;
    if (cum[mid] <= s) a = mid; else b = mid;
  }
  while (b < pts.length - 1 && cum[b] === cum[a]) b++; // step over repeated points, so there's a direction
  const t = cum[b] > cum[a] ? (s - cum[a]) / (cum[b] - cum[a]) : 0;
  const latlng = L.latLng(pts[a][0] + (pts[b][0] - pts[a][0]) * t, pts[a][1] + (pts[b][1] - pts[a][1]) * t);
  const p1 = map.project(pts[a]);
  const p2 = map.project(pts[b]);
  return { latlng, angle: Math.atan2(p2.y - p1.y, p2.x - p1.x) * 180 / Math.PI };
}

function arrowIcon(angle) {
  return L.divIcon({
    className: "route-arrow", iconSize: [14, 14], iconAnchor: [7, 7],
    html: `<svg viewBox="0 0 14 14" style="transform:rotate(${angle.toFixed(0)}deg)"><path d="M4.5 3 9.5 7 4.5 11"/></svg>`,
  });
}

function kmIcon(n) {
  const w = n >= 100 ? 30 : n >= 10 ? 24 : 18;
  return L.divIcon({ className: "km-mark", html: String(n), iconSize: [w, 17], iconAnchor: [w / 2, 8.5] });
}

function savedFor(job, index) {
  return state.saved.find((s) => s.job === job && s.index === index);
}

function renderResults() {
  const res = state.result;
  $("#results").hidden = !res;
  if (!res) {
    $("#elevation").hidden = true;
    return;
  }
  if (res.saved) {
    $("#results-title").textContent = "Saved route";
    $("#results-summary").textContent = `Saved ${dateFormat.format(new Date(res.saved.saved_at))}.`;
    $("#results-hint").hidden = true;
  } else {
    $("#results-title").textContent = "Suggested routes";
    const a = res.area;
    const where = res.route_type === "loop" ? `within ${km(res.distance_km / 2)} km of the start` : "within reach of the start and finish";
    $("#results-summary").textContent =
      `You've done ${nf0.format(a.traveled_km)} of the ${nf0.format(a.total_km)} km of roads ${where} (${nf0.format(a.traveled_pct)}%).`;
    $("#results-hint").hidden = false;
  }
  $("#route-list").replaceChildren(...res.routes.map(routeCard));
  renderProfile();
}

function routeCard(r, i) {
  const res = state.result;
  const li = el("li", { className: `route-card${i === state.selected ? " selected" : ""}` });
  const main = el("button", { type: "button", className: "route-main" });
  main.setAttribute("aria-pressed", String(i === state.selected));
  main.addEventListener("click", () => select(i));

  const title = el("span", { className: "route-title", textContent: res.saved ? res.saved.name : `Option ${i + 1}` });
  if (!res.saved && i === 0 && res.routes.length > 1) {
    const favoured = res.bike_network && res.bike_network.emphasis > 0;
    title.append(el("span", { className: "badge", textContent: favoured ? "best fit" : "most new road" }));
  }
  if (res.saved && res.saved.done) title.append(el("span", { className: "badge done", textContent: "done" }));

  const stats = el("span", { className: "route-stats" });
  stats.append(el("strong", { textContent: `${km(r.distance_km)} km` }),
    ` · ${km(r.new_km)} km new (${r.new_pct}%)` + (r.ascent_m != null ? ` · ↑ ${nf0.format(r.ascent_m)} m` : ""));

  const bar = el("span", { className: "bar", title: `${r.new_pct}% of the route is new to you` });
  const fill = el("span");
  fill.style.width = `${clamp(r.new_pct, 0, 100)}%`;
  bar.append(fill);
  main.append(title, stats);
  if (r.bike_km != null) {
    const bike = el("span", { className: "route-bike" });
    bike.innerHTML = icon("bike");
    bike.append(document.createTextNode(`${km(r.bike_km)} km on the bike network (${r.bike_pct}%)`));
    main.append(bike);
  }
  main.append(bar);

  const actions = el("div", { className: "route-actions" });
  actions.append(gpxLink(r.gpx_url));
  if (!res.saved) {
    const already = savedFor(res.job, i);
    const saveBtn = miniButton(already ? "Saved" : "Save", "save", () => saveRoute(i, saveBtn),
      already ? `Saved as "${already.name}"` : "Keep this route in your saved routes");
    saveBtn.disabled = Boolean(already);
    actions.append(saveBtn);
    if (res.route_type === "loop") {
      const rev = miniButton("Reverse", "reverse", () => reverseRoute(i, rev), "Go round the loop the other way");
      actions.append(rev);
    }
  }
  li.append(main, actions);
  return li;
}

function showResultsError(message) {
  const box = $("#results-error");
  box.textContent = message;
  box.hidden = false;
}

async function saveRoute(i, btn) {
  const res = state.result;
  btn.disabled = true;
  try {
    await api("/api/saved", { method: "POST", body: { job: res.job, index: i } });
    await loadSaved();
    if (state.result === res) renderResults();
  } catch (err) {
    btn.disabled = false;
    showResultsError(err.message);
  }
}

async function reverseRoute(i, btn) {
  const res = state.result;
  btn.disabled = true;
  $("#results-error").hidden = true;
  $("#results-note").hidden = true;
  try {
    const route = await api(`/api/jobs/${res.job}/routes/${i}/reverse`, { method: "POST" });
    if (state.result !== res) return;
    res.routes[i] = route;
    state.selected = i;
    drawRoutes();
    renderResults();
    const note = $("#results-note");
    note.textContent = route.detour_km > 0
      ? `Option ${i + 1} now goes the other way round. One-way streets add ${km(route.detour_km)} km of detours.`
      : `Option ${i + 1} now goes the other way round.`;
    note.hidden = false;
  } catch (err) {
    btn.disabled = false;
    showResultsError(err.message);
  }
}

// ---------- elevation profile ----------

const SVG_NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

function renderProfile() {
  const box = $("#elevation");
  const chart = $("#elevation-chart");
  layers.hover.clearLayers();
  const res = state.result;
  const route = res && res.routes[state.selected];
  if (!route) { box.hidden = true; return; }
  box.hidden = false;
  chart.replaceChildren();
  const prof = route.profile;
  if (!prof) {
    $("#elevation-stats").textContent = "";
    $("#elevation-note").textContent = "Elevation isn't available for this route: its data couldn't be downloaded.";
    return;
  }
  $("#elevation-stats").textContent = `↑ ${nf0.format(prof.ascent_m)} m   ↓ ${nf0.format(prof.descent_m)} m`;
  $("#elevation-note").textContent = "Point at the chart to find that spot on the map.";

  const W = chart.clientWidth || 340;
  const H = 130;
  const pad = { l: 38, r: 6, t: 8, b: 18 };
  const n = prof.km.length;
  const maxKm = prof.km[n - 1] || 1;
  let lo = Math.floor(prof.min_m / 10) * 10;
  let hi = Math.ceil(prof.max_m / 10) * 10;
  if (hi - lo < 40) { lo = Math.floor(((hi + lo) / 2 - 20) / 10) * 10; hi = lo + 40; }
  const x = (k) => pad.l + (k / maxKm) * (W - pad.l - pad.r);
  const y = (m) => pad.t + (1 - (m - lo) / (hi - lo)) * (H - pad.t - pad.b);
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": `Elevation from ${prof.min_m} to ${prof.max_m} m, with ${prof.ascent_m} m of climbing` });

  for (const m of [lo, (lo + hi) / 2, hi]) {
    svg.append(svgEl("line", { class: "grid", x1: pad.l, x2: W - pad.r, y1: y(m), y2: y(m) }));
    const label = svgEl("text", { class: "axis", x: pad.l - 5, y: y(m) + 3.5, "text-anchor": "end" });
    label.textContent = `${nf0.format(m)} m`;
    svg.append(label);
  }
  for (const k of [0, maxKm / 2, maxKm]) {
    const label = svgEl("text", { class: "axis", x: x(k), y: H - 3, "text-anchor": k === 0 ? "start" : k === maxKm ? "end" : "middle" });
    label.textContent = `${km(k)} km`;
    svg.append(label);
  }
  let d = `M${x(0)},${y(lo)}`;
  for (let i = 0; i < n; i++) d += `L${x(prof.km[i]).toFixed(1)},${y(prof.m[i]).toFixed(1)}`;
  svg.append(svgEl("path", { class: "area", d: `${d}L${x(maxKm)},${y(lo)}Z` }));
  // the line, red where the road is new and blue where it's done, as on the map
  let from = 0;
  for (let i = 1; i <= n; i++) {
    if (i < n && prof.new[i] === prof.new[from]) continue;
    const points = [];
    for (let j = Math.max(0, from - 1); j < i; j++) points.push(`${x(prof.km[j]).toFixed(1)},${y(prof.m[j]).toFixed(1)}`);
    svg.append(svgEl("polyline", { points: points.join(" "), fill: "none", "stroke-width": 2, "stroke-linejoin": "round",
      stroke: prof.new[from] ? color("--new") : color("--done") }));
    from = i;
  }

  const guide = svgEl("line", { class: "guide", y1: pad.t, y2: H - pad.b, visibility: "hidden" });
  const dot = svgEl("circle", { r: 3.5, fill: color("--text"), visibility: "hidden" });
  const readout = svgEl("text", { class: "readout", y: pad.t + 10, visibility: "hidden" });
  const hit = svgEl("rect", { x: pad.l, y: 0, width: W - pad.l - pad.r, height: H, fill: "transparent" });
  svg.append(guide, dot, readout, hit);
  const showAt = (clientX) => {
    const r = svg.getBoundingClientRect();
    const target = clamp((((clientX - r.left) * (W / r.width)) - pad.l) / (W - pad.l - pad.r), 0, 1) * maxKm;
    let a = 0;
    let b = n - 1;
    while (b - a > 1) {
      const mid = (a + b) >> 1;
      if (prof.km[mid] <= target) a = mid; else b = mid;
    }
    const i = target - prof.km[a] <= prof.km[b] - target ? a : b;
    const gx = x(prof.km[i]);
    guide.setAttribute("x1", gx);
    guide.setAttribute("x2", gx);
    dot.setAttribute("cx", gx);
    dot.setAttribute("cy", y(prof.m[i]));
    const right = gx > W * 0.6;
    readout.setAttribute("x", right ? gx - 6 : gx + 6);
    readout.setAttribute("text-anchor", right ? "end" : "start");
    readout.textContent = `${km(prof.km[i])} km · ${nf0.format(prof.m[i])} m`;
    for (const node of [guide, dot, readout]) node.setAttribute("visibility", "visible");
    showHover(prof.lat[i], prof.lon[i]);
  };
  const hide = () => {
    for (const node of [guide, dot, readout]) node.setAttribute("visibility", "hidden");
    layers.hover.clearLayers();
  };
  hit.addEventListener("pointermove", (e) => showAt(e.clientX));
  hit.addEventListener("pointerdown", (e) => showAt(e.clientX));
  hit.addEventListener("pointerleave", hide);
  chart.append(svg);
}

function showHover(lat, lon) {
  layers.hover.clearLayers();
  L.circleMarker([lat, lon], {
    radius: 7, color: "#fff", weight: 3, fillColor: "#1f2328", fillOpacity: 1, interactive: false,
  }).addTo(layers.hover);
}

// ---------- roads around the routes ----------

async function loadContext(result) {
  if (!result.context_url) return; // saved routes don't have them
  let ctx;
  try {
    ctx = await api(result.context_url);
  } catch (err) {
    console.warn("Couldn't load the roads around the route:", err.message);
    return;
  }
  if (state.result !== result) return; // a newer result is on show
  state.context = ctx;
  drawContext();
}

function drawContext() {
  layers.untraveled.clearLayers();
  layers.traveled.clearLayers();
  const ctx = state.context;
  if (!ctx) return;
  // the new roads are kept faint, as when most roads around are new they'd drown the route;
  // the done roads are bolder, in a colour that stands out from the map
  const line = { renderer: contextRenderer, interactive: false, lineJoin: "round" };
  if (ctx.untraveled.length) {
    L.polyline(ctx.untraveled, { ...line, weight: 1.4, color: color("--untraveled"),
      opacity: state.show.aerial ? 0.85 : 0.55, dashArray: "3 5" }).addTo(layers.untraveled);
  }
  if (ctx.traveled.length) {
    L.polyline(ctx.traveled, { ...line, weight: 2.4, color: color("--traveled"), opacity: 0.9 }).addTo(layers.traveled);
  }
}

// ---------- saved routes ----------

async function loadSaved() {
  try {
    state.saved = (await api("/api/saved")).routes;
  } catch (err) {
    savedError(err.message);
  }
  renderSaved();
}

function savedError(message) {
  const box = $("#saved-error");
  box.textContent = message;
  box.hidden = !message;
}

function renderSaved() {
  $("#saved-count").textContent = state.saved.length ? `(${state.saved.length})` : "";
  $("#saved-empty").hidden = state.saved.length > 0;
  const showing = state.result && state.result.saved ? state.result.saved.id : null;
  $("#saved-list").replaceChildren(...state.saved.map((s) => savedItem(s, s.id === showing)));
}

function savedItem(s, showing) {
  const li = el("li", { className: `saved-item${showing ? " showing" : ""}` });
  const name = el("div", { className: "saved-name" });
  if (state.renaming === s.id) {
    const input = el("input", { type: "text", value: s.name, maxLength: 80 });
    input.setAttribute("aria-label", "Route name");
    let finished = false;
    const commit = (keep) => {
      if (finished) return;
      finished = true;
      state.renaming = null;
      const value = input.value.trim();
      if (keep && value && value !== s.name) updateSaved(s.id, { name: value });
      else renderSaved();
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); commit(true); }
      if (e.key === "Escape") commit(false);
    });
    input.addEventListener("blur", () => commit(true));
    name.append(input);
    setTimeout(() => { input.focus(); input.select(); });
  } else {
    name.append(document.createTextNode(s.name));
    if (s.done) name.append(el("span", { className: "badge done", textContent: "done" }));
  }
  const meta = [
    `${s.mode === "run" ? "Run" : "Ride"} ${s.route_type === "loop" ? "loop" : "A → B"}`,
    `${km(s.distance_km)} km`,
    `${km(s.new_km)} km new`,
    s.ascent_m != null ? `↑ ${nf0.format(s.ascent_m)} m` : null,
    s.bike_km != null ? `${km(s.bike_km)} km on the bike network` : null,
    s.saved_at ? dateFormat.format(new Date(s.saved_at)) : null,
  ].filter(Boolean).join(" · ");

  const actions = el("div", { className: "route-actions" });
  actions.append(miniButton("Show", "eye", () => showSaved(s.id), "Show this route on the map"));
  actions.append(gpxLink(`/api/saved/${s.id}.gpx`));
  actions.append(miniButton("Rename", "edit", () => { state.renaming = s.id; renderSaved(); }));
  const done = miniButton(s.done ? "Done" : "Mark done", "check", () => updateSaved(s.id, { done: !s.done }),
    s.done ? "You've done this one. Click to undo." : "Mark it as ridden or run");
  done.setAttribute("aria-pressed", String(Boolean(s.done)));
  actions.append(done);
  const del = miniButton("Delete", "trash", () => {
    if (!del.classList.contains("danger")) { // ask once more, without a pop-up
      del.classList.add("danger");
      del.lastChild.textContent = "Delete?";
      setTimeout(() => { del.classList.remove("danger"); del.lastChild.textContent = "Delete"; }, 4000);
      return;
    }
    deleteSaved(s.id);
  }, "Delete this saved route");
  actions.append(del);
  li.append(name, el("span", { className: "saved-meta", textContent: meta }), actions);
  return li;
}

async function updateSaved(id, changes) {
  savedError("");
  try {
    const updated = await api(`/api/saved/${id}`, { method: "PATCH", body: changes });
    if (state.result && state.result.saved && state.result.saved.id === id) {
      Object.assign(state.result.saved, { name: updated.name, done: updated.done });
      renderResults();
    }
  } catch (err) {
    savedError(err.message);
  }
  await loadSaved();
}

async function deleteSaved(id) {
  savedError("");
  try {
    await api(`/api/saved/${id}`, { method: "DELETE" });
    if (state.result && state.result.saved && state.result.saved.id === id) clearRoutes();
  } catch (err) {
    savedError(err.message);
  }
  await loadSaved();
  if (state.result) renderResults(); // a suggestion's "Saved" button may be free again
}

async function showSaved(id) {
  savedError("");
  let s;
  try {
    s = await api(`/api/saved/${id}`);
  } catch (err) {
    savedError(err.message);
    return;
  }
  clearMessages();
  showResult({
    saved: { id, name: s.name, saved_at: s.saved_at, done: s.done },
    mode: s.mode, route_type: s.route_type, distance_km: s.distance_km, start: s.start, end: s.end,
    routes: [{
      index: 0, distance_km: s.distance_km, new_km: s.new_km, new_pct: s.new_pct, ascent_m: s.ascent_m,
      bike_km: s.bike_km, bike_pct: s.bike_pct,
      bounds: s.bounds, segments: s.segments, profile: s.profile, gpx_url: `/api/saved/${id}.gpx`,
    }],
  });
}

// ---------- clearing ----------

function clearRoutes() {
  state.result = null;
  state.context = null;
  state.selected = 0;
  for (const k of ["alts", "route", "marks", "hover", "untraveled", "traveled"]) layers[k].clearLayers();
  $("#results-stale").hidden = true;
  $("#results-error").hidden = true;
  renderResults();
  renderSaved();
}

// back to a clean slate: no routes or finish, the start back at your usual start (settings stay)
function clearAll() {
  if (state.job) return;
  setPick(null);
  map.closePopup();
  clearRoutes();
  clearMessages();
  state.lastJob = null;
  state.end = null;
  state.start = usualStart();
  for (const w of ["start", "end"]) {
    $(`#${w}-q`).value = "";
    $(`#${w}-results`).hidden = true;
  }
  renderPlaces();
  updateHints();
  save();
  initialView();
}

function resetOptions() {
  state.options = { ...DEFAULT_OPTIONS };
  applyStateToUI();
  markStale();
  save();
}

// ---------- start-up ----------

function renderStatus() {
  const c = state.config;
  const box = $("#status");
  if (c.export_problem) {
    box.className = "status warn";
    box.textContent = `No Strava history loaded, so every road counts as new. ${c.export_problem}`;
    return;
  }
  box.className = "status";
  box.textContent = `Using your Strava history: ${nf0.format(c.activities.ride)} rides, and ` +
    `${nf0.format(c.activities.run)} runs, walks and hikes.`;
}

function applyStateToUI() {
  const check = (name, value) => {
    const r = document.querySelector(`input[name="${name}"][value="${value}"]`);
    if (r) r.checked = true;
  };
  check("mode", state.mode);
  check("routeType", state.routeType);
  check("alternatives", state.options.alternatives);
  check("avoidMain", state.options.avoidMain);
  check("effort", state.options.effort);
  $("#count-all").checked = state.options.countAll;
  showEagerness();
  setDistanceRange();
  showDistance();
  $("#end-field").hidden = state.routeType !== "oneway";
  $("#legend").open = state.show.key;
  applyLayerToggles();
  renderPlaces();
  updateHints();
}

function initialView() {
  if (state.start && state.routeType === "oneway" && state.end) {
    fitMap(L.latLngBounds([[state.start.lat, state.start.lon], [state.end.lat, state.end.lon]]), 14);
  } else if (state.start) {
    map.setView([state.start.lat, state.start.lon], 13);
  } else if (state.config && state.config.areas.length) {
    map.setView([state.config.areas[0].lat, state.config.areas[0].lon], 11);
  }
}

function bindUI() {
  const onRadio = (name, fn) => document.querySelectorAll(`input[name="${name}"]`)
    .forEach((r) => r.addEventListener("change", () => fn(r.value)));
  onRadio("mode", setMode);
  onRadio("routeType", setRouteType);
  onRadio("alternatives", (v) => { state.options.alternatives = Number(v); markStale(); save(); });
  onRadio("avoidMain", (v) => { state.options.avoidMain = Number(v); markStale(); save(); });
  onRadio("effort", (v) => { state.options.effort = v; markStale(); save(); });
  $("#eagerness").addEventListener("input", (e) => { state.options.eagerness = Number(e.target.value); showEagerness(); markStale(); save(); });
  $("#count-all").addEventListener("change", (e) => { state.options.countAll = e.target.checked; markStale(); save(); });
  $("#bike-on").addEventListener("change", (e) => { state.options.bikeOn = e.target.checked; updateBikeField(); markStale(); save(); });
  $("#bike-strength").addEventListener("input", (e) => { state.options.bikeStrength = Number(e.target.value); showBikeStrength(); markStale(); save(); });
  $("#bikenet-toggle").addEventListener("click", () => setBikeNetworkShown(!state.show.bikenet));
  $("#distance-range").addEventListener("input", (e) => setDistance(e.target.value));
  $("#distance").addEventListener("change", (e) => setDistance(e.target.value));

  for (const which of ["start", "end"]) {
    $(`#${which}-q`).addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); search(which); }
      if (e.key === "Escape") $(`#${which}-results`).hidden = true;
    });
  }
  document.querySelectorAll("[data-search]").forEach((b) => b.addEventListener("click", () => search(b.dataset.search)));
  document.querySelectorAll("[data-pick]").forEach((b) => b.addEventListener("click", () => {
    setPick(state.pick === b.dataset.pick ? null : b.dataset.pick);
  }));
  document.querySelectorAll("[data-base]").forEach((b) => b.addEventListener("click", () => setBase(b.dataset.base)));
  $("#pick-cancel").addEventListener("click", () => setPick(null));
  $("#usual-start").addEventListener("click", useUsualStart);
  $("#locate").addEventListener("click", locate);
  $("#swap").addEventListener("click", () => {
    [state.start, state.end] = [state.end, state.start];
    renderPlaces();
    updateHints();
    markStale();
    save();
  });
  for (const k of ["untraveled", "traveled", "areas"]) {
    $(`#show-${k}`).addEventListener("change", (e) => { state.show[k] = e.target.checked; applyLayerToggles(); save(); });
  }
  $("#legend").addEventListener("toggle", (e) => { state.show.key = e.target.open; save(); });
  $("#plan-form").addEventListener("submit", generate);
  $("#cancel").addEventListener("click", cancelSearch);
  $("#clear").addEventListener("click", clearAll);
  $("#reset-options").addEventListener("click", resetOptions);

  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && state.pick) setPick(null); });
  document.addEventListener("click", (e) => { // close search suggestions when clicking elsewhere
    for (const which of ["start", "end"]) {
      const list = $(`#${which}-results`);
      if (!list.hidden && !e.target.closest(`#${which}-results, [data-search="${which}"], #${which}-q`)) list.hidden = true;
    }
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", restyle);
  let resizeTimer = 0;
  window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(renderProfile, 150); });
}

async function init() {
  restore();
  bindUI();
  setBase(state.show.aerial ? "aerial" : "map");
  applyStateToUI();
  initialView();
  try {
    state.config = await api("/api/config");
  } catch (err) {
    const box = $("#status");
    box.className = "status error";
    box.textContent = err.message;
    return;
  }
  renderStatus();
  renderAreas();
  if (!state.start || state.start.source === "usual") { // default, or refresh it from the latest history
    const p = usualStart();
    if (p) { state.start = p; renderPlaces(); save(); }
  }
  initialView();
  updateHints();
  if (state.show.bikenet) setBikeNetworkShown(true);
  await loadSaved();
  resume();
}

// after a reload: follow a search that's still running (say, a long download), or show the
// routes from the latest one while webapp.py still has them
async function resume() {
  const id = state.config.active_job || state.lastJob;
  if (!id) return;
  let s;
  try {
    s = await api(`/api/jobs/${id}`);
  } catch (_) { // expired, or webapp.py was restarted
    state.lastJob = null;
    save();
    return;
  }
  if (s.status === "done") {
    const r = s.result;
    showResult(r);
    if (r.mode !== state.mode || r.route_type !== state.routeType || r.distance_km !== state.distance[state.mode]) markStale();
  } else if (s.status === "queued" || s.status === "running") {
    showProgress(s);
    follow(id);
  }
}

init();
