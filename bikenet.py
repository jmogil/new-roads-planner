"""Local cycle networks, matched onto the road graph so ride routes can favour them.

The first source is Brisbane City Council's City Plan 2014 bicycle network overlay: its primary,
secondary and local cycle routes and the RiverWalk, published as open data under CC BY 4.0.
Each entry in SOURCES says where a network covers, where to download it, and how its categories
map onto the planner's classes, so another city's network can be added with another entry.

These are planning maps: most routes exist on the ground, but some may be planned rather than built.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from dataclasses import dataclass

import numpy as np
from scipy.spatial import cKDTree

import progress
import roads

NONE, PRIMARY, SECONDARY, LOCAL, RIVERWALK = 0, 1, 2, 3, 4
CLASS_NAMES = {PRIMARY: "primary", SECONDARY: "secondary", LOCAL: "local", RIVERWALK: "riverwalk"}
# At full emphasis, how much cheaper each class is to ride along, and how much more new road on it
# is worth (indexed by class; 0 = not on the network)
COST_DISCOUNT = np.array([0.0, 0.6, 0.5, 0.35, 0.6])
VALUE_BONUS = np.array([0.0, 1.0, 0.75, 0.4, 1.0])
MATCH_M = 15.0      # a road is on a cycle route if the route runs within this distance of it,
MAX_ANGLE = 35.0    # in roughly the same direction (so crossing streets don't count),
MIN_SHARE = 0.5     # along at least half its length
REFRESH_DAYS = 90   # download a source again after this long (keeping the old copy if that fails)


class Unavailable(Exception):
    """A cycle network couldn't be downloaded or read."""


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    short: str                  # for labels: "Prefer <short>"
    bbox: tuple                 # (west, south, east, north) it covers
    url: str                    # GeoJSON download
    category_field: str         # feature property holding the category code
    classes: dict               # category code -> class
    attribution: str

    def covers(self, lat: float, lon: float) -> bool:
        w, s, e, n = self.bbox
        return s <= lat <= n and w <= lon <= e

    def meta(self) -> dict:
        return {"id": self.id, "name": self.name, "short": self.short, "bbox": list(self.bbox),
                "attribution": self.attribution}


SOURCES = [
    Source(
        id="brisbane",
        name="Brisbane City Council bicycle network",
        short="Brisbane's bike network",
        bbox=(152.79, -27.67, 153.21, -27.27),
        url=("https://data.brisbane.qld.gov.au/api/explore/v2.1/catalog/datasets/"
             "cp14-bicycle-network-overlay/exports/geojson"),
        category_field="ovl2_cat",
        classes={"CYC_PCR": PRIMARY, "CYC_SCR": SECONDARY, "CYC_LCR": LOCAL,
                 "CYC_RW1": RIVERWALK, "CYC_RW2": RIVERWALK, "CYC_RWF": RIVERWALK},
        attribution="Bicycle network: Brisbane City Council, City Plan 2014 (CC BY 4.0)",
    ),
]


def source_for(lat: float, lon: float) -> Source | None:
    """The cycle network covering a point, if any."""
    return next((s for s in SOURCES if s.covers(lat, lon)), None)


def get_source(source_id: str) -> Source | None:
    return next((s for s in SOURCES if s.id == source_id), None)


