"""Application entry point."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID  # noqa: E402
from .window import MainWindow  # noqa: E402


class PlymouthConfigurator(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.add_main_option("screenshot", 0, GLib.OptionFlags.NONE, GLib.OptionArg.FILENAME,
                             "Render the window to PNG after a short delay and exit", "PATH")
        self.connect("handle-local-options", self._on_local_options)
        self._screenshot: str | None = None

    def _on_local_options(self, _app, options: GLib.VariantDict) -> int:
        path = options.lookup_value("screenshot", GLib.VariantType("ay"))
        if path is not None:
            self._screenshot = bytes(path.get_bytestring()).decode()
        return -1

    def do_startup(self):
        Adw.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_string((Path(__file__).with_name("style.css")).read_text())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self):
        win = self.props.active_window or MainWindow(self)
        win.present()
        if self._screenshot:
            GLib.timeout_add(3500, self._take_screenshot, win, self._screenshot)

    def _take_screenshot(self, win, path):
        paintable = Gtk.WidgetPaintable.new(win)
        w, h = win.get_width(), win.get_height()
        snapshot = Gtk.Snapshot()
        paintable.snapshot(snapshot, w, h)
        node = snapshot.to_node()
        if node is not None:
            texture = win.get_native().get_renderer().render_texture(node, None)
            texture.save_to_png(path)
            print(f"screenshot saved to {path}")
        self.quit()
        return False


def main(argv=None) -> int:
    app = PlymouthConfigurator()
    return app.run(sys.argv if argv is None else argv)
