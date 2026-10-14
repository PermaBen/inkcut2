"""Cutter command languages: HPGL (GCC Expert II), DMPL, GPGL, CAMM-GL, G-code.

Input paths are polylines in **material mm**, already ordered with passes
expanded.  Each protocol converts with ``device.to_device`` (integer steps),
drops zero-length duplicates after rounding, and emits init / moves / finish.

``device.protocol_options`` (all optional):

=================  =========================================================
use_speed_cmd      bool - emit the speed command (HPGL ``VS<cm/s>;``)
use_force_cmd      bool - emit the force command (HPGL ``FS<grams>;``)
return_home        bool - pen-up move to (0, 0) at the end (default True)
feed_after_mm      float - instead of returning home, park this far past the
                   end of the job along the feed (x) so it can be cut off
pen                int - emit ``SP<pen>;`` after init (HPGL/CAMM)
newline            bool - append "\\n" after every command (InkCut "pad")
max_points         int - max coordinate pairs per PD command (default 64)
clamp_negative     bool - clamp negative device coords to 0 (default True)
speed_cmd          str - template overriding the speed command, "{speed}"
force_cmd          str - template overriding the force command, "{force}"
mode               DMPL mode 1/2/3/4/6 (InkCut DMPLConfig)
terminator         GPGL command terminator (default "\\x03" ETX)
use_builtin, lift_mode ("implicit"|"z"|"custom"), precision, lower_z,
upper_z, lift_gcode, lower_gcode, scale                 (G-code)
=================  =========================================================

``device.init_cmd`` (when non-empty) replaces the protocol's reset/init string
(e.g. ``IN;``); speed/force commands are still added per the options above.
``device.finish_cmd`` (when non-empty) replaces the default finish (return
home + re-init); a pen-up is always emitted first.  Both may contain
``{speed}`` / ``{force}`` placeholders.
"""
from __future__ import annotations

import math


def _round_int(v: float) -> int:
    return int(math.floor(v + 0.5))


def _sub(template: str, speed, force) -> str:
    return (template.replace("{speed}", "%d" % _round_int(speed))
                    .replace("{force}", "%d" % _round_int(force)))


def _opt(device, key, default):
    opts = getattr(device, "protocol_options", None) or {}
    v = opts.get(key, default)
    return default if v is None else v


def _bool_opt(device, key, default):
    v = _opt(device, key, default)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def device_polylines(paths, device, clamp_negative=True):
    """Convert material-mm polylines into lists of integer device points.

    Consecutive duplicates (after rounding) are removed; polylines that collapse
    to a single point are dropped.
    """
    out = []
    for poly in paths or ():
        pts = []
        last = None
        for pt in poly:
            x, y = device.to_device(pt[0], pt[1])
            if clamp_negative:
                x = max(0, x)
                y = max(0, y)
            p = (x, y)
            if p != last:
                pts.append(p)
                last = p
        if len(pts) >= 2:
            out.append(pts)
    return out


def _resolve_speed_force(device, speed, force):
    if speed is None:
        speed = getattr(device, "default_speed", 30)
    if force is None:
        force = getattr(device, "default_force", 80)
    clamp_s = getattr(device, "clamp_speed", None)
    clamp_f = getattr(device, "clamp_force", None)
    if callable(clamp_s):
        speed = clamp_s(speed)
    if callable(clamp_f):
        force = clamp_f(force)
    return float(speed), float(force)


class Protocol:
    """Base class.  Subclasses implement :meth:`encode`."""
    id: str = ""
    name: str = ""
    extension: str = "plt"           # suggested file extension
    #: default protocol_options shown/edited by the GUI
    default_options: dict = {}
    #: short help per option key
    option_help: dict = {}

    def encode(self, paths, device, speed, force) -> bytes:
        raise NotImplementedError

    def cancel_sequence(self, device) -> bytes:
        """Bytes that safely lift the tool after an aborted transfer."""
        return b""

    def options(self, device) -> dict:
        d = dict(self.default_options)
        d.update(getattr(device, "protocol_options", None) or {})
        return d

    def __repr__(self):
        return "<Protocol %s>" % self.id


