"""InkCut2 cutting window (GTK 3).

Usage::

    python -m inkcut2.gui <design.json>          # normal use (launched by the Inkscape extension)
    python -m inkcut2.gui --demo                 # built-in sample sticker
    python -m inkcut2.gui --demo --snapshot out.png [--width 1200 --height 800]
                                                  # render offscreen to PNG and exit (automated checks)

Extra options (mainly for testing): ``--set key=value`` (override a JobSettings field, repeatable),
``--tab job|cut|device|connection``, ``--view material|job``, ``--sim 0.4`` (draw the simulation
state at 40 %), ``--auto-quit SECONDS``, ``--device NAME``.

Layout: large preview on the left, settings notebook on the right, action bar at the bottom.
Coordinates follow docs/ARCHITECTURE.md (material mm, x = feed to the right, y = width upward).
"""

import argparse
import copy
import json
import logging
import math
import os
import sys
import tempfile
import threading
import time
import traceback
import types

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from .gui_preview import PreviewArea, Scene  # noqa: E402
from . import gui_widgets as W  # noqa: E402

log = logging.getLogger("inkcut2.gui")

APP_TITLE = "InkCut2"

ORDER_CHOICES = [
    ("inside_out", "Inside first (holes before outlines)"),
    ("shortest", "Shortest travel (optimised)"),
    ("by_x", "Along the feed (long rolls)"),
    ("as_is", "As drawn (no sorting)"),
]
PASS_MODES = [("per_path", "Repeat each path"), ("per_job", "Repeat the whole job")]
CONN_KINDS = [
    ("serial", "Serial (COM port)"),
    ("printer", "Windows printer (USB driver)"),
    ("file", "File"),
    ("network", "Network (TCP/IP)"),
]
PARITIES = [("N", "None"), ("E", "Even"), ("O", "Odd"), ("M", "Mark"), ("S", "Space")]
FLOWS = [
    ("none", "None"),
    ("rtscts", "RTS/CTS (hardware)"),
    ("xonxoff", "XON/XOFF (software)"),
    ("dsrdtr", "DSR/DTR (hardware)"),
]
BAUDRATES = [300, 600, 1200, 2400, 4800, 9600, 14400, 19200, 38400, 57600, 115200]
BYTESIZES = [5, 6, 7, 8]
STOPBITS = [1, 1.5, 2]
# Display units for the material / cutting area (mm per unit). Profiles always store mm.
AREA_UNITS = {"mm": 1.0, "in": 25.4}
# per area spin: (min mm, max mm, {unit: (step, digits)})
AREA_SPINS = {
    "width_mm": (1, 100000, {"mm": (1, 1), "in": (0.25, 3)}),
    "length_mm": (1, 1000000, {"mm": (10, 1), "in": (1, 2)}),
    "pad": (0, 1000, {"mm": (0.5, 1), "in": (0.05, 3)}),
}
TABS = ["job", "cut", "device", "connection"]
RIGHT_PANE = 470  # default width (px) of the settings column


# ----------------------------------------------------------------------------------------
# backend modules (imported lazily so a broken module produces an error dialog, not silence)
# ----------------------------------------------------------------------------------------
def load_backend():
    from . import geometry, job, devices, protocols, transports  # noqa: F401
    return types.SimpleNamespace(geometry=geometry, job=job, devices=devices,
                                 protocols=protocols, transports=transports)


# ----------------------------------------------------------------------------------------
# design loading / demo
# ----------------------------------------------------------------------------------------
class DesignError(Exception):
    pass


UNIT_FACTORS = {"mm": 1.0, "cm": 10.0, "in": 25.4, "px": 25.4 / 96.0, "pt": 25.4 / 72.0}


def parse_design(data, name="design"):
    """Validate a design dict (contract format) -> {"name", "polylines", "colors"}."""
    if not isinstance(data, dict):
        raise DesignError("Design file must contain a JSON object.")
    units = str(data.get("units", "mm")).lower()
    if units not in UNIT_FACTORS:
        raise DesignError("Unsupported units %r in design file." % units)
    k = UNIT_FACTORS[units]
    raw = data.get("polylines")
    if not isinstance(raw, list):
        raise DesignError("Design file has no 'polylines' list.")
    colors_in = data.get("colors") if isinstance(data.get("colors"), list) else None
    polys, colors = [], []
    for i, pl in enumerate(raw):
        try:
            pts = [(float(p[0]) * k, float(p[1]) * k) for p in pl]
        except (TypeError, ValueError, IndexError):
            raise DesignError("Polyline %d is malformed (expected [[x, y], ...])." % i)
        if any(math.isnan(x) or math.isinf(x) or math.isnan(y) or math.isinf(y) for x, y in pts):
            raise DesignError("Polyline %d contains invalid numbers." % i)
        if len(pts) < 2:
            continue
        polys.append(pts)
        if colors_in is not None and i < len(colors_in):
            colors.append(colors_in[i])
    if not polys:
        raise DesignError("The selection contains no cuttable paths.\n\n"
                          "Select one or more objects (paths, shapes or text) in Inkscape and try again.")
    return {"name": str(data.get("name") or name), "polylines": polys,
            "colors": colors if colors_in is not None else None}


