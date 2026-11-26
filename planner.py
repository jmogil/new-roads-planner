"""Route search: maximise untraveled distance within a distance budget.

This is a prize-collecting / orienteering problem. The approach:

1. Every road gets a routing cost = length x road-type penalty, discounted for
   untraveled roads so paths between waypoints naturally soak up new streets.
2. Untraveled road length is binned into a grid; the richest cells within reach
   become candidate waypoints.
3. Shortest paths from every candidate are computed in one go (scipy, C speed).
4. A randomised greedy insertion (GRASP) builds loops start -> wp -> ... -> start,
   always adding the waypoint with the best (new metres gained / extra metres
   ridden), counting each road only once. Many restarts; best loop wins.
5. Small new loops/cul-de-sac circuits hanging off the route are spliced in if
   the budget allows.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

import progress
from roads import Projector, RoadGraph


@dataclass
class PlanOptions:
    distance_m: float
    tolerance: float = 0.05        # route may be up to +5% over target
    eagerness: float = 0.6         # 0 = ignore newness when routing legs, 0.9 = strongly prefer new roads
    avoid_main_roads: float = 1.0  # scales the highway penalties (0 = ignore road type)
    n_candidates: int = 180
    restarts: int = 8              # independent searches
    iterations: int = 40           # ruin & recreate steps per search
    seed: int | None = None


@dataclass
class Plan:
    nodes: list[int]                 # node sequence
    edges: list[int]                 # undirected edge id per step
    length_m: float
    new_m: float
    waypoints: list[int] = field(default_factory=list)


class Planner:
    def __init__(self, rg: RoadGraph, traveled_frac: np.ndarray, opts: PlanOptions):
        self.rg, self.opts = rg, opts
        self.rng = np.random.default_rng(opts.seed)
        new_frac = (1.0 - traveled_frac) * rg.edge_target
        self.new_len = rg.edge_len * new_frac
        pen = 1.0 + (rg.edge_pen - 1.0) * opts.avoid_main_roads
        self.edge_cost = rg.edge_len * pen * (1.0 - opts.eagerness * new_frac)
        self.edge_cost = np.maximum(self.edge_cost, 0.5)
        a_cost = self.edge_cost[rg.arc_edge]
        self.arc_lookup = {}
        for f, t, e in zip(rg.arc_from.tolist(), rg.arc_to.tolist(), rg.arc_edge.tolist()):
            self.arc_lookup[(f, t)] = e
        self.csr = csr_matrix((a_cost, (rg.arc_from, rg.arc_to)), shape=(rg.n_nodes, rg.n_nodes))
        self.proj = Projector(*rg.center)
        self.node_xy = self.proj.xy(rg.node_lat, rg.node_lon)
        # one-way routes: length of the most direct start -> finish leg found by the last plan()
        # (None if the finish could not be reached), so callers can explain an empty result
        self.direct_m: float | None = None

    # -- helpers ---------------------------------------------------------------

    def nearest_node(self, lat: float, lon: float) -> int:
        p = self.proj.xy(lat, lon)
        # only nodes that can both be reached and left
        deg = np.bincount(self.rg.arc_from, minlength=self.rg.n_nodes)
        d = np.hypot(*(self.node_xy - p).T)
        d[deg == 0] = np.inf
        return int(np.argmin(d))

    def _candidates(self, start: int, end: int) -> list[int]:
        rg, o = self.rg, self.opts
        D = o.distance_m
        cell = float(np.clip(D / 30.0, 250.0, 900.0))
        reach = D * (1 + o.tolerance) / 2.0  # a loop can't get further than half its length
        s_xy, e_xy = self.node_xy[start], self.node_xy[end]

        has_new = self.new_len > 1.0
        eu, ev = rg.edge_u[has_new], rg.edge_v[has_new]
        mid = (self.node_xy[eu] + self.node_xy[ev]) / 2.0
        # ellipse reach test (start/end foci) – a loop is the special case start == end
        ok = (np.hypot(*(mid - s_xy).T) + np.hypot(*(mid - e_xy).T)) <= 2 * reach * 0.9
        eu, ev, mid, gain = eu[ok], ev[ok], mid[ok], self.new_len[has_new][ok]
        if len(gain) == 0:
            return []
        cells = np.floor(mid / cell).astype(np.int64)
        key = cells[:, 0] * 1_000_003 + cells[:, 1]
        uniq, inv = np.unique(key, return_inverse=True)
        cell_gain = np.bincount(inv, weights=gain)
        order = np.argsort(-cell_gain)[: o.n_candidates]
        picks = []
        node_gain = {}
        for e_u, e_v, g, c in zip(eu, ev, gain, inv):
            node_gain[(c, e_u)] = node_gain.get((c, e_u), 0.0) + g
            node_gain[(c, e_v)] = node_gain.get((c, e_v), 0.0) + g
        best_in_cell = {}
        for (c, n), g in node_gain.items():
            if g > best_in_cell.get(c, (-1, 0))[0]:
                best_in_cell[c] = (g, n)
        for c in order:
            if cell_gain[c] < 150:  # not worth a detour
                break
            picks.append(int(best_in_cell[c][1]))
        return list(dict.fromkeys(picks))

    # -- main search -----------------------------------------------------------

    def plan(self, start: int, end: int | None = None, alternatives: int = 1) -> list[Plan]:
        """Best route first, then up to alternatives-1 routes sharing <50% of its roads."""
        o = self.opts
        end = start if end is None else end
        cands = self._candidates(start, end)
        pts = list(dict.fromkeys([start, end] + cands))
        print(f"  {len(cands)} candidate waypoint areas; computing shortest paths...")
        limit = o.distance_m * 1.2 * max(1.0, float(self.edge_cost.sum() / self.rg.edge_len.sum()))
        dist, pred = dijkstra(self.csr, directed=True, indices=pts,
                              return_predecessors=True, limit=limit)
        self._pred = pred.astype(np.int32)
        self._pts = pts
        self._row = {n: i for i, n in enumerate(pts)}
        self._legs: dict[tuple[int, int], tuple[np.ndarray, list[int], float, float] | None] = {}
        reachable = np.isfinite(dist[:, pts])
        # keep candidates reachable both ways from start/end
        ok_idx = [i for i in range(len(pts))
                  if reachable[self._row[start], i] and reachable[i, self._row[end]]]
        pool = [pts[i] for i in ok_idx if pts[i] not in (start, end)]

        budget = o.distance_m * (1 + o.tolerance)
        self.direct_m = None
        if end != start:
            # a one-way route has to cover the start -> finish leg itself
            direct = self._leg(start, end)
            if direct is None:
                return []
            self.direct_m = direct[2]
            if direct[2] > budget:
                return []
        found: list[Plan] = []
        n_restarts = o.restarts * (1 if alternatives == 1 else 2)
        for r in range(n_restarts):
            progress.report("search", r, n_restarts)
            progress.check()
            greedy = 0 if r == 0 else 3
            if alternatives > 1 and r >= n_restarts // 2:
                # diversify: hide a random 40% of target areas
                sub = [c for c in pool if self.rng.random() > 0.4]
            else:
                sub = pool
            tour = self._construct(start, end, sub, budget, greedy)
            tour = self._improve(tour, start, end, sub, budget, o.iterations)
            p = self._splice_loops(self._materialise(tour), budget)
            if p.edges:
                found.append(p)
        found.sort(key=lambda p: (-round(p.new_m), p.length_m))
        chosen: list[Plan] = []
        for p in found:
            s = set(p.edges)
            if all(len(s & set(q.edges)) / max(1, min(len(s), len(set(q.edges)))) < 0.5 for q in chosen):
                chosen.append(p)
            if len(chosen) >= alternatives:
                break
        return chosen

    # leg = (edge ids, node list, length, standalone new metres)
    def _leg(self, a: int, b: int):
        key = (a, b)
        if key in self._legs:
            return self._legs[key]
        if a == b:
            leg = (np.zeros(0, dtype=np.int32), [a], 0.0, 0.0)
        else:
            row = self._pred[self._row[a]]
            nodes = [b]
            cur = b
            while cur != a:
                cur = int(row[cur])
                if cur < 0:
                    self._legs[key] = None
                    return None
                nodes.append(cur)
            nodes.reverse()
            edges = np.fromiter((self.arc_lookup[(nodes[i], nodes[i + 1])] for i in range(len(nodes) - 1)),
                                dtype=np.int32, count=len(nodes) - 1)
            length = float(self.rg.edge_len[edges].sum())
            gain = float(self.new_len[np.unique(edges)].sum())
            leg = (edges, nodes, length, gain)
        self._legs[key] = leg
        return leg

    def _tour_value(self, tour) -> tuple[float, float]:
        edges = [self._leg(a, b)[0] for a, b in zip(tour[:-1], tour[1:])]
        e = np.unique(np.concatenate(edges)) if edges else np.zeros(0, dtype=np.int32)
        length = sum(self._leg(a, b)[2] for a, b in zip(tour[:-1], tour[1:]))
        return float(self.new_len[e].sum()), length

    def _improve(self, tour, start, end, pool, budget, iters):
        """Ruin & recreate: drop some waypoints (random or a contiguous run), refill greedily."""
        cur, (cur_v, cur_l) = tour, self._tour_value(tour)
        best, best_v, best_l = cur, cur_v, cur_l
        for _ in range(iters):
            progress.check()
            inner = cur[1:-1]
            if not inner:
                break
            k = max(1, int(round(len(inner) * self.rng.uniform(0.2, 0.5))))
            if self.rng.random() < 0.5:
                drop = set(self.rng.choice(len(inner), size=k, replace=False).tolist())
            else:  # a contiguous stretch = one region of the loop gets re-planned
                s0 = int(self.rng.integers(len(inner)))
                drop = {(s0 + i) % len(inner) for i in range(k)}
            partial = [cur[0]] + [w for i, w in enumerate(inner) if i not in drop] + [cur[-1]]
            cand = self._construct(start, end, pool, budget, greedy_k=3, tour=partial)
            v, length = self._tour_value(cand)
            if length > budget:
                continue
            # accept sideways moves too, so the search can drift across plateaus
            if v > cur_v - 1e-6 or (v > cur_v - 1.0 and length < cur_l):
                cur, cur_v, cur_l = cand, v, length
                if v > best_v + 1e-6 or (abs(v - best_v) <= 1e-6 and length < best_l):
                    best, best_v, best_l = cand, v, length
        return best

    def _construct(self, start, end, pool, budget, greedy_k, tour=None):
        cnt = np.zeros(len(self.new_len), dtype=np.int32)
        tour = [start, end] if tour is None else list(tour)
        total = 0.0
        # count the legs the tour already has: for a one-way route that includes the direct
        # start -> finish leg, which would otherwise ride along unbudgeted
        for a, b in zip(tour[:-1], tour[1:]):
            leg = self._leg(a, b)
            np.add.at(cnt, leg[0], 1)
            total += leg[2]
        in_tour = set(tour[1:-1])
        while True:
            options = []
            for pos in range(len(tour) - 1):
                a, b = tour[pos], tour[pos + 1]
                ab = self._leg(a, b)
                for c in pool:
                    if c in in_tour:
                        continue
                    ac, cb = self._leg(a, c), self._leg(c, b)
                    if ac is None or cb is None:
                        continue
                    dl = ac[2] + cb[2] - ab[2]
                    if total + dl > budget:
                        continue
                    approx = ac[3] + cb[3] - ab[3]
                    if approx <= 0:
                        continue
                    options.append((approx / max(dl, 50.0), pos, c, dl))
            if not options:
                break
            options.sort(reverse=True)
            # exact gain (roads counted once) for the most promising few
            scored = []
            for _, pos, c, dl in options[:20]:
                a, b = tour[pos], tour[pos + 1]
                ab, ac, cb = self._leg(a, b), self._leg(a, c), self._leg(c, b)
                # removing leg a->b loses roads that only it covered
                lost = self.new_len[ab[0][cnt[ab[0]] == 1]].sum()
                added_edges = np.unique(np.concatenate([ac[0], cb[0]]))
                after = cnt[added_edges] - np.isin(added_edges, ab[0])
                added = self.new_len[added_edges[after == 0]].sum()
                dg = added - lost
                if dg > 0:
                    scored.append((dg / max(dl, 50.0), pos, c, ab, ac, cb, dl))
            if not scored:
                break
            scored.sort(key=lambda s: -s[0])
            k = 1 if greedy_k == 0 else min(greedy_k, len(scored))
            _, pos, c, ab, ac, cb, dl = scored[int(self.rng.integers(k))]
            np.subtract.at(cnt, ab[0], 1)
            np.add.at(cnt, ac[0], 1)
            np.add.at(cnt, cb[0], 1)
            tour.insert(pos + 1, c)
            in_tour.add(c)
            total += dl
        return tour

    def _materialise(self, tour) -> Plan:
        nodes, edges = [tour[0]], []
        for a, b in zip(tour[:-1], tour[1:]):
            leg = self._leg(a, b)
            nodes.extend(leg[1][1:])
            edges.extend(leg[0].tolist())
        e = np.asarray(edges, dtype=np.int64)
        return Plan(nodes=nodes, edges=edges,
                    length_m=float(self.rg.edge_len[e].sum()) if len(e) else 0.0,
                    new_m=float(self.new_len[np.unique(e)].sum()) if len(e) else 0.0,
                    waypoints=list(tour[1:-1]))

    def _splice_loops(self, p: Plan, budget: float) -> Plan:
        """Add untraveled self-loop roads (e.g. cul-de-sac circuits) at nodes on the route."""
        rg = self.rg
        loops = np.where((rg.edge_u == rg.edge_v) & (self.new_len > 20))[0]
        if not len(loops):
            return p
        by_node = {}
        for e in loops:
            by_node.setdefault(int(rg.edge_u[e]), []).append(int(e))
        used = set(p.edges)
        nodes, edges = [p.nodes[0]], []
        length, new = p.length_m, p.new_m
        for i, e in enumerate(p.edges):
            n = p.nodes[i]
            for le in by_node.get(n, []):
                if le not in used and length + rg.edge_len[le] <= budget:
                    nodes.append(n)
                    edges.append(le)
                    used.add(le)
                    length += rg.edge_len[le]
                    new += self.new_len[le]
            nodes.append(p.nodes[i + 1])
            edges.append(e)
        return Plan(nodes=nodes, edges=edges, length_m=length, new_m=new, waypoints=p.waypoints)


# -- turning a loop around ------------------------------------------------------------


class OneWay(Exception):
    """A road on the route can't be travelled the other way, and there's no short way round it."""


def _arc_map(rg: RoadGraph) -> dict:
    """(from node, to node) -> the road used for that step, for every step allowed. Cached on the graph."""
    arcs = getattr(rg, "_arc_map", None)
    if arcs is None:
        arcs = rg._arc_map = {(f, t): e for f, t, e in zip(rg.arc_from.tolist(), rg.arc_to.tolist(),
                                                            rg.arc_edge.tolist())}
    return arcs


def _length_graph(rg: RoadGraph):
    csr = getattr(rg, "_length_csr", None)
    if csr is None:
        csr = rg._length_csr = csr_matrix((rg.edge_len[rg.arc_edge], (rg.arc_from, rg.arc_to)),
                                          shape=(rg.n_nodes, rg.n_nodes))
    return csr


def reverse_loop(rg: RoadGraph, plan: Plan, new_len: np.ndarray, max_extra_m: float = 1500.0) -> tuple[Plan, float]:
    """The same loop the other way round.

    Where a stretch of it is one-way (on the bike network, say a divided road whose carriageways
    are mapped separately), take the shortest legal way round instead, as long as that adds no more
    than max_extra_m. Returns (the reversed plan, metres the detours added). Raises OneWay, naming
    the street, when a stretch has no such way round.
    """
    arcs = _arc_map(rg)
    nodes, edges = plan.nodes[::-1], plan.edges[::-1]
    out_nodes, out_edges = [nodes[0]], []
    k = 0
    while k < len(edges):
        a, b, e = nodes[k], nodes[k + 1], edges[k]
        if a == b:  # a cul-de-sac circuit: it comes back to where it started either way
            out_nodes.append(b)
            out_edges.append(e)
            k += 1
            continue
        if (a, b) in arcs:
            out_nodes.append(b)
            out_edges.append(arcs[(a, b)])
            k += 1
            continue
        m = k  # the one-way stretch: every step from k up to m can't be taken backwards
        while m + 1 < len(edges) and nodes[m + 1] != nodes[m + 2] and (nodes[m + 1], nodes[m + 2]) not in arcs:
            m += 1
        target = nodes[m + 1]
        if target == a:  # a one-way stretch that comes back to where it started (say, the whole loop)
            raise OneWay(rg.edge_name[e] or "one of its roads")
        skipped = float(rg.edge_len[edges[k:m + 1]].sum())
        dist, pred = dijkstra(_length_graph(rg), directed=True, indices=a, return_predecessors=True,
                              limit=skipped + max_extra_m)
        if not np.isfinite(dist[target]):
            raise OneWay(rg.edge_name[e] or "one of its roads")
        path = [target]
        while path[-1] != a:
            path.append(int(pred[path[-1]]))
        path.reverse()
        for f, t in zip(path[:-1], path[1:]):
            out_nodes.append(t)
            out_edges.append(arcs[(f, t)])
        k = m + 1
    used = np.asarray(out_edges, dtype=np.int64)
    length = float(rg.edge_len[used].sum())
    reversed_plan = Plan(nodes=out_nodes, edges=[int(x) for x in out_edges], length_m=length,
                         new_m=float(new_len[np.unique(used)].sum()), waypoints=plan.waypoints[::-1])
    return reversed_plan, max(0.0, length - plan.length_m)
