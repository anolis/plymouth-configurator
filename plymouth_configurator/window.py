"""Main application window."""

from __future__ import annotations

import os
from pathlib import Path

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from . import APP_NAME, VERSION
from .installer import InstallCoordinator
from .bootstrap import plymouth_present
from .setup import PlymouthSetup
from .pictures import PictureCoordinator
from .system import (
    PrivilegedResult,
    distro_name,
    has_x11_renderer,
    initrd_tool,
    load_settings,
    plymouth_version,
    run_in_thread,
    run_privileged,
    save_settings,
)
from .themes import ThemeInfo, current_theme_name, scan_installed
from .widgets import ThemeCard, ThemePreview


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


class DetailsPanel(Gtk.Box):
    """Sidebar describing the selected theme with its actions."""

    def __init__(self, window: "MainWindow"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.theme: ThemeInfo | None = None

        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.append(self.stack)

        empty = Adw.StatusPage(
            icon_name="view-grid-symbolic",
            title="No theme selected",
            description="Pick a theme from the grid to see details and actions.",
        )
        empty.add_css_class("compact")
        self.stack.add_named(empty, "empty")

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        for side in ("start", "end", "top", "bottom"):
            getattr(content, f"set_margin_{side}")(16)
        scroller.set_child(content)
        self.stack.add_named(scroller, "details")

        self.preview = ThemePreview(radius=12.0, frame_fill=0.6, min_width=200)
        self.preview.add_css_class("details-preview")
        content.append(self.preview)

        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.title = Gtk.Label(xalign=0, wrap=True)
        self.title.add_css_class("details-title")
        head.append(self.title)
        pills = Gtk.Box(spacing=6)
        self.module = Gtk.Label()
        self.module.add_css_class("module-pill")
        pills.append(self.module)
        self.active = Gtk.Label(label="Active boot theme")
        self.active.add_css_class("badge")
        pills.append(self.active)
        head.append(pills)
        self.description = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
        self.description.set_margin_top(6)
        head.append(self.description)
        self.comment = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
        self.comment.add_css_class("dim-label")
        self.comment.add_css_class("caption")
        head.append(self.comment)
        content.append(head)

        actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.apply_btn = Gtk.Button()
        self.apply_btn.set_child(Adw.ButtonContent(icon_name="emblem-ok-symbolic",
                                                   label="Set as boot theme"))
        self.apply_btn.add_css_class("suggested-action")
        self.apply_btn.add_css_class("pill")
        self.apply_btn.connect("clicked", lambda *_: self.window.apply_theme(self.theme))
        actions.append(self.apply_btn)

        row = Gtk.Box(spacing=8, homogeneous=True)
        self.preview_btn = Gtk.Button()
        self.preview_btn.set_child(Adw.ButtonContent(icon_name="media-playback-start-symbolic",
                                                     label="Live preview"))
        self.preview_btn.set_tooltip_text("Run plymouthd on this display for a few seconds")
        self.preview_btn.connect("clicked", lambda *_: self.window.live_preview(self.theme))
        row.append(self.preview_btn)
        folder_btn = Gtk.Button()
        folder_btn.set_child(Adw.ButtonContent(icon_name="folder-open-symbolic", label="Open folder"))
        folder_btn.connect("clicked", lambda *_: self.window.open_folder(self.theme))
        row.append(folder_btn)
        actions.append(row)
        content.append(actions)

        group = Adw.PreferencesGroup()
        self.rows: dict[str, Adw.ActionRow] = {}
        for key, label in (("name", "Theme id"), ("path", "Location"), ("frames", "Animation"),
                           ("images", "Images"), ("size", "Size on disk")):
            r = Adw.ActionRow(title=label)
            r.add_css_class("property")
            r.set_subtitle_selectable(True)
            self.rows[key] = r
            group.add(r)
        content.append(group)

        self.uninstall_btn = Gtk.Button()
        self.uninstall_btn.set_child(Adw.ButtonContent(icon_name="user-trash-symbolic",
                                                       label="Uninstall theme"))
        self.uninstall_btn.add_css_class("destructive-action")
        self.uninstall_btn.set_halign(Gtk.Align.CENTER)
        self.uninstall_btn.connect("clicked", lambda *_: self.window.uninstall_theme(self.theme))
        content.append(self.uninstall_btn)

        self.stack.set_visible_child_name("empty")

    def show_theme(self, theme: ThemeInfo | None) -> None:
        self.theme = theme
        if theme is None:
            self.preview.set_theme(None)
            self.stack.set_visible_child_name("empty")
            return
        is_active = theme.name == self.window.current_name
        self.preview.set_theme(theme)
        self.preview.play()
        self.title.set_label(theme.name)
        self.module.set_label(theme.module)
        self.active.set_visible(is_active)
        desc = theme.description or theme.title
        self.description.set_label(desc)
        self.comment.set_label(theme.comment)
        self.comment.set_visible(bool(theme.comment))
        self.rows["name"].set_subtitle(theme.title if theme.title != theme.name else theme.name)
        self.rows["path"].set_subtitle(str(theme.path))
        self.rows["frames"].set_subtitle(
            f"{len(theme.frames)} frames" if theme.frames else "static / not detected")
        self.rows["images"].set_subtitle(str(theme.image_count))
        self.rows["size"].set_subtitle(human_size(theme.size_bytes))
        self.apply_btn.set_sensitive(not is_active)
        self.uninstall_btn.set_sensitive(not is_active)
        self.uninstall_btn.set_tooltip_text(
            "Switch to another theme before removing the active one" if is_active else "")
        self.stack.set_visible_child_name("details")


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title=APP_NAME)
        self.set_default_size(1180, 760)
        self.settings = load_settings()
        self.themes: list[ThemeInfo] = []
        self.current_name: str | None = None
        self.cards: dict[str, ThemeCard] = {}
        self._busy = 0
        self.installer = InstallCoordinator(self)
        self.plymouth_setup = PlymouthSetup(self)
        self.pictures = PictureCoordinator(self)

        self._build_actions()
        self._build_ui()
        self.refresh()
        GLib.idle_add(self.plymouth_setup.ensure)

    # -- UI ----------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

        toolbar = Adw.ToolbarView()
        self.toast_overlay.set_child(toolbar)

        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        install_menu = Gio.Menu()
        install_menu.append("Use a picture…", "win.use-picture")
        install_menu.append("From folder…", "win.install-folder")
        install_menu.append("From archive (zip / tar)…", "win.install-archive")
        install_menu.append("Browse adi1090x collection…", "win.install-collection")
        install_btn = Gtk.MenuButton(menu_model=install_menu)
        install_btn.set_child(Adw.ButtonContent(icon_name="list-add-symbolic", label="Install"))
        header.pack_start(install_btn)

        self.search_btn = Gtk.ToggleButton(icon_name="edit-find-symbolic",
                                           tooltip_text="Search themes (Ctrl+F)")
        header.pack_end(self._main_menu_button())
        header.pack_end(self.search_btn)
        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Rescan themes",
                                 action_name="win.refresh")
        header.pack_end(refresh_btn)
        self.spinner = Adw.Spinner()
        self.spinner.set_visible(False)
        header.pack_end(self.spinner)

        self.title_widget = Adw.WindowTitle(title=APP_NAME, subtitle="")
        header.set_title_widget(self.title_widget)

        self.search_bar = Gtk.SearchBar()
        self.search_entry = Gtk.SearchEntry(placeholder_text="Search by name, description or module")
        self.search_entry.set_hexpand(True)
        self.search_bar.set_child(self.search_entry)
        self.search_bar.connect_entry(self.search_entry)
        self.search_bar.set_key_capture_widget(self)
        self.search_btn.bind_property("active", self.search_bar, "search-mode-enabled",
                                      GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE)
        self.search_entry.connect("search-changed", lambda *_: self.flow.invalidate_filter())
        toolbar.add_top_bar(self.search_bar)

        self.split = Adw.OverlaySplitView()
        self.split.set_sidebar_position(Gtk.PackType.END)
        self.split.set_min_sidebar_width(320)
        self.split.set_max_sidebar_width(400)
        self.split.set_sidebar_width_fraction(0.3)

        self.drop_overlay = Gtk.Overlay(child=self.split)
        toolbar.set_content(self.drop_overlay)
        self._build_drop_target()

        self.details = DetailsPanel(self)
        self.split.set_sidebar(self.details)

        self.content_stack = Gtk.Stack()
        self.split.set_content(self.content_stack)

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.flow = Gtk.FlowBox()
        self.flow.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.flow.set_homogeneous(True)
        self.flow.set_min_children_per_line(2)
        self.flow.set_max_children_per_line(8)
        self.flow.set_column_spacing(16)
        self.flow.set_row_spacing(16)
        self.flow.set_valign(Gtk.Align.START)
        for side in ("start", "end", "top", "bottom"):
            getattr(self.flow, f"set_margin_{side}")(18)
        self.flow.set_filter_func(self._filter)
        self.flow.set_sort_func(self._sort)
        self.flow.connect("selected-children-changed", self._on_selection)
        scroller.set_child(self.flow)
        self.content_stack.add_named(scroller, "grid")

        self.empty = Adw.StatusPage(
            icon_name="preferences-desktop-wallpaper-symbolic",
            title="No Plymouth themes found",
            description="Nothing was found in /usr/share/plymouth/themes. "
                        "Install plymouth, or add themes with the Install button.",
        )
        self.content_stack.add_named(self.empty, "empty")

        loading = Adw.StatusPage(title="Scanning themes…")
        loading.set_child(Adw.Spinner())
        self.content_stack.add_named(loading, "loading")

        self.banner = Adw.Banner()
        self.banner.set_button_label("Install Plymouth")
        self.banner.connect("button-clicked", lambda *_: self.plymouth_setup.prompt())
        self.banner.set_revealed(False)
        toolbar.add_top_bar(self.banner)
        if not self._plymouth_present():
            self.banner.set_title("Plymouth does not seem to be installed on this system.")
            self.banner.set_revealed(True)

        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 900sp"))
        bp.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(bp)

    def _build_drop_target(self) -> None:
        self.drop_hint = Adw.StatusPage(
            icon_name="document-save-symbolic",
            title="Drop to install",
            description="Theme folders and zip / tar archives are accepted.",
        )
        self.drop_hint.add_css_class("drop-hint")
        self.drop_hint.set_can_target(False)
        self.drop_hint.set_visible(False)
        self.drop_overlay.add_overlay(self.drop_hint)

        target = Gtk.DropTarget.new(GObject.TYPE_NONE, Gdk.DragAction.COPY)
        target.set_gtypes([Gdk.FileList, Gio.File])
        target.connect("enter", self._on_drag_enter)
        target.connect("leave", lambda *_: self.drop_hint.set_visible(False))
        target.connect("drop", self._on_drop)
        self.add_controller(target)

    def _on_drag_enter(self, _target, _x, _y):
        self.drop_hint.set_visible(True)
        return Gdk.DragAction.COPY

    def _on_drop(self, _target, value, _x, _y) -> bool:
        self.drop_hint.set_visible(False)
        if self._busy:
            self.toast("Wait for the current operation to finish")
            return False
        if isinstance(value, Gdk.FileList):
            files = value.get_files()
        elif isinstance(value, Gio.File):
            files = [value]
        else:
            return False
        paths = [Path(f.get_path()) for f in files if f.get_path()]
        if not paths:
            self.toast("Only local files and folders can be dropped here")
            return False
        self.installer.from_paths(paths)
        return True

    def _main_menu_button(self) -> Gtk.MenuButton:
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("Rebuild initramfs after applying", "win.rebuild-initrd-after-apply")
        sub = Gio.Menu()
        for s in (5, 10, 20, 30, 60):
            item = Gio.MenuItem.new(f"{s} seconds", None)
            item.set_action_and_target_value("win.preview-seconds", GLib.Variant("i", s))
            sub.append_item(item)
        section.append_submenu("Live preview duration", sub)
        menu.append_section(None, section)
        section2 = Gio.Menu()
        section2.append("Rebuild initramfs now", "win.rebuild-initrd")
        section2.append("Show system info", "win.system-info")
        menu.append_section(None, section2)
        section3 = Gio.Menu()
        section3.append("About Plymouth Configurator", "win.about")
        menu.append_section(None, section3)
        return Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                              tooltip_text="Main menu")

    def _build_actions(self) -> None:
        def add(name, cb):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_: cb())
            self.add_action(a)
            return a

        add("refresh", self.refresh)
        add("install-folder", self.installer.from_folder)
        add("use-picture", self.pictures.choose)
        add("install-archive", self.installer.from_archive)
        add("install-collection", self.installer.from_collection)
        add("rebuild-initrd", self.rebuild_initrd)
        add("system-info", self.show_system_info)
        add("about", self.show_about)
        add("focus-search", lambda: self.search_btn.set_active(True))

        rebuild = Gio.SimpleAction.new_stateful(
            "rebuild-initrd-after-apply", None,
            GLib.Variant("b", bool(self.settings.get("rebuild_initrd", True))))
        rebuild.connect("change-state", self._on_toggle_rebuild)
        self.add_action(rebuild)

        seconds = Gio.SimpleAction.new_stateful(
            "preview-seconds", GLib.VariantType("i"),
            GLib.Variant("i", int(self.settings.get("preview_seconds", 10))))
        seconds.connect("change-state", self._on_preview_seconds)
        self.add_action(seconds)

        app = self.get_application()
        app.set_accels_for_action("win.refresh", ["F5", "<Control>r"])
        app.set_accels_for_action("win.focus-search", ["<Control>f"])
        app.set_accels_for_action("win.install-folder", ["<Control>o"])

    def _on_toggle_rebuild(self, action, value):
        action.set_state(value)
        self.settings["rebuild_initrd"] = value.get_boolean()
        save_settings(self.settings)

    def _on_preview_seconds(self, action, value):
        action.set_state(value)
        self.settings["preview_seconds"] = value.get_int32()
        save_settings(self.settings)

    # -- data --------------------------------------------------------------------

    @staticmethod
    def _plymouth_present() -> bool:
        return plymouth_present()

    def refresh(self) -> None:
        self.banner.set_title("Plymouth is not installed. Install it to use a boot screen.")
        self.banner.set_revealed(not self._plymouth_present())
        self.content_stack.set_visible_child_name("loading")
        selected = self.details.theme.name if self.details.theme else None

        def work():
            return scan_installed(), current_theme_name()

        def done(result, err):
            if err:
                self.toast(f"Scan failed: {err}")
                self.content_stack.set_visible_child_name("empty")
                return
            self.themes, self.current_name = result
            self._populate(selected)

        run_in_thread(work, done)

    def _populate(self, select_name: str | None = None) -> None:
        self.flow.remove_all()
        self.cards.clear()
        for theme in self.themes:
            card = ThemeCard(theme, width=int(self.settings.get("card_width", 240)))
            card.set_active(theme.name == self.current_name)
            self.cards[theme.name] = card
            self.flow.append(card)
        self.flow.invalidate_sort()
        self.flow.invalidate_filter()
        if not self.themes:
            self.content_stack.set_visible_child_name("empty")
            self.details.show_theme(None)
        else:
            self.content_stack.set_visible_child_name("grid")
            target = select_name if select_name in self.cards else self.current_name
            if target in self.cards:
                self.flow.select_child(self.cards[target].get_parent())
            else:
                self.details.show_theme(None)
        n = len(self.themes)
        cur = f"boot theme: {self.current_name}" if self.current_name else "boot theme unknown"
        self.title_widget.set_subtitle(f"{n} theme{'s' if n != 1 else ''} installed · {cur}")

    def _filter(self, child: Gtk.FlowBoxChild) -> bool:
        card = child.get_child()
        return card.theme.matches(self.search_entry.get_text())

    def _sort(self, a: Gtk.FlowBoxChild, b: Gtk.FlowBoxChild) -> int:
        ta, tb = a.get_child().theme, b.get_child().theme
        ka = (ta.name != self.current_name, ta.name.lower())
        kb = (tb.name != self.current_name, tb.name.lower())
        return (ka > kb) - (ka < kb)

    def _on_selection(self, flow: Gtk.FlowBox) -> None:
        children = flow.get_selected_children()
        theme = children[0].get_child().theme if children else None
        self.details.show_theme(theme)
        if theme is not None and self.split.get_collapsed():
            self.split.set_show_sidebar(True)

    # -- helpers -----------------------------------------------------------------

    def toast(self, text: str, timeout: int = 4) -> None:
        self.toast_overlay.add_toast(Adw.Toast(title=text, timeout=timeout))

    def set_busy(self, busy: bool) -> None:
        self._busy += 1 if busy else -1
        active = self._busy > 0
        self.spinner.set_visible(active)
        self.details.set_sensitive(not active)
        for name in ("install-folder", "install-archive", "install-collection", "use-picture",
                     "rebuild-initrd"):
            self.lookup_action(name).set_enabled(not active)

    def show_output(self, heading: str, output: str, ok: bool) -> None:
        dialog = Adw.AlertDialog(heading=heading,
                                 body="" if ok else "The command did not complete successfully.")
        view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                            wrap_mode=Gtk.WrapMode.WORD_CHAR)
        view.add_css_class("log-view")
        view.get_buffer().set_text(output or "(no output)")
        scroller = Gtk.ScrolledWindow(min_content_height=220, min_content_width=520,
                                      max_content_height=420, propagate_natural_height=True)
        scroller.add_css_class("card")
        scroller.set_child(view)
        dialog.set_extra_child(scroller)
        dialog.add_response("close", "Close")
        dialog.present(self)

    def confirm(self, heading: str, body: str, ok_label: str, destructive: bool, cb) -> None:
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ok", ok_label)
        dialog.set_response_appearance(
            "ok", Adw.ResponseAppearance.DESTRUCTIVE if destructive
            else Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, r: cb() if r == "ok" else None)
        dialog.present(self)

    def _privileged(self, args: list[str], label: str, on_ok=None,
                    pass_display: bool = False, show_log_on_success: bool = False,
                    on_finished=None) -> None:
        self.set_busy(True)
        self.toast(f"{label}… waiting for authentication")

        def done(result: PrivilegedResult):
            self.set_busy(False)
            if on_finished:
                on_finished()
            if result.ok:
                self.toast(f"{label}: done")
                if show_log_on_success:
                    self.show_output(label, result.output, True)
                if on_ok:
                    on_ok()
            elif result.cancelled:
                self.toast("Cancelled: authentication was not granted")
            else:
                # Applying/installing can succeed before an initramfs rebuild fails.
                self.refresh()
                self.show_output(f"{label} failed", result.output, False)

        run_privileged(args, done, pass_display=pass_display)

    # -- actions -----------------------------------------------------------------

    def apply_theme(self, theme: ThemeInfo | None) -> None:
        if theme is None:
            return
        if not self._plymouth_present():
            self.plymouth_setup.ensure(lambda: self.apply_theme(theme))
            return
        rebuild = bool(self.settings.get("rebuild_initrd", True))
        tool = initrd_tool()
        body = (f"“{theme.name}” will become the boot splash. ")
        if rebuild:
            body += (f"The initramfs will be regenerated with {tool}, which can take a minute."
                     if tool else "No initramfs tool was found, so the rebuild step will fail; "
                                  "you can disable it in the menu.")
        else:
            body += "The initramfs will not be rebuilt; run it later from the menu."
        args = ["set-default", theme.name] + ([] if rebuild else ["--no-rebuild"])
        self.confirm("Set boot theme?", body, "Apply", False,
                     lambda: self._privileged(args, f"Applying {theme.name}",
                                              on_ok=self.refresh))

    def uninstall_theme(self, theme: ThemeInfo | None) -> None:
        if theme is None:
            return
        if theme.name == self.current_name:
            self.toast("Switch to another theme before uninstalling the active one")
            return
        self.confirm("Uninstall theme?",
                     f"“{theme.name}” will be deleted from {theme.path.parent}. "
                     "This cannot be undone.", "Uninstall", True,
                     lambda: self._privileged(["uninstall", theme.name],
                                              f"Uninstalling {theme.name}", on_ok=self.refresh))

    def live_preview(self, theme: ThemeInfo | None) -> None:
        if theme is None:
            return
        if not has_x11_renderer():
            dialog = Adw.AlertDialog(
                heading="X11 renderer missing",
                body="Live preview runs plymouthd inside a window and needs Plymouth's x11 "
                     "renderer plugin. On Debian/Ubuntu install the “plymouth-x11” package; "
                     "on other distributions make sure plymouth was built with the x11 "
                     "renderer.\n\nYou can still apply the theme and see it at next boot.")
            dialog.add_response("close", "Close")
            dialog.present(self)
            return
        if not os.environ.get("DISPLAY"):
            self.toast("Live preview needs an X11 display (DISPLAY is not set)")
            return
        seconds = int(self.settings.get("preview_seconds", 10))
        self._privileged(["preview", theme.name, "--seconds", str(seconds)],
                         f"Previewing {theme.name} for {seconds}s", pass_display=True)

    def rebuild_initrd(self) -> None:
        tool = initrd_tool()
        if not tool:
            self.toast("No initramfs tool found (update-initramfs, mkinitcpio, dracut)")
            return
        self.confirm("Rebuild initramfs?", f"This runs {tool} for all installed kernels.",
                     "Rebuild", False,
                     lambda: self._privileged(["rebuild-initrd"], "Rebuilding initramfs",
                                              show_log_on_success=True))

    def open_folder(self, theme: ThemeInfo | None) -> None:
        if theme is None:
            return
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(theme.path)))
        launcher.launch(self, None, lambda l, res: self._launch_done(l, res))

    def _launch_done(self, launcher, result):
        try:
            launcher.launch_finish(result)
        except GLib.Error as exc:
            self.toast(f"Could not open folder: {exc.message}")

    def install_dirs(self, dirs: list[Path]) -> None:
        if not dirs:
            return
        names = ", ".join(d.name for d in dirs)
        self._privileged(["install"] + [str(d) for d in dirs],
                         f"Installing {len(dirs)} theme{'s' if len(dirs) != 1 else ''} ({names})",
                         on_ok=self.refresh)

    def show_system_info(self) -> None:
        tool = initrd_tool() or "none found"
        lines = [
            f"Distribution:      {distro_name()}",
            f"Plymouth:          {plymouth_version()}",
            f"Current theme:     {self.current_name or 'unknown'}",
            f"Initramfs tool:    {tool}",
            f"X11 renderer:      {'available' if has_x11_renderer() else 'missing (live preview disabled)'}",
            f"Theme directory:   /usr/share/plymouth/themes",
            f"Installed themes:  {len(self.themes)}",
        ]
        self.show_output("System info", "\n".join(lines), True)

    def show_about(self) -> None:
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon="preferences-desktop-wallpaper",
            version=VERSION,
            developer_name="anolis",
            license_type=Gtk.License.MIT_X11,
            comments="Browse, preview, install and apply Plymouth boot splash themes.",
            website="https://github.com/adi1090x/plymouth-themes",
        )
        about.set_debug_info("\n".join([
            f"Distribution: {distro_name()}",
            f"Plymouth: {plymouth_version()}",
            f"Initramfs tool: {initrd_tool()}",
            f"X11 renderer: {has_x11_renderer()}",
        ]))
        about.present(self)