@dataclass
class Network:
    source: Source
    lines: list            # one (n, 2) array of lon/lat per route line
    classes: np.ndarray    # the class of each line
    version: str           # changes whenever the data does

    @classmethod
    def from_geojson(cls, src: Source, data: dict, version: str) -> "Network":
        lines, classes = [], []
        for feature in data.get("features") or []:
            props = {str(k).lower(): v for k, v in (feature.get("properties") or {}).items()}
            cls_code = src.classes.get(str(props.get(src.category_field.lower(), "")))
            geom = feature.get("geometry") or {}
            if not cls_code:
                continue
            parts = {"LineString": [geom.get("coordinates")],
                     "MultiLineString": geom.get("coordinates")}.get(geom.get("type"), [])
            for part in parts or []:
                try:
                    arr = np.asarray(part, dtype=np.float64)
                except (TypeError, ValueError):
                    continue
                if arr.ndim == 2 and arr.shape[0] >= 2 and arr.shape[1] >= 2:
                    lines.append(arr[:, :2])
                    classes.append(cls_code)
        if not lines:
            raise Unavailable(f"the {src.name} data had no cycle routes in it")
        return cls(src, lines, np.asarray(classes, dtype=np.int8), version)

    def display(self, decimals: int = 5) -> dict:
        """The network for drawing on the map: lines of [lat, lon] by class name."""
        by_class = {name: [] for name in CLASS_NAMES.values()}
        for line, c in zip(self.lines, self.classes):
            by_class[CLASS_NAMES[int(c)]].append(np.round(line[:, ::-1], decimals).tolist())
        return dict(self.source.meta(), classes=by_class)


def load(cache_dir: str, src: Source, refresh_days: float = REFRESH_DAYS) -> Network:
    """The source's network, downloaded once into cache/bikenet/ and refreshed now and then."""
    folder = os.path.join(cache_dir, "bikenet")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{src.id}.geojson")
    if not os.path.exists(path) or time.time() - os.path.getmtime(path) > refresh_days * 86400:
        try:
            _download(src, path)
        except Unavailable as exc:
            if not os.path.exists(path):
                raise
            print(f"Couldn't refresh the {src.name} ({exc}); using the copy already downloaded.")
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        data = json.loads(raw)
    except ValueError:
        raise Unavailable(f"the saved {src.name} file is damaged; delete {path} to download it again")
    return Network.from_geojson(src, data, hashlib.md5(raw).hexdigest()[:12])


def _download(src: Source, path: str) -> None:
    print(f"Downloading the {src.name} (one-off)...", flush=True)
    progress.report("bikenet")
    tmp = path + ".part"
    try:
        code, out, err = roads._run_cancellable(
            ["curl", "-sS", "--max-time", "300", "--connect-timeout", "20", "-A", roads.USER_AGENT,
             "-o", tmp, "-w", "%{http_code}", src.url])
        if code != 0 or out.strip() != "200":
            raise Unavailable(f"the download failed ({out.strip() or err.strip()})")
        try:
            with open(tmp, encoding="utf-8") as fh:
                if not json.load(fh).get("features"):
                    raise ValueError
        except (ValueError, AttributeError):
            raise Unavailable("the download wasn't the expected GeoJSON")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _densify(lines_xy: list, step: float):
    """Points about every `step` metres along each line, with the line's direction there.

    Returns (points (P, 2), unit directions (P, 2), index of the line each point is on (P,)).
    """
    pts, dirs, owner = [], [], []
    for i, c in enumerate(lines_xy):
        seg = np.diff(c, axis=0)
        length = np.hypot(seg[:, 0], seg[:, 1])
        keep = length > 0
        if not keep.any():
            continue
        start, seg, length = c[:-1][keep], seg[keep], length[keep]
        n = np.maximum(1, np.ceil(length / step).astype(int))
        which = np.repeat(np.arange(len(length)), n)
        t = (np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n) + 0.5) / n[which]  # middle of each piece
        pts.append(start[which] + seg[which] * t[:, None])
        dirs.append(seg[which] / length[which, None])
        owner.append(np.full(n.sum(), i))
    if not pts:
        return np.zeros((0, 2)), np.zeros((0, 2)), np.zeros(0, dtype=int)
    return np.concatenate(pts), np.concatenate(dirs), np.concatenate(owner)


