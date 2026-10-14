"""Cut preview widget for the InkCut2 window (GTK 3 DrawingArea + cairo).

Draws everything in *material coordinates* (mm): x = along the media feed (to the
right), y = across the media width (upwards), origin (0, 0) = cutter origin.

The widget is fed a :class:`Scene` (plain data computed by gui.py from a Job) and
never imports the other inkcut2 modules, so it can be rendered offscreen
(``render()``/``render_png()``) for automated snapshots.
"""

import bisect
import math

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

import cairo  # noqa: E402

FONT = "Segoe UI"
RULER = 22  # px, ruler band thickness
STRIP = 24  # px, bottom status strip (cursor position + legend)

# palette -----------------------------------------------------------------------------
C_BG = (0.86, 0.87, 0.89)
C_MATERIAL = (1.0, 1.0, 1.0)
C_MARGIN = (0.55, 0.57, 0.62, 0.22)
C_GRID_MINOR = (0.92, 0.93, 0.95)
C_GRID_MAJOR = (0.80, 0.83, 0.87)
C_CLOSED = (0.12, 0.30, 0.66)
C_OPEN = (0.88, 0.47, 0.08)
C_WEED = (0.10, 0.58, 0.32)
C_OOB = (0.86, 0.12, 0.12)
C_TRAVEL = (0.45, 0.47, 0.52)
C_BOX = (0.0, 0.55, 0.62)
C_DONE = (0.62, 0.18, 0.80)
C_KNIFE = (0.85, 0.05, 0.25)
C_ORIGIN = (0.85, 0.15, 0.15)
C_TEXT = (0.20, 0.22, 0.25)

NICE_STEPS = [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000]
# inch grid: binary fractions, then feet / yards
NICE_STEPS_IN = [1 / 32, 1 / 16, 1 / 8, 1 / 4, 1 / 2, 1, 2, 3, 6, 12, 24, 36, 60, 120, 240, 480]

# display units for rulers / labels: mm per unit (all geometry stays in mm)
UNITS = {"mm": 1.0, "in": 25.4}


def fmt_len(mm, unit="mm"):
    """Material length for captions: '610 mm' or '24.02 in'."""
    if unit == "in":
        return "%s in" % ("%.2f" % (mm / 25.4)).rstrip("0").rstrip(".")
    return "%g mm" % round(mm, 2)


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _bbox(polys):
    xs = [p[0] for pl in polys for p in pl]
    ys = [p[1] for pl in polys for p in pl]
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _is_closed(poly, tol=0.02):
    """Closed contour test that also recognises closed paths extended by an overcut.

    A plain closed polyline ends on its first point. After geometry.apply_overcut the path
    continues past its start, so we also accept a path that *revisits* its first vertex later on.
    """
    n = len(poly)
    if n < 3:
        return False
    x0, y0 = poly[0]
    if abs(x0 - poly[-1][0]) < 1e-6 and abs(y0 - poly[-1][1]) < 1e-6:
        return True
    for k in range(2, n):
        if abs(poly[k][0] - x0) < tol and abs(poly[k][1] - y0) < tol:
            return True
    return False


def snap(value, step):
    """Round *value* to a multiple of *step* (step <= 0 -> unchanged)."""
    if step <= 0:
        return value
    return round(round(value / step) * step, 6)


def nice_step(px_per_mm, min_px, unit="mm"):
    """Smallest 'nice' grid step whose on-screen spacing is >= min_px.

    Steps are nice in *unit* (e.g. 1/4 in) but returned in mm.
    """
    f = UNITS.get(unit, 1.0)
    steps = NICE_STEPS_IN if unit == "in" else NICE_STEPS
    for st in steps:
        if st * f * px_per_mm >= min_px:
            return st * f
    return steps[-1] * f


def minor_step(major, px_per_mm, min_px, unit="mm"):
    """Subdivision of *major* (mm) for minor ticks/grid, or None if too dense."""
    for div in ((4, 2) if unit == "in" else (5, 2)):
        if major / div * px_per_mm >= min_px:
            return major / div
    return None


