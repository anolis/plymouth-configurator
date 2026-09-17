"""Installing themes from folders, archives and the adi1090x collection."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from gi.repository import Adw, Gio, GLib, Gtk

from . import COLLECTION_URL
from .system import CACHE_DIR, run_in_thread, which
from .themes import ThemeInfo, find_theme_dirs, load_theme_dirs
from .widgets import ThemeCard

if TYPE_CHECKING:
    from .window import MainWindow


ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2",
                    ".tbz2", ".tar.zst")


def is_archive(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(s) for s in ARCHIVE_SUFFIXES)


def extract_archive(archive: Path) -> Path:
    """Unpack ``archive`` into the cache and return the directory."""
    digest = hashlib.sha1(str(archive).encode()).hexdigest()[:10]
    stem = archive.name
    for suffix in ARCHIVE_SUFFIXES:
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    dest = CACHE_DIR / "extract" / f"{stem}-{digest}"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    if archive.name.lower().endswith(".zst"):
        subprocess.run(["tar", "--zstd", "-xf", str(archive), "-C", str(dest)], check=True)
    else:
        shutil.unpack_archive(str(archive), str(dest))
    return dest


class ThemePicker(Adw.Dialog):
    """Grid of candidate themes with checkboxes; installs the chosen ones."""

    def __init__(self, window: "MainWindow", title: str, themes: list[ThemeInfo],
                 installed: set[str], preselect: bool = False):
        super().__init__(title=title)
        self.window = window
        self.cards: list[ThemeCard] = []
        self.set_content_width(1000)
        self.set_content_height(700)

        toolbar = Adw.ToolbarView()
        self.set_child(toolbar)
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        select_all = Gtk.Button(label="Select all")
        select_all.connect("clicked", lambda *_: self._set_all(True))
        header.pack_start(select_all)
        select_none = Gtk.Button(label="Clear")
        select_none.connect("clicked", lambda *_: self._set_all(False))
        header.pack_start(select_none)

        self.search = Gtk.SearchEntry(placeholder_text="Filter…")
        self.search.connect("search-changed", lambda *_: self.flow.invalidate_filter())
        header.pack_end(self.search)

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                min_children_per_line=2, max_children_per_line=8,
                                column_spacing=14, row_spacing=14, valign=Gtk.Align.START)
        for side in ("start", "end", "top", "bottom"):
            getattr(self.flow, f"set_margin_{side}")(16)
        self.flow.set_filter_func(self._filter)
        self.flow.connect("child-activated", self._toggle_child)
        scroller.set_child(self.flow)
        toolbar.set_content(scroller)

        for theme in themes:
            card = ThemeCard(theme, width=200, show_checkbox=True)
            if theme.name in installed:
                card.badge.set_label("Installed")
                card.badge.remove_css_class("badge")
                card.badge.add_css_class("badge")
                card.badge.add_css_class("badge-installed")
                card.badge.set_visible(True)
            card.check.set_active(preselect)
            card.check.connect("toggled", lambda *_: self._update_button())
            self.cards.append(card)
            self.flow.append(card)

        bottom = Gtk.Box(spacing=8)
        bottom.set_margin_top(8)
        bottom.set_margin_bottom(8)
        bottom.set_margin_start(12)
        bottom.set_margin_end(12)
        self.summary = Gtk.Label(xalign=0, hexpand=True)
        self.summary.add_css_class("dim-label")
        bottom.append(self.summary)
        self.install_btn = Gtk.Button(label="Install")
        self.install_btn.add_css_class("suggested-action")
        self.install_btn.add_css_class("pill")
        self.install_btn.connect("clicked", self._install)
        bottom.append(self.install_btn)
        toolbar.add_bottom_bar(bottom)
        self._update_button()

    def _filter(self, child):
        return child.get_child().theme.matches(self.search.get_text())

    def _toggle_child(self, _flow, child):
        check = child.get_child().check
        check.set_active(not check.get_active())

    def _set_all(self, value: bool):
        for card in self.cards:
            if card.get_parent().get_child_visible() if hasattr(card.get_parent(), "get_child_visible") else True:
                card.check.set_active(value)

    def _selected(self) -> list[ThemeInfo]:
        return [c.theme for c in self.cards if c.check.get_active()]

    def _update_button(self):
        sel = self._selected()
        self.install_btn.set_sensitive(bool(sel))
        n = len(sel)
        self.install_btn.set_label(f"Install {n} theme{'s' if n != 1 else ''}" if n else "Install")
        self.summary.set_label(f"{len(self.cards)} themes found · {n} selected")

    def _install(self, *_):
        dirs = [t.path for t in self._selected()]
        self.close()
        self.window.install_dirs(dirs)


class InstallCoordinator:
    """Handles the three install sources and opens the picker."""

    def __init__(self, window: "MainWindow"):
        self.window = window

    # -- sources -----------------------------------------------------------------

    def from_folder(self) -> None:
        dialog = Gtk.FileDialog(title="Choose a theme folder or a folder containing themes")
        dialog.select_folder(self.window, None, self._folder_chosen)

    def _folder_chosen(self, dialog, result):
        try:
            file = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        self._scan_and_pick(Path(file.get_path()), f"Themes in {file.get_basename()}")

    def from_archive(self) -> None:
        dialog = Gtk.FileDialog(title="Choose a theme archive")
        f = Gtk.FileFilter()
        f.set_name("Archives")
        for pattern in ("*.zip", "*.tar", "*.tar.gz", "*.tgz", "*.tar.xz", "*.tar.bz2", "*.tar.zst"):
            f.add_pattern(pattern)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(f)
        dialog.set_filters(filters)
        dialog.set_default_filter(f)
        dialog.open(self.window, None, self._archive_chosen)

    def _archive_chosen(self, dialog, result):
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return
        self.from_paths([Path(file.get_path())])

    def from_paths(self, paths: list[Path], title: str | None = None) -> None:
        """Install from a mix of theme folders and archives (used by drag & drop)."""
        dirs = [p for p in paths if p.is_dir()]
        archives = [p for p in paths if p.is_file() and is_archive(p)]
        ignored = [p for p in paths if p not in dirs and p not in archives]
        if ignored:
            self.window.toast("Ignored: " + ", ".join(p.name for p in ignored[:3])
                              + (" …" if len(ignored) > 3 else "") + " (not a folder or archive)")
        if not dirs and not archives:
            return
        if title is None:
            names = [p.name for p in dirs + archives]
            title = f"Themes in {names[0]}" if len(names) == 1 else f"Themes from {len(names)} items"
        self.window.set_busy(True)

        def work():
            roots = list(dirs)
            for archive in archives:
                roots.append(extract_archive(archive))
            return roots

        def done(roots, err):
            self.window.set_busy(False)
            if err:
                self.window.toast(f"Could not extract archive: {err}", 6)
                return
            self._scan_and_pick(roots, title, preselect=True)

        run_in_thread(work, done)

    def from_collection(self) -> None:
        git = which("git")
        if not git:
            self.window.toast("git is required to download the collection")
            return
        repo = CACHE_DIR / "plymouth-themes"
        self.window.set_busy(True)
        self.window.toast("Fetching adi1090x/plymouth-themes… (first time downloads ~100 MB)", 6)

        def work():
            if (repo / ".git").is_dir():
                subprocess.run([git, "-C", str(repo), "pull", "--ff-only", "--quiet"],
                               check=False, capture_output=True, text=True, timeout=600)
            else:
                repo.parent.mkdir(parents=True, exist_ok=True)
                proc = subprocess.run([git, "clone", "--depth", "1", COLLECTION_URL, str(repo)],
                                      capture_output=True, text=True, timeout=1800)
                if proc.returncode != 0:
                    raise RuntimeError(proc.stderr.strip() or "git clone failed")
            return repo

        def done(path, err):
            self.window.set_busy(False)
            if err:
                self.window.toast(f"Download failed: {err}", 8)
                return
            self._scan_and_pick(path, "adi1090x plymouth-themes collection")

        run_in_thread(work, done)

    # -- picker ------------------------------------------------------------------

    def _scan_and_pick(self, roots: Path | list[Path], title: str,
                       preselect: bool = False) -> None:
        roots = [roots] if isinstance(roots, Path) else list(roots)
        self.window.set_busy(True)

        def work():
            themes, seen = [], set()
            for root in roots:
                for t in load_theme_dirs(find_theme_dirs(root), root=root):
                    if t.path not in seen:
                        seen.add(t.path)
                        themes.append(t)
            return themes

        def done(themes, err):
            self.window.set_busy(False)
            if err:
                self.window.toast(f"Scan failed: {err}")
                return
            if not themes:
                self.window.toast("No themes found there (looked for *.plymouth files)")
                return
            themes.sort(key=lambda t: (t.source, t.name.lower()))
            installed = {t.name for t in self.window.themes}
            ThemePicker(self.window, title, themes, installed,
                        preselect=preselect).present(self.window)

        run_in_thread(work, done)
