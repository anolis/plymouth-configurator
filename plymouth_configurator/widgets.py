"""Reusable widgets: the theme preview renderer and the grid card."""

from __future__ import annotations

import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from gi.repository import Gdk, GLib, Graphene, Gsk, Gtk

from .themes import ThemeInfo

FPS = 24
MAX_TEXTURE_PX = 720
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="preview-load")


class TextureCache:
    """Decode PNGs to downscaled GdkMemoryTextures, thread-safe, bounded."""

    def __init__(self, capacity: int = 4000):
        self._lock = threading.Lock()
        self._items: OrderedDict[Path, Gdk.Texture] = OrderedDict()
        self._capacity = capacity

    def get(self, path: Path) -> Gdk.Texture | None:
        with self._lock:
            tex = self._items.get(path)
            if tex is not None:
                self._items.move_to_end(path)
            return tex

    def load(self, path: Path) -> Gdk.Texture | None:
        tex = self.get(path)
        if tex is not None:
            return tex
        tex = _decode(path)
        if tex is None:
            return None
        with self._lock:
            self._items[path] = tex
            self._items.move_to_end(path)
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)
        return tex


def _decode(path: Path) -> Gdk.Texture | None:
    try:
        from PIL import Image

        with Image.open(path) as im:
            im = im.convert("RGBA")
            if max(im.size) > MAX_TEXTURE_PX:
                im.thumbnail((MAX_TEXTURE_PX, MAX_TEXTURE_PX), Image.LANCZOS)
            w, h = im.size
            data = im.tobytes()
        return Gdk.MemoryTexture.new(
            w, h, Gdk.MemoryFormat.R8G8B8A8, GLib.Bytes.new(data), w * 4
        )
    except Exception:
        return None


textures = TextureCache()


def _rgba(color) -> Gdk.RGBA:
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = color[0], color[1], color[2], 1.0
    return c


