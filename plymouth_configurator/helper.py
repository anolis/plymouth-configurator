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
import configparser
import fcntl
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import tempfile
from pathlib import Path

if __package__:
    from . import bootstrap
else:
    import bootstrap

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
    if not NAME_RE.fullmatch(name) or name in {".", ".."}:
        raise HelperError(f"Invalid theme name: {name!r}")
    return name


def theme_path(name: str) -> Path:
    path = THEME_DIR / validate_name(name)
    if path.is_symlink():
        raise HelperError(f"Refusing a symlinked theme directory: {path}")
    path = path.resolve()
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


def cmd_install_plymouth(args) -> None:
    if args.method == "native":
        bootstrap.install_native(run)
    elif args.method == "source":
        bootstrap.install_source(run)
        log("Source install complete. Configure your distribution's Plymouth initramfs/boot integration before rebooting.")
    else:
        if not args.package:
            raise HelperError("A local binary package is required")
        source = Path(os.path.abspath(args.package))
        bootstrap.binary_install_command(source)  # validate format/manager before copying
        parent_fd = _open_source(source.parent)
        try:
            fd = os.open(source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        with os.fdopen(fd, "rb") as inp:
            info = os.fstat(inp.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise HelperError("The package must be a regular, non-linked file")
            with tempfile.TemporaryDirectory(prefix="plymouth-package-") as work:
                staged = Path(work) / source.name
                with staged.open("xb") as out:
                    shutil.copyfileobj(inp, out)
                env = os.environ.copy()
                env["DEBIAN_FRONTEND"] = "noninteractive"
                run(bootstrap.binary_install_command(staged), env=env)
        if not bootstrap.plymouth_present():
            raise HelperError("Package installed, but Plymouth is still missing; additional Plymouth packages may be needed.")
    log("Plymouth installation finished.")


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


def rewrite_plymouth_file(plymouth_file: Path, dest: Path, source: Path | None = None) -> None:
    """Relocate paths while preserving the staged theme's internal layout."""
    staged = plymouth_file.parent
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.read(plymouth_file, encoding="utf-8")
    if not cp.has_section("Plymouth Theme"):
        raise HelperError(f"Missing [Plymouth Theme] in {plymouth_file.name}")

    def relocate(value: str, key: str) -> Path:
        path = Path(value)
        if path.is_absolute():
            roots = [dest, Path("/usr/share/plymouth/themes") / dest.name,
                     Path("/usr/local/share/plymouth/themes") / dest.name]
            if source is not None:
                roots.insert(0, source)
            for root in roots:
                if path.is_relative_to(root):
                    path = path.relative_to(root)
                    break
            else:
                # Match a relocated download against its actual internal layout,
                # keeping the longest existing suffix instead of flattening it.
                candidates = [Path(*path.parts[i:]) for i in range(1, len(path.parts))]
                if key == "ImageDir" and path.name == dest.name:
                    candidates.append(Path("."))
                path = next((p for p in candidates if
                             ((staged / p).is_dir() if key == "ImageDir" else (staged / p).is_file())), path)
                if path.is_absolute():
                    raise HelperError(f"Cannot locate {key} inside theme: {value}")
        if ".." in path.parts:
            raise HelperError(f"Path escapes theme: {value}")
        candidate = staged / path
        valid = candidate.is_dir() if key == "ImageDir" else candidate.is_file()
        if not valid:
            raise HelperError(f"Missing {key} in theme: {value}")
        return dest / path

    text = plymouth_file.read_text(encoding="utf-8", errors="replace")
    out = []
    for line in text.splitlines():
        m = re.match(r"^(\s*)(ImageDir|ScriptFile)(\s*=\s*)(.*)$", line)
        if m:
            indent, key, sep, value = m.groups()
            value = value.strip()
            line = f"{indent}{key}{sep}{relocate(value, key)}"
        out.append(line)
    plymouth_file.write_text("\n".join(out) + "\n", encoding="utf-8")


def _open_source(path: Path) -> int:
    """Pin each directory component without following a swapped-in symlink."""
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _copy_source(fd: int, dest: Path) -> None:
    """Copy from pinned descriptors; reject links, devices and source swaps."""
    dest.mkdir(mode=0o755)
    dest.chmod(0o755)
    for name in os.listdir(fd):
        if name in {".git", "__pycache__"}:
            continue
        before = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if not (stat.S_ISDIR(before.st_mode) or stat.S_ISREG(before.st_mode)):
            raise HelperError(f"Theme links and special files are not supported: {name}")
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if stat.S_ISDIR(before.st_mode):
            flags |= os.O_DIRECTORY
        child = os.open(name, flags, dir_fd=fd)
        try:
            after = os.fstat(child)
            if (before.st_dev, before.st_ino, before.st_mode) != (after.st_dev, after.st_ino, after.st_mode):
                raise HelperError(f"Theme changed while being copied: {name}")
            if stat.S_ISDIR(after.st_mode):
                _copy_source(child, dest / name)
            else:
                if after.st_nlink != 1:
                    raise HelperError(f"Hard-linked theme files are not supported: {name}")
                with os.fdopen(os.dup(child), "rb") as inp, (dest / name).open("xb") as out:
                    shutil.copyfileobj(inp, out)
                (dest / name).chmod(0o644)
        finally:
            os.close(child)


def cmd_install(args) -> None:
    if getattr(args, "apply", False) and len(args.sources) != 1:
        raise HelperError("Install-and-apply requires exactly one theme")
    THEME_DIR.mkdir(parents=True, exist_ok=True)
    for src in args.sources:
        src = Path(os.path.abspath(src))
        name = validate_name(src.name)
        dest = theme_path(name)
        log(f"Installing '{name}' → {dest}")
        work = Path(tempfile.mkdtemp(prefix=".install-", dir=THEME_DIR))
        keep_backup = False
        try:
            (work / "new").mkdir()
            staged = work / "new" / name
            backup = work / "previous"
            fd = _open_source(src)
            try:
                _copy_source(fd, staged)
            finally:
                os.close(fd)
            plymouth_file_in(staged)
            for metadata in staged.glob("*.plymouth"):
                rewrite_plymouth_file(metadata, dest, src)
            had_previous = dest.exists()
            if had_previous:
                dest.rename(backup)
            try:
                staged.rename(dest)
            except BaseException:
                if had_previous:
                    try:
                        backup.rename(dest)
                    except OSError as exc:
                        keep_backup = True
                        raise HelperError(f"Could not restore previous theme; backup retained at {backup}") from exc
                raise
        finally:
            if not keep_backup:
                shutil.rmtree(work)
        if uses_alternatives():
            run([which("update-alternatives"), "--install",
                 str(THEME_DIR / "default.plymouth"), "default.plymouth",
                 str(plymouth_file_in(dest)), "100"], check=False)
    log("Done.")
    if getattr(args, "apply", False):
        cmd_set_default(argparse.Namespace(name=name, rebuild=args.rebuild))


def active_theme_names() -> set[str]:
    """Protect every theme referenced by the tool, config or alternatives."""
    names = set()
    tool = which("plymouth-set-default-theme")
    if tool:
        result = subprocess.run([tool], capture_output=True, text=True, timeout=10)
        value = result.stdout.strip()
        if result.returncode == 0 and NAME_RE.fullmatch(value):
            names.add(value)
    if PLYMOUTHD_CONF.exists():
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        cp.read(PLYMOUTHD_CONF)
        value = cp.get("Daemon", "Theme", fallback="").strip()
        if value:
            names.add(value)
    default = THEME_DIR / "default.plymouth"
    if default.is_symlink():
        names.add(default.resolve().parent.name)
    return names


def cmd_uninstall(args) -> None:
    active = active_theme_names()
    # Validate the whole request before deleting any theme.
    for name in args.names:
        path = theme_path(name)
        if name in active:
            raise HelperError(f"Cannot remove active theme '{name}'; select another theme first")
        if not path.is_dir():
            raise HelperError(f"Theme '{name}' is not installed")
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

    p = sub.add_parser("install-plymouth", help="install Plymouth itself")
    p.add_argument("--method", choices=("native", "source", "binary"), default="native")
    p.add_argument("--package", help="local binary package for --method binary")
    p.set_defaults(func=cmd_install_plymouth)

    p = sub.add_parser("set-default", help="apply a theme")
    p.add_argument("name")
    p.add_argument("--no-rebuild", dest="rebuild", action="store_false")
    p.set_defaults(func=cmd_set_default)

    p = sub.add_parser("rebuild-initrd", help="regenerate the initramfs")
    p.set_defaults(func=cmd_rebuild_initrd)

    p = sub.add_parser("install", help="copy theme directories into the theme dir")
    p.add_argument("sources", nargs="+")
    p.add_argument("--apply", action="store_true", help="also apply the single installed theme")
    p.add_argument("--no-rebuild", dest="rebuild", action="store_false")
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
        THEME_DIR.mkdir(parents=True, exist_ok=True)
        # Serialize helper instances so applying and removing cannot race.
        with (THEME_DIR / ".configurator.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            args.func(args)
    except (HelperError, OSError, ValueError, configparser.Error, subprocess.SubprocessError) as exc:
        print(f"Error: {exc}", file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
