"""Pure 2-D polyline geometry for InkCut2.

Conventions (see docs/ARCHITECTURE.md):

* ``Point = tuple[float, float]`` in mm.
* ``Polyline = list[Point]``; a polyline is *closed* iff it has at least 3 points and
  its first point equals its last point within 1e-6 mm.
* Every function is pure: inputs are never mutated and fresh lists of fresh tuples are
  returned (points given as lists, e.g. from JSON, come back as tuples of floats).
* Angles are CCW in a y-up frame (material coordinates).

Only the standard library is used.
"""

import math

CLOSE_EPS = 1e-6          # first/last point tolerance for "closed"
_DUP_EPS = 1e-9           # consecutive points closer than this are duplicates

__all__ = [
    "bbox", "translate", "scale", "rotate", "mirror", "normalize", "grid_array",
    "max_grid", "order_paths", "apply_overcut", "apply_blade_offset", "path_length",
    "travel_moves", "weed_border", "test_cut_shape", "is_closed",
    "polygon_area", "point_in_polygon", "ORDER_MODES",
]

ORDER_MODES = ("as_is", "shortest", "inside_out", "by_x")


# --------------------------------------------------------------------------- basics

def _pts(poly):
    """Fresh list of float tuples."""
    return [(float(p[0]), float(p[1])) for p in poly]


def _copy(polys):
    return [_pts(p) for p in polys]


def is_closed(poly) -> bool:
    """True iff the polyline has >= 3 points and first == last (within 1e-6 mm)."""
    if len(poly) < 3:
        return False
    a, b = poly[0], poly[-1]
    return abs(a[0] - b[0]) <= CLOSE_EPS and abs(a[1] - b[1]) <= CLOSE_EPS


def _poly_bbox(poly):
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return (float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys)))


def bbox(polys):
    """(minx, miny, maxx, maxy) over all points of all polylines; (0,0,0,0) if empty."""
    minx = miny = math.inf
    maxx = maxy = -math.inf
    for poly in polys:
        if not poly:
            continue
        a, b, c, d = _poly_bbox(poly)
        if a < minx:
            minx = a
        if b < miny:
            miny = b
        if c > maxx:
            maxx = c
        if d > maxy:
            maxy = d
    if minx == math.inf:
        return (0.0, 0.0, 0.0, 0.0)
    return (minx, miny, maxx, maxy)


def translate(polys, dx, dy):
    dx = float(dx)
    dy = float(dy)
    return [[(p[0] + dx, p[1] + dy) for p in poly] for poly in polys]


def scale(polys, sx, sy, origin=(0, 0)):
    """Scale by (sx, sy) about ``origin``."""
    ox, oy = float(origin[0]), float(origin[1])
    sx = float(sx)
    sy = float(sy)
    return [[(ox + (p[0] - ox) * sx, oy + (p[1] - oy) * sy) for p in poly] for poly in polys]


def _cos_sin(degrees):
    """cos/sin with exact values for multiples of 90 degrees (no float noise)."""
    d = float(degrees) % 360.0
    q = d / 90.0
    if abs(q - round(q)) < 1e-12:
        return ((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0))[int(round(q)) % 4]
    r = math.radians(d)
    return math.cos(r), math.sin(r)


def _centre(polys):
    minx, miny, maxx, maxy = bbox(polys)
    return ((minx + maxx) / 2.0, (miny + maxy) / 2.0)


def rotate(polys, degrees, origin=None):
    """Rotate CCW (y-up frame) by ``degrees`` about ``origin`` (None = bbox centre)."""
    if origin is None:
        origin = _centre(polys)
    ox, oy = float(origin[0]), float(origin[1])
    c, s = _cos_sin(degrees)
    out = []
    for poly in polys:
        q = []
        for p in poly:
            x = p[0] - ox
            y = p[1] - oy
            q.append((ox + x * c - y * s, oy + x * s + y * c))
        out.append(q)
    return out


