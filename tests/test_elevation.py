"""Elevation lookups and route profiles, without downloading anything: python -m unittest discover tests"""
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import elevation  # noqa: E402
from synthetic import LAT0, LON0, HillElevation  # noqa: E402


def straight_route(km=4.0, split=0.5):
    """A west-to-east route through the middle of the synthetic hill: new road, then done road."""
    half = km / 2 / (111.32 * np.cos(np.radians(LAT0)))
    lons = np.linspace(LON0 - half, LON0 + half, 51)
    cut = int(len(lons) * split)
    return [{"new": True, "coords": [[LAT0, float(lo)] for lo in lons[:cut + 1]]},
            {"new": False, "coords": [[LAT0, float(lo)] for lo in lons[cut:]]}]


class ElevationTests(unittest.TestCase):
    def test_tile_names(self):
        self.assertEqual(elevation.tile_name(51.5, -0.12), "N51W001")
        self.assertEqual(elevation.tile_name(-33.87, 151.21), "S34E151")
        self.assertEqual(elevation.tile_name(0.5, 0.5), "N00E000")

    def test_heights_from_a_cached_tile(self):
        with tempfile.TemporaryDirectory() as cache:
            os.makedirs(os.path.join(cache, "elevation"))
            n = 11  # a tiny tile for N51W001, rising 10 m per column from west to east
            grid = np.tile(np.arange(n, dtype=np.int16) * 10, (n, 1))
            grid[0, 0] = elevation.VOID  # no data in the north-west corner
            np.save(os.path.join(cache, "elevation", "N51W001.npy"), grid)
            elev = elevation.Elevation(cache)
            h = elev.heights([51.5, 51.5, 51.999], [-0.5, -0.75, -0.999])
            self.assertAlmostEqual(h[0], 50.0)  # halfway across
            self.assertAlmostEqual(h[1], 25.0)  # a quarter of the way: interpolated
            self.assertTrue(np.isnan(h[2]))     # touches the void
            # a square that isn't cached, without permission to download: unknown
            self.assertTrue(np.isnan(elev.heights([10.0], [10.0], download=False)[0]))

    def test_climbing_ignores_small_wobbles(self):
        up, down = elevation.climb(np.array([0, 1, 0, 1, 0, 10, 5, 12, 0]), threshold=2.0)
        self.assertEqual((up, down), (17.0, 17.0))

    def test_profile_over_a_hill(self):
        prof = elevation.profile(straight_route(), HillElevation())
        self.assertEqual(len(prof["km"]), len(prof["m"]))
        self.assertEqual(len(prof["km"]), len(prof["new"]))
        self.assertAlmostEqual(prof["km"][-1], 4.0, delta=0.05)
        self.assertAlmostEqual(prof["max_m"], 120, delta=3)
        self.assertAlmostEqual(prof["ascent_m"], 95, delta=8)
        self.assertAlmostEqual(prof["descent_m"], prof["ascent_m"], delta=3)
        self.assertTrue(prof["new"][0])     # first half is new road...
        self.assertFalse(prof["new"][-1])   # ...second half is done
        self.assertEqual((prof["lat"][0], prof["lon"][-1]), (round(LAT0, 5), round(straight_route()[1]["coords"][-1][1], 5)))

    def test_no_data_means_no_profile(self):
        class NoData:
            def heights(self, lats, lons, download=True):
                return np.full(len(lats), np.nan)

        self.assertIsNone(elevation.profile(straight_route(), NoData()))


if __name__ == "__main__":
    unittest.main()
