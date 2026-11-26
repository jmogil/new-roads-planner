"""Ground elevation along routes, from SRTM (NASA's Shuttle Radar Topography Mission, about a 30 m grid).

The data comes from the Terrain Tiles open dataset on AWS (registry.opendata.aws/terrain-tiles),
in its "skadi" layout: one gzipped .hgt file per 1° x 1° square. Each square is downloaded once
(a few MB) and kept in cache/elevation/. Only the square's name, such as N51W001, is sent, never
a route.
"""
from __future__ import annotations

import gzip
import math
import os
import threading
import time
import zlib

import numpy as np

import progress
import roads

URL = "https://s3.amazonaws.com/elevation-tiles-prod/skadi/{ns}/{name}.hgt.gz"
VOID = -32768
RETRY_AFTER_S = 600  # after a failed download, don't keep retrying on every route


class ElevationUnavailable(Exception):
    """Elevation data couldn't be downloaded (for example, no internet connection)."""


def tile_name(lat: float, lon: float) -> str:
    """The 1° square containing a point, named by its south-west corner, e.g. N51W001."""
    la, lo = math.floor(lat), math.floor(lon)
    return f"{'N' if la >= 0 else 'S'}{abs(la):02d}{'E' if lo >= 0 else 'W'}{abs(lo):03d}"


