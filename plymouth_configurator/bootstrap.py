"""Plymouth installation plans shared by the GUI and standalone root helper."""

from dataclasses import dataclass
import os
from pathlib import Path
import platform
import shutil
import tempfile

SOURCE_URL = "https://gitlab.freedesktop.org/plymouth/plymouth.git"
SOURCE_REF = "24.004.60"


def find_tool(name: str) -> str | None:
    return shutil.which(name, path=os.environ.get("PATH", "") +
                        ":/usr/sbin:/sbin:/usr/local/sbin:/usr/local/bin")


def plymouth_present() -> bool:
    return bool(find_tool("plymouth") and find_tool("plymouthd"))


@dataclass(frozen=True)
class InstallPlan:
    manager: str
    command: tuple[str, ...]


PACKAGES = {
    "apt-get": ("install", "-y", "plymouth", "plymouth-label", "plymouth-x11"),
    "dnf": ("install", "-y", "plymouth", "plymouth-plugin-script"),
    "yum": ("install", "-y", "plymouth", "plymouth-plugin-script"),
    "pacman": ("-S", "--needed", "--noconfirm", "plymouth"),
    "zypper": ("--non-interactive", "install", "plymouth", "plymouth-plugin-script"),
    "apk": ("add", "plymouth"),
    "xbps-install": ("-S", "-y", "plymouth"),
}
FAMILIES = {
    "debian": ("apt-get",), "ubuntu": ("apt-get",),
    "fedora": ("dnf", "yum"), "rhel": ("dnf", "yum"), "centos": ("dnf", "yum"),
    "arch": ("pacman",), "manjaro": ("pacman",),
    "suse": ("zypper",), "opensuse": ("zypper",), "opensuse-tumbleweed": ("zypper",),
    "alpine": ("apk",), "void": ("xbps-install",),
}


def native_install_plan() -> InstallPlan | None:
    try:
        release = platform.freedesktop_os_release()
    except OSError:
        release = {}
    families = [release.get("ID", "")] + release.get("ID_LIKE", "").split()
    candidates = next((FAMILIES[f] for f in families if f in FAMILIES), tuple(PACKAGES))
    # An immutable host must use its own image/layering workflow.
    if Path("/run/ostree-booted").exists():
        return None
    for manager in candidates:
        tool = find_tool(manager)
        if tool:
            return InstallPlan(manager, (tool, *PACKAGES[manager]))
    return None


def install_native(run) -> None:
    plan = native_install_plan()
    if plan is None:
        raise ValueError("No supported native package manager found. Choose a source or binary package install.")
    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    run(list(plan.command), env=env)
    if not plymouth_present():
        raise ValueError("The package command finished, but plymouth and plymouthd are not both available.")


def install_source(run) -> None:
    required = ("git", "meson", "ninja", "cc", "pkg-config")
    missing = [name for name in required if not find_tool(name)]
    if missing:
        raise ValueError("Source installation requires: " + ", ".join(missing) +
                         ". Install these tools and Plymouth's development dependencies first.")
    # Use a private root-owned checkout and a fixed upstream release, never a
    # caller-supplied build tree or shell command.
    with tempfile.TemporaryDirectory(prefix="plymouth-source-") as work:
        source, build = Path(work) / "source", Path(work) / "build"
        env = os.environ.copy()
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_TERMINAL_PROMPT="0")
        run([find_tool("git"), "clone", "--depth", "1", "--branch", SOURCE_REF,
             "--", SOURCE_URL, str(source)], env=env)
        run([find_tool("meson"), "setup", str(build), str(source), "--prefix=/usr",
             "--sysconfdir=/etc", "--localstatedir=/var", "--wrap-mode=nodownload"], env=env)
        run([find_tool("meson"), "compile", "-C", str(build)], env=env)
        run([find_tool("meson"), "install", "-C", str(build), "--no-rebuild"], env=env)
    if find_tool("ldconfig"):
        run([find_tool("ldconfig")])
    if not plymouth_present():
        raise ValueError("Source installation finished, but Plymouth binaries were not found.")


def binary_install_command(package: Path) -> list[str]:
    """Use the distro's dependency-resolving installer for a local package."""
    plan = native_install_plan()
    manager = plan.manager if plan else None
    filename = package.name.lower()
    if manager == "apt-get" and filename.endswith(".deb"):
        return [plan.command[0], "install", "-y", str(package)]
    if manager in {"dnf", "yum"} and filename.endswith(".rpm"):
        return [plan.command[0], "install", "-y", str(package)]
    if manager == "zypper" and filename.endswith(".rpm"):
        return [plan.command[0], "--non-interactive", "install", str(package)]
    if manager == "pacman" and filename.endswith((".pkg.tar.zst", ".pkg.tar.xz", ".pkg.tar.gz")):
        return [plan.command[0], "-U", "--noconfirm", str(package)]
    raise ValueError("Choose a binary package matching this system (.deb, .rpm or .pkg.tar.*).")
