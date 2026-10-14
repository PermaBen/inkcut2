# InkCut2 — Inkscape → vinyl cutter plugin

Target: Inkscape 1.4.x on Windows, its bundled Python 3.12 (`C:\Program Files\Inkscape\bin\python.exe`),
which ships `gi` (GTK 3.24 + cairo), `pyserial` 3.5, `lxml`, `numpy`. **No other third-party deps.**
Primary device: GCC Expert II (HPGL). Must also speak the InkCut "Generic Driver" protocols.

## Workflow
1. User selects objects in Inkscape → *Extensions ▸ InkCut2 ▸ Cut Selection…*
2. `inkcut2.py` (inkex effect) converts the selection (text → path, groups, transforms, curves flattened)
   into polylines in **mm**, writes a *design JSON* to `%TEMP%`, launches the GUI **detached**
   (`pythonw.exe -m inkcut2.gui <design.json>`) and returns immediately, leaving the document unchanged.
3. GUI: preview + job/device/connection settings → send to cutter (or save .plt).

## File layout & ownership (one owner per file — do not edit files you don't own)
```
Custom Plugin/
  inkcut2.inx, inkcut2.py           # Inkscape integration        (agent D)
  install.ps1, README.md              #                             (agent D)
  inkcut2/__init__.py                # version only                (lead)
  inkcut2/geometry.py                # pure geometry               (agent B)
  inkcut2/job.py                     # JobSettings + Job pipeline  (agent B)
  inkcut2/devices.py                 # profiles, presets, InkCut import (agent A)
  inkcut2/protocols.py               # HPGL/DMPL/GPGL/CAMM encoders(agent A)
  inkcut2/transports.py              # serial/printer/file/network (agent A)
  inkcut2/gui.py                     # GTK3 window + preview       (agent C)
  tests/test_*.py                     # unittest (NOT pytest) — each agent owns its own test files
  docs/research.md                    # device/protocol research    (agent A)
```
Run tests with: `& "C:\Program Files\Inkscape\bin\python.exe" -m unittest discover -s tests -t .`
(from the project root). Stdlib `unittest` only.

## Coordinate systems
* **Design coords** (from Inkscape): mm, SVG orientation — x right, y **down**.
* **Material coords** (everything in `job.py`, preview, protocols input): mm, x = along media **feed/length**,
  y = **across** media width, origin (0,0) = cutter origin corner, y **up** when previewed
  (preview draws x to the right, y upward, like HPGL). `Job.layout()` converts design→material by
  flipping y so artwork reads upright when previewed.
* **Device coords**: integer steps. Conversion material→device happens only in
  `devices.DeviceProfile.to_device(x_mm, y_mm) -> (int, int)` using `steps_per_mm`, `swap_axes`,
  `invert_x`, `invert_y` (invert relative to width/length).

## Shared types
```python
Point = tuple[float, float]        # mm
Polyline = list[Point]             # closed iff first point == last point (within 1e-6)
```

## Design JSON (written by inkcut2.py, read by gui.py)
```json
{"version": 1, "name": "drawing.svg", "units": "mm",
 "polylines": [[[x, y], [x, y], ...], ...],
 "colors": ["#000000", ...]}        // optional, stroke/fill colour per polyline, same length
```

## geometry.py (agent B) — pure functions, never mutate inputs, return new lists
```python
def bbox(polys) -> tuple[float, float, float, float]          # minx, miny, maxx, maxy ; (0,0,0,0) if empty
def translate(polys, dx, dy) -> list[Polyline]
def scale(polys, sx, sy, origin: Point = (0, 0)) -> list[Polyline]
def rotate(polys, degrees, origin: Point | None = None) -> list[Polyline]   # CCW in a y-up frame; None = bbox centre
def mirror(polys, horizontal: bool, vertical: bool) -> list[Polyline]      # about bbox centre; horizontal = flip x
def normalize(polys) -> list[Polyline]                          # bbox min moved to (0,0)
def grid_array(polys, rows, cols, spacing_x, spacing_y) -> list[list[Polyline]]   # list of copies; copy i offset by (col*(w+sx), row*(h+sy))
def max_grid(item_w, item_h, area_w, area_h, spacing_x, spacing_y) -> tuple[int, int]  # (rows, cols) that fit
def order_paths(polys, mode: str, start: Point = (0, 0)) -> list[Polyline]
    # modes: "as_is", "shortest" (greedy nearest neighbour, may reverse open paths / rotate start
    # vertex of closed paths), "inside_out" (contained contours before their containers, then nearest),
    # "by_x" (sorted along feed, good for long rolls)
def apply_overcut(polys, overcut_mm) -> list[Polyline]          # closed paths continue past start by overcut_mm
def apply_blade_offset(polys, offset_mm, angle_threshold_deg=30) -> list[Polyline]   # drag-knife compensation
def path_length(polys) -> float
def travel_moves(polys, start: Point = (0, 0)) -> list[tuple[Point, Point]]   # pen-up moves, for preview
def weed_border(bbox, margin) -> Polyline                       # closed rectangle around bbox grown by margin
def test_cut_shape(size_mm=20.0) -> list[Polyline]              # square with a triangle inside (standard test cut)
def is_closed(poly) -> bool
```

