# new-roads-planner

Generate bike rides and runs that cover as many **roads you have never traveled** as possible.

It reads your full activity history from a Strava data export, works out which OpenStreetMap
roads you've already been on, and finds a loop (or point-to-point route) of the length you want
with as much new road as it can. You get a GPX file to load onto your bike computer or watch,
plus a map of the route.

There are two ways to use it:

- **The web app** (`webapp.py`): a point-and-click planner in your browser. Choose ride or run,
  pick a start on the map, choose a loop or an A → B route, and compare suggested routes.
- **The command line** (`newroutes.py`): the same planner in one command, which is handy for
  scripting.

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

## 3. Plan routes in the web app

```bash
.venv/bin/python webapp.py --export ~/Downloads/export_12345678.zip
```

Your browser opens the planner at <http://127.0.0.1:8765>. Then:

1. Choose **Ride** or **Run**.
2. Choose **Loop** (back to the start) or **A → B** (finish somewhere else).
3. Set the **start**: it defaults to where most of your activities start (usually home). You
   can also search for a place, click the map, or use your current location. For A → B, set
   the **finish** the same way.
4. Set the **distance**, then click **Generate routes**.

You get up to three different suggestions. Red is new road, blue is road you've done before,
and dashed orange shows other untraveled roads nearby. Click a suggestion to see it, then
**GPX** to download it.

The export path is remembered, so from then on this is enough:

```bash
.venv/bin/python webapp.py
```

See the [web app guide](docs/web-app.md) for every option, and for using it from your phone.

## 4. Or use the command line

```bash
.venv/bin/python newroutes.py --export ~/Downloads/export_12345678.zip --mode ride --distance 40
```

After the first run the export path is remembered, as it is for the web app. You can also set
it with the `STRAVA_EXPORT` environment variable:

```bash
.venv/bin/python newroutes.py --mode run --distance 10
```

The route starts from your most common start point (usually home) unless you pass
`--start LAT,LON` or `--start-address "..."`. Add `--end LAT,LON` for an A → B route.

### The first route in an area is slow

The web app and the command line share the same caches. Each slow step is only done once:

| Step | Typical time |
|---|---|
| Reading every FIT/GPX/TCX file in the export | 2–10 min, depending on how many activities you have |
| Downloading the road network for your area from OpenStreetMap | 2–30 min, depending on area size and how busy the public servers are |
| Every later route in the same area | 5–30 seconds |

The web app tells you before you start whether an area's road data is already downloaded,
and shows the downloaded areas on the map.

## 5. Use the route

The web app's **GPX** button downloads the route you're looking at. Each command-line run
writes three files to `output/`:

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

## Command-line options

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
| `--export PATH` | Your Strava export, as a folder or `.zip`. Remembered after first use; can also be set with `STRAVA_EXPORT`. |
| `--out DIR` | Where to write results (default `output/`). |

The web app has the same settings under **More options**, except the seed and output folder.
Run `.venv/bin/python webapp.py --help` for its own few flags (`--port`, `--host`, `--no-browser`).

## Keeping it up to date

When you've done more activities, request a fresh export from Strava and point `--export` at it
(for either tool). Activities that have already been read are skipped, so only the new ones get
parsed.

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
4. **Pipeline** (`pipeline.py`): ties those steps together for one route request, and keeps
   activities and road graphs in memory between requests.
5. **Output**: `export.py` writes the GPX, HTML map and JSON summary for the command line
   (`newroutes.py`). The web app (`webapp.py` plus the page in `web/`) draws routes on a
   [Leaflet](https://leafletjs.com) map and serves GPX downloads.

[docs/architecture.md](docs/architecture.md) goes into more detail, including the web app's API.

## Limitations and caveats

- **Your numbers won't exactly match Wandrer's.** "Traveled" is matched independently. Roads
  that run right alongside ones you've done (within ~20 m) can be counted as done.
- **Not every OSM road should be ridden.** Check the map for service roads, gated paths and
  unpleasant main roads before you go. Setting main roads to "avoid strongly" (`--avoid-main-roads 2`)
  helps.
- **`ride` includes unpaved paths and tracks.** That's great for gravel and MTB, less so on a road
  bike. There's no paved-only option yet; see the [roadmap](docs/roadmap.md).
- **Bigger distances mean bigger downloads.** The area fetched is a circle around the start (or
  around the midpoint of an A → B route) big enough for a route of that length. Planning a longer
  route than before downloads a bigger area.
- **An A → B route needs room for detours.** The finish has to be well within the distance
  (as a rule of thumb, the distance should be at least 1.5 times the straight line between start
  and finish) for the route to collect much new road.
- **Privacy:** your activity files stay on your computer, and `cache/` and `output/` are
  git-ignored. Some lookups do go out:
  - Road data comes from the public Overpass API (with two community mirrors as fallback),
    using a bounding box around your start point.
  - Place searches (`--start-address`, or the web app's search boxes) go to OSM Nominatim. The
    web app also sends the area the map is showing, so nearby matches come first.
  - The web app's map tiles come from OpenStreetMap's tile servers, and the page loads the
    Leaflet map library from unpkg.com.

## Troubleshooting

- **"Overpass returned 502/504" over and over**: the free public Overpass servers are busy.
  The tool retries and keeps finished tiles, so you can stop it and re-run later; it resumes.
- **"activities.csv is missing columns"**: Strava may have changed or localised the export
  format. Please open an issue with the first line of your `activities.csv`. It holds column
  names only, no personal data.
- **SSL errors**: downloads go through `curl` to avoid old-OpenSSL problems in some system
  Pythons, so make sure `curl` is on your PATH.
- **"Couldn't listen on 127.0.0.1:8765"**: the web app is probably already running in another
  terminal. Use that one, or start another with `--port 8766`.
- **"Another process is already downloading … waiting for it to finish"**: the command line
  and the web app share their caches, and only one of them downloads a new area at a time. The
  second one waits, then reuses the download.
- **To force a rebuild**: delete `cache/graph_*.pkl` to rebuild graphs from the saved `.osm`
  files, or delete the whole `cache/` folder to start from scratch.

## Development

```bash
.venv/bin/python -m unittest discover tests
```

The tests run the planner and the web app's API on a synthetic street grid, so they need no
Strava data or downloads. The [changelog](CHANGELOG.md) lists what's changed, and the
[roadmap](docs/roadmap.md) lists what might come next.

## Ideas / contributing

Pull requests are welcome. The [roadmap](docs/roadmap.md) has the current list of ideas,
including:

- A `--paved-only` option for road bikes (using OSM `surface` tags)
- Sync new activities from the Strava API instead of re-exporting
- A "directional" mode, so a road ridden the opposite way counts as new
- Elevation-aware routing (avoid or seek climbs)

## Credits

- Map data © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), available
  under the ODbL. Routes you generate are derived from OSM data.
- Built on [OSMnx](https://github.com/gboeing/osmnx), [SciPy](https://scipy.org),
  [fitparse](https://github.com/dtcooper/python-fitparse), [gpxpy](https://github.com/tkrajina/gpxpy),
  [Folium](https://python-visualization.github.io/folium/) and [Leaflet](https://leafletjs.com).

## License

MIT. See [LICENSE](LICENSE).
