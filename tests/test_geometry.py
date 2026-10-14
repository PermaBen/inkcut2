import math
import random
import time
import unittest

from vinylcut import geometry as G


def square(x0, y0, s, ccw=True):
    pts = [(x0, y0), (x0 + s, y0), (x0 + s, y0 + s), (x0, y0 + s)]
    if not ccw:
        pts = pts[::-1]
    return pts + [pts[0]]


def circle(cx, cy, r, n=32, start=0.0):
    pts = [(cx + r * math.cos(start + 2 * math.pi * i / n), cy + r * math.sin(start + 2 * math.pi * i / n))
           for i in range(n)]
    return pts + [pts[0]]


def seg_dist(p, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return math.hypot(p[0] - ax, p[1] - ay)
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
    return math.hypot(p[0] - ax - t * dx, p[1] - ay - t * dy)


def outline_dist(p, poly):
    return min(seg_dist(p, poly[i], poly[i + 1]) for i in range(len(poly) - 1))


def key(poly):
    b = G.bbox([poly])
    return tuple(round(v, 4) for v in b)


def sticker(cx, cy):
    """5 paths: outer circle r=10, two holes, an inner detail inside hole 1, an open stroke."""
    return [
        circle(cx, cy, 10, 64),
        circle(cx - 4, cy, 3, 16),
        circle(cx - 4, cy, 1, 8),
        circle(cx + 4, cy + 3, 2, 16),
        [(cx + 2, cy - 5), (cx + 6, cy - 5), (cx + 6, cy - 3)],
    ]


class BasicsTest(unittest.TestCase):
    def test_bbox(self):
        self.assertEqual(G.bbox([]), (0, 0, 0, 0))
        self.assertEqual(G.bbox([[]]), (0, 0, 0, 0))
        self.assertEqual(G.bbox([[(1, 2)]]), (1, 2, 1, 2))
        self.assertEqual(G.bbox([[(1, 2), (3, -1)], [(-5, 0)]]), (-5, -1, 3, 2))

    def test_is_closed(self):
        self.assertTrue(G.is_closed(square(0, 0, 1)))
        self.assertTrue(G.is_closed([(0, 0), (1, 0), (1, 1), (0, 5e-7)]))
        self.assertFalse(G.is_closed([(0, 0), (1, 0), (1, 1)]))
        self.assertFalse(G.is_closed([(0, 0), (0, 0)]))
        self.assertFalse(G.is_closed([(0, 0)]))
        self.assertFalse(G.is_closed([]))

    def test_translate_scale_no_mutation(self):
        src = [[[0, 0], [1, 2]]]
        out = G.translate(src, 1, -1)
        self.assertEqual(out, [[(1, -1), (2, 1)]])
        self.assertEqual(src, [[[0, 0], [1, 2]]])
        self.assertIsInstance(out[0][0], tuple)
        self.assertEqual(G.scale(src, 2, 3), [[(0, 0), (2, 6)]])
        self.assertEqual(G.scale(src, 2, 2, origin=(1, 2)), [[(-1, -2), (1, 2)]])
        self.assertEqual(G.translate([], 1, 1), [])

    def test_rotate(self):
        p = [[(1.0, 0.0)]]
        r = G.rotate(p, 90, origin=(0, 0))
        self.assertEqual(r, [[(0.0, 1.0)]])          # CCW, exact for multiples of 90
        r = G.rotate(p, -90, origin=(0, 0))
        self.assertEqual(r, [[(0.0, -1.0)]])
        r = G.rotate(p, 45, origin=(0, 0))
        self.assertAlmostEqual(r[0][0][0], math.sqrt(0.5))
        self.assertAlmostEqual(r[0][0][1], math.sqrt(0.5))
        rect = [[(0, 0), (10, 0), (10, 4), (0, 4), (0, 0)]]
        r = G.rotate(rect, 90)                        # about bbox centre (5, 2)
        self.assertEqual(G.bbox(r), (3.0, -3.0, 7.0, 7.0))
        self.assertTrue(G.is_closed(r[0]))
        self.assertEqual(G.rotate([], 30), [])
        self.assertEqual(G.rotate(rect, 360), [[(float(x), float(y)) for x, y in rect[0]]])

    def test_mirror(self):
        tri = [[(0, 0), (4, 0), (0, 2), (0, 0)]]
        h = G.mirror(tri, True, False)
        self.assertEqual(h, [[(4, 0), (0, 0), (4, 2), (4, 0)]])
        v = G.mirror(tri, False, True)
        self.assertEqual(v, [[(0, 2), (4, 2), (0, 0), (0, 2)]])
        self.assertEqual(G.bbox(G.mirror(tri, True, True)), G.bbox(tri))
        self.assertEqual(G.mirror(tri, False, False), [[(0, 0), (4, 0), (0, 2), (0, 0)]])
        self.assertEqual(G.mirror([], True, True), [])

    def test_normalize(self):
        out = G.normalize([[(5, 7), (8, 9)], [(6, 10)]])
        self.assertEqual(G.bbox(out)[:2], (0, 0))
        self.assertEqual(G.normalize([]), [])

    def test_path_length(self):
        self.assertAlmostEqual(G.path_length([square(0, 0, 10)]), 40)
        self.assertEqual(G.path_length([]), 0)
        self.assertEqual(G.path_length([[(1, 1)]]), 0)

    def test_weed_border(self):
        b = G.weed_border((0, 0, 10, 5), 3)
        self.assertTrue(G.is_closed(b))
        self.assertEqual(G.bbox([b]), (-3, -3, 13, 8))
        self.assertGreater(G.polygon_area(b), 0)

    def test_test_cut_shape(self):
        shapes = G.test_cut_shape(20)
        self.assertEqual(len(shapes), 2)
        self.assertTrue(all(G.is_closed(s) for s in shapes))
        tri, sq = shapes
        self.assertEqual(G.bbox([sq]), (0, 0, 20, 20))
        self.assertEqual(len(tri), 4)
        for p in tri[:-1]:
            self.assertTrue(G.point_in_polygon(p, sq))

    def test_travel_moves(self):
        polys = [[(1, 0), (2, 0)], [(2, 0), (2, 5)], [(10, 10), (11, 10)]]
        m = G.travel_moves(polys, (0, 0))
        self.assertEqual(m, [((0, 0), (1, 0)), ((2, 5), (10, 10))])
        m = G.travel_moves(polys, (0, 0), return_to_start=True)
        self.assertEqual(m[-1], ((11, 10), (0, 0)))
        self.assertEqual(G.travel_moves([], (0, 0)), [])
        self.assertEqual(G.travel_moves([], (0, 0), True), [])

    def test_area_pip(self):
        self.assertAlmostEqual(G.polygon_area(square(0, 0, 2)), 4)
        self.assertAlmostEqual(G.polygon_area(square(0, 0, 2, ccw=False)), -4)  # closing dup harmless
        self.assertAlmostEqual(G.polygon_area(square(0, 0, 2)[:-1]), 4)
        self.assertTrue(G.point_in_polygon((1, 1), square(0, 0, 2)))
        self.assertFalse(G.point_in_polygon((3, 1), square(0, 0, 2)))
        self.assertFalse(G.point_in_polygon((0, 0), [(0, 0), (1, 1)]))


class GridTest(unittest.TestCase):
    def test_grid_array(self):
        sq = [square(0, 0, 10)]
        copies = G.grid_array(sq, 2, 3, 2, 1)
        self.assertEqual(len(copies), 6)
        bbs = sorted(G.bbox(c) for c in copies)
        expected = sorted((c * 12.0, r * 11.0, c * 12.0 + 10, r * 11.0 + 10) for r in range(2) for c in range(3))
        self.assertEqual(bbs, expected)
        self.assertEqual(G.grid_array(sq, 0, 3, 0, 0), [])
        self.assertEqual(G.grid_array([], 2, 2, 0, 0), [[], [], [], []])
        copies[0][0][0] = None              # copies are independent of the input
        self.assertEqual(sq[0][0], (0, 0))

    def test_max_grid(self):
        # 3 x 10 + 2 x 2 = 34 <= 35 ; rows along h: 2*10 + 1*2 = 22 <= 25
        self.assertEqual(G.max_grid(10, 10, 35, 25, 2, 2), (2, 3))
        self.assertEqual(G.max_grid(10, 10, 34, 22, 2, 2), (2, 3))       # exact fit
        self.assertEqual(G.max_grid(10, 10, 9, 100, 2, 2), (8, 0))      # 8*10 + 7*2 = 94
        self.assertEqual(G.max_grid(10, 10, -5, -5, 2, 2), (0, 0))
        self.assertEqual(G.max_grid(0, 0, 10, 10, 0, 0), (1, 1))         # degenerate, finite
        self.assertEqual(G.max_grid(0, 5, 10, 5, 2, 0), (1, 6))
        rows, cols = G.max_grid(25.4, 25.4, 4990, 600, 2, 2)
        self.assertLessEqual(cols * 25.4 + (cols - 1) * 2, 4990)
        self.assertGreater((cols + 1) * 25.4 + cols * 2, 4990)
        self.assertEqual(rows, 21)


class OrderTest(unittest.TestCase):
    def test_empty_and_as_is(self):
        for mode in G.ORDER_MODES:
            self.assertEqual(G.order_paths([], mode), [])
            self.assertEqual(G.order_paths([[]], mode), [])
        polys = [[(5, 5), (6, 6)], [(0, 0), (1, 1)]]
        self.assertEqual(G.order_paths(polys, "as_is"), [[(5, 5), (6, 6)], [(0, 0), (1, 1)]])
        with self.assertRaises(ValueError):
            G.order_paths(polys, "bogus")

    def test_single_point(self):
        for mode in G.ORDER_MODES:
            self.assertEqual(G.order_paths([[(3, 4)]], mode), [[(3, 4)]])

    def test_shortest_reverses_open(self):
        polys = [[(10, 0), (20, 0)], [(2, 0), (1, 0)]]
        out = G.order_paths(polys, "shortest", (0, 0))
        self.assertEqual(out, [[(1, 0), (2, 0)], [(10, 0), (20, 0)]])
        self.assertEqual(polys[1], [(2, 0), (1, 0)])        # input untouched

    def test_shortest_rotates_closed(self):
        sq = square(10, 10, 5)
        out = G.order_paths([sq], "shortest", (100, 100))
        self.assertEqual(out[0][0], (15, 15))
        self.assertTrue(G.is_closed(out[0]))
        self.assertEqual(len(out[0]), len(sq))
        self.assertAlmostEqual(G.path_length(out), G.path_length([sq]))

    def test_shortest_beats_random_order(self):
        rnd = random.Random(1)
        polys = []
        for _ in range(300):
            x, y = rnd.uniform(0, 500), rnd.uniform(0, 500)
            polys.append([(x, y), (x + 1, y + 1)])
        tl = lambda ps: sum(math.dist(a, b) for a, b in G.travel_moves(ps, (0, 0)))
        out = G.order_paths(polys, "shortest")
        self.assertEqual(len(out), 300)
        self.assertLess(tl(out), tl(polys) / 4)
        self.assertEqual(sorted(key(p) for p in out), sorted(key(p) for p in polys))

    def test_grid_matches_bruteforce(self):
        # the bucket-grid nearest neighbour must agree with brute force (open 2-pt paths with
        # distinct distances, so ties do not matter)
        rnd = random.Random(7)
        polys = []
        for _ in range(200):
            x, y = rnd.uniform(0, 300), rnd.uniform(0, 100)
            polys.append([(x, y), (x + rnd.uniform(-3, 3), y + rnd.uniform(-3, 3))])
        out = G.order_paths(polys, "shortest", (0, 0))
        # brute force reference
        remaining = [list(p) for p in polys]
        pos = (0.0, 0.0)
        ref = []
        while remaining:
            best = min(range(len(remaining)),
                       key=lambda i: min(math.dist(pos, remaining[i][0]), math.dist(pos, remaining[i][-1])))
            p = remaining.pop(best)
            if math.dist(pos, p[-1]) < math.dist(pos, p[0]):
                p = p[::-1]
            ref.append(p)
            pos = p[-1]
        self.assertEqual(out, ref)

    def test_inside_out_simple(self):
        outer = square(0, 0, 100)
        inner = square(40, 40, 10)
        out = G.order_paths([outer, inner], "inside_out")
        self.assertEqual([key(p) for p in out], [key(inner), key(outer)])

    def test_inside_out_nested_and_open(self):
        outer = square(0, 0, 100)
        hole = square(20, 20, 40)
        island = square(30, 30, 10)
        stroke = [(70, 70), (80, 80)]                 # open path inside outer only
        separate = square(200, 0, 10)
        polys = [outer, separate, hole, stroke, island]
        out = G.order_paths(polys, "inside_out")
        pos = {key(p): i for i, p in enumerate(out)}
        self.assertLess(pos[key(island)], pos[key(hole)])
        self.assertLess(pos[key(hole)], pos[key(outer)])
        self.assertLess(pos[key(stroke)], pos[key(outer)])
        self.assertEqual(len(out), 5)

    def test_inside_out_test_cut(self):
        tri, sq = G.test_cut_shape(20)
        out = G.order_paths([sq, tri], "inside_out")
        self.assertEqual(key(out[0]), key(tri))

    def test_inside_out_duplicates_and_degenerate(self):
        a = square(0, 0, 10)
        b = square(0, 0, 10)
        flat = [(0, 0), (5, 0), (10, 0), (0, 0)]       # closed, zero area
        out = G.order_paths([a, b, flat], "inside_out")
        self.assertEqual(len(out), 3)

    def test_by_x(self):
        polys = [square(50, 0, 5), square(0, 0, 5), square(20, 0, 5)]
        out = G.order_paths(polys, "by_x")
        self.assertEqual([G.bbox([p])[0] for p in out], [0, 20, 50])
        outer = square(0, 0, 50)
        inner = square(40, 10, 10)                     # touches outer's max x
        out = G.order_paths([outer, inner], "by_x")
        self.assertEqual(key(out[0]), key(inner))
        # bands along the feed are visited in increasing x
        rng = random.Random(5)
        polys = [square(rng.uniform(0, 1000), rng.uniform(0, 600), 3) for _ in range(300)]
        out = G.order_paths(polys, "by_x")
        bands = [math.floor(G.bbox([p])[2] / G.BY_X_BAND_MM) for p in out]
        self.assertEqual(bands, sorted(bands))

    def test_inside_out_touching(self):
        outer = square(0, 0, 50)
        inner = square(40, 10, 10)                     # shares part of outer's right edge
        corner = square(0, 0, 5)                       # shares a corner and two edges
        out = G.order_paths([outer, inner, corner], "inside_out")
        self.assertEqual(key(out[-1]), key(outer))

    def test_open_vs_closed_preserved(self):
        polys = [square(0, 0, 3), [(5, 5), (6, 5), (7, 6)]]
        for mode in G.ORDER_MODES:
            out = G.order_paths(polys, mode)
            self.assertEqual(sum(G.is_closed(p) for p in out), 1, mode)

    def _array(self, n_copies):
        cols = 25
        rows = int(math.ceil(n_copies / cols))
        base = sticker(10, 10)
        copies = G.grid_array(base, rows, cols, 2, 2)[:n_copies]
        return copies, [p for c in copies for p in c]

    def test_inside_out_array_correct(self):
        copies, polys = self._array(60)
        out = G.order_paths(polys, "inside_out")
        pos = {key(p): i for i, p in enumerate(out)}
        self.assertEqual(len(pos), len(polys))
        for c in copies:
            outer, hole1, detail, hole2, stroke = (pos[key(p)] for p in c)
            self.assertLess(detail, hole1)
            self.assertLess(hole1, outer)
            self.assertLess(hole2, outer)
            self.assertLess(stroke, outer)
            # each sticker is finished before the next is started
            self.assertEqual(max(outer, hole1, detail, hole2, stroke) - min(outer, hole1, detail, hole2, stroke), 4)

    def test_performance(self):
        # 600 copies x 5 paths = 3000 paths, and 1000 copies = 5000 paths
        for n_copies, limit in ((600, 2.0), (1000, 2.0)):
            _, polys = self._array(n_copies)
            for mode in ("shortest", "inside_out", "by_x"):
                t0 = time.perf_counter()
                out = G.order_paths(polys, mode)
                dt = time.perf_counter() - t0
                self.assertEqual(len(out), len(polys))
                print("\n  order_paths(%s) %d paths: %.3f s" % (mode, len(polys), dt), end="")
                self.assertLess(dt, limit, "%s on %d paths took %.2fs" % (mode, len(polys), dt))

    def test_performance_scattered(self):
        rnd = random.Random(3)
        polys = []
        for _ in range(5000):
            x, y = rnd.uniform(0, 3000), rnd.uniform(0, 600)
            polys.append(circle(x, y, rnd.uniform(0.5, 4), 24))
        t0 = time.perf_counter()
        out = G.order_paths(polys, "shortest")
        dt = time.perf_counter() - t0
        print("\n  order_paths(shortest) 5000 scattered circles: %.3f s" % dt, end="")
        self.assertEqual(len(out), 5000)
        self.assertLess(dt, 2.0)


class OvercutTest(unittest.TestCase):
    def test_square(self):
        sq = square(0, 0, 10)
        out = G.apply_overcut([sq], 2.5)
        self.assertEqual(out[0][:5], sq)
        self.assertEqual(out[0][5:], [(2.5, 0.0)])
        out = G.apply_overcut([sq], 12)
        self.assertEqual(out[0][5:], [(10, 0), (10, 2)])
        self.assertAlmostEqual(G.path_length(out), 52)

    def test_open_and_zero(self):
        line = [(0, 0), (5, 0)]
        self.assertEqual(G.apply_overcut([line], 2), [line])
        self.assertEqual(G.apply_overcut([square(0, 0, 1)], 0), [square(0, 0, 1)])
        self.assertEqual(G.apply_overcut([], 1), [])

    def test_cap_and_degenerate(self):
        sq = square(0, 0, 1)
        out = G.apply_overcut([sq], 100)
        self.assertAlmostEqual(G.path_length(out), 8)        # at most one extra lap
        dot = [(1, 1), (1, 1), (1, 1)]
        self.assertEqual(G.apply_overcut([dot], 1), [dot])

    def test_duplicate_vertices(self):
        sq = [(0, 0), (10, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
        out = G.apply_overcut([sq], 11)
        self.assertEqual(out[0][-1], (10, 1))


class BladeOffsetTest(unittest.TestCase):
    def test_identity(self):
        sq = square(0, 0, 10)
        self.assertEqual(G.apply_blade_offset([sq], 0), [sq])
        self.assertEqual(G.apply_blade_offset([], 0.25), [])
        self.assertEqual(G.apply_blade_offset([[(1, 1)]], 0.25), [[(1, 1)]])

    def test_square_arcs(self):
        d = 0.25
        sq = square(0, 0, 10)
        out = G.apply_blade_offset([sq], d)[0]
        self.assertTrue(G.is_closed(out))
        corners = [(0, 0), (10, 0), (10, 10), (0, 10)]
        for c in corners:
            on_arc = [p for p in out if abs(math.dist(p, c) - d) < 1e-9]
            self.assertGreaterEqual(len(on_arc), 4, c)     # in, >=2 interior arc points, out
        for p in out:
            self.assertLessEqual(outline_dist(p, sq), d + 1e-9)
        # extension points: segment (0,0)->(10,0) extended to (10.25, 0)
        self.assertTrue(any(math.dist(p, (10 + d, 0)) < 1e-9 for p in out))
        self.assertTrue(any(math.dist(p, (10, d)) < 1e-9 for p in out))
        # the arc at (10,0) turns CCW (left turn) through (10+d*cos45, d*sin45)
        q = (10 + d * math.cos(math.pi / 4), d * math.sin(math.pi / 4))
        arc_pts = [p for p in out if abs(math.dist(p, (10, 0)) - d) < 1e-9]
        self.assertTrue(all(p[0] >= 10 - 1e-9 and p[1] >= -1e-9 for p in arc_pts))
        self.assertTrue(any(math.dist(p, q) < 0.05 for p in arc_pts))
        self.assertGreater(G.path_length([out]), 40)

    def test_cw_square(self):
        d = 0.5
        sq = square(0, 0, 10, ccw=False)
        out = G.apply_blade_offset([sq], d)[0]
        self.assertTrue(G.is_closed(out))
        for p in out:
            self.assertLessEqual(outline_dist(p, sq), d + 1e-9)

    def test_small_angles_no_arc(self):
        d = 0.25
        c = circle(0, 0, 20, 72)        # 5 deg per vertex
        out = G.apply_blade_offset([c], d)[0]
        self.assertTrue(G.is_closed(out))
        # 2 points per vertex (chord), no arc subdivision
        self.assertLessEqual(len(out), 2 * 72 + 1)
        for p in out:
            self.assertLessEqual(outline_dist(p, c), d + 1e-9)

    def test_open_path(self):
        d = 0.3
        line = [(0, 0), (10, 0), (10, 10)]
        out = G.apply_blade_offset([line], d)[0]
        self.assertEqual(out[0], (0.0, 0.0))
        self.assertAlmostEqual(out[1][0], 10 + d)
        self.assertAlmostEqual(out[-1][1], 10 + d)
        self.assertFalse(G.is_closed(out))
        for p in out:
            self.assertLessEqual(outline_dist(p, line), d + 1e-9)
        straight = [(0, 0), (5, 0)]
        self.assertEqual(G.apply_blade_offset([straight], d), [[(0, 0), (5 + d, 0)]])

    def test_reversal_and_degenerate(self):
        d = 0.25
        back = [(0, 0), (5, 0), (0, 0)]                 # closed per contract, zero area
        out = G.apply_blade_offset([back], d)[0]
        self.assertTrue(G.is_closed(out))
        for p in out:
            self.assertLessEqual(outline_dist(p, back), d + 1e-9)
        dup = [(0, 0), (0, 0), (0, 0)]
        self.assertEqual(G.apply_blade_offset([dup], d), [dup])
        self.assertEqual(G.apply_blade_offset([[(1, 1), (1, 1)]], d), [[(1, 1), (1, 1)]])

    def test_threshold(self):
        d = 0.25
        sq = square(0, 0, 10)
        no_arcs = G.apply_blade_offset([sq], d, angle_threshold_deg=95)[0]
        self.assertEqual(len(no_arcs), 9)               # start + 4 x (extension, chord end)
        with_arcs = G.apply_blade_offset([sq], d, angle_threshold_deg=30)[0]
        self.assertGreater(len(with_arcs), len(no_arcs))

    def test_no_mutation(self):
        sq = square(0, 0, 10)
        before = list(sq)
        G.apply_blade_offset([sq], 0.25)
        G.apply_overcut([sq], 1)
        G.order_paths([sq], "shortest", (10, 10))
        self.assertEqual(sq, before)


if __name__ == "__main__":
    unittest.main()
