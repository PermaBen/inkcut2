"""Inkscape selection -> cuttable polylines in millimetres.

Used by ``vinylcut.py`` (the inkex effect).  Kept separate so it can be unit-tested
against SVG files without Inkscape running.

Output follows docs/ARCHITECTURE.md: design coordinates in mm, SVG orientation
(x right, y down), origin at the page's top-left corner; each polyline is a list of
``(x, y)`` tuples, closed iff first point == last point.

Text cannot be outlined by inkex itself.  :func:`find_text` reports the text
elements reachable from a selection, :func:`convert_text_to_paths` runs the
Inkscape CLI on a temporary copy of the document to outline them, and
:func:`extract` then substitutes the outlined versions.
"""

import copy
import math
import os
import shutil
import subprocess
import sys
import tempfile

import inkex
from lxml import etree

MM_PER_PX = 25.4 / 96.0
DEFAULT_TOLERANCE_MM = 0.02
SVG_NS = "http://www.w3.org/2000/svg"
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"

GROUP_TAGS = {"g", "a", "switch", "svg", "symbol"}
SHAPE_TAGS = {"path", "rect", "circle", "ellipse", "line", "polyline", "polygon"}
TEXT_TAGS = {"text", "flowRoot"}
IMAGE_TAGS = {"image"}
_MAX_DEPTH = 64
_MAX_SEGMENTS = 20000  # per curve, safety cap


class Extraction:
    """Result of :func:`extract`."""

    def __init__(self):
        self.polylines = []  # list[list[tuple[float, float]]]  (mm)
        self.colors = []  # list[str] "#rrggbb", same length as polylines
        self.warnings = []  # list[str], human readable

    def warn(self, msg):
        if msg not in self.warnings:
            self.warnings.append(msg)

    def to_design(self, name="drawing.svg"):
        """Design JSON dict as specified in docs/ARCHITECTURE.md."""
        return {
            "version": 1,
            "name": name,
            "units": "mm",
            "polylines": [[[x, y] for (x, y) in pl] for pl in self.polylines],
            "colors": list(self.colors),
        }


# --------------------------------------------------------------------------- helpers


def _local(elem):
    """Local tag name of an SVG-namespace element, else None (comments, foreign tags)."""
    tag = elem.tag
    if not isinstance(tag, str):
        return None
    if tag.startswith("{"):
        ns, _, name = tag[1:].partition("}")
        return name if ns == SVG_NS else None
    return tag  # un-namespaced (sloppy files); treat as SVG


def _transform(elem):
    try:
        return inkex.Transform(elem.get("transform"))
    except Exception:  # malformed transform attribute
        return inkex.Transform()


def _style(elem):
    try:
        return elem.specified_style()
    except Exception:
        try:
            return inkex.Style(elem.get("style") or "")
        except Exception:
            return {}


def _is_hidden(elem):
    if (elem.get("display") or "").strip() == "none":
        return True
    try:
        style = elem.style  # inline style only; display is not inherited
        if (style.get("display") or "").strip() == "none":
            return True
    except Exception:
        pass
    try:
        if (elem.specified_style().get("display") or "").strip() == "none":
            return True
    except Exception:
        pass
    return False


def _is_invisible(elem):
    """visibility is inherited (and may be overridden by children); only matters for leaves."""
    vis = (_style(elem).get("visibility") or "").strip()
    return vis in ("hidden", "collapse")


def _hex(value):
    if value is None:
        return None
    value = str(value).strip()
    if not value or value in ("none", "transparent") or value.startswith("url("):
        return None
    try:
        rgb = inkex.Color(value).to_rgb()
        return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, int(c))) for c in list(rgb)[:3]))
    except Exception:
        return None


def element_color(elem):
    """Stroke colour, falling back to fill colour (SVG default fill = black)."""
    style = _style(elem)
    col = _hex(style.get("stroke"))
    if col:
        return col
    fill = style.get("fill")
    if fill is None:
        return "#000000"
    return _hex(fill) or "#000000"


def _href_target(elem):
    href = elem.get(XLINK_HREF) or elem.get("href") or ""
    if not href.startswith("#"):
        return None
    root = elem.getroottree().getroot()
    try:
        return root.getElementById(href[1:])
    except Exception:
        found = root.xpath("//*[@id=$i]", i=href[1:])
        return found[0] if found else None


