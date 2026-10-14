import re
import types
import unittest

from inkcut2 import protocols
from inkcut2.devices import DeviceProfile, preset

SQUARE = [[(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)]]


def hpgl_dev(**opts):
    return DeviceProfile(name="t", protocol="hpgl", steps_per_mm=40,
                         speed_range=(1, 70), force_range=(5, 350), protocol_options=opts)


class HPGLTests(unittest.TestCase):
    def test_gcc_square_exact(self):
        d = preset("GCC Expert II 24")
        out = protocols.encode(SQUARE, d, 30, 80)
        self.assertEqual(out, b"IN;VS30;FS80;PU0,0;PD400,0,400,400,0,400,0,0;PU0,0;IN;")

    def test_generic_square_exact(self):
        out = protocols.encode(SQUARE, preset("Generic HPGL"), 30, 80)
        self.assertEqual(out, b"IN;PU0,0;PD400,0,400,400,0,400,0,0;PU0,0;IN;")

    def test_two_paths_and_offset(self):
        paths = [[(5, 5), (6, 5)], [(20, 1), (20, 2), (21, 2)]]
        out = protocols.encode(paths, hpgl_dev(), 30, 80)
        self.assertEqual(out, b"IN;PU200,200;PD240,200;PU800,40;PD800,80,840,80;PU0,0;IN;")

    def test_duplicates_dropped_after_rounding(self):
        paths = [[(0, 0), (0.001, 0.001), (1, 0), (1.004, 0), (1, 1)],
                 [(3, 3), (3.001, 3.0)]]  # second path collapses to one point -> dropped
        out = protocols.encode(paths, hpgl_dev(), 30, 80)
        self.assertEqual(out, b"IN;PU0,0;PD40,0,40,40;PU0,0;IN;")

    def test_speed_force_clamped_and_rounded(self):
        d = hpgl_dev(use_speed_cmd=True, use_force_cmd=True)
        out = protocols.encode(SQUARE, d, 99.6, 1000)
        self.assertTrue(out.startswith(b"IN;VS70;FS350;"), out)
        out = protocols.encode(SQUARE, d, 12.5, 79.5)
        self.assertTrue(out.startswith(b"IN;VS13;FS80;"), out)

    def test_none_speed_force_uses_defaults(self):
        d = hpgl_dev(use_speed_cmd=True, use_force_cmd=True)
        d.default_speed, d.default_force = 25, 60
        self.assertTrue(protocols.encode(SQUARE, d, None, None).startswith(b"IN;VS25;FS60;"))

    def test_pen_newline_templates(self):
        d = hpgl_dev(pen=1, newline=True, use_speed_cmd=True, speed_cmd="!VS{speed};")
        out = protocols.encode([[(0, 0), (1, 0)]], d, 20, 80).decode()
        self.assertEqual(out, "IN;\nSP1;\n!VS20;\nPU0,0;\nPD40,0;\nPU0,0;\nIN;\n")

    def test_init_finish_override(self):
        d = hpgl_dev(use_force_cmd=True)
        d.init_cmd = "IN;PA;"
        d.finish_cmd = "PU0,0;!PG;"
        out = protocols.encode([[(0, 0), (1, 0)]], d, 20, 80)
        self.assertEqual(out, b"IN;PA;FS80;PU0,0;PD40,0;PU;PU0,0;!PG;")

    def test_no_return_home_and_feed(self):
        out = protocols.encode([[(0, 0), (1, 0)]], hpgl_dev(return_home=False), 20, 80)
        self.assertTrue(out.endswith(b"PD40,0;PU;IN;"), out)
        out = protocols.encode([[(0, 0), (100, 0)]], hpgl_dev(feed_after_mm=10), 20, 80)
        self.assertTrue(out.endswith(b"PD4000,0;PU4400,0;IN;"), out)

    def test_max_points_split(self):
        poly = [(i, 0) for i in range(0, 11)]
        out = protocols.encode([poly], hpgl_dev(max_points=4), 20, 80).decode()
        pd = re.findall(r"PD([0-9,]*);", out)
        self.assertEqual(len(pd), 3)
        self.assertEqual([len(x.split(",")) // 2 for x in pd], [4, 4, 2])

    def test_negative_clamped(self):
        out = protocols.encode([[(-1, -1), (1, 1)]], hpgl_dev(), 20, 80)
        self.assertIn(b"PU0,0;PD40,40;", out)

    def test_swap_axes_device(self):
        d = hpgl_dev()
        d.swap_axes = True
        out = protocols.encode([[(0, 0), (10, 1)]], d, 20, 80)
        self.assertIn(b"PD40,400;", out)

    def test_empty(self):
        self.assertEqual(protocols.encode([], hpgl_dev(), 20, 80), b"IN;PU0,0;IN;")

    def test_works_with_simple_namespace_device(self):
        dev = types.SimpleNamespace(protocol="hpgl", protocol_options={}, init_cmd="", finish_cmd="",
                                    default_speed=30, default_force=80,
                                    to_device=lambda x, y: (int(x * 40), int(y * 40)))
        self.assertEqual(protocols.encode([[(0, 0), (1, 0)]], dev, None, None),
                         b"IN;PU0,0;PD40,0;PU0,0;IN;")

    def test_cancel_sequence(self):
        self.assertEqual(protocols.cancel_sequence(hpgl_dev()), b";PU;")


class OtherProtocolTests(unittest.TestCase):
    def dev(self, proto, **opts):
        return DeviceProfile(name="t", protocol=proto, steps_per_mm=40, protocol_options=opts)

    def test_registry(self):
        for pid in ("hpgl", "dmpl", "gpgl", "camm", "gcode"):
            self.assertIn(pid, protocols.PROTOCOLS)
            self.assertEqual(protocols.PROTOCOLS[pid].id, pid)
        self.assertIs(protocols.get_protocol("camm-gl1"), protocols.PROTOCOLS["camm"])
        self.assertEqual(len(protocols.PROTOCOL_CHOICES), len(protocols.PROTOCOLS))
        with self.assertRaises(ValueError):
            protocols.get_protocol("nope")

    def test_all_produce_ascii_bytes(self):
        for pid in protocols.PROTOCOLS:
            out = protocols.encode(SQUARE, self.dev(pid), 30, 80)
            self.assertIsInstance(out, bytes)
            out.decode("ascii")
            self.assertGreater(len(out), 10)

    def test_dmpl_mode1(self):
        out = protocols.encode(SQUARE, self.dev("dmpl", mode=1, use_speed_cmd=True), 30, 80)
        self.assertEqual(out, b";:HAEC1V30  U0,0  D400,0  D400,400  D0,400  D0,0  U0,0  U0,0 ")

    def test_dmpl_mode6(self):
        out = protocols.encode([[(0, 0), (1, 0)]], self.dev("dmpl", mode=6), 30, 80)
        self.assertEqual(out, b"IN;PA;PU0,0;PD40,0;PU40,0;PU0,0;")

    def test_dmpl_other_modes(self):
        for mode, init in ((2, b" ;:ECN A L0 "), (3, b" ;:H A L0 "), (4, b" ;:H A L0 ")):
            out = protocols.encode(SQUARE, self.dev("dmpl", mode=mode), 30, 80)
            self.assertTrue(out.startswith(init), out)

    def test_gpgl(self):
        out = protocols.encode(SQUARE, self.dev("gpgl", use_speed_cmd=True, use_force_cmd=True),
                               30, 20)
        self.assertEqual(out, b"H\x03!30\x03FX20,1\x03M0,0\x03D400,0,400,400,0,400,0,0\x03M0,0\x03")
        out = protocols.encode(SQUARE, self.dev("gpgl", terminator="\n"), 30, 20)
        self.assertEqual(out, b"H\nM0,0\nD400,0,400,400,0,400,0,0\nM0,0\n")

    def test_camm(self):
        out = protocols.encode(SQUARE, self.dev("camm"), 30, 80)
        self.assertEqual(out, b"IN;VS30;FS80;M0,0;D400,0;D400,400;D0,400;D0,0;M0,0;")

    def test_gcode(self):
        out = protocols.encode([[(0, 0), (10, 0), (10, 5)]], self.dev("gcode"), 30, 80).decode()
        self.assertEqual(out, "G28 ; home\nG90 ; absolute coordinates\nG21 ; mm\n"
                              "G00 X0.000 Y0.000\nG01 X10.000 Y0.000\nG01 X10.000 Y5.000\n"
                              "G28 ; home\n")

    def test_gcode_z_and_feed(self):
        d = self.dev("gcode", lift_mode="z", lower_z=-1, upper_z=2, precision=1,
                     use_speed_cmd=True, use_builtin=False)
        out = protocols.encode([[(0, 0), (1, 0)]], d, 10, 80).decode()
        self.assertEqual(out, "G00 X0.0 Y0.0 Z2.0\nG01 X1.0 Y0.0 Z-1.0 F6000\nG00 Z2.0\n"
                              "G00 X0.0 Y0.0\n")

    def test_gcode_custom_lift(self):
        d = self.dev("gcode", lift_mode="custom", lift_gcode="M5", lower_gcode="M3",
                     use_builtin=False, return_home=False)
        out = protocols.encode([[(0, 0), (1, 0)], [(2, 0), (3, 0)]], d, 10, 80).decode()
        self.assertEqual(out, "G00 X0.000 Y0.000\nM3\nG01 X1.000 Y0.000\nM5\nG00 X2.000 Y0.000\n"
                              "M3\nG01 X3.000 Y0.000\nM5\n")


if __name__ == "__main__":
    unittest.main()
