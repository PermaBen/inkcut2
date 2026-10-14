"""Job settings and the design -> material -> toolpath pipeline.

Coordinate pipeline (see docs/ARCHITECTURE.md):

1. design coords (mm, x right, y down, from Inkscape) -> material coords by flipping y
   (``(x, y) -> (x, -y)``) so artwork reads upright in the y-up preview; material x runs along
   the media feed/length, material y across the media width.
2. one copy: scale (``scale_pct``) -> mirror (``mirror_h`` flips x, ``mirror_v`` flips y, about
   the bbox centre) -> rotate (``rotation`` degrees CCW, about the bbox centre) -> normalise so
   the copy's bbox min is (0, 0).
3. array: ``cols`` copies along material **x** (feed, gap ``spacing_x``) and ``rows`` copies along
   material **y** (across the width, gap ``spacing_y``). With ``auto_fill`` rows/cols come from
   :func:`geometry.max_grid` against the usable area (see :meth:`Job.usable_area`).
4. placement: every copy is shifted by ``offset + device padding`` (left/bottom padding), plus
   ``weed_margin`` when a weed border is enabled so the border itself starts at the offset.
5. optional weed border: a rectangle around the whole layout, kept as its own last "copy" and
   always cut last.
6. cutting: paths ordered (``order``), drag-knife blade offset applied, then overcut (overcut is
   applied to the compensated, still closed paths so the extra length follows the real knife
   path), then passes expanded.

``job.py`` never imports ``devices``: the device is duck-typed (``width_mm``, ``length_mm``,
``padding``, ``default_speed``, ``default_force``, ``default_overcut``,
``default_blade_offset``; optional ``speed_range``, ``force_range``, ``travel_speed`` (cm/s),
``blade_angle_threshold`` (deg)).
"""

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, fields

from . import geometry as G

CONFIG_ENV_VAR = "VINYLCUT_CONFIG_DIR"
SETTINGS_FILE = "job.json"

PASS_MODES = ("per_path", "per_job")
ORDER_MODES = G.ORDER_MODES

PEN_OVERHEAD_S = 0.15        # pen up + pen down settle time per path, for time estimates
LARGE_COPY_COUNT = 1000      # warn above this many copies
MAX_AUTO_COPIES = 5000       # auto_fill never creates more copies than this
_EPS = 1e-6


def config_dir() -> str:
    """Directory for VinylCut's config files (not created here).

    ``%VINYLCUT_CONFIG_DIR%`` if set (used by tests / portable installs), else
    ``%APPDATA%\\vinylcut``, else ``~/.config/vinylcut``.
    """
    env = os.environ.get(CONFIG_ENV_VAR)
    if env:
        return env
    appdata = os.environ.get("APPDATA")
    if appdata:
        return os.path.join(appdata, "vinylcut")
    return os.path.join(os.path.expanduser("~"), ".config", "vinylcut")


# ------------------------------------------------------------------------ settings

@dataclass
class JobSettings:
    # transform (applied to one copy, in this order: scale -> mirror -> rotate)
    scale_pct: float = 100.0
    mirror_h: bool = False            # flip left/right (e.g. heat-transfer vinyl)
    mirror_v: bool = False
    rotation: float = 0.0             # degrees CCW, any value
    # array / copies
    rows: int = 1                     # copies across the width (material y)
    cols: int = 1                     # copies along the feed (material x)
    spacing_x: float = 2.0            # mm gap between copies along feed
    spacing_y: float = 2.0            # mm gap across width
    auto_fill: bool = False           # rows/cols computed with geometry.max_grid to fill material
    # placement on material
    offset_x: float = 0.0             # mm from origin (+ device padding)
    offset_y: float = 0.0
    # cutting
    passes: int = 1
    pass_mode: str = "per_path"       # "per_path" (repeat each path N times) | "per_job"
    speed: float = None               # cm/s ; None -> device default
    force: float = None               # grams ; None -> device default
    overcut: float = None             # mm ; None -> device default
    blade_offset: float = None        # mm ; None -> device default
    order: str = "inside_out"
    weed_border: bool = False
    weed_margin: float = 3.0
    # --- additions (not in the original contract) ---
    fill_length: float = 0.0          # mm of material length auto_fill may use; 0 -> device length_mm

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d) -> "JobSettings":
        """Build from a dict: unknown keys ignored, values coerced, invalid values -> default."""
        s = cls()
        if not isinstance(d, dict):
            return s
        for f in fields(cls):
            if f.name not in d:
                continue
            kind = _KINDS.get(f.name, "str")
            try:
                val = _coerce(kind, d[f.name])
            except (TypeError, ValueError, OverflowError):
                continue
            if f.name == "pass_mode" and val not in PASS_MODES:
                continue
            if f.name == "order" and val not in ORDER_MODES:
                continue
            setattr(s, f.name, val)
        return s