# ---------------------------------------------------------------------------
# HP-GL
# ---------------------------------------------------------------------------
class HPGLProtocol(Protocol):
    """HP-GL for GCC Expert II and generic HPGL cutters.

    Output: ``IN;[SPn;][VSs;][FSf;]PUx,y;PDx,y,x,y,...;...PU;PU0,0;IN;``
    GCC: VS in cm/s, FS in grams, 0.025 mm units (40 steps/mm).
    """
    id = "hpgl"
    name = "HP-GL"
    extension = "plt"
    default_options = {"use_speed_cmd": False, "use_force_cmd": False, "return_home": True,
                       "feed_after_mm": 0.0, "pen": None, "newline": False,
                       "max_points": 64, "clamp_negative": True}
    option_help = {
        "use_speed_cmd": "Send VS<speed cm/s>; (GCC: yes)",
        "use_force_cmd": "Send FS<force g>; (GCC: yes)",
        "return_home": "Move to 0,0 with the blade up when finished",
        "feed_after_mm": "Instead of returning home, park this many mm past the end of the job",
        "pen": "Tool/pen number for SP (blank = do not send)",
        "newline": "Append a line feed after every command",
        "max_points": "Maximum points per PD command",
        "clamp_negative": "Clamp negative coordinates to 0",
    }
    reset = "IN;"
    up = "PU"
    down = "PD"

    def _cmd(self, s, nl):
        return s + ("\n" if nl else "")

    def init_string(self, device, speed, force, nl):
        out = []
        init = getattr(device, "init_cmd", "") or ""
        out.append(self._cmd(_sub(init, speed, force), False) if init else self._cmd(self.reset, nl))
        pen = _opt(device, "pen", None)
        if pen not in (None, ""):
            try:
                out.append(self._cmd("SP%d;" % int(pen), nl))
            except (TypeError, ValueError):
                pass
        if _bool_opt(device, "use_speed_cmd", self.default_options.get("use_speed_cmd")):
            tmpl = _opt(device, "speed_cmd", "") or self.speed_template
            out.append(self._cmd(_sub(tmpl, speed, force), nl))
        if _bool_opt(device, "use_force_cmd", self.default_options.get("use_force_cmd")):
            tmpl = _opt(device, "force_cmd", "") or self.force_template
            out.append(self._cmd(_sub(tmpl, speed, force), nl))
        return "".join(out)

    speed_template = "VS{speed};"
    force_template = "FS{force};"

    def finish_string(self, device, paths, speed, force, nl):
        fin = getattr(device, "finish_cmd", "") or ""
        if fin:
            return self._cmd(self.up + ";", nl) + _sub(fin, speed, force)
        out = []
        try:
            feed = float(_opt(device, "feed_after_mm", 0.0) or 0.0)
        except (TypeError, ValueError):
            feed = 0.0
        xs = [pt[0] for poly in (paths or ()) for pt in poly]
        if feed > 0 and xs:
            # park past the end of the job along the material feed axis (x)
            px, py = device.to_device(max(xs) + feed, 0)
            out.append(self._cmd("%s%d,%d;" % (self.up, max(0, px), max(0, py)), nl))
        elif _bool_opt(device, "return_home", True):
            out.append(self._cmd("%s0,0;" % self.up, nl))
        else:
            out.append(self._cmd(self.up + ";", nl))
        out.append(self._cmd(self.reset, nl))
        return "".join(out)

    def encode(self, paths, device, speed, force) -> bytes:
        speed, force = _resolve_speed_force(device, speed, force)
        nl = _bool_opt(device, "newline", False)
        maxp = max(1, int(_opt(device, "max_points", 64) or 64))
        polys = device_polylines(paths, device, _bool_opt(device, "clamp_negative", True))
        out = [self.init_string(device, speed, force, nl)]
        for pts in polys:
            x, y = pts[0]
            out.append(self._cmd("%s%d,%d;" % (self.up, x, y), nl))
            rest = pts[1:]
            for i in range(0, len(rest), maxp):
                chunk = rest[i:i + maxp]
                out.append(self._cmd(self.down + ",".join("%d,%d" % p for p in chunk) + ";", nl))
        out.append(self.finish_string(device, paths, speed, force, nl))
        return "".join(out).encode("ascii")

    def cancel_sequence(self, device) -> bytes:
        return b";PU;"


