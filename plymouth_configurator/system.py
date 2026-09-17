"""System introspection and privileged command execution."""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

from gi.repository import GLib

HELPER = Path(__file__).with_name("helper.py")
CACHE_DIR = Path(GLib.get_user_cache_dir()) / "plymouth-configurator"
CONFIG_FILE = Path(GLib.get_user_config_dir()) / "plymouth-configurator" / "settings.json"

_EXTRA_PATHS = ["/usr/sbin", "/sbin", "/usr/local/sbin"]


def which(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for p in _EXTRA_PATHS:
        cand = Path(p) / name
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def distro_name() -> str:
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "Linux"


def initrd_tool() -> str | None:
    for name in ("update-initramfs", "mkinitcpio", "dracut"):
        if which(name):
            return name
    return None


def has_x11_renderer() -> bool:
    patterns = [
        "/usr/lib/*/plymouth/renderers/x11.so",
        "/usr/lib64/plymouth/renderers/x11.so",
        "/usr/lib/plymouth/renderers/x11.so",
        "/usr/libexec/plymouth/renderers/x11.so",
    ]
    return any(glob.glob(p) for p in patterns)


def plymouth_version() -> str:
    tool = which("plymouth")
    if not tool:
        return "not installed"
    try:
        out = subprocess.run([tool, "--version"], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or out.stderr.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #

DEFAULT_SETTINGS = {"preview_seconds": 10, "rebuild_initrd": True, "card_width": 200}


def load_settings() -> dict:
    data = dict(DEFAULT_SETTINGS)
    try:
        data.update(json.loads(CONFIG_FILE.read_text()))
    except (OSError, ValueError):
        pass
    return data


def save_settings(data: dict) -> None:
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(data, indent=2))
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# privileged execution
# --------------------------------------------------------------------------- #

class PrivilegedResult:
    def __init__(self, ok: bool, output: str, cancelled: bool = False):
        self.ok = ok
        self.output = output
        self.cancelled = cancelled


def run_privileged(
    args: list[str],
    on_done: Callable[[PrivilegedResult], None],
    pass_display: bool = False,
) -> None:
    """Run ``helper.py args`` as root via pkexec in a thread; call back on main loop."""
    pkexec = which("pkexec")
    if pkexec is None:
        on_done(PrivilegedResult(False, "pkexec was not found. Install polkit, or run:\n"
                                 f"  sudo python3 {HELPER} {' '.join(args)}"))
        return

    cmd = [pkexec]
    if pass_display:
        env_args = []
        if os.environ.get("DISPLAY"):
            env_args.append(f"DISPLAY={os.environ['DISPLAY']}")
        if os.environ.get("XAUTHORITY"):
            env_args.append(f"XAUTHORITY={os.environ['XAUTHORITY']}")
        if env_args:
            cmd += ["env"] + env_args
    cmd += [sys.executable or "python3", str(HELPER)] + args

    def worker():
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
            output = (proc.stdout + proc.stderr).strip()
            cancelled = proc.returncode in (126, 127)
            if cancelled and not output:
                output = "Authentication was cancelled or refused."
            result = PrivilegedResult(proc.returncode == 0, output, cancelled)
        except OSError as exc:
            result = PrivilegedResult(False, str(exc))
        GLib.idle_add(on_done, result)

    threading.Thread(target=worker, daemon=True).start()


def run_in_thread(func: Callable, on_done: Callable) -> None:
    """Run ``func()`` in a thread and deliver ``(result, exception)`` on the main loop."""

    def worker():
        try:
            res, err = func(), None
        except Exception as exc:  # noqa: BLE001
            res, err = None, exc
        GLib.idle_add(on_done, res, err)

    threading.Thread(target=worker, daemon=True).start()
