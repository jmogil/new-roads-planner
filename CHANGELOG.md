# Changelog

All notable changes to this project.

## 0.4.0, 2026-11-26

### Added

- **Brisbane's bike network.** Rides can favour Brisbane City Council's bicycle network: the City
  Plan 2014 bicycle network overlay's primary, secondary and local cycle routes and the RiverWalk,
  CC BY 4.0.
  - A **Bike network** switch and strength dial appear for rides starting or finishing in
    Brisbane City. They're on by default at "balanced".
  - Favouring the network makes its roads cheaper to route along and its new road worth more.
    On a test loop, *balanced* took the share of the route on the network from about 40% to two
    thirds while giving up very little new road. Full strength took it past 80%.
  - Each suggestion says how far it runs on the network.
  - `--bike-network 0..1` does the same on the command line.
- **Bike network overlay**: a map button that shows the whole network by route class, with the
  council's attribution.
- `bikenet.py`: council cycle networks, downloaded once (refreshed every 90 days) and matched
  onto the road graph. A road is on a route when the route runs within 15 m in the same
  direction along at least half of it. Sources are listed in a table, so other cities can be
  added.

### Changed

- The search now maximises a route's *value*: new metres, weighted towards the bike network when
  it's favoured. With it off the value is exactly the new metres, so routes are unchanged (the
  same seed gives the same routes).

## 0.3.0, 2026-11-25

### Added (web app)

- **Cancel a running search** ([#1](https://github.com/jmogil/new-roads-planner/issues/1)).
  It stops within a second or so, and a cancelled download keeps its finished parts, so the
  next search there picks up where it stopped.
- **Progress and estimates** ([#3](https://github.com/jmogil/new-roads-planner/issues/3)):
  - Before you start, the hint says whether an area's road data still needs downloading, how
    many parts that is, and roughly how long it will take. The estimate learns from this
    computer's own download times.
  - While it works, a progress bar shows downloads part by part and search progress, with the
    time left.
- **Direction and distance markers** ([#2](https://github.com/jmogil/new-roads-planner/issues/2)):
  arrows along the chosen route, and km markers every 5 km for rides and 1 km for runs, spaced
  for the zoom level. **Reverse** turns a loop around. On the bike network, one-way stretches
  get the shortest legal way round, and the page says how much those detours add.
- **Elevation profile** ([#7](https://github.com/jmogil/new-roads-planner/issues/7)) for the
  chosen route:
  - The profile is coloured by new and done road, and pointing at it marks the spot on the map.
  - Each suggestion shows its total climb, and GPX downloads now include elevations.
  - The data is SRTM, from the AWS Terrain Tiles open dataset, downloaded a 1° square at a time
    and cached.
- **Saved routes** ([#4](https://github.com/jmogil/new-roads-planner/issues/4)): save a
  suggestion, then show it again, download its GPX, rename it, mark it done or delete it. Saved
  routes are kept in `cache/saved_routes/`.
- **Clear** button, to remove the routes and finish and go back to your usual start. **Reset
  these options** under More options puts the options back to their defaults.
- **Map / Aerial** switch for Esri aerial photos with place names. Routes and roads are drawn
  brighter on the photos.
- A new app icon: a road across a map. It also comes as a home-screen icon for phones.

### Changed

- "Roads you've done" is now drawn in a saturated teal, so it stands out from the grey map,
  with brighter versions for dark mode and the aerial photos.

### Fixed

- **Two processes downloading the same new area at once could corrupt it**
  ([#8](https://github.com/jmogil/new-roads-planner/issues/8)). That could happen with the
  command line and the web app, which share `cache/`. Downloads are now locked per area: the
  second process waits, then reuses the download. `tracks.pkl` is also written safely now.

## 0.2.0, 2026-11-24

### Added

- **Web app** (`webapp.py` and `web/`): a point-and-click planner in the browser, served from
  your own computer. You can:
  - choose Ride or Run, and a Loop or an A → B route
  - set the start and finish by place search, by clicking or dragging on the map, from your
    usual start, or from your current location
  - get hints when an A → B finish is too far for the distance, and when an area's road data
    still needs downloading
  - compare up to three suggestions on the map, with new road red and done road blue, and
    download each as GPX
  - see live progress, even across page reloads

  See [docs/web-app.md](docs/web-app.md).
- `pipeline.py`: the planning steps shared by the command line and the web app, with
  plain-language errors for problems you can fix.
- The Strava export path is remembered after first use (`cache/settings.json`).
- Tests for the planner and the web API on a synthetic street grid:
  `python -m unittest discover tests`.
- Documentation: [architecture](docs/architecture.md), [web app guide](docs/web-app.md) and
  [roadmap](docs/roadmap.md).

### Fixed

- **One-way (A → B) routes could overshoot the requested distance** by up to the direct
  start-to-finish distance, because the direct leg wasn't counted against the budget. For
  example, a 6 km request came back as 9.2 km. Routes now stay within the distance plus its 5%
  tolerance.
- A finish that couldn't be reached crashed the planner. Now you get a message saying what
  distance would work.

### Changed

- When several cached road graphs cover an area, the smallest is used, which makes planning
  faster.
- Road graph and coverage caches are written via a temporary file, so interrupting a save
  can't corrupt them.
- The command line's options and output files are unchanged. With the same `--seed` it
  produces the same routes as before, except one-way routes, which the fix above affects.

## 0.1.0, 2026-11-23

- First version: the command-line planner (`newroutes.py`). It reads a Strava bulk export
  (FIT, GPX and TCX), downloads OpenStreetMap roads through Overpass, marks the roads you've
  traveled, and searches for loops or one-way routes with the most new road. Output is GPX,
  an HTML map and a JSON summary.