class Elevation:
    """Looks up ground heights, downloading and caching 1° squares as they're needed."""

    def __init__(self, cache_dir: str):
        self.dir = os.path.join(cache_dir, "elevation")
        self._tiles: dict[str, np.ndarray | None] = {}
        self._failed_at = 0.0
        self._lock = threading.Lock()

    def heights(self, lats, lons, download: bool = True) -> np.ndarray:
        """Metres above sea level at each point (interpolated), NaN where unknown.

        With download=False, only squares already in the cache are used.
        """
        lats = np.asarray(lats, dtype=np.float64)
        lons = np.asarray(lons, dtype=np.float64)
        out = np.full(lats.shape, np.nan)
        la0, lo0 = np.floor(lats), np.floor(lons)
        for la, lo in sorted(set(zip(la0.tolist(), lo0.tolist()))):
            grid = self._tile(tile_name(la + 0.5, lo + 0.5), download)
            if grid is None:
                continue
            sel = (la0 == la) & (lo0 == lo)
            n = grid.shape[0]
            r = (la + 1 - lats[sel]) * (n - 1)  # rows run north to south
            c = (lons[sel] - lo) * (n - 1)
            r0 = np.clip(np.floor(r).astype(int), 0, n - 2)
            c0 = np.clip(np.floor(c).astype(int), 0, n - 2)
            fr, fc = r - r0, c - c0
            q = np.stack([grid[r0, c0], grid[r0, c0 + 1], grid[r0 + 1, c0], grid[r0 + 1, c0 + 1]]).astype(float)
            q[q == VOID] = np.nan
            out[sel] = ((q[0] * (1 - fc) + q[1] * fc) * (1 - fr) + (q[2] * (1 - fc) + q[3] * fc) * fr)
        return out

    def _tile(self, name: str, download: bool):
        with self._lock:
            if name in self._tiles:
                return self._tiles[name]
            path = os.path.join(self.dir, name + ".npy")
            missing = os.path.join(self.dir, name + ".missing")  # open sea: no file to download
            if not os.path.exists(path) and not os.path.exists(missing):
                if not download:
                    return None
                if time.time() - self._failed_at < RETRY_AFTER_S:
                    raise ElevationUnavailable("elevation data couldn't be downloaded a moment ago")
                try:
                    self._download(name, path, missing)
                except ElevationUnavailable:
                    self._failed_at = time.time()
                    raise
            grid = np.load(path, mmap_mode="r") if os.path.exists(path) else None
            self._tiles[name] = grid
            return grid

    def _download(self, name: str, path: str, missing: str) -> None:
        os.makedirs(self.dir, exist_ok=True)
        progress.report("elevation")
        print(f"Downloading elevation data for the 1° square {name} (one-off)...", flush=True)
        gz = os.path.join(self.dir, name + ".hgt.gz.part")
        try:
            code, out, err = roads._run_cancellable(
                ["curl", "-sS", "--max-time", "300", "--connect-timeout", "20", "-A", roads.USER_AGENT,
                 "-o", gz, "-w", "%{http_code}", URL.format(ns=name[:3], name=name)])
            status = out.strip()
            if code == 0 and status in ("403", "404"):  # no tile: all sea
                open(missing, "w").close()
                return
            if code != 0 or status != "200":
                raise ElevationUnavailable(f"couldn't download elevation data ({status or err.strip()})")
            try:
                with open(gz, "rb") as fh:
                    raw = gzip.decompress(fh.read())
            except (OSError, EOFError, zlib.error) as exc:  # a truncated or corrupt download
                raise ElevationUnavailable(f"the elevation download was damaged ({exc})")
            n = math.isqrt(len(raw) // 2)
            if n < 2 or n * n * 2 != len(raw):
                raise ElevationUnavailable("the elevation file wasn't in the expected format")
            grid = np.frombuffer(raw, ">i2").reshape(n, n).astype(np.int16)
            with open(path + ".part", "wb") as fh:
                np.save(fh, grid)
            os.replace(path + ".part", path)
        finally:
            if os.path.exists(gz):
                os.remove(gz)


def climb(h: np.ndarray, threshold: float = 2.0) -> tuple[float, float]:
    """Total ascent and descent, ignoring wobbles smaller than threshold metres."""
    up = down = 0.0
    ref = float(h[0])
    for v in map(float, h[1:]):
        if v - ref >= threshold:
            up, ref = up + v - ref, v
        elif ref - v >= threshold:
            down, ref = down + ref - v, v
    return up, down


def profile(segments: list[dict], elev: Elevation, max_points: int = 1500, download: bool = True) -> dict | None:
    """Elevation along a route given as runs of new and done road (export.route_segments).

    Samples about every 20–25 m. Returns distances (km), heights (m), whether each sample is on
    new road, the sample positions, and the ascent, descent, lowest and highest point. Returns
    None when there's no elevation data.
    """
    lat, lon, new = [], [], []
    for i, run in enumerate(segments):
        coords = run["coords"] if i == 0 else run["coords"][1:]  # each run starts where the last ended
        for la, lo in coords:
            lat.append(la)
            lon.append(lo)
            new.append(bool(run["new"]))
    if len(lat) < 2:
        return None
    lat, lon, new = np.array(lat), np.array(lon), np.array(new)
    xy = roads.Projector(float(lat.mean()), float(lon.mean())).xy(lat, lon)
    d = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(xy, axis=0).T))])
    total = float(d[-1])
    if total <= 0:
        return None
    step = max(20.0, total / max_points)
    s = np.append(np.arange(0.0, total, step), total)
    slat, slon = np.interp(s, d, lat), np.interp(s, d, lon)
    snew = new[np.clip(np.searchsorted(d, s, side="left"), 0, len(new) - 1)]
    h = elev.heights(slat, slon, download=download)
    ok = ~np.isnan(h)
    if not ok.any():
        return None
    h = np.interp(s, s[ok], h[ok])  # fill any gaps in the data
    # smooth over ~100 m: the data is noisy, and noise inflates the climbing total
    w = max(1, int(round(100 / step)))
    if w > 1:
        h = np.convolve(np.pad(h, (w // 2, w - 1 - w // 2), mode="edge"), np.ones(w) / w, mode="valid")
    up, down = climb(h)
    return {
        "km": np.round(s / 1000, 3).tolist(),
        "m": np.round(h, 1).tolist(),
        "new": snew.tolist(),
        "lat": np.round(slat, 5).tolist(),
        "lon": np.round(slon, 5).tolist(),
        "ascent_m": round(up),
        "descent_m": round(down),
        "min_m": round(float(h.min())),
        "max_m": round(float(h.max())),
    }