## job.py (agent B)
```python
@dataclass
class JobSettings:
    # transform (applied to one copy, in this order: scale → mirror → rotate)
    scale_pct: float = 100.0
    mirror_h: bool = False            # flip left/right (e.g. heat-transfer vinyl)
    mirror_v: bool = False
    rotation: float = 0.0             # degrees, any value; GUI offers 0/90/180/270 + free entry
    # array / copies
    rows: int = 1
    cols: int = 1
    spacing_x: float = 2.0            # mm gap between copies along feed
    spacing_y: float = 2.0            # mm gap across width
    auto_fill: bool = False           # rows/cols computed with geometry.max_grid to fill material
    # placement on material
    offset_x: float = 0.0             # mm from origin (+ device padding)
    offset_y: float = 0.0
    # cutting
    passes: int = 1
    pass_mode: str = "per_path"       # "per_path" (repeat each path N times) | "per_job"
    speed: float | None = None        # cm/s ; None → device default
    force: float | None = None        # grams ; None → device default
    overcut: float | None = None      # mm ; None → device default
    blade_offset: float | None = None # mm ; None → device default
    order: str = "inside_out"
    weed_border: bool = False
    weed_margin: float = 3.0
    def to_dict(self) -> dict ; @classmethod from_dict(cls, d) -> "JobSettings"   # ignore unknown keys

def load_last_settings() -> JobSettings      # %APPDATA%\inkcut2\job.json (missing/corrupt → defaults)
def save_last_settings(s: JobSettings) -> None

class Job:
    def __init__(self, design: list[Polyline], settings: JobSettings, device: "DeviceProfile")
    def copy_geometry(self) -> list[Polyline]          # one copy: design→material (y flip), scale, mirror, rotate, normalised to (0,0)
    def layout(self) -> list[list[Polyline]]           # all copies, placed in material coords incl. offset + device padding; + weed border as its own last "copy"
    def preview_paths(self) -> list[Polyline]          # final cut order for ONE pass (ordered, overcut, blade offset applied)
    def toolpaths(self) -> list[Polyline]              # what gets sent: preview_paths with passes expanded per pass_mode
    def effective(self, name) -> float                 # resolved speed/force/overcut/blade_offset (setting or device default)
    def stats(self) -> dict  # {"copies", "paths", "cut_length_mm", "travel_length_mm", "size_mm": (w, h), "est_time_s"}
    def warnings(self) -> list[str]                    # e.g. "Design exceeds material width by 12.3 mm"
    def fits(self) -> bool
```
`Job` must also work with the device stand-in `types.SimpleNamespace(width_mm=610, length_mm=5000,
padding=(0,0,0,0), default_speed=30, default_force=80, default_overcut=0.5, default_blade_offset=0.25)`
so agent B can test without agent A's code.

## devices.py (agent A)
```python
@dataclass
class ConnectionSettings:
    kind: str = "serial"             # "serial" | "printer" (Windows raw spooler — GCC USB driver installs as a printer) | "file" | "network"
    port: str = ""                   # e.g. "COM4"
    baudrate: int = 9600
    bytesize: int = 8                # 5,6,7,8
    parity: str = "N"                # "N","E","O","M","S"
    stopbits: float = 1              # 1, 1.5, 2
    flow: str = "rtscts"             # "none" | "rtscts" | "xonxoff" | "dsrdtr"
    printer_name: str = ""
    file_path: str = ""
    host: str = ""
    net_port: int = 9100
    chunk_size: int = 512            # bytes per write
    chunk_delay_ms: int = 0

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
    def to_device(self, x_mm, y_mm) -> tuple[int, int]
    def to_dict(self) -> dict ; @classmethod from_dict(cls, d)

PRESETS: dict[str, DeviceProfile]        # at least "GCC Expert II 24", "GCC Expert II 52", "Generic HPGL"
def load_profiles() -> list[DeviceProfile]       # %APPDATA%\inkcut2\devices.json, else presets
def save_profiles(profiles) -> None
def import_inkcut_devices(path=None) -> list[DeviceProfile]   # default ~/.config/inkcut/inkcut.device.json (jsonpickle format)
```

## protocols.py (agent A)
```python
class Protocol:
    id: str ; name: str
    def encode(self, paths: list[Polyline], device: DeviceProfile, speed: float, force: float) -> bytes
PROTOCOLS: dict[str, Protocol]    # "hpgl", "dmpl", "gpgl", "camm" (+ anything else InkCut's generic driver offers)
def encode(paths, device, speed, force) -> bytes     # dispatch on device.protocol
```
Input paths are in **material mm**, already ordered and with passes expanded. Protocol does
material→device via `device.to_device`, emits init (speed/force), PU/PD moves, finish (pen up, return/park).

## transports.py (agent A)
```python
def list_serial_ports() -> list[tuple[str, str]]       # (device, description)
def list_printers() -> list[str]                       # Windows printers via ctypes/winspool (no pywin32!)
class Transport:
    def open(self) -> None
    def write(self, data: bytes, progress=None, cancel_event=None) -> None   # progress(sent, total); honours chunking/flow; raises on error; stops if cancel_event.is_set()
    def close(self) -> None
def make_transport(conn: ConnectionSettings) -> Transport
```
`write` is blocking; the GUI calls it from a worker thread and marshals progress with `GLib.idle_add`.

## gui.py (agent C)
`python -m inkcut2.gui <design.json>` (also `--demo` to open with a built-in sample design, and
`--snapshot out.png` to render the preview offscreen and exit — used for automated checks).
GTK 3 window, see agent C brief for features.
