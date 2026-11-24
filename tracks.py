"""Load GPS tracks from a Strava bulk export (FIT.gz / GPX / TCX) and cache them.

The parsed tracks are cached in cache/tracks.pkl so the slow FIT parsing only
happens once; re-running only parses activities that are new since last time.
"""
from __future__ import annotations

import csv
import gzip
import io
import os
import pickle
import re
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

import numpy as np

SEMI_TO_DEG = 180.0 / 2**31

# Strava activity types grouped into the modes the router understands.
MODE_TYPES = {
    "ride": {"Ride", "E-Bike Ride", "Gravel Ride", "Mountain Bike Ride", "Virtual Ride"},
    "run": {"Run", "Trail Run", "Walk", "Hike"},
}
GPS_TYPES = set().union(*MODE_TYPES.values())


@dataclass
class Track:
    activity_id: str
    activity_type: str
    date: str
    name: str
    latlon: np.ndarray  # (n, 2) float32, degrees


def _parse_fit(raw: bytes) -> np.ndarray:
    from fitparse import FitFile

    ff = FitFile(io.BytesIO(raw))
    pts = []
    for msg in ff.get_messages("record"):
        lat = lon = None
        for f in msg.fields:
            if f.name == "position_lat":
                lat = f.value
            elif f.name == "position_long":
                lon = f.value
        if lat is not None and lon is not None:
            pts.append((lat * SEMI_TO_DEG, lon * SEMI_TO_DEG))
    return np.asarray(pts, dtype=np.float32).reshape(-1, 2)


_GPX_PT = re.compile(rb'<trkpt[^>]*?lat="([-\d.]+)"[^>]*?lon="([-\d.]+)"')
_GPX_PT_REV = re.compile(rb'<trkpt[^>]*?lon="([-\d.]+)"[^>]*?lat="([-\d.]+)"')
_TCX_PT = re.compile(
    rb"<LatitudeDegrees>([-\d.]+)</LatitudeDegrees>\s*<LongitudeDegrees>([-\d.]+)</LongitudeDegrees>"
)


def _parse_text(raw: bytes, kind: str) -> np.ndarray:
    if kind == "tcx":
        pts = [(float(a), float(b)) for a, b in _TCX_PT.findall(raw)]
    else:
        pts = [(float(a), float(b)) for a, b in _GPX_PT.findall(raw)]
        if not pts:
            pts = [(float(b), float(a)) for a, b in _GPX_PT_REV.findall(raw)]
    return np.asarray(pts, dtype=np.float32).reshape(-1, 2)


def parse_file(path: str) -> np.ndarray:
    with open(path, "rb") as fh:
        raw = fh.read()
    name = path.lower()
    if name.endswith(".gz"):
        raw = gzip.decompress(raw)
        name = name[:-3]
    try:
        if name.endswith(".fit"):
            return _parse_fit(raw)
        if name.endswith(".tcx"):
            return _parse_text(raw, "tcx")
        if name.endswith(".gpx"):
            return _parse_text(raw, "gpx")
    except Exception as exc:  # corrupt files happen in old exports
        print(f"  ! could not parse {os.path.basename(path)}: {exc}")
    return np.zeros((0, 2), dtype=np.float32)


def _job(args):
    meta, path = args
    return meta, parse_file(path)


def read_activity_index(export_dir: str) -> list[dict]:
    with open(os.path.join(export_dir, "activities.csv"), newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        wanted = ("Activity ID", "Activity Date", "Activity Name", "Activity Type", "Filename")
        missing = [n for n in wanted if n not in header]
        if missing:
            raise SystemExit(
                f"activities.csv is missing columns {missing}. Strava may have changed the export "
                "format or localised the headers; please open an issue with the first line of the file.")
        col = {name: header.index(name) for name in wanted}
        out = []
        for row in reader:
            out.append({k: row[i] for k, i in col.items()})
    return out


def load_tracks(export_dir: str, cache_dir: str, workers: int | None = None) -> list[Track]:
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, "tracks.pkl")
    cached: dict[str, Track] = {}
    if os.path.exists(cache_path):
        with open(cache_path, "rb") as fh:
            cached = pickle.load(fh)

    todo = []
    for a in read_activity_index(export_dir):
        if a["Activity Type"] not in GPS_TYPES or not a["Filename"]:
            continue
        if a["Activity ID"] in cached:
            continue
        path = os.path.join(export_dir, a["Filename"])
        if os.path.exists(path):
            todo.append((a, path))

    if todo:
        print(f"Parsing {len(todo)} new activity files (first run takes a few minutes)...")
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for i, (a, pts) in enumerate(pool.map(_job, todo, chunksize=8), 1):
                cached[a["Activity ID"]] = Track(
                    activity_id=a["Activity ID"],
                    activity_type=a["Activity Type"],
                    date=a["Activity Date"],
                    name=a["Activity Name"],
                    latlon=pts,
                )
                if i % 200 == 0:
                    print(f"  {i}/{len(todo)}")
        with open(cache_path, "wb") as fh:
            pickle.dump(cached, fh)

    return [t for t in cached.values() if len(t.latlon) > 1]


def tracks_for_mode(tracks: list[Track], mode: str) -> list[Track]:
    if mode == "all":
        return tracks
    return [t for t in tracks if t.activity_type in MODE_TYPES[mode]]
