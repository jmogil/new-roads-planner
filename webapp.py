#!/usr/bin/env python3
"""Point-and-click web app for the new roads planner.

    .venv/bin/python webapp.py --export ~/Downloads/export_12345678.zip

then use the page that opens at http://127.0.0.1:8765. The export is remembered,
so next time `.venv/bin/python webapp.py` is enough.

Everything runs on your computer: the page only talks to this local server,
which runs the same planner as newroutes.py. The outside services involved are
map tiles (OpenStreetMap, and Esri for the aerial view), road data downloads
(Overpass), elevation data (the AWS Terrain Tiles open dataset) and place search
(Nominatim). Only Python's standard library is needed on top of requirements.txt.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import queue
import re
import sys
import threading
import time
import traceback
import uuid
import warnings
import webbrowser
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

warnings.filterwarnings("ignore", message=".*OpenSSL.*")

import numpy as np  # noqa: E402

import bikenet  # noqa: E402
import elevation  # noqa: E402
import export  # noqa: E402
import pipeline  # noqa: E402
import progress  # noqa: E402
import roads  # noqa: E402
import saved  # noqa: E402
import planner  # noqa: E402

VERSION = "0.4.0"
HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")
KEEP_JOBS = 12          # finished searches kept for GPX downloads (each holds its routes)
MAX_BODY = 64 * 1024
EFFORT = {"quick": (4, 20), "normal": (8, 40), "thorough": (16, 80)}  # (restarts, iterations)
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
FINISHED = ("done", "error", "cancelled")
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png"}
CSP = ("default-src 'self'; script-src 'self' https://unpkg.com; "
       "style-src 'self' 'unsafe-inline' https://unpkg.com; "
       "img-src 'self' data: https://tile.openstreetmap.org https://server.arcgisonline.com https://unpkg.com; "
       "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


# --- route searches ("jobs") run one at a time in a background thread -------------------


class Job:
    def __init__(self, request: pipeline.RouteRequest):
        self.id = uuid.uuid4().hex[:12]
        self.request = request
        self.status = "queued"  # -> running -> done | error | cancelled
        self.log: list[str] = []
        self.created = time.time()
        self.started: float | None = None
        self.finished: float | None = None
        self.result: pipeline.RouteResult | None = None
        self.summary: dict | None = None
        self.error: str | None = None
        self.context: bytes | None = None
        self.cancel_event = threading.Event()
        self.stage: str | None = None  # what the planner is doing, from progress.report()
        self.done: float | None = None
        self.total: float | None = None
        self.stage_started = self.last_step_at = 0.0
        self.stage_done0 = 0.0
        self.typical_tile_s = roads.DEFAULT_TILE_SECONDS
        self._partial = ""

    def write(self, text: str) -> None:
        """Receives everything the planner print()s while working on this job."""
        self._partial += text
        if "\n" in self._partial:
            *lines, self._partial = self._partial.split("\n")
            self.log.extend(s.strip() for s in lines if s.strip())
            del self.log[:-200]

    def on_progress(self, stage: str, done, total) -> None:
        now = time.time()
        if stage != self.stage:
            self.stage, self.stage_started, self.last_step_at = stage, now, now
            self.stage_done0 = done or 0
        elif done is not None and self.done is not None and done > self.done:
            self.last_step_at = now
        self.done, self.total = done, total

    def eta_s(self) -> float | None:
        """Rough seconds left in the current stage, where that can be judged."""
        if self.total is None or self.done is None:
            return None
        now, left = time.time(), self.total - self.done
        if self.stage == "download":
            finished_here = self.done - self.stage_done0
            per_tile = ((self.last_step_at - self.stage_started) / finished_here if finished_here > 0
                        else self.typical_tile_s)
            in_progress = now - self.last_step_at  # already spent on the tile being fetched
            return max(left * per_tile - in_progress, 0) + self.total * pipeline.BUILD_SECONDS_PER_TILE
        if self.stage == "search" and self.done > 0:
            return left * (now - self.stage_started) / self.done
        return None

    def status_json(self) -> dict:
        now = self.finished or time.time()
        out = {"id": self.id, "status": self.status, "log": self.log[-40:],
               "elapsed_s": round(now - (self.started or now), 1), "progress": None}
        if self.stage and self.status == "running":
            eta = self.eta_s()
            out["progress"] = {"stage": self.stage, "done": self.done, "total": self.total,
                               "eta_s": None if eta is None else round(eta)}
        if self.status == "done":
            out["result"] = self.summary
        elif self.status == "error":
            out["error"] = self.error
        elif self.status == "cancelled":
            out["stage"] = self.stage  # what it was doing when stopped
        return out


class ThreadTee(io.TextIOBase):
    """Stands in for sys.stdout: passes everything through, and copies what the
    planner thread prints into the job it is working on, for the page's progress view."""

    def __init__(self, real):
        self.real = real
        self.local = threading.local()

    def write(self, s):
        job = getattr(self.local, "job", None)
        if job is not None:
            job.write(s)
        return self.real.write(s)

    def flush(self):
        self.real.flush()

    @property
    def encoding(self):
        return getattr(self.real, "encoding", "utf-8")

    def isatty(self):
        return self.real.isatty()

    def fileno(self):
        return self.real.fileno()


