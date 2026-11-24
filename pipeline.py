"""The route-planning pipeline shared by the command line (newroutes.py) and the web app (webapp.py).

    ws = Workspace(resolve_export("~/Downloads/export_12345678.zip"))
    result = ws.plan(RouteRequest(mode="ride", distance_km=40))
    best = result.plans[0]  # best.length_m, best.new_m

A Workspace keeps your parsed activities and the road graphs it has loaded in
memory, so a long-running process such as the web app only pays for loading
them once. Progress is reported with print(), like the rest of the code.
"""
from __future__ import annotations

import json
import math
import os
import threading
from collections import Counter, OrderedDict
from dataclasses import dataclass

import numpy as np

import roads
import tracks as tracks_mod
from planner import Plan, PlanOptions, Planner

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CACHE = os.path.join(HERE, "cache")
MODES = ("ride", "run")
MAX_SNAP_M = 1000.0  # a start or finish further than this from a usable road is probably a mis-click


class PlannerError(Exception):
    """Something the user can fix: a missing export, bad input, or no possible route."""


# --- where the Strava export is ----------------------------------------------------


def _settings_file(cache_dir: str) -> str:
    return os.path.join(cache_dir, "settings.json")


def _read_settings(cache_dir: str) -> dict:
    try:
        with open(_settings_file(cache_dir)) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def remembered_export(cache_dir: str = DEFAULT_CACHE) -> str | None:
    return _read_settings(cache_dir).get("export") or None


def _remember_export(cache_dir: str, path: str) -> None:
    settings = _read_settings(cache_dir)
    if settings.get("export") == path:
        return
    settings["export"] = path
    os.makedirs(cache_dir, exist_ok=True)
    with open(_settings_file(cache_dir), "w") as fh:
        json.dump(settings, fh, indent=2)


def resolve_export(path: str | None = None, cache_dir: str = DEFAULT_CACHE) -> str:
    """Return the unzipped Strava export folder, extracting a .zip on first use.

    Uses, in order: `path`, the STRAVA_EXPORT environment variable, or the export
    used last time (remembered in cache/settings.json, which is git-ignored).
    """
    given = path or os.environ.get("STRAVA_EXPORT")
    path = given or remembered_export(cache_dir)
    if not path:
        raise PlannerError(
            "Tell me where your Strava export is: --export /path/to/export_12345 "
            "(the unzipped folder or the .zip), or set STRAVA_EXPORT. See README.md.")
    path = os.path.abspath(os.path.expanduser(path))
    if path.lower().endswith(".zip") and os.path.isfile(path):
        folder = path[:-4]
        if not os.path.exists(os.path.join(folder, "activities.csv")):
            import zipfile
            print(f"Unzipping {os.path.basename(path)} (once)...")
            with zipfile.ZipFile(path) as zf:
                zf.extractall(folder)
        path = folder
    if not os.path.exists(os.path.join(path, "activities.csv")):
        if not given:
            raise PlannerError(f"The Strava export used last time ({path}) isn't there any more. "
                               "Pass --export with its new location. See README.md.")
        raise PlannerError(f"No activities.csv in {path} - is this the folder from Strava's account "
                           "download? See README.md.")
    if given:
        _remember_export(cache_dir, path)
    return path


def common_start(trks) -> tuple[float, float] | None:
    """Most frequent start location (≈200 m grid) among your activities; None without any."""
    if not trks:
        return None

    def cell(t):
        return round(float(t.latlon[0][0]) / 0.002), round(float(t.latlon[0][1]) / 0.002)

    best, _ = Counter(map(cell, trks)).most_common(1)[0]
    pts = np.array([t.latlon[0] for t in trks if cell(t) == best], dtype=np.float64)
    return float(pts[:, 0].mean()), float(pts[:, 1].mean())


# --- requests and results ------------------------------------------------------------