_KINDS = {
    "scale_pct": "float", "mirror_h": "bool", "mirror_v": "bool", "rotation": "float",
    "rows": "int", "cols": "int", "spacing_x": "float", "spacing_y": "float",
    "auto_fill": "bool", "offset_x": "float", "offset_y": "float", "passes": "int",
    "pass_mode": "str", "speed": "optfloat", "force": "optfloat", "overcut": "optfloat",
    "blade_offset": "optfloat", "order": "str", "weed_border": "bool", "weed_margin": "float",
    "fill_length": "float",
}


def _coerce(kind, v):
    if kind == "bool":
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        if isinstance(v, str):
            t = v.strip().lower()
            if t in ("1", "true", "yes", "on", "y", "t"):
                return True
            if t in ("0", "false", "no", "off", "n", "f", ""):
                return False
        raise ValueError(v)
    if kind == "optfloat":
        if v is None or (isinstance(v, str) and v.strip().lower() in ("", "none", "null")):
            return None
        kind = "float"
    if kind == "float":
        if isinstance(v, bool):
            raise ValueError(v)
        f = float(v)
        if not math.isfinite(f):
            raise ValueError(v)
        return f
    if kind == "int":
        if isinstance(v, bool):
            raise ValueError(v)
        f = float(v)
        if not math.isfinite(f):
            raise ValueError(v)
        return int(round(f))
    if v is None:
        raise ValueError(v)
    return str(v)


def load_last_settings() -> JobSettings:
    """Last used settings from ``config_dir()/job.json``; missing/corrupt -> defaults."""
    path = os.path.join(config_dir(), SETTINGS_FILE)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return JobSettings()
    return JobSettings.from_dict(data)


