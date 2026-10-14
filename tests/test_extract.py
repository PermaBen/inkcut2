"""Tests for inkcut2.extract and the inkcut2.py Inkscape effect (agent D).

Run from the project root:
    & "C:\\Program Files\\Inkscape\\bin\\python.exe" -m unittest discover -s tests -t .
"""

import json
import math
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIX = os.path.join(HERE, "fixtures")
INKEX_DIR = r"C:\Program Files\Inkscape\share\inkscape\extensions"
for _p in (ROOT, INKEX_DIR):
    if _p not in sys.path and os.path.isdir(_p):
        sys.path.append(_p)

try:
    import inkex  # noqa: E402
    from inkcut2 import extract as ex  # noqa: E402
except ImportError as _err:  # pragma: no cover
    inkex = None
    _IMPORT_ERROR = _err

TOL = 0.05  # mm, for bbox comparisons (flattening tolerance is 0.02)


def load(name):
    return inkex.load_svg(os.path.join(FIX, name)).getroot()


def bbox(polys):
    xs = [p[0] for pl in polys for p in pl]
    ys = [p[1] for pl in polys for p in pl]
    return (min(xs), min(ys), max(xs), max(ys))


def closed(pl):
    return len(pl) > 2 and pl[0] == pl[-1]


@unittest.skipIf(inkex is None, "inkex not importable (run with Inkscape's python)")
class ExtractShapes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svg = load("shapes.svg")

    def run_ids(self, *ids, **kw):
        els = [self.svg.getElementById(i) for i in ids]
        self.assertTrue(all(e is not None for e in els), ids)
        return ex.extract(self.svg, els, **kw)

    def assertBBox(self, polys, expected, tol=TOL):
        got = bbox(polys)
        for g, e in zip(got, expected):
            self.assertAlmostEqual(g, e, delta=tol, msg="bbox %r != %r" % (got, expected))

    def test_group_transform_rect_circle(self):
        r = self.run_ids("g1")
        self.assertEqual(len(r.polylines), 2)
        self.assertEqual(len(r.colors), 2)
        rect, circ = r.polylines
        self.assertBBox([rect], (10, 20, 30, 30))
        self.assertBBox([circ], (40, 20, 60, 40))
        self.assertTrue(closed(rect) and closed(circ))
        self.assertEqual(r.colors, ["#ff0000", "#0000ff"])  # stroke, then fill fallback
        for x, y in circ:  # every vertex on the circle
            self.assertAlmostEqual(math.hypot(x - 50, y - 30), 10, delta=1e-3)

    def test_select_child_uses_parent_transform(self):
        r = self.run_ids("c1")
        self.assertBBox(r.polylines, (40, 20, 60, 40))
        r = self.run_ids("inlayer")
        self.assertBBox(r.polylines, (5, 175, 15, 185))

    def test_cubic(self):
        r = self.run_ids("cubic1")
        self.assertEqual(len(r.polylines), 1)
        self.assertBBox(r.polylines, (10, 2.5, 30, 10))
        self.assertFalse(closed(r.polylines[0]))
        self.assertGreater(len(r.polylines[0]), 10)  # actually flattened

    def test_arcs(self):
        r = self.run_ids("arc1")
        self.assertBBox(r.polylines, (10, 30, 50, 50))
        self.assertFalse(closed(r.polylines[0]))
        for x, y in r.polylines[0]:
            self.assertAlmostEqual(math.hypot(x - 30, y - 50), 20, delta=1e-3)
        r = self.run_ids("arc2")
        self.assertEqual(len(r.polylines), 1)
        self.assertBBox(r.polylines, (70, 40, 90, 60))
        self.assertTrue(closed(r.polylines[0]))
        r = self.run_ids("arc3")  # radii scaled up per SVG spec
        self.assertBBox(r.polylines, (0, 80, 20, 90))

    def test_flatten_tolerance(self):
        for tol in (0.02, 0.2):
            r = self.run_ids("arc2", tolerance_mm=tol)
            pts = r.polylines[0]
            worst = 0.0
            for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                mx, my = (x0 + x1) / 2, (y0 + y1) / 2
                worst = max(worst, 10 - math.hypot(mx - 80, my - 50))
            self.assertLessEqual(worst, tol + 1e-4)
        coarse = len(self.run_ids("arc2", tolerance_mm=0.2).polylines[0])
        fine = len(self.run_ids("arc2", tolerance_mm=0.02).polylines[0])
        self.assertGreater(fine, coarse)

    def test_use_clones(self):
        r = self.run_ids("use1")
        self.assertBBox(r.polylines, (40, 60, 60, 80))
        self.assertEqual(r.colors, ["#00ff00"])
        r = self.run_ids("use2")
        self.assertEqual(len(r.polylines), 2)
        self.assertBBox(r.polylines, (110, 120, 160, 140))

    def test_subpaths_split(self):
        r = self.run_ids("multi")
        self.assertEqual(len(r.polylines), 2)
        a, b = r.polylines
        self.assertTrue(closed(a))
        self.assertEqual(len(a), 5)
        self.assertBBox([a], (100, 10, 110, 20))
        self.assertFalse(closed(b))
        self.assertBBox([b], (120, 10, 130, 10))

    def test_basic_shapes(self):
        self.assertBBox(self.run_ids("ell1").polylines, (140, 30, 160, 70))
        r = self.run_ids("line1")
        self.assertBBox(r.polylines, (0, 100, 50, 110))
        self.assertEqual(r.colors, ["#123456"])
        r = self.run_ids("pl1")
        self.assertFalse(closed(r.polylines[0]))
        self.assertBBox(r.polylines, (0, 120, 20, 130))
        r = self.run_ids("pg1")
        self.assertTrue(closed(r.polylines[0]))
        self.assertBBox(r.polylines, (30, 120, 50, 130))
        r = self.run_ids("rr1")
        self.assertTrue(closed(r.polylines[0]))
        self.assertBBox(r.polylines, (60, 100, 80, 110))

    def test_hidden_and_images(self):
        self.assertEqual(self.run_ids("hiddengroup").polylines, [])
        self.assertEqual(self.run_ids("hr2").polylines, [])
        self.assertEqual(self.run_ids("hr1").polylines, [])  # child of hidden group
        r = self.run_ids("mixed")
        self.assertEqual(len(r.polylines), 1)
        self.assertBBox(r.polylines, (5, 150, 15, 160))
        self.assertTrue(any("image" in w for w in r.warnings))

    def test_no_duplicates_when_parent_and_child_selected(self):
        r = self.run_ids("g1", "r1", "c1")
        self.assertEqual(len(r.polylines), 2)

    def test_whole_document(self):
        r = ex.extract(self.svg, ex.document_elements(self.svg))
        # nothing from <defs> by itself, nothing hidden
        self.assertBBox(r.polylines, (0, 2.5, 160, 185))
        self.assertTrue(any("image" in w for w in r.warnings))

    def test_document_not_modified(self):
        # inkex itself re-serialises transform="" when it first wraps an element (which
        # is why inkcut2.py never writes the document back); after one pass the tree
        # must be stable, i.e. extraction makes no edits of its own.
        svg = load("shapes.svg")
        ex.extract(svg, ex.document_elements(svg))
        before = inkex.etree.tostring(svg)
        ex.find_text(svg, ex.document_elements(svg))
        ex.extract(svg, ex.document_elements(svg))
        ex.extract(svg, [svg.getElementById("c1"), svg.getElementById("inlayer")])
        self.assertEqual(inkex.etree.tostring(svg), before)

    def test_design_json(self):
        r = self.run_ids("g1", "cubic1")
        d = r.to_design("x.svg")
        self.assertEqual(d["version"], 1)
        self.assertEqual(d["units"], "mm")
        self.assertEqual(len(d["polylines"]), len(d["colors"]))
        json.dumps(d)
        for pl in d["polylines"]:
            for pt in pl:
                self.assertEqual(len(pt), 2)


