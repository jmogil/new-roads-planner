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
import webapp  # noqa: E402
from synthetic import grid_graph, node  # noqa: E402

N = 25


class GridWorkspace(pipeline.Workspace):
    """A workspace with no Strava history whose road network is always the synthetic grid."""

    def __init__(self, cache_dir):
        super().__init__(None, cache_dir)
        self.grid = grid_graph(N)

    def graph_for(self, mode, start, end, distance_m, tolerance):
        return self.grid


class WebAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.ws = GridWorkspace(cls.tmp.name)
        cls.app = webapp.App(cls.ws, export_problem="No export in tests.")
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

    def plan(self, **body):
        status, _, data = self.request("POST", "/api/plan", body)
        self.assertEqual(status, 202, data)
        job = json.loads(data)["job"]
        for _ in range(600):
            _, s = self.get_json(f"/api/jobs/{job}")
            if s["status"] in ("done", "error"):
                return s
            time.sleep(0.05)
        self.fail("the search didn't finish")

    def point(self, i, j):
        rg, n = self.ws.grid, node(N, i, j)
        return {"lat": float(rg.node_lat[n]), "lon": float(rg.node_lon[n])}

    # -- tests ----------------------------------------------------------------------

    def test_config(self):
        status, c = self.get_json("/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(c["activities"], {"ride": 0, "run": 0})
        self.assertEqual(c["usual_start"], {"ride": None, "run": None})
        self.assertEqual(c["export_problem"], "No export in tests.")
        self.assertIn("active_job", c)

    def test_loop_with_alternatives_gpx_and_nearby_roads(self):
        s = self.plan(mode="ride", distance_km=6, start=self.point(12, 12), end=None,
                      alternatives=2, effort="quick")
        self.assertEqual(s["status"], "done", s.get("error"))
        res = s["result"]
        self.assertEqual(res["route_type"], "loop")
        self.assertTrue(1 <= len(res["routes"]) <= 2)
        self.assertTrue(any("Searching for the route" in line for line in s["log"]))  # progress reached the page
        best = res["routes"][0]
        self.assertLessEqual(best["distance_km"], 6 * 1.05 + 0.01)
        self.assertGreater(best["new_km"], 0)
        first, last = best["segments"][0]["coords"][0], best["segments"][-1]["coords"][-1]
        self.assertEqual(first, last)  # a loop ends where it starts

        status, headers, gpx = self.request("GET", best["gpx_url"])
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/gpx+xml")
        self.assertIn("attachment", headers["content-disposition"])
        self.assertIn(b"<trkpt", gpx)

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
        last = route["segments"][-1]["coords"][-1]
        self.assertAlmostEqual(last[0], end["lat"], places=4)
        self.assertAlmostEqual(last[1], end["lon"], places=4)

    def test_finish_out_of_reach_is_explained(self):
        s = self.plan(mode="ride", distance_km=2, start=self.point(12, 0), end=self.point(12, 24))
        self.assertEqual(s["status"], "error")
        self.assertIn("straight line", s["error"])

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
        status, _, _ = self.request("GET", "/api/config", headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)  # the planner's own page

    def test_static_files(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("text/html"))
        self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])
        self.assertIn(b"New Roads Planner", body)
        for path in ("/app.js", "/style.css", "/favicon.svg"):
            self.assertEqual(self.request("GET", path)[0], 200, path)
        for path in ("/../webapp.py", "/%2e%2e/webapp.py", "/nope.html", "/api/nope"):
            self.assertEqual(self.request("GET", path)[0], 404, path)


if __name__ == "__main__":
    unittest.main()
