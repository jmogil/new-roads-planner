"""Routes you've saved from the web app, kept in cache/saved_routes/.

Each saved route is two files: <id>.gpx, ready to load onto a device, and <id>.json, with what
the page needs to show it again (the route split into new and done road, the elevation profile
and the numbers). They're in cache/ because they show where you go, and cache/ is git-ignored.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid

ID = re.compile(r"[0-9a-f]{12}")
SUMMARY = ("id", "name", "saved_at", "mode", "route_type", "distance_km", "new_km", "new_pct",
           "ascent_m", "done", "bounds", "job", "index")  # job/index: which suggestion it was saved from
MAX_NAME = 80


class NotFound(KeyError):
    """No saved route with that id."""


def clean_name(name) -> str:
    """A tidy one-line name: no control characters or runs of spaces, at most MAX_NAME characters."""
    text = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", str(name or ""))).strip()
    return text[:MAX_NAME].strip()


def file_name(name: str) -> str:
    """A safe file name for a route's GPX download, from its name."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()[:60].strip("-")
    return f"{slug or 'route'}.gpx"


class SavedRoutes:
    def __init__(self, cache_dir: str):
        self.dir = os.path.join(cache_dir, "saved_routes")
        self._lock = threading.Lock()

    def _path(self, rid: str, ext: str) -> str:
        if not ID.fullmatch(rid or ""):
            raise NotFound(rid)
        return os.path.join(self.dir, f"{rid}.{ext}")

    def _write(self, path: str, text: str) -> None:
        os.makedirs(self.dir, exist_ok=True)
        with open(path + ".part", "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(path + ".part", path)

    def get(self, rid: str) -> dict:
        """The whole saved route. Raises KeyError if there's no such route."""
        try:
            with open(self._path(rid, "json"), encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            raise NotFound(rid)

    def gpx(self, rid: str) -> str:
        try:
            with open(self._path(rid, "gpx"), encoding="utf-8") as fh:
                return fh.read()
        except FileNotFoundError:
            raise NotFound(rid)

    def list(self) -> list[dict]:
        """Summaries of every saved route, newest first."""
        if not os.path.isdir(self.dir):
            return []
        out = []
        for entry in os.listdir(self.dir):
            rid, ext = os.path.splitext(entry)
            if ext != ".json" or not ID.fullmatch(rid):
                continue
            try:
                out.append(summary(self.get(rid)))
            except (KeyError, OSError, ValueError):
                continue  # half-written or hand-edited: skip it rather than fail the list
        return sorted(out, key=lambda r: r.get("saved_at") or "", reverse=True)

    def add(self, record: dict, gpx: str) -> dict:
        rid = uuid.uuid4().hex[:12]
        record = dict(record, id=rid, saved_at=time.strftime("%Y-%m-%dT%H:%M:%S"), done=False,
                      name=clean_name(record.get("name")) or "Saved route")
        with self._lock:
            self._write(self._path(rid, "gpx"), gpx)
            self._write(self._path(rid, "json"), json.dumps(record, separators=(",", ":")))
        return summary(record)

    def update(self, rid: str, name=None, done=None) -> dict:
        with self._lock:
            record = self.get(rid)
            if name is not None:
                record["name"] = clean_name(name) or record["name"]
            if done is not None:
                record["done"] = bool(done)
            self._write(self._path(rid, "json"), json.dumps(record, separators=(",", ":")))
        return summary(record)

    def delete(self, rid: str) -> None:
        with self._lock:
            found = False
            for ext in ("json", "gpx"):
                try:
                    os.remove(self._path(rid, ext))
                    found = True
                except FileNotFoundError:
                    pass
            if not found:
                raise NotFound(rid)


def summary(record: dict) -> dict:
    return {k: record.get(k) for k in SUMMARY}
