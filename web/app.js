/* New Roads Planner - web front end. Talks to webapp.py's JSON API on the same origin. */
"use strict";

const MODES = {
  ride: { distance: 40, min: 5, max: 200, step: 1, plural: "rides" },
  run: { distance: 10, min: 2, max: 60, step: 0.5, plural: "runs" },
};
const STORE_KEY = "new-roads-planner:v1";
const POLL_MS = 1000;

const $ = (sel) => document.querySelector(sel);
const rootStyle = getComputedStyle(document.documentElement);
const color = (name) => rootStyle.getPropertyValue(name).trim();
const nf0 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
const nf1 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
const km = (v) => nf1.format(v);

const state = {
  mode: "ride",
  routeType: "loop",            // "loop" | "oneway"
  distance: { ride: MODES.ride.distance, run: MODES.run.distance },
  start: null,                  // { lat, lon, label, source: "usual" | "search" | "map" | "gps" }
  end: null,
  options: { alternatives: 3, avoidMain: 1, eagerness: 0.6, effort: "normal", countAll: false },
  show: { untraveled: true, traveled: false, areas: true, key: true },
  config: null,                 // from /api/config
  pick: null,                   // "start" | "end" while the next map click sets it
  job: null,                    // id of the search in progress
  result: null,                 // the finished search
  selected: 0,                  // which suggested route is highlighted
};

// ---------- saving settings between visits (this browser only) ----------

function save() {
  const { mode, routeType, distance, start, end, options, show } = state;
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify({ mode, routeType, distance, start, end, options, show }));
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
  for (const k in state.show) if (typeof (s.show || {})[k] === "boolean") state.show[k] = s.show[k];
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

function distanceKm(a, b) {
  const rad = Math.PI / 180;
  const h = Math.sin((b.lat - a.lat) * rad / 2) ** 2 +
    Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin((b.lon - a.lon) * rad / 2) ** 2;
  return 12742 * Math.asin(Math.min(1, Math.sqrt(h)));
}

// ---------- the map ----------

const map = L.map("map", { zoomControl: false, worldCopyJump: true }).setView([20, 0], 2);
L.control.zoom({ position: "topright" }).addTo(map);
L.control.scale({ imperial: false, position: "bottomright" }).addTo(map);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
}).addTo(map);

// draw order: downloaded areas < nearby roads < other suggestions < chosen route < markers
for (const [pane, z] of [["areas", 350], ["context", 380], ["alts", 390]]) map.createPane(pane).style.zIndex = z;
const contextRenderer = L.canvas({ pane: "context", padding: 0.3 }); // thousands of lines: canvas is much faster
const layers = {
  areas: L.layerGroup(),
  untraveled: L.layerGroup(),
  traveled: L.layerGroup(),
  alts: L.layerGroup().addTo(map),
  route: L.layerGroup().addTo(map),
  places: L.layerGroup().addTo(map),
};
const markers = {};

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
      color: color("--area"), weight: 1.5, opacity: 0.55, dashArray: "6 8", fillOpacity: 0.025,
    }).addTo(layers.areas);
  }
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
  const box = document.createElement("div");
  box.className = "popup-actions";
  const add = (text, which) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = text;
    b.addEventListener("click", () => { map.closePopup(); setPlace(which, place, { pan: false }); });
    box.append(b);
  };
  add("Start here", "start");
  if (state.routeType === "oneway") add("Finish here", "end");
  L.popup({ closeButton: false, offset: [0, -2] }).setLatLng(e.latlng).setContent(box).openOn(map);
});

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
  const el = $(`#${which}-label`);
  const p = state[which];
  if (!p) { el.textContent = emptyText; return; }
  const name = document.createElement("strong");
  name.textContent = p.label || "Dropped pin";
  el.replaceChildren(name, ` · ${p.lat.toFixed(5)}, ${p.lon.toFixed(5)}`);
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

// "Brisbane City Hall, 64, Adelaide Street, Brisbane City, ..." -> "Brisbane City Hall, Adelaide Street"
function shortName(name) {
  const parts = name.split(",").map((s) => s.trim()).filter((s) => s && !/^\d+[a-z]?$/i.test(s));
  return parts.slice(0, 2).join(", ") || name;
}

