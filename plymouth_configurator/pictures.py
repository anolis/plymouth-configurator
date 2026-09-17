"""Choose, preview and apply a personal picture as a Plymouth boot theme."""

from pathlib import Path
import shutil

from gi.repository import Adw, Gio, GLib, Gtk

from .picture_theme import create_picture_theme, set_picture_fit
from .system import CACHE_DIR, run_in_thread


class PictureCoordinator:
    def __init__(self, window):
        self.window = window

    def choose(self):
        self.window.plymouth_setup.ensure(self._choose_file)

    def _choose_file(self):
        dialog = Gtk.FileDialog(title="Choose a boot screen picture")
        image_filter = Gtk.FileFilter()
        image_filter.set_name("Pictures (PNG, JPEG, WebP, BMP, TIFF)")
        for mime in ("image/png", "image/jpeg", "image/webp", "image/bmp", "image/tiff"):
            image_filter.add_mime_type(mime)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(image_filter)
        dialog.set_filters(filters)
        dialog.open(self.window, None, self._chosen)

    def _chosen(self, dialog, result):
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return
        path = file.get_path()
        if not path:
            self.window.toast("Choose a local image file")
            return
        self.window.set_busy(True)

        def done(theme, error):
            self.window.set_busy(False)
            if error:
                self.window.show_output("Could not read picture", str(error), False)
                return
            self._preview(theme)

        run_in_thread(lambda: create_picture_theme(Path(path), CACHE_DIR / "pictures"), done)

    def _preview(self, theme):
        rebuild = bool(self.window.settings.get("rebuild_initrd", True))
        body = "Install this picture as a new theme and use it for your boot screen. "
        body += ("The initramfs will be rebuilt so it is available at the next boot."
                 if rebuild else "Rebuilding is disabled; rebuild the initramfs later to see it at boot.")
        dialog = Adw.AlertDialog(heading="Use this picture as your boot screen?", body=body)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        picture = Gtk.Picture.new_for_filename(str(theme / "background.png"))
        picture.set_content_fit(Gtk.ContentFit.COVER)
        picture.set_can_shrink(True)
        frame = Gtk.AspectFrame(ratio=16 / 9, obey_child=False, child=picture)
        frame.set_size_request(400, 225)
        frame.add_css_class("picture-preview")
        box.append(frame)
        fit = Gtk.DropDown.new_from_strings(["Fill screen (crop edges)", "Fit picture (black borders)"])
        fit.connect("notify::selected", lambda *_: picture.set_content_fit(
            Gtk.ContentFit.COVER if fit.get_selected() == 0 else Gtk.ContentFit.CONTAIN))
        box.append(fit)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("apply", "Use as boot screen")
        dialog.set_response_appearance("apply", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("apply")
        dialog.set_close_response("cancel")

        def cleanup():
            shutil.rmtree(theme.parent, ignore_errors=True)

        def response(_dialog, choice):
            if choice != "apply":
                cleanup()
                return
            try:
                set_picture_fit(theme, fit.get_selected() == 0)
            except OSError as error:
                cleanup()
                self.window.show_output("Could not prepare picture", str(error), False)
                return
            args = ["install", str(theme), "--apply"]
            if not rebuild:
                args.append("--no-rebuild")
            self.window._privileged(args, "Setting picture boot screen",
                                    on_ok=self.window.refresh, on_finished=cleanup)

        dialog.connect("response", response)
        dialog.present(self.window)
