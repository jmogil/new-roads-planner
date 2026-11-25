"""Shared-cache safety and download helpers in roads.py: python -m unittest discover tests"""
import os
import pickle
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import progress  # noqa: E402
import roads  # noqa: E402


class AreaLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lock = os.path.join(self.tmp.name, "locks", "ride_test.lock")
        self._poll = roads.LOCK_POLL_S
        roads.LOCK_POLL_S = 0.02

    def tearDown(self):
        roads.LOCK_POLL_S = self._poll
        self.tmp.cleanup()

    def hold_lock(self, seconds, started):
        with roads.area_lock(self.lock, "test data"):
            started.set()
            time.sleep(seconds)

    def test_second_holder_waits_for_the_first(self):
        events = []
        started = threading.Event()
        holder = threading.Thread(target=lambda: (self.hold_lock(0.3, started), events.append("first done")))
        holder.start()
        started.wait(2)
        with roads.area_lock(self.lock, "test data"):
            events.append("second in")
        holder.join()
        self.assertEqual(events, ["first done", "second in"])

    def test_waiting_can_be_cancelled(self):
        started = threading.Event()
        holder = threading.Thread(target=self.hold_lock, args=(1.0, started))
        holder.start()
        started.wait(2)
        cancel = threading.Event()
        threading.Timer(0.1, cancel.set).start()
        t0 = time.time()
        with progress.track(cancel=cancel):
            with self.assertRaises(progress.Cancelled):
                with roads.area_lock(self.lock, "test data"):
                    pass
        self.assertLess(time.time() - t0, 0.8)
        holder.join()

    def test_load_graph_downloads_an_area_once(self):
        """Two processes (here, threads) asking for the same new area: one builds, the other reuses."""
        calls = []

        def fake_build(cache_dir, mode, lat, lon, radius_m):
            calls.append(1)
            time.sleep(0.3)  # a slow download
            path = os.path.join(cache_dir, roads._cache_name(mode, lat, lon, radius_m))
            with open(path, "wb") as fh:
                pickle.dump({"graph": "built once"}, fh)
            return {"graph": "built once"}

        real_build, roads._build_graph = roads._build_graph, fake_build
        try:
            results = []
            workers = [threading.Thread(target=lambda: results.append(
                roads.load_graph(self.tmp.name, "ride", -27.0, 153.0, 5000))) for _ in range(2)]
            for w in workers:
                w.start()
            for w in workers:
                w.join()
        finally:
            roads._build_graph = real_build
        self.assertEqual(len(calls), 1)
        self.assertEqual(results, [{"graph": "built once"}] * 2)


class DownloadHelperTests(unittest.TestCase):
    def test_tile_timing_estimates(self):
        with tempfile.TemporaryDirectory() as cache:
            self.assertEqual(roads.typical_tile_seconds(cache), roads.DEFAULT_TILE_SECONDS)
            for s in (30, 90, 40, 50):
                roads._record_tile_seconds(cache, s)
            self.assertEqual(roads.typical_tile_seconds(cache), 45.0)  # median

    def test_tiles_per_side(self):
        self.assertEqual(roads.tiles_per_side(5000), 1)
        self.assertEqual(roads.tiles_per_side(25000), 5)  # 25 tiles for a 40 km ride loop

    def test_cancelling_stops_a_running_command(self):
        cancel = threading.Event()
        threading.Timer(0.2, cancel.set).start()
        t0 = time.time()
        with progress.track(cancel=cancel):
            with self.assertRaises(progress.Cancelled):
                roads._run_cancellable(["sleep", "5"])
        self.assertLess(time.time() - t0, 2.0)

    def test_commands_run_normally_without_cancelling(self):
        self.assertEqual(roads._run_cancellable(["echo", "hello"]), (0, "hello\n", ""))


if __name__ == "__main__":
    unittest.main()