function showList(list, items) {
  list.replaceChildren(...items.map((it) => {
    const li = document.createElement("li");
    if (it.pick) {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = it.text;
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
  $("#distance-range").value = String(Math.min(m.max, Math.max(m.min, d)));
  $("#distance").value = String(d);
}

function showEagerness() {
  const v = state.options.eagerness;
  $("#eagerness").value = String(v);
  $("#eagerness-value").textContent = `${v < 0.3 ? "a little" : v < 0.75 ? "balanced" : "keenly"} · ${v.toFixed(2)}`;
}

function searchCenter() {
  if (!state.start) return null;
  if (state.routeType === "oneway" && state.end) {
    return { lat: (state.start.lat + state.end.lat) / 2, lon: (state.start.lon + state.end.lon) / 2 };
  }
  return state.start;
}

// mirrors pipeline.Workspace.graph_for: is there a downloaded road graph big enough?
function areaReady(mode, center, distance) {
  const needKm = distance * 1.05 / 2 + 1.5;
  return ((state.config && state.config.areas) || [])
    .some((a) => a.mode === mode && distanceKm(center, a) + needKm <= a.radius_m / 1000);
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

  const area = $("#area-hint");
  const c = searchCenter();
  if (!c || !state.config) {
    area.hidden = true;
  } else {
    const ready = areaReady(state.mode, c, d);
    area.hidden = false;
    area.className = `hint ${ready ? "ok" : "warn"}`;
    area.textContent = ready
      ? "✓ Road data for this area is downloaded, so this takes seconds."
      : "The first route here downloads road data from OpenStreetMap. That's a one-off, but can take 2–30 minutes.";
  }
  updateGenerate();
}

function updateGenerate() {
  const btn = $("#generate");
  const needEnd = state.routeType === "oneway" && !state.end;
  btn.disabled = Boolean(state.job) || !state.start || needEnd || !state.config;
  btn.textContent = state.job ? "Planning…" : !state.start ? "Choose a start" : needEnd ? "Choose a finish" : "Generate routes";
}

function showError(message) {
  const el = $("#form-error");
  el.textContent = message;
  el.hidden = false;
}

function clearError() {
  $("#form-error").hidden = true;
}

function markStale() {
  if (state.result) $("#results-stale").hidden = false;
}

// ---------- searching ----------

async function generate(e) {
  e.preventDefault();
  const oneway = state.routeType === "oneway";
  if (state.job || !state.start || (oneway && !state.end)) return;
  clearError();
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
  };
  state.job = "starting";
  updateGenerate();
  showProgress({ status: "queued", log: [], elapsed_s: 0 });
  try {
    const { job } = await api("/api/plan", { method: "POST", body });
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
      finish(id, null, err.message);
      return;
    }
    showProgress(s);
    if (s.status === "done") finish(id, s.result);
    else if (s.status === "error") finish(id, null, s.error);
    else setTimeout(tick, POLL_MS);
  };
  tick();
}

function finish(id, result, error) {
  if (state.job !== id) return;
  state.job = null;
  $("#progress").hidden = true;
  updateGenerate();
  if (error) showError(error);
  else showResult(result);
}

function showProgress(s) {
  $("#progress").hidden = false;
  const last = s.log.length ? s.log[s.log.length - 1] : "";
  $("#progress-text").textContent = s.status === "queued" ? "Waiting for the planner…" : friendly(last) || "Starting…";
  const t = Math.round(s.elapsed_s || 0);
  $("#progress-time").textContent = `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`;
  $("#progress-log").textContent = s.log.join("\n");
}

// the planner's terminal messages, in plainer words
function friendly(line) {
  const tile = /^tile (\d+)\/(\d+)/.exec(line);
  if (tile) return `Downloading road data: part ${tile[1]} of ${tile[2]}…`;
  if (/^Downloading OpenStreetMap/.test(line)) return "Downloading road data (a one-off for this area)…";
  if (/trying again in/.test(line)) return "OpenStreetMap's servers are busy. Retrying…";
  if (/^Building road graph/.test(line)) return "Building the road network…";
  if (/^Unzipping/.test(line)) return "Unzipping your Strava export…";
  if (/^Parsing \d+ new activity/.test(line)) return "Reading new activities from your export…";
  if (/^Matching your GPS history/.test(line)) return "Working out which roads you've done…";
  if (/candidate waypoint areas|^Searching for the route/.test(line)) return "Searching for the route with the most new road…";
  if (/^Road network:|^Within|activities count towards|^Start:/.test(line)) return "Loading the road network…";
  return line;
}

// ---------- results ----------

function showResult(result) {
  state.result = result;
  state.selected = 0;
  $("#results-stale").hidden = true;
  drawRoutes();
  renderResults();
  const all = L.latLngBounds(result.routes[0].bounds);
  for (const r of result.routes) all.extend(r.bounds);
  fitMap(all);
  loadContext(result);
}

// fit the map to bounds, keeping clear of the map key while it's open on a roomy map
function fitMap(bounds, maxZoom = 16) {
  const key = $("#legend");
  const roomy = $("#map").clientWidth > 640 && key.open && key.offsetParent !== null;
  map.fitBounds(bounds, { paddingTopLeft: [roomy ? key.offsetWidth + 28 : 40, 40], paddingBottomRight: [40, 40], maxZoom });
}

function select(i) {
  if (!state.result || i === state.selected) return;
  state.selected = i;
  drawRoutes();
  renderResults();
}

function drawRoutes() {
  layers.alts.clearLayers();
  layers.route.clearLayers();
  const res = state.result;
  if (!res) return;
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
  L.polyline(sel.segments.map((s) => s.coords), { ...line, color: color("--panel"), weight: 9, opacity: 0.9 }).addTo(layers.route);
  for (const s of sel.segments) {
    L.polyline(s.coords, { ...line, color: s.new ? color("--new") : color("--done"), weight: 5, opacity: 1 }).addTo(layers.route);
  }
}

function renderResults() {
  const res = state.result;
  $("#results").hidden = !res;
  if (!res) return;
  const a = res.area;
  const where = res.route_type === "loop" ? `within ${km(res.distance_km / 2)} km of the start` : "within reach of the start and finish";
  $("#results-summary").textContent =
    `You've done ${nf0.format(a.traveled_km)} of the ${nf0.format(a.total_km)} km of roads ${where} (${nf0.format(a.traveled_pct)}%).`;
  $("#route-list").replaceChildren(...res.routes.map(routeCard));
}

function routeCard(r, i) {
  const li = document.createElement("li");
  li.className = `route-card${i === state.selected ? " selected" : ""}`;

  const main = document.createElement("button");
  main.type = "button";
  main.className = "route-main";
  main.setAttribute("aria-pressed", String(i === state.selected));
  main.addEventListener("click", () => select(i));

  const title = document.createElement("span");
  title.className = "route-title";
  title.textContent = `Option ${i + 1}`;
  if (i === 0 && state.result.routes.length > 1) {
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = "most new road";
    title.append(badge);
  }

  const stats = document.createElement("span");
  stats.className = "route-stats";
  const length = document.createElement("strong");
  length.textContent = `${km(r.distance_km)} km`;
  stats.append(length, ` · ${km(r.new_km)} km new (${r.new_pct}%)`);

  const bar = document.createElement("span");
  bar.className = "bar";
  bar.title = `${r.new_pct}% of the route is new to you`;
  const fill = document.createElement("span");
  fill.style.width = `${Math.max(0, Math.min(100, r.new_pct))}%`;
  bar.append(fill);
  main.append(title, stats, bar);

  const gpx = document.createElement("a");
  gpx.className = "gpx-btn";
  gpx.href = r.gpx_url;
  gpx.setAttribute("download", "");
  gpx.title = "Download as GPX, for a bike computer, watch or route app";
  gpx.innerHTML = '<svg class="i" aria-hidden="true"><use href="#i-download"/></svg>GPX';

  li.append(main, gpx);
  return li;
}

async function loadContext(result) {
  layers.untraveled.clearLayers();
  layers.traveled.clearLayers();
  let ctx;
  try {
    ctx = await api(result.context_url);
  } catch (err) {
    console.warn("Couldn't load the roads around the route:", err.message);
    return;
  }
  if (state.result !== result) return; // a newer search has finished since
  // kept faint: when most roads around are new, this layer covers everything and must not drown the route
  const line = { renderer: contextRenderer, interactive: false, weight: 1.4, lineJoin: "round" };
  if (ctx.untraveled.length) {
    L.polyline(ctx.untraveled, { ...line, color: color("--untraveled"), opacity: 0.55, dashArray: "3 5" }).addTo(layers.untraveled);
  }
  if (ctx.traveled.length) {
    L.polyline(ctx.traveled, { ...line, color: color("--traveled"), opacity: 0.5 }).addTo(layers.traveled);
  }
}

// ---------- start-up ----------

function renderStatus() {
  const c = state.config;
  const el = $("#status");
  if (c.export_problem) {
    el.className = "status warn";
    el.textContent = `No Strava history loaded, so every road counts as new. ${c.export_problem}`;
    return;
  }
  el.className = "status";
  el.textContent = `Using your Strava history: ${nf0.format(c.activities.ride)} rides, and ` +
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

  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && state.pick) setPick(null); });
  document.addEventListener("click", (e) => { // close search suggestions when clicking elsewhere
    for (const which of ["start", "end"]) {
      const list = $(`#${which}-results`);
      if (!list.hidden && !e.target.closest(`#${which}-results, [data-search="${which}"], #${which}-q`)) list.hidden = true;
    }
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    renderAreas();
    drawRoutes();
    if (state.result) loadContext(state.result);
  });
}

async function init() {
  restore();
  bindUI();
  applyStateToUI();
  initialView();
  try {
    state.config = await api("/api/config");
  } catch (err) {
    const el = $("#status");
    el.className = "status error";
    el.textContent = err.message;
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
  if (state.config.active_job) { // e.g. the page was reloaded during a long download
    showProgress({ status: "running", log: [], elapsed_s: 0 });
    follow(state.config.active_job);
  }
}

init();
