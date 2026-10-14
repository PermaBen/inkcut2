# InkCut2 device and protocol research

Agent A. Sources are listed at the end of each section. **[verify]** marks
anything that should be checked on the real machine.

## 1. GCC Expert II (the user's cutter)

| Item | Value | Source |
|---|---|---|
| Models | EX II-24 / 24LX, EX II-52 / 52LX (LX adds AAS II contour-mark sensor) | Expert II manual, spec table p.72 |
| Max cutting width | 600 mm (23.6") for 24; 1320 mm (51.96") for 52 | manual p.72 |
| Max media width | 719 mm (28.3") / 1470 mm (57.87") | manual p.72 |
| Max cutting length | 25000 mm ("Poll Size", VLCD) | manual §3.2, p.45 |
| Max speed | 705 mm/s (24), 635 mm/s (52), measured at 45° | manual p.72 |
| Cutting force | 0–350 g (VLCD "Blade Force 0–350") | manual p.45, p.72 |
| Driver defaults | speed **30 cm/s**, force **80 g**, offset **0.25 mm** | manual §3.5 Pen page, p.53; troubleshooting ("default tool force is 80 gf") |
| Blade offset choices (firmware) | 0.000, 0.175, **0.250 (default)**, 0.275, 0.300, 0.500, 0.750, 1.000 mm | manual p.46 |
| Mechanical resolution | 0.009 mm (24), 0.012 mm (52) | manual p.72 |
| **Software resolution** | **0.025 mm, so 40 steps/mm (standard HP-GL, 1016 per inch)** | manual p.72; Expert 24 LX manual spec table |
| Buffer | 1 MB (500 KB with AAS) on the 24; the 52 lists 3 MB (label inferred from a garbled table) | manual p.72 |
| Command set | HP-GL, HP-GL/2 | manual p.72, §2.5.3, §3.4 |
| Interfaces | USB 2.0 full speed and RS-232; **serial is disabled automatically while USB is in use** | manual §2.5 |
| Origin | "bottom right" of the material, configurable (ORIGIN SET key) | manual (GreatCut-S/SCAL appendix, §3.1.2) |
| Units in GCC material table | Force (g), Speed (cm/s), Offset (mm) | manual §3.6, p.56 |

Reference values from the GCC material table (p.56): vehicle/window vinyl
uses a red blade, 85 g, 60 cm/s and 0.25 mm offset. Small text uses a black
blade, 90–150 g, 9 cm/s and 0.175 mm offset. Stencil uses 180 g, 15 cm/s and
0.25–0.5 mm offset. For Kapton stickers, start near the vinyl values and run
a test cut.

