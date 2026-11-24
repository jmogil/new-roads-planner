#!/usr/bin/env python3
"""Plan a route that maximises roads you have never traveled.

Examples
  python newroutes.py --export ~/Downloads/export_12345678 --mode ride --distance 40
  python newroutes.py --export ~/Downloads/export_12345678.zip --mode run --distance 10 --start 51.5007,-0.1246
  python newroutes.py --mode ride --distance 60 --start-address "Town Hall, Springfield"

The export location can also be set once with the STRAVA_EXPORT environment variable.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time
import warnings
from collections import Counter

warnings.filterwarnings("ignore", message=".*OpenSSL.*")

import numpy as np  # noqa: E402

import export  # noqa: E402
import roads  # noqa: E402
import tracks as tracks_mod  # noqa: E402
from planner import PlanOptions, Planner  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def parse_latlon(s: str) -> tuple[float, float]:
    a, b = (float(x) for x in s.replace(" ", "").split(","))
    return a, b


def resolve_export(path: str | None) -> str:
    """Return the unzipped Strava export folder (extracting a .zip on first use)."""
    path = path or os.environ.get("STRAVA_EXPORT")
    if not path:
        sys.exit("Tell me where your Strava export is: --export /path/to/export_12345 "
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
        sys.exit(f"No activities.csv in {path} - is this the folder from Strava's account download? See README.md.")
    return path


def common_start(trks) -> tuple[float, float]:
    """Most frequent start location (≈200 m grid) among your activities."""
    c = Counter((round(float(t.latlon[0][0]) / 0.002), round(float(t.latlon[0][1]) / 0.002)) for t in trks)
    (a, b), _ = c.most_common(1)[0]
    pts = np.array([t.latlon[0] for t in trks
                    if round(float(t.latlon[0][0]) / 0.002) == a and round(float(t.latlon[0][1]) / 0.002) == b])
    return float(pts[:, 0].mean()), float(pts[:, 1].mean())


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["ride", "run"], default="ride",
                    help="ride = bike network & your rides; run = foot network & runs/walks/hikes")
    ap.add_argument("--distance", type=float, required=True, help="target distance in km")
    ap.add_argument("--start", type=parse_latlon, help="start as lat,lon (default: your most common start)")
    ap.add_argument("--start-address", help="start address (geocoded via OpenStreetMap Nominatim)")
    ap.add_argument("--end", type=parse_latlon, help="finish as lat,lon for a one-way route (default: loop)")
    ap.add_argument("--tolerance", type=float, default=0.05, help="allowed overshoot fraction (default 0.05)")
    ap.add_argument("--eagerness", type=float, default=0.6,
                    help="0..0.95: how hard connecting legs chase new roads (default 0.6)")
    ap.add_argument("--avoid-main-roads", type=float, default=1.0,
                    help="0 = ignore road type, 1 = default penalties, 2 = avoid strongly")
    ap.add_argument("--restarts", type=int, default=8, help="independent searches (more = slower, better)")
    ap.add_argument("--iterations", type=int, default=40, help="improvement steps per search")
    ap.add_argument("--alternatives", type=int, default=1,
                    help="also output up to N-1 distinct alternative routes (default 1 = best only)")
    ap.add_argument("--seed", type=int, help="random seed for the search (for reproducible results)")
    ap.add_argument("--count-all-activities", action="store_true",
                    help="treat roads from ANY activity type as traveled (default: same mode only)")
    ap.add_argument("--export", "--export-dir", dest="export",
                    help="your Strava bulk export: unzipped folder or .zip (or set STRAVA_EXPORT)")
    ap.add_argument("--out", default=os.path.join(HERE, "output"), help="output folder")
    args = ap.parse_args(argv)

    t0 = time.time()
    cache = os.path.join(HERE, "cache")
    os.makedirs(args.out, exist_ok=True)
    export_dir = resolve_export(args.export)

    all_tracks = tracks_mod.load_tracks(export_dir, cache)
    if not all_tracks:
        sys.exit("No GPS activities found in the export.")
    mode_tracks = tracks_mod.tracks_for_mode(all_tracks, args.mode)
    used_tracks = all_tracks if args.count_all_activities else mode_tracks
    print(f"{len(used_tracks)} activities count towards 'traveled' "
          f"({'all types' if args.count_all_activities else args.mode + ' activities'}).")

    if args.start_address:
        start = roads.geocode(args.start_address)
        print(f"Start: {args.start_address} -> {start[0]:.5f},{start[1]:.5f}")
    elif args.start:
        start = args.start
    else:
        start = common_start(mode_tracks)
        print(f"Start: your most common {args.mode} start point ({start[0]:.4f},{start[1]:.4f})")
    end = args.end

    D = args.distance * 1000.0
    if end:
        center = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
        need = D * (1 + args.tolerance) / 2 + 1500
    else:
        center = start
        need = D * (1 + args.tolerance) / 2 + 1500
    # round so nearby future requests reuse the same downloaded graph
    c_lat, c_lon = round(center[0], 2), round(center[1], 2)
    radius = math.ceil((need + 1500) / 5000.0) * 5000.0
    if roads.find_cached_graph(cache, args.mode, center[0], center[1], need):
        rg = roads.load_graph(cache, args.mode, center[0], center[1], need)
    else:
        rg = roads.load_graph(cache, args.mode, c_lat, c_lon, radius)
    print(f"Road network: {rg.n_nodes:,} junctions, {len(rg.edge_len):,} road segments, "
          f"{rg.edge_len.sum()/1000:,.0f} km")

    frac = roads.compute_coverage(rg, used_tracks, cache)
    proj = roads.Projector(*rg.center)
    nxy = proj.xy(rg.node_lat, rg.node_lon)
    sxy = proj.xy(*start)
    mid = (nxy[rg.edge_u] + nxy[rg.edge_v]) / 2
    within = (np.hypot(*(mid - sxy).T) <= D / 2) & rg.edge_target
    tot = rg.edge_len[within].sum()
    done = (rg.edge_len * frac)[within].sum()
    print(f"Within {D/2000:.0f} km of start: {done/1000:,.0f} of {tot/1000:,.0f} km traveled "
          f"({100*done/max(tot,1):.1f}%)")

    opts = PlanOptions(distance_m=D, tolerance=args.tolerance, eagerness=args.eagerness,
                       avoid_main_roads=args.avoid_main_roads, restarts=args.restarts, iterations=args.iterations, seed=args.seed)
    planner = Planner(rg, frac, opts)
    s_node = planner.nearest_node(*start)
    e_node = planner.nearest_node(*end) if end else None
    print("Searching for the route with the most new road...")
    plans = planner.plan(s_node, e_node, alternatives=max(1, args.alternatives))
    if not plans:
        print("No route found - try a larger distance or another start point.")
        return 1

    stamp = time.strftime("%Y%m%d-%H%M%S")
    for i, plan in enumerate(plans):
        km, new_km = plan.length_m / 1000, plan.new_m / 1000
        suffix = "" if i == 0 else f"_alt{i}"
        base = os.path.join(args.out, f"{args.mode}_{args.distance:g}km_{stamp}{suffix}")
        title = f"{args.mode.title()} · {km:.1f} km" + ("" if i == 0 else f" (alternative {i})")
        stats = (f"<b>{new_km:.1f} km new</b> ({100*new_km/km:.0f}% of route)<br>"
                 f"{len(plan.waypoints)} target areas")
        export.write_gpx(base + ".gpx", rg, plan,
                         name=f"New roads {args.mode} {km:.1f} km ({new_km:.1f} new)"
                              + ("" if i == 0 else f" alt {i}"))
        export.write_map(base + ".html", rg, plan, planner.new_len, title, stats)
        export.write_summary(base + ".json", {
            "mode": args.mode, "target_km": args.distance, "route_km": round(km, 2),
            "new_km": round(new_km, 2), "new_pct": round(100 * new_km / km, 1),
            "start": list(start), "end": list(end) if end else None,
            "options": vars(opts),
        })
        label = "Route" if i == 0 else f"Alternative {i}"
        print(f"\n{label}: {km:.1f} km, of which {new_km:.1f} km ({100*new_km/km:.0f}%) is new to you.")
        print(f"  GPX: {base}.gpx\n  Map: {base}.html")
    print(f"\nDone in {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
