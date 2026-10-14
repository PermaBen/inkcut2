"""Device profiles, presets, persistence and InkCut device import.

Coordinate conventions (see docs/ARCHITECTURE.md):

* Material coords: mm, x = along the media feed (length), y = across the media
  (width), origin at the cutter origin corner, y up when previewed.
* Device coords: integer steps.  The only material -> device conversion lives in
  :meth:`DeviceProfile.to_device`.

For a GCC Expert II (and practically every roll-fed HPGL cutter) the HPGL X axis
is the media-feed axis and Y runs along the carriage, with the origin at the
front-right pinch roller ("The origin point is on the bottom right", Expert II
manual).  Material coords therefore map 1:1 onto GCC device coords:
``swap_axes=False, invert_x=False, invert_y=False``.  See docs/research.md.
"""
from __future__ import annotations

import copy
import json
import math
import os
import tempfile
from dataclasses import dataclass, field, fields, asdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Choices for the GUI
# ---------------------------------------------------------------------------
CONNECTION_KINDS = [
    ("printer", "USB / Windows printer (raw spooler)"),
    ("serial", "Serial port (COM / USB-serial)"),
    ("file", "Save to file (.plt)"),
    ("network", "Network (TCP socket)"),
]
BAUD_RATES = [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200]
BYTESIZES = [8, 7, 6, 5]
PARITIES = [("N", "None"), ("E", "Even"), ("O", "Odd"), ("M", "Mark"), ("S", "Space")]
STOPBITS = [1, 1.5, 2]
FLOW_CONTROLS = [
    ("rtscts", "Hardware (RTS/CTS)"),
    ("xonxoff", "Software (XON/XOFF)"),
    ("dsrdtr", "Hardware (DSR/DTR)"),
    ("none", "None"),
]

#: InkCut internal unit: 1 px = 1/90 inch  (inkcut/core/svg.py ``_uuconv``)
INKCUT_PX_PER_MM = 3.5433070866
#: InkCut uses this as "no length limit" for roll media (inkcut/device/plugin.py)
INKCUT_UNLIMITED_LENGTH_PX = 900000


def _to_float(v, default):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return f


def _to_int(v, default):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _to_bool(v, default=False):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off", ""):
            return False
    return default


def _to_tuple(v, n, default):
    try:
        t = tuple(float(x) for x in v)
    except (TypeError, ValueError):
        return tuple(default)
    if len(t) != n:
        return tuple(default)
    return t


# ---------------------------------------------------------------------------
# Connection settings
# ---------------------------------------------------------------------------
@dataclass
class ConnectionSettings:
    kind: str = "serial"             # "serial" | "printer" | "file" | "network"
    port: str = ""                   # e.g. "COM4" (pyserial URLs such as "loop://" also work)
    baudrate: int = 9600
    bytesize: int = 8                # 5,6,7,8
    parity: str = "N"                # "N","E","O","M","S"
    stopbits: float = 1              # 1, 1.5, 2
    flow: str = "rtscts"             # "none" | "rtscts" | "xonxoff" | "dsrdtr"
    printer_name: str = ""
    file_path: str = ""              # "{time}" is replaced by a timestamp when writing
    host: str = ""
    net_port: int = 9100
    chunk_size: int = 512            # bytes per write
    chunk_delay_ms: int = 0
    # --- additions (not in the original contract) ---
    timeout_s: float = 120.0         # give up if the device accepts no data for this long

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d) -> "ConnectionSettings":
        c = cls()
        if not isinstance(d, dict):
            return c
        kind = str(d.get("kind", c.kind)).lower()
        c.kind = kind if kind in ("serial", "printer", "file", "network") else c.kind
        c.port = str(d.get("port", c.port) or "")
        c.baudrate = _to_int(d.get("baudrate"), c.baudrate) or c.baudrate
        bs = _to_int(d.get("bytesize"), c.bytesize)
        c.bytesize = bs if bs in (5, 6, 7, 8) else 8
        par = str(d.get("parity", c.parity) or "N").strip().upper()[:1]
        c.parity = par if par in ("N", "E", "O", "M", "S") else "N"
        sb = _to_float(d.get("stopbits"), c.stopbits)
        c.stopbits = sb if sb in (1, 1.5, 2) else 1
        if c.stopbits in (1, 2):
            c.stopbits = int(c.stopbits)
        flow = str(d.get("flow", c.flow) or "none").lower()
        c.flow = flow if flow in ("none", "rtscts", "xonxoff", "dsrdtr") else "none"
        c.printer_name = str(d.get("printer_name", "") or "")
        c.file_path = str(d.get("file_path", "") or "")
        c.host = str(d.get("host", "") or "")
        c.net_port = _to_int(d.get("net_port"), c.net_port)
        c.chunk_size = max(1, _to_int(d.get("chunk_size"), c.chunk_size))
        c.chunk_delay_ms = max(0, _to_int(d.get("chunk_delay_ms"), c.chunk_delay_ms))
        c.timeout_s = max(0.1, _to_float(d.get("timeout_s"), c.timeout_s))
        return c

    def describe(self) -> str:
        """Short human readable description, e.g. 'COM4 9600 8N1 RTS/CTS'."""
        if self.kind == "serial":
            sb = ("%g" % self.stopbits)
            flow = dict(FLOW_CONTROLS).get(self.flow, self.flow)
            return "%s %d %d%s%s %s" % (self.port or "(no port)", self.baudrate, self.bytesize,
                                        self.parity, sb, flow)
        if self.kind == "printer":
            return "Printer '%s'" % (self.printer_name or "(none)")
        if self.kind == "file":
            return "File %s" % (self.file_path or "(none)")
        if self.kind == "network":
            return "TCP %s:%d" % (self.host or "(no host)", self.net_port)
        return self.kind


