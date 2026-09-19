# Plymouth Configurator

A desktop app for managing [Plymouth](https://www.freedesktop.org/wiki/Software/Plymouth/)
boot splash themes. It shows every installed theme in a grid with a live,
animated preview rendered from the theme's own frames, and lets you apply,
live-preview, install and remove themes without touching the terminal.

**Project page:** https://anolis.github.io/plymouth-configurator/

![Main window](screenshots/main.png)

## Features

- **Use a personal picture as the boot screen.** Choose **Install → Use a
  picture…**, preview it, choose fill or fit, and apply it. PNG, JPEG, WebP,
  BMP and TIFF are supported. Generated themes keep password prompts,
  questions and boot messages visible over a dark panel.
- **Plymouth setup prompt.** If the client or daemon is missing, the app
  offers to install Plymouth with the distribution's package manager.
  Failed or unavailable package-manager installs offer an explicit choice
  of a local binary package or an upstream source build.
- **Grid of animated previews.** Each card paints the theme's background
  gradient (parsed from its `.script` or `.plymouth`), its background image
  and its animation frames. Hover a card to play the animation.
- **Details sidebar** with description, author comment, module, location,
  frame count and size on disk.
- **Set as boot theme.** Runs `plymouth-set-default-theme`, registers the
  theme with `update-alternatives` on Debian-based systems, and rebuilds the
  initramfs with whichever tool the distribution uses (`update-initramfs`,
  `mkinitcpio` or `dracut`). The rebuild can be turned off in the menu.
- **Live preview.** Runs `plymouthd` on your current display for a few
  seconds using `plymouth.splash=<theme>`, so you can watch the real thing
  without changing the default. Needs Plymouth's x11 renderer
  (`plymouth-x11` on Debian/Ubuntu).
- **Install themes** from a folder, from a zip/tar archive, or straight from
  the [adi1090x/plymouth-themes](https://github.com/adi1090x/plymouth-themes)
  collection (80+ themes). A picker shows previews of everything found and
  lets you install several at once. `.plymouth` paths are rewritten to the
  install location.
- **Drag and drop.** Drop a theme folder, a folder full of themes, or a
  zip / tar archive onto the window and the picker opens with everything
  found already selected.
- **Uninstall** themes you no longer want (the active theme is protected).
- Search, keyboard shortcuts (`Ctrl+F`, `F5`, `Ctrl+O`), and a
  responsive layout that collapses the sidebar on narrow windows.

All privileged operations go through a command-line helper
(`plymouth_configurator/helper.py`) launched with `pkexec`, so you get a
normal polkit password prompt and only those specific actions run as root.

## Requirements

- Python 3.11+
- GTK 4.10+ and libadwaita 1.5+ with PyGObject
- Pillow
- polkit (`pkexec`) for privileged actions
- Plymouth itself; optionally `plymouth-x11` for live previews
- `zstd` to import `.tar.zst` archives

Debian / Ubuntu:

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-pil plymouth plymouth-x11 policykit-1
```

Arch:

```bash
sudo pacman -S python-gobject gtk4 libadwaita python-pillow plymouth polkit
```

Fedora:

```bash
sudo dnf install python3-gobject gtk4 libadwaita python3-pillow plymouth plymouth-plugin-script polkit
```

## Running

```bash
git clone https://github.com/anolis/plymouth-configurator.git
cd plymouth-configurator
./plymouth-configurator          # run from the checkout
make install                     # optional: launcher + app-menu entry in ~/.local
```

## How it applies a theme

1. `plymouth-set-default-theme <name>` writes `Theme=<name>` to
   `/etc/plymouth/plymouthd.conf` (or the helper writes it directly if the
   tool is missing).
2. On systems that manage `default.plymouth` through `update-alternatives`,
   the theme is registered and selected there too.
3. The initramfs is regenerated so the new splash is available at boot.

On a newly configured system, the distribution may also require enabling
Plymouth in its initramfs configuration and adding `splash` to the kernel
command line. This app does not edit the bootloader configuration.

## Installing Plymouth from the app

The startup prompt and **Install Plymouth** banner check for both `plymouth`
and `plymouthd`. Native installation prefers the manager matching
`/etc/os-release`: APT, DNF/YUM, pacman, Zypper, APK or XBPS. Installation
requires administrator authentication. Dismissing the prompt leaves the
banner available for later.

If that fails, you can retry, inspect the error, select a trusted `.deb`,
`.rpm` or Arch `.pkg.tar.*` file, or explicitly confirm a source install.
Binary packages must match the detected manager; dependencies are resolved
by that manager using its normal verification policies.

The source fallback clones release `24.004.60` from the
[official Plymouth repository](https://gitlab.freedesktop.org/plymouth/plymouth),
builds it with Meson, and installs under `/usr`. Git, a C compiler, Meson,
Ninja, pkg-config and the development libraries required by Plymouth must
already be present. Source installation runs with administrator rights,
is not tracked by the package manager, and still requires distro-specific
boot integration. Native packages are the preferred route. Immutable
OSTree systems require their distribution's installation workflow.

## Theme installation safety

Archives are extracted to unique private directories. Absolute paths,
parent traversal, links and special files are rejected. Theme folders also
reject symbolic links, hard links and special files; supply a self-contained
folder with regular files instead. Descriptor-based copying prevents
swapping a source path for a symlink during installation.

Each theme is copied and validated before replacing an existing installation.
Failed replacements restore the previous theme; if restoration itself fails,
the error identifies the retained backup directory. Internal script/image
paths are preserved. Batch installations commit one theme at a time.
The helper serializes its operations and refuses to remove a theme referenced
by the current configuration, default-theme tool or alternatives link.
Malformed themes are logged and skipped when browsing.

## Manual use of the helper

Everything the GUI does with elevated rights can also be run by hand:

```bash
sudo python3 plymouth_configurator/helper.py set-default angular
sudo python3 plymouth_configurator/helper.py install ~/plymouth-themes/pack_1/angular
sudo python3 plymouth_configurator/helper.py uninstall angular
sudo python3 plymouth_configurator/helper.py preview angular --seconds 15 --display "$DISPLAY"
sudo python3 plymouth_configurator/helper.py rebuild-initrd
sudo python3 plymouth_configurator/helper.py install-plymouth
```

## Layout

| File | Purpose |
| --- | --- |
| `plymouth_configurator/themes.py` | Theme discovery, `.plymouth` parsing, preview analysis (frames, background, colours) |
| `plymouth_configurator/widgets.py` | `ThemePreview` renderer and `ThemeCard` grid tile |
| `plymouth_configurator/window.py` | Main window, details sidebar, actions |
| `plymouth_configurator/installer.py` | Folder / archive / collection sources and the picker dialog |
| `plymouth_configurator/system.py` | Distro detection, settings, `pkexec` runner |
| `plymouth_configurator/helper.py` | Standalone root helper |
| `plymouth_configurator/archives.py` | Validated archive extraction |
| `plymouth_configurator/bootstrap.py` | Package-manager detection and Plymouth installation |
| `plymouth_configurator/setup.py` | Plymouth setup dialogs and fallback choices |
| `plymouth_configurator/picture_theme.py` | Image conversion and picture theme generation |
| `plymouth_configurator/pictures.py` | Picture selection, preview and apply flow |

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The tests use temporary directories and mocked system commands. They do not
install packages, modify system themes or rebuild the initramfs. Pillow is
required; the zstd extraction test is skipped when `zstd` is unavailable.

## License

MIT. Themes from the adi1090x collection keep their own licenses.
