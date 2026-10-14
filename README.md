# InkCut2 cut from Inkscape on standard Vinyl Cutters

InkCut2 is an Inkscape 1.4 extension for Windows. Select artwork in Inkscape, pick
**Extensions ▸ InkCut2 ▸ Cut Selection…**, and a cutting window opens with a
preview and all the job settings. From there you send the job to the cutter or save it
as a `.plt` file. Your drawing is never changed.

It was built for cutting Kapton stickers on a **GCC Expert II**. It also works with other
HPGL cutters and with the protocols InkCut's "Generic Driver" supports.

## Install

Requirements: Windows and Inkscape 1.4 (tested with 1.4.4). You don't need to install
anything else: InkCut2 uses the Python, GTK and pyserial that come with Inkscape.

1. Download `InkCut2-<version>.zip` from the **Releases** page (on the right of the
   GitHub page) and extract it.
2. Close Inkscape.
3. In the extracted folder, double-click **`install.cmd`**.
   It copies `inkcut2.inx`, `inkcut2_all.inx`, `inkcut2.py` and the `inkcut2\` folder
   into `%APPDATA%\inkscape\extensions`. Your other extensions are left alone.
4. Start Inkscape. The **Extensions ▸ InkCut2** menu now has two entries.

If Windows SmartScreen warns about `install.cmd`, choose *More info ▸ Run anyway*, or
install by hand: copy those four items into the folder shown in Inkscape under
**Edit ▸ Preferences ▸ System ▸ User extensions**.

To update, install the new release the same way. To uninstall, delete `inkcut2.inx`,
`inkcut2_all.inx`, `inkcut2.py` and the `inkcut2` folder from
`%APPDATA%\inkscape\extensions`.

Linux and macOS are untested. Copying the same four items into the Inkscape user
extensions folder may work if Inkscape's Python has `gi` (GTK 3) and `pyserial`. The
Windows-printer (USB) connection is Windows-only.

## Usage

1. Draw or import your sticker artwork. InkCut2 reads the document's real units
   (mm, inches or px), so 50 mm in Inkscape comes out as 50 mm on the cutter.
2. Select what you want to cut. Groups, layers, clones, shapes and text are all fine.
3. Choose **Extensions ▸ InkCut2 ▸ Cut Selection…**. To send every visible object
   without selecting anything, use **Cut Whole Document…**.
4. The cutting window opens and Inkscape is free again. In the window:
   - check the preview, which shows the cut paths, pen-up travel and the material area,
   - set the transform, copies and cut settings,
   - choose your cutter and connection,
   - press **Cut**. A test cut is a good idea before a new roll or blade.

What gets cut:

- Every path, rectangle, circle, ellipse, line, polyline and polygon is cut along its
  outline, with all group, layer and clone transforms applied. Curves are approximated
  within 0.02 mm.
- Text is converted to outlines for you, using Inkscape in the background. This adds a
  few seconds.
- Hidden objects are skipped (`display:none`, hidden layers).
- Bitmap images are skipped and you get a warning. Run **Path ▸ Trace Bitmap** first.
- Stroke width is ignored, because the blade follows the centre line of the path. To cut
  around a thick line, run **Path ▸ Stroke to Path** first.
- Clips and masks are ignored, so the whole shape is cut. Run **Path ▸ Intersection** or
  release the clip first.

## Features (cutting window)

- **Preview**: material outline, cut order and travel moves, size and estimated time,
  plus warnings when the design doesn't fit.
- **Transform**: scale, mirror horizontally or vertically (for heat-transfer vinyl), and
  rotate (0/90/180/270 or any angle).
- **Array / copies**: rows × columns with spacing, or auto-fill the material width.
- **Cutting**: speed, force, number of passes (per path or per whole job), overcut on
  closed shapes, drag-knife blade-offset compensation, and cut order (inside-out by
  default so small parts don't shift). You can also add a weeding border.
- **Devices**: presets for the GCC Expert II 24 and 52 and generic HPGL, and you can
  edit your own profiles. You can import the devices you already set up in **InkCut**.
- **Connection**: serial port (port, baud, data bits, parity, stop bits, flow control),
  Windows printer (raw), network socket, or save to file (`.plt`).
- The settings from your last job are remembered.

## Connecting the GCC Expert II

The Expert II has two connections, and InkCut2 works with either:

- **USB**: when you install GCC's USB driver, the cutter appears as a **Windows
  printer** (for example "GCC Expert II"). In InkCut2, choose the *Printer* connection
  and pick that printer. Data is sent raw (HPGL), with no printer driver formatting.
  Don't open the printer's "Printing Preferences" to change anything.
- **Serial (RS-232)**: connect with a serial cable, or with a USB-to-serial adapter,
  which shows up as `COMx` in Device Manager ▸ Ports. In InkCut2, choose *Serial*, pick
  the COM port and set the baud rate, parity and flow control to **the same values as
  the cutter's own setup menu**. The usual setting is 9600 baud, 8 data bits, no parity,
  1 stop bit, hardware (RTS/CTS) flow control. If the cutter misses commands or stops
  partway through a long job, check flow control first.

Before the first real job, load material, set the origin on the cutter, and run a test
cut. Use it to tune force and blade offset for your Kapton and liner.

## Troubleshooting

| Symptom | What to check |
|---|---|
| Menu entry missing | Restart Inkscape after installing. Check that `%APPDATA%\inkscape\extensions\inkcut2.inx` exists. |
| "Nothing is selected" | Select objects first, or use *Cut Whole Document…*. |
| Window doesn't open | Look in `%TEMP%\inkcut2\gui.log`. It records every launch and any Python error. |
| Text missing or warned about | Automatic conversion failed. Select the text and run **Path ▸ Object to Path**, then cut again. |
| Wrong size | Check **File ▸ Document Properties**: the display units and the scale. InkCut2 honours both. Mirror and scale settings in the cutting window apply on top. |
| Cutter does nothing | Check the connection type, port or printer name, and the serial settings against the cutter's menu. For a quick check, save to a `.plt` file and look at it. |
| Jagged curves | Lower the tolerance in `inkcut2.inx` (`tolerance` parameter, in mm), then reinstall. |

Files InkCut2 uses:

- `%TEMP%\inkcut2\design-*.json`: the exported artwork, one file per run. Files older
  than 7 days are deleted automatically.
- `%TEMP%\inkcut2\gui.log`: the cutting window's log.
- `%APPDATA%\inkcut2\`: saved job settings and device profiles.

To report a problem, include `gui.log` and the matching `design-*.json`.

## Development

- Run the tests with Inkscape's Python, from the project folder:
  `$env:PYTHONPATH="C:\Program Files\Inkscape\share\inkscape\extensions"; & "C:\Program Files\Inkscape\bin\python.exe" -m unittest discover -s tests -t .`
- Install your working copy into Inkscape: `powershell -ExecutionPolicy Bypass -File install.ps1`
- Make a release:
  1. Bump `__version__` in `inkcut2/__init__.py`.
  2. Run `powershell -ExecutionPolicy Bypass -File build_release.ps1`, which writes `dist\InkCut2-<version>.zip`.
  3. On GitHub, open **Releases ▸ Draft a new release**, create the tag `v<version>`, and attach the zip.

Design notes: `docs/ARCHITECTURE.md` (module layout and coordinate systems) and
`docs/research.md` (GCC Expert II and InkCut protocol details).
