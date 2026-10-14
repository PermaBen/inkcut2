#!/usr/bin/env python3
"""InkCut2 Inkscape effect: send the selection (or whole drawing) to the cutting window.

Converts the selection to polylines in mm (see inkcut2/extract.py), writes a design
JSON to %TEMP%\\inkcut2\\ and launches ``python -m inkcut2.gui <json>`` detached.
The Inkscape document is never modified.

Environment:
    INKCUT2_NO_LAUNCH=1   don't start the GUI, print the JSON path to stderr (tests)
"""

import datetime
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import inkex  # noqa: E402

from inkcut2 import extract  # noqa: E402

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
KEEP_DESIGNS_DAYS = 7


def work_dir():
    path = os.path.join(tempfile.gettempdir(), "inkcut2")
    os.makedirs(path, exist_ok=True)
    return path


def cleanup_old(path):
    cutoff = time.time() - KEEP_DESIGNS_DAYS * 86400
    try:
        for name in os.listdir(path):
            if name.startswith("design-") and name.endswith(".json"):
                full = os.path.join(path, name)
                if os.path.getmtime(full) < cutoff:
                    os.remove(full)
    except OSError:
        pass


def gui_command(json_path):
    """(argv, env, cwd) used to start the cutting window."""
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if sys.platform == "win32" and os.path.isfile(pyw):
        exe = pyw
    env = dict(os.environ)
    # The child must import our package first; keep inkex importable too.
    inkex_dir = os.path.dirname(os.path.dirname(os.path.abspath(inkex.__file__)))
    paths = [HERE, inkex_dir] + [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    seen, uniq = set(), []
    for p in paths:
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    env["PYTHONPATH"] = os.pathsep.join(uniq)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONSTARTUP", None)
    env["PYTHONIOENCODING"] = "utf-8"
    return [exe, "-m", "inkcut2.gui", json_path], env, HERE


def launch_gui(json_path):
    argv, env, cwd = gui_command(json_path)
    log_path = os.path.join(os.path.dirname(json_path), "gui.log")
    try:
        if os.path.getsize(log_path) > 1_000_000:
            os.remove(log_path)
    except OSError:
        pass
    log = open(log_path, "ab")
    try:
        log.write(("\n--- %s %s\n" % (datetime.datetime.now().isoformat(" ", "seconds"), argv)).encode())
        log.flush()
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log,
            close_fds=True,
            **kwargs
        )
        proc.returncode = 0  # don't warn about a still-running child on exit
    finally:
        log.close()
    return log_path


class InkCut2(inkex.EffectExtension):
    def add_arguments(self, pars):
        pars.add_argument("--scope", default="selection", choices=["selection", "document"])
        pars.add_argument("--tolerance", type=float, default=extract.DEFAULT_TOLERANCE_MM)

    def has_changed(self, ret):
        # Never write a document back: Inkscape then keeps the drawing untouched
        # (no undo step), even if inkex normalised an attribute while reading.
        return False

    def design_name(self):
        name = self.svg.get("sodipodi:docname") or self.svg.get(
            "{http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd}docname"
        )
        if not name:
            doc = os.environ.get("DOCUMENT_PATH") or ""
            name = os.path.basename(doc) if doc else ""
        return name or "drawing.svg"

    def elements(self):
        if self.options.scope == "document":
            return extract.document_elements(self.svg)
        sel = self.svg.selection
        try:
            return list(sel.rendering_order().values())
        except Exception:
            return list(sel.values())

    def effect(self):
        elements = self.elements()
        if not elements:
            if self.options.scope == "document":
                inkex.errormsg("InkCut2: the drawing is empty - nothing to cut.")
            else:
                inkex.errormsg(
                    "InkCut2: nothing is selected.\n\n"
                    "Select the objects you want to cut first (Edit > Select All, Ctrl+A, "
                    "selects everything on the current layer), or use "
                    "Extensions > InkCut2 > Cut Whole Document."
                )
            return False

        converted, text_ids, notes = None, None, []
        texts = extract.find_text(self.svg, elements)
        if texts:
            try:
                converted, text_ids = extract.convert_text_to_paths(self.svg, texts, workdir=work_dir())
            except Exception as err:  # keep going without the text
                notes.append("Text could not be converted to paths automatically (%s)." % err)

        result = extract.extract(
            self.svg, elements, tolerance_mm=self.options.tolerance, converted=converted, text_ids=text_ids
        )
        warnings = notes + result.warnings
        if not result.polylines:
            inkex.errormsg(
                "InkCut2: the selection contains nothing that can be cut "
                "(no visible paths, shapes or text).\n" + "\n".join(warnings)
            )
            return False

        folder = work_dir()
        cleanup_old(folder)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        json_path = os.path.join(folder, "design-%s.json" % stamp)
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(result.to_design(self.design_name()), fh, separators=(",", ":"))

        if warnings:
            inkex.errormsg("InkCut2 warnings:\n- " + "\n- ".join(warnings))

        if os.environ.get("INKCUT2_NO_LAUNCH") == "1":
            sys.stderr.write(json_path + "\n")
            sys.stderr.flush()
        else:
            try:
                launch_gui(json_path)
            except Exception as err:
                inkex.errormsg("InkCut2: could not start the cutting window: %s\nDesign saved to %s" % (err, json_path))
        return False


if __name__ == "__main__":
    InkCut2().run()
