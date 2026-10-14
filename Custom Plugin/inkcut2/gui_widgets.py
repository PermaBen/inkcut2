"""Small GTK 3 form helpers used by the InkCut2 window (gui.py)."""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GObject, Gtk  # noqa: E402

CSS = b"""
.vc-header { font-weight: bold; }
.vc-dim { opacity: 0.70; }
.vc-small { font-size: 90%; }
.vc-warning { color: #c62828; }
.vc-ok { color: #2e7d32; }
.vc-stat { font-weight: bold; }
.vc-statusbar { padding: 4px 8px; }
.vc-toolbar { padding: 4px 6px; }
"""


def install_css():
    try:
        from gi.repository import Gdk
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        screen = Gdk.Screen.get_default()
        if screen is not None:
            Gtk.StyleContext.add_provider_for_screen(screen, prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    except Exception:  # styling is cosmetic only
        pass


def add_class(widget, *names):
    ctx = widget.get_style_context()
    for n in names:
        ctx.add_class(n)
    return widget


def _no_wheel_unless_focused(widget, _event):
    """Let the scroll wheel scroll the settings panel instead of changing an unfocused value."""
    if not widget.has_focus():
        GObject.signal_stop_emission_by_name(widget, "scroll-event")
        return False
    return False


def spin(lo, hi, step, digits=0, value=None, page=None, width=7, tooltip=None):
    adj = Gtk.Adjustment(lower=lo, upper=hi, step_increment=step,
                         page_increment=page if page is not None else step * 10, page_size=0)
    sb = Gtk.SpinButton(adjustment=adj, digits=digits)
    sb.set_numeric(True)
    sb.set_width_chars(width)
    sb.set_update_policy(Gtk.SpinButtonUpdatePolicy.IF_VALID)
    sb.set_hexpand(True)
    sb.set_value(lo if value is None else value)
    sb.connect("scroll-event", _no_wheel_unless_focused)
    if tooltip:
        sb.set_tooltip_text(tooltip)
    return sb


def combo(items, active=None, tooltip=None):
    """items: list of (id, label). ids are converted to str."""
    c = Gtk.ComboBoxText()
    for i, label in items:
        c.append(str(i), label)
    if active is not None:
        c.set_active_id(str(active))
    c.set_hexpand(True)
    c.connect("scroll-event", _no_wheel_unless_focused)
    if tooltip:
        c.set_tooltip_text(tooltip)
    return c


def label(text, xalign=0.0, wrap=False, css=None, markup=False):
    lb = Gtk.Label()
    if markup:
        lb.set_markup(text)
    else:
        lb.set_text(text)
    lb.set_xalign(xalign)
    if wrap:
        lb.set_line_wrap(True)
        lb.set_max_width_chars(40)
    if css:
        add_class(lb, *([css] if isinstance(css, str) else css))
    return lb


def button(text, cb=None, tooltip=None, icon=None, css=None):
    b = Gtk.Button(label=text)
    if icon:
        try:
            img = Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
            b.set_image(img)
            b.set_always_show_image(True)
        except Exception:
            pass
    if cb:
        b.connect("clicked", cb)
    if tooltip:
        b.set_tooltip_text(tooltip)
    if css:
        add_class(b, css)
    return b


def hbox(*children, spacing=6, linked=False, expand_first=False):
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0 if linked else spacing)
    if linked:
        add_class(box, "linked")
    for i, ch in enumerate(children):
        box.pack_start(ch, expand_first and i == 0, expand_first and i == 0, 0)
    return box


class Form(Gtk.Grid):
    """Three-column form: label | widget | unit."""

    def __init__(self, margin=14):
        super().__init__()
        self.set_column_spacing(10)
        self.set_row_spacing(7)
        self.set_margin_start(margin)
        self.set_margin_end(margin)
        self.set_margin_top(max(0, margin - 2))
        self.set_margin_bottom(margin)
        self.row = 0

    def header(self, text, tooltip=None):
        lb = label(text, css="vc-header")
        lb.set_margin_top(4 if self.row == 0 else 14)
        if tooltip:
            lb.set_tooltip_text(tooltip)
        self.attach(lb, 0, self.row, 3, 1)
        self.row += 1
        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        self.attach(sep, 0, self.row, 3, 1)
        self.row += 1
        return lb

    def add(self, text, widget, unit=None, tooltip=None):
        lb = label(text)
        lb.set_margin_start(4)
        self.attach(lb, 0, self.row, 1, 1)
        widget.set_hexpand(True)
        self.attach(widget, 1, self.row, 1 if unit is not None else 2, 1)
        if unit is not None:
            self.attach(label(unit, css="vc-dim"), 2, self.row, 1, 1)
        if tooltip:
            lb.set_tooltip_text(tooltip)
            if not widget.get_tooltip_text():
                widget.set_tooltip_text(tooltip)
        self.row += 1
        return widget

    def wide(self, widget, indent=True):
        if indent:
            widget.set_margin_start(4)
        self.attach(widget, 0, self.row, 3, 1)
        self.row += 1
        return widget

    def note(self, text):
        lb = label(text, wrap=True, css=["vc-dim", "vc-small"])
        return self.wide(lb)


class OptionalSpin(Gtk.Box):
    """A spin button plus a 'device default' checkbox. get() -> None when default."""

    def __init__(self, lo, hi, step, digits, on_change, tooltip=None, default_text="Device default"):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._guard = False
        self._on_change = on_change
        self.spin = spin(lo, hi, step, digits, tooltip=tooltip)
        self.check = Gtk.CheckButton(label=default_text)
        self.check.set_tooltip_text("Use the value configured in the device profile")
        self.pack_start(self.spin, True, True, 0)
        self.pack_start(self.check, False, False, 0)
        self.spin.connect("value-changed", self._changed)
        self.check.connect("toggled", self._changed)

    def _changed(self, *_):
        self.spin.set_sensitive(not self.check.get_active())
        if not self._guard and self._on_change:
            self._on_change(self.get())

    def get(self):
        return None if self.check.get_active() else float(self.spin.get_value())

    def set(self, value, default):
        self._guard = True
        try:
            if value is None:
                self.check.set_active(True)
                self.spin.set_value(float(default))
            else:
                self.check.set_active(False)
                self.spin.set_value(float(value))
            self.spin.set_sensitive(value is not None)
        finally:
            self._guard = False

    def set_range(self, lo, hi):
        self._guard = True
        try:
            self.spin.set_range(lo, hi)
        finally:
            self._guard = False