def _num(elem, name):
    try:
        return float(inkex.units.convert_unit(elem.get(name) or "0", "px"))
    except Exception:
        try:
            return float(elem.get(name) or 0)
        except ValueError:
            return 0.0


def _sigma_max(t):
    """Largest singular value of the linear part (max length scale factor)."""
    (a, c, _), (b, d, _) = t.matrix
    s = (a * a + b * b + c * c + d * d) / 2.0
    q = math.sqrt(((a * a + b * b - c * c - d * d) / 2.0) ** 2 + (a * c + b * d) ** 2)
    return math.sqrt(max(0.0, s + q))


def mm_per_user_unit(svg):
    """Millimetres per SVG user unit, honouring width/height vs viewBox."""
    try:
        scale = float(svg.scale)  # px per user unit
    except Exception:
        scale = 1.0
    if not (scale > 0 and math.isfinite(scale)):
        scale = 1.0
    return scale * MM_PER_PX


def root_transform(svg):
    """Transform from root user units to design mm (page top-left = origin)."""
    k = mm_per_user_unit(svg)
    try:
        vb = svg.get_viewbox()
    except Exception:
        vb = [0, 0, 0, 0]
    vbx, vby = (vb[0], vb[1]) if vb and len(vb) == 4 else (0.0, 0.0)
    return inkex.Transform(scale=k) @ inkex.Transform(translate=(-vbx, -vby))


def parent_transform(elem):
    """Composed transform of elem's parent (root user units), i.e. excluding elem's own.

    Computed from the raw attributes: inkex's ``composed_transform()`` re-serialises
    ``transform`` attributes, which would count as a document change.
    """
    chain = []
    parent = elem.getparent()
    while parent is not None:
        chain.append(parent)
        parent = parent.getparent()
    t = inkex.Transform()
    for anc in reversed(chain):
        t = t @ _transform(anc)
    return t


def ancestors_hidden(elem):
    parent = elem.getparent()
    while parent is not None and _local(parent) is not None:
        if _local(parent) == "svg" and parent.getparent() is None:
            break
        if _is_hidden(parent):
            return True
        parent = parent.getparent()
    return False


# --------------------------------------------------------------------------- flattening


def _cubic_points(p0, p1, p2, p3, tol):
    """Uniform subdivision with a guaranteed chord-error bound <= tol."""
    ddx = max(abs(p0[0] - 2 * p1[0] + p2[0]), abs(p1[0] - 2 * p2[0] + p3[0]))
    ddy = max(abs(p0[1] - 2 * p1[1] + p2[1]), abs(p1[1] - 2 * p2[1] + p3[1]))
    dd = math.hypot(ddx, ddy)
    n = int(math.ceil(math.sqrt(0.75 * dd / tol))) if dd > 0 else 1
    n = max(1, min(n, _MAX_SEGMENTS))
    pts = []
    for i in range(1, n + 1):
        t = i / n
        u = 1 - t
        a, b, c, d = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
        pts.append(
            (
                a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
                a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1],
            )
        )
    pts[-1] = (p3[0], p3[1])
    return pts


def _quad_points(p0, p1, p2, tol):
    dd = math.hypot(p0[0] - 2 * p1[0] + p2[0], p0[1] - 2 * p1[1] + p2[1])
    n = int(math.ceil(math.sqrt(dd / (4.0 * tol)))) if dd > 0 else 1
    n = max(1, min(n, _MAX_SEGMENTS))
    pts = []
    for i in range(1, n + 1):
        t = i / n
        u = 1 - t
        pts.append(
            (
                u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
                u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1],
            )
        )
    pts[-1] = (p2[0], p2[1])
    return pts