class ThemePreview(Gtk.Widget):
    """Draws a theme's splash: gradient, background image and animation frame."""

    __gtype_name__ = "ThemePreview"

    def __init__(self, theme: ThemeInfo | None = None, radius: float = 10.0,
                 frame_fill: float = 0.62, min_width: int = 120):
        super().__init__()
        self._theme: ThemeInfo | None = None
        self._radius = radius
        self._frame_fill = frame_fill
        self._min_width = min_width
        self._frames: list[Gdk.Texture | None] = []
        self._bg: Gdk.Texture | None = None
        self._static: Gdk.Texture | None = None
        self._index = 0
        self._timer = 0
        self._generation = 0
        self._frames_requested = False
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.add_css_class("theme-preview")
        self.connect("unmap", lambda *_: self.stop())
        if theme is not None:
            self.set_theme(theme)

    # -- model -----------------------------------------------------------------

    @property
    def theme(self) -> ThemeInfo | None:
        return self._theme

    def set_theme(self, theme: ThemeInfo | None) -> None:
        self.stop()
        self._theme = theme
        self._generation += 1
        self._frames, self._bg, self._static = [], None, None
        self._index = 0
        self._frames_requested = False
        self.queue_draw()
        if theme is None:
            return
        gen = self._generation
        first = [p for p in (theme.background_image, theme.static_image) if p]
        if theme.frames:
            first.append(theme.frames[0])
            self._frames = [None] * len(theme.frames)
        for path in first:
            _executor.submit(self._load_one, gen, path)

    def _load_one(self, gen: int, path: Path) -> None:
        tex = textures.load(path)
        GLib.idle_add(self._apply_texture, gen, path, tex)

    def _apply_texture(self, gen: int, path: Path, tex) -> bool:
        if gen != self._generation or self._theme is None or tex is None:
            return False
        t = self._theme
        if path == t.background_image:
            self._bg = tex
        if path == t.static_image:
            self._static = tex
        if t.frames:
            try:
                self._frames[t.frames.index(path)] = tex
            except ValueError:
                pass
        self.queue_draw()
        return False

    def _request_frames(self) -> None:
        if self._frames_requested or self._theme is None:
            return
        self._frames_requested = True
        gen = self._generation
        for path in self._theme.frames[1:]:
            _executor.submit(self._load_one, gen, path)

    # -- animation -------------------------------------------------------------

    @property
    def playing(self) -> bool:
        return self._timer != 0

    def play(self) -> None:
        if self._theme is None or len(self._theme.frames) < 2 or self._timer:
            return
        self._request_frames()
        self._timer = GLib.timeout_add(1000 // FPS, self._tick)

    def stop(self) -> None:
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        self._index = 0
        self.queue_draw()

    def _tick(self) -> bool:
        if not self._frames:
            self._timer = 0
            return False
        self._index = (self._index + 1) % len(self._frames)
        self.queue_draw()
        return True

    # -- layout / drawing --------------------------------------------------------

    def do_get_request_mode(self):
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            return (self._min_width, self._min_width, -1, -1)
        width = for_size if for_size > 0 else self._min_width
        h = int(width * 9 / 16)
        return (h, h, -1, -1)

    def _current_frame(self):
        if self._frames:
            tex = self._frames[self._index]
            if tex is None:
                # fall back to nearest loaded frame so playback never blanks
                for cand in self._frames[self._index::-1]:
                    if cand is not None:
                        return cand
                for cand in self._frames:
                    if cand is not None:
                        return cand
            return tex
        return self._static

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        w, h = self.get_width(), self.get_height()
        if w <= 0 or h <= 0:
            return
        bounds = Graphene.Rect().init(0, 0, w, h)
        rounded = Gsk.RoundedRect()
        rounded.init_from_rect(bounds, self._radius)
        snapshot.push_rounded_clip(rounded)

        top = self._theme.top_color if self._theme else (0.05, 0.05, 0.05)
        bottom = self._theme.bottom_color if self._theme else (0.02, 0.02, 0.02)
        stop_a, stop_b = Gsk.ColorStop(), Gsk.ColorStop()
        stop_a.offset, stop_a.color = 0.0, _rgba(top)
        stop_b.offset, stop_b.color = 1.0, _rgba(bottom)
        snapshot.append_linear_gradient(
            bounds, Graphene.Point().init(0, 0), Graphene.Point().init(0, h), [stop_a, stop_b]
        )

        if self._bg is not None:
            tw, th = self._bg.get_width(), self._bg.get_height()
            scale = (min(w / tw, h / th) if self._theme and self._theme.background_fit
                     else max(w / tw, h / th))
            dw, dh = tw * scale, th * scale
            snapshot.append_texture(
                self._bg, Graphene.Rect().init((w - dw) / 2, (h - dh) / 2, dw, dh)
            )

        tex = self._current_frame()
        if tex is not None:
            tw, th = tex.get_width(), tex.get_height()
            box_w, box_h = w * self._frame_fill, h * self._frame_fill
            scale = min(box_w / tw, box_h / th)
            dw, dh = tw * scale, th * scale
            snapshot.append_texture(
                tex, Graphene.Rect().init((w - dw) / 2, (h - dh) / 2, dw, dh)
            )
        elif self._theme is not None and not self._theme.has_preview:
            self._draw_placeholder(snapshot, w, h)
        snapshot.pop()

    def _draw_placeholder(self, snapshot, w, h):
        layout = self.create_pango_layout(self._theme.module if self._theme else "")
        layout.set_width(int(w * 0.9 * 1024))
        from gi.repository import Pango

        layout.set_alignment(Pango.Alignment.CENTER)
        _ink, logical = layout.get_pixel_extents()
        snapshot.save()
        snapshot.translate(Graphene.Point().init((w - logical.width) / 2 - logical.x,
                                                 (h - logical.height) / 2))
        color = Gdk.RGBA()
        color.parse("rgba(255,255,255,0.45)")
        snapshot.append_layout(layout, color)
        snapshot.restore()


class ThemeCard(Gtk.Box):
    """A grid tile: preview on top, name and description below."""

    def __init__(self, theme: ThemeInfo, width: int = 200, show_checkbox: bool = False):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.theme = theme
        self.add_css_class("card")
        self.add_css_class("theme-card")
        self.set_size_request(width, -1)

        self.preview = ThemePreview(theme, radius=0.0, min_width=width)
        overlay = Gtk.Overlay(child=self.preview)

        self.badge = Gtk.Label(label="Active")
        self.badge.add_css_class("badge")
        self.badge.set_halign(Gtk.Align.END)
        self.badge.set_valign(Gtk.Align.START)
        self.badge.set_margin_top(8)
        self.badge.set_margin_end(8)
        self.badge.set_visible(False)
        overlay.add_overlay(self.badge)

        self.check: Gtk.CheckButton | None = None
        if show_checkbox:
            self.check = Gtk.CheckButton()
            self.check.add_css_class("selection-mode")
            self.check.add_css_class("preview-check")
            self.check.set_halign(Gtk.Align.START)
            self.check.set_valign(Gtk.Align.START)
            self.check.set_margin_top(8)
            self.check.set_margin_start(8)
            overlay.add_overlay(self.check)

        if theme.frames:
            frames = Gtk.Label(label=f"{len(theme.frames)} frames")
            frames.add_css_class("badge")
            frames.add_css_class("badge-dim")
            frames.set_halign(Gtk.Align.END)
            frames.set_valign(Gtk.Align.END)
            frames.set_margin_bottom(8)
            frames.set_margin_end(8)
            overlay.add_overlay(frames)

        self.append(overlay)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        body.add_css_class("card-body")
        title = Gtk.Label(label=theme.name, xalign=0)
        title.add_css_class("heading")
        title.set_ellipsize(3)  # Pango.EllipsizeMode.END
        title.set_max_width_chars(12)
        body.append(title)
        subtitle_text = theme.description or theme.title
        if theme.source:
            subtitle_text = f"{theme.source} · {subtitle_text}"
        subtitle = Gtk.Label(label=subtitle_text, xalign=0)
        subtitle.add_css_class("dim-label")
        subtitle.add_css_class("caption")
        subtitle.set_ellipsize(3)
        subtitle.set_max_width_chars(12)
        body.append(subtitle)
        self.append(body)

        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_: self.preview.play())
        motion.connect("leave", lambda *_: self.preview.stop())
        self.add_controller(motion)

    def set_active(self, active: bool) -> None:
        self.badge.set_visible(active)
        if active:
            self.add_css_class("active-theme")
        else:
            self.remove_css_class("active-theme")
