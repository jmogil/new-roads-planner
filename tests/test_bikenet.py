"""Matching a local cycle network onto the road graph, without downloading anything:
python -m unittest discover tests"""
import json
import os
import sys
import tempfile
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bikenet  # noqa: E402
from synthetic import LAT0, LON0, grid_graph, node  # noqa: E402

N = 25
TEST_SOURCE = bikenet.Source(
    id="testville", name="Testville bike network", short="Testville's bike network",
    bbox=(LON0 - 0.1, LAT0 - 0.1, LON0 + 0.1, LAT0 + 0.1),
    url="http://127.0.0.1:9/nothing-here",  # the discard port: downloads fail at once
    category_field="ovl2_cat", classes={"PCR": bikenet.PRIMARY, "LCR": bikenet.LOCAL},
    attribution="Testville Council")


def feature(code, coords, kind="LineString"):
    return {"type": "Feature", "properties": {"ovl2_cat": code}, "geometry": {"type": kind, "coordinates": coords}}


def grid_line(rg, points, north_m=0.0):
    """[lon, lat] along grid junctions (row, column), shifted north by north_m metres."""
    return [[float(rg.node_lon[node(N, i, j)]), float(rg.node_lat[node(N, i, j)]) + north_m / 110_540]
            for i, j in points]


def edge_between(rg, a, b):
    hits = np.where(((rg.edge_u == a) & (rg.edge_v == b)) | ((rg.edge_u == b) & (rg.edge_v == a)))[0]
    return int(hits[0])


class BikeNetworkTests(unittest.TestCase):
    def setUp(self):
        self.rg = grid_graph(N)
        data = {"features": [
            # a primary route 5 m north of row 12, from column 2 to column 22
            feature("PCR", grid_line(self.rg, [(12, 2), (12, 22)], north_m=5)),
            # a local route straight up column 8, crossing rows 13 to 16
            feature("LCR", [grid_line(self.rg, [(13, 8), (16, 8)])], kind="MultiLineString"),
            feature("XYZ", grid_line(self.rg, [(20, 0), (20, 24)])),  # a category we don't use
        ]}
        self.net = bikenet.Network.from_geojson(TEST_SOURCE, data, version="v1")

    def test_reads_lines_and_categories(self):
        self.assertEqual(len(self.net.lines), 2)  # the unknown category is skipped
        self.assertEqual(self.net.classes.tolist(), [bikenet.PRIMARY, bikenet.LOCAL])
        shown = self.net.display()
        self.assertEqual(len(shown["classes"]["primary"]), 1)
        self.assertEqual(shown["classes"]["primary"][0][0][0], round(self.net.lines[0][0, 1], 5))  # [lat, lon]

    def test_property_names_are_matched_whatever_their_case(self):
        arcgis_style = {"features": [{"properties": {"OVL2_CAT": "PCR"},
                                      "geometry": {"type": "LineString", "coordinates": [[0, 0], [0.001, 0]]}}]}
        self.assertEqual(len(bikenet.Network.from_geojson(TEST_SOURCE, arcgis_style, "v").lines), 1)

    def test_roads_along_a_route_match_and_crossing_streets_do_not(self):
        cls = bikenet.match(self.rg, self.net)
        for j in range(2, 22):  # every block of row 12 under the primary route
            self.assertEqual(cls[edge_between(self.rg, node(N, 12, j), node(N, 12, j + 1))], bikenet.PRIMARY)
        for i in range(13, 16):  # column 8 under the local route
            self.assertEqual(cls[edge_between(self.rg, node(N, i, 8), node(N, i + 1, 8))], bikenet.LOCAL)
        self.assertEqual(cls[edge_between(self.rg, node(N, 12, 0), node(N, 12, 1))], bikenet.NONE)  # past the end
        self.assertEqual(cls[edge_between(self.rg, node(N, 20, 5), node(N, 20, 6))], bikenet.NONE)  # unused category
        self.assertEqual(int((cls > 0).sum()), 20 + 3)

    def test_the_direction_check_keeps_out_crossings(self):
        # count a road as matched if even a sliver of it is: row 14's blocks either side of column 8
        # have samples within 15 m of the local route, but at right angles to it
        cls = bikenet.match(self.rg, self.net, min_share=0.01)
        for j in (7, 8):
            self.assertEqual(cls[edge_between(self.rg, node(N, 14, j), node(N, 14, j + 1))], bikenet.NONE)
        loose = bikenet.match(self.rg, self.net, min_share=0.01, max_angle=180)  # no direction check
        self.assertEqual(loose[edge_between(self.rg, node(N, 14, 7), node(N, 14, 8))], bikenet.LOCAL)

    def test_weights(self):
        classes = np.array([bikenet.NONE, bikenet.PRIMARY, bikenet.SECONDARY, bikenet.LOCAL, bikenet.RIVERWALK])
        np.testing.assert_allclose(bikenet.cost_factors(classes, 0.0), 1.0)
        np.testing.assert_allclose(bikenet.value_factors(classes, 0.0), 1.0)
        cost, value = bikenet.cost_factors(classes, 1.0), bikenet.value_factors(classes, 1.0)
        self.assertEqual((cost[0], value[0]), (1.0, 1.0))  # off the network: unchanged
        self.assertTrue(cost[1] < cost[2] < cost[3] < 1.0 and value[1] > value[2] > value[3] > 1.0)
        self.assertEqual((cost[4], value[4]), (cost[1], value[1]))  # the RiverWalk ranks with primary routes

    def test_matches_are_cached_per_graph_and_data_version(self):
        with tempfile.TemporaryDirectory() as cache:
            calls = []
            real = bikenet.match
            bikenet.match = lambda rg, net: calls.append(1) or real(rg, net)
            try:
                first = bikenet.edge_classes(self.rg, self.net, cache)
                again = bikenet.edge_classes(self.rg, self.net, cache)
                self.net.version = "v2"  # new data: match again
                bikenet.edge_classes(self.rg, self.net, cache)
            finally:
                bikenet.match = real
            np.testing.assert_array_equal(first, again)
            self.assertEqual(len(calls), 2)

    def test_load_keeps_the_old_copy_when_a_refresh_fails(self):
        with tempfile.TemporaryDirectory() as cache:
            with self.assertRaises(bikenet.Unavailable):  # nothing downloaded yet, and no server
                bikenet.load(cache, TEST_SOURCE)
            path = os.path.join(cache, "bikenet", "testville.geojson")
            with open(path, "w") as fh:
                json.dump({"features": [feature("PCR", [[0, 0], [0.001, 0]])]}, fh)
            self.assertEqual(len(bikenet.load(cache, TEST_SOURCE).lines), 1)  # fresh: used as it is
            old = time.time() - 200 * 86400
            os.utime(path, (old, old))  # stale, and the refresh will fail
            self.assertEqual(len(bikenet.load(cache, TEST_SOURCE).lines), 1)

    def test_sources(self):
        self.assertEqual(bikenet.source_for(-27.47, 153.02).id, "brisbane")  # Brisbane CBD
        self.assertIsNone(bikenet.source_for(51.5, -0.12))                    # London: no network yet
        self.assertIsNone(bikenet.get_source("nowhere"))


if __name__ == "__main__":
    unittest.main()