def load_design(path):
    if not path or not os.path.exists(path):
        raise DesignError("Design file not found:\n%s" % path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise DesignError("Could not read design file:\n%s\n\n%s" % (path, e))
    return parse_design(data, os.path.basename(path))


def _rounded_rect(x0, y0, w, h, r, seg=8):
    pts = []
    corners = [(x0 + w - r, y0 + r, -90), (x0 + w - r, y0 + h - r, 0),
               (x0 + r, y0 + h - r, 90), (x0 + r, y0 + r, 180)]
    for cx, cy, a0 in corners:  # y-down design coords, clockwise on screen
        for i in range(seg + 1):
            a = math.radians(a0 + 90.0 * i / seg)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    pts.append(pts[0])
    return pts


def _circle(cx, cy, r, seg=40):
    pts = [(cx + r * math.cos(2 * math.pi * i / seg), cy + r * math.sin(2 * math.pi * i / seg))
           for i in range(seg)]
    pts.append(pts[0])
    return pts


def _star(cx, cy, R, r, n=5):
    pts = []
    for i in range(2 * n):
        rad = R if i % 2 == 0 else r
        a = -math.pi / 2 + math.pi * i / n  # first point straight up (y-down coords)
        pts.append((cx + rad * math.cos(a), cy + rad * math.sin(a)))
    pts.append(pts[0])
    return pts


def demo_design():
    """Sample sticker: rounded-rect contour, a round hole, a star and an open wavy line (mm, y down)."""
    wave = [(36 + i, 52 + 1.6 * math.sin(i / 3.0)) for i in range(0, 41)]
    polys = [
        _rounded_rect(0, 0, 90, 60, 8),
        _circle(16, 30, 6),
        _star(58, 27, 17, 7),
        wave,
    ]
    return {"name": "Demo sticker", "polylines": polys, "colors": None}


# ----------------------------------------------------------------------------------------
# small pure helpers (unit tested)
# ----------------------------------------------------------------------------------------
def coerce_value(text):
    """'--set key=value' value parsing: JSON literal if possible, else the raw string."""
    try:
        return json.loads(text)
    except ValueError:
        return text


def progress_to_timeline(frac, passes, pass_mode):
    """Map fraction of bytes sent -> (fraction of one-pass preview timeline, current pass or None)."""
    frac = max(0.0, min(1.0, float(frac)))
    passes = max(1, int(passes or 1))
    if pass_mode == "per_job" and passes > 1:
        x = frac * passes
        k = min(passes - 1, int(x))
        return x - k if frac < 1.0 else 1.0, k + 1
    return frac, None


def format_duration(seconds):
    seconds = max(0, int(round(seconds or 0)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%d h %02d min" % (h, m)
    if m:
        return "%d min %02d s" % (m, s)
    return "%d s" % s


def format_length(mm):
    mm = float(mm or 0)
    return "%.2f m" % (mm / 1000.0) if mm >= 1000 else "%.0f mm" % mm


def format_bytes(n):
    return "%.1f kB" % (n / 1024.0) if n >= 1024 else "%d B" % n


def unique_name(name, existing):
    existing = set(existing)
    if name not in existing:
        return name
    i = 2
    while "%s (%d)" % (name, i) in existing:
        i += 1
    return "%s (%d)" % (name, i)


def stopbits_value(v):
    v = float(v)
    return int(v) if v.is_integer() else v


def _state_path():
    # Same folder as job.json / devices.json (INKCUT2_CONFIG_DIR overrides).
    override = os.environ.get("INKCUT2_CONFIG_DIR")
    if override:
        return os.path.join(override, "gui.json")
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "inkcut2", "gui.json")


def load_gui_state():
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_gui_state(state):
    try:
        p = _state_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1)
    except OSError:
        log.exception("could not save GUI state")


# ----------------------------------------------------------------------------------------
# the window contents
# ----------------------------------------------------------------------------------------
class CutterApp:
    def __init__(self, design, backend, settings=None, persist=True, device_name=None):
        self.m = backend
        self.design = design
        self.polylines = design["polylines"]
        self.persist = persist
        self.gstate = load_gui_state() if persist else {}
        if settings is None:
            settings = backend.job.load_last_settings() if persist else backend.job.JobSettings()
        self.settings = settings
        self.profiles = list(backend.devices.load_profiles() or [])
        if not self.profiles:
            self.profiles = [copy.deepcopy(p) for p in backend.devices.PRESETS.values()]
        want = device_name or self.gstate.get("device")
        if not want and persist and hasattr(backend.devices, "load_selected_name"):
            try:
                want = backend.devices.load_selected_name()
            except Exception:  # noqa: BLE001
                want = None
        names = [p.name for p in self.profiles]
        self.dev_index = names.index(want) if want in names else 0
        self.device_dirty = False
        self.job = None
        self.scene = None
        self.stats = {}
        self._copy_size = (0.0, 0.0)
        self._updating = False
        self._rebuild_id = 0
        self._busy = False
        self._cancel = None
        self._send_meta = None
        self._send_t0 = 0.0
        self._last_error = None
        self.window = None

        self.root = self._build()
        self._refresh_profile_combo()
        self._load_device_widgets()
        self._load_conn_widgets()
        self._load_settings_widgets()
        self.rebuild()

    # ------------------------------------------------------------------ properties
    @property
    def device(self):
        return self.profiles[self.dev_index]

    # ------------------------------------------------------------------ build UI
    def _build(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.paned.set_wide_handle(True)
        root.pack_start(self.paned, True, True, 0)

        # left: preview + toolbar
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        left.pack_start(self._build_preview_toolbar(), False, False, 0)
        self.preview = PreviewArea()
        self.preview.get_offset = lambda: (self.settings.offset_x, self.settings.offset_y)
        self.preview.on_job_moved = self._on_job_dragged
        self.preview.on_sim_finished = self._on_sim_finished
        tg = self.gstate.get("toggles", {})
        self.preview.set_toggles(arrows=tg.get("arrows", True), order=tg.get("order", True),
                                 travel=tg.get("travel", True), boxes=tg.get("boxes", True))
        for key, btn in self.toggle_buttons.items():
            btn.set_active(getattr(self.preview, "show_" + key))
        frame = Gtk.Frame()
        frame.set_shadow_type(Gtk.ShadowType.IN)
        frame.add(self.preview)
        left.pack_start(frame, True, True, 0)
        self.paned.pack1(left, True, False)

        # right: settings notebook + summary
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        right.set_size_request(400, -1)
        self.notebook = Gtk.Notebook()
        self.notebook.set_scrollable(True)
        self.tab_labels = {}
        for key, title, builder in (("job", "Job", self._build_job_tab),
                                    ("cut", "Cut", self._build_cut_tab),
                                    ("device", "Device", self._build_device_tab),
                                    ("connection", "Connection", self._build_conn_tab)):
            sw = Gtk.ScrolledWindow()
            sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            sw.set_propagate_natural_width(True)
            sw.add(builder())
            lb = Gtk.Label(label=title)
            self.tab_labels[key] = lb
            self.notebook.append_page(sw, lb)
        right.pack_start(self.notebook, True, True, 0)
        right.pack_start(self._build_summary(), False, False, 0)
        self.paned.pack2(right, False, False)

        root.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)
        root.pack_start(self._build_action_bar(), False, False, 0)
        return root

    def _build_preview_toolbar(self):
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        W.add_class(bar, "vc-toolbar")
        bar.set_margin_start(4)
        bar.set_margin_end(4)
        bar.set_margin_top(4)
        bar.set_margin_bottom(4)
        self.toggle_buttons = {}
        group = []
        for key, text, tip in (("arrows", "Direction", "Show cutting direction arrows"),
                               ("order", "Order", "Number the paths in cutting order (only shown for up to 150 paths)"),
                               ("travel", "Travel", "Show knife-up travel moves (thin dashed lines)"),
                               ("boxes", "Copy boxes", "Show the bounding box of every copy")):
            b = Gtk.ToggleButton(label=text)
            b.set_tooltip_text(tip)
            b.connect("toggled", self._on_preview_toggle, key)
            self.toggle_buttons[key] = b
            group.append(b)
        bar.pack_start(W.hbox(*group, linked=True), False, False, 0)
        bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 4)
        zoom = W.hbox(
            W.button("-", lambda *_: self.preview.zoom(1 / 1.25), "Zoom out (or scroll wheel)"),
            W.button("+", lambda *_: self.preview.zoom(1.25), "Zoom in (or scroll wheel)"),
            W.button("Fit", lambda *_: self.preview.fit("material"), "Show the full material width"),
            W.button("Job", lambda *_: self.preview.fit("job"), "Zoom to the job"),
            linked=True)
        bar.pack_start(zoom, False, False, 0)
        bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 4)
        self.sim_button = Gtk.ToggleButton(label="Simulate")
        self.sim_button.set_tooltip_text("Animate the knife along the cut paths in cutting order")
        self.sim_button.connect("toggled", self._on_simulate)
        bar.pack_start(self.sim_button, False, False, 0)
        return bar

    # ---------------- Job tab
    def _build_job_tab(self):
        f = W.Form()
        w = self.w = {}
        f.header("Size")
        w["scale_pct"] = f.add("Scale", W.spin(0.1, 10000, 1, 1, 100, page=10,
                                               tooltip="Uniform scale of the design"), "%")
        w["scale_pct"].connect("value-changed", self._on_spin, "scale_pct")
        w["size_w"] = f.add("Width", W.spin(0.1, 100000, 1, 1, tooltip="Width of one copy (along the feed)"), "mm")
        w["size_h"] = f.add("Height", W.spin(0.1, 100000, 1, 1, tooltip="Height of one copy (across the width)"), "mm")
        w["size_w"].connect("value-changed", self._on_size, 0)
        w["size_h"].connect("value-changed", self._on_size, 1)
        lock = Gtk.CheckButton(label="Lock aspect ratio")
        lock.set_active(True)
        lock.set_sensitive(False)
        lock.set_tooltip_text("Width and height always scale together (uniform scaling)")
        w["lock"] = f.wide(lock)
        w["size_note"] = f.note("")

        f.header("Mirror and rotation")
        w["mirror_h"] = Gtk.ToggleButton(label="Mirror horizontal")
        w["mirror_h"].set_tooltip_text("Flip left/right, e.g. for heat-transfer vinyl (HTV) cut from the back")
        w["mirror_v"] = Gtk.ToggleButton(label="Mirror vertical")
        w["mirror_v"].set_tooltip_text("Flip top/bottom")
        for k in ("mirror_h", "mirror_v"):
            w[k].connect("toggled", self._on_toggle, k)
        mb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, homogeneous=True)
        mb.pack_start(w["mirror_h"], True, True, 0)
        mb.pack_start(w["mirror_v"], True, True, 0)
        f.wide(mb)
        self.rot_buttons = {}
        rb = []
        for deg in (0, 90, 180, 270):
            b = Gtk.ToggleButton(label="%d°" % deg)
            b.set_tooltip_text("Rotate %d° counter-clockwise" % deg)
            b.connect("toggled", self._on_rot_button, deg)
            self.rot_buttons[deg] = b
            rb.append(b)
        rbox = W.hbox(*rb, linked=True)
        for b in rb:
            b.set_hexpand(True)
        f.add("Rotation", rbox)
        w["rotation"] = f.add("Angle", W.spin(-360, 360, 1, 1, 0, page=15,
                                              tooltip="Free rotation angle, counter-clockwise"), "°")
        w["rotation"].connect("value-changed", self._on_spin, "rotation")

        f.header("Array (copies)")
        w["rows"] = f.add("Rows", W.spin(1, 999, 1, 0, 1, tooltip="Copies across the media width (y)"))
        w["cols"] = f.add("Columns", W.spin(1, 999, 1, 0, 1, tooltip="Copies along the feed (x)"))
        w["spacing_x"] = f.add("Spacing along feed", W.spin(0, 10000, 0.5, 1, 2, tooltip="Gap between columns"), "mm")
        w["spacing_y"] = f.add("Spacing across", W.spin(0, 10000, 0.5, 1, 2, tooltip="Gap between rows"), "mm")
        for k in ("rows", "cols", "spacing_x", "spacing_y"):
            w[k].connect("value-changed", self._on_spin, k)
        w["auto_fill"] = f.wide(Gtk.CheckButton(label="Fill material (as many copies as fit)"))
        w["auto_fill"].set_tooltip_text("Compute rows and columns automatically to fill the usable material")
        w["auto_fill"].connect("toggled", self._on_toggle, "auto_fill")
        if hasattr(self.settings, "fill_length"):
            w["fill_length"] = f.add("Fill length", W.spin(0, 1000000, 10, 0, 0,
                                                           tooltip="Material length that 'Fill material' may use "
                                                                   "(0 = the whole device length)"), "mm")
            w["fill_length"].connect("value-changed", self._on_spin, "fill_length")
        w["copies_note"] = f.note("")

        f.header("Position on material")
        w["offset_x"] = f.add("Offset along feed (x)", W.spin(0, 100000, 1, 1, 0, tooltip="Distance from the origin along the feed"), "mm")
        w["offset_y"] = f.add("Offset across (y)", W.spin(0, 100000, 1, 1, 0, tooltip="Distance from the origin across the media"), "mm")
        for k in ("offset_x", "offset_y"):
            w[k].connect("value-changed", self._on_spin, k)
        pb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, homogeneous=True)
        pb.pack_start(W.button("Reset to origin", self._on_reset_origin, "Move the job to offset 0, 0"), True, True, 0)
        pb.pack_start(W.button("Center across width", self._on_center_y, "Center the job across the usable media width"), True, True, 0)
        f.wide(pb)
        f.note("Tip: drag the job in the preview to place it (snaps to 1 mm, hold Shift for 0.1 mm).")
        return f

    # ---------------- Cut tab
    def _build_cut_tab(self):
        f = W.Form()
        w = self.w
        f.header("Passes")
        w["passes"] = f.add("Passes", W.spin(1, 50, 1, 0, 1, tooltip="Cut every path several times (thick or tough material)"))
        w["passes"].connect("value-changed", self._on_spin, "passes")
        w["pass_mode"] = f.add("Repeat", W.combo(PASS_MODES, tooltip="Repeat each path before moving on, or repeat the whole job"))
        w["pass_mode"].connect("changed", self._on_combo, "pass_mode")

        f.header("Cutting order")
        w["order"] = f.add("Path order", W.combo(ORDER_CHOICES, tooltip="Sorting / optimisation of the cut order"))
        w["order"].connect("changed", self._on_combo, "order")

        f.header("Tool")
        w["speed"] = f.add("Speed", W.OptionalSpin(1, 60, 1, 0, lambda v: self._on_optional("speed", v)), "cm/s")
        w["force"] = f.add("Force", W.OptionalSpin(5, 250, 5, 0, lambda v: self._on_optional("force", v)), "g")
        w["overcut"] = f.add("Overcut", W.OptionalSpin(0, 10, 0.1, 2, lambda v: self._on_optional("overcut", v),
                                                       tooltip="Closed paths continue past their start point"), "mm")
        w["blade_offset"] = f.add("Blade offset", W.OptionalSpin(0, 5, 0.05, 2, lambda v: self._on_optional("blade_offset", v),
                                                                 tooltip="Drag-knife tip offset compensation (0.25 mm for a 45° blade)"), "mm")

        f.header("Weeding")
        w["weed_border"] = f.wide(Gtk.CheckButton(label="Cut a weed border around the job"))
        w["weed_border"].set_tooltip_text("Adds a rectangle around all copies to make weeding easier")
        w["weed_border"].connect("toggled", self._on_toggle, "weed_border")
        w["weed_margin"] = f.add("Border distance", W.spin(0, 1000, 0.5, 1, 3), "mm")
        w["weed_margin"].connect("value-changed", self._on_spin, "weed_margin")
        f.note("Test Cut (bottom bar) cuts a 20 mm square with a triangle at the current job offset "
               "using these speed and force values.")
        return f

    # ---------------- Device tab
    def _build_device_tab(self):
        f = W.Form()
        d = self.dw = {}
        f.header("Profile")
        d["profile"] = W.combo([])
        d["profile"].connect("changed", self._on_profile_changed)
        f.wide(d["profile"])
        new_btn = Gtk.MenuButton(label="New")
        new_btn.set_tooltip_text("New profile from a preset")
        menu = Gtk.Menu()
        for pname in self.m.devices.PRESETS:
            it = Gtk.MenuItem(label=pname)
            it.connect("activate", self._on_new_profile, pname)
            menu.append(it)
        menu.show_all()
        new_btn.set_popup(menu)
        d["save"] = W.button("Save", self._on_save_profiles, "Save all device profiles")
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, homogeneous=True)
        for b in (new_btn, W.button("Duplicate", self._on_dup_profile, "Copy this profile"),
                  W.button("Delete", self._on_del_profile, "Delete this profile"), d["save"]):
            row.pack_start(b, True, True, 0)
        f.wide(row)
        f.wide(W.button("Import from InkCut…", self._on_import_inkcut,
                        "Import device profiles from InkCut (~/.config/inkcut/inkcut.device.json)"))

        f.header("Machine")
        d["name"] = f.add("Name", Gtk.Entry())
        d["name"].connect("changed", self._on_dev_name)
        protos = self.m.protocols.PROTOCOLS
        d["protocol"] = f.add("Protocol", W.combo([(k, getattr(v, "name", k) or k) for k, v in protos.items()],
                                                  tooltip="Command language (InkCut generic drivers)"))
        d["protocol"].connect("changed", self._on_dev_combo, "protocol")
        d["steps_per_mm"] = f.add("Resolution", W.spin(1, 1000, 1, 2, 40, tooltip="Device steps per mm (HPGL: 40)"), "steps/mm")
        d["steps_per_mm"].connect("value-changed", self._on_dev_spin, "steps_per_mm")
        d["notes"] = f.note("")
        exp = Gtk.Expander(label="Protocol options")
        exp.set_tooltip_text("Driver options of the selected protocol (as in InkCut's generic drivers)")
        d["proto_grid"] = Gtk.Grid(column_spacing=10, row_spacing=5)
        d["proto_grid"].set_margin_top(6)
        d["proto_grid"].set_margin_start(12)
        exp.add(d["proto_grid"])
        f.wide(exp)

        f.header("Material / cutting area")
        # Values are always stored in mm; these spins only *display* in the chosen unit.
        d["area_unit"] = {}
        unit_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, homogeneous=True)
        W.add_class(unit_box, "linked")
        group = None
        for u in AREA_UNITS:
            rb = Gtk.RadioButton.new_with_label_from_widget(group, u)
            rb.set_mode(False)  # draw as a toggle button
            rb.set_tooltip_text("Show the material size and margins in %s" % ("millimetres" if u == "mm" else "inches"))
            rb.connect("toggled", self._on_area_unit)
            group = group or rb
            unit_box.pack_start(rb, True, True, 0)
            d["area_unit"][u] = rb
        f.add("Units", unit_box)
        d["area_units"] = []  # unit labels next to the spins
        d["width_mm"] = f.add("Cuttable width", W.spin(1, 100000, 1, 1, 610, tooltip="Across the feed"), "mm")
        d["area_units"].append(f.get_child_at(2, f.row - 1))
        d["length_mm"] = f.add("Max. length", W.spin(1, 1000000, 10, 1, 5000, tooltip="Along the feed (roll length)"), "mm")
        d["area_units"].append(f.get_child_at(2, f.row - 1))
        for k in ("width_mm", "length_mm"):
            d[k].connect("value-changed", self._on_dev_spin, k)
        d["pad"] = []
        for i, (txt, tip) in enumerate((("Margin start (x min)", "Unusable margin at the origin end along the feed"),
                                        ("Margin bottom (y min)", "Unusable margin at the origin side across the width"),
                                        ("Margin end (x max)", "Unusable margin at the far end of the length"),
                                        ("Margin top (y max)", "Unusable margin at the far side of the width"))):
            sb = f.add(txt, W.spin(0, 1000, 0.5, 1, 0, tooltip=tip), "mm")
            d["area_units"].append(f.get_child_at(2, f.row - 1))
            sb.connect("value-changed", self._on_dev_pad, i)
            d["pad"].append(sb)
        self.area_unit = self.gstate.get("area_unit") if self.gstate.get("area_unit") in AREA_UNITS else "mm"
        self._updating = True
        try:
            d["area_unit"][self.area_unit].set_active(True)
        finally:
            self._updating = False
        self._apply_area_unit()

        f.header("Axes")
        axes = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        for k, txt, tip in (("swap_axes", "Swap X/Y", "Exchange the device axes"),
                            ("invert_x", "Invert X", "Mirror the device x axis (relative to the length)"),
                            ("invert_y", "Invert Y", "Mirror the device y axis (relative to the width)")):
            cb = Gtk.CheckButton(label=txt)
            cb.set_tooltip_text(tip)
            cb.connect("toggled", self._on_dev_check, k)
            d[k] = cb
            axes.pack_start(cb, False, False, 0)
        f.wide(axes)

        f.header("Defaults")
        d["default_speed"] = f.add("Speed", W.spin(1, 60, 1, 0, 30), "cm/s")
        d["default_force"] = f.add("Force", W.spin(5, 250, 5, 0, 80), "g")
        d["default_overcut"] = f.add("Overcut", W.spin(0, 10, 0.1, 2, 0.5), "mm")
        d["default_blade_offset"] = f.add("Blade offset", W.spin(0, 5, 0.05, 2, 0.25), "mm")
        for k in ("default_speed", "default_force", "default_overcut", "default_blade_offset"):
            d[k].connect("value-changed", self._on_dev_spin, k)
        f.header("Machine limits", tooltip="Allowed speed / force range of the machine")
        d["speed_range"] = [f.add("Min. speed", W.spin(0, 1000, 1, 0, 1), "cm/s"),
                            f.add("Max. speed", W.spin(0, 1000, 1, 0, 60), "cm/s")]
        d["force_range"] = [f.add("Min. force", W.spin(0, 5000, 5, 0, 5), "g"),
                            f.add("Max. force", W.spin(0, 5000, 5, 0, 250), "g")]
        for key in ("speed_range", "force_range"):
            for i, sb in enumerate(d[key]):
                sb.connect("value-changed", self._on_dev_range, key, i)

        f.header("Custom commands")
        for k, txt, tip in (("init_cmd", "Init", "Raw commands sent before the job (overrides the protocol default when not empty)"),
                            ("finish_cmd", "Finish", "Raw commands sent after the job")):
            tv = Gtk.TextView()
            tv.set_monospace(True)
            tv.set_wrap_mode(Gtk.WrapMode.CHAR)
            tv.set_tooltip_text(tip)
            tv.get_buffer().connect("changed", self._on_dev_text, k)
            sw = Gtk.ScrolledWindow()
            sw.set_shadow_type(Gtk.ShadowType.IN)
            sw.set_size_request(-1, 48)
            sw.add(tv)
            d[k] = tv
            f.add(txt, sw, tooltip=tip)
        return f

    # ---------------- Connection tab
    def _build_conn_tab(self):
        f = W.Form()
        c = self.cw = {}
        f.header("Connection")
        c["kind"] = f.add("Type", W.combo(CONN_KINDS, tooltip="How the cutter is connected. The GCC USB driver installs as a Windows printer."))
        c["kind"].connect("changed", self._on_conn_kind)
        self.conn_stack = Gtk.Stack()
        self.conn_stack.set_homogeneous(False)

        # serial
        g = W.Form(margin=0)
        c["port"] = Gtk.ComboBoxText.new_with_entry()
        c["port"].get_child().connect("changed", self._on_conn_entry, "port")
        c["port"].connect("changed", self._on_port_combo)
        c["port"].set_hexpand(True)
        g.add("Port", W.hbox(c["port"], W.button("Refresh", self._refresh_ports, "Re-scan serial ports"),
                             expand_first=True))
        c["port_desc"] = g.note("")
        c["baudrate"] = Gtk.ComboBoxText.new_with_entry()
        for b in BAUDRATES:
            c["baudrate"].append(str(b), str(b))
        c["baudrate"].get_child().connect("changed", self._on_baud)
        g.add("Baud rate", c["baudrate"], "bit/s")
        c["bytesize"] = g.add("Data bits", W.combo([(b, str(b)) for b in BYTESIZES]))
        c["parity"] = g.add("Parity", W.combo(PARITIES))
        c["stopbits"] = g.add("Stop bits", W.combo([(str(s), str(s)) for s in STOPBITS]))
        c["flow"] = g.add("Flow control", W.combo(FLOWS, tooltip="Handshake. Most GCC / HPGL cutters use RTS/CTS hardware flow control."))
        for k in ("bytesize", "parity", "stopbits", "flow"):
            c[k].connect("changed", self._on_conn_combo, k)
        g.show_all()  # Gtk.Stack only switches to visible children
        self.conn_stack.add_named(g, "serial")

        # printer
        g = W.Form(margin=0)
        c["printer_name"] = Gtk.ComboBoxText.new_with_entry()
        c["printer_name"].set_hexpand(True)
        c["printer_name"].get_child().connect("changed", self._on_conn_entry, "printer_name")
        g.add("Printer", W.hbox(c["printer_name"], W.button("Refresh", self._refresh_printers, "Re-scan Windows printers"),
                                expand_first=True))
        g.note("Data is sent RAW through the Windows spooler (use this for the GCC USB driver).")
        g.show_all()  # Gtk.Stack only switches to visible children
        self.conn_stack.add_named(g, "printer")

        # file
        g = W.Form(margin=0)
        c["file_path"] = Gtk.Entry()
        c["file_path"].set_hexpand(True)
        c["file_path"].connect("changed", self._on_conn_entry, "file_path")
        g.add("File", W.hbox(c["file_path"], W.button("Browse…", self._on_browse_file), expand_first=True))
        g.note("Writes the cutter commands to this file (or a device path such as \\\\.\\COM4 or LPT1). "
               "{time} in the name is replaced by a timestamp.")
        g.show_all()  # Gtk.Stack only switches to visible children
        self.conn_stack.add_named(g, "file")

        # network
        g = W.Form(margin=0)
        c["host"] = g.add("Host", Gtk.Entry())
        c["host"].connect("changed", self._on_conn_entry, "host")
        c["net_port"] = g.add("Port", W.spin(1, 65535, 1, 0, 9100))
        c["net_port"].connect("value-changed", self._on_conn_spin, "net_port")
        g.show_all()  # Gtk.Stack only switches to visible children
        self.conn_stack.add_named(g, "network")
        f.wide(self.conn_stack, indent=False)

        f.header("Data transfer")
        c["chunk_size"] = f.add("Chunk size", W.spin(1, 1048576, 64, 0, 512, tooltip="Bytes per write"), "bytes")
        c["chunk_delay_ms"] = f.add("Delay between chunks", W.spin(0, 10000, 5, 0, 0), "ms")
        for k in ("chunk_size", "chunk_delay_ms"):
            c[k].connect("value-changed", self._on_conn_spin, k)
        if hasattr(self.device.connection, "timeout_s"):
            c["timeout_s"] = f.add("Timeout", W.spin(1, 3600, 5, 0, 120,
                                                     tooltip="Give up when the cutter accepts no data for this long"), "s")
            c["timeout_s"].connect("value-changed", self._on_conn_spin, "timeout_s")
        c["test"] = f.wide(W.button("Test connection", self._on_test_connection, "Open and close the connection (nothing is cut)"))
        c["test_result"] = f.note("")
        f.note("Connection settings belong to the selected device profile; use Save on the Device tab "
               "(they are also saved automatically when you send a job).")
        return f

    # ---------------- summary + action bar
    def _build_summary(self):
        frame = Gtk.Frame(label=" Job summary ")
        frame.set_margin_start(8)
        frame.set_margin_end(8)
        frame.set_margin_bottom(6)
        frame.set_margin_top(6)
        g = Gtk.Grid(column_spacing=12, row_spacing=4)
        g.set_margin_start(10)
        g.set_margin_end(10)
        g.set_margin_top(6)
        g.set_margin_bottom(8)
        self.stat_labels = {}
        items = [("copies", "Copies"), ("paths", "Paths"), ("size", "Size"),
                 ("cut", "Cut length"), ("travel", "Travel"), ("time", "Est. time")]
        for i, (k, txt) in enumerate(items):
            r, cidx = divmod(i, 2)
            g.attach(W.label(txt, css="vc-dim"), cidx * 2, r, 1, 1)
            v = W.label("–", css="vc-stat")
            v.set_hexpand(True)
            g.attach(v, cidx * 2 + 1, r, 1, 1)
            self.stat_labels[k] = v
        self.warn_label = W.label("", wrap=True, css="vc-warning")
        g.attach(self.warn_label, 0, 3, 4, 1)
        frame.add(g)
        return frame

    def _build_action_bar(self):
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        W.add_class(bar, "vc-statusbar")
        bar.set_margin_start(8)
        bar.set_margin_end(8)
        bar.set_margin_top(6)
        bar.set_margin_bottom(6)
        self.btn_test = W.button("Test Cut", self._on_test_cut, "Cut a small test shape at the current job offset")
        self.btn_save = W.button("Save .plt…", self._on_save_plt, "Save the cutter commands to a file")
        bar.pack_start(self.btn_test, False, False, 0)
        bar.pack_start(self.btn_save, False, False, 0)
        mid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.progress = Gtk.ProgressBar()
        self.progress.set_show_text(True)
        self.progress.set_text("Ready")
        self.progress.set_valign(Gtk.Align.CENTER)
        self.status_label = W.label("", css="vc-small")
        self.status_label.set_ellipsize(3)
        mid.pack_start(self.progress, False, False, 0)
        mid.pack_start(self.status_label, False, False, 0)
        mid.set_valign(Gtk.Align.CENTER)
        bar.pack_start(mid, True, True, 6)
        self.btn_stop = W.button("Stop", self._on_stop, "Stop sending (the cutter may finish commands already in its buffer)",
                                 css="destructive-action")
        self.btn_stop.set_sensitive(False)
        self.btn_send = W.button("Send to cutter", self._on_send, "Cut the job (Ctrl+Enter)", css="suggested-action")
        self.btn_send.set_size_request(150, -1)
        bar.pack_start(self.btn_stop, False, False, 0)
        bar.pack_start(self.btn_send, False, False, 0)
        return bar

    # ------------------------------------------------------------------ load widgets
    def _load_settings_widgets(self):
        s, w, dev = self.settings, self.w, self.device
        self._updating = True
        try:
            for k in ("scale_pct", "rotation", "rows", "cols", "spacing_x", "spacing_y",
                      "offset_x", "offset_y", "passes", "weed_margin", "fill_length"):
                if k in w:
                    w[k].set_value(float(getattr(s, k) or 0))
            for k in ("mirror_h", "mirror_v", "auto_fill", "weed_border"):
                w[k].set_active(bool(getattr(s, k)))
            w["pass_mode"].set_active_id(str(s.pass_mode))
            if w["order"].get_active_id() != s.order and not w["order"].set_active_id(str(s.order)):
                w["order"].set_active_id("inside_out")
            self._sync_optional_ranges()
            w["speed"].set(s.speed, dev.default_speed)
            w["force"].set(s.force, dev.default_force)
            w["overcut"].set(s.overcut, dev.default_overcut)
            w["blade_offset"].set(s.blade_offset, dev.default_blade_offset)
            self._sync_rot_buttons()
            self._sync_sensitivity()
        finally:
            self._updating = False

    def _sync_optional_ranges(self):
        dev = self.device
        try:
            lo, hi = (float(v) for v in dev.speed_range)
            self.w["speed"].set_range(min(lo, hi), max(lo, hi))
        except (TypeError, ValueError):
            pass
        try:
            lo, hi = (float(v) for v in dev.force_range)
            self.w["force"].set_range(min(lo, hi), max(lo, hi))
        except (TypeError, ValueError):
            pass

    def _sync_rot_buttons(self):
        rot = float(self.settings.rotation) % 360.0
        prev = self._updating
        self._updating = True
        try:
            for deg, b in self.rot_buttons.items():
                b.set_active(abs(rot - deg) < 1e-6)
        finally:
            self._updating = prev

    def _sync_sensitivity(self):
        af = bool(self.settings.auto_fill)
        self.w["rows"].set_sensitive(not af)
        self.w["cols"].set_sensitive(not af)
        if "fill_length" in self.w:
            self.w["fill_length"].set_sensitive(af)
        self.w["weed_margin"].set_sensitive(bool(self.settings.weed_border))
        self.w["pass_mode"].set_sensitive(int(self.settings.passes) > 1)

    def _refresh_profile_combo(self):
        self._updating = True
        try:
            cb = self.dw["profile"]
            cb.remove_all()
            for i, p in enumerate(self.profiles):
                cb.append(str(i), p.name)
            cb.set_active(self.dev_index)
        finally:
            self._updating = False

    def _load_device_widgets(self):
        d, dev = self.dw, self.device
        self._updating = True
        try:
            d["name"].set_text(dev.name)
            if not d["protocol"].set_active_id(str(dev.protocol)):
                d["protocol"].append(str(dev.protocol), str(dev.protocol))
                d["protocol"].set_active_id(str(dev.protocol))
            for k in ("steps_per_mm", "default_speed", "default_force",
                      "default_overcut", "default_blade_offset"):
                d[k].set_value(float(getattr(dev, k)))
            self._show_area_values()
            for k in ("swap_axes", "invert_x", "invert_y"):
                d[k].set_active(bool(getattr(dev, k)))
            for key in ("speed_range", "force_range"):
                rng = tuple(getattr(dev, key))
                for i in range(2):
                    d[key][i].set_value(float(rng[i]))
            self._apply_default_ranges()
            for k in ("init_cmd", "finish_cmd"):
                d[k].get_buffer().set_text(getattr(dev, k) or "")
            notes = str(getattr(dev, "notes", "") or "")
            d["notes"].set_text(notes)
            d["notes"].set_visible(bool(notes))
            self._build_proto_options()
        finally:
            self._updating = False
        self._mark_dirty(self.device_dirty)

    def _build_proto_options(self):
        """(Re)create the editable protocol_options for the current device/protocol."""
        grid = self.dw["proto_grid"]
        for ch in grid.get_children():
            grid.remove(ch)
        dev = self.device
        proto = self.m.protocols.PROTOCOLS.get(dev.protocol)
        try:
            opts = proto.options(dev) if proto is not None and hasattr(proto, "options") \
                else dict(dev.protocol_options or {})
        except Exception:  # noqa: BLE001
            opts = dict(dev.protocol_options or {})
        helps = getattr(proto, "option_help", {}) or {}
        defaults = getattr(proto, "default_options", {}) or {}
        prev = self._updating
        self._updating = True
        try:
            for row, (key, val) in enumerate(sorted(opts.items())):
                kind = type(defaults.get(key, val))
                lbl = W.label(key.replace("_", " "))
                tip = helps.get(key)
                if kind is bool:
                    wdg = Gtk.CheckButton()
                    wdg.set_active(bool(val))
                    wdg.connect("toggled", self._on_proto_opt, key, bool)
                elif kind in (int, float):
                    digits = 0 if kind is int else 2
                    wdg = W.spin(-1e6, 1e6, 1 if kind is int else 0.5, digits, float(val or 0))
                    wdg.connect("value-changed", self._on_proto_opt, key, kind)
                elif isinstance(val, (dict, list)):
                    continue
                else:
                    wdg = Gtk.Entry()
                    wdg.set_text("" if val is None else str(val))
                    wdg.set_placeholder_text("(not set)")
                    wdg.connect("changed", self._on_proto_opt, key, str)
                if tip:
                    lbl.set_tooltip_text(tip)
                    wdg.set_tooltip_text(tip)
                wdg.set_hexpand(True)
                grid.attach(lbl, 0, row, 1, 1)
                grid.attach(wdg, 1, row, 1, 1)
            if not opts:
                grid.attach(W.label("This protocol has no options.", css="vc-dim"), 0, 0, 2, 1)
        finally:
            self._updating = prev
        grid.show_all()

    def _on_proto_opt(self, wdg, key, kind):
        if self._updating:
            return
        if kind is bool:
            val = bool(wdg.get_active())
        elif kind is int:
            val = int(round(wdg.get_value()))
        elif kind is float:
            val = float(wdg.get_value())
        else:
            txt = wdg.get_text().strip()
            val = None if txt == "" else (int(txt) if txt.lstrip("-").isdigit() else txt)
        opts = dict(self.device.protocol_options or {})
        opts[key] = val
        self.device.protocol_options = opts
        self._dev_changed(rebuild=False)

    def _apply_default_ranges(self):
        d, dev = self.dw, self.device
        lo, hi = sorted(float(v) for v in dev.speed_range)
        d["default_speed"].set_range(lo, max(hi, lo + 1e-3))
        lo, hi = sorted(float(v) for v in dev.force_range)
        d["default_force"].set_range(lo, max(hi, lo + 1e-3))

    def _load_conn_widgets(self):
        c, conn = self.cw, self.device.connection
        self._updating = True
        try:
            c["kind"].set_active_id(conn.kind if conn.kind in dict(CONN_KINDS) else "serial")
            self.conn_stack.set_visible_child_name(c["kind"].get_active_id() or "serial")
            self._fill_ports()
            c["port"].get_child().set_text(conn.port or "")
            c["baudrate"].get_child().set_text(str(conn.baudrate))
            c["bytesize"].set_active_id(str(int(conn.bytesize)))
            c["parity"].set_active_id(str(conn.parity).upper()[:1] or "N")
            c["stopbits"].set_active_id(str(stopbits_value(conn.stopbits)))
            c["flow"].set_active_id(str(conn.flow) if str(conn.flow) in dict(FLOWS) else "none")
            self._fill_printers()
            c["printer_name"].get_child().set_text(conn.printer_name or "")
            c["file_path"].set_text(conn.file_path or "")
            c["host"].set_text(conn.host or "")
            c["net_port"].set_value(float(conn.net_port))
            c["chunk_size"].set_value(float(conn.chunk_size))
            c["chunk_delay_ms"].set_value(float(conn.chunk_delay_ms))
            if "timeout_s" in c:
                c["timeout_s"].set_value(float(getattr(conn, "timeout_s", 120) or 120))
            c["test_result"].set_text("")
        finally:
            self._updating = False
        self._update_port_desc()

    def _fill_ports(self):
        cb = self.cw["port"]
        cb.remove_all()
        self._ports = []
        try:
            self._ports = list(self.m.transports.list_serial_ports())
        except Exception as e:  # noqa: BLE001
            log.exception("list_serial_ports failed")
            self.status("Could not list serial ports: %s" % e, "error")
        for dev, _desc in self._ports:
            cb.append(dev, dev)

    def _fill_printers(self):
        cb = self.cw["printer_name"]
        cb.remove_all()
        try:
            for name in self.m.transports.list_printers():
                cb.append(name, name)
        except Exception as e:  # noqa: BLE001
            log.exception("list_printers failed")
            self.status("Could not list printers: %s" % e, "error")

    def _update_port_desc(self):
        port = self.cw["port"].get_child().get_text().strip()
        desc = dict(getattr(self, "_ports", [])).get(port)
        if desc:
            self.cw["port_desc"].set_text(desc)
        elif not self._ports:
            self.cw["port_desc"].set_text("No serial ports found. Install the cutter's USB-serial driver, "
                                          "or use 'Windows printer' for the GCC USB driver.")
        else:
            self.cw["port_desc"].set_text("Available: " + ", ".join(p for p, _ in self._ports))

    # ------------------------------------------------------------------ settings callbacks
    def _changed(self):
        self._sync_sensitivity()
        self.schedule_rebuild()

    def _on_spin(self, sb, key):
        if self._updating:
            return
        v = sb.get_value()
        if key in ("rows", "cols", "passes"):
            v = int(round(v))
        setattr(self.settings, key, v)
        if key == "rotation":
            self._sync_rot_buttons()
        self._changed()

    def _on_toggle(self, b, key):
        if self._updating:
            return
        setattr(self.settings, key, bool(b.get_active()))
        self._changed()

    def _on_combo(self, cb, key):
        if self._updating:
            return
        v = cb.get_active_id()
        if v is not None:
            setattr(self.settings, key, v)
            self._changed()

    def _on_optional(self, key, value):
        if self._updating:
            return
        setattr(self.settings, key, value)
        self._changed()

    def _on_rot_button(self, b, deg):
        if self._updating:
            return
        if not b.get_active():
            # keep one button pressed when the angle still matches it
            self._sync_rot_buttons()
            return
        self.settings.rotation = float(deg)
        self._updating = True
        try:
            self.w["rotation"].set_value(float(deg))
        finally:
            self._updating = False
        self._sync_rot_buttons()
        self._changed()

    def _on_size(self, sb, axis):
        if self._updating:
            return
        cur = self._copy_size[axis]
        if cur <= 1e-9:
            return
        new_scale = float(self.settings.scale_pct) * sb.get_value() / cur
        self.settings.scale_pct = max(0.1, min(10000.0, new_scale))
        self._updating = True
        try:
            self.w["scale_pct"].set_value(self.settings.scale_pct)
        finally:
            self._updating = False
        self._changed()

    def _set_offsets(self, x, y):
        self.settings.offset_x = max(0.0, float(x))
        self.settings.offset_y = max(0.0, float(y))
        self._updating = True
        try:
            self.w["offset_x"].set_value(self.settings.offset_x)
            self.w["offset_y"].set_value(self.settings.offset_y)
        finally:
            self._updating = False

    def _on_reset_origin(self, *_):
        self._set_offsets(0, 0)
        self.rebuild()

    def _on_center_y(self, *_):
        sc = self.scene
        if not sc or not sc.job_bbox:
            return
        pl, pb, pr, pt = sc.padding
        h = sc.job_bbox[3] - sc.job_bbox[1]
        self._set_offsets(self.settings.offset_x, round(max(0.0, (sc.width_mm - pb - pt - h) / 2.0), 1))
        self.rebuild()

    def _on_job_dragged(self, x, y):
        self._set_offsets(x, y)
        self.rebuild()
        self.status("Job moved to x %.1f mm, y %.1f mm" % (self.settings.offset_x, self.settings.offset_y))

    # ------------------------------------------------------------------ device callbacks
    def _mark_dirty(self, dirty=True):
        self.device_dirty = bool(dirty)
        self.dw["save"].set_label("Save *" if dirty else "Save")
        self.tab_labels["device"].set_text("Device *" if dirty else "Device")

    def _dev_changed(self, rebuild=True):
        self._mark_dirty(True)
        if rebuild:
            self.schedule_rebuild()

    def _on_dev_spin(self, sb, key):
        if self._updating:
            return
        if key in ("width_mm", "length_mm"):
            value = self._area_mm(sb, getattr(self.device, key))
        else:
            value = float(sb.get_value())
        setattr(self.device, key, value)
        if key.startswith("default_"):
            self._refresh_optional_defaults()
        self._dev_changed()

    def _on_dev_pad(self, sb, i):
        if self._updating:
            return
        pad = list(tuple(self.device.padding or (0, 0, 0, 0)) + (0, 0, 0, 0))[:4]
        pad[i] = self._area_mm(sb, float(pad[i]))
        self.device.padding = tuple(pad)
        self._dev_changed()

    # material / cutting area units ------------------------------------------------------
    def _area_spins(self):
        d = self.dw
        return [("width_mm", d["width_mm"]), ("length_mm", d["length_mm"])] + [("pad", sb) for sb in d["pad"]]

    def _apply_area_unit(self):
        """Re-scale the area spins to self.area_unit and show the device's (mm) values in it."""
        f = AREA_UNITS[self.area_unit]
        prev, self._updating = self._updating, True
        try:
            for key, sb in self._area_spins():
                lo, hi, per_unit = AREA_SPINS[key]
                step, digits = per_unit[self.area_unit]
                sb.set_digits(digits)
                sb.set_increments(step, step * 10)
                sb.set_range(lo / f, hi / f)
            for lb in self.dw["area_units"]:
                lb.set_text(self.area_unit)
            self._show_area_values()
        finally:
            self._updating = prev
        self.preview.set_unit(self.area_unit)

    def _show_area_values(self):
        """Put the device's width/length/margins (mm) into the spins, in the display unit."""
        d, dev, f = self.dw, self.device, AREA_UNITS[self.area_unit]
        d["width_mm"].set_value(float(dev.width_mm) / f)
        d["length_mm"].set_value(float(dev.length_mm) / f)
        pad = tuple(dev.padding or (0, 0, 0, 0)) + (0, 0, 0, 0)
        for i in range(4):
            d["pad"][i].set_value(float(pad[i]) / f)

    def _area_mm(self, sb, old_mm):
        """Spin value back to mm. Keeps *old_mm* exactly when the spin only shows it rounded
        (so switching to inches and back never drifts the stored size)."""
        f = AREA_UNITS[self.area_unit]
        v = float(sb.get_value())
        shown = old_mm / f
        if abs(v - shown) < 1e-9 or abs(v - round(shown, sb.get_digits())) < 1e-9:
            return float(old_mm)
        return v * f

    def _on_area_unit(self, rb):
        if self._updating or not rb.get_active():
            return
        self.area_unit = next(u for u, b in self.dw["area_unit"].items() if b is rb)
        self.gstate["area_unit"] = self.area_unit
        self._apply_area_unit()

    def set_area_unit(self, unit):
        """Switch the material / cutting area display unit ("mm" | "in")."""
        self.dw["area_unit"][unit].set_active(True)

    def _on_dev_range(self, sb, key, i):
        if self._updating:
            return
        rng = list(getattr(self.device, key))
        rng[i] = float(sb.get_value())
        setattr(self.device, key, tuple(rng))
        self._updating = True
        try:
            self._apply_default_ranges()
            self._sync_optional_ranges()
        finally:
            self._updating = False
        self._dev_changed()

    def _on_dev_check(self, cb, key):
        if self._updating:
            return
        setattr(self.device, key, bool(cb.get_active()))
        self._dev_changed()

    def _on_dev_combo(self, cb, key):
        if self._updating:
            return
        v = cb.get_active_id()
        if v:
            setattr(self.device, key, v)
            if key == "protocol":
                self._build_proto_options()
            self._dev_changed()

    def _on_dev_text(self, buf, key):
        if self._updating:
            return
        setattr(self.device, key, buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False))
        self._dev_changed(rebuild=False)

    def _on_dev_name(self, entry):
        if self._updating:
            return
        name = entry.get_text().strip()
        if not name:
            return
        self.device.name = name
        self._refresh_profile_combo()
        self._dev_changed(rebuild=False)

    def _refresh_optional_defaults(self):
        s, dev, w = self.settings, self.device, self.w
        self._updating = True
        try:
            self._sync_optional_ranges()
            w["speed"].set(s.speed, dev.default_speed)
            w["force"].set(s.force, dev.default_force)
            w["overcut"].set(s.overcut, dev.default_overcut)
            w["blade_offset"].set(s.blade_offset, dev.default_blade_offset)
        finally:
            self._updating = False

    def _on_profile_changed(self, cb):
        if self._updating:
            return
        i = cb.get_active()
        if i < 0 or i >= len(self.profiles) or i == self.dev_index:
            return
        self.dev_index = i
        self._load_device_widgets()
        self._load_conn_widgets()
        self._refresh_optional_defaults()
        self.rebuild()
        self.status("Device: %s" % self.device.name)

    def _select_profile(self, i):
        self.dev_index = i
        self._refresh_profile_combo()
        self._load_device_widgets()
        self._load_conn_widgets()
        self._refresh_optional_defaults()
        self.rebuild()

    def _on_new_profile(self, _item, preset):
        p = copy.deepcopy(self.m.devices.PRESETS[preset])
        p.name = unique_name(p.name, [q.name for q in self.profiles])
        self.profiles.append(p)
        self._select_profile(len(self.profiles) - 1)
        self._mark_dirty(True)
        self.status("New profile '%s' (not saved yet)" % p.name)

    def _on_dup_profile(self, *_):
        p = copy.deepcopy(self.device)
        p.name = unique_name(p.name + " copy", [q.name for q in self.profiles])
        self.profiles.append(p)
        self._select_profile(len(self.profiles) - 1)
        self._mark_dirty(True)
        self.status("Duplicated profile as '%s' (not saved yet)" % p.name)

    def _on_del_profile(self, *_):
        if len(self.profiles) <= 1:
            self.message("Cannot delete", "At least one device profile is required.", Gtk.MessageType.INFO)
            return
        if not self.confirm("Delete device profile?", "Delete '%s'? This takes effect when you press Save." % self.device.name):
            return
        del self.profiles[self.dev_index]
        self._select_profile(max(0, self.dev_index - 1))
        self._mark_dirty(True)

    def _on_save_profiles(self, *_):
        self.save_profiles()

    def save_profiles(self, quiet=False):
        try:
            try:
                self.m.devices.save_profiles(self.profiles, selected=self.device.name)
            except TypeError:  # contract signature: save_profiles(profiles)
                self.m.devices.save_profiles(self.profiles)
        except Exception as e:  # noqa: BLE001
            log.exception("save_profiles failed")
            self.status("Could not save device profiles: %s" % e, "error")
            if not quiet:
                self.message("Could not save device profiles", str(e))
            return False
        self._mark_dirty(False)
        if not quiet:
            self.status("Device profiles saved", "ok")
        return True

    def _on_import_inkcut(self, *_):
        try:
            imported = list(self.m.devices.import_inkcut_devices())
        except Exception as e:  # noqa: BLE001
            log.exception("import_inkcut_devices failed")
            self.message("Import from InkCut failed", str(e))
            return
        if not imported:
            self.message("Import from InkCut", "No devices were found in the InkCut configuration.", Gtk.MessageType.INFO)
            return
        for p in imported:
            p.name = unique_name(p.name, [q.name for q in self.profiles])
            self.profiles.append(p)
        self._select_profile(len(self.profiles) - 1)
        self._mark_dirty(True)
        self.status("Imported %d device(s) from InkCut — press Save to keep them" % len(imported), "ok")

    # ------------------------------------------------------------------ connection callbacks
    def _conn_set(self, key, value):
        setattr(self.device.connection, key, value)
        self._dev_changed(rebuild=False)

    def _on_conn_kind(self, cb):
        kind = cb.get_active_id() or "serial"
        self.conn_stack.set_visible_child_name(kind)
        if not self._updating:
            self._conn_set("kind", kind)

    def _on_conn_entry(self, entry, key):
        if key == "port":
            self._update_port_desc()
        if self._updating:
            return
        self._conn_set(key, entry.get_text().strip())

    def _on_port_combo(self, cb):
        pass  # the entry's 'changed' handler stores the value

    def _on_baud(self, entry):
        if self._updating:
            return
        txt = entry.get_text().strip()
        try:
            v = int(txt)
            if v <= 0:
                raise ValueError
        except ValueError:
            entry.get_style_context().add_class("error")
            return
        entry.get_style_context().remove_class("error")
        self._conn_set("baudrate", v)

    def _on_conn_combo(self, cb, key):
        if self._updating:
            return
        v = cb.get_active_id()
        if v is None:
            return
        if key == "bytesize":
            v = int(v)
        elif key == "stopbits":
            v = stopbits_value(v)
        self._conn_set(key, v)

    def _on_conn_spin(self, sb, key):
        if self._updating:
            return
        v = sb.get_value()
        self._conn_set(key, float(v) if key == "timeout_s" else int(round(v)))

    def _refresh_ports(self, *_):
        cur = self.cw["port"].get_child().get_text()
        self._updating = True
        try:
            self._fill_ports()
            self.cw["port"].get_child().set_text(cur)
        finally:
            self._updating = False
        self._update_port_desc()
        self.status("Found %d serial port(s)" % len(self._ports))

    def _refresh_printers(self, *_):
        cur = self.cw["printer_name"].get_child().get_text()
        self._updating = True
        try:
            self._fill_printers()
            self.cw["printer_name"].get_child().set_text(cur)
        finally:
            self._updating = False

    def _on_browse_file(self, *_):
        path = self._file_dialog("Output file", self.cw["file_path"].get_text() or "cutter-output.plt")
        if path:
            self.cw["file_path"].set_text(path)

    def _on_test_connection(self, *_):
        conn = copy.deepcopy(self.device.connection)
        btn, res = self.cw["test"], self.cw["test_result"]
        btn.set_sensitive(False)
        res.set_text("Testing…")

        def work():
            try:
                t = self.m.transports.make_transport(conn)
                t.open()
                t.close()
                GLib.idle_add(done, None)
            except Exception as e:  # noqa: BLE001
                log.exception("test connection failed")
                GLib.idle_add(done, e)

        def done(err):
            btn.set_sensitive(True)
            if err is None:
                res.set_markup("<span foreground='#2e7d32'><b>Connection OK</b></span> (opened and closed)")
                self.status("Connection test OK", "ok")
            else:
                res.set_markup("<span foreground='#c62828'><b>Failed:</b></span> %s" % GLib.markup_escape_text(str(err)))
                self.status("Connection test failed: %s" % err, "error")
            return False

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ preview callbacks
    def _on_preview_toggle(self, b, key):
        self.preview.set_toggles(**{key: b.get_active()})
        self.gstate.setdefault("toggles", {})[key] = bool(b.get_active())

    def _on_simulate(self, b):
        if self._updating:
            return
        if b.get_active():
            est = float(self.stats.get("est_time_s") or 0)
            dur = max(4.0, min(30.0, est / 8.0 if est else 6.0))
            if not self.preview.start_simulation(dur):
                self._set_sim_button(False)
        else:
            self.preview.stop_simulation(notify=False)

    def _set_sim_button(self, active):
        self._updating = True
        try:
            self.sim_button.set_active(active)
        finally:
            self._updating = False

    def _on_sim_finished(self):
        self._set_sim_button(False)

    # ------------------------------------------------------------------ rebuild
    def schedule_rebuild(self, delay_ms=100):
        if self._rebuild_id:
            GLib.source_remove(self._rebuild_id)
        self._rebuild_id = GLib.timeout_add(delay_ms, self._rebuild_cb)

    def _rebuild_cb(self):
        self._rebuild_id = 0
        self.rebuild()
        return False

    def flush(self):
        """Apply any pending (debounced) rebuild now."""
        if self._rebuild_id:
            GLib.source_remove(self._rebuild_id)
            self._rebuild_id = 0
            self.rebuild()

    def rebuild(self):
        if self._rebuild_id:
            GLib.source_remove(self._rebuild_id)
            self._rebuild_id = 0
        if self.preview.simulating:
            self.preview.stop_simulation()
        m, s, dev = self.m, self.settings, self.device
        try:
            # the Job caches results: give it private copies so later UI edits cannot leak in
            job = m.job.Job(self.polylines, copy.deepcopy(s), copy.deepcopy(dev))
            one = job.copy_geometry()
            b1 = m.geometry.bbox(one)
            self._copy_size = (b1[2] - b1[0], b1[3] - b1[1])
            lay = job.layout()
            paths = job.preview_paths()
            warns = list(job.warnings())
            stats = dict(job.stats())
            copies = lay[:-1] if (s.weed_border and lay) else lay
            weed = lay[-1][0] if (s.weed_border and lay and lay[-1]) else None
            boxes = [m.geometry.bbox(c) for c in copies if c]
            try:
                travel = m.geometry.travel_moves(paths, (0.0, 0.0))
            except Exception:  # noqa: BLE001
                travel = None
            scene = Scene(width_mm=dev.width_mm, length_mm=dev.length_mm, padding=dev.padding,
                          paths=paths, copy_boxes=boxes, weed=weed, travel=travel, warnings=warns,
                          passes=s.passes, pass_mode=s.pass_mode)
        except Exception as e:  # noqa: BLE001
            log.exception("rebuilding the job failed")
            self._last_error = traceback.format_exc()
            self.status("Could not build the job: %s" % e, "error")
            self.warn_label.set_text("Error: %s" % e)
            return False
        self.job, self.scene, self.stats = job, scene, stats
        self.preview.set_scene(scene)
        self._update_summary(stats, warns)
        self._updating = True
        try:
            self.w["size_w"].set_value(round(self._copy_size[0], 2))
            self.w["size_h"].set_value(round(self._copy_size[1], 2))
        finally:
            self._updating = False
        k = max(1e-9, float(s.scale_pct) / 100.0)
        self.w["size_note"].set_text("One copy: %.1f × %.1f mm (%.1f × %.1f mm at 100 %%, after rotation)"
                                     % (self._copy_size[0], self._copy_size[1],
                                        self._copy_size[0] / k, self._copy_size[1] / k))
        n = stats.get("copies", len(copies))
        rows, cols = stats.get("rows", s.rows), stats.get("cols", s.cols)
        if s.auto_fill:
            self.w["copies_note"].set_text("Fill material: %d rows × %d columns = %d copies fit" % (rows, cols, n))
        else:
            self.w["copies_note"].set_text("%d rows × %d columns = %d copies" % (rows, cols, n))
        self.btn_send.set_sensitive(not self._busy and bool(paths))
        self.btn_save.set_sensitive(not self._busy and bool(paths))
        if self.persist:
            try:
                m.job.save_last_settings(s)
            except Exception:  # noqa: BLE001
                log.exception("save_last_settings failed")
        return True

    def _update_summary(self, st, warns):
        L = self.stat_labels
        L["copies"].set_text(str(st.get("copies", "–")))
        L["paths"].set_text(str(st.get("paths", "–")))
        size = st.get("size_mm") or (0, 0)
        L["size"].set_text("%.1f × %.1f mm" % (float(size[0]), float(size[1])))
        cut = st.get("cut_length_mm", 0)
        passes = int(st.get("passes", self.settings.passes) or 1)
        if passes > 1:
            total = st.get("total_cut_length_mm", cut * passes)
            L["cut"].set_text("%s × %d = %s" % (format_length(cut), passes, format_length(total)))
        else:
            L["cut"].set_text(format_length(cut))
        L["travel"].set_text(format_length(st.get("travel_length_mm", 0)))
        L["time"].set_text(format_duration(st.get("est_time_s", 0)))
        if warns:
            self.warn_label.set_text("\n".join("• " + w for w in warns))
        else:
            self.warn_label.set_text("")

    # ------------------------------------------------------------------ status / dialogs
    def status(self, text, kind="info"):
        color = {"error": "#c62828", "ok": "#2e7d32"}.get(kind)
        esc = GLib.markup_escape_text(str(text))
        self.status_label.set_markup("<span foreground='%s'>%s</span>" % (color, esc) if color else esc)
        if kind == "error":
            log.warning(text)

    def message(self, title, text, mtype=Gtk.MessageType.ERROR):
        if self.window is None:
            log.info("%s: %s", title, text)
            return
        dlg = Gtk.MessageDialog(transient_for=self.window, modal=True, message_type=mtype,
                                buttons=Gtk.ButtonsType.OK, text=title)
        dlg.format_secondary_text(str(text))
        dlg.run()
        dlg.destroy()

    def confirm(self, title, text, ok_label="OK"):
        if self.window is None:
            return True
        dlg = Gtk.MessageDialog(transient_for=self.window, modal=True, message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.NONE, text=title)
        dlg.format_secondary_text(str(text))
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dlg.add_button(ok_label, Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.CANCEL)
        r = dlg.run()
        dlg.destroy()
        return r == Gtk.ResponseType.OK

    def _file_dialog(self, title, suggested, filters=(("Plotter files (*.plt, *.hpgl)", ("*.plt", "*.hpgl")),)):
        dlg = Gtk.FileChooserDialog(title=title, transient_for=self.window, action=Gtk.FileChooserAction.SAVE)
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dlg.add_button("Save", Gtk.ResponseType.ACCEPT)
        dlg.set_do_overwrite_confirmation(True)
        folder, base = os.path.split(suggested)
        if folder and os.path.isdir(folder):
            dlg.set_current_folder(folder)
        dlg.set_current_name(base or "output.plt")
        for name, pats in filters:
            flt = Gtk.FileFilter()
            flt.set_name(name)
            for p in pats:
                flt.add_pattern(p)
            dlg.add_filter(flt)
        flt = Gtk.FileFilter()
        flt.set_name("All files")
        flt.add_pattern("*")
        dlg.add_filter(flt)
        path = dlg.get_filename() if dlg.run() == Gtk.ResponseType.ACCEPT else None
        dlg.destroy()
        return path

    # ------------------------------------------------------------------ actions
    def _set_busy(self, busy):
        self._busy = busy
        has = bool(self.scene and self.scene.paths)
        self.btn_send.set_sensitive(not busy and has)
        self.btn_save.set_sensitive(not busy and has)
        self.btn_test.set_sensitive(not busy)
        self.btn_stop.set_sensitive(busy)
        self.dw["profile"].set_sensitive(not busy)

    def _on_send(self, *_):
        if self._busy:
            return
        self.flush()
        job = self.job
        if job is None:
            self.message("Cannot send", "The job could not be built. %s" % (self._last_error or ""))
            return
        try:
            paths = job.toolpaths()
            warns = list(job.warnings())
            speed, force = job.effective("speed"), job.effective("force")
        except Exception as e:  # noqa: BLE001
            log.exception("preparing the job failed")
            self.message("Cannot send", str(e))
            return
        if not paths:
            self.message("Nothing to cut", "The job contains no paths.", Gtk.MessageType.INFO)
            return
        if warns and not self.confirm("The job has warnings",
                                      "\n".join("• " + w for w in warns) + "\n\nSend to the cutter anyway?",
                                      "Send anyway"):
            self.status("Sending cancelled")
            return
        if self.device_dirty:
            self.save_profiles(quiet=True)
        self.preview.stop_simulation(notify=True)
        self._start_send(paths, speed, force, "Cutting", show_on_preview=True)

    def _on_test_cut(self, *_):
        if self._busy:
            return
        self.flush()
        m, s, dev = self.m, self.settings, self.device
        try:
            shape = m.geometry.test_cut_shape()
            pad = tuple(dev.padding or (0, 0, 0, 0)) + (0, 0, 0, 0)
            shape = m.geometry.translate(shape, s.offset_x + pad[0], s.offset_y + pad[1])
            job = self.job or m.job.Job(self.polylines, s, dev)
            ov, bo = job.effective("overcut"), job.effective("blade_offset")
            if ov:
                shape = m.geometry.apply_overcut(shape, ov)
            if bo:
                shape = m.geometry.apply_blade_offset(shape, bo)
            speed, force = job.effective("speed"), job.effective("force")
        except Exception as e:  # noqa: BLE001
            log.exception("test cut preparation failed")
            self.message("Test cut failed", str(e))
            return
        if not self.confirm("Test cut", "Cut a test shape at x %.1f mm, y %.1f mm with speed %g cm/s and force %g g?\n\n"
                            "Make sure the area is free (move the job offset if necessary)."
                            % (s.offset_x, s.offset_y, speed, force), "Cut test"):
            return
        if self.device_dirty:
            self.save_profiles(quiet=True)
        self._start_send(shape, speed, force, "Test cut", show_on_preview=False)

    def _on_save_plt(self, *_):
        self.flush()
        job = self.job
        if job is None:
            return
        stem = os.path.splitext(os.path.basename(self.design.get("name") or "job"))[0] or "job"
        proto = self.m.protocols.PROTOCOLS.get(self.device.protocol)
        ext = str(getattr(proto, "extension", "plt") or "plt")
        path = self._file_dialog("Save cutter file", os.path.join(self.gstate.get("plt_dir", ""), stem + "." + ext))
        if not path:
            return
        try:
            data = self.m.protocols.encode(job.toolpaths(), self.device, job.effective("speed"), job.effective("force"))
            with open(path, "wb") as f:
                f.write(data)
        except Exception as e:  # noqa: BLE001
            log.exception("save .plt failed")
            self.message("Could not save the file", str(e))
            return
        self.gstate["plt_dir"] = os.path.dirname(path)
        self.status("Saved %s (%s)" % (path, format_bytes(len(data))), "ok")

    def _on_stop(self, *_):
        if self._cancel is not None:
            self._cancel.set()
            self.status("Stopping…")
        self.preview.stop_simulation(notify=True)

    def _start_send(self, paths, speed, force, label, show_on_preview):
        dev = copy.deepcopy(self.device)
        self._cancel = threading.Event()
        self._send_meta = {"label": label, "preview": show_on_preview,
                           "passes": int(self.settings.passes), "mode": self.settings.pass_mode}
        self._send_t0 = time.time()
        self._set_busy(True)
        self.progress.set_fraction(0.0)
        self.progress.set_text("%s: encoding…" % label)
        self.status("%s: preparing data for %s…" % (label, dev.name))
        threading.Thread(target=self._send_worker, args=(paths, dev, speed, force, self._cancel),
                         daemon=True).start()

    def _send_worker(self, paths, dev, speed, force, cancel):
        try:
            data = self.m.protocols.encode(paths, dev, speed, force)
            GLib.idle_add(self._on_send_progress, 0, len(data))
            tr = self.m.transports.make_transport(dev.connection)
            # on Stop, lift the blade (protocol specific) if the transport supports it
            if hasattr(tr, "cancel_data") and hasattr(self.m.protocols, "cancel_sequence"):
                try:
                    tr.cancel_data = self.m.protocols.cancel_sequence(dev) or tr.cancel_data
                except Exception:  # noqa: BLE001
                    pass
            tr.open()
            last = [-1.0]

            def prog(sent, total):
                frac = (sent / float(total)) if total else 1.0
                if frac - last[0] >= 0.004 or sent >= total:
                    last[0] = frac
                    GLib.idle_add(self._on_send_progress, sent, total)

            try:
                tr.write(data, progress=prog, cancel_event=cancel)
            finally:
                tr.close()
            GLib.idle_add(self._on_send_done, None, cancel.is_set(), len(data))
        except Exception as e:  # noqa: BLE001
            log.exception("sending failed")
            GLib.idle_add(self._on_send_done, e, cancel.is_set(), 0)

    def _on_send_progress(self, sent, total):
        meta = self._send_meta or {}
        frac = (sent / float(total)) if total else 0.0
        self.progress.set_fraction(frac)
        self.progress.set_text("%s %d%%  (%s / %s)" % (meta.get("label", ""), round(frac * 100),
                                                        format_bytes(sent), format_bytes(total)))
        if meta.get("preview"):
            tl, pass_no = progress_to_timeline(frac, meta.get("passes", 1), meta.get("mode"))
            label = "Sending" + (" – pass %d/%d" % (pass_no, meta["passes"]) if pass_no else "")
            self.preview.set_progress(tl, label)
        return False

    def _on_send_done(self, err, cancelled, nbytes):
        meta = self._send_meta or {}
        label = meta.get("label", "Job")
        self._set_busy(False)
        self._cancel = None
        dt = time.time() - self._send_t0
        if err is not None:
            self.progress.set_text("%s failed" % label)
            self.preview.set_progress(None)
            self.status("%s failed: %s" % (label, err), "error")
            self.message("%s failed" % label, "%s\n\nCheck the Connection tab (port, baud rate, flow control) "
                         "and that the cutter is switched on and online." % err)
        elif cancelled:
            self.progress.set_text("Stopped")
            self.preview.set_progress(None)
            self.status("Stopped by user. The cutter may still finish commands already in its buffer.")
        else:
            self.progress.set_fraction(1.0)
            self.progress.set_text("%s: done" % label)
            self.status("%s sent: %s in %s" % (label, format_bytes(nbytes), format_duration(dt)), "ok")
            if meta.get("preview"):
                self.preview.set_progress(1.0, "Sent")
                GLib.timeout_add(2500, self._clear_progress)
        return False

    def _clear_progress(self):
        if not self._busy:
            self.preview.set_progress(None)
        return False

    # ------------------------------------------------------------------ window lifecycle
    def attach_window(self, win):
        self.window = win
        win.connect("delete-event", self._on_delete)
        win.connect("key-press-event", self._on_key)

    def _on_key(self, _w, ev):
        ctrl = bool(ev.state & Gdk.ModifierType.CONTROL_MASK)
        if ctrl and ev.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._on_send()
            return True
        if ctrl and ev.keyval in (Gdk.KEY_s, Gdk.KEY_S):
            self._on_save_plt()
            return True
        if ev.keyval == Gdk.KEY_Escape:
            if self._busy:
                self._on_stop()
                return True
            if self.preview.simulating:
                self.preview.stop_simulation(notify=True)
                return True
        return False

    def _on_delete(self, win, _ev):
        if self._busy and not self.confirm("A job is being sent", "Stop sending and close InkCut2?", "Stop and close"):
            return True
        if self._cancel is not None:
            self._cancel.set()
        if self.device_dirty and self.window is not None:
            dlg = Gtk.MessageDialog(transient_for=self.window, modal=True, message_type=Gtk.MessageType.QUESTION,
                                    buttons=Gtk.ButtonsType.NONE, text="Save device profile changes?")
            dlg.add_button("Discard", Gtk.ResponseType.NO)
            dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
            dlg.add_button("Save", Gtk.ResponseType.YES)
            r = dlg.run()
            dlg.destroy()
            if r == Gtk.ResponseType.CANCEL or r == Gtk.ResponseType.DELETE_EVENT:
                return True
            if r == Gtk.ResponseType.YES and not self.save_profiles():
                return True
        if self.persist:
            self.flush()
            try:
                if not win.is_maximized():
                    self.gstate["window"] = list(win.get_size())
                self.gstate["maximized"] = bool(win.is_maximized())
                self.gstate["paned"] = self.paned.get_position()
            except Exception:  # noqa: BLE001
                pass
            self.gstate["device"] = self.device.name
            save_gui_state(self.gstate)
        Gtk.main_quit()
        return False


