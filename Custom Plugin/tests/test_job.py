import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

from inkcut2 import geometry as G
from inkcut2 import job as J
from inkcut2.job import Job, JobSettings


def device(**kw):
    d = dict(width_mm=610, length_mm=5000, padding=(0, 0, 0, 0), default_speed=30, default_force=80,
             default_overcut=0.5, default_blade_offset=0.25)
    d.update(kw)
    return SimpleNamespace(**d)


def square(x0, y0, s):
    return [(x0, y0), (x0 + s, y0), (x0 + s, y0 + s), (x0, y0 + s), (x0, y0)]


def plain(**kw):
    """Settings with no overcut / blade offset so geometry is easy to check."""
    kw.setdefault("overcut", 0.0)
    kw.setdefault("blade_offset", 0.0)
    return JobSettings(**kw)


def rnd(b):
    return tuple(round(v, 6) for v in b)


class PipelineTest(unittest.TestCase):
    def test_y_flip(self):
        # SVG triangle: apex at the top (y = 0, y down), base at y = 10
        tri = [(5, 0), (10, 10), (0, 10), (5, 0)]
        job = Job([tri], plain(), device())
        cg = job.copy_geometry()
        self.assertEqual(G.bbox(cg), (0, 0, 10, 10))
        self.assertEqual(cg[0][0], (5.0, 10.0))          # top of design -> max y in material
        self.assertEqual(cg[0][1], (10.0, 0.0))
        self.assertTrue(G.is_closed(cg[0]))

    def test_normalised_and_input_untouched(self):
        design = [[[100, 200], [110, 200], [110, 220]]]
        job = Job(design, plain(), device())
        self.assertEqual(G.bbox(job.copy_geometry()), (0, 0, 10, 20))
        self.assertEqual(design, [[[100, 200], [110, 200], [110, 220]]])

    def test_array_bboxes(self):
        job = Job([square(0, 0, 10)], plain(rows=3, cols=2, spacing_x=2, spacing_y=2), device())
        lay = job.layout()
        self.assertEqual(len(lay), 6)
        got = sorted(rnd(G.bbox(c)) for c in lay)
        exp = sorted((c * 12.0, r * 12.0, c * 12.0 + 10, r * 12.0 + 10) for r in range(3) for c in range(2))
        self.assertEqual(got, exp)
        self.assertEqual(job.grid_size(), (3, 2))
        st = job.stats()
        self.assertEqual(st["copies"], 6)
        self.assertEqual(st["paths"], 6)
        self.assertEqual(rnd(st["size_mm"]), (22.0, 34.0))   # cols along x, rows along y

    def test_offset_and_padding(self):
        dev = device(padding=(5, 3, 1, 1))
        job = Job([square(0, 0, 10)], plain(offset_x=7, offset_y=4), dev)
        self.assertEqual(rnd(job.bounds()), (12, 7, 22, 17))

    def test_mirror_h(self):
        # asymmetric "L": vertical bar on the left, foot to the right (SVG coords)
        L = [(0, 0), (2, 0), (2, 8), (6, 8), (6, 10), (0, 10), (0, 0)]
        base = Job([L], plain(), device()).copy_geometry()
        mir = Job([L], plain(mirror_h=True), device()).copy_geometry()
        self.assertEqual(G.bbox(base), G.bbox(mir))
        # in material coords the bar is at x in [0,2]; mirrored it is at x in [4,6]
        self.assertIn((0.0, 10.0), base[0])
        self.assertIn((6.0, 10.0), mir[0])
        self.assertEqual(sorted(mir[0]), sorted((6.0 - x, y) for x, y in base[0]))
        mv = Job([L], plain(mirror_v=True), device()).copy_geometry()
        self.assertEqual(sorted(mv[0]), sorted((x, 10.0 - y) for x, y in base[0]))

    def test_rotation_swaps(self):
        rect = square(0, 0, 1)
        rect = [(x * 20, y * 10) for x, y in rect]
        j0 = Job([rect], plain(), device())
        j90 = Job([rect], plain(rotation=90), device())
        self.assertEqual(rnd(j0.copy_size()), (20, 10))
        self.assertEqual(rnd(j90.copy_size()), (10, 20))
        self.assertEqual(G.bbox(j90.copy_geometry())[:2], (0, 0))
        j45 = Job([rect], plain(rotation=45), device())
        w, h = j45.copy_size()
        self.assertAlmostEqual(w, 30 / math.sqrt(2))

    def test_rotation_direction(self):
        # marker point at material (+x end); rotating 90 CCW should move it to the top
        line = [(0, 0), (10, 0)]          # horizontal line, SVG y = 0
        arrow = [(10, 0), (9, 1)]        # short tick at the right end, going down in SVG
        j = Job([line, arrow], plain(rotation=90), device())
        cg = j.copy_geometry()
        tip = cg[1][0]
        self.assertAlmostEqual(tip[1], G.bbox(cg)[3])     # right end now at max y

    def test_scale(self):
        j = Job([square(0, 0, 10)], plain(scale_pct=250), device())
        self.assertEqual(rnd(j.copy_size()), (25, 25))
        bad = Job([square(0, 0, 10)], plain(scale_pct=0), device())
        self.assertEqual(rnd(bad.copy_size()), (10, 10))
        self.assertTrue(any("Scale" in w for w in bad.warnings()))

    def test_auto_fill(self):
        dev = device(width_mm=50, length_mm=100)
        j = Job([square(0, 0, 10)], plain(auto_fill=True, rows=7, cols=7, spacing_x=2, spacing_y=2), dev)
        # along x: 8*10 + 7*2 = 94 <= 100 ; across y: 4*10 + 3*2 = 46 <= 50
        self.assertEqual(j.grid_size(), (4, 8))
        self.assertEqual(j.stats()["copies"], 32)
        self.assertTrue(j.fits())
        # padding + offset reduce the usable area
        dev = device(width_mm=50, length_mm=100, padding=(5, 0, 5, 0))
        j = Job([square(0, 0, 10)], plain(auto_fill=True, offset_y=5), dev)
        self.assertEqual(j.usable_area(), (90, 45))
        self.assertEqual(j.grid_size(), (3, 7))
        self.assertTrue(j.fits())
        # fill_length overrides material length
        j = Job([square(0, 0, 10)], plain(auto_fill=True, fill_length=50), device(width_mm=50))
        self.assertEqual(j.grid_size(), (4, 4))
        # too big -> one copy + warning
        j = Job([square(0, 0, 100)], plain(auto_fill=True), device(width_mm=50, length_mm=100))
        self.assertEqual(j.grid_size(), (1, 1))
        self.assertTrue(any("auto-fill" in w for w in j.warnings()))
        self.assertFalse(j.fits())

    def test_auto_fill_cap(self):
        j = Job([square(0, 0, 1)], plain(auto_fill=True, spacing_x=0, spacing_y=0),
                device(width_mm=610, length_mm=50000))
        r, c = j.grid_size()
        self.assertLessEqual(r * c, J.MAX_AUTO_COPIES)
        self.assertTrue(any("limited" in w for w in j.warnings()))

    def test_weed_border(self):
        s = plain(rows=2, cols=2, spacing_x=2, spacing_y=2, weed_border=True, weed_margin=3, offset_x=1)
        j = Job([square(0, 0, 10)], s, device())
        lay = j.layout()
        self.assertEqual(len(lay), 5)
        border = lay[-1]
        self.assertEqual(len(border), 1)
        self.assertTrue(G.is_closed(border[0]))
        body = G.bbox([p for c in lay[:-1] for p in c])
        self.assertEqual(rnd(body), (4, 3, 26, 25))
        self.assertEqual(rnd(G.bbox(border)), (1, 0, 29, 28))   # border starts at offset
        self.assertEqual(rnd(j.preview_paths()[-1][0]), rnd(border[0][0]))  # cut last
        self.assertEqual(rnd(j.bounds()), (1, 0, 29, 28))
        # auto fill accounts for the border margins
        dev = device(width_mm=28, length_mm=28)
        s = plain(auto_fill=True, spacing_x=2, spacing_y=2, weed_border=True, weed_margin=3)
        j = Job([square(0, 0, 10)], s, dev)
        self.assertEqual(j.grid_size(), (2, 2))
        self.assertTrue(j.fits())

    def test_order_in_preview(self):
        design = G.test_cut_shape(20)[::-1]          # square first, triangle second
        j = Job(design, plain(order="inside_out"), device())
        pv = j.preview_paths()
        self.assertEqual(len(pv), 2)
        self.assertLess(G.bbox([pv[0]])[2] - G.bbox([pv[0]])[0], 20)   # triangle first
        j = Job(design, plain(order="as_is"), device())
        self.assertEqual(rnd(G.bbox([j.preview_paths()[0]])), (0, 0, 20, 20))

    def test_overcut_and_blade_offset_applied(self):
        j = Job([square(0, 0, 10)], JobSettings(overcut=1.0, blade_offset=0.0), device())
        self.assertAlmostEqual(G.path_length(j.preview_paths()), 41)
        j = Job([square(0, 0, 10)], JobSettings(), device())       # device defaults 0.5 / 0.25
        pv = j.preview_paths()
        self.assertGreater(G.path_length(pv), 40.5)
        self.assertEqual(j.effective("overcut"), 0.5)
        self.assertEqual(j.effective("blade_offset"), 0.25)
        # ordered_paths stays uncompensated
        self.assertAlmostEqual(G.path_length(j.ordered_paths()), 40)

    def test_passes(self):
        design = [square(0, 0, 10), square(20, 0, 10)]
        one = Job(design, plain(order="as_is"), device()).preview_paths()
        pp = Job(design, plain(order="as_is", passes=3), device()).toolpaths()
        self.assertEqual(pp, [one[0]] * 3 + [one[1]] * 3)
        pj = Job(design, plain(order="as_is", passes=2, pass_mode="per_job"), device())
        self.assertEqual(pj.toolpaths(), one + one)
        self.assertTrue(any("per_job" in w for w in pj.warnings()))
        st = pj.stats()
        self.assertEqual(st["passes"], 2)
        self.assertEqual(st["toolpaths"], 4)
        self.assertAlmostEqual(st["total_cut_length_mm"], 2 * st["cut_length_mm"])

    def test_effective(self):
        j = Job([square(0, 0, 1)], JobSettings(speed=12, force=None), device())
        self.assertEqual(j.effective("speed"), 12)
        self.assertEqual(j.effective("force"), 80)
        with self.assertRaises(KeyError):
            j.effective("colour")
        j = Job([square(0, 0, 1)], JobSettings(), SimpleNamespace(width_mm=100, length_mm=100))
        self.assertEqual(j.effective("speed"), 30)      # fallback when the device lacks defaults
        self.assertEqual(j.effective("overcut"), 0)

    def test_stats_time(self):
        # inside_out (default) re-starts the square at its corner nearest the origin: (30, 40)
        j = Job([square(0, 0, 10)], plain(speed=10, offset_x=30, offset_y=40), device())
        st = j.stats()
        self.assertAlmostEqual(st["cut_length_mm"], 40)
        self.assertAlmostEqual(st["travel_length_mm"], 50)
        # 40 mm / 100 mm/s + 50 mm / 200 mm/s + 1 path overhead
        self.assertAlmostEqual(st["est_time_s"], 0.4 + 0.25 + J.PEN_OVERHEAD_S)
        dev = device(travel_speed=50)
        st = Job([square(0, 0, 10)], plain(speed=10, offset_x=30, offset_y=40), dev).stats()
        self.assertAlmostEqual(st["est_time_s"], 0.4 + 0.1 + J.PEN_OVERHEAD_S)
        st3 = Job([square(0, 0, 10)], plain(speed=10, offset_x=30, offset_y=40, passes=3), device()).stats()
        self.assertAlmostEqual(st3["est_time_s"], 3 * 0.4 + 0.25 + 3 * J.PEN_OVERHEAD_S)

    def test_warnings_and_fits(self):
        j = Job([square(0, 0, 10)], plain(offset_y=605.0), device())
        w = j.warnings()
        self.assertTrue(any("width by 5.0 mm" in x for x in w), w)
        self.assertFalse(j.fits())
        j = Job([square(0, 0, 10)], plain(offset_x=4995), device())
        self.assertTrue(any("length by 5.0 mm" in x for x in j.warnings()))
        j = Job([square(0, 0, 10)], plain(offset_x=-2), device())
        self.assertFalse(j.fits())
        j = Job([square(0, 0, 10)], plain(), device(padding=(0, 0, 0, 5), width_mm=12))
        self.assertTrue(any("width by 3.0 mm" in x for x in j.warnings()))
        ok = Job([square(0, 0, 10)], plain(), device())
        self.assertEqual(ok.warnings(), [])
        self.assertTrue(ok.fits())
        big = Job([square(0, 0, 1)], plain(rows=40, cols=40, spacing_x=0.5, spacing_y=0.5), device())
        self.assertTrue(any("Large job" in x for x in big.warnings()))
        rng = Job([square(0, 0, 1)], plain(speed=99), device(speed_range=(1, 60)))
        self.assertTrue(any("Speed" in x for x in rng.warnings()))

    def test_empty(self):
        for design in ([], [[]]):
            j = Job(design, JobSettings(), device())
            self.assertEqual(j.copy_geometry(), [])
            self.assertEqual(j.layout(), [])
            self.assertEqual(j.preview_paths(), [])
            self.assertEqual(j.toolpaths(), [])
            self.assertFalse(j.fits())
            self.assertTrue(any("empty" in w for w in j.warnings()))
            st = j.stats()
            self.assertEqual(st["paths"], 0)
            self.assertEqual(st["est_time_s"], 0)

    def test_single_point_and_open(self):
        j = Job([[(3, 3)], [(0, 0), (5, 5)]], JobSettings(), device())
        self.assertEqual(len(j.toolpaths()), 2)
        self.assertTrue(j.fits())

    def test_invalid_modes_fallback(self):
        s = JobSettings(order="zigzag", pass_mode="sometimes", rows=0, cols=-3, passes=0)
        j = Job([square(0, 0, 10)], s, device())
        self.assertEqual(len(j.toolpaths()), 1)
        self.assertEqual(j.grid_size(), (1, 1))
        self.assertEqual(len(j.warnings()), 2)

    def test_large_array_pipeline_speed(self):
        import time
        sticker = [square(0, 0, 20), square(2, 2, 3), square(10, 10, 4), square(11, 11, 2),
                   [(2, 15), (8, 15), (8, 18)]]
        t0 = time.perf_counter()
        j = Job(sticker, JobSettings(rows=24, cols=22), device())   # 528 copies, 2640 paths
        tp = j.toolpaths()
        st = j.stats()
        dt = time.perf_counter() - t0
        print("\n  Job 528 copies (2640 paths) full pipeline + stats: %.3f s" % dt, end="")
        self.assertEqual(st["copies"], 528)
        self.assertEqual(len(tp), 2640)
        self.assertLess(dt, 3.0)

    def test_no_devices_import(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run([sys.executable, "-c",
                              "import sys, inkcut2.job; print('inkcut2.devices' in sys.modules)"],
                             cwd=root, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "False", out.stderr)


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.environ.get(J.CONFIG_ENV_VAR)
        os.environ[J.CONFIG_ENV_VAR] = os.path.join(self.tmp, "cfg")

    def tearDown(self):
        if self.old is None:
            os.environ.pop(J.CONFIG_ENV_VAR, None)
        else:
            os.environ[J.CONFIG_ENV_VAR] = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_config_dir(self):
        self.assertEqual(J.config_dir(), os.path.join(self.tmp, "cfg"))
        os.environ.pop(J.CONFIG_ENV_VAR)
        appdata = os.environ.get("APPDATA")
        if appdata:
            self.assertEqual(J.config_dir(), os.path.join(appdata, "inkcut2"))

    def test_round_trip(self):
        s = JobSettings(scale_pct=50, mirror_h=True, rotation=90, rows=3, cols=4, auto_fill=True,
                        passes=2, pass_mode="per_job", speed=12.5, force=None, overcut=1.0,
                        order="shortest", weed_border=True, weed_margin=4.5)
        J.save_last_settings(s)
        self.assertTrue(os.path.isfile(os.path.join(J.config_dir(), "job.json")))
        self.assertEqual(J.load_last_settings(), s)
        self.assertEqual(JobSettings.from_dict(s.to_dict()), s)
        self.assertEqual(json.loads(json.dumps(s.to_dict())), s.to_dict())

    def test_missing_and_corrupt(self):
        self.assertEqual(J.load_last_settings(), JobSettings())
        os.makedirs(J.config_dir())
        with open(os.path.join(J.config_dir(), "job.json"), "w") as fh:
            fh.write("{not json")
        self.assertEqual(J.load_last_settings(), JobSettings())
        with open(os.path.join(J.config_dir(), "job.json"), "w") as fh:
            fh.write("[1, 2]")
        self.assertEqual(J.load_last_settings(), JobSettings())

    def test_from_dict_coercion(self):
        s = JobSettings.from_dict({
            "scale_pct": "75", "mirror_h": "true", "mirror_v": 0, "rows": "3", "cols": 2.0,
            "passes": "2.6", "speed": "", "force": "120", "overcut": None, "order": "nope",
            "pass_mode": "per_job", "weed_border": "yes", "spacing_x": "abc", "bogus": 1,
            "rotation": float("nan"),
        })
        self.assertEqual(s.scale_pct, 75.0)
        self.assertIs(s.mirror_h, True)
        self.assertIs(s.mirror_v, False)
        self.assertEqual((s.rows, s.cols, s.passes), (3, 2, 3))
        self.assertIsNone(s.speed)
        self.assertEqual(s.force, 120.0)
        self.assertIsNone(s.overcut)
        self.assertEqual(s.order, "inside_out")
        self.assertEqual(s.pass_mode, "per_job")
        self.assertIs(s.weed_border, True)
        self.assertEqual(s.spacing_x, 2.0)
        self.assertEqual(s.rotation, 0.0)
        self.assertEqual(JobSettings.from_dict(None), JobSettings())


if __name__ == "__main__":
    unittest.main()