def mirror(polys, horizontal, vertical):
    """Mirror about the bbox centre. horizontal=True flips x (left/right), vertical flips y."""
    if not horizontal and not vertical:
        return _copy(polys)
    cx, cy = _centre(polys)
    sx = -1.0 if horizontal else 1.0
    sy = -1.0 if vertical else 1.0
    return scale(polys, sx, sy, (cx, cy))


def normalize(polys):
    """Translate so the bbox minimum is at (0, 0)."""
    minx, miny, _, _ = bbox(polys)
    return translate(polys, -minx, -miny)


def grid_array(polys, rows, cols, spacing_x, spacing_y):
    """Array copies of ``polys``.

    Returns a list of copies (each a list of polylines). Copy (row, col) is offset by
    ``(col * (w + spacing_x), row * (h + spacing_y))`` where (w, h) is the bbox size, i.e.
    **cols run along x** (material feed) and **rows run along y** (across the width).
    Copies are emitted row-major (row 0: col 0..cols-1, row 1: ...). rows/cols <= 0 -> [].
    """
    rows = int(rows)
    cols = int(cols)
    if rows <= 0 or cols <= 0:
        return []
    minx, miny, maxx, maxy = bbox(polys)
    px = (maxx - minx) + float(spacing_x)
    py = (maxy - miny) + float(spacing_y)
    base = _copy(polys)
    out = []
    for r in range(rows):
        for c in range(cols):
            out.append(translate(base, c * px, r * py))
    return out


def _fit_count(item, area, spacing):
    item = max(0.0, float(item))
    area = float(area)
    spacing = float(spacing)
    if area < item - 1e-9 or area < 0:
        return 0
    pitch = item + spacing
    if pitch <= 1e-9:
        return 1          # zero-size item with no spacing: avoid an infinite count
    return max(0, int(math.floor((area + spacing) / pitch + 1e-9)))


def max_grid(item_w, item_h, area_w, area_h, spacing_x, spacing_y):
    """(rows, cols) of items (item_w x item_h) that fit in area_w x area_h.

    ``n`` items with gap ``s`` need ``n*item + (n-1)*s``. cols are counted along the w/x
    axis, rows along the h/y axis (same convention as :func:`grid_array`). Either may be 0.
    """
    return (_fit_count(item_h, area_h, spacing_y), _fit_count(item_w, area_w, spacing_x))


def path_length(polys):
    total = 0.0
    for poly in polys:
        for i in range(1, len(poly)):
            a = poly[i - 1]
            b = poly[i]
            total += math.hypot(b[0] - a[0], b[1] - a[1])
    return total


def travel_moves(polys, start=(0, 0), return_to_start=False):
    """Pen-up moves ``(from, to)``: start -> first path, end of path i -> start of path i+1.

    Zero-length moves are omitted. With ``return_to_start=True`` a final move from the end of
    the last path back to ``start`` is appended (the default omits it).
    """
    start = (float(start[0]), float(start[1]))
    pos = start
    moves = []
    for poly in polys:
        if not poly:
            continue
        p0 = (float(poly[0][0]), float(poly[0][1]))
        if math.hypot(p0[0] - pos[0], p0[1] - pos[1]) > _DUP_EPS:
            moves.append((pos, p0))
        pos = (float(poly[-1][0]), float(poly[-1][1]))
    if return_to_start and math.hypot(pos[0] - start[0], pos[1] - start[1]) > _DUP_EPS:
        moves.append((pos, start))
    return moves


def weed_border(bbox, margin):
    """Closed CCW rectangle around ``bbox`` grown by ``margin`` on every side."""
    minx, miny, maxx, maxy = (float(v) for v in bbox)
    m = float(margin)
    a = (minx - m, miny - m)
    return [a, (maxx + m, miny - m), (maxx + m, maxy + m), (minx - m, maxy + m), a]