@dataclass
class RouteRequest:
    mode: str = "ride"                         # "ride" or "run"
    distance_km: float = 40.0
    start: tuple[float, float] | None = None   # (lat, lon); None = your most common start point
    end: tuple[float, float] | None = None     # (lat, lon) to finish elsewhere; None = loop
    alternatives: int = 1                      # distinct routes to return, best first
    tolerance: float = 0.05                    # the route may run this much over the distance
    eagerness: float = 0.6                     # 0..0.95: how hard connecting legs chase new roads
    avoid_main_roads: float = 1.0              # 0 = ignore road type, 1 = default, 2 = avoid strongly
    restarts: int = 8                          # search effort
    iterations: int = 40
    seed: int | None = None                    # set for reproducible results
    count_all_activities: bool = False         # roads from any activity type count as traveled

    def validate(self) -> None:
        if self.mode not in MODES:
            raise PlannerError(f"Mode must be one of: {', '.join(MODES)}.")
        if not 0.5 <= self.distance_km <= 400:
            raise PlannerError("Distance must be between 0.5 and 400 km.")
        for label, p in (("start", self.start), ("finish", self.end)):
            if p is not None and not (-90 <= p[0] <= 90 and -180 <= p[1] <= 180):
                raise PlannerError(f"The {label} isn't a valid latitude/longitude.")
        limits = [("alternatives", self.alternatives, 1, 5), ("tolerance", self.tolerance, 0, 0.5),
                  ("eagerness", self.eagerness, 0, 0.95), ("avoid_main_roads", self.avoid_main_roads, 0, 3),
                  ("restarts", self.restarts, 1, 64), ("iterations", self.iterations, 1, 500)]
        for name, value, lo, hi in limits:
            if not lo <= value <= hi:
                raise PlannerError(f"{name} must be between {lo} and {hi}.")


@dataclass
class RouteResult:
    request: RouteRequest
    options: PlanOptions
    start: tuple[float, float]
    end: tuple[float, float] | None   # None for a loop
    graph: roads.RoadGraph
    new_len: np.ndarray               # untraveled metres of each road: what the search maximised
    plans: list[Plan]                 # best first
    activities_used: int
    area: dict                        # how much of the road network within reach is already traveled


def area_stats(rg: roads.RoadGraph, frac: np.ndarray, start, end, distance_m: float) -> dict:
    """Traveled vs total road length everywhere a route of this length could reach.

    That's an ellipse with the start and finish as foci; for a loop, a circle of
    half the distance around the start.
    """
    proj = roads.Projector(*rg.center)
    nxy = proj.xy(rg.node_lat, rg.node_lon)
    mid = (nxy[rg.edge_u] + nxy[rg.edge_v]) / 2
    s, e = proj.xy(*start), proj.xy(*(end or start))
    within = ((np.hypot(*(mid - s).T) + np.hypot(*(mid - e).T)) <= distance_m) & rg.edge_target
    total = float(rg.edge_len[within].sum())
    done = float((rg.edge_len * frac)[within].sum())
    return {"total_km": total / 1000, "traveled_km": done / 1000,
            "traveled_pct": 100 * done / max(total, 1.0)}


# --- the workspace ---------------------------------------------------------------------


