import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from vinylcut import devices
from vinylcut.devices import ConnectionSettings, DeviceProfile, PRESETS


class ToDeviceTests(unittest.TestCase):
    def test_identity(self):
        d = DeviceProfile(name="t", steps_per_mm=40)
        self.assertEqual(d.to_device(0, 0), (0, 0))
        self.assertEqual(d.to_device(10, 2.5), (400, 100))
        self.assertIsInstance(d.to_device(1.01, 1.0)[0], int)

    def test_rounding_half_up(self):
        d = DeviceProfile(name="t", steps_per_mm=1)
        self.assertEqual(d.to_device(0.5, 1.5), (1, 2))
        self.assertEqual(d.to_device(2.5, 2.4999), (3, 2))

    def test_swap(self):
        d = DeviceProfile(name="t", steps_per_mm=10, swap_axes=True)
        self.assertEqual(d.to_device(1, 2), (20, 10))

    def test_invert(self):
        d = DeviceProfile(name="t", steps_per_mm=10, width_mm=100, length_mm=1000,
                          invert_x=True, invert_y=True)
        self.assertEqual(d.to_device(0, 0), (10000, 1000))
        self.assertEqual(d.to_device(1000, 100), (0, 0))

    def test_invert_then_swap(self):
        d = DeviceProfile(name="t", steps_per_mm=1, width_mm=100, length_mm=1000,
                          swap_axes=True, invert_y=True)
        # material (x=5, y=10) -> y inverted to 90 -> swapped: device (90, 5)
        self.assertEqual(d.to_device(5, 10), (90, 5))

    def test_from_device_inverse(self):
        for kw in ({}, {"swap_axes": True}, {"invert_x": True}, {"swap_axes": True, "invert_y": True}):
            d = DeviceProfile(name="t", steps_per_mm=40, width_mm=600, length_mm=2000, **kw)
            x, y = d.from_device(*d.to_device(123.45, 67.8))
            self.assertAlmostEqual(x, 123.45, delta=0.02)
            self.assertAlmostEqual(y, 67.8, delta=0.02)

    def test_gcc_preset_is_identity_mapping(self):
        g = PRESETS["GCC Expert II 24"]
        self.assertFalse(g.swap_axes or g.invert_x or g.invert_y)
        self.assertEqual(g.steps_per_mm, 40.0)
        self.assertEqual(g.to_device(25.4, 0), (1016, 0))