def test_cut_shape(size_mm=20.0):
    """Standard test cut: a closed square (size_mm) at the origin with a closed triangle inside.

    Returned as ``[triangle, square]`` so even "as_is" order cuts the inside first.
    """
    s = float(size_mm)
    tri = [(0.25 * s, 0.25 * s), (0.75 * s, 0.25 * s), (0.5 * s, 0.75 * s), (0.25 * s, 0.25 * s)]
    sq = [(0.0, 0.0), (s, 0.0), (s, s), (0.0, s), (0.0, 0.0)]
    return [tri, sq]


def polygon_area(poly):
    """Signed shoelace area (positive = CCW in a y-up frame). Works on open or closed lists."""
    n = len(poly)
    if n < 3:
        return 0.0
    a = 0.0
    x0, y0 = poly[-1][0], poly[-1][1]
    for p in poly:
        x1, y1 = p[0], p[1]
        a += x0 * y1 - x1 * y0
        x0, y0 = x1, y1
    return a / 2.0


def point_in_polygon(pt, poly):
    """Even-odd ray-casting test (boundary points are undefined)."""
    x, y = pt[0], pt[1]
    inside = False
    n = len(poly)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i][0], poly[i][1]
        xj, yj = poly[j][0], poly[j][1]
        if (yi > y) != (yj > y):
            xint = xi + (y - yi) * (xj - xi) / (yj - yi)
            if x < xint:
                inside = not inside
        j = i
    return inside


# ------------------------------------------------------------------- path ordering

_MAX_KEYS = 32      # sampled vertices per closed path used to *select* the nearest path
_SMALL_N = 40       # below this, brute-force nearest neighbour is faster than the grid


def _keys_for(poly):
    if is_closed(poly):
        verts = poly[:-1]
        if len(verts) > _MAX_KEYS:
            step = len(verts) / float(_MAX_KEYS)
            verts = [verts[int(i * step)] for i in range(_MAX_KEYS)]
        return verts
    if len(poly) == 1:
        return [poly[0]]
    return [poly[0], poly[-1]]


def _orient(poly, pos):
    """Copy of ``poly`` re-started at the vertex nearest ``pos`` (closed) or reversed (open)."""
    px, py = pos
    if is_closed(poly):
        verts = poly[:-1]
        best = 0
        bd = math.inf
        for i, p in enumerate(verts):
            d = (p[0] - px) ** 2 + (p[1] - py) ** 2
            if d < bd:
                bd = d
                best = i
        if best == 0:
            return _pts(poly)
        rot = _pts(verts[best:] + verts[:best])
        rot.append(rot[0])
        return rot
    if len(poly) >= 2:
        a, b = poly[0], poly[-1]
        if (b[0] - px) ** 2 + (b[1] - py) ** 2 < (a[0] - px) ** 2 + (a[1] - py) ** 2:
            return _pts(reversed(poly))
    return _pts(poly)


