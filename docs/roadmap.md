# Roadmap

Where the project is and where it could go next. Nothing here is a commitment. It's a list to
choose from, roughly in order of value for effort within each section. See the
[changelog](../CHANGELOG.md) for what's already been done.

## Done

- **v0.1, command-line prototype.** Strava bulk-export parsing, OpenStreetMap road download,
  traveled-road matching, and an orienteering-style search for loops and one-way routes, with
  GPX, HTML map and JSON output.
- **v0.2, local web app.** A point-and-click planner. Choose ride or run and loop or A → B, set
  the start and finish by search, map click or usual start, compare up to three suggestions,
  and download GPX. Also: a shared pipeline for both front ends, the export path remembered,
  a fix for one-way routes overshooting the distance, and the first automated tests.

## Next: web app

- **Cancel a running search.** Downloads can take a while. A cancel button needs the download
  loop and the search to check a stop flag between tiles or restarts.
- **Direction and distance markers** on the route: arrows, and a marker every 5 or 10 km. For
  loops, a **reverse direction** button (one-way streets permitting on rides).
- **Download progress as a percentage**, and an estimate before starting ("about 25 tiles").
- **Saved routes.** Keep routes you like with a name and date, and mark routes as done.
- **Don't suggest the same thing twice.** Penalise roads in recently suggested routes, so
  "generate again" explores further.
- **Via points and areas to avoid.** Click to add a "go through here" point, or draw an area
  to keep out of.
- **Elevation profile** for the chosen route, once elevation data is in (see below).
- **Guard against two processes downloading the same area at once**, for example the command
  line and the web app. A lock file per tile would do it.

## Next: planner

- **Paved-only option** for road bikes, using OSM `surface` and `tracktype` tags.
- **Directional mode,** so a road ridden in the other direction counts as new.
- **Elevation-aware routing:** avoid or seek climbs, and report total ascent.
- **A better fallback when the finish is barely reachable.** Today, if the cheapest-by-preference
  path to the finish is over budget, the planner gives up, even when the plain shortest path
  would fit.
- **Time budgets** ("a 90-minute ride") using a typical speed per activity.
- **Surface and road-type preferences** beyond "avoid main roads", such as preferring bike
  paths or avoiding gravel.

## Next: data

- **Sync from the Strava API** instead of re-exporting. This needs an API application and
  OAuth, and has to stay within Strava's terms for personal use.
- **Other sources:** a folder of GPX/FIT files (Garmin, Wahoo, Coros), or a Garmin Connect
  export.
- **Wandrer-style stats:** % of each suburb or town done, and progress over time.

## Later: a hosted version

Running on your own computer keeps everything private and simple, but it means having a
computer running. A hosted version (a phone-friendly site, or an app) would need:

- **Somewhere to run the planner.** It needs a few hundred MB of memory per area and tens of
  seconds of CPU per search, so a small server or serverless jobs with a cache of road graphs.
- **Accounts and the Strava API** in place of the bulk export, which in turn means Strava's API
  agreement, rate limits, and storing people's location history securely.
- **Privacy design:** a person's activity history reveals where they live and their routines.
  It would need clear retention rules, deletion, and no public sharing by default.
- **OpenStreetMap usage policies:** the public Overpass, Nominatim and tile servers aren't meant
  for a busy service, so it would need its own extracts or paid providers.

A lighter step first: package the local app so it starts with a double-click, and use
`--host 0.0.0.0` (already supported) for phones on the home network.

## Open questions

- Should "new" have a time limit, so roads you haven't been on for, say, five years count as new
  again?
- Should the planner use a stricter or looser GPS matching distance than 20 m in dense CBD
  streets, where parallel roads are close together?