class App:
    def __init__(self, ws: pipeline.Workspace, export_problem: str | None = None):
        self.ws = ws
        self.export_problem = export_problem
        self.elevation = elevation.Elevation(ws.cache_dir)
        self.saved = saved.SavedRoutes(ws.cache_dir)
        self.bike_display: dict[str, bytes] = {}  # cycle networks ready for the map overlay
        self.jobs: OrderedDict[str, Job] = OrderedDict()
        self.lock = threading.Lock()
        self.queue: queue.Queue[Job] = queue.Queue()
        self.geocode_lock = threading.Lock()
        self.last_geocode = 0.0
        if not isinstance(sys.stdout, ThreadTee):
            sys.stdout = ThreadTee(sys.stdout)
        self.tee = sys.stdout
        # a daemon thread, so Ctrl+C stops the app at once even mid-download (downloads resume)
        threading.Thread(target=self._worker, name="planner", daemon=True).start()

    def submit(self, req: pipeline.RouteRequest) -> Job:
        job = Job(req)
        with self.lock:
            self.jobs[job.id] = job
            finished = [j for j in self.jobs.values() if j.status in FINISHED]
            for old in finished[:max(0, len(self.jobs) - KEEP_JOBS)]:
                del self.jobs[old.id]
        self.queue.put(job)
        return job

    def job(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise ApiError(404, "That route search has expired. Generate the routes again.")
        return job

    def finished_job(self, job_id: str) -> Job:
        job = self.job(job_id)
        if job.status != "done":
            raise ApiError(409, "That route search hasn't finished.")
        return job

    def active_job(self) -> str | None:
        """The search being worked on (or waiting), so a reloaded page can pick it up again."""
        for j in list(self.jobs.values()):
            if j.status in ("queued", "running"):
                return j.id
        return None

    def cancel(self, job_id: str) -> dict:
        job = self.job(job_id)
        if job.status == "queued":
            job.status, job.finished = "cancelled", time.time()
        elif job.status == "running":
            job.cancel_event.set()  # the planner stops at its next check, within a second or so
        return job.status_json()

    def _worker(self):
        while True:
            job = self.queue.get()
            if job.status == "queued":  # skip searches cancelled while they waited
                self._run(job)

    def _run(self, job: Job):
        self.tee.local.job = job
        job.status, job.started = "running", time.time()
        job.typical_tile_s = roads.typical_tile_seconds(self.ws.cache_dir)
        try:
            with progress.track(job.on_progress, job.cancel_event):
                job.result = self.ws.plan(job.request)
                job.summary = summarise(job, self.elevation)
            job.status = "done"
        except progress.Cancelled:
            print("Search cancelled.")
            job.status = "cancelled"
        except pipeline.PlannerError as exc:
            job.error, job.status = str(exc), "error"
        except Exception as exc:  # show it on the page, with the details in the terminal
            traceback.print_exc()
            job.error = (f"Something went wrong ({type(exc).__name__}: {exc}). "
                         "The terminal running webapp.py has the details.")
            job.status = "error"
        finally:
            job.finished = time.time()
            self.tee.local.job = None


# --- JSON payloads ---------------------------------------------------------------------


def parse_point(value, label):
    if value is None:
        return None
    try:
        lat, lon = float(value["lat"]), float(value["lon"])
    except (TypeError, KeyError, ValueError):
        raise ApiError(400, f'The {label} should look like {{"lat": 51.5007, "lon": -0.1246}}.')
    if not (math.isfinite(lat) and math.isfinite(lon) and abs(lat) <= 90 and abs(lon) <= 180):
        raise ApiError(400, f"The {label} isn't a valid latitude/longitude.")
    return lat, lon


def parse_route_request(body: dict) -> pipeline.RouteRequest:
    def number(name, default, cast=float):
        try:
            value = cast(body.get(name, default))
        except (TypeError, ValueError):
            raise ApiError(400, f"{name} should be a number.")
        if not math.isfinite(value):
            raise ApiError(400, f"{name} should be a number.")
        return value

    effort = body.get("effort", "normal")
    if effort not in EFFORT:
        raise ApiError(400, f"effort should be one of: {', '.join(EFFORT)}.")
    restarts, iterations = EFFORT[effort]
    req = pipeline.RouteRequest(
        mode=str(body.get("mode", "ride")),
        distance_km=number("distance_km", 40.0),
        start=parse_point(body.get("start"), "start"),
        end=parse_point(body.get("end"), "finish"),
        alternatives=number("alternatives", 3, int),
        eagerness=number("eagerness", 0.6),
        avoid_main_roads=number("avoid_main_roads", 1.0),
        bike_network=number("bike_network", 0.0),
        restarts=restarts, iterations=iterations,
        count_all_activities=bool(body.get("count_all_activities", False)),
    )
    try:
        req.validate()
    except pipeline.PlannerError as exc:
        raise ApiError(400, str(exc))
    return req


def elevation_profile(segments: list[dict], elev: elevation.Elevation, download: bool = True) -> dict | None:
    try:
        return elevation.profile(segments, elev, download=download)
    except (elevation.ElevationUnavailable, OSError, ValueError) as exc:  # not Cancelled: that stops the search
        print(f"No elevation for this route: {exc}.")
        return None


def route_json(job: Job, i: int, elev: elevation.Elevation, download: bool = True) -> dict:
    """One route as the page draws it: split into new and done runs, with its elevation."""
    res = job.result
    plan = res.plans[i]
    segments = export.route_segments(res.graph, plan, res.new_len)
    lats = [p[0] for s in segments for p in s["coords"]]
    lons = [p[1] for s in segments for p in s["coords"]]
    profile = elevation_profile(segments, elev, download)
    km, new_km = plan.length_m / 1000, plan.new_m / 1000
    bike_m = res.bike_m(plan)
    return {
        "index": i,
        "distance_km": round(km, 2),
        "new_km": round(new_km, 2),
        "new_pct": round(100 * new_km / km) if km else 0,
        "ascent_m": profile["ascent_m"] if profile else None,
        "bike_km": None if bike_m is None else round(bike_m / 1000, 2),
        "bike_pct": None if bike_m is None else (round(100 * bike_m / plan.length_m) if plan.length_m else 0),
        "targets": len(plan.waypoints),
        "bounds": [[min(lats), min(lons)], [max(lats), max(lons)]],
        "segments": segments,
        "profile": profile,
        "gpx_url": f"/api/jobs/{job.id}/routes/{i}.gpx",
    }


def summarise(job: Job, elev: elevation.Elevation) -> dict:
    res = job.result
    routes = []
    for i in range(len(res.plans)):
        if i == 0:
            print("Adding elevation...")
        routes.append(route_json(job, i, elev))
    req = res.request
    return {
        "job": job.id,
        "mode": req.mode,
        "route_type": "loop" if res.end is None else "oneway",
        "distance_km": req.distance_km,
        "start": list(res.start),
        "end": list(res.end) if res.end else None,
        "activities_used": res.activities_used,
        "area": {k: round(v, 1) for k, v in res.area.items()},
        "bike_network": None if res.bike_source is None else dict(
            res.bike_source.meta(), emphasis=res.options.bike_network),
        "routes": routes,
        "context_url": f"/api/jobs/{job.id}/context",
    }


def context_json(job: Job) -> bytes:
    """Untraveled and traveled roads around the routes, for the map's background layers."""
    if job.context is None:
        lat = [b[0] for r in job.summary["routes"] for b in r["bounds"]]
        lon = [b[1] for r in job.summary["routes"] for b in r["bounds"]]
        pad = 0.01
        bounds = ((min(lat) - pad, min(lon) - pad), (max(lat) + pad, max(lon) + pad))
        roads_near = export.nearby_roads(job.result.graph, job.result.new_len, bounds)
        job.context = json.dumps(roads_near, separators=(",", ":")).encode()
    return job.context


def route_index(job: Job, index: int) -> int:
    if not 0 <= index < len(job.result.plans):
        raise ApiError(404, "No such route.")
    return index


def gpx_text(job: Job, index: int, elev: elevation.Elevation) -> tuple[str, str]:
    """(file name, GPX) for one of a search's routes, with elevation if it's been downloaded."""
    res = job.result
    plan, mode = res.plans[route_index(job, index)], res.request.mode
    km, new_km = plan.length_m / 1000, plan.new_m / 1000
    option = "" if index == 0 else f" #{index + 1}"
    xml = export.gpx_xml(res.graph, plan, name=f"New roads {mode} {km:.1f} km ({new_km:.1f} new){option}",
                         heights=lambda lats, lons: elev.heights(lats, lons, download=False))
    stamp = time.strftime("%Y%m%d-%H%M", time.localtime(job.created))
    return f"{mode}_{km:.0f}km_{stamp}_option{index + 1}.gpx", xml


def reverse_route(job: Job, index: int, elev: elevation.Elevation) -> dict:
    """Turn a loop around, going round any one-way stretches by the shortest legal detour."""
    res = job.result
    index = route_index(job, index)
    if res.end is not None:
        raise ApiError(400, "Only loops can be reversed. For an A → B route, swap the start and finish "
                            "and generate again.")
    try:
        plan, extra_m = planner.reverse_loop(res.graph, res.plans[index], res.new_len)
    except planner.OneWay as exc:
        raise ApiError(409, f"This loop can't be ridden the other way: {exc} is one-way, with no short way round.")
    res.plans[index] = plan
    route = route_json(job, index, elev, download=False)
    job.summary["routes"][index] = route
    return dict(route, detour_km=round(extra_m / 1000, 2))


def save_route(app: App, body: dict) -> dict:
    job = app.finished_job(str(body.get("job", "")))
    try:
        index = route_index(job, int(body.get("index", -1)))
    except (TypeError, ValueError):
        raise ApiError(400, "index should be a number.")
    route, summary = job.summary["routes"][index], job.summary
    _, gpx = gpx_text(job, index, app.elevation)
    default = (f"{summary['mode'].title()} {'loop' if summary['route_type'] == 'loop' else 'A → B'} · "
               f"{route['distance_km']:.1f} km")
    record = {
        "name": saved.clean_name(body.get("name")) or default,
        "mode": summary["mode"], "route_type": summary["route_type"],
        "distance_km": route["distance_km"], "new_km": route["new_km"], "new_pct": route["new_pct"],
        "ascent_m": route["ascent_m"], "bike_km": route.get("bike_km"), "bike_pct": route.get("bike_pct"),
        "start": summary["start"], "end": summary["end"],
        "bounds": route["bounds"], "segments": route["segments"], "profile": route["profile"],
        "job": job.id, "index": index,
    }
    return app.saved.add(record, gpx)


def bike_network_json(app: App, source_id: str) -> bytes:
    """A cycle network for the map overlay, downloaded on first use."""
    source = bikenet.get_source(source_id)
    if source is None:
        raise ApiError(404, "No such cycle network.")
    with app.lock:
        cached = app.bike_display.get(source_id)
    if cached is None:
        try:
            net = app.ws.bike_network(source)
        except bikenet.Unavailable as exc:
            raise ApiError(502, f"The {source.name} isn't available right now ({exc}).")
        cached = json.dumps(net.display(), separators=(",", ":")).encode()
        with app.lock:
            app.bike_display[source_id] = cached
    return cached


def config_json(app: App) -> dict:
    ws = app.ws
    usual = {}
    for mode in pipeline.MODES:
        p = ws.usual_start(mode)
        usual[mode] = {"lat": p[0], "lon": p[1]} if p else None
    return {
        "version": VERSION,
        "export": ws.export_dir,
        "export_problem": app.export_problem,
        "activities": {mode: len(ws.tracks_for(mode)) for mode in pipeline.MODES},
        "usual_start": usual,
        "areas": [{k: a[k] for k in ("mode", "lat", "lon", "radius_m")} for a in roads.cached_areas(ws.cache_dir)],
        "bike_networks": [source.meta() for source in bikenet.SOURCES],
        "active_job": app.active_job(),
    }


def estimate_json(app: App, query: dict) -> dict:
    def arg(name):
        return (query.get(name) or [""])[0].strip()

    def point(name, label):
        text = arg(name)
        if not text:
            return None
        try:
            lat, lon = (float(v) for v in text.split(","))
        except ValueError:
            raise ApiError(400, f"{name} should look like 51.5007,-0.1246.")
        return parse_point({"lat": lat, "lon": lon}, label)

    mode = arg("mode")
    if mode not in pipeline.MODES:
        raise ApiError(400, f"mode should be one of: {', '.join(pipeline.MODES)}.")
    try:
        distance = float(arg("distance_km"))
    except ValueError:
        raise ApiError(400, "distance_km should be a number.")
    start = point("start", "start")
    if start is None or not (math.isfinite(distance) and 0.5 <= distance <= 400):
        raise ApiError(400, "Give a start and a distance between 0.5 and 400 km.")
    return app.ws.estimate(mode, start, point("end", "finish"), distance)


def geocode_json(app: App, query: dict) -> dict:
    q = (query.get("q") or [""])[0].strip()
    if not q or len(q) > 200:
        raise ApiError(400, "Type a place or address to search for.")
    viewbox = None
    try:
        parts = [float(x) for x in (query.get("viewbox") or [""])[0].split(",")]
        if len(parts) == 4 and all(map(math.isfinite, parts)):
            viewbox = parts
    except ValueError:
        pass
    with app.geocode_lock:  # Nominatim's usage policy: at most one request per second
        time.sleep(max(0.0, 1.0 - (time.time() - app.last_geocode)))
        try:
            results = roads.search_places(q, limit=5, viewbox=viewbox)
        except Exception as exc:
            print(f"Place search failed: {exc}", file=sys.stderr)
            raise ApiError(502, "Place search (OpenStreetMap Nominatim) isn't answering. "
                                "Try again, or click the map instead.")
        finally:
            app.last_geocode = time.time()
    return {"results": results}


def _json_default(o):
    if isinstance(o, np.generic):
        return o.item()
    raise TypeError(f"{type(o).__name__} is not JSON serializable")


# --- HTTP ----------------------------------------------------------------------------

JOB = r"/api/jobs/([0-9a-f]{12})"
SAVED = r"/api/saved/([0-9a-f]{12})"


class Handler(BaseHTTPRequestHandler):
    server_version = f"NewRoadsPlanner/{VERSION}"

    @property
    def app(self) -> App:
        return self.server.app

    def do_GET(self):
        self._handle(self._get)

    def do_POST(self):
        self._handle(self._post)

    def do_PATCH(self):
        self._handle(self._patch)

    def do_DELETE(self):
        self._handle(self._delete)

    def _handle(self, fn):
        try:
            self._check_host()
            fn(urlparse(self.path))
        except ApiError as exc:
            self._json({"error": exc.message}, exc.status)
        except saved.NotFound:
            self._json({"error": "That saved route doesn't exist any more."}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            self._json({"error": "Internal error. The terminal running webapp.py has the details."}, 500)

    def _get(self, url):
        app, path = self.app, url.path
        if path == "/api/config":
            return self._json(config_json(app))
        if path == "/api/geocode":
            return self._json(geocode_json(app, parse_qs(url.query)))
        if path == "/api/estimate":
            return self._json(estimate_json(app, parse_qs(url.query)))
        if path == "/api/saved":
            return self._json({"routes": app.saved.list()})
        if m := re.fullmatch(r"/api/bike-network/([a-z0-9-]{1,40})", path):
            return self._send(200, bike_network_json(app, m.group(1)), "application/json", cache="private, max-age=3600")
        if m := re.fullmatch(JOB, path):
            return self._json(app.job(m.group(1)).status_json())
        if m := re.fullmatch(JOB + "/context", path):
            return self._send(200, context_json(app.finished_job(m.group(1))), "application/json")
        if m := re.fullmatch(JOB + r"/routes/(\d{1,2})\.gpx", path):
            filename, xml = gpx_text(app.finished_job(m.group(1)), int(m.group(2)), app.elevation)
            return self._download(filename, xml)
        if m := re.fullmatch(SAVED, path):
            return self._json(app.saved.get(m.group(1)))
        if m := re.fullmatch(SAVED + r"\.gpx", path):
            return self._download(saved.file_name(app.saved.get(m.group(1))["name"]), app.saved.gpx(m.group(1)))
        if path.startswith("/api/"):
            raise ApiError(404, "Not found.")
        return self._static(path)

    def _post(self, url):
        app, path = self.app, url.path
        if path == "/api/plan":
            job = app.submit(parse_route_request(self._read_json()))
            return self._json({"job": job.id}, 202)
        if m := re.fullmatch(JOB + "/cancel", path):
            return self._json(app.cancel(m.group(1)))
        if m := re.fullmatch(JOB + r"/routes/(\d{1,2})/reverse", path):
            return self._json(reverse_route(app.finished_job(m.group(1)), int(m.group(2)), app.elevation))
        if path == "/api/saved":
            return self._json(save_route(app, self._read_json()), 201)
        raise ApiError(404, "Not found.")

    def _patch(self, url):
        m = re.fullmatch(SAVED, url.path)
        if not m:
            raise ApiError(404, "Not found.")
        body = self._read_json()
        done = body.get("done")
        if done is not None and not isinstance(done, bool):
            raise ApiError(400, "done should be true or false.")
        return self._json(self.app.saved.update(m.group(1), name=body.get("name"), done=done))

    def _delete(self, url):
        m = re.fullmatch(SAVED, url.path)
        if not m:
            raise ApiError(404, "Not found.")
        self.app.saved.delete(m.group(1))
        return self._json({"deleted": m.group(1)})

    # -- safety: answer only pages served from this computer --------------------------

    def _check_host(self):
        host = (self.headers.get("Host") or "").strip().lower()
        name = host[:host.find("]") + 1] if host.startswith("[") else host.split(":")[0]
        allowed = self.server.allowed_hosts  # None when listening on every interface, by request
        if allowed is not None and name not in allowed:  # e.g. DNS rebinding from a web page
            raise ApiError(403, "Unexpected Host header.")
        origin = self.headers.get("Origin")  # browsers send it on cross-site requests
        if origin and urlparse(origin).netloc.lower() != host:
            raise ApiError(403, "Requests from other websites aren't allowed.")

    def _read_json(self) -> dict:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":  # also forces a CORS preflight on cross-site requests
            raise ApiError(415, "Send JSON with Content-Type: application/json.")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 < n <= MAX_BODY:
            raise ApiError(400, "Missing or oversized request body.")
        try:
            body = json.loads(self.rfile.read(n))
        except ValueError:
            raise ApiError(400, "That isn't valid JSON.")
        if not isinstance(body, dict):
            raise ApiError(400, "Expected a JSON object.")
        return body

    # -- responses -------------------------------------------------------------------

    def _static(self, path):
        aliases = {"": "index.html", "/": "index.html", "/favicon.ico": "favicon.svg"}
        rel = aliases.get(path) or path.lstrip("/")
        root = os.path.realpath(WEB_DIR)
        full = os.path.realpath(os.path.join(root, rel))
        ctype = CONTENT_TYPES.get(os.path.splitext(full)[1].lower())
        if not full.startswith(root + os.sep) or not ctype or not os.path.isfile(full):
            raise ApiError(404, "Not found.")
        with open(full, "rb") as fh:
            body = fh.read()
        headers = {"Content-Security-Policy": CSP} if ctype.startswith("text/html") else {}
        self._send(200, body, ctype, headers, cache="no-cache")

    def _download(self, filename: str, text: str):
        self._send(200, text.encode(), "application/gpx+xml",
                   {"Content-Disposition": f'attachment; filename="{filename}"'})

    def _json(self, obj, status=200):
        body = json.dumps(obj, separators=(",", ":"), default=_json_default).encode()
        self._send(status, body, "application/json")

    def _send(self, status, body: bytes, ctype: str, headers=None, cache="no-store"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_request(self, code="-", size="-"):
        # the page polls every second; only log problems
        if str(code).isdigit() and int(code) >= 400:
            super().log_request(code, size)


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.app = app
    if host in ("0.0.0.0", "::", ""):
        server.allowed_hosts = None
    else:
        server.allowed_hosts = LOCAL_HOSTS | {host.lower() if ":" not in host else f"[{host.lower()}]"}
    return server


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--export", "--export-dir", dest="export",
                    help="your Strava bulk export: unzipped folder or .zip (remembered after first use)")
    ap.add_argument("--port", type=int, default=8765, help="port to serve on (default 8765)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="address to listen on (default 127.0.0.1: this computer only). 0.0.0.0 lets "
                         "other devices on your network use it, with no password, so only do that "
                         "on a network you trust.")
    ap.add_argument("--no-browser", action="store_true", help="don't open the page in a browser")
    args = ap.parse_args(argv)

    problem = None
    try:
        export_dir = pipeline.resolve_export(args.export)
    except pipeline.PlannerError as exc:
        export_dir, problem = None, str(exc)
        print(f"Warning: {exc}\nStarting anyway, with no activity history: every road will count as new.")
    ws = pipeline.Workspace(export_dir)
    if export_dir:
        print(f"Loading your activities from {export_dir} ...")
        print(f"{len(ws.all_tracks()):,} GPS activities loaded.")
    app = App(ws, problem)
    try:
        server = make_server(app, args.host, args.port)
    except OSError as exc:
        sys.exit(f"Couldn't listen on {args.host}:{args.port} ({exc.strerror}). "
                 f"Is webapp.py already running? Otherwise try --port {args.port + 1}.")
    shown_host = "127.0.0.1" if server.allowed_hosts is None else args.host
    url = f"http://{shown_host}:{args.port}/"
    print(f"\nNew Roads Planner is running at {url}\nPress Ctrl+C to stop.", flush=True)
    if not args.no_browser:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