class Workspace:
    """Your activities, plus the road graphs loaded so far, kept in memory between routes."""

    def __init__(self, export_dir: str | None, cache_dir: str = DEFAULT_CACHE, keep_graphs: int = 3):
        self.export_dir = export_dir  # None: no history, so every road counts as new
        self.cache_dir = cache_dir
        self.keep_graphs = keep_graphs
        os.makedirs(cache_dir, exist_ok=True)
        self._lock = threading.RLock()
        self._tracks = None
        self._usual: dict[str, tuple[float, float] | None] = {}
        self._graphs: OrderedDict[str, roads.RoadGraph] = OrderedDict()

    def all_tracks(self) -> list:
        with self._lock:
            if self._tracks is None:
                self._tracks = (tracks_mod.load_tracks(self.export_dir, self.cache_dir)
                                if self.export_dir else [])
            return self._tracks

    def tracks_for(self, mode: str, count_all: bool = False) -> list:
        trks = self.all_tracks()
        return trks if count_all else tracks_mod.tracks_for_mode(trks, mode)

    def usual_start(self, mode: str) -> tuple[float, float] | None:
        with self._lock:
            if mode not in self._usual:
                self._usual[mode] = common_start(self.tracks_for(mode))
            return self._usual[mode]

    def graph_for(self, mode: str, start, end, distance_m: float, tolerance: float) -> roads.RoadGraph:
        """The road graph covering every route of this length, downloaded first if need be."""
        center = start if end is None else ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
        need = distance_m * (1 + tolerance) / 2 + 1500
        path = roads.find_cached_graph(self.cache_dir, mode, center[0], center[1], need)
        with self._lock:
            if path in self._graphs:
                self._graphs.move_to_end(path)
                return self._graphs[path]
        if path:
            rg = roads.load_graph(self.cache_dir, mode, center[0], center[1], need)
        else:
            # round so nearby future requests reuse the same downloaded graph
            c_lat, c_lon = round(center[0], 2), round(center[1], 2)
            radius = math.ceil((need + 1500) / 5000.0) * 5000.0
            rg = roads.load_graph(self.cache_dir, mode, c_lat, c_lon, radius)
            path = (roads.find_cached_graph(self.cache_dir, mode, c_lat, c_lon, radius)
                    or f"{mode}:{c_lat}:{c_lon}:{radius}")
        with self._lock:
            self._graphs[path] = rg
            while len(self._graphs) > self.keep_graphs:
                self._graphs.popitem(last=False)
        return rg

    def plan(self, req: RouteRequest) -> RouteResult:
        req.validate()
        used = self.tracks_for(req.mode, req.count_all_activities)
        print(f"{len(used)} activities count towards 'traveled' "
              f"({'all types' if req.count_all_activities else req.mode + ' activities'}).")

        start = req.start
        if start is None:
            start = self.usual_start(req.mode)
            if start is None:
                raise PlannerError(f"Choose a start point: there are no {req.mode} activities "
                                   "to find your usual start from.")
            print(f"Start: your most common {req.mode} start point ({start[0]:.4f},{start[1]:.4f})")
        start = (float(start[0]), float(start[1]))
        end = None if req.end is None else (float(req.end[0]), float(req.end[1]))
        D = req.distance_km * 1000.0
        if end is not None:
            gap = roads.haversine_m(*start, *end)
            if gap < 50:
                end = None  # finishing where you start is a loop
            elif gap >= D * (1 + req.tolerance):
                raise PlannerError(
                    f"The finish is {gap / 1000:.1f} km from the start in a straight line, so a "
                    f"{req.distance_km:g} km route can't reach it. Pick a closer finish, or a longer "
                    f"distance: roads wind, so try {math.ceil(gap * 1.3 / 1000)} km or more.")

        rg = self.graph_for(req.mode, start, end, D, req.tolerance)
        print(f"Road network: {rg.n_nodes:,} junctions, {len(rg.edge_len):,} road segments, "
              f"{rg.edge_len.sum() / 1000:,.0f} km")
        frac = roads.compute_coverage(rg, used, self.cache_dir)
        area = area_stats(rg, frac, start, end, D)
        where = f"Within {D / 2000:.0f} km of start" if end is None else "Within reach of start and finish"
        print(f"{where}: {area['traveled_km']:,.0f} of {area['total_km']:,.0f} km traveled "
              f"({area['traveled_pct']:.1f}%)")

        opts = PlanOptions(distance_m=D, tolerance=req.tolerance, eagerness=req.eagerness,
                           avoid_main_roads=req.avoid_main_roads, restarts=req.restarts,
                           iterations=req.iterations, seed=req.seed)
        planner = Planner(rg, frac, opts)
        s_node = self._snap(planner, rg, start, "start", req.mode)
        e_node = self._snap(planner, rg, end, "finish", req.mode) if end else None
        print("Searching for the route with the most new road...")
        plans = planner.plan(s_node, e_node, alternatives=req.alternatives)
        if not plans:
            if e_node is not None and e_node != s_node:
                if planner.direct_m is None:
                    raise PlannerError(f"Couldn't find a way from the start to the finish on the {req.mode} "
                                       f"network within {req.distance_km:g} km. Try a longer distance.")
                raise PlannerError(
                    f"The most direct way from start to finish is about {planner.direct_m / 1000:.1f} km by "
                    f"road, so a {req.distance_km:g} km route can't get there. Try "
                    f"{math.ceil(planner.direct_m * 1.2 / 1000)} km or more.")
            raise PlannerError("No route found - try a larger distance or another start point.")
        return RouteResult(request=req, options=opts, start=start, end=end, graph=rg,
                           new_len=planner.new_len, plans=plans, activities_used=len(used), area=area)

    @staticmethod
    def _snap(planner: Planner, rg: roads.RoadGraph, point, label: str, mode: str) -> int:
        """Nearest usable junction to a point, refusing points far from any road."""
        n = planner.nearest_node(*point)
        gap = roads.haversine_m(point[0], point[1], float(rg.node_lat[n]), float(rg.node_lon[n]))
        if gap > MAX_SNAP_M:
            raise PlannerError(f"The {label} is {gap / 1000:.1f} km from the nearest road you can "
                               f"{mode} on. Move it closer to a road.")
        return n