# ----------------------------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------------------------
def _setup_logging():
    path = os.path.join(tempfile.gettempdir(), "inkcut2-gui.log")
    handlers = []
    try:
        handlers.append(logging.FileHandler(path, encoding="utf-8"))
    except OSError:
        pass
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.INFO, handlers=handlers or [logging.NullHandler()],
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def error_dialog(title, text):
    log.error("%s: %s", title, text)
    try:
        dlg = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.OK, text=title)
        dlg.set_title(APP_TITLE)
        dlg.format_secondary_text(str(text))
        dlg.run()
        dlg.destroy()
    except Exception:  # noqa: BLE001
        pass


def _out(*a):
    if sys.stdout is not None:
        print(*a)
        sys.stdout.flush()


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="python -m inkcut2.gui", description="InkCut2 cutting window")
    ap.add_argument("design", nargs="?", help="design JSON written by the Inkscape extension")
    ap.add_argument("--demo", action="store_true", help="open with a built-in sample design")
    ap.add_argument("--snapshot", metavar="PNG", help="render the window offscreen to PNG and exit")
    ap.add_argument("--width", type=int, default=1200)
    ap.add_argument("--height", type=int, default=800)
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="override a job setting, e.g. --set rotation=90 --set rows=3")
    ap.add_argument("--tab", choices=TABS, default=None, help="notebook page to show")
    ap.add_argument("--view", choices=["auto", "material", "job"], default="auto",
                    help="initial preview zoom: auto (origin + job), material (full width), job")
    ap.add_argument("--sim", type=float, default=None, help="snapshot: draw simulation progress (0..1)")
    ap.add_argument("--device", default=None, help="device profile name to select")
    ap.add_argument("--units", choices=list(AREA_UNITS), default=None,
                    help="display unit for the material / cutting area and preview rulers")
    ap.add_argument("--auto-quit", type=float, default=None, metavar="SECONDS", help="close the window after N seconds")
    return ap.parse_args(argv)