def save_last_settings(s: JobSettings) -> None:
    """Write ``config_dir()/job.json`` atomically (creates the directory)."""
    d = config_dir()
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, SETTINGS_FILE)
    fd, tmp = tempfile.mkstemp(prefix="job.", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(s.to_dict(), fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ----------------------------------------------------------------------------- job

_FALLBACK_DEFAULTS = {"speed": 30.0, "force": 80.0, "overcut": 0.0, "blade_offset": 0.0}


def _num(v, default):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


class Job:
    """One cutting job. Results are computed lazily and cached: treat ``design``, ``settings``
    and ``device`` as immutable for the lifetime of a Job (create a new Job after a change)."""

    def __init__(self, design, settings=None, device=None):
        self.design = [[(float(p[0]), float(p[1])) for p in poly] for poly in (design or []) if len(poly) > 0]
        self.settings = settings if settings is not None else JobSettings()
        self.device = device
        self._cache = {}
        self._notes = []          # warnings produced while sanitising settings
        self._p = self._sanitise()

    # ---------------------------------------------------------------- helpers
    def _cached(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def _dev(self, name, default):
        return getattr(self.device, name, default) if self.device is not None else default

    def _padding(self):
        pad = self._dev("padding", (0, 0, 0, 0))
        try:
            vals = [float(v) for v in pad]
        except (TypeError, ValueError):
            vals = []
        if len(vals) != 4:
            vals = [0.0, 0.0, 0.0, 0.0]
        return tuple(vals)

    def _sanitise(self):
        s = self.settings
        notes = self._notes
        p = {}
        sc = _num(s.scale_pct, 100.0)
        if sc <= 0:
            notes.append("Scale must be greater than 0 %%; using 100 %% (got %g)." % sc)
            sc = 100.0
        p["scale"] = sc / 100.0
        p["rotation"] = _num(s.rotation, 0.0)
        p["rows"] = max(1, int(_num(s.rows, 1)))
        p["cols"] = max(1, int(_num(s.cols, 1)))
        p["sx"] = _num(s.spacing_x, 0.0)
        p["sy"] = _num(s.spacing_y, 0.0)
        p["ox"] = _num(s.offset_x, 0.0)
        p["oy"] = _num(s.offset_y, 0.0)
        p["passes"] = max(1, int(_num(s.passes, 1)))
        pm = s.pass_mode
        if pm not in PASS_MODES:
            notes.append("Unknown pass mode %r; using 'per_path'." % (pm,))
            pm = "per_path"
        p["pass_mode"] = pm
        order = s.order
        if order not in ORDER_MODES:
            notes.append("Unknown path order %r; using 'inside_out'." % (order,))
            order = "inside_out"
        p["order"] = order
        p["weed"] = bool(s.weed_border)
        p["margin"] = max(0.0, _num(s.weed_margin, 0.0)) if p["weed"] else 0.0
        return p

    # ---------------------------------------------------------------- settings resolution
    def effective(self, name) -> float:
        """Resolved ``speed`` (cm/s), ``force`` (g), ``overcut`` (mm) or ``blade_offset`` (mm):
        the job setting when not None, else the device's ``default_<name>``."""
        if name not in _FALLBACK_DEFAULTS:
            raise KeyError("unknown parameter %r" % (name,))
        v = getattr(self.settings, name, None)
        if v is not None:
            f = _num(v, None)
            if f is not None:
                return f
        return _num(self._dev("default_" + name, _FALLBACK_DEFAULTS[name]), _FALLBACK_DEFAULTS[name])

    # ---------------------------------------------------------------- geometry pipeline
    def copy_geometry(self):
        """One copy in material coords: y flipped, scaled, mirrored, rotated, normalised to (0,0)."""
        return self._cached("copy", self._compute_copy)

    def _compute_copy(self):
        if not self.design:
            return []
        p = self._p
        s = self.settings
        polys = [[(x, -y) for x, y in poly] for poly in self.design]
        if p["scale"] != 1.0:
            polys = G.scale(polys, p["scale"], p["scale"])
        polys = G.mirror(polys, bool(s.mirror_h), bool(s.mirror_v))
        if p["rotation"] % 360.0 != 0.0:
            polys = G.rotate(polys, p["rotation"])
        return G.normalize(polys)

    def copy_size(self):
        """(w, h) of one copy in material mm (w along feed/x, h across width/y)."""
        b = G.bbox(self.copy_geometry())
        return (b[2] - b[0], b[3] - b[1])

    def usable_area(self):
        """(along_x, across_y) mm available for the array: material length/width minus device
        padding, offset and (with a weed border) twice the weed margin. ``fill_length`` > 0
        replaces the device length."""
        pl, pb, pr, pt = self._padding()
        p = self._p
        length = _num(self._dev("length_mm", 5000.0), 5000.0)
        fl = _num(getattr(self.settings, "fill_length", 0.0), 0.0)
        if fl > 0:
            length = fl
        width = _num(self._dev("width_mm", 610.0), 610.0)
        m2 = 2.0 * p["margin"]
        return (length - pl - pr - p["ox"] - m2, width - pb - pt - p["oy"] - m2)

    def grid_size(self):
        """(rows, cols) actually used (rows across the width/y, cols along the feed/x)."""
        return self._cached("grid", self._compute_grid)

    def _compute_grid(self):
        p = self._p
        if not self.settings.auto_fill:
            return (p["rows"], p["cols"])
        if not self.copy_geometry():
            return (1, 1)
        w, h = self.copy_size()
        ax, ay = self.usable_area()
        rows, cols = G.max_grid(w, h, ax, ay, p["sx"], p["sy"])
        if rows < 1 or cols < 1:
            self._cache["autofill_nofit"] = True
        rows = max(1, rows)
        cols = max(1, cols)
        if rows * cols > MAX_AUTO_COPIES:
            self._cache["autofill_capped"] = rows * cols
            cols = max(1, MAX_AUTO_COPIES // rows)
            rows = min(rows, MAX_AUTO_COPIES)
        return (rows, cols)

    def layout(self):
        """All copies placed in material coords (offset + padding applied); with a weed border
        enabled, ``[border]`` is appended as its own last copy."""
        return self._cached("layout", self._compute_layout)

    def _compute_layout(self):
        copy = self.copy_geometry()
        if not copy:
            return []
        p = self._p
        pl, pb, _, _ = self._padding()
        rows, cols = self.grid_size()
        dx = p["ox"] + pl + p["margin"]
        dy = p["oy"] + pb + p["margin"]
        placed = G.translate(copy, dx, dy)
        copies = G.grid_array(placed, rows, cols, p["sx"], p["sy"])
        if p["weed"]:
            copies.append([G.weed_border(G.bbox(placed_copy for c in copies for placed_copy in c),
                                         p["margin"])])
        return copies

    def bounds(self):
        """bbox (minx, miny, maxx, maxy) of the whole layout incl. weed border."""
        return self._cached("bounds", lambda: G.bbox(poly for c in self.layout() for poly in c))

    def ordered_paths(self):
        """Layout paths in cutting order, before blade offset / overcut (weed border last)."""
        return self._cached("ordered", self._compute_ordered)

    def _compute_ordered(self):
        copies = self.layout()
        if not copies:
            return []
        if self._p["weed"]:
            body, border = copies[:-1], copies[-1]
        else:
            body, border = copies, []
        paths = [poly for c in body for poly in c]
        out = G.order_paths(paths, self._p["order"], start=(0.0, 0.0))
        out.extend(G.order_paths(border, "as_is"))
        return out

    def preview_paths(self):
        """Final cut order for ONE pass: ordered, blade offset, then overcut applied."""
        return self._cached("preview", self._compute_preview)

    def _compute_preview(self):
        paths = self.ordered_paths()
        bo = self.effective("blade_offset")
        if bo > 0:
            thr = _num(self._dev("blade_angle_threshold", 30.0), 30.0)
            paths = G.apply_blade_offset(paths, bo, thr)
        oc = self.effective("overcut")
        if oc > 0:
            paths = G.apply_overcut(paths, oc)
        return paths

    def toolpaths(self):
        """What gets sent: preview_paths with passes expanded ("per_path": each path repeated N
        times consecutively; "per_job": the whole sequence repeated N times)."""
        return self._cached("toolpaths", self._compute_toolpaths)

    def _compute_toolpaths(self):
        paths = self.preview_paths()
        n = self._p["passes"]
        if n <= 1:
            return [list(p) for p in paths]
        if self._p["pass_mode"] == "per_job":
            return [list(p) for _ in range(n) for p in paths]
        return [list(p) for p in paths for _ in range(n)]

    # ---------------------------------------------------------------- reporting
    def stats(self) -> dict:
        """Summary for the GUI.

        Keys: ``copies``, ``rows``, ``cols``, ``paths`` (per pass), ``passes``, ``toolpaths``,
        ``cut_length_mm`` / ``travel_length_mm`` (one pass, travel from origin (0,0), no return),
        ``total_cut_length_mm`` / ``total_travel_length_mm`` (all passes), ``size_mm`` (w along
        feed, h across width, incl. weed border), ``bbox``, ``est_time_s`` (all passes: cut
        length / speed + travel / travel speed + PEN_OVERHEAD_S per path; travel speed = device
        ``travel_speed`` cm/s if present else 2x cut speed).
        """
        return dict(self._cached("stats", self._compute_stats))

    def _compute_stats(self):
        rows, cols = self.grid_size() if self.design else (0, 0)
        prev = self.preview_paths()
        tp = self.toolpaths()
        cut1 = G.path_length(prev)
        trav1 = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in G.travel_moves(prev, (0.0, 0.0)))
        cut = G.path_length(tp)
        trav = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in G.travel_moves(tp, (0.0, 0.0)))
        speed = self.effective("speed")
        speed_mm = speed * 10.0 if speed > 0 else 10.0
        ts = _num(self._dev("travel_speed", None), None)
        travel_mm = ts * 10.0 if ts is not None and ts > 0 else 2.0 * speed_mm
        est = cut / speed_mm + trav / travel_mm + len(tp) * PEN_OVERHEAD_S
        b = self.bounds()
        return {
            "copies": rows * cols,
            "rows": rows,
            "cols": cols,
            "paths": len(prev),
            "passes": self._p["passes"],
            "toolpaths": len(tp),
            "cut_length_mm": cut1,
            "travel_length_mm": trav1,
            "total_cut_length_mm": cut,
            "total_travel_length_mm": trav,
            "size_mm": (b[2] - b[0], b[3] - b[1]),
            "bbox": b,
            "est_time_s": est,
        }

    def _bounds_problems(self):
        """List of human readable out-of-material problems (empty -> fits)."""
        if not self.design:
            return []
        pl, pb, pr, pt = self._padding()
        width = _num(self._dev("width_mm", 610.0), 610.0)
        length = _num(self._dev("length_mm", 5000.0), 5000.0)
        minx, miny, maxx, maxy = self.bounds()
        out = []
        over_w = maxy - (width - pt)
        if over_w > _EPS:
            out.append("Design exceeds material width by %.1f mm" % over_w)
        over_l = maxx - (length - pr)
        if over_l > _EPS:
            out.append("Design exceeds material length by %.1f mm" % over_l)
        under_x = pl - minx
        if under_x > _EPS:
            out.append("Design starts %.1f mm before the cutting area along the feed (check offset X)" % under_x)
        under_y = pb - miny
        if under_y > _EPS:
            out.append("Design starts %.1f mm outside the cutting area across the width (check offset Y)" % under_y)
        return out

    def warnings(self) -> list:
        return list(self._cached("warnings", self._compute_warnings))

    def _compute_warnings(self):
        if not self.design:
            return ["Design is empty - nothing to cut"]
        out = list(self._notes)
        w, h = self.copy_size()
        if w <= _EPS and h <= _EPS:
            out.append("Design has zero size")
        rows, cols = self.grid_size()        # also fills the auto-fill flags
        if self._cache.get("autofill_nofit"):
            out.append("Design is larger than the usable material area; auto-fill placed a single copy")
        if self._cache.get("autofill_capped"):
            out.append("Auto-fill limited to %d copies (would be %d)" % (rows * cols, self._cache["autofill_capped"]))
        out.extend(self._bounds_problems())
        copies = rows * cols
        if copies > LARGE_COPY_COUNT:
            out.append("Large job: %d copies (%d paths) - sending may take a while" %
                       (copies, copies * len(self.design)))
        if self._p["passes"] > 1 and self._p["pass_mode"] == "per_job":
            out.append("%d passes in 'per_job' mode re-cut the whole job each pass; the material may "
                       "shift between passes ('per_path' is usually more accurate)" % self._p["passes"])
        for name, rng_attr, unit in (("speed", "speed_range", "cm/s"), ("force", "force_range", "g")):
            rng = self._dev(rng_attr, None)
            try:
                lo, hi = float(rng[0]), float(rng[1])
            except (TypeError, ValueError, IndexError, KeyError):
                continue
            v = self.effective(name)
            if v < lo - _EPS or v > hi + _EPS:
                out.append("%s %g %s is outside the device range %g-%g %s" %
                           (name.capitalize(), v, unit, lo, hi, unit))
        return out

    def fits(self) -> bool:
        """True when there is something to cut and it lies inside the cuttable material area."""
        return bool(self.design) and not self._bounds_problems()
