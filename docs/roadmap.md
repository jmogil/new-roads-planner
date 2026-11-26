# Roadmap

Where the project is and where it could go next. Nothing here is a commitment. It's a list to
choose from, roughly in order of value for effort within each section. See the
[changelog](../CHANGELOG.md) for what's already been done.

Each item has a [GitHub issue](https://github.com/jmogil/new-roads-planner/issues) with the
details, a "done when" checklist and where to start in the code. Work is tracked and discussed
there. The issues are labelled by area:
[web app](https://github.com/jmogil/new-roads-planner/labels/web%20app),
[planner](https://github.com/jmogil/new-roads-planner/labels/planner),
[data](https://github.com/jmogil/new-roads-planner/labels/data) and
[hosting](https://github.com/jmogil/new-roads-planner/labels/hosting).

## Done

- **v0.1, command-line prototype.** Strava bulk-export parsing, OpenStreetMap road download,
  traveled-road matching, and an orienteering-style search for loops and one-way routes, with
  GPX, HTML map and JSON output.
- **v0.2, local web app.** A point-and-click planner. Choose ride or run and loop or A → B, set
  the start and finish by search, map click or usual start, compare up to three suggestions,
  and download GPX. Also: a shared pipeline for both front ends, the export path remembered,
  a fix for one-way routes overshooting the distance, and the first automated tests.
- **v0.3, web app round two.** Covers:
  - Cancelling a search ([#1](https://github.com/jmogil/new-roads-planner/issues/1)).
  - Direction arrows, km markers and reversing loops
    ([#2](https://github.com/jmogil/new-roads-planner/issues/2)).
  - Download progress and estimates ([#3](https://github.com/jmogil/new-roads-planner/issues/3)).
  - Saved routes ([#4](https://github.com/jmogil/new-roads-planner/issues/4)).
  - Elevation profiles ([#7](https://github.com/jmogil/new-roads-planner/issues/7)).
  - A lock so the command line and the web app can't corrupt a shared download
    ([#8](https://github.com/jmogil/new-roads-planner/issues/8)).
  - Also: a Clear button, an aerial view, a clearer colour for roads you've done, and a new icon.
- **v0.4, Brisbane's bike network.** Rides can favour Brisbane City Council's cycle routes with a
  strength dial, the network can be shown on the map, and routes report how much of them is on it.

## Next: web app

- **Don't suggest the same thing twice**
  ([#5](https://github.com/jmogil/new-roads-planner/issues/5)). Steer away from roads in
  recent suggestions, so "generate again" explores further.
- **Via points and areas to avoid** ([#6](https://github.com/jmogil/new-roads-planner/issues/6)).
  Click to add a "go through here" point, or draw an area to keep out of.

## Next: bike networks

- **More councils' networks.** Moreton Bay, Logan, Ipswich and other cities could each be added as
  an entry in `bikenet.SOURCES` where they publish their cycle networks as open data.
- **Cycle infrastructure anywhere, from OpenStreetMap.** OSM tags cycleways, bike lanes and
  signed bike routes worldwide. Reading those tags when building the graph would let rides favour
  cycle infrastructure everywhere, with council data adding detail where it exists.

## Next: planner

- **Paved-only option** for road bikes, using OSM `surface` and `tracktype` tags
  ([#9](https://github.com/jmogil/new-roads-planner/issues/9)).
- **Directional mode,** so a road ridden in the other direction counts as new
  ([#10](https://github.com/jmogil/new-roads-planner/issues/10)).
- **Elevation-aware routing:** avoid or seek climbs
  ([#11](https://github.com/jmogil/new-roads-planner/issues/11)). The web app already has the
  elevation data and reports each route's climbing, so this is about using it in the search.
- **A better fallback when the finish is barely reachable**
  ([#12](https://github.com/jmogil/new-roads-planner/issues/12)). Today, if the
  cheapest-by-preference path to the finish is over budget, the planner gives up, even when
  the plain shortest path would fit.
- **Time budgets** ("a 90-minute ride") using a typical speed per activity
  ([#13](https://github.com/jmogil/new-roads-planner/issues/13)).
- **Surface and road-type preferences** beyond "avoid main roads", such as preferring bike
  paths or avoiding gravel ([#14](https://github.com/jmogil/new-roads-planner/issues/14)).

## Next: data

- **Sync from the Strava API** instead of re-exporting
  ([#15](https://github.com/jmogil/new-roads-planner/issues/15)). This needs an API
  application and OAuth, and has to stay within Strava's terms for personal use.
- **Other sources:** a folder of GPX/FIT files (Garmin, Wahoo, Coros), or a Garmin Connect
  export ([#16](https://github.com/jmogil/new-roads-planner/issues/16)).
- **Wandrer-style stats:** % of each suburb or town done, and progress over time
  ([#17](https://github.com/jmogil/new-roads-planner/issues/17)).

## Later: a hosted version

Running on your own computer keeps everything private and simple, but it means having a
computer running. A hosted version (a phone-friendly site, or an app) would need the following.
It's being worked through in [#18](https://github.com/jmogil/new-roads-planner/issues/18).

- **Somewhere to run the planner.** It needs a few hundred MB of memory per area and tens of
  seconds of CPU per search, so a small server or serverless jobs with a cache of road graphs.
- **Accounts and the Strava API** in place of the bulk export, which in turn means Strava's API
  agreement, rate limits, and storing people's location history securely.
- **Privacy design:** a person's activity history reveals where they live and their routines.
  It would need clear retention rules, deletion, and no public sharing by default.
- **OpenStreetMap usage policies:** the public Overpass, Nominatim and tile servers aren't meant
  for a busy service, so it would need its own extracts or paid providers.

A lighter step first: package the local app so it starts with a double-click
([#19](https://github.com/jmogil/new-roads-planner/issues/19)), and use `--host 0.0.0.0`
(already supported) for phones on the home network.

## Open questions

- Should "new" have a time limit, so roads you haven't been on for, say, five years count as new
  again? ([#20](https://github.com/jmogil/new-roads-planner/issues/20))
- Should the planner use a stricter or looser GPS matching distance than 20 m in dense CBD
  streets, where parallel roads are close together?
  ([#21](https://github.com/jmogil/new-roads-planner/issues/21))
