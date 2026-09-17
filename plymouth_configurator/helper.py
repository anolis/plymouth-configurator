#!/usr/bin/env python3
"""Privileged helper for Plymouth Configurator.

This script is run as root through ``pkexec`` (or ``sudo``) and performs the
few operations that need elevated rights: applying a theme, rebuilding the
initramfs, installing/removing theme directories, and running a live preview.

It deliberately depends only on the standard library so it can be executed on
its own, outside the application package.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

THEME_DIR = Path("/usr/share/plymouth/themes")
PLYMOUTHD_CONF = Path("/etc/plymouth/plymouthd.conf")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

INITRD_TOOLS = [
    ("update-initramfs", ["update-initramfs", "-u", "-k", "all"]),
    ("mkinitcpio", ["mkinitcpio", "-P"]),
    ("dracut", ["dracut", "--regenerate-all", "--force"]),
]


class HelperError(Exception):
    pass


def log(msg: str) -> None:
    print(msg, flush=True)


def which(name: str) -> str | None:
    paths = os.environ.get("PATH", "").split(os.pathsep) + [
        "/usr/sbin",
        "/sbin",
        "/usr/local/sbin",
        "/usr/bin",
        "/bin",
    ]
    for p in paths:
        cand = Path(p) / name
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def run(cmd: list[str], env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    log("$ " + " ".join(cmd))
    proc = subprocess.run(cmd, env=env, text=True, capture_output=True)
    if proc.stdout.strip():
        log(proc.stdout.rstrip())
    if proc.stderr.strip():
        log(proc.stderr.rstrip())
    if check and proc.returncode != 0:
        raise HelperError(f"'{cmd[0]}' failed with exit code {proc.returncode}")
    return proc


def validate_name(name: str) -> str:
    if not NAME_RE.match(name) or name in {".", ".."}:
        raise HelperError(f"Invalid theme name: {name!r}")
    return name


def theme_path(name: str) -> Path:
    path = (THEME_DIR / validate_name(name)).resolve()
    if path.parent != THEME_DIR.resolve():
        raise HelperError(f"Refusing to touch path outside theme directory: {path}")
    return path


def uses_alternatives() -> bool:
    default = THEME_DIR / "default.plymouth"
    return which("update-alternatives") is not None and (
        default.is_symlink() and "alternatives" in os.readlink(default)
    )


def plymouth_file_in(theme_dir: Path) -> Path:
    preferred = theme_dir / f"{theme_dir.name}.plymouth"
    if preferred.is_file():
        return preferred
    files = sorted(theme_dir.glob("*.plymouth"))
    if not files:
        raise HelperError(f"No .plymouth file in {theme_dir}")
    return files[0]


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def cmd_rebuild_initrd(_args=None) -> None:
    for name, cmd in INITRD_TOOLS:
        tool = which(name)
        if tool:
            log(f"Rebuilding initramfs with {name} (this can take a while)…")
            run([tool] + cmd[1:])
            return
    raise HelperError(
        "No initramfs tool found (looked for update-initramfs, mkinitcpio, dracut)."
    )


def cmd_set_default(args) -> None:
    name = validate_name(args.name)
    path = theme_path(name)
    plymouth_file = plymouth_file_in(path)

    tool = which("plymouth-set-default-theme")
    if tool:
        run([tool, name])
    else:
        log(f"plymouth-set-default-theme not found, writing {PLYMOUTHD_CONF}")
        PLYMOUTHD_CONF.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        if PLYMOUTHD_CONF.is_file():
            lines = PLYMOUTHD_CONF.read_text().splitlines()
        out, in_daemon, wrote = [], False, False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("["):
                if in_daemon and not wrote:
                    out.append(f"Theme={name}")
                    wrote = True
                in_daemon = stripped.lower() == "[daemon]"
            elif in_daemon and re.match(r"^\s*Theme\s*=", line):
                if not wrote:
                    out.append(f"Theme={name}")
                    wrote = True
                continue
            out.append(line)
        if not wrote:
            if not in_daemon:
                out.append("[Daemon]")
            out.append(f"Theme={name}")
        PLYMOUTHD_CONF.write_text("\n".join(out) + "\n")

    if uses_alternatives():
        ua = which("update-alternatives")
        run([ua, "--install", str(THEME_DIR / "default.plymouth"), "default.plymouth",
             str(plymouth_file), "100"], check=False)
        run([ua, "--set", "default.plymouth", str(plymouth_file)], check=False)

    log(f"Default theme set to '{name}'.")
    if args.rebuild:
        cmd_rebuild_initrd()
    else:
        log("Initramfs not rebuilt; the change takes effect after the next rebuild.")


def rewrite_plymouth_file(plymouth_file: Path, dest: Path) -> None:
    text = plymouth_file.read_text(encoding="utf-8", errors="replace")
    out = []
    for line in text.splitlines():
        m = re.match(r"^(\s*)(ImageDir|ScriptFile)(\s*=\s*)(.*)$", line)
        if m:
            indent, key, sep, value = m.groups()
            value = value.strip()
            if key == "ImageDir":
                line = f"{indent}{key}{sep}{dest}"
            else:
                line = f"{indent}{key}{sep}{dest / Path(value).name}"
        out.append(line)
    plymouth_file.write_text("\n".join(out) + "\n", encoding="utf-8")


def cmd_install(args) -> None:
    THEME_DIR.mkdir(parents=True, exist_ok=True)
    for src in args.sources:
        src = Path(src).resolve()
        if not src.is_dir():
            raise HelperError(f"Not a directory: {src}")
        plymouth_file_in(src)  # validates
        name = validate_name(src.name)
        dest = theme_path(name)
        if dest.exists():
            log(f"Replacing existing theme '{name}'")
            shutil.rmtree(dest)
        log(f"Installing '{name}' → {dest}")
        shutil.copytree(src, dest, symlinks=False,
                        ignore=shutil.ignore_patterns(".git", "__pycache__"))
        os.chmod(dest, 0o755)
        for root, dirs, files in os.walk(dest):
            for d in dirs:
                os.chmod(Path(root) / d, 0o755)
            for f in files:
                os.chmod(Path(root) / f, 0o644)
        rewrite_plymouth_file(plymouth_file_in(dest), dest)
        if uses_alternatives():
            run([which("update-alternatives"), "--install",
                 str(THEME_DIR / "default.plymouth"), "default.plymouth",
                 str(plymouth_file_in(dest)), "100"], check=False)
    log("Done.")


def cmd_uninstall(args) -> None:
    for name in args.names:
        path = theme_path(name)
        if not path.is_dir():
            raise HelperError(f"Theme '{name}' is not installed")
        if uses_alternatives():
            try:
                pf = plymouth_file_in(path)
                run([which("update-alternatives"), "--remove", "default.plymouth", str(pf)],
                    check=False)
            except HelperError:
                pass
        log(f"Removing {path}")
        shutil.rmtree(path)
    log("Done.")


def cmd_preview(args) -> None:
    name = validate_name(args.name)
    if not theme_path(name).is_dir():
        raise HelperError(f"Theme '{name}' is not installed")
    plymouthd = which("plymouthd")
    plymouth = which("plymouth")
    if not plymouthd or not plymouth:
        raise HelperError("plymouthd/plymouth not found")

    env = os.environ.copy()
    if args.display:
        env["DISPLAY"] = args.display
    if args.xauthority:
        env["XAUTHORITY"] = args.xauthority

    if subprocess.run([plymouth, "--ping"]).returncode == 0:
        raise HelperError("A plymouth daemon is already running; stop it first.")

    log(f"Starting plymouthd with theme '{name}' for {args.seconds}s")
    run([plymouthd, f"--kernel-command-line=plymouth.splash={name}", "--mode=boot",
         "--no-boot-log"], env=env)
    try:
        run([plymouth, "--show-splash"], env=env, check=False)
        for i in range(max(1, args.seconds)):
            subprocess.run([plymouth, f"--update=test{i}"], env=env)
            time.sleep(1)
    finally:
        subprocess.run([plymouth, "quit"], env=env)
    log("Preview finished.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("set-default", help="apply a theme")
    p.add_argument("name")
    p.add_argument("--no-rebuild", dest="rebuild", action="store_false")
    p.set_defaults(func=cmd_set_default)

    p = sub.add_parser("rebuild-initrd", help="regenerate the initramfs")
    p.set_defaults(func=cmd_rebuild_initrd)

    p = sub.add_parser("install", help="copy theme directories into the theme dir")
    p.add_argument("sources", nargs="+")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("uninstall", help="remove installed themes")
    p.add_argument("names", nargs="+")
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("preview", help="show a theme with plymouthd on the current display")
    p.add_argument("name")
    p.add_argument("--seconds", type=int, default=10)
    p.add_argument("--display", default=os.environ.get("DISPLAY", ""))
    p.add_argument("--xauthority", default=os.environ.get("XAUTHORITY", ""))
    p.set_defaults(func=cmd_preview)

    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        print("This helper must run as root (it is normally launched via pkexec).",
              file=sys.stderr)
        return 2
    try:
        args.func(args)
    except HelperError as exc:
        print(f"Error: {exc}", file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