# ---------------------------------------------------------------------------
# Device profile
# ---------------------------------------------------------------------------
@dataclass
class DeviceProfile:
    name: str
    protocol: str = "hpgl"           # key into protocols.PROTOCOLS
    width_mm: float = 610.0          # across feed (cuttable width)
    length_mm: float = 5000.0        # along feed
    padding: tuple = (0, 0, 0, 0)    # mm: left(x min), bottom(y min), right, top
    steps_per_mm: float = 40.0
    swap_axes: bool = False
    invert_x: bool = False
    invert_y: bool = False
    speed_range: tuple = (1, 60)     # cm/s
    force_range: tuple = (5, 250)    # grams
    default_speed: float = 30
    default_force: float = 80
    default_overcut: float = 0.5
    default_blade_offset: float = 0.25
    init_cmd: str = ""               # optional raw prefix (overrides protocol default when non-empty)
    finish_cmd: str = ""
    protocol_options: dict = field(default_factory=dict)
    connection: ConnectionSettings = field(default_factory=ConnectionSettings)
    # --- additions (not in the original contract) ---
    notes: str = ""                  # free text (e.g. import remarks)

    # -- geometry ---------------------------------------------------------
    def to_device_mm(self, x_mm: float, y_mm: float) -> tuple:
        """Material mm -> device axes in mm (swap/invert applied, not scaled)."""
        x = float(x_mm)
        y = float(y_mm)
        if self.invert_x:
            x = self.length_mm - x
        if self.invert_y:
            y = self.width_mm - y
        if self.swap_axes:
            x, y = y, x
        return x, y

    def to_device(self, x_mm, y_mm) -> tuple:
        """Material mm -> integer device steps (round half away from zero)."""
        x, y = self.to_device_mm(x_mm, y_mm)
        s = self.steps_per_mm
        return _round(x * s), _round(y * s)

    def from_device(self, x_steps, y_steps) -> tuple:
        """Inverse of :meth:`to_device` (device steps -> material mm)."""
        s = self.steps_per_mm or 1.0
        x, y = x_steps / s, y_steps / s
        if self.swap_axes:
            x, y = y, x
        if self.invert_y:
            y = self.width_mm - y
        if self.invert_x:
            x = self.length_mm - x
        return x, y

    @property
    def usable_area(self) -> tuple:
        """(x0, y0, x1, y1) in material mm inside the padding."""
        l, b, r, t = self.padding
        return (l, b, self.length_mm - r, self.width_mm - t)

    def clamp_speed(self, v) -> float:
        lo, hi = self.speed_range
        return min(max(float(v), lo), hi)

    def clamp_force(self, v) -> float:
        lo, hi = self.force_range
        return min(max(float(v), lo), hi)

    # -- serialisation ----------------------------------------------------
    def to_dict(self) -> dict:
        d = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if f.name == "connection":
                v = v.to_dict()
            elif isinstance(v, tuple):
                v = list(v)
            elif isinstance(v, dict):
                v = copy.deepcopy(v)
            d[f.name] = v
        return d

    @classmethod
    def from_dict(cls, d) -> "DeviceProfile":
        if not isinstance(d, dict):
            d = {}
        p = cls(name=str(d.get("name") or "Unnamed device"))
        p.protocol = str(d.get("protocol") or p.protocol).lower()
        p.width_mm = _to_float(d.get("width_mm"), p.width_mm)
        p.length_mm = _to_float(d.get("length_mm"), p.length_mm)
        p.padding = _to_tuple(d.get("padding", p.padding), 4, p.padding)
        p.steps_per_mm = _to_float(d.get("steps_per_mm"), p.steps_per_mm) or 40.0
        p.swap_axes = _to_bool(d.get("swap_axes"), p.swap_axes)
        p.invert_x = _to_bool(d.get("invert_x"), p.invert_x)
        p.invert_y = _to_bool(d.get("invert_y"), p.invert_y)
        p.speed_range = _to_tuple(d.get("speed_range", p.speed_range), 2, p.speed_range)
        p.force_range = _to_tuple(d.get("force_range", p.force_range), 2, p.force_range)
        p.default_speed = _to_float(d.get("default_speed"), p.default_speed)
        p.default_force = _to_float(d.get("default_force"), p.default_force)
        p.default_overcut = _to_float(d.get("default_overcut"), p.default_overcut)
        p.default_blade_offset = _to_float(d.get("default_blade_offset"), p.default_blade_offset)
        p.init_cmd = str(d.get("init_cmd") or "")
        p.finish_cmd = str(d.get("finish_cmd") or "")
        po = d.get("protocol_options")
        p.protocol_options = copy.deepcopy(po) if isinstance(po, dict) else {}
        p.connection = ConnectionSettings.from_dict(d.get("connection"))
        p.notes = str(d.get("notes") or "")
        return p

    def copy(self, **changes) -> "DeviceProfile":
        p = copy.deepcopy(self)
        for k, v in changes.items():
            setattr(p, k, v)
        return p


