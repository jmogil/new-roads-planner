# Changelog

All notable changes to this project.

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