# ---------------------------------------------------------------------------
# DMPL (as InkCut's DMPLProtocol)
# ---------------------------------------------------------------------------
class DMPLProtocol(Protocol):
    """Houston Instruments DMPL.

    InkCut modes: 1 ``;:HAEC1``, 2 `` ;:ECN A L0 ``, 3/4 `` ;:H A L0 ``,
    6 ``IN;PA;`` (HPGL-like).  Moves `` U x,y `` / `` D x,y `` (mode 6:
    ``PUx,y;``/``PDx,y;``).  Speed ``V<n> ``, force ``BP<n> ``.
    """
    id = "dmpl"
    name = "DMPL"
    extension = "dmpl"
    default_options = {"mode": 1, "use_speed_cmd": False, "use_force_cmd": False,
                       "return_home": True, "clamp_negative": True}
    option_help = {"mode": "DMPL dialect (1, 2, 3, 4 or 6)",
                   "use_speed_cmd": "Send V<speed>", "use_force_cmd": "Send BP<force>",
                   "return_home": "Move to 0,0 when finished"}

    INIT = {1: ";:HAEC1", 2: " ;:ECN A L0 ", 3: " ;:H A L0 ", 4: " ;:H A L0 ", 6: "IN;PA;"}

    def encode(self, paths, device, speed, force) -> bytes:
        speed, force = _resolve_speed_force(device, speed, force)
        try:
            mode = int(_opt(device, "mode", 1))
        except (TypeError, ValueError):
            mode = 1
        if mode not in self.INIT:
            mode = 1
        out = []
        init = getattr(device, "init_cmd", "") or ""
        out.append(_sub(init, speed, force) if init else self.INIT[mode])
        if _bool_opt(device, "use_speed_cmd", False):
            out.append("V%d " % _round_int(speed))
        if _bool_opt(device, "use_force_cmd", False):
            out.append("BP%d " % _round_int(force))

        def mv(z, x, y):
            if mode in (1, 2, 3, 4):
                return " %s%d,%d " % ("D" if z else "U", x, y)
            return "%s%d,%d;" % ("PD" if z else "PU", x, y)

        polys = device_polylines(paths, device, _bool_opt(device, "clamp_negative", True))
        for pts in polys:
            for i, (x, y) in enumerate(pts):
                out.append(mv(i > 0, x, y))
        fin = getattr(device, "finish_cmd", "") or ""
        if polys:
            out.append(mv(False, *polys[-1][-1]))      # lift the tool where it is
        if fin:
            out.append(_sub(fin, speed, force))
        elif _bool_opt(device, "return_home", True):
            out.append(mv(False, 0, 0))
        return "".join(out).encode("ascii")

    def cancel_sequence(self, device) -> bytes:
        return b" U " if int(_opt(device, "mode", 1) or 1) != 6 else b";PU;"


# ---------------------------------------------------------------------------
# GP-GL (Graphtec), as InkCut's GPGLProtocol plus terminators
# ---------------------------------------------------------------------------
class GPGLProtocol(Protocol):
    """Graphtec GP-GL.  ``H`` home, ``M x,y`` move, ``D x,y[,x,y...]`` draw,
    ``!n`` speed, ``FXn,1`` force level.  InkCut writes no separators; real
    Graphtec firmware expects a terminator (ETX 0x03 by default here).
    """
    id = "gpgl"
    name = "GP-GL (Graphtec)"
    extension = "gpgl"
    default_options = {"use_speed_cmd": False, "use_force_cmd": False, "return_home": True,
                       "terminator": "\x03", "max_points": 64, "clamp_negative": True}
    option_help = {"terminator": "Command terminator (ETX \\x03, or \\n)",
                   "use_speed_cmd": "Send !<speed>", "use_force_cmd": "Send FX<force level>,1",
                   "return_home": "Move to 0,0 when finished"}

    def encode(self, paths, device, speed, force) -> bytes:
        speed, force = _resolve_speed_force(device, speed, force)
        t = str(_opt(device, "terminator", "\x03"))
        maxp = max(1, int(_opt(device, "max_points", 64) or 64))
        out = []
        init = getattr(device, "init_cmd", "") or ""
        out.append(_sub(init, speed, force) if init else "H" + t)
        if _bool_opt(device, "use_speed_cmd", False):
            out.append("!%d%s" % (_round_int(speed), t))
        if _bool_opt(device, "use_force_cmd", False):
            out.append("FX%d,1%s" % (_round_int(force), t))
        for pts in device_polylines(paths, device, _bool_opt(device, "clamp_negative", True)):
            out.append("M%d,%d%s" % (pts[0][0], pts[0][1], t))
            rest = pts[1:]
            for i in range(0, len(rest), maxp):
                out.append("D" + ",".join("%d,%d" % p for p in rest[i:i + maxp]) + t)
        fin = getattr(device, "finish_cmd", "") or ""
        if fin:
            out.append(_sub(fin, speed, force))
        elif _bool_opt(device, "return_home", True):
            out.append("M0,0" + t)
        return "".join(out).encode("ascii")

    def cancel_sequence(self, device) -> bytes:
        t = str(_opt(device, "terminator", "\x03"))
        return (t + "M0,0" + t).encode("ascii")


