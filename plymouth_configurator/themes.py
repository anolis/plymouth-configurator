"""Discovery and analysis of Plymouth themes.

A theme is a directory containing a ``<name>.plymouth`` INI file. Script
themes additionally have a ``.script`` file and a set of PNG images, often a
numbered animation sequence such as ``progress-0.png`` … ``progress-32.png``.

This module reads that metadata and works out how to draw a representative
preview: background colours (from the script), an optional background image,
and either an animation frame sequence or a single static image.
"""

from __future__ import annotations

import configparser
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

THEME_DIRS = [
    Path("/usr/share/plymouth/themes"),
    Path("/usr/local/share/plymouth/themes"),
]
SYSTEM_THEME_DIR = THEME_DIRS[0]
PLYMOUTHD_CONF = Path("/etc/plymouth/plymouthd.conf")

_SEQ_RE = re.compile(r"^(?P<prefix>.*?)(?P<num>\d+)\.png$", re.IGNORECASE)
_COLOR_RE = {
    "top": re.compile(
        r"Window\.SetBackgroundTopColor\s*\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)"
    ),
    "bottom": re.compile(
        r"Window\.SetBackgroundBottomColor\s*\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)"
    ),
}
_IMAGE_VAR_RE = re.compile(r'(\w+)\s*=\s*Image\s*\(\s*"([^"]+)"\s*\)')
_IMAGE_ANY_RE = re.compile(r'Image\s*\(\s*"([^"]+)"\s*\)')
_BG_NAME_RE = re.compile(r"(background|wallpaper|^bg[._-]|^bg$)", re.IGNORECASE)
_UI_NAME_RE = re.compile(
    r"(password|entry|lock|bullet|keyboard|capslock|keymap|dot|box|cursor|text)",
    re.IGNORECASE,
)

Color = tuple[float, float, float]


@dataclass
class ThemeInfo:
    """Everything the UI needs to know about one theme."""

    name: str  # directory name; what plymouth-set-default-theme expects
    title: str  # Name= field
    description: str
    comment: str
    module: str
    path: Path
    plymouth_file: Path
    script_file: Path | None
    image_dir: Path
    frames: list[Path] = field(default_factory=list)
    static_image: Path | None = None
    background_image: Path | None = None
    background_fit: bool = False
    top_color: Color = (0.0, 0.0, 0.0)
    bottom_color: Color = (0.0, 0.0, 0.0)
    image_count: int = 0
    size_bytes: int = 0
    source: str = ""  # e.g. "pack_1" for collection themes

    @property
    def is_script(self) -> bool:
        return self.module == "script"

    @property
    def has_preview(self) -> bool:
        return bool(self.frames or self.static_image or self.background_image)

    def matches(self, query: str) -> bool:
        q = query.strip().lower()
        if not q:
            return True
        hay = " ".join(
            [self.name, self.title, self.description, self.comment, self.module, self.source]
        ).lower()
        return all(part in hay for part in q.split())


def _read_ini(path: Path) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str  # keep key case
    try:
        cp.read(path, encoding="utf-8")
    except UnicodeDecodeError:
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        cp.optionxform = str
        cp.read(path, encoding="latin-1")
    return cp


def _find_plymouth_file(theme_dir: Path) -> Path | None:
    preferred = theme_dir / f"{theme_dir.name}.plymouth"
    if preferred.is_file():
        return preferred
    files = sorted(theme_dir.glob("*.plymouth"))
    return files[0] if files else None


def _parse_color(match: re.Match | None, default: Color) -> Color:
    if not match:
        return default
    try:
        r, g, b = (min(max(float(v), 0.0), 1.0) for v in match.groups())
        return (r, g, b)
    except ValueError:
        return default


def _parse_hex_color(value: str) -> Color | None:
    value = value.strip().lower()
    if value.startswith("0x"):
        value = value[2:]
    elif value.startswith("#"):
        value = value[1:]
    if len(value) != 6:
        return None
    try:
        n = int(value, 16)
    except ValueError:
        return None
    return (((n >> 16) & 0xFF) / 255, ((n >> 8) & 0xFF) / 255, (n & 0xFF) / 255)


