"""Road network download (OpenStreetMap via OSMnx) and traveled/untraveled marking.

The graph is stored as plain numpy arrays:
  * nodes: lat/lon per node
  * undirected edges ("roads"): endpoints, length, highway penalty, geometry
  * directed arcs: which way you're allowed to travel each road (one-way streets)
"""
from __future__ import annotations

import glob
import hashlib
import math
import os
import pickle
from dataclasses import dataclass

import numpy as np
from scipy.spatial import cKDTree

# --- network definitions -------------------------------------------------------

_EXCL_COMMON = (
    "abandoned|bus_guideway|construction|corridor|elevator|escalator|motor|no|planned|"
    "platform|proposed|raceway|razed|rest_area|services"
)

NETWORKS = {
    # bikes: all roads except motorways, plus cycleways/paths, plus footways that allow bikes
    "ride": dict(
        network_type="bike",
        custom_filter=[
            f'["highway"]["area"!~"yes"]["highway"!~"{_EXCL_COMMON}|footway|steps|bridleway|pedestrian"]'
            '["bicycle"!~"no|dismount"]["access"!~"private|no"]'
            '["service"!~"private|parking_aisle|driveway|emergency_access|drive-through"]',
            '["highway"~"footway|pedestrian"]["bicycle"~"yes|designated|permissive"]["access"!~"private|no"]',
        ],
        # cost multipliers so routes prefer quieter roads
        penalty={"trunk": 4.0, "trunk_link": 3.0, "primary": 2.0, "primary_link": 1.6,
                 "secondary": 1.3, "secondary_link": 1.2, "track": 1.3, "path": 1.2},
    ),
    # runs/walks/hikes: everything walkable incl. cycleways (shared paths), both directions
    "run": dict(
        network_type="walk",
        custom_filter=[
            f'["highway"]["area"!~"yes"]["highway"!~"{_EXCL_COMMON}"]'
            '["foot"!~"no"]["access"!~"private|no"]'
            '["service"!~"private|parking_aisle|driveway|emergency_access|drive-through"]',
        ],
        penalty={"trunk": 4.0, "trunk_link": 3.0, "primary": 2.5, "primary_link": 2.0,
                 "secondary": 1.5, "secondary_link": 1.3},
    ),
}

# --- local planar projection (good to a few metres over ~100 km) -----------------