def _arc_points(p0, rx, ry, phi_deg, large, sweep, p1, tol):
    """Elliptical arc per SVG 1.1 F.6.5/F.6.6 (radii corrected), flattened directly."""
    x0, y0 = p0
    x1, y1 = p1
    if abs(x0 - x1) < 1e-12 and abs(y0 - y1) < 1e-12:
        return []
    rx, ry = abs(rx), abs(ry)
    if rx < 1e-12 or ry < 1e-12:
        return [(x1, y1)]
    phi = math.radians(phi_deg % 360.0)
    cphi, sphi = math.cos(phi), math.sin(phi)
    dx2, dy2 = (x0 - x1) / 2.0, (y0 - y1) / 2.0
    x1p = cphi * dx2 + sphi * dy2
    y1p = -sphi * dx2 + cphi * dy2
    lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if lam > 1:
        s = math.sqrt(lam)
        rx, ry = rx * s, ry * s
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    coef = math.sqrt(max(0.0, num / den)) if den > 0 else 0.0
    if bool(large) == bool(sweep):
        coef = -coef
    cxp = coef * rx * y1p / ry
    cyp = -coef * ry * x1p / rx
    cx = cphi * cxp - sphi * cyp + (x0 + x1) / 2.0
    cy = sphi * cxp + cphi * cyp + (y0 + y1) / 2.0
    ux, uy = (x1p - cxp) / rx, (y1p - cyp) / ry
    vx, vy = (-x1p - cxp) / rx, (-y1p - cyp) / ry
    theta1 = math.atan2(uy, ux)
    dtheta = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
    if not sweep and dtheta > 0:
        dtheta -= 2 * math.pi
    elif sweep and dtheta < 0:
        dtheta += 2 * math.pi
    r = max(rx, ry)
    step = 2 * math.acos(1 - tol / r) if tol < r else math.pi / 2
    step = min(step, math.pi / 8)
    n = max(1, min(int(math.ceil(abs(dtheta) / step)), _MAX_SEGMENTS))
    pts = []
    for i in range(1, n):
        t = theta1 + dtheta * i / n
        ex, ey = rx * math.cos(t), ry * math.sin(t)
        pts.append((cx + cphi * ex - sphi * ey, cy + sphi * ex + cphi * ey))
    pts.append((x1, y1))
    return pts


def path_to_polylines(path, tol):
    """Flatten an inkex Path (in its own coordinates) into [(points, closed)] subpaths."""
    out = []
    pts = []
    closed = False
    first = None

    def flush():
        if len(pts) >= 2:
            out.append((list(pts), closed))

    for seg in inkex.Path(path).to_non_shorthand().proxy_iterator():
        letter = seg.letter
        if letter == "M":
            flush()
            end = seg.end_point
            pts = [(end.x, end.y)]
            first = pts[0]
            closed = False
            continue
        if letter == "Z":
            if pts:
                if pts[-1] != first:
                    pts.append(first)
                closed = True
                flush()
            pts, closed = [], False
            continue
        if not pts:  # drawing command after Z without a new M: restarts at subpath start
            start = first if first is not None else (0.0, 0.0)
            pts = [start]
            closed = False
        cur = pts[-1]
        if letter in "LHV":
            e = seg.end_point
            pts.append((e.x, e.y))
        elif letter == "C":
            c1, c2, e = [(p.x, p.y) for p in seg.control_points]
            pts.extend(_cubic_points(cur, c1, c2, e, tol))
        elif letter == "Q":
            c1, e = [(p.x, p.y) for p in seg.control_points]
            pts.extend(_quad_points(cur, c1, e, tol))
        elif letter == "A":
            a = seg.command
            e = seg.end_point
            pts.extend(_arc_points(cur, a.rx, a.ry, a.x_axis_rotation, a.large_arc, a.sweep, (e.x, e.y), tol))
        else:  # unknown -> straight line to its end point
            e = seg.end_point
            pts.append((e.x, e.y))
    flush()
    return out


def _clean(points, closed, ndigits=4):
    res = []
    for x, y in points:
        p = (round(x, ndigits), round(y, ndigits))
        if not res or abs(p[0] - res[-1][0]) > 1e-9 or abs(p[1] - res[-1][1]) > 1e-9:
            res.append(p)
    if len(res) >= 2 and abs(res[0][0] - res[-1][0]) < 1e-6 and abs(res[0][1] - res[-1][1]) < 1e-6:
        res[-1] = res[0]
        closed = True
    if closed and res and res[-1] != res[0]:
        res.append(res[0])
    if len(res) < 2 or (closed and len(res) < 3):
        return None
    return res