def _round(v: float) -> int:
    return int(math.floor(v + 0.5)) if v >= 0 else -int(math.floor(-v + 0.5))


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------
def _gcc(name, width, speed_max):
    # Expert II manual (V.10 2018): cut width 600 / 1320 mm, force up to 350 g,
    # speed 705 / 635 mm/s, software resolution 0.025 mm (= 40 steps/mm),
    # driver defaults 30 cm/s, 80 g, 0.25 mm offset.  The cutter performs blade
    # offset compensation itself (VLCD "Blade Offset", default 0.25 mm), so the
    # software blade offset defaults to 0 to avoid compensating twice.
    return DeviceProfile(
        name=name,
        protocol="hpgl",
        width_mm=width,
        length_mm=25000.0,           # VLCD "Poll Size": max plotting length 25000 mm
        padding=(0, 0, 0, 0),
        steps_per_mm=40.0,
        swap_axes=False, invert_x=False, invert_y=False,
        speed_range=(1, speed_max),
        force_range=(5, 350),
        default_speed=30,
        default_force=80,
        default_overcut=0.5,
        default_blade_offset=0.0,
        protocol_options={"use_speed_cmd": True, "use_force_cmd": True, "return_home": True},
        connection=ConnectionSettings(kind="printer", port="", baudrate=9600, bytesize=8,
                                      parity="N", stopbits=1, flow="rtscts",
                                      chunk_size=512, chunk_delay_ms=0),
        notes=("Connect by USB with the GCC driver installed (cutter in 'Common USB mode'): it "
               "appears as a Windows printer; choose it under Connection. RS-232 alternative: "
               "9600 8N1, hardware (RTS/CTS) handshake. The cutter applies its own blade offset "
               "(VLCD), so software blade offset is 0 by default."),
    )