class _KeyGrid:
    """Uniform bucket grid over key points supporting 'pop nearest item'.

    Items are removed eagerly from their buckets; the grid is rebuilt with a coarser cell when
    the live population drops below a quarter of what it was built with, so ring searches stay
    cheap near the end of a greedy tour.
    """

    def __init__(self, keys):
        self.keys = keys
        self.alive = set(range(len(keys)))
        self._build(sorted(self.alive))

    def _build(self, items):
        keys = self.keys
        minx = miny = math.inf
        maxx = maxy = -math.inf
        n = 0
        for i in items:
            for x, y in keys[i]:
                n += 1
                if x < minx:
                    minx = x
                if x > maxx:
                    maxx = x
                if y < miny:
                    miny = y
                if y > maxy:
                    maxy = y
        w = maxx - minx
        h = maxy - miny
        if w * h > 1e-12:
            cell = math.sqrt(w * h / max(1, n)) * 1.5
        elif max(w, h) > 1e-12:
            cell = max(w, h) / max(1, n) * 2.0
        else:
            cell = 1.0
        cell = max(cell, 1e-6)
        # keep the grid dimension bounded
        while (w / cell + 1) * (h / cell + 1) > 4.0 * n + 16:
            cell *= 1.5
        self.cell = cell
        self.inv = 1.0 / cell
        self.ox = minx
        self.oy = miny
        self.nx = int(w * self.inv) + 1
        self.ny = int(h * self.inv) + 1
        cells = {}
        item_cells = {}
        inv = self.inv
        for i in items:
            cs = set()
            for x, y in keys[i]:
                k = (int((x - minx) * inv), int((y - miny) * inv))
                bucket = cells.get(k)
                if bucket is None:
                    bucket = cells[k] = {}
                lst = bucket.get(i)
                if lst is None:
                    bucket[i] = [(x, y)]
                else:
                    lst.append((x, y))
                cs.add(k)
            item_cells[i] = cs
        self.cells = cells
        self.item_cells = item_cells
        self.built = len(items)

    def remove(self, i):
        cells = self.cells
        for k in self.item_cells.pop(i):
            bucket = cells[k]
            del bucket[i]
            if not bucket:
                del cells[k]
        self.alive.discard(i)

    def pop_nearest(self, pos):
        if not self.alive:
            return None
        if self.built > 64 and len(self.alive) * 4 < self.built:
            self._build(sorted(self.alive))
        qx, qy = float(pos[0]), float(pos[1])
        inv = self.inv
        cx = int(math.floor((qx - self.ox) * inv))
        cy = int(math.floor((qy - self.oy) * inv))
        nx1 = self.nx - 1
        ny1 = self.ny - 1
        r = max(0, -cx, cx - nx1, -cy, cy - ny1)
        rmax = max(cx, nx1 - cx, cy, ny1 - cy, r)
        cells = self.cells
        best_d = math.inf
        best_i = None
        cell = self.cell
        while r <= rmax:
            if r == 0:
                ring = ((cx, cy),)
            else:
                ring = []
                x0 = max(cx - r, 0)
                x1 = min(cx + r, nx1)
                for yy in (cy - r, cy + r):
                    if 0 <= yy <= ny1:
                        for xx in range(x0, x1 + 1):
                            ring.append((xx, yy))
                y0 = max(cy - r + 1, 0)
                y1 = min(cy + r - 1, ny1)
                for xx in (cx - r, cx + r):
                    if 0 <= xx <= nx1:
                        for yy in range(y0, y1 + 1):
                            ring.append((xx, yy))
            for k in ring:
                bucket = cells.get(k)
                if bucket is None:
                    continue
                for i, pts in bucket.items():
                    for x, y in pts:
                        d = (x - qx) * (x - qx) + (y - qy) * (y - qy)
                        if d < best_d or (d == best_d and i < best_i):
                            best_d = d
                            best_i = i
            if best_i is not None:
                lim = r * cell
                if best_d <= lim * lim:
                    break
            r += 1
        if best_i is None:          # should not happen; fall back to any live item
            best_i = min(self.alive)
        self.remove(best_i)
        return best_i


def _nn_visit(keys, start, emit):
    """Greedy nearest-neighbour visit of items described by key-point lists.

    ``emit(index, pos) -> new_pos`` is called once per item in visiting order.
    Returns the final position.
    """
    pos = (float(start[0]), float(start[1]))
    n = len(keys)
    if n == 0:
        return pos
    if n <= _SMALL_N:
        remaining = list(range(n))
        while remaining:
            px, py = pos
            bd = math.inf
            bj = 0
            for j, item in enumerate(remaining):
                for x, y in keys[item]:
                    d = (x - px) * (x - px) + (y - py) * (y - py)
                    if d < bd:
                        bd = d
                        bj = j
            item = remaining.pop(bj)
            pos = emit(item, pos)
        return pos
    grid = _KeyGrid(keys)
    while True:
        item = grid.pop_nearest(pos)
        if item is None:
            break
        pos = emit(item, pos)
    return pos


def _order_shortest(paths, start):
    keys = [_keys_for(p) for p in paths]
    out = []

    def emit(i, pos):
        q = _orient(paths[i], pos)
        out.append(q)
        return q[-1]

    _nn_visit(keys, start, emit)
    return out