### USB connection mode
* The Expert-series USB has two modes, switched on the panel (Pause+Origin Set,
  then Data Clear or Cut Test): **"Common USB mode"** and **"GCC USB mode"**
  (Expert 24 LX manual §2.5.2; the Expert II manual has the same "USB-port
  switching mode" in §3.2.3). Install the driver in *Common USB mode*.
* The GCC driver installs the cutter as a **Windows printer**. To remove it,
  the manual says to open *Devices and Printers*, right-click the printer and
  choose *Remove device*, then delete the driver package under *Print server
  properties > Driver*. The manual's "Expert II Print Driver Setting"
  chapter also covers this driver. So the cutter can be reached by sending
  **RAW HP-GL through the Windows spooler** (`PrinterTransport`). It does not
  show up as a virtual COM port. **[verify]** the printer name once the
  driver is installed. At research time no GCC printer was installed on this
  PC (printers seen: OneNote, Print to PDF, HP x3, Epilog, network HP).
* GreatCut talks to "USB / Firewire devices" directly (GreatCut help:
  *Device Setting – Interface Setup*). It can also output to a COM port,
  TCP/IP, or a Windows spooler printer.

### RS-232 defaults
* **9600 bps, 8 data bits, no parity, 1 stop bit.** Source: Expert II
  manual, Warning 5 "Communication error": "for example – 9600bps, no parity,
  8 bits, 1 stop bit". The Expert 24 LX manual says the same.
* Handshake: the manual says to match the software but gives no value.
  GreatCut help (Quickstart; Tips 2/4/5 "Buffer overflow") says "Default
  settings are: 9600 or 19200, 8, None, 1, **Flow control: Hardware**" and
  that buffer overflows are fixed by setting flow control to hardware. So the
  default is **RTS/CTS**. **[verify]**
* This PC's only COM port, **COM3, is "Intel(R) Active Management Technology –
  SOL"**, not the cutter. The user's InkCut profile points at it, so InkCut
  probably never reached the cutter over serial.

### HP-GL dialect / speed & force commands
* The manual only says "HP-GL / HP-GL/2". It gives no command reference.
  GCC's driver, GreatCut and Sure Cuts A Lot ("Use Software Speed and
  Pressure", manual appendix A-8) all set speed and pressure from software, so
  the firmware accepts host speed/force commands. HP-GL/2 cutter convention is
  `VS<cm/s>;` and `FS<grams>;`. Both units match GCC's own UI (cm/s, g).
  InkCut2 sends `VS`/`FS` for the GCC presets. **[verify]** with a test cut:
  send FS80 and then FS200 and compare blade depth. If the cutter ignores
  them, set them on VLCD instead (the options can be turned off).
* No GCC-specific escape commands were found. GreatCut's `.ECD` drivers are
  encrypted (`Cutter\*.ECD`). Their driver-editor string table
  (`Lang\eng\Cuttereng.tra`) only shows generic templates such as
  `PU %x%,%y%;` and `PD %x%,%y%;`, plus `%PlotInit%`, `%Speed%` and
  `%Pressure%` instructions. There is no Expert II-specific file. The closest
  is `GCCExpertPro.ECD`.
* The firmware applies blade offset (drag-knife) compensation itself (VLCD
  "Blade Offset"). **InkCut2's GCC presets therefore set software blade
  offset to 0**, so the offset is not applied twice. If you prefer software
  compensation, set the cutter's offset to 0.000 and use the job's blade
  offset.

### Axis orientation (choice for InkCut2)
For roll cutters the HP-GL **X axis is the media-feed direction and Y is the
carriage direction**, with the origin at the front-right. InkCut users of
GCC cutters, including this user, enable `swap_xy` to get that. Standard
HP-GL that reads upright in an X-right/Y-up frame cuts non-mirrored (Inkscape's
`hpgl_encoder.py` flips SVG y the same way). InkCut2's material frame (x
along feed, y across, y-up) already **is** that frame. The GCC presets
therefore use `swap_axes=False, invert_x=False, invert_y=False`, and
`to_device(x, y) = (round(40·x), round(40·y))`. **[verify]** that a test
"F"/arrow shape is not mirrored. If it is, toggle `invert_y`.

Sources: GCC Expert II User Manual V.10 (2018)
<https://www.gccworld.com/data/file/upload/UserManual/Cutter/Expert-II-User-Manual.pdf>
(copy on ManualsLib <https://www.manualslib.com/manual/3533207/Gcc-Technologies-Expert-Ii-Series.html>,
pages 30, 44–47, 49, 52–53, 56, 72). Expert 24 LX User Manual
<https://www.synergy17.com/downloads/GCC/manuals/Expert24LXusermanual.pdf>.
GreatCut 4 help (`Lang\eng\help.chm`, decompiled: Quickstart, "Buffer Overflow
(Serial Port)", "Cutter does not respond", "Plotter via USB is not working",
"Device Setting – Interface Setup"). Retailer listings (heatpressnation.com,
inkjetbiz.com) give similar numbers. One third-party page lists 400 mm/s and
250 g, which are actually the older Expert 24 LX figures.