def _build_presets() -> dict:
    presets = [
        _gcc("GCC Expert II 24", 600.0, 70),
        _gcc("GCC Expert II 52", 1320.0, 63),
        DeviceProfile(
            name="Generic HPGL",
            protocol="hpgl",
            width_mm=610.0, length_mm=5000.0,
            steps_per_mm=40.0,
            speed_range=(1, 60), force_range=(5, 250),
            default_speed=30, default_force=80,
            default_overcut=0.5, default_blade_offset=0.25,
            protocol_options={"use_speed_cmd": False, "use_force_cmd": False, "return_home": True},
            connection=ConnectionSettings(kind="serial", flow="rtscts"),
            notes="Plain HP-GL (IN/PU/PD), 40 steps/mm. Enable VS/FS in protocol options if your cutter honours them.",
        ),
        DeviceProfile(
            name="Generic DMPL",
            protocol="dmpl",
            width_mm=610.0, length_mm=5000.0,
            steps_per_mm=39.37,      # DMPL EC1 = 0.001 inch
            protocol_options={"mode": 1, "use_speed_cmd": False, "use_force_cmd": False},
            connection=ConnectionSettings(kind="serial", flow="rtscts"),
            notes="Houston Instruments DMPL as emitted by InkCut (mode 1, EC1 = 0.001 in units).",
        ),
        DeviceProfile(
            name="Generic GPGL (Graphtec)",
            protocol="gpgl",
            width_mm=600.0, length_mm=5000.0,
            steps_per_mm=10.0,       # Graphtec GP-GL default step 0.1 mm (configurable on the cutter)
            swap_axes=True, invert_y=True,
            speed_range=(1, 60), force_range=(1, 38),
            default_speed=30, default_force=14,
            protocol_options={"use_speed_cmd": False, "use_force_cmd": False},
            connection=ConnectionSettings(kind="serial", flow="rtscts"),
            notes="Graphtec GP-GL. Force is a cutter force LEVEL (FX), not grams. Check the GP-GL step size set on the cutter (0.1 mm = 10 steps/mm).",
        ),
        DeviceProfile(
            name="Generic CAMM-GL (Roland)",
            protocol="camm",
            width_mm=584.0, length_mm=5000.0,
            steps_per_mm=40.0,       # CAMM-GL I: 0.025 mm/step
            speed_range=(1, 50), force_range=(30, 250),
            protocol_options={"use_speed_cmd": True, "use_force_cmd": True},
            connection=ConnectionSettings(kind="serial", flow="rtscts"),
            notes="Roland CAMM-GL I mode 2 style (IN; VS; FS; M/D moves) as emitted by InkCut.",
        ),
        DeviceProfile(
            name="Generic G-code",
            protocol="gcode",
            width_mm=300.0, length_mm=300.0,
            steps_per_mm=40.0,       # unused: G-code coordinates are written in mm
            default_overcut=0.0, default_blade_offset=0.0,
            protocol_options={"use_builtin": True, "lift_mode": "implicit", "precision": 3},
            connection=ConnectionSettings(kind="serial", baudrate=115200, flow="none"),
            notes="G-code for drag-knife CNC/plotters. Coordinates in mm.",
        ),
        DeviceProfile(
            name="HPGL file (.plt)",
            protocol="hpgl",
            width_mm=610.0, length_mm=5000.0,
            steps_per_mm=40.0,
            protocol_options={"use_speed_cmd": False, "use_force_cmd": False, "return_home": True},
            connection=ConnectionSettings(
                kind="file",
                file_path=os.path.join(os.path.expanduser("~"), "Documents", "inkcut2-{time}.plt")),
            notes="Writes an HP-GL file instead of sending to a cutter.",
        ),
    ]
    return {p.name: p for p in presets}


PRESETS: dict = _build_presets()


def preset(name: str) -> DeviceProfile:
    """Return a fresh deep copy of a preset (KeyError if unknown)."""
    return copy.deepcopy(PRESETS[name])


# ---------------------------------------------------------------------------
# Persistence: %APPDATA%\inkcut2\devices.json
# ---------------------------------------------------------------------------
def config_dir() -> Path:
    # Same resolution as job.config_dir() so all settings share one folder.
    override = os.environ.get("INKCUT2_CONFIG_DIR")
    if override:
        return Path(override)
    base = os.environ.get("APPDATA")
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "inkcut2"


def profiles_path() -> Path:
    return config_dir() / "devices.json"


def _read_store(path=None) -> dict:
    p = Path(path) if path else profiles_path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if isinstance(data, list):           # tolerate a bare list of profiles
        data = {"profiles": data}
    return data if isinstance(data, dict) else {}


def load_profiles(path=None) -> list:
    """Profiles from %APPDATA%\\inkcut2\\devices.json, else copies of the presets."""
    data = _read_store(path)
    items = data.get("profiles")
    profiles = []
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                try:
                    profiles.append(DeviceProfile.from_dict(item))
                except Exception:   # never let one bad entry break loading
                    continue
    if not profiles:
        profiles = [copy.deepcopy(p) for p in PRESETS.values()]
    return profiles