class DictTests(unittest.TestCase):
    def test_round_trip(self):
        p = DeviceProfile(name="X", protocol="dmpl", width_mm=500, padding=(1, 2, 3, 4),
                          speed_range=(2, 40), protocol_options={"mode": 2, "a": [1, 2]},
                          connection=ConnectionSettings(kind="network", host="h", net_port=1234,
                                                        stopbits=1.5, parity="E", flow="xonxoff"),
                          init_cmd="IN;", finish_cmd="PU;", notes="n")
        d = p.to_dict()
        json.dumps(d)  # must be JSON serialisable
        q = DeviceProfile.from_dict(json.loads(json.dumps(d)))
        self.assertEqual(p, q)
        self.assertIsInstance(q.padding, tuple)
        self.assertIsInstance(q.connection, ConnectionSettings)

    def test_presets_round_trip(self):
        for name, p in PRESETS.items():
            self.assertEqual(DeviceProfile.from_dict(p.to_dict()), p, name)

    def test_tolerant(self):
        q = DeviceProfile.from_dict({"name": "Y", "unknown": 5, "width_mm": "abc",
                                     "padding": [1, 2], "connection": {"parity": "even",
                                                                       "flow": "bogus",
                                                                       "bytesize": 9,
                                                                       "extra": 1}})
        self.assertEqual(q.name, "Y")
        self.assertEqual(q.width_mm, 610.0)
        self.assertEqual(q.padding, (0, 0, 0, 0))
        self.assertEqual(q.connection.parity, "E")
        self.assertEqual(q.connection.flow, "none")
        self.assertEqual(q.connection.bytesize, 8)
        self.assertEqual(DeviceProfile.from_dict({}).name, "Unnamed device")
        self.assertEqual(DeviceProfile.from_dict(None).name, "Unnamed device")
        self.assertEqual(ConnectionSettings.from_dict(None), ConnectionSettings())

    def test_required_presets(self):
        for n in ("GCC Expert II 24", "GCC Expert II 52", "Generic HPGL"):
            self.assertIn(n, PRESETS)
        self.assertEqual(PRESETS["GCC Expert II 52"].width_mm, 1320.0)
        from vinylcut import protocols
        for p in PRESETS.values():
            self.assertIn(p.protocol, protocols.PROTOCOLS)

    def test_preset_copy_is_independent(self):
        a = devices.preset("GCC Expert II 24")
        a.protocol_options["x"] = 1
        a.connection.port = "COM9"
        self.assertNotIn("x", PRESETS["GCC Expert II 24"].protocol_options)
        self.assertEqual(PRESETS["GCC Expert II 24"].connection.port, "")


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "sub", "devices.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_gives_presets(self):
        profs = devices.load_profiles(self.path)
        self.assertEqual([p.name for p in profs], list(PRESETS))

    def test_save_load(self):
        p = devices.preset("GCC Expert II 24")
        p.connection.printer_name = "GCC Expert II-24"
        devices.save_profiles([p], selected=p.name, path=self.path)
        back = devices.load_profiles(self.path)
        self.assertEqual(back, [p])
        self.assertEqual(devices.load_selected_name(self.path), p.name)

    def test_corrupt_gives_presets(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("{not json")
        self.assertEqual(len(devices.load_profiles(self.path)), len(PRESETS))

    def test_default_path_uses_appdata(self):
        old = os.environ.get("APPDATA")
        os.environ["APPDATA"] = self.tmp
        try:
            self.assertEqual(devices.profiles_path(), Path(self.tmp) / "vinylcut" / "devices.json")
            devices.save_profiles([devices.preset("Generic HPGL")])
            self.assertEqual(devices.load_profiles()[0].name, "Generic HPGL")
        finally:
            if old is None:
                del os.environ["APPDATA"]
            else:
                os.environ["APPDATA"] = old


def _reduce(typ, state):
    return {"py/reduce": ["py/newobj", {"py/tuple": [{"py/type": typ}]}, state]}


class InkCutSyntheticTests(unittest.TestCase):
    """A hand-built jsonpickle file covering the transports/protocols we map."""

    def _device(self, name, transport, tconfig, proto="hpgl", swap=True, **cfg):
        config = {"step_time": 10.0, "step_size": 3.5433070866, "interpolate": False,
                  "swap_xy": swap, "mirror_x": False, "mirror_y": False,
                  "scale": {"py/object": "atom.catom.atomclist", "py/seq": [1.0, 1.0]},
                  "speed": 10.0, "speed_units": "cm/s", "speed_enabled": True,
                  "force": 120.0, "force_units": "g", "force_enabled": True,
                  "commands_before": "", "commands_after": "",
                  "commands_connect": "", "commands_disconnect": ""}
        config.update(cfg)
        return _reduce("inkcut.device.plugin.Device", {
            "name": name, "manufacturer": "Inkcut", "model": "Generic Driver",
            "area": _reduce("inkcut.core.models.AreaBase", {
                "size": {"py/object": "atom.catom.atomclist", "py/seq": [2126.0, 3543.307]},
                "padding": {"py/object": "atom.catom.atomclist",
                            "py/seq": [35.433, 0.0, 0.0, 70.866]}}),
            "filters": {"py/object": "atom.catom.atomlist", "py/seq": [
                _reduce("inkcut.device.filters.overcut.OvercutFilter", {
                    "config": _reduce("inkcut.device.filters.overcut.OvercutConfig",
                                      {"overcut": 3.5433070866})}),
                _reduce("inkcut.device.filters.blade_offset.BladeOffsetFilter", {
                    "config": _reduce("inkcut.device.filters.blade_offset.BladeOffsetConfig",
                                      {"offset": 0.88582677165, "cutoff": 20.0})})]},
            "connection": _reduce(transport, {
                "declaration": _reduce("inkcut.device.extensions.DeviceTransport", {"id": ""}),
                "config": _reduce(transport + "Config", tconfig),
                "protocol": _reduce("inkcut.device.protocols.x.P", {
                    "declaration": _reduce("inkcut.device.extensions.DeviceProtocol",
                                           {"id": proto}),
                    "config": _reduce("inkcut.device.protocols.hpgl.HPGLConfig", {"pad": True})})}),
            "jobs": {"py/object": "atom.catom.atomlist", "py/seq": [{"py/id": 3}]},
            "config": _reduce("inkcut.device.plugin.DeviceConfig", config)})

    def test_synthetic(self):
        data = {"devices": {"py/object": "atom.catom.atomlist", "py/seq": [
            self._device("Serial", "inkcut.device.transports.serialport.plugin.SerialTransport",
                         {"port": "COM7", "baudrate": 19200, "bytesize": 7, "parity": "Even",
                          "stopbits": 2, "xonxoff": False, "rtscts": True, "dsrdtr": False},
                         commands_before="!PG;"),
            self._device("Qt", "inkcut.device.transports.qtserialport.plugin.QtSerialTransport",
                         {"port": "COM5", "baudrate": 38400, "bytesize": 8, "parity": 3,
                          "stopbits": 3, "flowcontrol": 2}, swap=False, proto="camm-gl1"),
            self._device("Printer", "inkcut.device.transports.printer.plugin.PrinterTransport",
                         {"printer": "GCC Expert II-24"}, mirror_x=True, speed_units="in/s",
                         speed=2.0),
            self._device("Disk", "inkcut.device.transports.disk.plugin.FileTransport",
                         {"directory": "C:\\out", "format": "job-{time}.{protocol}"}, proto="dmpl"),
        ]}, "device": {"py/id": 2}}
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "inkcut.device.json")
            with open(path, "w") as f:
                json.dump(data, f)
            profs = devices.import_inkcut_devices(path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual([p.name for p in profs], ["Serial", "Qt", "Printer", "Disk"])
        s, q, pr, dk = profs
        # area: 2126 px = 600 mm wide, 3543.307 px = 1000 mm long
        self.assertAlmostEqual(s.width_mm, 600.0, places=1)
        self.assertAlmostEqual(s.length_mm, 1000.0, places=1)
        # InkCut padding [left 10mm, top 0, right 0, bottom 20mm] -> ours (left=bottom, bottom=left, ...)
        self.assertAlmostEqual(s.padding[0], 20.0, places=2)
        self.assertAlmostEqual(s.padding[1], 10.0, places=2)
        self.assertAlmostEqual(s.default_overcut, 1.0, places=3)
        self.assertAlmostEqual(s.default_blade_offset, 0.25, places=3)
        self.assertEqual(s.connection.kind, "serial")
        self.assertEqual((s.connection.port, s.connection.baudrate, s.connection.bytesize,
                          s.connection.parity, s.connection.stopbits, s.connection.flow),
                         ("COM7", 19200, 7, "E", 2, "rtscts"))
        self.assertEqual(s.default_speed, 10.0)
        self.assertEqual(s.default_force, 120.0)
        self.assertTrue(s.protocol_options["use_speed_cmd"])
        self.assertTrue(s.protocol_options["use_force_cmd"])
        self.assertTrue(s.protocol_options["newline"])
        self.assertEqual(s.init_cmd, "IN;!PG;")
        self.assertFalse(s.swap_axes)
        # Qt serial, CAMM, no swap -> swap_axes + invert_y
        self.assertEqual(q.protocol, "camm")
        self.assertEqual((q.connection.port, q.connection.baudrate, q.connection.parity,
                          q.connection.stopbits, q.connection.flow),
                         ("COM5", 38400, "O", 1.5, "xonxoff"))
        self.assertTrue(q.swap_axes)
        self.assertTrue(q.invert_y)
        # printer + mirror_x toggles invert_y; speed in/s -> cm/s
        self.assertEqual(pr.connection.kind, "printer")
        self.assertEqual(pr.connection.printer_name, "GCC Expert II-24")
        self.assertTrue(pr.invert_y)
        self.assertAlmostEqual(pr.default_speed, 5.08, places=2)
        # disk
        self.assertEqual(dk.protocol, "dmpl")
        self.assertEqual(dk.connection.kind, "file")
        self.assertTrue(dk.connection.file_path.endswith("job-{time}.dmpl"))
        for p in profs:
            DeviceProfile.from_dict(json.loads(json.dumps(p.to_dict())))

    def test_missing_file(self):
        self.assertEqual(devices.import_inkcut_devices(os.path.join(tempfile.gettempdir(),
                                                                    "nope-vinylcut.json")), [])

    def test_bad_file(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "bad.json")
            with open(path, "w") as f:
                f.write("garbage")
            with self.assertRaises(ValueError):
                devices.import_inkcut_devices(path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


REAL_INKCUT = devices.default_inkcut_path()


@unittest.skipUnless(REAL_INKCUT.exists(), "user's InkCut config not present")
class InkCutRealFileTests(unittest.TestCase):
    def test_gcc_expert_ii(self):
        profs = devices.import_inkcut_devices()
        names = [p.name for p in profs]
        self.assertIn("GCC Expert II", names)
        g = profs[names.index("GCC Expert II")]
        self.assertEqual(g.protocol, "hpgl")
        self.assertEqual(g.connection.kind, "serial")
        self.assertEqual(g.connection.port, "COM3")
        self.assertEqual(g.connection.baudrate, 9600)
        self.assertEqual((g.connection.bytesize, g.connection.parity, g.connection.stopbits),
                         (8, "N", 1))
        self.assertEqual(g.connection.flow, "none")
        self.assertAlmostEqual(g.width_mm, 508.0, places=1)       # 1800 px / (90/25.4)
        self.assertEqual(g.length_mm, 25000.0)                     # InkCut "unlimited"
        for v in g.padding:
            self.assertAlmostEqual(v, 2.822, places=2)             # 10 px
        self.assertAlmostEqual(g.default_speed, 2.79, places=2)    # 1.1 in/s
        self.assertEqual(g.default_force, 40.0)
        self.assertEqual(g.steps_per_mm, 40.0)
        self.assertFalse(g.swap_axes)                              # InkCut swap_xy=True
        self.assertTrue(g.protocol_options["use_speed_cmd"])
        self.assertFalse(g.protocol_options["use_force_cmd"])
        self.assertIn("InkCut", g.notes)


if __name__ == "__main__":
    unittest.main()