@unittest.skipIf(inkex is None, "inkex not importable")
class ExtractUnits(unittest.TestCase):
    def check(self, name, eid, expected):
        svg = load(name)
        r = ex.extract(svg, [svg.getElementById(eid)])
        got = bbox(r.polylines)
        for g, e in zip(got, expected):
            self.assertAlmostEqual(g, e, delta=TOL, msg="%s: %r != %r" % (name, got, expected))
        return r

    def test_viewbox_scale(self):
        self.check("viewbox_scale.svg", "rect1", (10, 5, 60, 30))
        self.check("viewbox_scale.svg", "circle1", (65, 15, 85, 35))

    def test_inches(self):
        r = self.check("inches.svg", "rect1", (25.4, 12.7, 76.2, 38.1))
        self.assertTrue(closed(r.polylines[0]))

    def test_px_and_viewbox_offset(self):
        self.check("px_offset.svg", "rect1", (0, 12.7, 25.4, 25.4))


@unittest.skipIf(inkex is None, "inkex not importable")
class ExtractText(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svg = load("text.svg")

    def els(self, *ids):
        return [self.svg.getElementById(i) for i in ids]

    def test_find_text(self):
        texts = ex.find_text(self.svg, self.els("rect1", "g1", "use1", "g2"))
        self.assertEqual(len(texts), 3)
        self.assertEqual(ex.find_text(self.svg, self.els("rect1")), [])

    def test_text_without_conversion_warns(self):
        r = ex.extract(self.svg, self.els("rect1", "g1"))
        self.assertEqual(len(r.polylines), 1)
        self.assertTrue(any("text" in w for w in r.warnings))

    def test_actions_string(self):
        s = ex.text_to_path_actions(["a", "b"], r"C:\t\o.svg")
        self.assertEqual(s, r"select-by-id:a,b;object-to-path;export-filename:C:\t\o.svg;export-plain-svg;export-do")

    @unittest.skipUnless(ex and ex.find_inkscape(), "Inkscape CLI not found")
    def test_text_conversion_with_inkscape(self):
        before = inkex.etree.tostring(self.svg)
        sel = self.els("rect1", "g1", "use1", "g2")
        texts = ex.find_text(self.svg, sel)
        converted, ids = ex.convert_text_to_paths(self.svg, texts)
        self.assertEqual(inkex.etree.tostring(self.svg), before, "original document modified")
        self.assertEqual(len(ids), 3)
        r = ex.extract(self.svg, sel, converted=converted, text_ids=ids)
        self.assertEqual(r.warnings, [])
        # text1: 'Hi' in a translated group
        r1 = ex.extract(self.svg, self.els("g1"), converted=converted, text_ids=ids)
        self.assertGreaterEqual(len(r1.polylines), 3)
        x0, y0, x1, y1 = bbox(r1.polylines)
        self.assertTrue(15 < x0 < 18 and 25 < x1 < 28 and 15 < y0 < 18 and 24.5 < y1 < 25.5, bbox(r1.polylines))
        self.assertTrue(all(c == "#ff0000" for c in r1.colors))
        self.assertTrue(all(closed(p) for p in r1.polylines))
        # clone of a text in <defs>
        r2 = ex.extract(self.svg, self.els("use1"), converted=converted, text_ids=ids)
        x0, y0, x1, y1 = bbox(r2.polylines)
        self.assertTrue(50 <= x0 and x1 <= 59 and 21 <= y0 and y1 <= 30.5, bbox(r2.polylines))
        # id-less text with a coloured tspan
        r3 = ex.extract(self.svg, self.els("g2"), converted=converted, text_ids=ids)
        self.assertGreaterEqual(len(r3.polylines), 2)
        self.assertIn("#00aa00", r3.colors)


@unittest.skipIf(inkex is None, "inkex not importable")
class EffectCommandLine(unittest.TestCase):
    """Run inkcut2.py the way Inkscape does (no GUI launch)."""

    def run_effect(self, fixture, *ids, extra=()):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([INKEX_DIR, ROOT])
        env["INKCUT2_NO_LAUNCH"] = "1"
        args = [sys.executable, os.path.join(ROOT, "inkcut2.py")]
        args += ["--id=%s" % i for i in ids] + list(extra) + [os.path.join(FIX, fixture)]
        return subprocess.run(args, env=env, cwd=ROOT, capture_output=True, timeout=300)

    def test_writes_json_and_leaves_document(self):
        p = self.run_effect("viewbox_scale.svg", "rect1", "circle1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, b"", "document must be unchanged (no output)")
        path = p.stderr.decode().strip().splitlines()[-1]
        self.assertTrue(path.endswith(".json") and os.path.isfile(path), p.stderr)
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        self.assertEqual(d["version"], 1)
        self.assertEqual(d["name"], "viewbox_scale.svg")
        self.assertEqual(len(d["polylines"]), 2)
        self.assertEqual(len(d["colors"]), 2)
        os.remove(path)

    def test_text_selection(self):
        p = self.run_effect("text.svg", "rect1", "text1", "use1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout, b"", "document must be unchanged (no output)")
        path = p.stderr.decode().strip().splitlines()[-1]
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        self.assertGreaterEqual(len(d["polylines"]), 5)  # rect + H + i(2) + D(2)
        self.assertIn("#ff0000", d["colors"])
        os.remove(path)

    def test_nothing_selected(self):
        p = self.run_effect("shapes.svg")
        self.assertEqual(p.stdout, b"")
        self.assertIn(b"select", p.stderr.lower())
        self.assertNotIn(b".json", p.stderr)

    def test_whole_document_scope(self):
        p = self.run_effect("inches.svg", extra=["--scope=document"])
        self.assertEqual(p.returncode, 0, p.stderr)
        path = p.stderr.decode().strip().splitlines()[-1]
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(len(json.load(fh)["polylines"]), 1)
        os.remove(path)


if __name__ == "__main__":
    unittest.main()