def load_selected_name(path=None):
    """Name of the profile that was selected when last saved (or None)."""
    sel = _read_store(path).get("selected")
    return sel if isinstance(sel, str) else None


def save_profiles(profiles, selected=None, path=None) -> None:
    """Atomically write profiles (and optionally the selected profile name)."""
    p = Path(path) if path else profiles_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"version": 1,
            "profiles": [pr.to_dict() for pr in profiles]}
    if selected is None:
        old = load_selected_name(p)
        if old:
            data["selected"] = old
    else:
        data["selected"] = selected
    fd, tmp = tempfile.mkstemp(prefix="devices-", suffix=".json", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# InkCut import (jsonpickle format)
# ---------------------------------------------------------------------------
def default_inkcut_path() -> Path:
    return Path(os.path.expanduser("~")) / ".config" / "inkcut" / "inkcut.device.json"


def _unpickle(o):
    """Turn jsonpickle structures into plain dicts/lists.

    Objects become dicts with their state plus ``"__type__"`` (dotted class name).
    ``py/id`` back-references are returned as ``{"__ref__": n}`` (not resolved).
    """
    if isinstance(o, list):
        return [_unpickle(x) for x in o]
    if not isinstance(o, dict):
        return o
    if "py/reduce" in o:
        red = o["py/reduce"]
        typ = ""
        state = {}
        if isinstance(red, list):
            if len(red) > 1:
                args = red[1]
                if isinstance(args, dict):
                    tup = args.get("py/tuple")
                    if isinstance(tup, list) and tup and isinstance(tup[0], dict):
                        typ = tup[0].get("py/type", "") or ""
            if len(red) > 2:
                state = _unpickle(red[2])
        out = dict(state) if isinstance(state, dict) else {"__state__": state}
        out["__type__"] = typ
        return out
    if "py/object" in o:
        typ = o.get("py/object", "")
        if "py/seq" in o:
            return [_unpickle(x) for x in o["py/seq"]]
        if "py/state" in o:
            st = _unpickle(o["py/state"])
            out = dict(st) if isinstance(st, dict) else {"__state__": st}
        else:
            out = {k: _unpickle(v) for k, v in o.items() if not k.startswith("py/")}
        out["__type__"] = typ
        return out
    if "py/tuple" in o:
        return [_unpickle(x) for x in o["py/tuple"]]
    if "py/seq" in o:
        return [_unpickle(x) for x in o["py/seq"]]
    if "py/set" in o:
        return [_unpickle(x) for x in o["py/set"]]
    if "py/id" in o:
        return {"__ref__": o["py/id"]}
    if "py/type" in o:
        return {"__type__": o["py/type"], "__class_ref__": True}
    if "py/function" in o:
        return {"__function__": o["py/function"]}
    return {k: _unpickle(v) for k, v in o.items()}


def _obj(d, key):
    v = d.get(key) if isinstance(d, dict) else None
    return v if isinstance(v, dict) else {}


def _list(d, key):
    v = d.get(key) if isinstance(d, dict) else None
    return v if isinstance(v, list) else []


_QT_PARITY = {0: "N", 2: "E", 3: "O", 4: "S", 5: "M"}       # QSerialPort::Parity
_QT_STOPBITS = {1: 1, 2: 2, 3: 1.5}                         # QSerialPort::StopBits
_QT_FLOW = {0: "none", 1: "rtscts", 2: "xonxoff"}           # QSerialPort::FlowControl
_PYSERIAL_PARITY = {"none": "N", "even": "E", "odd": "O", "mark": "M", "space": "S"}

_PROTOCOL_MAP = {"hpgl": "hpgl", "dmpl": "dmpl", "gpgl": "gpgl", "camm-gl1": "camm",
                 "camm": "camm", "gcode": "gcode", "debug": "hpgl"}


def _px_to_mm(v, default=0.0):
    return round(_to_float(v, default * INKCUT_PX_PER_MM) / INKCUT_PX_PER_MM, 3)


def _convert_connection(conn: dict, protocol_ext: str) -> ConnectionSettings:
    c = ConnectionSettings()
    typ = str(conn.get("__type__", ""))
    decl = _obj(conn, "declaration")
    tid = str(decl.get("id", "") or "")
    cfg = _obj(conn, "config")
    if tid == "qtserial" or "qtserialport" in typ:
        c.kind = "serial"
        c.port = str(cfg.get("port", "") or "")
        c.baudrate = _to_int(cfg.get("baudrate"), 9600)
        c.bytesize = _to_int(cfg.get("bytesize"), 8)
        c.parity = _QT_PARITY.get(_to_int(cfg.get("parity"), 0), "N")
        c.stopbits = _QT_STOPBITS.get(_to_int(cfg.get("stopbits"), 1), 1)
        c.flow = _QT_FLOW.get(_to_int(cfg.get("flowcontrol"), 0), "none")
    elif tid == "serial" or "serialport" in typ:
        c.kind = "serial"
        c.port = str(cfg.get("port", "") or "")
        c.baudrate = _to_int(cfg.get("baudrate"), 9600)
        c.bytesize = _to_int(cfg.get("bytesize"), 8)
        c.parity = _PYSERIAL_PARITY.get(str(cfg.get("parity", "None")).lower(), "N")
        sb = _to_float(cfg.get("stopbits"), 1)
        c.stopbits = sb if sb in (1.5,) else int(sb) if sb in (1, 2) else 1
        if _to_bool(cfg.get("rtscts")):
            c.flow = "rtscts"
        elif _to_bool(cfg.get("xonxoff")):
            c.flow = "xonxoff"
        elif _to_bool(cfg.get("dsrdtr")):
            c.flow = "dsrdtr"
        else:
            c.flow = "none"
    elif tid == "printer" or "printer" in typ:
        c.kind = "printer"
        c.printer_name = str(cfg.get("printer", "") or "")
    elif tid == "disk" or ".disk." in typ:
        c.kind = "file"
        directory = str(cfg.get("directory", "") or "") or tempfile.gettempdir()
        fmt = str(cfg.get("format", "") or "inkcut-{time}.{protocol}")
        fmt = fmt.replace("{protocol}", protocol_ext)
        c.file_path = os.path.join(directory, fmt)
    elif tid in ("parallel", "raw") or "parallelport" in typ or ".raw." in typ:
        c.kind = "file"
        c.file_path = str(cfg.get("device_path", "") or "")
    return c


def _convert_inkcut_device(dev: dict) -> DeviceProfile:
    name = str(dev.get("name") or "InkCut device")
    cfg = _obj(dev, "config")
    area = _obj(dev, "area")
    conn = _obj(dev, "connection")
    proto = _obj(conn, "protocol")
    proto_decl = _obj(proto, "declaration")
    proto_cfg = _obj(proto, "config")
    pid = str(proto_decl.get("id", "") or "").lower()
    if not pid:
        t = str(proto.get("__type__", "")).lower()
        for key in ("hpgl", "dmpl", "gpgl", "camm", "gcode"):
            if key in t:
                pid = key
                break
    protocol = _PROTOCOL_MAP.get(pid, "hpgl")
    notes = []

    p = DeviceProfile(name=name, protocol=protocol)

    # --- area: InkCut size = [width (across), length (along feed)] in px,
    #     padding = [left, top, right, bottom] in px (InkCut material frame,
    #     x across the width, y along the feed).
    size = area.get("size") if isinstance(area.get("size"), list) else []
    w_px = _to_float(size[0], 0) if len(size) > 0 else 0
    h_px = _to_float(size[1], 0) if len(size) > 1 else 0
    if w_px > 0:
        p.width_mm = round(w_px / INKCUT_PX_PER_MM, 2)
    else:
        notes.append("InkCut width was 0 (unset); using default %.0f mm." % p.width_mm)
    if h_px > 0 and h_px < INKCUT_UNLIMITED_LENGTH_PX:
        p.length_mm = round(h_px / INKCUT_PX_PER_MM, 2)
    else:
        p.length_mm = 25000.0 if "gcc" in name.lower() else 50000.0
    pad = area.get("padding") if isinstance(area.get("padding"), list) else []
    if len(pad) == 4:
        il, it, ir, ib = (_px_to_mm(v) for v in pad)
        # ours: (left = x min = feed start, bottom = y min, right = x max, top = y max)
        p.padding = (ib, il, it, ir)

    # --- axes.  InkCut (no swap): device X = across the media width.
    #     InkCut swap_xy rotates by 90 deg so device X = along the feed, which
    #     is our native material x.  Without swap we need swap_axes + invert_y to
    #     stay mirror-free (proper rotation).  mirror_x/mirror_y in InkCut negate
    #     its x (across = our y) / y (along = our x).
    swap = _to_bool(cfg.get("swap_xy"))
    if swap:
        p.swap_axes, p.invert_x, p.invert_y = False, False, False
    else:
        p.swap_axes, p.invert_x, p.invert_y = True, False, True
    if _to_bool(cfg.get("mirror_x")):
        p.invert_y = not p.invert_y
    if _to_bool(cfg.get("mirror_y")):
        p.invert_x = not p.invert_x
    rot = _to_int(cfg.get("rotation"), 0)
    if rot:
        notes.append("InkCut output rotation %d deg was only used for its preview; ignored." % rot)

    # --- resolution.  InkCut HPGL/DMPL scale 1021/90 units per px (= 40.197/mm,
    #     0.5 %% larger than true HPGL 40/mm); we use the correct 40/mm.  GPGL/CAMM
    #     are sent in raw px (1/90 in) by InkCut, so keep that faithfully.
    scale = cfg.get("scale") if isinstance(cfg.get("scale"), list) else [1, 1]
    sx = abs(_to_float(scale[0], 1)) if len(scale) > 0 else 1.0
    sy = abs(_to_float(scale[1], 1)) if len(scale) > 1 else 1.0
    s = (sx + sy) / 2.0 or 1.0
    if abs(sx - sy) > 1e-6:
        notes.append("InkCut scale differed per axis (%g, %g); averaged to %g." % (sx, sy, s))
    if protocol in ("hpgl", "dmpl"):
        base = 40.0 if protocol == "hpgl" else 1021.0 / 90.0 * INKCUT_PX_PER_MM
        p.steps_per_mm = round(base * s, 6)
    elif protocol == "gcode":
        p.steps_per_mm = 40.0           # unused by G-code (written in mm)
        notes.append("InkCut G-code coordinates were in px (1/90 in); InkCut2 writes mm.")
    else:
        p.steps_per_mm = round(INKCUT_PX_PER_MM * s, 6)

    # --- speed / force
    speed = _to_float(cfg.get("speed"), 4.0)
    units = str(cfg.get("speed_units", "in/s") or "in/s")
    speed_cms = speed * 2.54 if units.startswith("in") else speed
    p.default_speed = round(speed_cms, 2)
    p.default_force = _to_float(cfg.get("force"), 40.0)
    speed_enabled = _to_bool(cfg.get("speed_enabled"))
    force_enabled = _to_bool(cfg.get("force_enabled"))
    if speed_enabled and protocol in ("hpgl", "camm", "dmpl", "gpgl"):
        notes.append("InkCut sent the raw speed value %s (%s) as an integer (e.g. VS%d); InkCut2 "
                     "sends cm/s, so the default speed is %.2f cm/s - verify on the cutter."
                     % (speed, units, int(speed), p.default_speed))
    lo, hi = p.speed_range
    p.speed_range = (min(lo, p.default_speed), max(hi, p.default_speed))
    lo, hi = p.force_range
    p.force_range = (min(lo, p.default_force), max(hi, p.default_force))
    if "gcc" in name.lower() and "expert" in name.lower():
        p.speed_range = (min(1, p.speed_range[0]), max(70, p.speed_range[1]))
        p.force_range = (min(5, p.force_range[0]), max(350, p.force_range[1]))

    opts = {}
    if protocol in ("hpgl", "camm", "dmpl", "gpgl"):
        opts["use_speed_cmd"] = speed_enabled
        opts["use_force_cmd"] = force_enabled
    if protocol == "hpgl":
        opts["return_home"] = True
        if _to_bool(proto_cfg.get("pad")):
            opts["newline"] = True
    elif protocol == "dmpl":
        mode = _to_int(proto_cfg.get("mode"), 1)
        opts["mode"] = mode if mode in (1, 2, 3, 4, 6) else 1
    elif protocol == "gcode":
        opts["use_builtin"] = _to_bool(proto_cfg.get("use_builtin"), True)
        lm = _to_int(proto_cfg.get("lift_mode"), 0)
        opts["lift_mode"] = {0: "implicit", 1: "custom", 2: "z"}.get(lm, "implicit")
        opts["precision"] = _to_int(proto_cfg.get("precision"), 0)
        opts["lower_z"] = _to_float(proto_cfg.get("lower_z"), 0.0)
        opts["upper_z"] = _to_float(proto_cfg.get("upper_z"), 1.0)
        opts["lift_gcode"] = str(proto_cfg.get("lift_gcode", "") or "")
        opts["lower_gcode"] = str(proto_cfg.get("lower_gcode", "") or "")
        opts["scale"] = round(s, 6)
    p.protocol_options = opts

    # --- filters (overcut / blade offset), values stored in px.  Without a
    #     filter InkCut applied none, so the faithful default is 0.
    p.default_overcut = 0.0
    p.default_blade_offset = 0.0
    for f in _list(dev, "filters"):
        if not isinstance(f, dict):
            continue
        t = str(f.get("__type__", "")).lower()
        fc = _obj(f, "config")
        if "overcut" in t:
            p.default_overcut = _px_to_mm(fc.get("overcut"))
        elif "blade" in t:
            p.default_blade_offset = _px_to_mm(fc.get("offset"))
            p.protocol_options["blade_offset_cutoff_deg"] = _to_float(fc.get("cutoff"), 5.0)
        elif "repeat" in t:
            notes.append("InkCut repeat filter (%s passes) -> set passes in the job." % fc.get("steps"))

    # --- custom commands
    before = "%s%s" % (cfg.get("commands_connect", "") or "", cfg.get("commands_before", "") or "")
    after = "%s%s" % (cfg.get("commands_after", "") or "", cfg.get("commands_disconnect", "") or "")
    if before:
        p.init_cmd = ("IN;" if protocol == "hpgl" else "") + before
    if after:
        p.finish_cmd = ("PU0,0;IN;" if protocol == "hpgl" else "") + after

    # --- connection
    ext = {"hpgl": "hpgl", "dmpl": "dmpl", "gpgl": "gpgl", "camm": "camm-gl1",
           "gcode": "gcode"}.get(protocol, protocol)
    p.connection = _convert_connection(conn, ext)

    # --- pacing (InkCut "interpolate": 1 step of step_size px every step_time ms)
    if _to_bool(cfg.get("interpolate")) and not _to_bool(cfg.get("spooled")):
        step_mm = _to_float(cfg.get("step_size"), INKCUT_PX_PER_MM) / INKCUT_PX_PER_MM
        step_ms = _to_float(cfg.get("step_time"), 0)
        if step_ms > 0:
            notes.append("InkCut streamed in %.3g mm steps every %g ms (~%.1f mm/s) to avoid "
                         "buffer overflow; InkCut2 relies on handshake/chunking instead."
                         % (step_mm, step_ms, step_mm * 1000.0 / step_ms))
    if p.connection.kind == "serial" and p.connection.flow == "none":
        notes.append("Serial flow control was 'none'; most cutters (GCC included) expect "
                     "hardware RTS/CTS handshake.")
    if p.connection.kind == "serial" and p.connection.port.upper() == "COM3" and \
            "gcc" in name.lower():
        notes.append("COM3 on this PC may not be the cutter (check Device Manager).")
    notes.insert(0, "Imported from InkCut (%s %s)." % (dev.get("manufacturer", "") or "",
                                                       dev.get("model", "") or ""))
    p.notes = " ".join(n.strip() for n in notes if n)
    return p


def import_inkcut_devices(path=None) -> list:
    """Parse InkCut's ``inkcut.device.json`` and return :class:`DeviceProfile` s.

    Returns an empty list if the file does not exist; raises ``ValueError`` with
    a readable message if it cannot be parsed.
    """
    p = Path(path) if path else default_inkcut_path()
    if not p.exists():
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError) as e:
        raise ValueError("Could not read InkCut device file %s: %s" % (p, e))
    data = _unpickle(raw)
    devices = []
    if isinstance(data, dict):
        devs = data.get("devices")
        if isinstance(devs, list):
            devices = devs
        elif isinstance(data.get("device"), dict) and data["device"].get("name"):
            devices = [data["device"]]
    elif isinstance(data, list):
        devices = data
    out = []
    seen = {}
    for dev in devices:
        if not isinstance(dev, dict) or "__ref__" in dev:
            continue
        if "name" not in dev and "config" not in dev:
            continue
        try:
            prof = _convert_inkcut_device(dev)
        except Exception as e:  # keep importing the others
            prof = DeviceProfile(name=str(dev.get("name") or "InkCut device"),
                                 notes="Import failed: %s" % e)
        # unique names
        base = prof.name
        n = seen.get(base, 0)
        if n:
            prof.name = "%s (%d)" % (base, n + 1)
        seen[base] = n + 1
        out.append(prof)
    return out