class Scene:
    """Plain-data description of what the preview shows (all mm, material coords)."""

    def __init__(self, width_mm=610.0, length_mm=5000.0, padding=(0, 0, 0, 0), paths=None,
                 copy_boxes=None, weed=None, travel=None, warnings=None, fit_length=None,
                 passes=1, pass_mode="per_path", start=(0.0, 0.0)):
        self.width_mm = float(width_mm)
        self.length_mm = float(length_mm)
        pad = tuple(padding or (0, 0, 0, 0)) + (0, 0, 0, 0)
        self.padding = tuple(float(v) for v in pad[:4])  # left, bottom, right, top
        self.paths = [list(map(tuple, p)) for p in (paths or []) if len(p) >= 2]
        self.copy_boxes = list(copy_boxes or [])
        self.weed = weed
        self.warnings = list(warnings or [])
        self.passes = max(1, int(passes or 1))
        self.pass_mode = pass_mode
        self.start = tuple(start)
        self.closed = [_is_closed(p) for p in self.paths]
        self.travel = list(travel) if travel is not None else self._travel()
        self.job_bbox = _bbox(self.paths)
        self.oob = [self._out_of_bounds(p) for p in self.paths]
        self.is_weed = [self._matches_weed(p) for p in self.paths]
        if fit_length is None:
            jx = self.job_bbox[2] if self.job_bbox else 0.0
            fit_length = max(jx * 1.15 + 20.0, self.width_mm * 0.5, 100.0)
        self.fit_length = min(self.length_mm, fit_length) if self.length_mm > 0 else fit_length
        self._build_timeline()

    # ---- derived data ------------------------------------------------------------
    def usable_rect(self):
        pl, pb, pr, pt = self.padding
        return (pl, pb, self.length_mm - pr, self.width_mm - pt)

    def _out_of_bounds(self, poly, tol=1e-3):
        x0, y0, x1, y1 = self.usable_rect()
        for x, y in poly:
            if x < x0 - tol or y < y0 - tol or x > x1 + tol or y > y1 + tol:
                return True
        return False

    def _matches_weed(self, poly):
        if not self.weed:
            return False
        a, b = _bbox([poly]), _bbox([self.weed])
        if not a or not b:
            return False
        return all(abs(u - v) < 1.5 for u, v in zip(a, b))

    def _travel(self):
        out, cur = [], self.start
        for p in self.paths:
            out.append((cur, p[0]))
            cur = p[-1]
        return out

    def _build_timeline(self):
        """Cumulative distances (cut + travel) for simulation / progress display."""
        self.tl_start = []   # distance at which the knife reaches path i start
        self.tl_cums = []    # per path: cumulative vertex distances from its start
        self.tl_from = []    # where the travel move to path i begins
        d, cur = 0.0, self.start
        for p in self.paths:
            self.tl_from.append(cur)
            d += _dist(cur, p[0])
            self.tl_start.append(d)
            cums = [0.0]
            for a, b in zip(p, p[1:]):
                cums.append(cums[-1] + _dist(a, b))
            self.tl_cums.append(cums)
            d += cums[-1]
            cur = p[-1]
        self.tl_total = d
        self.cut_total = sum(c[-1] for c in self.tl_cums)

    def state_at(self, frac):
        """(done_count, partial_index, partial_len, knife_point, in_travel) at timeline frac."""
        if not self.paths or self.tl_total <= 0:
            return 0, None, 0.0, self.start, False
        D = max(0.0, min(1.0, frac)) * self.tl_total
        i = bisect.bisect_right(self.tl_start, D) - 1
        if i < 0:
            # still travelling to the first path
            a, b = self.tl_from[0], self.paths[0][0]
            seg = self.tl_start[0]
            t = D / seg if seg > 0 else 1.0
            return 0, None, 0.0, (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t), True
        local = D - self.tl_start[i]
        cums = self.tl_cums[i]
        if local >= cums[-1]:
            # path i finished; maybe travelling to i + 1
            if i + 1 < len(self.paths):
                a, b = self.paths[i][-1], self.paths[i + 1][0]
                seg = self.tl_start[i + 1] - (self.tl_start[i] + cums[-1])
                t = (local - cums[-1]) / seg if seg > 0 else 1.0
                return i + 1, None, 0.0, (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t), True
            return len(self.paths), None, 0.0, self.paths[-1][-1], False
        k = max(0, bisect.bisect_right(cums, local) - 1)
        k = min(k, len(cums) - 2)
        p = self.paths[i]
        seg = cums[k + 1] - cums[k]
        t = (local - cums[k]) / seg if seg > 0 else 0.0
        a, b = p[k], p[k + 1]
        return i, i, local, (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t), False


