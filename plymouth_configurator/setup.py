"""Interactive Plymouth setup, with explicit fallback choices."""

from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk

from .bootstrap import SOURCE_REF, native_install_plan, plymouth_present
from .system import run_privileged


class PlymouthSetup:
    def __init__(self, window):
        self.window = window
        self.running = False
        self.prompt_open = False

    def ensure(self, on_ready=None):
        if plymouth_present():
            if on_ready:
                on_ready()
            return False
        self.prompt(on_ready)
        return False

    def prompt(self, on_ready=None):
        if self.running or self.prompt_open:
            return
        plan = native_install_plan()
        if plan is None:
            self._fallback("No supported package manager was detected.", on_ready)
            return
        dialog = Adw.AlertDialog(
            heading="Install Plymouth?",
            body=f"Plymouth is needed to display your boot screen. Install it using {plan.manager}? "
                 "Your system will ask for administrator authentication.")
        dialog.add_response("cancel", "Not now")
        dialog.add_response("install", "Install Plymouth")
        dialog.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("install")
        dialog.set_close_response("cancel")
        self.prompt_open = True

        def response(_dialog, choice):
            self.prompt_open = False
            if choice == "install":
                self._run("native", on_ready)

        dialog.connect("response", response)
        dialog.present(self.window)

    def _fallback(self, reason, on_ready=None, output=""):
        dialog = Adw.AlertDialog(
            heading="Other ways to install Plymouth",
            body=reason + " Choose a trusted binary package for your distribution, or build "
                 "the upstream source. Source builds need development dependencies and "
                 "manual integration with your distribution's boot process.")
        dialog.add_response("cancel", "Cancel")
        if output:
            dialog.add_response("log", "View error")
        if native_install_plan():
            dialog.add_response("retry", "Retry package manager")
        dialog.add_response("binary", "Choose binary package…")
        dialog.add_response("source", "Build from source…")
        dialog.set_close_response("cancel")
        self.prompt_open = True

        def response(_dialog, choice):
            self.prompt_open = False
            if choice == "retry":
                self._run("native", on_ready)
            elif choice == "binary":
                self._choose_binary(on_ready)
            elif choice == "source":
                self.window.confirm(
                    "Build and install Plymouth?",
                    f"Download release {SOURCE_REF} from Plymouth's official Git repository, "
                    "build it with Meson, and install it under /usr with administrator rights. "
                    "Git, a C compiler, Meson, Ninja, pkg-config and Plymouth development "
                    "dependencies must already be installed. This installation is not managed "
                    "by your package manager. You will need to configure boot integration.",
                    "Build and install", False, lambda: self._run("source", on_ready))
            elif choice == "log":
                self.window.show_output("Plymouth installation failed", output, False)

        dialog.connect("response", response)
        dialog.present(self.window)

    def _choose_binary(self, on_ready):
        dialog = Gtk.FileDialog(title="Choose a Plymouth package for this distribution")
        filter_ = Gtk.FileFilter()
        filter_.set_name("Binary packages")
        for pattern in ("*.deb", "*.rpm", "*.pkg.tar.zst", "*.pkg.tar.xz", "*.pkg.tar.gz"):
            filter_.add_pattern(pattern)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(filter_)
        dialog.set_filters(filters)

        def chosen(dialog, result):
            try:
                file = dialog.open_finish(result)
            except GLib.Error:
                return
            path = file.get_path()
            if not path:
                self.window.toast("Choose a local package file")
                return
            self.window.confirm("Install binary package?",
                                f"Install {Path(path).name} using your package manager? "
                                "Only use packages from a source you trust and built for your distribution.",
                                "Install", False,
                                lambda: self._run("binary", on_ready, path))

        dialog.open(self.window, None, chosen)

    def _run(self, method, on_ready=None, package=None):
        if self.running:
            return
        self.running = True
        self.window.set_busy(True)
        self.window.toast("Installing Plymouth… waiting for authentication")
        args = ["install-plymouth", "--method", method]
        if package:
            args += ["--package", package]

        def done(result):
            self.running = False
            self.window.set_busy(False)
            self.window.refresh()
            if result.cancelled:
                self.window.toast("Plymouth installation cancelled")
            elif result.ok and plymouth_present():
                if method == "source":
                    self.window.show_output("Plymouth installed from source", result.output, True)
                else:
                    self.window.toast("Plymouth installed")
                if on_ready:
                    on_ready()
            else:
                self._fallback("Plymouth could not be installed.", on_ready,
                               result.output or "Plymouth binaries are still missing.")

        run_privileged(args, done)
