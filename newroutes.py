#!/usr/bin/env python3
"""Plan a route that maximises roads you have never traveled.

Examples
  python newroutes.py --export ~/Downloads/export_12345678 --mode ride --distance 40
  python newroutes.py --export ~/Downloads/export_12345678.zip --mode run --distance 10 --start 51.5007,-0.1246
  python newroutes.py --mode ride --distance 60 --start-address "Town Hall, Springfield"

The export location is remembered after the first run; it can also be set with
the STRAVA_EXPORT environment variable. For a point-and-click version, run webapp.py.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import warnings

warnings.filterwarnings("ignore", message=".*OpenSSL.*")

import export  # noqa: E402
import pipeline  # noqa: E402
import roads  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def parse_latlon(s: str) -> tuple[float, float]:
    a, b = (float(x) for x in s.replace(" ", "").split(","))
    return a, b


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
    os.makedirs(args.out, exist_ok=True)
    try:
        ws = pipeline.Workspace(pipeline.resolve_export(args.export))
        if not ws.all_tracks():
            sys.exit("No GPS activities found in the export.")
        start = args.start
        if args.start_address:
            start = roads.geocode(args.start_address)
            print(f"Start: {args.start_address} -> {start[0]:.5f},{start[1]:.5f}")
        result = ws.plan(pipeline.RouteRequest(
            mode=args.mode, distance_km=args.distance, start=start, end=args.end,
            alternatives=max(1, args.alternatives), tolerance=args.tolerance, eagerness=args.eagerness,
            avoid_main_roads=args.avoid_main_roads, restarts=args.restarts, iterations=args.iterations,
            seed=args.seed, count_all_activities=args.count_all_activities))
    except pipeline.PlannerError as exc:
        print(exc, file=sys.stderr)
        return 1

    rg, stamp = result.graph, time.strftime("%Y%m%d-%H%M%S")
    for i, plan in enumerate(result.plans):
        km, new_km = plan.length_m / 1000, plan.new_m / 1000
        suffix = "" if i == 0 else f"_alt{i}"
        base = os.path.join(args.out, f"{args.mode}_{args.distance:g}km_{stamp}{suffix}")
        title = f"{args.mode.title()} · {km:.1f} km" + ("" if i == 0 else f" (alternative {i})")
        stats = (f"<b>{new_km:.1f} km new</b> ({100*new_km/km:.0f}% of route)<br>"
                 f"{len(plan.waypoints)} target areas")
        export.write_gpx(base + ".gpx", rg, plan,
                         name=f"New roads {args.mode} {km:.1f} km ({new_km:.1f} new)"
                              + ("" if i == 0 else f" alt {i}"))
        export.write_map(base + ".html", rg, plan, result.new_len, title, stats)
        export.write_summary(base + ".json", {
            "mode": args.mode, "target_km": args.distance, "route_km": round(km, 2),
            "new_km": round(new_km, 2), "new_pct": round(100 * new_km / km, 1),
            "start": list(result.start), "end": list(result.end) if result.end else None,
            "options": vars(result.options),
        })
        label = "Route" if i == 0 else f"Alternative {i}"
        print(f"\n{label}: {km:.1f} km, of which {new_km:.1f} km ({100*new_km/km:.0f}%) is new to you.")
        print(f"  GPX: {base}.gpx\n  Map: {base}.html")
    print(f"\nDone in {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
