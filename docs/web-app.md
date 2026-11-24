# Web app guide

The web app is a point-and-click front end for the planner. It runs on your own computer:
`webapp.py` is a small local server, and the page in your browser talks only to it. It uses
the same planner, caches and Strava export as the command line (`newroutes.py`), so anything
one of them has downloaded or worked out, the other can reuse.

- [Starting and stopping it](#starting-and-stopping-it)
- [Planning a route](#planning-a-route)
- [More options](#more-options)
- [Reading the results](#reading-the-results)
- [Using it from your phone](#using-it-from-your-phone)
- [Troubleshooting](#troubleshooting)

## Starting and stopping it

Install the tool as in the [README](../README.md#2-install), then:

```bash
.venv/bin/python webapp.py --export ~/Downloads/export_12345678.zip
```

The terminal shows how many activities it loaded. Then your browser opens
<http://127.0.0.1:8765>. The export path is remembered (in `cache/settings.json`), so next
time `.venv/bin/python webapp.py` is enough. Pass `--export` again when you have a newer
export.

| Flag | Meaning |
|---|---|
| `--export PATH` | Your Strava export, as a folder or `.zip`. Remembered after first use. |
| `--port N` | Serve on another port (default 8765). |
| `--host ADDRESS` | Which network address to listen on. The default, `127.0.0.1`, means this computer only. See [Using it from your phone](#using-it-from-your-phone). |
| `--no-browser` | Don't open a browser tab. |

Without an export, the app still starts, but every road counts as new. The page shows a warning
when that happens.

To stop it, press **Ctrl+C** in the terminal. If a road-data download was in progress, the
finished parts are kept and the download resumes next time.

## Planning a route

The panel on the left (below the map on a phone) is the whole form. Your choices are
remembered in this browser for next time.

### Activity: Ride or Run

- **Ride** routes on the bike network: all roads except motorways, plus cycleways, paths and
  footways that allow bikes. One-way streets are respected. Your rides count as traveled.
- **Run** routes on everything walkable, in both directions. Your runs, walks and hikes count
  as traveled.

Each activity keeps its own distance. Ride defaults to 40 km and Run to 10 km.

### Route: Loop or A → B

- **Loop** finishes back at the start.
- **A → B** finishes somewhere else, such as a ride to work or a run to a café. A **Finish**
  section appears under the start.

### Start and finish

The start defaults to **your usual start**: the place most of your activities for that
activity type begin, usually home. You can change it in several ways:

| How | What to do |
|---|---|
| Search | Type a place or address and press Enter (or the magnifier). Pick one of the matches. |
| Pick on map | Press **Pick on map**, then click the map. Esc cancels. |
| Click the map | Click anywhere and choose **Start here** or **Finish here**. |
| Drag | Drag the **S** or **F** marker. |
| Usual start | Go back to your usual start for the current activity. |
| My location | Use your browser's location. It asks for permission the first time. |

**Swap start and finish** reverses an A → B route. The route starts and ends at the nearest
junction you can ride or run on. If you drop a pin more than 1 km from any such road, the
planner asks you to move it.

### Distance

Use the slider (5–200 km for rides, 2–60 km for runs) or type any value up to 400 km. Routes
can run up to 5% over the distance. The hints under the slider tell you:

- For **A → B**, how far the finish is in a straight line. You get a warning when the distance
  can't reach it, or leaves little room for detours: the distance should be at least about 1.5
  times the straight line to collect much new road.
- Whether **road data for this area is downloaded**. If it isn't, the first route there
  downloads it from OpenStreetMap. That's a one-off that can take 2–30 minutes, and longer
  routes need bigger areas. Downloaded areas appear on the map as dashed circles.

### Generate routes

Press **Generate routes**. A progress card shows what the planner is doing and how long it's
taken. **Details** shows the planner's own messages. Routes in a downloaded area take seconds.
A new area takes longer because of the download.

You can close or reload the page while it works. When you come back, it picks the search up
again, or shows the finished routes. The page keeps showing your latest routes after a reload
until you stop `webapp.py`. Searches run one at a time.

## More options

| Option | What it does |
|---|---|
| **Routes to suggest** (1, 2 or 3) | How many different routes to show. Extra suggestions share less than half their roads with each other. Asking for more than one roughly doubles the search time. |
| **Main roads** | *Don't mind* treats all roads alike. *Avoid* (the default) makes trunk, primary and secondary roads count as longer, so routes prefer quieter streets. *Avoid strongly* doubles that. |
| **Chase new roads** (0–0.95) | How strongly the links between areas of new road detour onto new streets. Higher finds more new road but zig-zags more. The default is 0.6. |
| **Search effort** | *Quick*, *Normal* or *Thorough*: how hard the planner searches. Thorough takes about four times as long as Normal and usually finds a little more new road. |
| **Count roads from every activity type as done** | For example, a road you've run counts as done when planning a ride. |

These match the command line's `--alternatives`, `--avoid-main-roads`, `--eagerness`,
`--restarts`/`--iterations` and `--count-all-activities`.

## Reading the results

Each suggestion appears as a card showing its length and how much of it is new to you. The bar
shows the same split, red for new and blue for done. The card marked **most new road** is the
planner's best route.

On the map:

| Line | Meaning |
|---|---|
| Red | The chosen route, on roads new to you |
| Blue | The chosen route, on roads you've done before |
| Purple | The other suggestions. Click one, or its card, to choose it. |
| Dashed orange | Other untraveled roads around the routes |
| Grey | Roads you've done. This layer is off by default. |
| Dashed circle | Areas whose road data is downloaded |

The **Map key** in the corner switches the background layers on and off, and folds away.

**GPX** downloads that route, ready for Garmin Connect (Courses), Komoot, Ride with GPS, or
copying straight to a bike computer or watch. Files are named like
`ride_41km_20261124-0930_option1.gpx`.

The planner starts fresh every time, so **Generate routes** again for different suggestions.
If you change the settings after generating, the results are marked out of date until you
generate again.

The line above the cards says how much of the area within reach you've already covered, for
example *"You've done 2,140 of the 5,980 km of roads within 15 km of the start (36%)"*.

## Using it from your phone

The app can run on your computer and be used from a phone or tablet on the same Wi-Fi:

```bash
.venv/bin/python webapp.py --host 0.0.0.0
```

Then open `http://<your computer's IP address>:8765` on the phone. On a Mac, the IP address is
in System Settings → Wi-Fi → Details. Keep these in mind:

- **Anyone on that network can use it.** There's no password, so only do this on a network you
  trust, such as home Wi-Fi rather than a café's.
- **My location won't work on the phone.** Browsers only share location with secure (HTTPS)
  pages or the computer's own address. Search or tap the map instead.
- The computer has to stay awake and running the app.

## Troubleshooting

| Problem | Fix |
|---|---|
| "Can't reach the planner" | The terminal running `webapp.py` was closed or stopped. Start it again and reload the page. |
| "Couldn't listen on 127.0.0.1:8765" | The app is already running in another terminal, so use that one, or start with `--port 8766`. |
| "No Strava history loaded" | Start once with `--export` pointing at your export. If you've moved the export, use its new location. |
| A search finds nothing | Add the suburb or town. Results near the area the map is showing come first. |
| "The start is … from the nearest road you can ride on" | The pin is in a park, water or private land. Drag it onto a street. |
| "The most direct way from start to finish is about … km" | That finish needs a longer distance. Raise it, or move the finish closer. |
| The download is slow, or keeps retrying | The public OpenStreetMap servers are busy. Leave it running, or stop and try later; finished parts are kept. |
| A route uses a road you'd rather avoid | Set **Main roads** to *Avoid strongly*, generate again, or pick another suggestion. Always check a route before you go. |