# --------------------------------------------------------------------------- walking


class _Walker:
    def __init__(self, svg, tolerance_mm, converted=None, text_ids=None, collect_text=False):
        self.svg = svg
        self.tol = max(float(tolerance_mm or DEFAULT_TOLERANCE_MM), 1e-4)
        self.converted = converted
        self.text_ids = text_ids or {}
        self.collect_text = collect_text
        self.texts = []
        self.result = Extraction()
        self.root_t = root_transform(svg)
        self.counts = {"image": 0, "text_missing": 0, "error": 0}

    def walk(self, elem, parent_t, depth=0, chain=()):
        name = _local(elem)
        if name is None or depth > _MAX_DEPTH:
            return
        if _is_hidden(elem):
            return
        t = parent_t @ _transform(elem)
        if name in GROUP_TAGS:
            if name == "svg" and depth > 0:  # nested <svg>: honour x/y only
                t = t @ inkex.Transform(translate=(_num(elem, "x"), _num(elem, "y")))
            for child in elem:
                self.walk(child, t, depth + 1, chain)
        elif name == "use":
            target = _href_target(elem)
            tid = target.get("id") if target is not None else None
            if target is None or tid in chain:
                return
            t = t @ inkex.Transform(translate=(_num(elem, "x"), _num(elem, "y")))
            self.walk(target, t, depth + 1, chain + (tid,))
        elif name in TEXT_TAGS:
            self.text(elem, parent_t, depth, chain)
        elif name in SHAPE_TAGS:
            if not self.collect_text:
                self.shape(elem, t)
        elif name in IMAGE_TAGS:
            self.counts["image"] += 1
        # everything else (defs, clipPath, mask, marker, style, title, ...) is ignored

    def text(self, elem, parent_t, depth, chain):
        if self.collect_text:
            if elem not in self.texts:
                self.texts.append(elem)
            return
        conv = None
        if self.converted is not None:
            tid = self.text_ids.get(elem) or elem.get("id")
            if tid:
                try:
                    conv = self.converted.getElementById(tid)
                except Exception:
                    conv = None
        if conv is None or _local(conv) in TEXT_TAGS:
            self.counts["text_missing"] += 1
            return
        # the outlined element sits at the same place in the converted tree, so the
        # accumulated parent transform still applies (it carries its own transform).
        self.walk(conv, parent_t, depth + 1, chain)

    def shape(self, elem, t):
        if _is_invisible(elem):
            return
        try:
            path = elem.path if hasattr(elem, "path") else inkex.Path(elem.get("d") or "")
        except Exception:
            self.counts["error"] += 1
            return
        m = self.root_t @ t
        scale = _sigma_max(m)
        if scale <= 1e-12:
            return
        tol_local = self.tol / scale
        try:
            subpaths = path_to_polylines(path, tol_local)
        except Exception:
            self.counts["error"] += 1
            return
        if not subpaths:
            return
        color = element_color(elem)
        (a, c, e), (b, d, f) = m.matrix
        for pts, closed in subpaths:
            mapped = [(a * x + c * y + e, b * x + d * y + f) for x, y in pts]
            poly = _clean(mapped, closed)
            if poly:
                self.result.polylines.append(poly)
                self.result.colors.append(color)

    def finish(self):
        r = self.result
        if self.counts["image"]:
            r.warn(
                "Skipped %d bitmap image(s): images can't be cut. "
                "Use Path > Trace Bitmap first." % self.counts["image"]
            )
        if self.counts["text_missing"]:
            r.warn(
                "Skipped %d text object(s) that could not be converted to paths. "
                "Use Path > Object to Path on them and try again." % self.counts["text_missing"]
            )
        if self.counts["error"]:
            r.warn("Skipped %d object(s) with unreadable geometry." % self.counts["error"])
        return r


def top_level(elements):
    """Drop elements whose ancestor is also in the list (avoid cutting twice)."""
    elements = list(elements)
    ids = set(id(e) for e in elements)
    keep = []
    seen = set()
    for e in elements:
        if id(e) in seen:
            continue
        seen.add(id(e))
        p = e.getparent()
        nested = False
        while p is not None:
            if id(p) in ids:
                nested = True
                break
            p = p.getparent()
        if not nested:
            keep.append(e)
    return keep


