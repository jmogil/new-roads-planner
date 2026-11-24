"""Small synthetic road networks for tests, so they need no OpenStreetMap download or Strava data."""
from __future__ import annotations

import numpy as np

from roads import RoadGraph

LAT0, LON0 = -27.0, 153.0  # arbitrary; anywhere works


def grid_graph(n: int = 25, spacing_m: float = 200.0, mode: str = "ride") -> RoadGraph:
    """An n x n street grid with two-way streets every `spacing_m` metres, centred on (LAT0, LON0)."""
    kx = 111_320.0 * np.cos(np.radians(LAT0))
    ky = 110_540.0
    half = (n - 1) / 2
    jj, ii = np.meshgrid(np.arange(n), np.arange(n))
    node_lat = LAT0 + (ii.ravel() - half) * spacing_m / ky
    node_lon = LON0 + (jj.ravel() - half) * spacing_m / kx

    eu, ev = [], []
    for i in range(n):
        for j in range(n):
            a = node(n, i, j)
            if j + 1 < n:
                eu.append(a)
                ev.append(a + 1)
            if i + 1 < n:
                eu.append(a)
                ev.append(a + n)
    eu = np.array(eu, dtype=np.int32)
    ev = np.array(ev, dtype=np.int32)
    n_edges = len(eu)
    geom = np.empty((2 * n_edges, 2))
    geom[0::2] = np.stack([node_lon[eu], node_lat[eu]], axis=1)
    geom[1::2] = np.stack([node_lon[ev], node_lat[ev]], axis=1)
    return RoadGraph(
        mode=mode, center=(LAT0, LON0), radius_m=spacing_m * n,
        node_lat=node_lat, node_lon=node_lon,
        edge_u=eu, edge_v=ev,
        edge_len=np.full(n_edges, spacing_m), edge_pen=np.ones(n_edges),
        edge_name=[""] * n_edges, edge_target=np.ones(n_edges, dtype=bool),
        geom_offsets=np.arange(0, 2 * n_edges + 1, 2, dtype=np.int64), geom_lonlat=geom,
        arc_from=np.concatenate([eu, ev]), arc_to=np.concatenate([ev, eu]),
        arc_edge=np.concatenate([np.arange(n_edges), np.arange(n_edges)]).astype(np.int32),
    )


def node(n: int, i: int, j: int) -> int:
    """Node id of row i, column j in an n x n grid_graph."""
    return i * n + j


def random_traveled(rg: RoadGraph, share: float = 0.4, seed: int = 1) -> np.ndarray:
    """A traveled fraction per road: roughly `share` of the roads fully traveled, the rest new."""
    rng = np.random.default_rng(seed)
    return (rng.random(len(rg.edge_len)) < share).astype(float)