def _image_area(path: Path) -> int:
    try:
        from PIL import Image

        with Image.open(path) as im:
            w, h = im.size
            return w * h
    except Exception:
        return 0


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def analyze_images(image_dir: Path, script_text: str) -> dict:
    """Work out frames, background and colours for a preview."""
    result: dict = {
        "frames": [],
        "static_image": None,
        "background_image": None,
        "background_fit": bool(re.search(r"\bfill_screen\s*=\s*0\s*;", script_text)),
        "top_color": (0.0, 0.0, 0.0),
        "bottom_color": (0.0, 0.0, 0.0),
        "image_count": 0,
    }
    if not image_dir.is_dir():
        return result

    pngs = sorted(p for p in image_dir.rglob("*.png") if p.is_file())
    result["image_count"] = len(pngs)
    if not pngs:
        return result

    # Colours from the script.
    top = _parse_color(_COLOR_RE["top"].search(script_text), (0.0, 0.0, 0.0))
    bottom = _parse_color(_COLOR_RE["bottom"].search(script_text), top)
    result["top_color"], result["bottom_color"] = top, bottom

    by_name = {p.name: p for p in pngs}

    # Background image: an image the script scales to the window size, or one
    # with a tell-tale name.
    background: Path | None = None
    for var, fname in _IMAGE_VAR_RE.findall(script_text):
        if re.search(rf"\b{re.escape(var)}\b[^\n]*\.Scale\s*\(\s*Window\.GetWidth", script_text):
            if fname in by_name:
                background = by_name[fname]
                break
    if background is None:
        for fname in _IMAGE_ANY_RE.findall(script_text):
            if _BG_NAME_RE.search(fname) and fname in by_name:
                background = by_name[fname]
                break
    if background is None:
        for p in pngs:
            if _BG_NAME_RE.search(p.stem):
                background = p
                break
    result["background_image"] = background

    # Animation frames: the longest numbered sequence.
    groups: dict[str, list[tuple[int, Path]]] = {}
    for p in pngs:
        if p == background:
            continue
        m = _SEQ_RE.match(p.name)
        if not m:
            continue
        key = f"{p.parent}/{m.group('prefix').lower()}"
        groups.setdefault(key, []).append((int(m.group("num")), p))
    sequences = [sorted(g) for g in groups.values() if len(g) >= 4]
    if sequences:
        sequences.sort(key=len, reverse=True)
        result["frames"] = [p for _n, p in sequences[0]]
        return result

    # Static fallback: a logo, else the biggest non-UI image.
    candidates = [p for p in pngs if p != background and not _UI_NAME_RE.search(p.stem)]
    if not candidates:
        if background is not None:
            return result
        candidates = pngs
    logos = [p for p in candidates if "logo" in p.stem.lower()]
    pool = logos or candidates
    result["static_image"] = max(pool, key=_image_area)
    return result


def load_theme(theme_dir: Path, source: str = "") -> ThemeInfo | None:
    """Build a ThemeInfo for ``theme_dir`` or return None if it is not a theme."""
    try:
        return _load_theme(theme_dir, source)
    except (configparser.Error, OSError, UnicodeError, ValueError) as exc:
        logging.getLogger(__name__).warning("Skipping theme %s: %s", theme_dir, exc)
        return None