# ---------------------------------------------------------------------------
# CAMM-GL I (Roland), as InkCut's CAMMGL1Protocol
# ---------------------------------------------------------------------------
class CAMMProtocol(Protocol):
    """Roland CAMM-GL I: ``IN;`` ``VSn;`` ``FSn;`` ``SPn;`` ``Mx,y;`` ``Dx,y;``."""
    id = "camm"
    name = "CAMM-GL I (Roland)"
    extension = "camm"
    default_options = {"use_speed_cmd": True, "use_force_cmd": True, "return_home": True,
                       "pen": None, "clamp_negative": True}
    option_help = {"use_speed_cmd": "Send VS<speed>;", "use_force_cmd": "Send FS<force>;",
                   "pen": "Tool number for SP (blank = do not send)",
                   "return_home": "Move to 0,0 when finished"}

    def encode(self, paths, device, speed, force) -> bytes:
        speed, force = _resolve_speed_force(device, speed, force)
        out = []
        init = getattr(device, "init_cmd", "") or ""
        out.append(_sub(init, speed, force) if init else "IN;")
        pen = _opt(device, "pen", None)
        if pen not in (None, ""):
            try:
                out.append("SP%d;" % int(pen))
            except (TypeError, ValueError):
                pass
        if _bool_opt(device, "use_speed_cmd", True):
            out.append("VS%d;" % _round_int(speed))
        if _bool_opt(device, "use_force_cmd", True):
            out.append("FS%d;" % _round_int(force))
        for pts in device_polylines(paths, device, _bool_opt(device, "clamp_negative", True)):
            out.append("M%d,%d;" % pts[0])
            for p in pts[1:]:
                out.append("D%d,%d;" % p)
        fin = getattr(device, "finish_cmd", "") or ""
        if fin:
            out.append(_sub(fin, speed, force))
        elif _bool_opt(device, "return_home", True):
            out.append("M0,0;")
        return "".join(out).encode("ascii")

    def cancel_sequence(self, device) -> bytes:
        return b";M0,0;"


