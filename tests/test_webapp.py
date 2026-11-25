"""The web app's HTTP API end to end, on a synthetic street grid: python -m unittest discover tests"""
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pipeline  # noqa: E402
import progress  # noqa: E402
import webapp  # noqa: E402
from synthetic import LAT0, LON0, HillElevation, grid_graph, node  # noqa: E402

N = 25


class GridWorkspace(pipeline.Workspace):
    """No Strava history, and a synthetic grid for a road network: two-way streets for runs,
    alternating one-way east-west streets for rides. `delay` makes searches pretend to spend that
    long downloading, so they can be cancelled part-way."""

    def __init__(self, cache_dir):
        super().__init__(None, cache_dir)
        self.grids = {"run": grid_graph(N, mode="run"), "ride": grid_graph(N, mode="ride", one_way_rows=True)}
        self.delay = 0.0

    def graph_for(self, mode, start, end, distance_m, tolerance):
        if self.delay:
            progress.report("download", 0, 4)
            progress.sleep(self.delay)
        return self.grids[mode]


class WebAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.ws = GridWorkspace(cls.tmp.name)
        cls.app = webapp.App(cls.ws, export_problem="No export in tests.")
        cls.app.elevation = HillElevation()  # no downloads in tests
        cls.server = webapp.make_server(cls.app, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    # -- helpers --------------------------------------------------------------------

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Host": f"127.0.0.1:{self.port}"}
        if body is not None:
            body = body if isinstance(body, str) else json.dumps(body)
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        conn.request(method, path, body=body, headers=h)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res.status, {k.lower(): v for k, v in res.getheaders()}, data

    def get_json(self, path):
        status, _, data = self.request("GET", path)
        return status, json.loads(data)

    def submit(self, **body):
        status, _, data = self.request("POST", "/api/plan", body)
        self.assertEqual(status, 202, data)
        return json.loads(data)["job"]

    def wait(self, job, until=("done", "error", "cancelled"), seconds=30):
        for _ in range(int(seconds / 0.05)):
            _, s = self.get_json(f"/api/jobs/{job}")
            if s["status"] in until:
                return s
            time.sleep(0.05)
        self.fail(f"job {job} never reached {until}")

    def plan(self, **body):
        return self.wait(self.submit(**body))

    def point(self, i, j):
        rg, n = self.ws.grids["run"], node(N, i, j)
        return {"lat": float(rg.node_lat[n]), "lon": float(rg.node_lon[n])}

    @staticmethod
    def coords(route):
        return [p for k, run in enumerate(route["segments"]) for p in (run["coords"] if k == 0 else run["coords"][1:])]

    # -- searching ------------------------------------------------------------------

    def test_config(self):
        status, c = self.get_json("/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(c["activities"], {"ride": 0, "run": 0})
        self.assertEqual(c["usual_start"], {"ride": None, "run": None})
        self.assertEqual(c["export_problem"], "No export in tests.")
        self.assertIn("active_job", c)

    def test_loop_with_alternatives_elevation_gpx_and_nearby_roads(self):
        s = self.plan(mode="ride", distance_km=6, start=self.point(12, 12), end=None,
                      alternatives=2, effort="quick")
        self.assertEqual(s["status"], "done", s.get("error"))
        self.assertIsNone(s["progress"])
        res = s["result"]
        self.assertEqual(res["route_type"], "loop")
        self.assertTrue(1 <= len(res["routes"]) <= 2)
        self.assertTrue(any("Searching for the route" in line for line in s["log"]))  # progress reached the page
        best = res["routes"][0]
        self.assertLessEqual(best["distance_km"], 6 * 1.05 + 0.01)
        self.assertGreater(best["new_km"], 0)
        coords = self.coords(best)
        self.assertEqual(coords[0], coords[-1])  # a loop ends where it starts
        self.assertGreater(best["ascent_m"], 0)  # it climbs the synthetic hill
        self.assertEqual(len(best["profile"]["km"]), len(best["profile"]["m"]))

        status, headers, gpx = self.request("GET", best["gpx_url"])
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/gpx+xml")
        self.assertIn("attachment", headers["content-disposition"])
        self.assertIn(b"<trkpt", gpx)
        self.assertIn(b"<ele>", gpx)

        status, ctx = self.get_json(res["context_url"])
        self.assertEqual(status, 200)
        self.assertTrue(ctx["untraveled"])  # no history: every road is new
        self.assertEqual(ctx["traveled"], [])

    def test_one_way_finishes_at_the_finish(self):
        start, end = self.point(12, 4), self.point(12, 20)
        s = self.plan(mode="run", distance_km=6, start=start, end=end, alternatives=1, effort="quick")
        self.assertEqual(s["status"], "done", s.get("error"))
        route = s["result"]["routes"][0]
        self.assertEqual(s["result"]["route_type"], "oneway")
        self.assertLessEqual(route["distance_km"], 6 * 1.05 + 0.01)
        last = self.coords(route)[-1]
        self.assertAlmostEqual(last[0], end["lat"], places=4)
        self.assertAlmostEqual(last[1], end["lon"], places=4)

    def test_finish_out_of_reach_is_explained(self):
        s = self.plan(mode="ride", distance_km=2, start=self.point(12, 0), end=self.point(12, 24))
        self.assertEqual(s["status"], "error")
        self.assertIn("straight line", s["error"])

    # -- progress and cancelling (#1, #3) ----------------------------------------------

    def test_cancel_a_running_search(self):
        self.ws.delay = 10
        try:
            job = self.submit(mode="run", distance_km=5, start=self.point(12, 12))
            s = self.wait(job, until=("running",))
            for _ in range(100):  # wait for its first progress report
                if s["progress"]:
                    break
                time.sleep(0.05)
                _, s = self.get_json(f"/api/jobs/{job}")
            self.assertEqual((s["progress"]["stage"], s["progress"]["done"], s["progress"]["total"]),
                             ("download", 0, 4))
            self.assertGreater(s["progress"]["eta_s"], 0)
            t0 = time.time()
            status, _, _ = self.request("POST", f"/api/jobs/{job}/cancel")
            self.assertEqual(status, 200)
            s = self.wait(job)
            self.assertEqual(s["status"], "cancelled")
            self.assertEqual(s["stage"], "download")
            self.assertLess(time.time() - t0, 3)
        finally:
            self.ws.delay = 0

    def test_cancel_a_waiting_search(self):
        self.ws.delay = 10
        try:
            first = self.submit(mode="run", distance_km=5, start=self.point(12, 12))
            second = self.submit(mode="run", distance_km=5, start=self.point(12, 12))
            self.wait(first, until=("running",))
            _, s = self.get_json(f"/api/jobs/{second}")
            self.assertEqual(s["status"], "queued")
            status, _, data = self.request("POST", f"/api/jobs/{second}/cancel")
            self.assertEqual(json.loads(data)["status"], "cancelled")  # straight away
            self.request("POST", f"/api/jobs/{first}/cancel")
            self.assertEqual(self.wait(first)["status"], "cancelled")
            time.sleep(0.2)  # the worker must skip the cancelled one rather than run it
            self.assertEqual(self.get_json(f"/api/jobs/{second}")[1]["status"], "cancelled")
        finally:
            self.ws.delay = 0

    def test_estimate(self):
        status, e = self.get_json(f"/api/estimate?mode=ride&distance_km=10&start={LAT0},{LON0}")
        self.assertEqual(status, 200)
        self.assertEqual((e["ready"], e["tiles"], e["tiles_left"]), (False, 4, 4))
        self.assertGreaterEqual(e["minutes"], 1)
        ready = os.path.join(self.tmp.name, "graph_ride_51.5000_-0.1200_20000.pkl")
        open(ready, "wb").close()  # only the name matters for this check
        try:
            self.assertEqual(self.get_json(f"/api/estimate?mode=ride&distance_km=10&start={LAT0},{LON0}")[1],
                             {"ready": True})
        finally:
            os.remove(ready)
        self.assertEqual(self.get_json("/api/estimate?mode=swim&distance_km=10&start=1,1")[0], 400)
        self.assertEqual(self.get_json("/api/estimate?mode=ride&distance_km=10")[0], 400)

    # -- reversing loops (#2) ------------------------------------------------------------

    def test_reverse_a_loop(self):
        s = self.plan(mode="run", distance_km=5, start=self.point(12, 12), alternatives=1, effort="quick")
        route = s["result"]["routes"][0]
        status, _, data = self.request("POST", f"/api/jobs/{s['id']}/routes/0/reverse")
        self.assertEqual(status, 200, data)
        rev = json.loads(data)
        self.assertEqual(self.coords(rev), self.coords(route)[::-1])
        self.assertEqual(rev["distance_km"], route["distance_km"])
        _, again = self.get_json(f"/api/jobs/{s['id']}")  # the job now holds the reversed route
        self.assertEqual(self.coords(again["result"]["routes"][0]), self.coords(rev))

    def test_one_way_streets_get_a_detour(self):
        """On the one-way grid, a reversed ride loop can't simply run backwards: it goes round the block."""
        s = self.plan(mode="ride", distance_km=5, start=self.point(12, 12), alternatives=1, effort="quick")
        self.assertEqual(s["status"], "done", s.get("error"))
        route = s["result"]["routes"][0]
        status, _, data = self.request("POST", f"/api/jobs/{s['id']}/routes/0/reverse")
        self.assertEqual(status, 200, data)
        rev = json.loads(data)
        self.assertGreater(rev["detour_km"], 0)
        self.assertAlmostEqual(rev["distance_km"], route["distance_km"] + rev["detour_km"], delta=0.02)
        coords = self.coords(rev)
        self.assertEqual(coords[0], coords[-1])  # still a loop
        plan, rg = self.app.jobs[s["id"]].result.plans[0], self.ws.grids["ride"]
        allowed = set(zip(rg.arc_from.tolist(), rg.arc_to.tolist()))
        self.assertTrue(all((a, b) in allowed for a, b in zip(plan.nodes[:-1], plan.nodes[1:])))  # every step legal

    def test_only_loops_reverse(self):
        s = self.plan(mode="run", distance_km=6, start=self.point(12, 4), end=self.point(12, 20), alternatives=1)
        self.assertEqual(self.request("POST", f"/api/jobs/{s['id']}/routes/0/reverse")[0], 400)

    # -- saved routes (#4) -----------------------------------------------------------------

    def test_save_rename_mark_done_and_delete(self):
        s = self.plan(mode="run", distance_km=5, start=self.point(12, 12), alternatives=1, effort="quick")
        status, _, data = self.request("POST", "/api/saved", {"job": s["id"], "index": 0, "name": "  Morning\tloop "})
        self.assertEqual(status, 201, data)
        item = json.loads(data)
        self.assertEqual((item["name"], item["mode"], item["done"]), ("Morning loop", "run", False))
        self.assertIn(item["id"], [r["id"] for r in self.get_json("/api/saved")[1]["routes"]])

        status, full = self.get_json(f"/api/saved/{item['id']}")
        self.assertTrue(full["segments"])
        self.assertIsNotNone(full["profile"])
        status, headers, gpx = self.request("GET", f"/api/saved/{item['id']}.gpx")
        self.assertEqual(status, 200)
        self.assertIn('filename="morning-loop.gpx"', headers["content-disposition"])
        self.assertIn(b"<ele>", gpx)

        status, _, data = self.request("PATCH", f"/api/saved/{item['id']}", {"name": "Hill loop", "done": True})
        self.assertEqual(status, 200)
        self.assertEqual((json.loads(data)["name"], json.loads(data)["done"]), ("Hill loop", True))
        self.assertEqual(self.request("PATCH", f"/api/saved/{item['id']}", {"done": "yes"})[0], 400)

        self.assertEqual(self.request("DELETE", f"/api/saved/{item['id']}")[0], 200)
        self.assertEqual(self.get_json(f"/api/saved/{item['id']}")[0], 404)
        self.assertEqual(self.request("DELETE", f"/api/saved/{item['id']}")[0], 404)
        self.assertEqual(self.request("POST", "/api/saved", {"job": "0123456789ab", "index": 0})[0], 404)

    # -- input checking and safety -----------------------------------------------------

    def test_bad_requests(self):
        status, _, _ = self.request("POST", "/api/plan", "{}", {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, _, data = self.request("POST", "/api/plan", {"mode": "swim", "start": self.point(1, 1)})
        self.assertEqual(status, 400)
        self.assertIn("Mode", json.loads(data)["error"])
        status, _, _ = self.request("POST", "/api/plan", {"distance_km": "far", "start": self.point(1, 1)})
        self.assertEqual(status, 400)
        status, _, _ = self.request("POST", "/api/plan", {"start": {"lat": "x"}})
        self.assertEqual(status, 400)
        status, _, _ = self.request("POST", "/api/plan", "not json")
        self.assertEqual(status, 400)
        status, _ = self.get_json("/api/jobs/0123456789ab")
        self.assertEqual(status, 404)

    def test_other_sites_are_refused(self):
        status, _, _ = self.request("GET", "/api/config", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)  # DNS rebinding
        status, _, _ = self.request("POST", "/api/plan", {"start": self.point(1, 1)},
                                    {"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/plan", {"start": self.point(1, 1)}, {"Origin": "null"})
        self.assertEqual(status, 403)  # e.g. a sandboxed frame or a local file
        status, _, _ = self.request("DELETE", "/api/saved/0123456789ab", headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        status, _, _ = self.request("GET", "/api/config", headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)  # the planner's own page

    def test_static_files(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("text/html"))
        self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])
        self.assertIn(b"New Roads Planner", body)
        for path in ("/app.js", "/style.css", "/favicon.svg", "/favicon.ico"):
            self.assertEqual(self.request("GET", path)[0], 200, path)
        for path in ("/../webapp.py", "/%2e%2e/webapp.py", "/nope.html", "/api/nope"):
            self.assertEqual(self.request("GET", path)[0], 404, path)


if __name__ == "__main__":
    unittest.main()
