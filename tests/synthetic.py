"""Small synthetic road networks for tests, so they need no OpenStreetMap download or Strava data."""
from __future__ import annotations

import numpy as np

from roads import RoadGraph

LAT0, LON0 = 51.5, -0.12  # arbitrary; anywhere works


def grid_graph(n: int = 25, spacing_m: float = 200.0, mode: str = "ride", one_way_rows: bool = False) -> RoadGraph:
    """An n x n street grid with streets every `spacing_m` metres, centred on (LAT0, LON0).

    Every street is two-way unless one_way_rows is set: then the east-west streets alternate
    between one-way eastbound (even rows) and westbound (odd rows), like Manhattan.
    """
    kx = 111_320.0 * np.cos(np.radians(LAT0))
    ky = 110_540.0
    half = (n - 1) / 2
    jj, ii = np.meshgrid(np.arange(n), np.arange(n))
    node_lat = LAT0 + (ii.ravel() - half) * spacing_m / ky
    node_lon = LON0 + (jj.ravel() - half) * spacing_m / kx

    eu, ev, names, arcs = [], [], [], []
    for i in range(n):
        for j in range(n):
            a = node(n, i, j)
            if j + 1 < n:  # east-west street
                e = len(eu)
                eu.append(a)
                ev.append(a + 1)
                names.append(f"Row {i} Street")
                if not one_way_rows or i % 2 == 0:
                    arcs.append((a, a + 1, e))
                if not one_way_rows or i % 2 == 1:
                    arcs.append((a + 1, a, e))
            if i + 1 < n:  # north-south avenue, always two-way
                e = len(eu)
                eu.append(a)
                ev.append(a + n)
                names.append(f"Column {j} Avenue")
                arcs += [(a, a + n, e), (a + n, a, e)]
    eu = np.array(eu, dtype=np.int32)
    ev = np.array(ev, dtype=np.int32)
    n_edges = len(eu)
    geom = np.empty((2 * n_edges, 2))
    geom[0::2] = np.stack([node_lon[eu], node_lat[eu]], axis=1)
    geom[1::2] = np.stack([node_lon[ev], node_lat[ev]], axis=1)
    arcs = np.array(arcs, dtype=np.int32)
    return RoadGraph(
        mode=mode, center=(LAT0, LON0), radius_m=spacing_m * n,
        node_lat=node_lat, node_lon=node_lon,
        edge_u=eu, edge_v=ev,
        edge_len=np.full(n_edges, spacing_m), edge_pen=np.ones(n_edges),
        edge_name=names, edge_target=np.ones(n_edges, dtype=bool),
        geom_offsets=np.arange(0, 2 * n_edges + 1, 2, dtype=np.int64), geom_lonlat=geom,
        arc_from=arcs[:, 0], arc_to=arcs[:, 1], arc_edge=arcs[:, 2],
    )


def node(n: int, i: int, j: int) -> int:
    """Node id of row i, column j in an n x n grid_graph."""
    return i * n + j


def random_traveled(rg: RoadGraph, share: float = 0.4, seed: int = 1) -> np.ndarray:
    """A traveled fraction per road: roughly `share` of the roads fully traveled, the rest new."""
    rng = np.random.default_rng(seed)
    return (rng.random(len(rg.edge_len)) < share).astype(float)


class HillElevation:
    """Stands in for elevation.Elevation in tests: one smooth hill, 100 m high, centred on the grid."""

    def heights(self, lats, lons, download=True):
        lats, lons = np.asarray(lats, dtype=float), np.asarray(lons, dtype=float)
        d2 = ((lats - LAT0) * 110_540) ** 2 + ((lons - LON0) * 111_320 * np.cos(np.radians(LAT0))) ** 2
        return 20 + 100 * np.exp(-d2 / (2 * 800.0 ** 2))