def document_elements(svg):
    """Everything drawable in the document (for 'Cut Whole Document')."""
    return [c for c in svg if _local(c) not in (None, "defs", "metadata", "title", "desc", "style", "script")]


def find_text(svg, elements):
    """Text/flowRoot elements reachable from elements (through groups and clones)."""
    w = _Walker(svg, DEFAULT_TOLERANCE_MM, collect_text=True)
    for e in top_level(elements):
        if ancestors_hidden(e):
            continue
        w.walk(e, parent_transform(e))
    return w.texts


def extract(svg, elements, tolerance_mm=DEFAULT_TOLERANCE_MM, converted=None, text_ids=None):
    """Convert elements (inkex elements of svg) to an :class:`Extraction`.

    converted: optional SvgDocumentElement returned by :func:`convert_text_to_paths`;
    text_ids: mapping original text element -> id in converted (for id-less texts).
    Without converted, text is skipped with a warning.
    """
    w = _Walker(svg, tolerance_mm, converted=converted, text_ids=text_ids)
    for e in top_level(elements):
        if ancestors_hidden(e):
            continue
        w.walk(e, parent_transform(e))
    return w.finish()


# --------------------------------------------------------------------------- text -> path


def find_inkscape():
    """Path of the Inkscape console executable, or None."""
    cand = []
    env = os.environ.get("INKSCAPE_COMMAND")
    if env:
        cand.append(env)
    bindir = os.path.dirname(sys.executable)
    cand += [os.path.join(bindir, "inkscape.com"), os.path.join(bindir, "inkscape.exe")]
    if sys.platform == "win32":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        cand.append(os.path.join(pf, "Inkscape", "bin", "inkscape.com"))
    for c in cand:
        if c and os.path.isfile(c):
            return c
    return shutil.which("inkscape")


def text_to_path_actions(ids, out_svg):
    """Inkscape 1.4 action string that outlines the given ids and exports a plain SVG."""
    return "select-by-id:{};object-to-path;export-filename:{};export-plain-svg;export-do".format(
        ",".join(ids), out_svg
    )


def convert_text_to_paths(svg, texts, workdir=None, inkscape=None, timeout=180):
    """Outline texts using the Inkscape CLI on a temporary copy of svg.

    Returns (converted_root, text_ids) where text_ids maps each original text element
    to the id it has in converted_root.  Raises RuntimeError on failure.
    The original document is never modified.
    """
    inkscape = inkscape or find_inkscape()
    if not inkscape:
        raise RuntimeError("Inkscape executable not found")
    root = svg.getroottree().getroot()
    clone = copy.deepcopy(root)
    wanted = set(id(t) for t in texts)
    text_ids = {}
    used = set(e.get("id") for e in root.iter() if isinstance(e.tag, str) and e.get("id"))
    n = 0
    for orig, dup in zip(root.iter(), clone.iter()):
        if id(orig) not in wanted:
            continue
        tid = orig.get("id")
        if not tid:
            n += 1
            while "vinylcut-text-%d" % n in used:
                n += 1
            tid = "vinylcut-text-%d" % n
            dup.set("id", tid)
        text_ids[orig] = tid
    if len(text_ids) != len(wanted):
        raise RuntimeError("could not map text elements")

    tmp = tempfile.mkdtemp(prefix="t2p-", dir=workdir)
    try:
        src = os.path.join(tmp, "in.svg")
        out = os.path.join(tmp, "out.svg")
        acts = os.path.join(tmp, "actions.txt")
        with open(src, "wb") as fh:
            fh.write(etree.tostring(clone, xml_declaration=True, encoding="UTF-8"))
        with open(acts, "w", encoding="utf-8") as fh:
            fh.write(text_to_path_actions(text_ids.values(), out))
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        proc = subprocess.run(
            [inkscape, src, "--actions-file=" + acts],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            cwd=tmp,
            **kwargs
        )
        if not os.path.isfile(out) or os.path.getsize(out) == 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()
            raise RuntimeError("Inkscape produced no output (exit %s) %s" % (proc.returncode, err[-500:]))
        converted = inkex.load_svg(out).getroot()
        return converted, text_ids
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