class Projector:
    def __init__(self, lat0: float, lon0: float):
        self.lat0, self.lon0 = lat0, lon0
        self.kx = 111_320.0 * math.cos(math.radians(lat0))
        self.ky = 110_540.0

    def xy(self, lat, lon):
        lat = np.asarray(lat, dtype=np.float64)
        lon = np.asarray(lon, dtype=np.float64)
        return np.stack([(lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky], axis=-1)


def haversine_m(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = (0.5 - math.cos((lat2 - lat1) * p) / 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * (1 - math.cos((lon2 - lon1) * p)) / 2)
    return 12_742_000 * math.asin(math.sqrt(a))


# --- graph container -------------------------------------------------------------


@dataclass
class RoadGraph:
    mode: str
    center: tuple[float, float]
    radius_m: float
    node_lat: np.ndarray       # (N,)
    node_lon: np.ndarray       # (N,)
    edge_u: np.ndarray         # (E,) undirected road endpoints (u < v not guaranteed)
    edge_v: np.ndarray
    edge_len: np.ndarray       # metres
    edge_pen: np.ndarray       # cost multiplier
    edge_name: list            # street name or ""
    edge_target: np.ndarray    # bool: counts as a road to collect (False for sidewalks/crossings)
    geom_offsets: np.ndarray   # (E+1,) into geom_lonlat
    geom_lonlat: np.ndarray    # (M, 2), oriented u -> v
    arc_from: np.ndarray       # (A,) directed arcs you may travel
    arc_to: np.ndarray
    arc_edge: np.ndarray       # undirected edge id of each arc

    @property
    def n_nodes(self):
        return len(self.node_lat)

    def edge_coords(self, e: int, from_node: int | None = None) -> np.ndarray:
        c = self.geom_lonlat[self.geom_offsets[e]:self.geom_offsets[e + 1]]
        if from_node is not None and from_node != self.edge_u[e]:
            c = c[::-1]
        return c


def _hw_penalty(hw, table) -> float:
    if isinstance(hw, (list, tuple)):
        return max((table.get(h, 1.0) for h in hw), default=1.0)
    return table.get(hw, 1.0)


def _cache_name(mode, lat, lon, radius_m):
    return f"graph_{mode}_{lat:.4f}_{lon:.4f}_{int(radius_m)}.pkl"


def cached_areas(cache_dir, mode=None) -> list[dict]:
    """Areas with a road graph ready in the cache: [{"mode", "lat", "lon", "radius_m", "path"}]."""
    out = []
    for path in glob.glob(os.path.join(cache_dir, f"graph_{mode or '*'}_*.pkl")):
        try:
            _, m, clat, clon, crad = os.path.basename(path)[:-4].split("_")
            out.append({"mode": m, "lat": float(clat), "lon": float(clon), "radius_m": float(crad),
                        "path": path})
        except ValueError:
            continue
    return out


def find_cached_graph(cache_dir, mode, lat, lon, radius_m) -> str | None:
    """Return the smallest cached graph file whose circle fully contains the requested circle."""
    fits = [a for a in cached_areas(cache_dir, mode)
            if haversine_m(lat, lon, a["lat"], a["lon"]) + radius_m <= a["radius_m"]]
    return min(fits, key=lambda a: a["radius_m"])["path"] if fits else None


def load_graph(cache_dir: str, mode: str, lat: float, lon: float, radius_m: float) -> RoadGraph:
    os.makedirs(cache_dir, exist_ok=True)
    hit = find_cached_graph(cache_dir, mode, lat, lon, radius_m)
    if hit:
        with open(hit, "rb") as fh:
            return pickle.load(fh)

    import osmnx as ox

    net = NETWORKS[mode]
    print(f"Downloading OpenStreetMap {mode} network, radius {radius_m/1000:.1f} km "
          "(one-off; cached afterwards)...")
    osm_path = os.path.join(cache_dir, f"osm_{mode}_{lat:.4f}_{lon:.4f}_{int(radius_m)}.osm")
    if not os.path.exists(osm_path):
        _download_osm(osm_path, net["custom_filter"], lat, lon, radius_m)
    print("Building road graph...")
    if "footway" not in ox.settings.useful_tags_way:
        ox.settings.useful_tags_way = list(ox.settings.useful_tags_way) + ["footway"]
    G = ox.graph_from_xml(osm_path, bidirectional=net["network_type"] in ox.settings.bidirectional_network_types,
                          simplify=True, retain_all=False)
    rg = _graph_to_arrays(G, mode, (lat, lon), radius_m, net["penalty"])
    # the raw .osm file is kept so the graph can be rebuilt without re-downloading
    # (delete cache/graph_*.pkl to force a rebuild)
    path = os.path.join(cache_dir, _cache_name(mode, lat, lon, radius_m))
    with open(path + ".part", "wb") as fh:  # write-then-rename, so an interrupted save can't leave a broken cache
        pickle.dump(rg, fh, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(path + ".part", path)
    return rg


# main public Overpass server first, then community mirrors (listed on the OSM wiki)
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
USER_AGENT = "new-roads-planner/0.1 (open-source route planner)"


def _download_osm(out_path, filters, lat, lon, radius_m):
    """Fetch OSM ways via the Overpass API.

    Uses curl rather than Python's requests: the macOS system Python ships an old
    LibreSSL that overpass-api.de refuses to handshake with.
    """
    dlat = radius_m / 110_540.0
    dlon = radius_m / (111_320.0 * math.cos(math.radians(lat)))
    s, w, n, e = lat - dlat, lon - dlon, lat + dlat, lon + dlon
    # modest tiles: Overpass schedules small requests far more readily than big ones
    k = max(1, math.ceil(2 * radius_m / TILE_M))
    tiles = []
    for i in range(k):
        for j in range(k):
            tiles.append((s + (n - s) * i / k, w + (e - w) * j / k,
                          s + (n - s) * (i + 1) / k, w + (e - w) * (j + 1) / k))
    tile_paths = []
    for t_i, (ts, tw, tn, te) in enumerate(tiles, 1):
        tp = f"{out_path}.tile{t_i}"
        tile_paths.append(tp)
        if os.path.exists(tp):
            continue
        print(f"  tile {t_i}/{len(tiles)}")
        bbox = f"{ts:.6f},{tw:.6f},{tn:.6f},{te:.6f}"
        body = "".join(f"way{f}({bbox});" for f in filters)
        _overpass_fetch(f"[out:xml][timeout:300];({body});(._;>;);out body qt;", tp)
    _merge_osm_xml(tile_paths, out_path)
    for tp in tile_paths:
        os.remove(tp)


TILE_M = 12_000


def _merge_osm_xml(paths, out_path):
    """Merge Overpass XML tiles, de-duplicating nodes and ways shared at tile borders."""
    import xml.etree.ElementTree as ET

    tmp = out_path + ".part"
    with open(tmp, "wb") as out:
        out.write(b'<?xml version="1.0" encoding="UTF-8"?>\n<osm version="0.6">\n')
        for kind in ("node", "way"):
            seen = set()
            for p in paths:
                ctx = ET.iterparse(p, events=("start", "end"))
                _, root = next(ctx)
                for ev, el in ctx:
                    if ev != "end" or el.tag != kind:
                        continue
                    oid = el.get("id")
                    if oid not in seen:
                        seen.add(oid)
                        out.write(ET.tostring(el))
                    root.clear()
        out.write(b"</osm>\n")
    os.replace(tmp, out_path)


def _overpass_fetch(query: str, out_path: str):
    import subprocess
    import time as _time

    q_path = out_path + ".query"
    with open(q_path, "w") as fh:
        fh.write(query)
    tmp = out_path + ".part"
    for attempt in range(9):
        url = OVERPASS_URLS[attempt % len(OVERPASS_URLS)]
        host = url.split("/")[2]
        res = subprocess.run(
            ["curl", "-sS", "--fail-with-body", "-A", USER_AGENT, "--max-time", "420", "--connect-timeout", "30",
             "--data-urlencode", f"data@{q_path}", url, "-o", tmp, "-w", "%{http_code}"],
            capture_output=True, text=True)
        code = res.stdout.strip()
        if res.returncode == 0 and code == "200":
            # Overpass can answer 200 with a truncated result + <remark> on timeouts
            with open(tmp, "rb") as fh:
                fh.seek(max(0, os.path.getsize(tmp) - 4096))
                tail = fh.read()
            if b"<remark>" in tail or b"</osm>" not in tail:
                code = "200 with incomplete data"
                res = None
        if res is not None and res.returncode == 0 and code == "200":
            os.replace(tmp, out_path)
            os.remove(q_path)
            return
        wait = 20 * (attempt // len(OVERPASS_URLS) + 1)
        print(f"  {host} returned {code or res.stderr.strip()}; trying again in {wait}s...", flush=True)
        _time.sleep(wait)
    raise RuntimeError("Could not download OpenStreetMap data from Overpass (try again later).")


def search_places(query: str, limit: int = 5, viewbox=None) -> list[dict]:
    """Look up places with OSM Nominatim (via curl, see _download_osm).

    viewbox = (west, north, east, south) prefers results in that area without
    excluding others. Returns [{"name", "lat", "lon"}], best match first.
    """
    import json
    import subprocess

    cmd = ["curl", "-sS", "-G", "--fail", "--max-time", "20", "-A", USER_AGENT,
           "https://nominatim.openstreetmap.org/search",
           "--data-urlencode", f"q={query}", "--data-urlencode", "format=json",
           "--data-urlencode", f"limit={int(limit)}"]
    if viewbox:
        cmd += ["--data-urlencode", "viewbox=" + ",".join(f"{float(v):.5f}" for v in viewbox)]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [{"name": h.get("display_name", ""), "lat": float(h["lat"]), "lon": float(h["lon"])}
            for h in json.loads(res.stdout)]


def geocode(address: str) -> tuple[float, float]:
    """Look up an address with OSM Nominatim."""
    hits = search_places(address, limit=1)
    if not hits:
        raise SystemExit(f"Address not found: {address}")
    return hits[0]["lat"], hits[0]["lon"]


def _graph_to_arrays(G, mode, center, radius_m, pen_table) -> RoadGraph:
    osm_ids = list(G.nodes)
    idx = {n: i for i, n in enumerate(osm_ids)}
    node_lat = np.array([G.nodes[n]["y"] for n in osm_ids])
    node_lon = np.array([G.nodes[n]["x"] for n in osm_ids])

    edge_key: dict[tuple, int] = {}  # geometry fingerprint -> undirected edge id
    eu, ev, elen, epen, ename, etarget, geoms = [], [], [], [], [], [], []
    arcs: dict[tuple[int, int], tuple[float, int]] = {}  # (from,to) -> (cost, edge)

    for u, v, data in G.edges(data=True):
        a, b = idx[u], idx[v]
        length = float(data.get("length", 0.0))
        if length <= 0:
            continue
        if "geometry" in data:
            c = np.asarray(data["geometry"].coords)
        else:
            c = np.array([[node_lon[a], node_lat[a]], [node_lon[b], node_lat[b]]])
        # orient geometry a -> b
        if np.hypot(*(c[0] - [node_lon[a], node_lat[a]])) > np.hypot(*(c[-1] - [node_lon[a], node_lat[a]])):
            c = c[::-1]
        # one physical road = same endpoints + same set of shape points, whichever
        # direction (or how many times) OSMnx emitted it
        key = (min(a, b), max(a, b), tuple(sorted(map(tuple, np.round(c, 7).tolist()))))
        e = edge_key.get(key)
        if e is None:
            e = len(eu)
            edge_key[key] = e
            eu.append(a)
            ev.append(b)
            elen.append(length)
            epen.append(_hw_penalty(data.get("highway"), pen_table))
            name = data.get("name", "")
            ename.append(name[0] if isinstance(name, list) else (name or ""))
            fw = data.get("footway", "")
            fw = set(fw) if isinstance(fw, list) else {fw}
            # sidewalks/crossings mapped separately duplicate the street itself: route on
            # them if useful, but don't treat them as roads to collect
            etarget.append(not (fw & {"sidewalk", "crossing", "traffic_island"}))
            geoms.append(c)
        # directed arc a -> b (OSMnx already added reverse arcs where travel is allowed)
        cost = length * epen[e]
        if a != b:
            prev = arcs.get((a, b))
            if prev is None or cost < prev[0]:
                arcs[(a, b)] = (cost, e)

    offsets = np.zeros(len(geoms) + 1, dtype=np.int64)
    offsets[1:] = np.cumsum([len(g) for g in geoms])
    arc_items = list(arcs.items())
    return RoadGraph(
        mode=mode, center=center, radius_m=radius_m,
        node_lat=node_lat, node_lon=node_lon,
        edge_u=np.array(eu, dtype=np.int32), edge_v=np.array(ev, dtype=np.int32),
        edge_len=np.array(elen), edge_pen=np.array(epen), edge_name=ename,
        edge_target=np.array(etarget, dtype=bool),
        geom_offsets=offsets, geom_lonlat=np.concatenate(geoms).astype(np.float64),
        arc_from=np.array([k[0] for k, _ in arc_items], dtype=np.int32),
        arc_to=np.array([k[1] for k, _ in arc_items], dtype=np.int32),
        arc_edge=np.array([v[1] for _, v in arc_items], dtype=np.int32),
    )


# --- coverage: which roads have you already traveled? ---------------------------

MATCH_RADIUS_M = 20.0      # GPS point within this distance of a road = traveled there
SAMPLE_STEP_M = 10.0       # sample roads every 10 m
GPS_DENSIFY_M = 8.0        # fill gaps between GPS fixes
GPS_MAX_GAP_M = 300.0      # larger gaps = signal loss / pause; don't join them


def _densify(xy: np.ndarray) -> np.ndarray:
    if len(xy) < 2:
        return xy
    d = np.hypot(*(xy[1:] - xy[:-1]).T)
    ok = (d > GPS_DENSIFY_M) & (d <= GPS_MAX_GAP_M)
    if not ok.any():
        return xy
    n = np.where(ok, np.ceil(d / GPS_DENSIFY_M).astype(int) - 1, 0)
    seg = np.repeat(np.arange(len(d)), n)
    # fractional position of each inserted point within its segment
    within = np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n) + 1
    t = within / (n[seg] + 1)
    extra = xy[seg] + (xy[seg + 1] - xy[seg]) * t[:, None]
    return np.concatenate([xy, extra])


def gps_points_xy(tracks, proj: Projector, bbox_xy) -> np.ndarray:
    (x0, y0), (x1, y1) = bbox_xy
    out = []
    for t in tracks:
        xy = proj.xy(t.latlon[:, 0], t.latlon[:, 1])
        inside = (xy[:, 0] > x0) & (xy[:, 0] < x1) & (xy[:, 1] > y0) & (xy[:, 1] < y1)
        if inside.sum() < 2:
            continue
        out.append(_densify(xy))
    if not out:
        return np.zeros((0, 2))
    pts = np.concatenate(out)
    # de-duplicate on a 3 m grid – repeated routes overlap massively
    grid = np.unique(np.round(pts / 3.0).astype(np.int64), axis=0)
    return grid * 3.0


def road_samples(rg: RoadGraph, proj: Projector):
    """Points every SAMPLE_STEP_M along each road (skipping the ends near junctions)."""
    xy_all = proj.xy(rg.geom_lonlat[:, 1], rg.geom_lonlat[:, 0])
    pts, owners = [], []
    for e in range(len(rg.edge_len)):
        c = xy_all[rg.geom_offsets[e]:rg.geom_offsets[e + 1]]
        seg = np.hypot(*(c[1:] - c[:-1]).T)
        cum = np.concatenate([[0.0], np.cumsum(seg)])
        L = cum[-1]
        if L <= 0:
            continue
        trim = min(MATCH_RADIUS_M, L / 3)  # ignore bits a crossing street would touch
        n = max(1, int((L - 2 * trim) // SAMPLE_STEP_M) + 1)
        s = np.linspace(trim, L - trim, n) if n > 1 else np.array([L / 2])
        x = np.interp(s, cum, c[:, 0])
        y = np.interp(s, cum, c[:, 1])
        pts.append(np.stack([x, y], axis=1))
        owners.append(np.full(len(s), e, dtype=np.int32))
    return np.concatenate(pts), np.concatenate(owners)


def compute_coverage(rg: RoadGraph, tracks, cache_dir: str) -> np.ndarray:
    """Return fraction (0..1) of each road already traveled."""
    sig = hashlib.md5(
        (f"{rg.mode}{rg.center}{rg.radius_m}{len(rg.edge_len)}" +
         ",".join(sorted(t.activity_id for t in tracks))).encode()).hexdigest()[:12]
    path = os.path.join(cache_dir, f"coverage_{sig}.npy")
    if os.path.exists(path):
        return np.load(path)

    proj = Projector(*rg.center)
    nxy = proj.xy(rg.node_lat, rg.node_lon)
    pad = 500
    bbox = ((nxy[:, 0].min() - pad, nxy[:, 1].min() - pad), (nxy[:, 0].max() + pad, nxy[:, 1].max() + pad))
    print("Matching your GPS history to the road network...")
    gps = gps_points_xy(tracks, proj, bbox)
    samples, owner = road_samples(rg, proj)
    if len(gps):
        tree = cKDTree(gps)
        d, _ = tree.query(samples, k=1, distance_upper_bound=MATCH_RADIUS_M)
        hit = np.isfinite(d)
    else:
        hit = np.zeros(len(samples), dtype=bool)
    n_tot = np.bincount(owner, minlength=len(rg.edge_len))
    n_hit = np.bincount(owner, weights=hit, minlength=len(rg.edge_len))
    frac = np.divide(n_hit, n_tot, out=np.zeros(len(rg.edge_len)), where=n_tot > 0)
    # snap GPS noise: mostly-done counts as done, a couple of stray hits counts as new
    frac = np.where(frac >= 0.85, 1.0, np.where(frac <= 0.15, 0.0, frac))
    with open(path + ".part", "wb") as fh:
        np.save(fh, frac)
    os.replace(path + ".part", path)
    return frac