def apply_overrides(settings, pairs):
    for item in pairs:
        if "=" not in item:
            raise ValueError("--set expects KEY=VALUE, got %r" % item)
        k, v = item.split("=", 1)
        k = k.strip()
        if not hasattr(settings, k):
            raise ValueError("unknown job setting %r" % k)
        setattr(settings, k, coerce_value(v.strip()))
    return settings


def _pump(seconds=0.5):
    end = time.time() + seconds
    while time.time() < end:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        time.sleep(0.02)


def snapshot(app, args):
    out = os.path.abspath(args.snapshot)
    stem, ext = os.path.splitext(out)
    preview_out = stem + "_preview" + (ext or ".png")
    W.install_css()
    full_ok = False
    try:
        win = Gtk.OffscreenWindow()
        win.set_default_size(args.width, args.height)
        app.root.set_size_request(args.width, args.height)
        win.add(app.root)
        win.show_all()
        if args.tab:
            app.notebook.set_current_page(TABS.index(args.tab))
        app.paned.set_position(max(300, args.width - RIGHT_PANE))
        app.preview.fit(args.view)
        if args.sim is not None:
            app.preview.set_progress(args.sim, "Simulating")
        _pump(0.6)
        app.preview.fit(args.view)
        _pump(0.3)
        pix = win.get_pixbuf()
        if pix is not None:
            pix.savev(out, "png", [], [])
            full_ok = True
    except Exception:  # noqa: BLE001
        log.exception("offscreen window snapshot failed; falling back to preview only")
    app.preview.render_png(preview_out, args.width, args.height, fit=args.view)
    if not full_ok:
        app.preview.render_png(out, args.width, args.height, fit=args.view)
    st = app.stats
    _out("snapshot: %s" % out)
    _out("preview:  %s" % preview_out)
    _out("stats: %s" % json.dumps({k: v for k, v in st.items()}, default=str))
    _out("warnings: %s" % json.dumps(app.scene.warnings if app.scene else []))
    return 0


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    _setup_logging()
    log.info("InkCut2 GUI starting: %s", " ".join(sys.argv[1:] if argv is None else argv))
    headless = bool(args.snapshot)

    def fail(title, text, code=2):
        if headless:
            _out("ERROR: %s: %s" % (title, text))
            log.error("%s: %s", title, text)
        else:
            error_dialog(title, text)
        return code

    try:
        backend = load_backend()
    except Exception:  # noqa: BLE001
        return fail("InkCut2 could not start", "A program module failed to load:\n\n" + traceback.format_exc())

    try:
        if args.demo:
            design = demo_design()
        elif args.design:
            design = load_design(args.design)
        else:
            return fail("No design", "Start InkCut2 from Inkscape (Extensions > InkCut2) "
                                     "or pass a design JSON file / --demo.")
    except DesignError as e:
        return fail("Cannot open the design", str(e))

    try:
        settings = None
        if headless:
            settings = backend.job.JobSettings()
        elif args.set:
            settings = backend.job.load_last_settings()
        if args.set:
            apply_overrides(settings, args.set)
    except Exception as e:  # noqa: BLE001
        return fail("Invalid option", str(e))

    if headless:
        try:
            app = CutterApp(design, backend, settings=settings, persist=False, device_name=args.device)
            if args.units:
                app.set_area_unit(args.units)
            return snapshot(app, args)
        except Exception:  # noqa: BLE001
            return fail("Snapshot failed", traceback.format_exc(), 1)

    W.install_css()
    app_ref = {}

    def excepthook(etype, value, tb):
        log.error("unhandled exception", exc_info=(etype, value, tb))
        app = app_ref.get("app")
        if app is not None:
            try:
                app.status("Internal error: %s" % value, "error")
            except Exception:  # noqa: BLE001
                pass

    sys.excepthook = excepthook
    try:
        app = CutterApp(design, backend, settings=settings, persist=True, device_name=args.device)
    except Exception:  # noqa: BLE001
        return fail("InkCut2 could not start", traceback.format_exc(), 1)
    app_ref["app"] = app
    win = Gtk.Window(title="%s — %s" % (APP_TITLE, design.get("name") or "design"))
    try:
        win.set_icon_name("printer")
    except Exception:  # noqa: BLE001
        pass
    size = app.gstate.get("window") or [1280, 820]
    try:
        win.set_default_size(int(size[0]), int(size[1]))
    except (TypeError, ValueError, IndexError):
        win.set_default_size(1280, 820)
    win.add(app.root)
    app.attach_window(win)
    win.show_all()
    if app.gstate.get("maximized"):
        win.maximize()
    pos = app.gstate.get("paned")
    w_now = win.get_size()[0]
    app.paned.set_position(int(pos) if isinstance(pos, int) and 300 < pos < w_now - 300 else max(300, w_now - RIGHT_PANE))
    if args.tab:
        app.notebook.set_current_page(TABS.index(args.tab))
    app.preview.fit(args.view)
    app.status("%d path(s) loaded from %s" % (len(design["polylines"]), design.get("name")))
    if args.auto_quit:
        GLib.timeout_add(int(args.auto_quit * 1000), lambda: (Gtk.main_quit(), False)[1])
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
