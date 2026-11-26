"""Route search on a synthetic street grid: python -m unittest discover tests"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

import bikenet  # noqa: E402
from planner import OneWay, Plan, PlanOptions, Planner, reverse_loop  # noqa: E402
from synthetic import grid_graph, node, random_traveled  # noqa: E402

N = 25  # 25 x 25 grid, 200 m blocks = 4.8 km across


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.rg = grid_graph(N)
        self.frac = random_traveled(self.rg)

    def planner(self, km, **kw):
        opts = PlanOptions(distance_m=km * 1000, seed=7, restarts=3, iterations=10, **kw)
        return Planner(self.rg, self.frac, opts)

    def assert_valid_route(self, plan, start, end, budget_m):
        rg = self.rg
        self.assertEqual(plan.nodes[0], start)
        self.assertEqual(plan.nodes[-1], end)
        self.assertEqual(len(plan.nodes), len(plan.edges) + 1)
        for k, e in enumerate(plan.edges):  # consecutive and connected
            self.assertEqual({plan.nodes[k], plan.nodes[k + 1]}, {int(rg.edge_u[e]), int(rg.edge_v[e])})
        self.assertAlmostEqual(plan.length_m, float(rg.edge_len[plan.edges].sum()), places=3)
        self.assertLessEqual(plan.length_m, budget_m + 1e-6)
        self.assertGreater(plan.new_m, 0)

    def test_loop_returns_to_start_within_distance(self):
        s = node(N, 12, 12)
        plans = self.planner(6).plan(s)
        self.assertTrue(plans)
        for plan in plans:
            self.assert_valid_route(plan, s, s, 6000 * 1.05)

    def test_one_way_ends_at_finish_within_distance(self):
        s, e = node(N, 12, 4), node(N, 12, 20)  # 3.2 km apart
        plans = self.planner(6).plan(s, e)
        self.assertTrue(plans)
        for plan in plans:
            self.assert_valid_route(plan, s, e, 6000 * 1.05)

    def test_one_way_beyond_budget_returns_nothing(self):
        s, e = node(N, 12, 4), node(N, 12, 21)  # 3.4 km by road, budget 3.15 km
        p = self.planner(3)
        self.assertEqual(p.plan(s, e), [])
        self.assertGreater(p.direct_m, 3000 * 1.05)  # explains why

    def test_one_way_unreachable_returns_nothing(self):
        s, e = node(N, 0, 0), node(N, 24, 24)  # 9.6 km by road, 2 km asked for
        self.assertEqual(self.planner(2).plan(s, e), [])

    def test_alternatives_mostly_use_different_roads(self):
        s = node(N, 12, 12)
        plans = self.planner(6).plan(s, alternatives=3)
        self.assertGreaterEqual(len(plans), 2)
        for a in range(len(plans)):
            for b in range(a):
                ea, eb = set(plans[a].edges), set(plans[b].edges)
                self.assertLess(len(ea & eb) / min(len(ea), len(eb)), 0.5)


class ReverseLoopTests(unittest.TestCase):
    def test_two_way_loop_simply_reverses(self):
        rg = grid_graph(N)
        plan = Planner(rg, random_traveled(rg), PlanOptions(distance_m=5000, seed=3, restarts=2, iterations=5)).plan(
            node(N, 12, 12))[0]
        rev, extra = reverse_loop(rg, plan, np.zeros(len(rg.edge_len)))
        self.assertEqual(rev.nodes, plan.nodes[::-1])
        self.assertEqual(extra, 0.0)

    def test_detours_round_a_one_way_street_or_refuses(self):
        # nodes 0-1-2 joined by two-way roads, "One Way Street" running only 2 -> 0, and a two-way
        # back way 0-3-2 that's 200 m longer
        rg = grid_graph(2)  # borrow a tiny 4-junction graph and replace its roads
        rg.edge_u, rg.edge_v = np.array([0, 1, 2, 0, 3], np.int32), np.array([1, 2, 0, 3, 2], np.int32)
        rg.edge_len = np.array([1000.0, 1000.0, 1000.0, 600.0, 600.0])
        rg.edge_name = ["", "", "One Way Street", "", ""]
        arcs = [(0, 1, 0), (1, 0, 0), (1, 2, 1), (2, 1, 1), (2, 0, 2), (0, 3, 3), (3, 0, 3), (3, 2, 4), (2, 3, 4)]
        rg.arc_from, rg.arc_to, rg.arc_edge = (np.array(c, np.int32) for c in zip(*arcs))
        loop = Plan(nodes=[0, 1, 2, 0], edges=[0, 1, 2], length_m=3000.0, new_m=0.0)

        rev, extra = reverse_loop(rg, loop, np.zeros(5), max_extra_m=500)
        self.assertEqual(rev.nodes, [0, 3, 2, 1, 0])  # round the back way, then the two-way roads home
        self.assertAlmostEqual(extra, 200.0)
        with self.assertRaises(OneWay) as cm:  # not if the back way is too long a detour
            reverse_loop(rg, loop, np.zeros(5), max_extra_m=100)
        self.assertEqual(str(cm.exception), "One Way Street")

    def test_an_all_one_way_loop_cannot_reverse(self):
        rg = grid_graph(2)
        rg.edge_u, rg.edge_v = np.array([0, 1, 2], np.int32), np.array([1, 2, 0], np.int32)
        rg.edge_len, rg.edge_name = np.full(3, 1000.0), ["Ring Road", "Ring Road", "Ring Road"]
        rg.arc_from, rg.arc_to, rg.arc_edge = rg.edge_u.copy(), rg.edge_v.copy(), np.arange(3, dtype=np.int32)
        with self.assertRaises(OneWay):
            reverse_loop(rg, Plan(nodes=[0, 1, 2, 0], edges=[0, 1, 2], length_m=3000.0, new_m=0.0), np.zeros(3))

class BikeEmphasisTests(unittest.TestCase):
    def setUp(self):
        self.rg = grid_graph(N)
        self.frac = random_traveled(self.rg)
        # the east-west streets of rows 10 to 14 are on a primary cycle route
        rg = self.rg
        east_west = rg.edge_v == rg.edge_u + 1
        row = rg.edge_u // N
        self.cls = np.where(east_west & (row >= 10) & (row <= 14), bikenet.PRIMARY, bikenet.NONE).astype(np.int8)

    def route(self, emphasis, bike_class="default"):
        opts = PlanOptions(distance_m=6000, seed=7, restarts=3, iterations=10, bike_network=emphasis)
        cls = self.cls if bike_class == "default" else bike_class
        return Planner(self.rg, self.frac, opts, bike_class=cls).plan(node(N, 7, 12))[0]

    def share_on_network(self, plan):
        edges = np.asarray(plan.edges)
        return float(self.rg.edge_len[edges][self.cls[edges] > 0].sum() / plan.length_m)

    def test_emphasis_puts_more_of_the_route_on_the_network(self):
        off, some, strong = (self.route(e) for e in (0.0, 0.5, 1.0))
        self.assertLess(self.share_on_network(off), self.share_on_network(some))
        self.assertLessEqual(self.share_on_network(some), self.share_on_network(strong))
        self.assertGreater(strong.score, strong.new_m)  # new road on the network counted for more
        self.assertLessEqual(strong.length_m, 6000 * 1.05)

    def test_without_emphasis_nothing_changes(self):
        plain, with_classes = self.route(0.0, bike_class=None), self.route(0.0)
        self.assertEqual(plain.edges, with_classes.edges)
        self.assertEqual(plain.score, plain.new_m)


if __name__ == "__main__":
    unittest.main()