def _load_theme(theme_dir: Path, source: str) -> ThemeInfo | None:
    theme_dir = Path(theme_dir)
    plymouth_file = _find_plymouth_file(theme_dir)
    if plymouth_file is None:
        return None

    cp = _read_ini(plymouth_file)
    section = "Plymouth Theme"
    if not cp.has_section(section):
        raise ValueError("Missing [Plymouth Theme] section")
    title = cp.get(section, "Name", fallback=theme_dir.name).strip()
    description = cp.get(section, "Description", fallback="").strip()
    comment = cp.get(section, "Comment", fallback="").strip()
    module = cp.get(section, "ModuleName", fallback="script").strip()

    script_value = cp.get(module, "ScriptFile", fallback="") or cp.get("script", "ScriptFile", fallback="")
    image_value = cp.get(module, "ImageDir", fallback="") or cp.get("script", "ImageDir", fallback="")

    # Files may reference their final install location; when reading a theme
    # that is not (yet) installed, fall back to the directory we were given.
    script_file: Path | None = None
    if script_value:
        candidate = Path(script_value)
        if candidate.is_file() and candidate.parent == theme_dir:
            script_file = candidate
        else:
            local = theme_dir / candidate.name
            script_file = local if local.is_file() else (candidate if candidate.is_file() else None)
    if script_file is None:
        scripts = sorted(theme_dir.glob("*.script"))
        script_file = scripts[0] if scripts else None

    image_dir = theme_dir
    if image_value:
        candidate = Path(image_value)
        if candidate.is_dir() and any(candidate.glob("*.png")) and not any(theme_dir.glob("*.png")):
            image_dir = candidate

    script_text = ""
    if script_file is not None:
        try:
            script_text = script_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            script_text = ""

    info = analyze_images(image_dir, script_text)
    if module != "script":
        # two-step & friends declare colours as hex in the .plymouth file
        for key, field_name in (("BackgroundStartColor", "top_color"),
                                ("BackgroundEndColor", "bottom_color")):
            value = cp.get(module, key, fallback="").strip()
            color = _parse_hex_color(value)
            if color is not None:
                info[field_name] = color
        if info["top_color"] != (0.0, 0.0, 0.0) and info["bottom_color"] == (0.0, 0.0, 0.0) \
                and not cp.has_option(module, "BackgroundEndColor"):
            info["bottom_color"] = info["top_color"]
    return ThemeInfo(
        name=theme_dir.name,
        title=title,
        description=description,
        comment=comment,
        module=module,
        path=theme_dir,
        plymouth_file=plymouth_file,
        script_file=script_file,
        image_dir=image_dir,
        size_bytes=_dir_size(theme_dir),
        source=source,
        **info,
    )


def scan_installed() -> list[ThemeInfo]:
    """All themes installed in the system theme directories."""
    themes: dict[str, ThemeInfo] = {}
    for base in THEME_DIRS:
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if entry.name in themes:
                continue
            theme = load_theme(entry)
            if theme is not None:
                themes[entry.name] = theme
    return sorted(themes.values(), key=lambda t: t.name.lower())


def find_theme_dirs(root: Path, max_depth: int = 4) -> list[Path]:
    """Directories under ``root`` (inclusive) that contain a .plymouth file."""
    root = Path(root)
    found: list[Path] = []
    if not root.is_dir():
        return found
    root_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        if len(Path(dirpath).parts) - root_depth >= max_depth:
            dirnames[:] = []
        if any(f.endswith(".plymouth") for f in filenames):
            found.append(Path(dirpath))
            dirnames[:] = []  # a theme does not contain other themes
    return found


def load_theme_dirs(dirs: list[Path], root: Path | None = None) -> list[ThemeInfo]:
    themes = []
    for d in dirs:
        source = ""
        if root is not None and d.parent != Path(root):
            source = d.parent.name
        t = load_theme(d, source=source)
        if t is not None:
            themes.append(t)
    return themes


def _which(name: str) -> str | None:
    paths = os.environ.get("PATH", "").split(os.pathsep) + [
        "/usr/sbin",
        "/sbin",
        "/usr/local/sbin",
    ]
    for p in paths:
        cand = Path(p) / name
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def current_theme_name() -> str | None:
    """Name of the theme plymouth will use at boot, best effort."""
    tool = _which("plymouth-set-default-theme")
    if tool:
        try:
            out = subprocess.run(
                [tool], capture_output=True, text=True, timeout=10, check=False
            ).stdout.strip()
            if out and "/" not in out and " " not in out:
                return out
        except (OSError, subprocess.SubprocessError):
            pass
    if PLYMOUTHD_CONF.is_file():
        try:
            cp = _read_ini(PLYMOUTHD_CONF)
            theme = cp.get("Daemon", "Theme", fallback="").strip()
            if theme:
                return theme
        except (configparser.Error, OSError):
            pass
    default = SYSTEM_THEME_DIR / "default.plymouth"
    try:
        target = default.resolve(strict=True)
        if target.is_file() and target.suffix == ".plymouth":
            return target.parent.name
    except OSError:
        pass
    return None