def _classify(pt, poly, eps=1e-7):
    """1 = strictly inside, -1 = outside, 0 = on the boundary (within eps)."""
    x, y = pt[0], pt[1]
    inside = False
    n = len(poly)
    j = n - 1
    eps2 = eps * eps
    for i in range(n):
        xi, yi = poly[i][0], poly[i][1]
        xj, yj = poly[j][0], poly[j][1]
        dx = xj - xi
        dy = yj - yi
        L2 = dx * dx + dy * dy
        t = ((x - xi) * dx + (y - yi) * dy) / L2 if L2 > 0 else 0.0
        t = 0.0 if t < 0 else (1.0 if t > 1 else t)
        ex = xi + t * dx - x
        ey = yi + t * dy - y
        if ex * ex + ey * ey <= eps2:
            return 0
        if (yi > y) != (yj > y):
            if x < xi + (y - yi) * dx / dy:
                inside = not inside
        j = i
    return 1 if inside else -1


def _contains(outer, inner):
    """Is ``inner`` inside closed polygon ``outer``?

    Samples vertices and edge midpoints of ``inner`` spread along it, ignores samples lying on
    ``outer``'s boundary (touching contours) and takes a best-of-3 vote of the decisive ones.
    """
    verts = inner[:-1] if is_closed(inner) else inner
    n = len(verts)
    samples = []
    for k in range(5):
        i = (k * n) // 5
        samples.append(verts[i])
        if n > 1:
            a = verts[i]
            b = verts[(i + 1) % n]
            samples.append(((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0))
    ins = outs = 0
    for p in samples:
        c = _classify(p, outer)
        if c > 0:
            ins += 1
        elif c < 0:
            outs += 1
        if ins >= 2 or outs >= 2:
            break
    return ins > outs


def _containment_parents(paths):
    """parent[i] = index of the smallest closed contour containing path i, or -1.

    Containers are bucketed by bbox in a uniform grid; a path only tests containers registered
    in the bucket of its first vertex whose bbox encloses its bbox and whose area is larger,
    smallest first, so the cost is ~linear for typical sticker sheets.
    """
    n = len(paths)
    parent = [-1] * n
    closed = [is_closed(p) for p in paths]
    bbs = [_poly_bbox(p) for p in paths]
    areas = [abs(polygon_area(p[:-1])) if closed[i] else 0.0 for i, p in enumerate(paths)]
    containers = [i for i in range(n) if closed[i] and areas[i] > 1e-12]
    if not containers:
        return parent
    sizes = sorted(max(bbs[i][2] - bbs[i][0], bbs[i][3] - bbs[i][1]) for i in containers)
    cell = sizes[len(sizes) // 2] or 1.0
    gminx = min(bbs[i][0] for i in containers)
    gminy = min(bbs[i][1] for i in containers)
    gmaxx = max(bbs[i][2] for i in containers)
    gmaxy = max(bbs[i][3] for i in containers)
    gw = gmaxx - gminx
    gh = gmaxy - gminy
    while (gw / cell + 1) * (gh / cell + 1) > 16384:
        cell *= 1.5
    inv = 1.0 / cell
    grid = {}
    for j in containers:
        b = bbs[j]
        ix0 = int((b[0] - gminx) * inv)
        ix1 = int((b[2] - gminx) * inv)
        iy0 = int((b[1] - gminy) * inv)
        iy1 = int((b[3] - gminy) * inv)
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                grid.setdefault((ix, iy), []).append(j)
    eps = 1e-9
    for i in range(n):
        b = bbs[i]
        p0 = paths[i][0]
        if p0[0] < gminx or p0[1] < gminy:
            continue
        cands = grid.get((int((p0[0] - gminx) * inv), int((p0[1] - gminy) * inv)))
        if not cands:
            continue
        ai = areas[i]
        good = []
        for j in cands:
            if j == i or areas[j] <= ai:
                continue
            bj = bbs[j]
            if (bj[0] <= b[0] + eps and bj[1] <= b[1] + eps
                    and bj[2] >= b[2] - eps and bj[3] >= b[3] - eps):
                good.append(j)
        if not good:
            continue
        good.sort(key=lambda j: areas[j])
        for j in good:
            if _contains(paths[j], paths[i]):
                parent[i] = j
                break
    return parent


def _order_inside_out(paths, start):
    n = len(paths)
    parent = _containment_parents(paths)
    children = [[] for _ in range(n)]
    roots = []
    for i in range(n):
        if parent[i] >= 0:
            children[parent[i]].append(i)
        else:
            roots.append(i)
    keys = [_keys_for(p) for p in paths]
    out = []

    def emit_subtree(node, pos):
        ch = children[node]
        if ch:
            pos = _nn_visit([keys[c] for c in ch], pos, lambda k, p: emit_subtree(ch[k], p))
        q = _orient(paths[node], pos)
        out.append(q)
        return q[-1]

    _nn_visit([keys[r] for r in roots], start, lambda k, p: emit_subtree(roots[k], p))
    return out


BY_X_BAND_MM = 50.0


def _order_by_x(paths, start, band=BY_X_BAND_MM):
    # Paths are grouped into bands of ``band`` mm by their *max* x and bands are cut in
    # increasing x, so the required media advance only moves forward band by band. A contained
    # contour has max x <= its container's max x, so it lands in the same or an earlier band;
    # inside a band the inside_out ordering (containment + nearest neighbour) is used, so
    # stickers stay safe and travel stays short.
    bands = {}
    for p in paths:
        bands.setdefault(int(math.floor(_poly_bbox(p)[2] / band)), []).append(p)
    out = []
    pos = (float(start[0]), float(start[1]))
    for k in sorted(bands):
        part = _order_inside_out(bands[k], pos)
        out.extend(part)
        pos = part[-1][-1]
    return out


def order_paths(polys, mode, start=(0, 0)):
    """Return the polylines in cutting order.

    Empty polylines are dropped. Modes:

    * ``"as_is"``: input order.
    * ``"shortest"``: greedy nearest neighbour from ``start`` (bucket-grid index, ~O(n));
      open paths may be reversed, closed paths re-started at their vertex nearest the knife.
    * ``"inside_out"``: every path lying inside a closed contour is cut before that contour
      (holes/details before the outline so the piece cannot shift). Implemented as a
      containment tree (parent = smallest enclosing closed contour) visited post-order; top
      level groups and siblings are chosen nearest-neighbour, so each sticker is finished
      (inner parts, then outline) before moving to the next one.
    * ``"by_x"``: sorted along the feed by each path's max x (ties: smaller bbox first), which
      keeps media movement monotonic and preserves inside-before-outside.
    """
    paths = [_pts(p) for p in polys if len(p) > 0]
    if mode == "as_is":
        return paths
    if mode == "shortest":
        return _order_shortest(paths, start)
    if mode == "inside_out":
        return _order_inside_out(paths, start)
    if mode == "by_x":
        return _order_by_x(paths, start)
    raise ValueError("unknown order mode %r (expected one of %s)" % (mode, ", ".join(ORDER_MODES)))


# ------------------------------------------------------------ overcut / blade offset

def _dedupe(poly):
    out = []
    for p in poly:
        p = (float(p[0]), float(p[1]))
        if out and abs(p[0] - out[-1][0]) <= _DUP_EPS and abs(p[1] - out[-1][1]) <= _DUP_EPS:
            continue
        out.append(p)
    return out


def _overcut_one(poly, overcut):
    pts = _dedupe(poly)
    if len(pts) < 3:
        return _pts(poly)
    pts[-1] = pts[0]
    nseg = len(pts) - 1
    perim = path_length([pts])
    if perim <= 1e-12:
        return _pts(poly)
    remaining = min(overcut, perim)      # never more than one extra lap
    out = list(pts)
    i = 0
    while remaining > 1e-12:
        a = pts[i]
        b = pts[i + 1]
        seg = math.hypot(b[0] - a[0], b[1] - a[1])
        if seg >= remaining - 1e-12:
            t = remaining / seg
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            break
        out.append(b)
        remaining -= seg
        i = (i + 1) % nseg
    return out


def apply_overcut(polys, overcut_mm):
    """Closed paths continue along themselves past the start by ``overcut_mm`` (the last
    partial segment is interpolated; capped at one full extra lap). Open paths are copied."""
    oc = float(overcut_mm or 0.0)
    if oc <= 0:
        return _copy(polys)
    return [_overcut_one(p, oc) if is_closed(p) else _pts(p) for p in polys]


def _unit(a, b):
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    L = math.hypot(dx, dy)
    return (dx / L, dy / L)


def _corner(out, p, ua, ub, d, thr):
    """Append the axis moves that turn the blade at corner ``p`` from direction ua to ub."""
    cross = ua[0] * ub[1] - ua[1] * ub[0]
    dot = ua[0] * ub[0] + ua[1] * ub[1]
    turn = math.atan2(cross, dot)                     # signed, (-pi, pi]
    if abs(turn) > thr:
        a0 = math.atan2(ua[1], ua[0])
        steps = max(2, int(math.ceil(abs(turn) / (math.pi / 12.0))))   # <= 15 deg per step
        for k in range(1, steps):
            ang = a0 + turn * k / steps
            out.append((p[0] + d * math.cos(ang), p[1] + d * math.sin(ang)))
    out.append((p[0] + d * ub[0], p[1] + d * ub[1]))


def _blade_one(poly, d, thr):
    closed = is_closed(poly)
    pts = _dedupe(poly)
    if closed:
        if len(pts) > 1 and math.hypot(pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1]) <= CLOSE_EPS:
            pts = pts[:-1]
        if len(pts) < 2:
            return _pts(poly)
        n = len(pts)
        dirs = [_unit(pts[i], pts[(i + 1) % n]) for i in range(n)]
        ul = dirs[-1]
        p0 = pts[0]
        out = [(p0[0] + d * ul[0], p0[1] + d * ul[1])]
        _corner(out, p0, ul, dirs[0], d, thr)
        for i in range(n):
            p = pts[(i + 1) % n]
            u = dirs[i]
            out.append((p[0] + d * u[0], p[1] + d * u[1]))
            if i + 1 < n:
                _corner(out, p, u, dirs[i + 1], d, thr)
        out[-1] = out[0]
        return _dedupe(out) if len(out) > 3 else out
    if len(pts) < 2:
        return _pts(poly)
    dirs = [_unit(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    out = [pts[0]]
    for i, u in enumerate(dirs):
        p = pts[i + 1]
        out.append((p[0] + d * u[0], p[1] + d * u[1]))
        if i + 1 < len(dirs):
            _corner(out, p, u, dirs[i + 1], d, thr)
    return _dedupe(out)


def apply_blade_offset(polys, offset_mm, angle_threshold_deg=30):
    """Drag-knife (tangential offset) compensation.

    A drag knife's tip trails the controlled axis by ``d = offset_mm`` along the direction of
    travel, so the *axis* path is generated such that the *tip* follows the design:

    * every segment P->Q is extended by d: the axis goes to ``Q + d*u`` (u = unit direction),
      which leaves the tip exactly on Q;
    * at a vertex where the direction changes by more than ``angle_threshold_deg`` the axis
      sweeps an arc of radius d centred on the vertex, from ``Q + d*u_in`` to ``Q + d*u_out``
      (shorter turn direction, <= 15 deg per arc step), so the blade swivels in place;
      smaller direction changes use the straight chord between those two points;
    * closed paths treat the start as a corner too: the axis starts at ``P0 + d*u_last``,
      swivels onto the first segment and ends back at the same point (output stays closed);
    * open paths start at P0 (blade orientation unknown there) and end at ``Pn + d*u_last``.

    Every output point is within d of the original outline. offset <= 0 -> unchanged copy.
    """
    d = float(offset_mm or 0.0)
    if d <= 0:
        return _copy(polys)
    thr = math.radians(max(0.0, float(angle_threshold_deg)))
    return [_blade_one(p, d, thr) if len(p) >= 2 else _pts(p) for p in polys]