## 2. GreatCut 4 install (`C:\Program Files (x86)\GCC\GreatCut 4`)
* No output device is configured. `plotman.ini` only has AppName, and
  `%APPDATA%\EUROSYSTEMS\GreatCut 4\*` holds backups of cocut/filter/plotman.ini
  with no device sections. The registry under `HKCU\Software\EuroSYSTEMS`
  holds only toolbar state.
* GCC drivers are `Cutter\*.ECD` and `Cutter\defaultdrv\*.ECD` (Expert Pro,
  Jaguar JG*, Puma*, RX, iCraft, Bengal, ...). They are encrypted/compressed,
  so they could not be read.
* GreatCut job features worth copying in InkCut2: weeding lines (horizontal/vertical) and a
  weed frame; multi copy; "WYSIWYG" vs "Origin Point" output; overcut; tool
  offset per material (`%IToolOffset%`); speed/pressure per material
  (`%IMatSpeed%`, `%IMatPressure%`); a reduced-speed feed init (`(FF:VS1)`);
  segment sorting; output preview.

## 3. InkCut 2.1.7 "Generic Driver" (source: `site-packages\inkcut\device`)

### Units
InkCut's internal unit is the **px at 90 dpi**: `_uuconv = {'in': 90, 'mm':
3.5433070866, 'cm': 35.433070866, ...}` (`core/svg.py`). Every length in the
device config (step_size, area size/padding, overcut, blade offset) is in px.

### The Generic Driver
`drivers/manifest.enaml` declares `DeviceDriver(manufacturer='Inkcut',
model='Generic Driver', custom=True)`. It sets no width/length, so
`width=''` becomes 0 px and length defaults to 900000 px ("unlimited"). It
has no protocol or connection filter, so **all** protocols and transports are
offered. Other drivers in the same file are only presets (width, protocols,
`default_config` such as `swap_xy`). One of them is "Gcc Bob cat 60"
(hpgl/dmpl, serial).

### DeviceConfig fields (`device/plugin.py`) and their effect
| field | meaning |
|---|---|
| `speed` (float, default 4) + `speed_units` ('in/s' default, or 'cm/s') | **sent raw as an integer**: `VS%i` truncates the number in its own units (1.1 in/s gives `VS1`). It is also used to pace streaming when not interpolating. |
| `speed_enabled` | send `set_velocity` after `connection_made` |
| `force` (g, default 40), `force_enabled` | send `FS%i; ` (note: InkCut adds a trailing space) |
| `step_size` (px; default 1 mm = 3.543 px) / `step_time` (ms) | with `interpolate=True` every segment is split into step_size pieces, and InkCut sleeps `step_time` ms per piece (flow control by timing). The default step_time is 1000·step_size/speed. |
| `interpolate`, `spooled`, `custom_rate` | pacing. A transport with `always_spools` (printer, file) disables delays. |
| `quality_factor` | prescale before Qt converts curves to polygons (curve flattening fineness) |
| `swap_xy` | rotates the model 90° (`QTransform.rotate(90)`). This is a proper rotation, not a mirror. Device X then runs along the job length (feed). |
| `mirror_x`, `mirror_y` | negate the scale on that axis (applied before the swap) |
| `scale [sx, sy]` | final output scale (calibration) |
| `rotation` (0/90/-90) | only used by `Device.transform()` for the preview |
| `commands_connect / _before / _after / _disconnect` | raw strings. connect is sent right after open (after the protocol's `IN;`), before is sent before the job, after is sent after `protocol.finish()`, disconnect is sent before close. |
| `area.size [w, h]`, `area.padding [left, top, right, bottom]` (px) | media area. x runs across the width and y along the length. The job is offset by padding_left / padding_bottom. |
| filters: `overcut` (px), `blade-offset` (offset px, cutoff angle deg), `min-line`, `repeat` (steps) | applied to the path before output |

### Protocols (exact output)
| id | init | move up / down | speed | force | finish | scale |
|---|---|---|---|---|---|---|
| `hpgl` | `IN;` | `PU%i,%i;` / `PD%i,%i;` (one point per command) | `VS%i;` | `FS%i; ` | `IN;` | **1021/90 per px = 40.197/mm** (0.5 % larger than true HP-GL, 40/mm) |
| `hpgl` option `pad` | appends `\n` to every write | | | | | |
| `dmpl` mode 1 | `;:HAEC1` | ` U%i,%i ` / ` D%i,%i ` | `V%i ` | `BP%i ` | none | 1021/90 per px |
| `dmpl` mode 2 | ` ;:ECN A L0 ` | same | | | | |
| `dmpl` mode 3/4 | ` ;:H A L0 ` | same | | | | |
| `dmpl` mode 6 | `IN;PA;` | `PU%i,%i;` / `PD%i,%i;` | | | | |
| `gpgl` | `H` | `M%i,%i` / `D%i,%i` (**no terminators**) | `!%i` | `FX%i,1` | none | raw px (1/90 in), so drivers use `scale` (e.g. Silhouette 5.64 gives 20/mm) |
| `camm-gl1` | `IN;` | `M%i,%i;` / `D%i,%i;` | `VS%i;` | `FS%i;` | none | raw px |
| `gcode` | `G28`/`G98`/`G90` (if `use_builtin`) | `G00/G01 X.. Y..` (+Z for lift_mode Z, custom lift/lower blocks) | – | – | `G28`,`G98` | raw px |
| `debug` ("Virtual") | logs only | | | | | |

After the last path, InkCut moves to the model's end point, which is (0, 0)
unless feed_to_end is set. With PU, that gives an effective "return home".
Absolute coordinates are clamped to ≥ 0.

### Transports
* **serial** (pyserial via Twisted `SerialPort`): port, baudrate, bytesize,
  parity name ('None', 'Even', ...), stopbits, xonxoff/rtscts flags (dsrdtr is
  patched in afterwards). It does no chunking; each command is written as it
  is produced, paced by step_time/speed.
* **qtserial** (QSerialPort): ints from Qt enums. Parity 0 = None, 2 = Even,
  3 = Odd, 4 = Space, 5 = Mark. Stopbits 1 = 1, 2 = 2, 3 = 1.5. Flowcontrol
  0 = none, 1 = RTS/CTS, 2 = XON/XOFF.
* **printer** (win32print, pywin32): `OpenPrinter`,
  `StartDocPrinter(p, 1, ("Inkcut job", None, "RAW"))`, `StartPagePrinter`,
  `WritePrinter` per command, then `EndPagePrinter`/`EndDocPrinter`/`ClosePrinter`
  (CUPS/lpr on Linux). It always spools, so no delays. InkCut2 does the
  same with ctypes (`winspool.drv`) and does not need pywin32.
* **disk**: writes `directory/format`, where the format is
  `inkcut-{time}.{protocol}`.
* **parallel/raw**: opens a device path (`/dev/usb/lp0`). Not supported on
  Windows.

### The user's InkCut file (`~/.config/inkcut/inkcut.device.json`)
The file is jsonpickle. Objects are encoded as
`{"py/reduce": ["py/newobj", {"py/tuple": [{"py/type": T}]}, state]}`, lists as
`{"py/object": "atom.catom.atomlist", "py/seq": [...]}`, and back-references as
`{"py/id": n}`. The top-level `"device": {"py/id": 2}` is the current device.
It contains two devices plus job history.

**"GCC Expert II"** (Generic Driver):
| InkCut | value | InkCut2 import |
|---|---|---|
| area.size | [1800, 900000] px | width **508 mm** (1800/3.5433 = 20 in). Length is "unlimited", so 25000 mm (GCC max) is used. |
| area.padding | [10, 10, 10, 10] px | 2.822 mm on all sides |
| transport | qtserial COM3, 9600, 8, parity 0, stopbits 1, flowcontrol 0 | serial COM3 9600 8N1, flow **none** (copied faithfully; a note recommends RTS/CTS) |
| protocol | hpgl, pad=false | `hpgl`, 40 steps/mm (InkCut's 40.197 quirk corrected) |
| swap_xy | true | swap_axes=False (feed = device X, identical to the GCC preset) |
| speed | 1.1 in/s, speed_enabled | default_speed **2.79 cm/s**, use_speed_cmd=True. InkCut actually sent `VS1`. **[verify]** what you want. 30 cm/s is GCC's default. |
| force | 40 g, force_enabled=false | default_force 40, use_force_cmd=False |
| interpolate, step_size, step_time | true, 3.5433 px (1 mm), 127 ms | InkCut throttled to about 7.9 mm/s. Not reproduced: InkCut2 relies on handshake and chunking, and the GCC has a 1 MB buffer. Recorded in notes. |
| quality_factor | 10 | (curve flattening is done by the Inkscape side) |
| filters | none | overcut 0, blade offset 0 |

The second device, "New device Inkcut Generic Driver", uses GPGL over pyserial
COM3 with a 0 width. It is an unused default.

## 4. Inkscape plotter extension (`share\inkscape\extensions`)
* `plotter.inx` serial defaults: 9600 baud, 8 bits, 1 stop, no parity,
  **XON/XOFF** flow by default. The flow choices are xonxoff, rtscts, dsrdtr+rtscts
  and none. Resolution is 1016 dpi (40/mm), pen 1, force 0 (omitted) and speed 0
  (omitted).
* `hpgl_encoder.py` flips SVG y (`mirrorY = -1`) so output reads upright. It
  joins consecutive PD points into one `PD x,y,x,y` command and emits
  `;PU;SP%d`, `;VS%d` and `;FS%d` when they change. `plotter.py` wraps the
  output as `IN` + hpgl + `;PU0,0;SP0;IN; `. DMPL is `;:HAL0[V%d]EC1 ... ,P0,U0,0,Z `
  and KNK is `ZG ... ;SP0;PU0,0;@ `.
* `inkex/ports.py` serial: `timeout=0.1`. Flow maps to xonxoff, rtscts, or
  dsrdtr+rtscts.

## 5. Decisions implemented in InkCut2
* **HP-GL output** for GCC:
  `IN;VS<cm/s>;FS<g>;PUx,y;PDx1,y1,x2,y2,...;...;PU0,0;IN;`. Coordinates are
  integers. Consecutive duplicates after rounding are dropped, and paths that
  collapse to a single point are removed. Each PD carries at most 64 points.
  Negative values are clamped to 0. Speed and force are clamped to the profile
  ranges and rounded.
* `protocol_options`: `use_speed_cmd`, `use_force_cmd`, `return_home`,
  `feed_after_mm` (park past the job instead of returning home), `pen` (SP),
  `newline` (InkCut "pad"), `max_points`, `clamp_negative`, and
  `speed_cmd`/`force_cmd` templates. DMPL adds `mode`, GP-GL adds
  `terminator` (default ETX `\x03`; InkCut sends none), and G-code adds
  `use_builtin`, `lift_mode`, `precision`, `lower_z`, `upper_z`,
  `lift_gcode`, `lower_gcode` and `scale`.
* Transports chunk at command boundaries (`;`, LF, ETX), so a cancel never leaves
  half a command. Serial keeps the OS output queue at or below one chunk
  (responsive cancel), detects a stalled handshake after `timeout_s` and raises
  a readable `TransportTimeout`, and resets the output buffer on cancel and sends
  `protocols.cancel_sequence()` (`;PU;`). Printer output is RAW through
  winspool via ctypes. The document starts on the first write, so a connection
  test spools nothing. Cancel calls `AbortPrinter`.
