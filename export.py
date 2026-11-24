"""Write the planned route as GPX and as an interactive HTML map."""
from __future__ import annotations

import json

import numpy as np

from planner import Plan
from roads import RoadGraph


def route_pieces(rg: RoadGraph, plan: Plan, new_len: np.ndarray):
    """Yield (coords lon/lat array, is_new) per step; a road is 'new' only on its first pass."""
    seen = set()
    for i, e in enumerate(plan.edges):
        coords = rg.edge_coords(e, from_node=plan.nodes[i])
        is_new = e not in seen and new_len[e] > 0.5 * rg.edge_len[e]
        seen.add(e)
        yield coords, is_new


def route_coords(rg: RoadGraph, plan: Plan) -> np.ndarray:
    out = []
    for i, e in enumerate(plan.edges):
        c = rg.edge_coords(e, from_node=plan.nodes[i])
        out.append(c if not out else c[1:])
    return np.concatenate(out) if out else np.zeros((0, 2))


def write_gpx(path: str, rg: RoadGraph, plan: Plan, name: str):
    import gpxpy.gpx

    gpx = gpxpy.gpx.GPX()
    gpx.creator = "new-roads-planner"
    trk = gpxpy.gpx.GPXTrack(name=name)
    seg = gpxpy.gpx.GPXTrackSegment()
    for lon, lat in route_coords(rg, plan):
        seg.points.append(gpxpy.gpx.GPXTrackPoint(latitude=round(float(lat), 6),
                                                  longitude=round(float(lon), 6)))
    trk.segments.append(seg)
    gpx.tracks.append(trk)
    with open(path, "w") as fh:
        fh.write(gpx.to_xml())


def _lines_geojson(rg: RoadGraph, edge_ids) -> dict:
    lines = [np.round(rg.edge_coords(int(e)), 5).tolist() for e in edge_ids]
    return {"type": "Feature", "properties": {},
            "geometry": {"type": "MultiLineString", "coordinates": lines}}


def write_map(path: str, rg: RoadGraph, plan: Plan, new_len: np.ndarray, title: str, stats_html: str):
    import folium

    coords = route_coords(rg, plan)
    lat0, lon0 = float(coords[:, 1].mean()), float(coords[:, 0].mean())
    m = folium.Map(location=[lat0, lon0], zoom_start=13, tiles="OpenStreetMap", control_scale=True)
    # soften the basemap so the route stands out
    m.get_root().header.add_child(folium.Element(
        "<style>.leaflet-tile-pane{filter:grayscale(.85) brightness(1.05) contrast(.85);}</style>"))

    # roads around the route: untraveled (red, dashed) and traveled (grey)
    pad = 0.01
    x0, y0 = coords.min(axis=0) - pad
    x1, y1 = coords.max(axis=0) + pad
    mid_lon = (rg.node_lon[rg.edge_u] + rg.node_lon[rg.edge_v]) / 2
    mid_lat = (rg.node_lat[rg.edge_u] + rg.node_lat[rg.edge_v]) / 2
    near = (mid_lon > x0) & (mid_lon < x1) & (mid_lat > y0) & (mid_lat < y1)
    untrav = np.where(near & (new_len > 0.5 * rg.edge_len))[0]
    trav = np.where(near & (new_len <= 0.5 * rg.edge_len))[0]

    fg_t = folium.FeatureGroup(name="Roads already traveled", show=True)
    folium.GeoJson(_lines_geojson(rg, trav),
                   style_function=lambda _: {"color": "#5f6368", "weight": 2, "opacity": 0.55}).add_to(fg_t)
    fg_t.add_to(m)
    fg_u = folium.FeatureGroup(name="Untraveled roads", show=True)
    folium.GeoJson(_lines_geojson(rg, untrav),
                   style_function=lambda _: {"color": "#e8710a", "weight": 1.5, "opacity": 0.7,
                                             "dashArray": "4 4"}).add_to(fg_u)
    fg_u.add_to(m)

    fg_r = folium.FeatureGroup(name="Planned route", show=True)
    for c, is_new in route_pieces(rg, plan, new_len):
        folium.PolyLine([(lat, lon) for lon, lat in c],
                        color="#d93025" if is_new else "#1a73e8",
                        weight=5 if is_new else 4, opacity=0.9).add_to(fg_r)
    fg_r.add_to(m)

    folium.Marker([coords[0, 1], coords[0, 0]], tooltip="Start",
                  icon=folium.Icon(color="green", icon="play")).add_to(m)
    if np.hypot(*(coords[-1] - coords[0])) > 1e-4:
        folium.Marker([coords[-1, 1], coords[-1, 0]], tooltip="Finish",
                      icon=folium.Icon(color="black", icon="stop")).add_to(m)

    legend = f"""
    <div style="position:fixed;top:12px;left:56px;z-index:9999;background:white;padding:10px 14px;
                border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.3);font:13px/1.45 system-ui,sans-serif;max-width:300px">
      <b>{title}</b><br>{stats_html}<br>
      <span style="color:#d93025">&#9644;</span> route: new road &nbsp;
      <span style="color:#1a73e8">&#9644;</span> route: done before<br>
      <span style="color:#e8710a">- -</span> other untraveled roads &nbsp;
      <span style="color:#5f6368">&#9644;</span> roads you've done
    </div>"""
    m.get_root().html.add_child(folium.Element(legend))
    folium.LayerControl(collapsed=True).add_to(m)
    m.fit_bounds([[coords[:, 1].min(), coords[:, 0].min()], [coords[:, 1].max(), coords[:, 0].max()]])
    m.save(path)


def write_summary(path: str, info: dict):
    with open(path, "w") as fh:
        json.dump(info, fh, indent=2)
