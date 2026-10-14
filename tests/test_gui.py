"""Tests for the VinylCut GTK window (vinylcut/gui.py, gui_preview.py, gui_widgets.py).

Stdlib unittest only. GTK tests use offscreen rendering and never open a visible window.
Config files are redirected to a temporary directory so the user's settings are untouched.
"""

import json
import os
import shutil
import tempfile
import time
import unittest

_TMP = None
_OLD_ENV = {}


def setUpModule():
    global _TMP
    _TMP = tempfile.mkdtemp(prefix="vinylcut-gui-test-")
    for k in ("APPDATA", "VINYLCUT_CONFIG_DIR"):
        _OLD_ENV[k] = os.environ.get(k)
    os.environ["APPDATA"] = _TMP
    os.environ["VINYLCUT_CONFIG_DIR"] = os.path.join(_TMP, "vinylcut")


def tearDownModule():
    for k, v in _OLD_ENV.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(_TMP, ignore_errors=True)


try:
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk  # noqa: F401
    HAVE_GTK = True
except Exception:  # noqa: BLE001
    HAVE_GTK = False

if HAVE_GTK:
    from vinylcut import gui
    from vinylcut import gui_preview as gp

    try:
        BACKEND = gui.load_backend()
        BACKEND_ERR = None
    except Exception as e:  # noqa: BLE001
        BACKEND, BACKEND_ERR = None, e


def _pump(seconds=0.2):
    from gi.repository import Gtk
    end = time.time() + seconds
    while time.time() < end:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        time.sleep(0.01)