def match(rg: roads.RoadGraph, net: Network, tol_m: float = MATCH_M, max_angle: float = MAX_ANGLE,
          min_share: float = MIN_SHARE) -> np.ndarray:
    """The network class of every road in the graph (0 where it isn't on the network).

    Each road is sampled every 10 m. A sample is on a route if a point of the route lies within
    tol_m and the route runs within max_angle of the road's direction there, so crossing streets
    don't count. A road is on the network if at least min_share of its samples are, and takes the
    class most of them matched.
    """
    n_edges = len(rg.edge_len)
    out = np.zeros(n_edges, dtype=np.int8)
    w, s, e, n = net.source.bbox
    mid_lat = (rg.node_lat[rg.edge_u] + rg.node_lat[rg.edge_v]) / 2
    mid_lon = (rg.node_lon[rg.edge_u] + rg.node_lon[rg.edge_v]) / 2
    pad = 0.01
    edges = np.where((mid_lat > s - pad) & (mid_lat < n + pad) & (mid_lon > w - pad) & (mid_lon < e + pad))[0]
    if not len(edges):
        return out
    proj = roads.Projector(*rg.center)
    bpts, bdir, bline = _densify([proj.xy(line[:, 1], line[:, 0]) for line in net.lines], 5.0)
    bcls = net.classes[bline]
    xy_all = proj.xy(rg.geom_lonlat[:, 1], rg.geom_lonlat[:, 0])
    spts, sdir, sown = _densify([xy_all[rg.geom_offsets[k]:rg.geom_offsets[k + 1]] for k in edges], 10.0)
    sedge = edges[sown]
    tree = cKDTree(bpts)
    min_cos = math.cos(math.radians(max_angle))
    scls = np.zeros(len(spts), dtype=np.int8)
    for lo in range(0, len(spts), 200_000):  # in chunks, to keep memory modest on big graphs
        hi = min(lo + 200_000, len(spts))
        d, idx = tree.query(spts[lo:hi], k=4, distance_upper_bound=tol_m)
        ok = np.isfinite(d)
        idx = np.where(ok, idx, 0)
        ok &= np.abs(np.einsum("sd,skd->sk", sdir[lo:hi], bdir[idx])) >= min_cos
        nearest = np.argmax(ok, axis=1)  # the closest point that lines up (query results are sorted)
        scls[lo:hi] = np.where(ok.any(axis=1), bcls[idx[np.arange(hi - lo), nearest]], NONE)
    total = np.bincount(sedge, minlength=n_edges)
    codes = np.array(list(CLASS_NAMES))
    counts = np.stack([np.bincount(sedge[scls == c], minlength=n_edges) for c in codes])
    share = np.divide(counts.sum(axis=0), total, out=np.zeros(n_edges), where=total > 0)
    out[:] = np.where(share >= min_share, codes[counts.argmax(axis=0)], NONE)
    return out


def edge_classes(rg: roads.RoadGraph, net: Network, cache_dir: str) -> np.ndarray:
    """match(), cached per graph and per version of the network data."""
    key = hashlib.md5(f"{net.source.id}|{net.version}|{rg.mode}|{rg.center}|{rg.radius_m}|{len(rg.edge_len)}|"
                      f"{MATCH_M}|{MAX_ANGLE}|{MIN_SHARE}".encode()).hexdigest()[:12]
    path = os.path.join(cache_dir, "bikenet", f"classes_{key}.npy")
    if os.path.exists(path):
        return np.load(path)
    print(f"Matching the {net.source.name} to the road network (one-off)...", flush=True)
    progress.report("bikenet")
    classes = match(rg, net)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".part", "wb") as fh:
        np.save(fh, classes)
    os.replace(path + ".part", path)
    return classes


def cost_factors(classes: np.ndarray, emphasis: float) -> np.ndarray:
    """Multipliers on each road's routing cost: network roads get cheaper as emphasis rises (0..1)."""
    return 1.0 - emphasis * COST_DISCOUNT[classes]


def value_factors(classes: np.ndarray, emphasis: float) -> np.ndarray:
    """Multipliers on the value of new road: new road on the network is worth more as emphasis rises."""
    return 1.0 + emphasis * VALUE_BONUS[classes]