# ---------------------------------------------------------------------------
# G-code (as InkCut's GCodeProtocol, coordinates in mm)
# ---------------------------------------------------------------------------
class GCodeProtocol(Protocol):
    """G-code: ``G28``/``G90`` start, ``G00`` travel, ``G01`` cut.

    Coordinates are written in mm (``device.to_device_mm``; ``steps_per_mm``
    is ignored, option ``scale`` multiplies, default 1.0).  ``lift_mode`` "implicit"
    (the machine lifts on G00), "z" (Z moves to upper_z/lower_z) or "custom"
    (lift_gcode / lower_gcode blocks).  Feed ``F`` = speed cm/s * 600 mm/min
    when ``use_speed_cmd``.
    """
    id = "gcode"
    name = "G-code"
    extension = "gcode"
    default_options = {"use_builtin": True, "lift_mode": "implicit", "precision": 3,
                       "lower_z": 0.0, "upper_z": 1.0, "lift_gcode": "", "lower_gcode": "",
                       "use_speed_cmd": False, "return_home": True, "clamp_negative": True,
                       "scale": 1.0}
    option_help = {"use_builtin": "Send G28/G90 at start and G28 at the end",
                   "lift_mode": "implicit | z | custom", "precision": "Decimal places",
                   "lower_z": "Z when cutting (lift_mode z)", "upper_z": "Z when travelling",
                   "lift_gcode": "Block sent to lift the tool (custom)",
                   "lower_gcode": "Block sent to lower the tool (custom)",
                   "use_speed_cmd": "Add F<mm/min> to cutting moves"}

    def encode(self, paths, device, speed, force) -> bytes:
        speed, force = _resolve_speed_force(device, speed, force)
        o = self.options(device)
        prec = max(0, min(10, int(o.get("precision", 3) or 0)))
        mode = str(o.get("lift_mode", "implicit")).lower()
        if mode in ("0",):
            mode = "implicit"
        elif mode in ("1",):
            mode = "custom"
        elif mode in ("2",):
            mode = "z"
        try:
            scale = float(o.get("scale", 1.0) or 1.0)
        except (TypeError, ValueError):
            scale = 1.0
        clamp = _bool_opt(device, "clamp_negative", True)
        feed = " F%d" % _round_int(speed * 600) if _bool_opt(device, "use_speed_cmd", False) else ""

        def fmt(v):
            return "%.*f" % (prec, v)

        def block(s):
            s = s or ""
            return s if not s or s.endswith("\n") else s + "\n"

        out = []
        init = getattr(device, "init_cmd", "") or ""
        if init:
            out.append(block(_sub(init, speed, force)))
        elif _bool_opt(device, "use_builtin", True):
            out.append("G28 ; home\nG90 ; absolute coordinates\nG21 ; mm\n")
        up = True

        def move(z, x, y):
            nonlocal up
            lines = []
            if z and up:
                if mode == "custom":
                    lines.append(block(o.get("lower_gcode", "")))
                up = False
            elif not z and not up:
                if mode == "custom":
                    lines.append(block(o.get("lift_gcode", "")))
                up = True
            line = "G0%d X%s Y%s" % (1 if z else 0, fmt(x), fmt(y))
            if mode == "z":
                line += " Z%s" % fmt(float(o.get("lower_z", 0.0)) if z else float(o.get("upper_z", 1.0)))
            if z:
                line += feed
            lines.append(line + "\n")
            return "".join(lines)

        for poly in paths or ():
            pts = []
            last = None
            for pt in poly:
                x, y = device.to_device_mm(pt[0], pt[1]) if hasattr(device, "to_device_mm") \
                    else (float(pt[0]), float(pt[1]))
                x, y = x * scale, y * scale
                if clamp:
                    x, y = max(0.0, x), max(0.0, y)
                key = (round(x, prec), round(y, prec))
                if key != last:
                    pts.append((x, y))
                    last = key
            if len(pts) < 2:
                continue
            for i, (x, y) in enumerate(pts):
                out.append(move(i > 0, x, y))
        if not up:
            if mode == "custom":
                out.append(block(o.get("lift_gcode", "")))
            elif mode == "z":
                out.append("G00 Z%s\n" % fmt(float(o.get("upper_z", 1.0))))
        fin = getattr(device, "finish_cmd", "") or ""
        if fin:
            out.append(block(_sub(fin, speed, force)))
        elif _bool_opt(device, "use_builtin", True):
            out.append("G28 ; home\n")
        elif _bool_opt(device, "return_home", True):
            out.append("G00 X%s Y%s\n" % (fmt(0), fmt(0)))
        return "".join(out).encode("ascii")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
PROTOCOLS: dict = {p.id: p for p in (HPGLProtocol(), DMPLProtocol(), GPGLProtocol(),
                                     CAMMProtocol(), GCodeProtocol())}
#: aliases (InkCut ids etc.)
ALIASES = {"camm-gl1": "camm", "camm-gl": "camm", "hp-gl": "hpgl", "hpgl2": "hpgl",
           "gp-gl": "gpgl", "g-code": "gcode", "debug": "hpgl"}
#: [(id, display name)] for GUI combo boxes
PROTOCOL_CHOICES = [(p.id, p.name) for p in PROTOCOLS.values()]


def get_protocol(protocol_id: str) -> Protocol:
    pid = (protocol_id or "hpgl").lower()
    pid = ALIASES.get(pid, pid)
    try:
        return PROTOCOLS[pid]
    except KeyError:
        raise ValueError("Unknown cutter protocol '%s' (known: %s)"
                         % (protocol_id, ", ".join(PROTOCOLS)))


def encode(paths, device, speed, force) -> bytes:
    """Encode material-mm paths using ``device.protocol``."""
    return get_protocol(getattr(device, "protocol", "hpgl")).encode(paths, device, speed, force)


def cancel_sequence(device) -> bytes:
    """Bytes to send after an aborted job to lift the tool (may be empty)."""
    try:
        return get_protocol(getattr(device, "protocol", "hpgl")).cancel_sequence(device)
    except ValueError:
        return b""