@unittest.skipUnless(HAVE_GTK, "GTK 3 (gi) not available")
class TestHelpers(unittest.TestCase):
    def test_parse_design_ok_and_units(self):
        d = gui.parse_design({"version": 1, "name": "a.svg", "units": "cm",
                              "polylines": [[[0, 0], [1, 0], [1, 1], [0, 0]], [[5, 5]]],
                              "colors": ["#000", "#f00"]})
        self.assertEqual(d["name"], "a.svg")
        self.assertEqual(len(d["polylines"]), 1)           # single-point polyline dropped
        self.assertEqual(d["polylines"][0][1], (10.0, 0.0))  # cm -> mm
        self.assertEqual(d["colors"], ["#000"])

    def test_parse_design_errors(self):
        for bad in ([], {"polylines": "x"}, {"polylines": [[["a", 1], [2, 3]]]},
                    {"polylines": []}, {"units": "furlong", "polylines": [[[0, 0], [1, 1]]]},
                    {"polylines": [[[0, 0], [float("nan"), 1]]]}):
            with self.assertRaises(gui.DesignError):
                gui.parse_design(bad)

    def test_load_design_file(self):
        p = os.path.join(_TMP, "design.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "polylines": [[[0, 0], [10, 0], [10, 5]]]}, f)
        self.assertEqual(len(gui.load_design(p)["polylines"]), 1)
        with self.assertRaises(gui.DesignError):
            gui.load_design(os.path.join(_TMP, "missing.json"))
        bad = os.path.join(_TMP, "bad.json")
        with open(bad, "w") as f:
            f.write("{not json")
        with self.assertRaises(gui.DesignError):
            gui.load_design(bad)

    def test_demo_design(self):
        d = gui.demo_design()
        closed = [gp._is_closed(p) for p in d["polylines"]]
        self.assertEqual(closed, [True, True, True, False])

    def test_small_helpers(self):
        self.assertEqual(gui.coerce_value("true"), True)
        self.assertEqual(gui.coerce_value("2.5"), 2.5)
        self.assertEqual(gui.coerce_value("null"), None)
        self.assertEqual(gui.coerce_value("shortest"), "shortest")
        self.assertEqual(gui.format_duration(59), "59 s")
        self.assertEqual(gui.format_duration(125), "2 min 05 s")
        self.assertEqual(gui.format_duration(3700), "1 h 01 min")
        self.assertEqual(gui.format_length(950), "950 mm")
        self.assertEqual(gui.format_length(2500), "2.50 m")
        self.assertEqual(gui.unique_name("A", ["A", "A (2)"]), "A (3)")
        self.assertEqual(gui.unique_name("B", ["A"]), "B")
        self.assertEqual(gui.stopbits_value("1.5"), 1.5)
        self.assertIsInstance(gui.stopbits_value("2"), int)

    def test_progress_mapping(self):
        self.assertEqual(gui.progress_to_timeline(0.5, 3, "per_path"), (0.5, None))
        f, k = gui.progress_to_timeline(0.5, 2, "per_job")
        self.assertAlmostEqual(f, 0.0)
        self.assertEqual(k, 2)
        f, k = gui.progress_to_timeline(0.25, 2, "per_job")
        self.assertAlmostEqual(f, 0.5)
        self.assertEqual(k, 1)
        self.assertEqual(gui.progress_to_timeline(1.0, 3, "per_job"), (1.0, 3))

    def test_apply_overrides(self):
        class S:
            rows = 1
            mirror_h = False
        s = gui.apply_overrides(S(), ["rows=3", "mirror_h=true"])
        self.assertEqual((s.rows, s.mirror_h), (3, True))
        with self.assertRaises(ValueError):
            gui.apply_overrides(S(), ["nope=1"])
        with self.assertRaises(ValueError):
            gui.apply_overrides(S(), ["rows"])


@unittest.skipUnless(HAVE_GTK, "GTK 3 (gi) not available")
class TestPreview(unittest.TestCase):
    def test_snap_and_steps(self):
        self.assertEqual(gp.snap(12.4, 1), 12)
        self.assertEqual(gp.snap(12.46, 0.1), 12.5)
        self.assertEqual(gp.snap(3.3, 0), 3.3)
        self.assertGreaterEqual(gp.nice_step(2.0, 60) * 2.0, 60)
        # inch steps are binary fractions / whole inches, returned in mm
        st = gp.nice_step(2.0, 60, "in")
        self.assertGreaterEqual(st * 2.0, 60)
        self.assertIn(round(st / 25.4, 6), [round(v, 6) for v in gp.NICE_STEPS_IN])
        self.assertAlmostEqual(gp.minor_step(25.4, 10.0, 8, "in"), 25.4 / 4)

    def test_fmt_len_units(self):
        self.assertEqual(gp.fmt_len(610, "mm"), "610 mm")
        self.assertEqual(gp.fmt_len(609.6, "in"), "24 in")
        self.assertEqual(gp.fmt_len(610, "in"), "24.02 in")

    def test_closed_detection_with_overcut(self):
        sq = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
        self.assertTrue(gp._is_closed(sq))
        self.assertTrue(gp._is_closed(sq + [(0.5, 0)]))      # overcut continues past start
        self.assertFalse(gp._is_closed([(0, 0), (10, 0), (10, 10)]))

    def test_scene_bounds_and_timeline(self):
        sq = [(5, 5), (15, 5), (15, 15), (5, 15), (5, 5)]
        out = [(20, 5), (20, 120)]                     # crosses the 100 mm material width
        sc = gp.Scene(width_mm=100, length_mm=1000, padding=(2, 2, 2, 2), paths=[sq, out])
        self.assertEqual(sc.oob, [False, True])
        self.assertEqual(sc.closed, [True, False])
        self.assertAlmostEqual(sc.cut_total, 40.0 + 115.0)
        inside = gp.Scene(width_mm=100, length_mm=1000, padding=(6, 0, 0, 0), paths=[sq])
        self.assertEqual(inside.oob, [True])           # starts inside the 6 mm left margin
        done, pi, plen, knife, travel = sc.state_at(0.0)
        self.assertEqual(done, 0)
        done, pi, plen, knife, travel = sc.state_at(1.0)
        self.assertEqual(done, 2)
        self.assertEqual(knife, (20, 120))
        # monotonic progress
        last = -1
        for i in range(21):
            d = sc.state_at(i / 20.0)[0]
            self.assertGreaterEqual(d, last)
            last = d

    def test_render_png(self):
        sq = [(5, 5), (15, 5), (15, 15), (5, 15), (5, 5)]
        area = gp.PreviewArea()
        area.set_scene(gp.Scene(width_mm=100, length_mm=500, paths=[sq], warnings=["test warning"]))
        area.set_progress(0.5, "Simulating")
        p = os.path.join(_TMP, "preview.png")
        area.render_png(p, 640, 400, fit="material")
        area.set_unit("in")
        area.render_png(os.path.join(_TMP, "preview_in.png"), 640, 400, fit="material")
        area.set_unit("bogus")
        self.assertEqual(area.unit, "mm")
        with open(p, "rb") as f:
            self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")
        # fit puts the material origin inside the view
        self.assertTrue(0 < area.sx(0) < 640 and 0 < area.sy(0) < 400)
        x, y = area.to_mm(area.sx(12.5), area.sy(7.5))
        self.assertAlmostEqual(x, 12.5)
        self.assertAlmostEqual(y, 7.5)


@unittest.skipUnless(HAVE_GTK, "GTK 3 (gi) not available")
class TestWindow(unittest.TestCase):
    def setUp(self):
        if BACKEND is None:
            self.skipTest("backend modules not importable: %s" % BACKEND_ERR)
        from gi.repository import Gtk
        self.app = gui.CutterApp(gui.demo_design(), BACKEND, settings=BACKEND.job.JobSettings(),
                                 persist=False)
        self.win = Gtk.OffscreenWindow()
        self.win.add(self.app.root)
        self.win.set_default_size(1200, 800)
        self.win.show_all()
        _pump(0.1)

    def tearDown(self):
        if getattr(self, "win", None) is not None:
            self.win.destroy()

    def test_builds_scene(self):
        self.assertIsNotNone(self.app.scene)
        self.assertEqual(len(self.app.scene.paths), 4)
        self.assertEqual(self.app.stats.get("copies"), 1)

    def test_array_mirror_rotate_via_widgets(self):
        a = self.app
        a.w["rows"].set_value(2)
        a.w["cols"].set_value(3)
        a.w["mirror_h"].set_active(True)
        a.rot_buttons[90].set_active(True)
        a.flush()
        self.assertEqual((a.settings.rows, a.settings.cols), (2, 3))
        self.assertTrue(a.settings.mirror_h)
        self.assertEqual(a.settings.rotation, 90.0)
        self.assertEqual(a.w["rotation"].get_value(), 90.0)
        self.assertEqual(a.stats["copies"], 6)
        self.assertEqual(len(a.scene.paths), 24)
        # demo is 90 x 60 mm; rotated 90 deg -> 60 x 90
        self.assertAlmostEqual(a._copy_size[0], 60.0, places=3)
        self.assertAlmostEqual(a._copy_size[1], 90.0, places=3)

    def test_drag_sets_offset(self):
        a = self.app
        a.preview.on_job_moved(15.0, 8.0)
        self.assertEqual((a.settings.offset_x, a.settings.offset_y), (15.0, 8.0))
        self.assertEqual(a.w["offset_x"].get_value(), 15.0)
        self.assertAlmostEqual(a.scene.job_bbox[0], 15.0 + a.device.padding[0], places=3)
        a._on_reset_origin()
        self.assertEqual((a.settings.offset_x, a.settings.offset_y), (0.0, 0.0))

    def test_area_units_toggle(self):
        a = self.app
        d = a.dw
        dev = a.device
        dev.width_mm, dev.length_mm, dev.padding = 610.0, 5000.0, (10.0, 2.0, 0.0, 0.0)
        a._load_device_widgets()
        a._mark_dirty(False)
        d["area_unit"]["in"].set_active(True)
        self.assertEqual(a.area_unit, "in")
        self.assertEqual(a.preview.unit, "in")
        self.assertAlmostEqual(d["width_mm"].get_value(), 610 / 25.4, places=6)
        self.assertAlmostEqual(d["pad"][0].get_value(), 10 / 25.4, places=6)
        self.assertEqual(d["area_units"][0].get_text(), "in")
        self.assertFalse(a.device_dirty)             # switching units is not an edit
        self.assertEqual(dev.width_mm, 610.0)
        # an edit in inches is stored in mm
        d["width_mm"].set_value(24.0)
        self.assertAlmostEqual(dev.width_mm, 609.6, places=6)
        d["pad"][1].set_value(0.5)
        self.assertAlmostEqual(dev.padding[1], 12.7, places=6)
        # re-entering the displayed (rounded) value keeps the exact mm value
        d["length_mm"].set_value(round(5000 / 25.4, d["length_mm"].get_digits()))
        self.assertEqual(dev.length_mm, 5000.0)
        d["area_unit"]["mm"].set_active(True)
        self.assertAlmostEqual(d["width_mm"].get_value(), 609.6, places=6)
        self.assertEqual(a.preview.unit, "mm")
        self.assertEqual(a.gstate.get("area_unit"), "mm")

    def test_optional_speed(self):
        a = self.app
        a.w["speed"].check.set_active(False)
        a.w["speed"].spin.set_value(12)
        self.assertEqual(a.settings.speed, 12.0)
        a.w["speed"].check.set_active(True)
        self.assertIsNone(a.settings.speed)

    def test_connection_widgets_update_profile(self):
        a = self.app
        a.cw["kind"].set_active_id("serial")
        a.cw["parity"].set_active_id("O")
        a.cw["stopbits"].set_active_id("2")
        a.cw["bytesize"].set_active_id("7")
        a.cw["flow"].set_active_id("dsrdtr")
        a.cw["baudrate"].get_child().set_text("115200")
        c = a.device.connection
        self.assertEqual((c.kind, c.parity, c.stopbits, c.bytesize, c.flow, c.baudrate),
                         ("serial", "O", 2, 7, "dsrdtr", 115200))
        self.assertTrue(a.device_dirty)
        self.assertEqual(a.conn_stack.get_visible_child_name(), "serial")

    def test_send_to_file(self):
        a = self.app
        out = os.path.join(_TMP, "out.plt")
        a.device.connection.kind = "file"
        a.device.connection.file_path = out
        a.flush()
        a._start_send(a.job.toolpaths(), a.job.effective("speed"), a.job.effective("force"), "Cutting", True)
        end = time.time() + 15
        while a._busy and time.time() < end:
            _pump(0.05)
        self.assertFalse(a._busy)
        self.assertTrue(os.path.exists(out))
        self.assertGreater(os.path.getsize(out), 20)
        self.assertEqual(a.progress.get_fraction(), 1.0)

    def test_failed_send_reports_error(self):
        a = self.app
        msgs = []
        a.message = lambda *args, **kw: msgs.append(args)
        a.device.connection.kind = "network"
        a.device.connection.host = "256.256.256.256"  # invalid -> error, never sends anywhere
        a.device.connection.net_port = 9
        a.flush()
        a._start_send(a.job.toolpaths(), 30, 80, "Cutting", True)
        end = time.time() + 20
        while a._busy and time.time() < end:
            _pump(0.05)
        self.assertFalse(a._busy)
        self.assertTrue(msgs, "an error dialog should have been requested")


@unittest.skipUnless(HAVE_GTK, "GTK 3 (gi) not available")
class TestCommandLine(unittest.TestCase):
    def setUp(self):
        if BACKEND is None:
            self.skipTest("backend modules not importable: %s" % BACKEND_ERR)

    def test_snapshot(self):
        out = os.path.join(_TMP, "snap.png")
        rc = gui.main(["--demo", "--snapshot", out, "--width", "900", "--height", "600",
                       "--set", "rows=2", "--set", "mirror_v=true"])
        self.assertEqual(rc, 0)
        for p in (out, os.path.join(_TMP, "snap_preview.png")):
            with open(p, "rb") as f:
                self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")

    def test_snapshot_bad_design(self):
        bad = os.path.join(_TMP, "empty.json")
        with open(bad, "w") as f:
            json.dump({"version": 1, "polylines": []}, f)
        self.assertEqual(gui.main([bad, "--snapshot", os.path.join(_TMP, "x.png")]), 2)
        self.assertEqual(gui.main([os.path.join(_TMP, "nope.json"), "--snapshot", os.path.join(_TMP, "y.png")]), 2)

    def test_snapshot_bad_override(self):
        self.assertEqual(gui.main(["--demo", "--set", "bogus=1", "--snapshot", os.path.join(_TMP, "z.png")]), 2)


if __name__ == "__main__":
    unittest.main()
