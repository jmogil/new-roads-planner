"""Route search on a synthetic street grid: python -m unittest discover tests"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from planner import PlanOptions, Planner  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