class PreviewArea(Gtk.DrawingArea):
    """Zoomable / pannable cut preview with draggable job placement."""

    def __init__(self):
        super().__init__()
        self.scene = Scene()
        self.scale = 1.0     # px per mm
        self.ox = 40.0       # screen x of material x=0
        self.oy = 400.0      # screen y of material y=0
        self._fit_pending = "auto"
        self._mat_key = None
        self.show_arrows = True
        self.show_order = True
        self.show_travel = True
        self.show_boxes = True
        self.snap_mm = 1.0
        self.unit = "mm"     # display unit for rulers, grid and readouts ("mm" | "in")
        # callbacks (set by owner)
        self.get_offset = None        # () -> (offset_x, offset_y)
        self.on_job_moved = None      # (new_offset_x, new_offset_y) -> None
        self.on_sim_finished = None   # () -> None
        self.on_cursor = None         # (x_mm, y_mm) | None -> None
        # interaction state
        self._drag = None             # dict when dragging the job
        self._pan = None              # (start_px, start_py, ox, oy)
        self.drag_delta = (0.0, 0.0)
        self.cursor_mm = None
        # progress / simulation
        self.progress = None          # timeline fraction 0..1 or None
        self.progress_label = ""
        self._sim_source = 0
        self._sim_t0 = 0
        self._sim_duration = 5.0
        self._size = (800, 600)

        self.set_can_focus(True)
        self.set_size_request(420, 320)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                        | Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.SCROLL_MASK
                        | Gdk.EventMask.SMOOTH_SCROLL_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.connect("draw", self._on_draw)
        self.connect("button-press-event", self._on_press)
        self.connect("button-release-event", self._on_release)
        self.connect("motion-notify-event", self._on_motion)
        self.connect("scroll-event", self._on_scroll)
        self.connect("leave-notify-event", self._on_leave)

    # ---- public API ----------------------------------------------------------------
    def set_unit(self, unit):
        """Show rulers, grid and readouts in "mm" or "in" (geometry is unaffected)."""
        self.unit = unit if unit in UNITS else "mm"
        self.queue_draw()

    def set_scene(self, scene):
        key = (scene.width_mm, scene.length_mm)
        if self._mat_key is not None and key != self._mat_key and not self._fit_pending:
            self._fit_pending = "auto"
        self._mat_key = key
        self.scene = scene
        self._drag = None
        self.drag_delta = (0.0, 0.0)
        self.queue_draw()

    def set_toggles(self, arrows=None, order=None, travel=None, boxes=None):
        if arrows is not None:
            self.show_arrows = bool(arrows)
        if order is not None:
            self.show_order = bool(order)
        if travel is not None:
            self.show_travel = bool(travel)
        if boxes is not None:
            self.show_boxes = bool(boxes)
        self.queue_draw()

    def fit(self, mode="material"):
        """mode: 'material' (media width + job) or 'job' (zoom to the job)."""
        self._fit_pending = mode
        self.queue_draw()

    def zoom(self, factor, cx=None, cy=None):
        w, h = self._size
        cx = w / 2.0 if cx is None else cx
        cy = h / 2.0 if cy is None else cy
        new = max(0.02, min(400.0, self.scale * factor))
        f = new / self.scale
        self.ox = cx - (cx - self.ox) * f
        self.oy = cy - (cy - self.oy) * f
        self.scale = new
        self.queue_draw()

    def set_progress(self, frac, label=""):
        self.progress = None if frac is None else max(0.0, min(1.0, frac))
        self.progress_label = label
        self.queue_draw()

    def start_simulation(self, duration_s=6.0):
        self.stop_simulation(notify=False)
        if not self.scene.paths:
            return False
        self._sim_duration = max(0.5, float(duration_s))
        self._sim_t0 = GLib.get_monotonic_time()
        self.progress = 0.0
        self.progress_label = "Simulating"
        self._sim_source = GLib.timeout_add(30, self._sim_tick)
        return True

    def stop_simulation(self, notify=True):
        was = bool(self._sim_source)
        if self._sim_source:
            GLib.source_remove(self._sim_source)
            self._sim_source = 0
            self.progress = None
            self.progress_label = ""
            self.queue_draw()
        if was and notify and self.on_sim_finished:
            self.on_sim_finished()

    @property
    def simulating(self):
        return bool(self._sim_source)

    def _sim_tick(self):
        el = (GLib.get_monotonic_time() - self._sim_t0) / 1e6
        frac = el / self._sim_duration
        if frac >= 1.0:
            self.progress = 1.0
            self.queue_draw()
            self._sim_source = 0
            GLib.timeout_add(700, self._sim_clear)
            return False
        self.progress = frac
        self.progress_label = "Simulating"
        self.queue_draw()
        return True

    def _sim_clear(self):
        if not self._sim_source and self.progress_label == "Simulating":
            self.progress = None
            self.progress_label = ""
            self.queue_draw()
            if self.on_sim_finished:
                self.on_sim_finished()
        return False

    # ---- coordinate helpers --------------------------------------------------------
    def sx(self, x):
        return self.ox + x * self.scale

    def sy(self, y):
        return self.oy - y * self.scale

    def to_mm(self, px, py):
        return ((px - self.ox) / self.scale, (self.oy - py) / self.scale)

    def _do_fit(self, w, h, mode):
        sc = self.scene
        if mode == "job" and sc.job_bbox:
            x0, y0, x1, y1 = sc.job_bbox
            mx = max(5.0, (x1 - x0) * 0.08)
            my = max(5.0, (y1 - y0) * 0.08)
            x0, y0, x1, y1 = x0 - mx, y0 - my, x1 + mx, y1 + my
        elif mode == "auto" and sc.job_bbox:
            # origin + job with some surrounding material (default view)
            jx0, jy0, jx1, jy1 = sc.job_bbox
            x0, y0 = min(0.0, jx0), min(0.0, jy0)
            x1 = max(jx1 * 1.2, jx1 + 25.0)
            y1 = max(jy1 * 1.2, jy1 + 25.0)
            if sc.length_mm > 0 and jx1 <= sc.length_mm:
                x1 = min(x1, sc.length_mm)
            if jy1 <= sc.width_mm:
                y1 = min(y1, sc.width_mm)
        else:
            x0, y0, x1, y1 = 0.0, 0.0, sc.fit_length, sc.width_mm
            if sc.job_bbox:
                jx0, jy0, jx1, jy1 = sc.job_bbox
                x0, y0 = min(x0, jx0), min(y0, jy0)
                x1, y1 = max(x1, jx1), max(y1, jy1)
        ml, mr, mt, mb = RULER + 34, 24, RULER + 40, STRIP + 50
        aw, ah = max(10, w - ml - mr), max(10, h - mt - mb)
        dw, dh = max(1e-3, x1 - x0), max(1e-3, y1 - y0)
        self.scale = max(0.02, min(400.0, min(aw / dw, ah / dh)))
        self.ox = ml + (aw - dw * self.scale) / 2.0 - x0 * self.scale
        self.oy = mt + (ah + dh * self.scale) / 2.0 + y0 * self.scale

    # ---- events --------------------------------------------------------------------
    def _job_hit(self, px, py, tol=6):
        bb = self.scene.job_bbox
        if not bb:
            return False
        x0, y0 = self.sx(bb[0]) - tol, self.sy(bb[3]) - tol
        x1, y1 = self.sx(bb[2]) + tol, self.sy(bb[1]) + tol
        return x0 <= px <= x1 and y0 <= py <= y1

    def _set_cursor(self, name):
        win = self.get_window()
        if win is None:
            return
        cur = Gdk.Cursor.new_from_name(win.get_display(), name) if name else None
        win.set_cursor(cur)

    def _on_press(self, _w, ev):
        self.grab_focus()
        if ev.type != Gdk.EventType.BUTTON_PRESS:
            return False
        if ev.button == 1 and self._job_hit(ev.x, ev.y) and self.get_offset and self.progress is None:
            off = self.get_offset()
            self._drag = {"px": ev.x, "py": ev.y, "off": (float(off[0]), float(off[1])),
                          "new": (float(off[0]), float(off[1]))}
            self._set_cursor("grabbing")
            return True
        if ev.button in (1, 2, 3):
            self._pan = (ev.x, ev.y, self.ox, self.oy)
            self._set_cursor("grabbing")
            return True
        return False

    def _on_release(self, _w, ev):
        if self._drag is not None:
            d = self._drag
            moved = d["new"] != d["off"]
            self._set_cursor("grab")
            if moved and self.on_job_moved:
                # keep drawing the dragged position until the owner sends a new scene
                self.on_job_moved(*d["new"])
            self._drag = None
            self.drag_delta = (0.0, 0.0)
            self.queue_draw()
            return True
        if self._pan is not None:
            self._pan = None
            self._set_cursor(None)
            return True
        return False

    def _on_motion(self, _w, ev):
        self.cursor_mm = self.to_mm(ev.x, ev.y)
        if self.on_cursor:
            self.on_cursor(self.cursor_mm)
        if self._drag is not None:
            d = self._drag
            dx = (ev.x - d["px"]) / self.scale
            dy = -(ev.y - d["py"]) / self.scale
            step = 0.1 if (ev.state & Gdk.ModifierType.SHIFT_MASK) else self.snap_mm
            nx = max(0.0, snap(d["off"][0] + dx, step))
            ny = max(0.0, snap(d["off"][1] + dy, step))
            d["new"] = (nx, ny)
            self.drag_delta = (nx - d["off"][0], ny - d["off"][1])
            self.queue_draw()
            return True
        if self._pan is not None:
            x0, y0, ox, oy = self._pan
            self.ox = ox + (ev.x - x0)
            self.oy = oy + (ev.y - y0)
            self.queue_draw()
            return True
        self._set_cursor("grab" if (self._job_hit(ev.x, ev.y) and self.progress is None) else None)
        self.queue_draw()
        return False

    def _on_leave(self, _w, _ev):
        self.cursor_mm = None
        if self.on_cursor:
            self.on_cursor(None)
        self.queue_draw()
        return False

    def _on_scroll(self, _w, ev):
        factor = 1.0
        if ev.direction == Gdk.ScrollDirection.UP:
            factor = 1.2
        elif ev.direction == Gdk.ScrollDirection.DOWN:
            factor = 1 / 1.2
        elif ev.direction == Gdk.ScrollDirection.SMOOTH:
            ok, _dx, dy = ev.get_scroll_deltas()
            if ok and dy:
                factor = 1.2 ** (-dy)
        if factor != 1.0:
            self.zoom(factor, ev.x, ev.y)
        return True

    def _on_draw(self, _w, cr):
        alloc = self.get_allocation()
        self.render(cr, alloc.width, alloc.height)
        return False

    # ---- offscreen ---------------------------------------------------------------
    def render_png(self, path, width, height, fit=None):
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(width), int(height))
        cr = cairo.Context(surf)
        if fit:
            self._fit_pending = fit
        self.render(cr, int(width), int(height))
        surf.write_to_png(path)
        return path

    # ---- rendering -----------------------------------------------------------------
    def render(self, cr, w, h):
        self._size = (w, h)
        if self._fit_pending:
            self._do_fit(w, h, self._fit_pending)
            self._fit_pending = None
        sc = self.scene
        cr.set_source_rgb(*C_BG)
        cr.paint()
        cr.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)

        x0m, y0m = self.to_mm(0, h)
        x1m, y1m = self.to_mm(w, 0)
        vis_len = min(sc.length_mm, max(sc.fit_length, x1m + 10)) if sc.length_mm > 0 else max(sc.fit_length, x1m)
        self._draw_material(cr, sc, vis_len)
        self._draw_grid(cr, sc, vis_len, x0m, y0m, x1m, y1m)
        self._draw_margins(cr, sc, vis_len)
        self._draw_origin(cr, sc)

        dx, dy = self.drag_delta
        cr.save()
        if self.show_boxes:
            self._draw_boxes(cr, sc, dx, dy)
        if self.show_travel and self.progress is None:
            self._draw_travel(cr, sc, dx, dy)
        self._draw_paths(cr, sc, dx, dy)
        cr.restore()

        self._draw_rulers(cr, w, h)
        self._draw_status_strip(cr, sc, w, h)
        self._draw_banner(cr, sc, w, h)
        if self._drag is not None:
            self._draw_drag_readout(cr)

    # material / grid -------------------------------------------------------------
    def _draw_material(self, cr, sc, vis_len):
        X0, X1 = self.sx(0), self.sx(vis_len)
        Y0, Y1 = self.sy(0), self.sy(sc.width_mm)
        # drop shadow
        cr.set_source_rgba(0, 0, 0, 0.12)
        cr.rectangle(X0 + 3, Y1 + 3, X1 - X0, Y0 - Y1)
        cr.fill()
        cr.set_source_rgb(*C_MATERIAL)
        cr.rectangle(X0, Y1, X1 - X0, Y0 - Y1)
        cr.fill()
        cr.set_line_width(1.0)
        cr.set_source_rgb(0.45, 0.47, 0.52)
        cr.move_to(X1, Y1)
        cr.line_to(X0, Y1)
        cr.line_to(X0, Y0)
        cr.line_to(X1, Y0)
        if vis_len >= sc.length_mm - 1e-6:
            cr.close_path()
        cr.stroke()
        if vis_len < sc.length_mm - 1e-6:
            # the roll continues: fade + jagged tear line
            g = cairo.LinearGradient(X1 - 40, 0, X1, 0)
            g.add_color_stop_rgba(0, C_BG[0], C_BG[1], C_BG[2], 0.0)
            g.add_color_stop_rgba(1, C_BG[0], C_BG[1], C_BG[2], 0.9)
            cr.set_source(g)
            cr.rectangle(X1 - 40, Y1, 40, Y0 - Y1)
            cr.fill()
        # media feed arrow + width label
        cr.set_source_rgb(*C_TEXT)
        ay = Y0 + 32
        cr.set_line_width(1.5)
        cr.move_to(X0, ay)
        cr.line_to(X0 + 70, ay)
        cr.stroke()
        cr.move_to(X0 + 76, ay)
        cr.line_to(X0 + 66, ay - 5)
        cr.line_to(X0 + 66, ay + 5)
        cr.close_path()
        cr.fill()
        u = self.unit
        roll = ("roll " if vis_len < sc.length_mm - 1e-6 else "length ") + fmt_len(sc.length_mm, u)
        self._text(cr, X0 + 84, ay + 4, "Feed direction (x)   |   material %s wide, %s" % (fmt_len(sc.width_mm, u), roll),
                   size=11, color=C_TEXT)

    def _draw_grid(self, cr, sc, vis_len, x0m, y0m, x1m, y1m):
        s = self.scale
        major = nice_step(s, 60, self.unit)
        minor = minor_step(major, s, 8, self.unit)
        gx0, gx1 = max(0.0, x0m), min(vis_len, x1m)
        gy0, gy1 = max(0.0, y0m), min(sc.width_mm, y1m)
        if gx1 <= gx0 or gy1 <= gy0:
            return
        cr.set_line_width(1.0)
        for step, col in ((minor, C_GRID_MINOR), (major, C_GRID_MAJOR)):
            if not step:
                continue
            cr.set_source_rgb(*col)
            i = math.ceil(gx0 / step)
            while i * step <= gx1 + 1e-9:
                X = round(self.sx(i * step)) + 0.5
                cr.move_to(X, self.sy(gy0))
                cr.line_to(X, self.sy(gy1))
                i += 1
            j = math.ceil(gy0 / step)
            while j * step <= gy1 + 1e-9:
                Y = round(self.sy(j * step)) + 0.5
                cr.move_to(self.sx(gx0), Y)
                cr.line_to(self.sx(gx1), Y)
                j += 1
            cr.stroke()

    def _draw_margins(self, cr, sc, vis_len):
        pl, pb, pr, pt = sc.padding
        if not any((pl, pb, pr, pt)):
            return
        cr.set_source_rgba(*C_MARGIN)
        W, L = sc.width_mm, sc.length_mm
        rects = []
        if pb > 0:
            rects.append((0, 0, vis_len, pb))
        if pt > 0:
            rects.append((0, W - pt, vis_len, W))
        if pl > 0:
            rects.append((0, pb, pl, W - pt))
        if pr > 0 and L - pr < vis_len:
            rects.append((L - pr, pb, min(vis_len, L), W - pt))
        for a, b, c, d in rects:
            cr.rectangle(self.sx(a), self.sy(d), (c - a) * self.scale, (d - b) * self.scale)
        cr.fill()
        # usable area outline
        x0, y0, x1, y1 = sc.usable_rect()
        x1 = min(x1, vis_len)
        cr.set_source_rgba(0.35, 0.38, 0.45, 0.8)
        cr.set_line_width(1.0)
        cr.set_dash([5, 4])
        cr.rectangle(self.sx(x0), self.sy(y1), (x1 - x0) * self.scale, (y1 - y0) * self.scale)
        cr.stroke()
        cr.set_dash([])

    def _draw_origin(self, cr, sc):
        X, Y = self.sx(0), self.sy(0)
        cr.set_source_rgb(*C_ORIGIN)
        cr.set_line_width(1.5)
        cr.arc(X, Y, 6, 0, 2 * math.pi)
        cr.stroke()
        cr.move_to(X - 11, Y)
        cr.line_to(X + 11, Y)
        cr.move_to(X, Y - 11)
        cr.line_to(X, Y + 11)
        cr.stroke()
        self._text(cr, X + 9, Y + 15, "origin", size=10, color=C_ORIGIN)

    # job -------------------------------------------------------------------------
    def _poly_path(self, cr, poly, dx=0.0, dy=0.0):
        it = iter(poly)
        x, y = next(it)
        cr.move_to(self.sx(x + dx), self.sy(y + dy))
        for x, y in it:
            cr.line_to(self.sx(x + dx), self.sy(y + dy))

    def _draw_boxes(self, cr, sc, dx, dy):
        if not sc.copy_boxes or len(sc.copy_boxes) > 2000:
            return
        cr.set_line_width(1.0)
        cr.set_dash([3, 3])
        cr.set_source_rgba(C_BOX[0], C_BOX[1], C_BOX[2], 0.7)
        for b in sc.copy_boxes:
            x0, y0, x1, y1 = b
            cr.rectangle(self.sx(x0 + dx), self.sy(y1 + dy), (x1 - x0) * self.scale, (y1 - y0) * self.scale)
        cr.stroke()
        cr.set_dash([])
        if 1 < len(sc.copy_boxes) <= 60 and self.scale * min(b[2] - b[0] for b in sc.copy_boxes) > 40:
            for i, b in enumerate(sc.copy_boxes):
                self._text(cr, self.sx(b[0] + dx) + 3, self.sy(b[3] + dy) + 11, "#%d" % (i + 1),
                           size=9, color=(C_BOX[0], C_BOX[1], C_BOX[2]))

    def _draw_travel(self, cr, sc, dx, dy):
        if not sc.travel:
            return
        cr.set_line_width(1.0)
        cr.set_dash([4, 3])
        cr.set_source_rgba(C_TRAVEL[0], C_TRAVEL[1], C_TRAVEL[2], 0.85)
        for a, b in sc.travel:
            # the move from the origin is not dragged with the job
            ax, ay = (a[0], a[1]) if a == sc.start else (a[0] + dx, a[1] + dy)
            cr.move_to(self.sx(ax), self.sy(ay))
            cr.line_to(self.sx(b[0] + dx), self.sy(b[1] + dy))
        cr.stroke()
        cr.set_dash([])

    def _path_color(self, sc, i):
        if sc.oob[i]:
            return C_OOB
        if sc.is_weed[i]:
            return C_WEED
        return C_CLOSED if sc.closed[i] else C_OPEN

    def _draw_paths(self, cr, sc, dx, dy):
        n = len(sc.paths)
        if not n:
            return
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        prog = self.progress
        state = sc.state_at(prog) if prog is not None else None
        done = state[0] if state else 0
        lw = 1.6 if n < 3000 else 1.0
        # remaining / normal paths
        for i, p in enumerate(sc.paths):
            col = self._path_color(sc, i)
            if state is not None and i < done:
                continue
            alpha = 0.35 if state is not None else 1.0
            cr.set_source_rgba(col[0], col[1], col[2], alpha)
            cr.set_line_width(2.4 if sc.oob[i] else lw)
            if not sc.closed[i]:
                cr.set_dash([7, 3])
            self._poly_path(cr, p, dx, dy)
            cr.stroke()
            cr.set_dash([])
        if state is not None:
            cr.set_source_rgb(*C_DONE)
            cr.set_line_width(2.6)
            for i in range(min(done, n)):
                self._poly_path(cr, sc.paths[i], dx, dy)
            cr.stroke()
            _d, pi, plen, knife, in_travel = state
            if pi is not None:
                p = sc.paths[pi]
                cums = sc.tl_cums[pi]
                cr.move_to(self.sx(p[0][0] + dx), self.sy(p[0][1] + dy))
                for k in range(1, len(p)):
                    if cums[k] <= plen:
                        cr.line_to(self.sx(p[k][0] + dx), self.sy(p[k][1] + dy))
                    else:
                        break
                cr.line_to(self.sx(knife[0] + dx), self.sy(knife[1] + dy))
                cr.stroke()
            # knife
            KX, KY = self.sx(knife[0] + dx), self.sy(knife[1] + dy)
            cr.set_source_rgba(C_KNIFE[0], C_KNIFE[1], C_KNIFE[2], 0.25)
            cr.arc(KX, KY, 9, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgb(*C_KNIFE)
            cr.arc(KX, KY, 3.5 if not in_travel else 2.5, 0, 2 * math.pi)
            cr.fill()
            cr.set_line_width(1.0)
            cr.move_to(KX - 12, KY)
            cr.line_to(KX + 12, KY)
            cr.move_to(KX, KY - 12)
            cr.line_to(KX, KY + 12)
            cr.stroke()
        if self.show_arrows and n <= 4000:
            self._draw_arrows(cr, sc, dx, dy)
        if self.show_order and n <= 150:
            self._draw_order(cr, sc, dx, dy)

    def _draw_arrows(self, cr, sc, dx, dy):
        s = self.scale
        for i, p in enumerate(sc.paths):
            cums = sc.tl_cums[i]
            L = cums[-1]
            Ls = L * s
            if Ls < 24:
                continue
            k = int(max(1, min(6, Ls // 160)))
            col = self._path_color(sc, i)
            cr.set_source_rgb(*col)
            for m in range(k):
                target = L * (m + 0.5) / k
                j = max(0, min(len(cums) - 2, bisect.bisect_right(cums, target) - 1))
                a, b = p[j], p[j + 1]
                seg = cums[j + 1] - cums[j]
                if seg <= 1e-9:
                    continue
                t = (target - cums[j]) / seg
                mx, my = a[0] + (b[0] - a[0]) * t + dx, a[1] + (b[1] - a[1]) * t + dy
                ux, uy = (b[0] - a[0]) / seg, -(b[1] - a[1]) / seg  # screen direction (y down)
                X, Y = self.sx(mx), self.sy(my)
                sz = 6.0
                cr.move_to(X + ux * sz, Y + uy * sz)
                cr.line_to(X - ux * sz - uy * sz * 0.65, Y - uy * sz + ux * sz * 0.65)
                cr.line_to(X - ux * sz * 0.4, Y - uy * sz * 0.4)
                cr.line_to(X - ux * sz + uy * sz * 0.65, Y - uy * sz - ux * sz * 0.65)
                cr.close_path()
                cr.fill()

    def _draw_order(self, cr, sc, dx, dy):
        cr.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(9)
        placed = []
        for i, p in enumerate(sc.paths):
            X, Y = self.sx(p[0][0] + dx), self.sy(p[0][1] + dy)
            if any(abs(X - a) < 14 and abs(Y - b) < 14 for a, b in placed):
                X += 14
            placed.append((X, Y))
            label = str(i + 1)
            ext = cr.text_extents(label)
            r = max(7.0, ext.width / 2 + 3.5)
            cr.set_source_rgba(1, 1, 1, 0.92)
            cr.new_path()
            cr.arc(X, Y, r, 0, 2 * math.pi)
            cr.fill_preserve()
            col = self._path_color(sc, i)
            cr.set_source_rgb(*col)
            cr.set_line_width(1.2)
            cr.stroke()
            cr.move_to(X - ext.width / 2 - ext.x_bearing, Y + ext.height / 2)
            cr.show_text(label)
        cr.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)

    # overlays --------------------------------------------------------------------
    def _draw_rulers(self, cr, w, h):
        s = self.scale
        uf = UNITS[self.unit]
        major = nice_step(s, 70, self.unit)
        minor = minor_step(major, s, 6, self.unit)
        cr.set_source_rgba(0.97, 0.97, 0.98, 0.96)
        cr.rectangle(0, 0, w, RULER)
        cr.rectangle(0, RULER, RULER, h - RULER)
        cr.fill()
        cr.set_source_rgb(0.72, 0.74, 0.78)
        cr.set_line_width(1.0)
        cr.move_to(0, RULER + 0.5)
        cr.line_to(w, RULER + 0.5)
        cr.move_to(RULER + 0.5, RULER)
        cr.line_to(RULER + 0.5, h)
        cr.stroke()
        x0m, _ = self.to_mm(RULER, 0)
        x1m, _ = self.to_mm(w, 0)
        _, y1m = self.to_mm(0, RULER)
        _, y0m = self.to_mm(0, h)
        cr.set_source_rgb(0.40, 0.42, 0.46)
        if minor:
            i = math.ceil(x0m / minor)
            while i * minor <= x1m:
                X = round(self.sx(i * minor)) + 0.5
                cr.move_to(X, RULER - 5)
                cr.line_to(X, RULER)
                i += 1
            j = math.ceil(y0m / minor)
            while j * minor <= y1m:
                Y = round(self.sy(j * minor)) + 0.5
                cr.move_to(RULER - 5, Y)
                cr.line_to(RULER, Y)
                j += 1
        cr.stroke()
        fmt = "%g"
        i = math.ceil(x0m / major)
        while i * major <= x1m:
            X = round(self.sx(i * major)) + 0.5
            cr.move_to(X, RULER - 11)
            cr.line_to(X, RULER)
            cr.stroke()
            self._text(cr, X + 3, 11, fmt % round(i * major / uf, 3), size=9, color=(0.30, 0.32, 0.36))
            i += 1
        j = math.ceil(y0m / major)
        while j * major <= y1m:
            Y = round(self.sy(j * major)) + 0.5
            cr.move_to(RULER - 11, Y)
            cr.line_to(RULER, Y)
            cr.stroke()
            cr.save()
            cr.translate(11, Y - 3)
            cr.rotate(-math.pi / 2)
            self._text(cr, 0, 0, fmt % round(j * major / uf, 3), size=9, color=(0.30, 0.32, 0.36))
            cr.restore()
            j += 1
        # corner
        cr.set_source_rgb(0.93, 0.94, 0.95)
        cr.rectangle(0, 0, RULER, RULER)
        cr.fill()
        self._text(cr, RULER / 2, 15, self.unit, size=8, color=(0.35, 0.37, 0.41), anchor="center")
        # axis captions (with drawn arrows; the toy font API has no reliable arrow glyph)
        cap = (0.20, 0.22, 0.26)
        cr.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(10)
        tx = "x (feed)"
        tw = cr.text_extents(tx).x_advance
        bx = w - tw - 30
        cr.set_source_rgb(0.90, 0.92, 0.95)
        cr.rectangle(bx - 6, 0, w - bx + 6, RULER)
        cr.fill()
        cr.move_to(bx, 15)
        cr.set_source_rgb(*cap)
        cr.show_text(tx)
        self._arrow(cr, w - 22, 11, w - 6, 11, cap)
        ty = "y (width)"
        th = cr.text_extents(ty).x_advance
        by = h - STRIP - th - 34
        cr.set_source_rgb(0.90, 0.92, 0.95)
        cr.rectangle(0, by, RULER, h - STRIP - by)
        cr.fill()
        cr.save()
        cr.translate(15, h - STRIP - 6)
        cr.rotate(-math.pi / 2)
        cr.move_to(0, 0)
        cr.set_source_rgb(*cap)
        cr.show_text(ty)
        cr.restore()
        self._arrow(cr, 11, h - STRIP - th - 12, 11, h - STRIP - th - 28, cap)
        cr.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)

    def _job_screen_rect(self):
        bb = self.scene.job_bbox
        if not bb:
            return None
        dx, dy = self.drag_delta
        return (self.sx(bb[0] + dx), self.sy(bb[3] + dy), self.sx(bb[2] + dx), self.sy(bb[1] + dy))

    def _draw_banner(self, cr, sc, w, h):
        y = RULER + 8
        if sc.warnings:
            txt = sc.warnings[0]
            if len(sc.warnings) > 1:
                txt += "   (+%d more)" % (len(sc.warnings) - 1)
            cr.set_font_size(12)
            cr.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            text = "Warning: " + txt
            ext = cr.text_extents(text)
            bw = min(w - RULER - 40, ext.width + 28)
            bx = RULER + (w - RULER - bw) / 2
            by = y
            jr = self._job_screen_rect()
            m = 14  # order labels / arrows stick out of the bbox a little
            if jr and not (jr[2] + m < bx or jr[0] - m > bx + bw or jr[3] + m < by or jr[1] - m > by + 26):
                by = h - STRIP - 36  # the job is under the top banner position: move it down
            self._round_rect(cr, bx, by, bw, 26, 5)
            cr.set_source_rgba(0.80, 0.10, 0.10, 0.92)
            cr.fill()
            cr.save()
            cr.rectangle(bx, by, bw, 26)
            cr.clip()
            cr.set_source_rgb(1, 1, 1)
            cr.move_to(bx + 14, by + 17)
            cr.show_text(text)
            cr.restore()
            cr.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
            if by == y:
                y += 32
        if self.progress is not None:
            label = "%s  %d%%" % (self.progress_label or "Progress", round(self.progress * 100))
            cr.set_font_size(12)
            cr.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            ext = cr.text_extents(label)
            bw = ext.width + 24
            bx = w - bw - 12
            self._round_rect(cr, bx, y, bw, 26, 5)
            cr.set_source_rgba(0.42, 0.12, 0.56, 0.92)
            cr.fill()
            cr.set_source_rgb(1, 1, 1)
            cr.move_to(bx + 12, y + 17)
            cr.show_text(label)
            cr.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)

    def _draw_legend(self, cr, sc, w, h):
        items = [(C_CLOSED, "Closed cut", False), (C_OPEN, "Open cut", True)]
        if any(sc.is_weed):
            items.append((C_WEED, "Weed border", False))
        if self.show_travel:
            items.append((C_TRAVEL, "Travel (knife up)", True))
        if any(sc.oob):
            items.append((C_OOB, "Outside cut area", False))
        if self.progress is not None:
            items.append((C_DONE, "Done", False))
        cr.set_font_size(10)
        widths = [cr.text_extents(t).x_advance for _c, t, _d in items]
        total = sum(widths) + len(items) * 32
        x = w - total - 4
        y = h - STRIP
        for (col, txt, dashed), tw in zip(items, widths):
            cr.set_source_rgb(*col)
            cr.set_line_width(2.0)
            if dashed:
                cr.set_dash([4, 2])
            cr.move_to(x, y + STRIP / 2)
            cr.line_to(x + 18, y + STRIP / 2)
            cr.stroke()
            cr.set_dash([])
            self._text(cr, x + 23, y + STRIP / 2 + 4, txt, size=10, color=C_TEXT)
            x += tw + 32
        return w - total - 4  # left edge of the legend

    def _draw_status_strip(self, cr, sc, w, h):
        cr.set_source_rgba(0.97, 0.97, 0.98, 0.97)
        cr.rectangle(0, h - STRIP, w, STRIP)
        cr.fill()
        cr.set_source_rgb(0.72, 0.74, 0.78)
        cr.set_line_width(1.0)
        cr.move_to(0, h - STRIP + 0.5)
        cr.line_to(w, h - STRIP + 0.5)
        cr.stroke()
        legend_x = self._draw_legend(cr, sc, w, h)
        parts = []
        if self.cursor_mm is not None:
            parts.append("x %s  y %s %s" % (self._fmt_pos(self.cursor_mm[0]), self._fmt_pos(self.cursor_mm[1]), self.unit))
        parts.append("zoom %.2f px/%s" % (self.scale * UNITS[self.unit], self.unit))
        hint = "drag the job to move it · wheel: zoom · right/middle drag: pan"
        cr.set_font_size(10)
        text = "    ".join(parts)
        if cr.text_extents(text + "    " + hint).x_advance < legend_x - 24:
            text += "    " + hint
        cr.save()
        cr.rectangle(0, h - STRIP, max(0, legend_x - 12), STRIP)
        cr.clip()
        self._text(cr, 8, h - STRIP / 2 + 4, text, size=10, color=(0.30, 0.32, 0.36))
        cr.restore()

    def _fmt_pos(self, mm):
        """A position in the display unit, without the unit suffix."""
        return "%.2f" % (mm / 25.4) if self.unit == "in" else "%.1f" % mm

    def _draw_drag_readout(self, cr):
        nx, ny = self._drag["new"]
        bb = self.scene.job_bbox
        if not bb:
            return
        dx, dy = self.drag_delta
        X, Y = self.sx(bb[0] + dx), self.sy(bb[3] + dy) - 10
        label = "offset  x %s  y %s %s" % (self._fmt_pos(nx), self._fmt_pos(ny), self.unit)
        cr.set_font_size(11)
        ext = cr.text_extents(label)
        self._round_rect(cr, X, Y - 16, ext.width + 14, 20, 4)
        cr.set_source_rgba(0.12, 0.14, 0.18, 0.88)
        cr.fill()
        cr.set_source_rgb(1, 1, 1)
        cr.move_to(X + 7, Y - 2)
        cr.show_text(label)
        # outline of the moving job
        cr.set_source_rgba(C_KNIFE[0], C_KNIFE[1], C_KNIFE[2], 0.8)
        cr.set_line_width(1.0)
        cr.set_dash([6, 3])
        cr.rectangle(self.sx(bb[0] + dx), self.sy(bb[3] + dy), (bb[2] - bb[0]) * self.scale,
                     (bb[3] - bb[1]) * self.scale)
        cr.stroke()
        cr.set_dash([])

    # helpers ---------------------------------------------------------------------
    @staticmethod
    def _arrow(cr, x0, y0, x1, y1, color, head=5.0):
        cr.set_source_rgb(*color[:3])
        cr.set_line_width(1.2)
        cr.move_to(x0, y0)
        cr.line_to(x1, y1)
        cr.stroke()
        L = math.hypot(x1 - x0, y1 - y0) or 1.0
        ux, uy = (x1 - x0) / L, (y1 - y0) / L
        cr.move_to(x1, y1)
        cr.line_to(x1 - ux * head - uy * head * 0.6, y1 - uy * head + ux * head * 0.6)
        cr.line_to(x1 - ux * head + uy * head * 0.6, y1 - uy * head - ux * head * 0.6)
        cr.close_path()
        cr.fill()

    @staticmethod
    def _round_rect(cr, x, y, w, h, r):
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
        cr.close_path()

    @staticmethod
    def _text(cr, x, y, text, size=11, color=C_TEXT, anchor="left"):
        cr.set_font_size(size)
        cr.set_source_rgb(*color[:3])
        if anchor != "left":
            ext = cr.text_extents(text)
            x -= ext.x_advance if anchor == "right" else ext.x_advance / 2.0
        cr.move_to(x, y)
        cr.show_text(text)
