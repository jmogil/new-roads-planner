# new-roads-planner

Generate bike rides and runs that cover as many **roads you have never traveled** as possible.

It reads your full activity history from a Strava data export, works out which OpenStreetMap
roads you've already been on, and finds a loop (or point-to-point route) of the length you want
with as much new road as it can. You get a GPX file to load onto your bike computer or watch,
plus an interactive map.

It's a do-it-yourself take on what [Wandrer](https://wandrer.earth) measures, with a route
optimiser added. Everything runs on your own computer. You don't need a Strava API key, and
your activities are never uploaded anywhere.

> Not affiliated with, or endorsed by, Strava or Wandrer.

---

## 1. Get your data from Strava

You need Strava's "bulk export" of your account. It contains a GPS file for every activity.

1. On the **Strava website** (the app can't do this), go to
   **Settings → My Account** ([strava.com/account](https://www.strava.com/account)).
2. Under **Download your account**, click **Get Started**.
3. Click **Request download**.
4. Strava emails a download link to your account's email address. This can take a few hours.
5. Download the `.zip`. It's named something like `export_12345678.zip`. You can unzip it or
   leave it zipped; the tool accepts either.

Strava's own instructions are here:
[How do I export my Strava data?](https://support.strava.com/hc/en-us/articles/216918437)

## 2. Install

You need **Python 3.9+** and **curl**. Both come with macOS and most Linux distributions;
Windows 10/11 includes curl.

```bash
git clone https://github.com/<your-username>/new-roads-planner.git
cd new-roads-planner
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

On Windows, use `.venv\Scripts\pip` and `.venv\Scripts\python` instead of `.venv/bin/...`.

## 3. Generate a route

```bash
.venv/bin/python newroutes.py --export ~/Downloads/export_12345678.zip --mode ride --distance 40
```

To avoid typing the path every time, set it once:

```bash
export STRAVA_EXPORT=~/Downloads/export_12345678
```

```bash
.venv/bin/python newroutes.py --mode run --distance 10
```

The first run is slow, but each step is only done once and then cached:

| Step | Typical time |
|---|---|
| Reading every FIT/GPX/TCX file in the export | 2–10 min, depending on how many activities you have |
| Downloading the road network for your area from OpenStreetMap | 2–30 min, depending on area size and how busy the public servers are |
| Every later route in the same area | 5–30 seconds |

The route starts from your most common start point (usually home) unless you pass
`--start LAT,LON` or `--start-address "..."`.

## 4. Use the route

Each run writes three files to `output/`:

| File | What it is |
|---|---|
| `*.gpx` | The route. Import it into Garmin Connect (Courses), Komoot, Ride with GPS, or straight onto most bike computers and watches. |
| `*.html` | A map you open in your browser. **Red** = new road on this route, **blue** = part of the route you've done before, **dashed orange** = other untraveled roads nearby, **grey** = roads you've done. |
| `*.json` | Distance, new km and the settings used. |

What the terminal output looks like (illustrative numbers):

```
1200 activities count towards 'traveled' (ride activities).
Within 15 km of start: 2,140 of 5,980 km traveled (35.8%)
Route: 31.2 km, of which 19.4 km (62%) is new to you.
```

## Options

| Flag | Meaning |
|---|---|
| `--mode ride\|run` | `ride` uses the bike network (no motorways, one-way streets respected) and your rides. `run` uses everything walkable and your runs, walks and hikes. |
| `--distance KM` | Target length. The route can run up to `--tolerance` over (default 5%). |
| `--start LAT,LON` | Start point. Defaults to your most common start point for that mode. |
| `--start-address "..."` | Start from an address (looked up with OpenStreetMap Nominatim). |
| `--end LAT,LON` | Finish somewhere else (one-way route) instead of looping back. |
| `--alternatives N` | Also writes up to N−1 alternative routes that share less than half their roads with each other. |
| `--eagerness 0..0.95` | How much the connecting legs chase new roads. Higher = more new km but more zig-zag. Default 0.6. |
| `--avoid-main-roads X` | 0 ignores road type, 1 is the default, 2 avoids primary and trunk roads strongly. |
| `--count-all-activities` | Count roads from any activity as traveled, e.g. a road you walked counts for rides too. |
| `--restarts N`, `--iterations N` | Search effort (defaults 8 and 40). |
| `--seed N` | Makes the search reproducible. |
| `--export PATH` | Your Strava export, as a folder or `.zip`. Can also be set with `STRAVA_EXPORT`. |
| `--out DIR` | Where to write results (default `output/`). |

## Keeping it up to date

When you've done more activities, request a fresh export from Strava and point `--export` at it.
Activities that have already been read are skipped, so only the new ones get parsed.

## How it works

1. **Tracks** (`tracks.py`): reads every FIT, GPX and TCX file in the export and caches the GPS points.
2. **Roads** (`roads.py`): downloads the OpenStreetMap network around the start from the Overpass
   API in small tiles, then builds a graph with [OSMnx](https://github.com/gboeing/osmnx).
   GPS points are densified, and every road is sampled every 10 m. A sample counts as traveled
   if one of your GPS points is within 20 m of it, so each road gets a 0–1 "traveled" fraction.
3. **Planning** (`planner.py`): this is a variant of the
   [orienteering problem](https://en.wikipedia.org/wiki/Orienteering_problem):
   - Untraveled roads cost less to route along.
   - The richest clusters of new road become candidate waypoints.
   - A greedy search builds loops through them, maximising new metres per extra metre travelled
     and counting each road only once.
   - A "ruin and recreate" step then repeatedly drops part of the loop and refills it, keeping
     whatever collects more new road.
   - Small untraveled loops (cul-de-sac circuits) are spliced in when the budget allows.
   - Separately-mapped sidewalks and crossings can be used to get around, but they don't count
     as new roads.
4. **Export** (`export.py`): writes the GPX, HTML map and JSON summary.

## Limitations and caveats

- **Your numbers won't exactly match Wandrer's.** "Traveled" is matched independently. Roads
  that run right alongside ones you've done (within ~20 m) can be counted as done.
- **Not every OSM road should be ridden.** Check the map for service roads, gated paths and
  unpleasant main roads before you go. `--avoid-main-roads 2` helps.
- **`ride` includes unpaved paths and tracks.** That's great for gravel and MTB, less so on a road
  bike. There's no paved-only option yet; see Ideas below.
- **Bigger distances mean bigger downloads.** The area fetched is a box around the start big
  enough for a loop of that length. Planning a longer route than before downloads a bigger area.
- **Privacy:** your activity files stay on your computer, and `cache/` and `output/` are
  git-ignored. Two lookups do go out:
  - Road data comes from the public Overpass API (with two community mirrors as fallback),
    using a bounding box around your start point.
  - `--start-address` sends the address to OSM Nominatim.

## Troubleshooting

- **"Overpass returned 502/504" over and over**: the free public Overpass servers are busy.
  The tool retries and keeps finished tiles, so you can stop it and re-run later; it resumes.
- **"activities.csv is missing columns"**: Strava may have changed or localised the export
  format. Please open an issue with the first line of your `activities.csv`. It holds column
  names only, no personal data.
- **SSL errors**: downloads go through `curl` to avoid old-OpenSSL problems in some system
  Pythons, so make sure `curl` is on your PATH.
- **To force a rebuild**: delete `cache/graph_*.pkl` to rebuild graphs from the saved `.osm`
  files, or delete the whole `cache/` folder to start from scratch.

## Ideas / contributing

Pull requests are welcome. Some directions:

- A `--paved-only` option for road bikes (using OSM `surface` tags)
- Sync new activities from the Strava API instead of re-exporting
- A "directional" mode, so a road ridden the opposite way counts as new
- Elevation-aware routing (avoid or seek climbs)
- A small web UI for picking start points and comparing alternatives

## Credits

- Map data © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), available
  under the ODbL. Routes you generate are derived from OSM data.
- Built on [OSMnx](https://github.com/gboeing/osmnx), [SciPy](https://scipy.org),
  [fitparse](https://github.com/dtcooper/python-fitparse), [gpxpy](https://github.com/tkrajina/gpxpy)
  and [Folium](https://python-visualization.github.io/folium/).

## License

MIT. See [LICENSE](LICENSE).
