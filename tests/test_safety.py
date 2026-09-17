import io
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from plymouth_configurator import archives, helper, themes


class FilesystemTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)


class ArchiveTests(FilesystemTest):
    def tar(self, name, member_name, kind=tarfile.REGTYPE, mode="w"):
        path = self.root / name
        with tarfile.open(path, mode) as out:
            member = tarfile.TarInfo(member_name)
            member.type = kind
            member.linkname = "../outside"
            if kind == tarfile.REGTYPE:
                member.size = 4
                out.addfile(member, io.BytesIO(b"test"))
            else:
                out.addfile(member)
        return path

    def test_tar_variants_reject_traversal_and_clean_up(self):
        for suffix, mode in (("tar", "w"), ("tar.gz", "w:gz"),
                             ("tar.xz", "w:xz"), ("tar.bz2", "w:bz2")):
            with self.subTest(suffix=suffix):
                path = self.tar("bad." + suffix, "../outside", mode=mode)
                with self.assertRaises(ValueError):
                    archives.extract_archive(path, self.root / "cache")
                self.assertEqual(list((self.root / "cache/extract").iterdir()), [])
                self.assertFalse((self.root / "cache/extract/outside").exists())

    def test_tar_rejects_links_and_special_files(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                archives.extract_archive(self.tar("bad.tar", "link", kind), self.root / "cache")

    def test_zip_rejects_absolute_traversal_and_links(self):
        for name in ("../outside", str(self.root / "outside"), "..\\outside", "link"):
            path = self.root / "bad.zip"
            with zipfile.ZipFile(path, "w") as out:
                info = zipfile.ZipInfo(name)
                if name == "link":
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                out.writestr(info, "test")
            with self.subTest(name=name), self.assertRaises(ValueError):
                archives.extract_archive(path, self.root / "cache")
        self.assertFalse((self.root / "outside").exists())

    def test_valid_archives_and_repeated_extraction_are_independent(self):
        tar = self.tar("good.tar", "demo/demo.plymouth")
        zip_path = self.root / "good.zip"
        with zipfile.ZipFile(zip_path, "w") as out:
            out.writestr("demo/demo.plymouth", "test")
        for path in (tar, zip_path):
            first = archives.extract_archive(path, self.root / "cache")
            second = archives.extract_archive(path, self.root / "cache")
            self.assertNotEqual(first, second)
            self.assertEqual((first / "demo/demo.plymouth").read_text(), "test")

    @unittest.skipUnless(shutil.which("zstd"), "zstd not installed")
    def test_zstd_uses_the_same_validation(self):
        for member, valid in (("../outside", False), ("demo/file", True)):
            tar = self.tar("input.tar", member)
            packed = self.root / "input.tar.zst"
            subprocess.run(["zstd", "-q", "-f", str(tar), "-o", str(packed)], check=True)
            if valid:
                dest = archives.extract_archive(packed, self.root / "cache")
                self.assertEqual((dest / member).read_text(), "test")
            else:
                with self.assertRaises(ValueError):
                    archives.extract_archive(packed, self.root / "cache")


class HelperTests(FilesystemTest):
    def setUp(self):
        super().setUp()
        self.installed = self.root / "installed"
        self.installed.mkdir()
        self.conf = self.root / "plymouthd.conf"
        for name, value in (("THEME_DIR", self.installed), ("PLYMOUTHD_CONF", self.conf)):
            patcher = patch.object(helper, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(helper, "which", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def theme(self, parent, name="demo"):
        path = parent / name
        path.mkdir(parents=True)
        (path / f"{name}.plymouth").write_text(
            "[Plymouth Theme]\nName=Demo\nModuleName=script\n"
            "[script]\nScriptFile=scripts/main.script\nImageDir=images\n")
        (path / "scripts").mkdir()
        (path / "scripts/main.script").write_text("original script")
        (path / "images").mkdir()
        return path

    def install(self, source):
        helper.cmd_install(SimpleNamespace(sources=[str(source)]))

    def test_nested_paths_and_installing_from_installed_folder(self):
        source = self.theme(self.root / "input")
        self.install(source)
        dest = self.installed / "demo"
        config = (dest / "demo.plymouth").read_text()
        self.assertIn(f"ScriptFile={dest}/scripts/main.script", config)
        self.assertIn(f"ImageDir={dest}/images", config)
        self.install(dest)
        self.assertEqual((dest / "scripts/main.script").read_text(), "original script")

    def test_absolute_paths_preserve_nested_layout(self):
        source = self.theme(self.root / "input")
        metadata = source / "demo.plymouth"
        metadata.write_text(metadata.read_text().replace("scripts/main.script", "/usr/share/plymouth/themes/demo/scripts/main.script")
                            .replace("ImageDir=images", f"ImageDir={source}/images"))
        self.install(source)
        self.assertIn(f"ScriptFile={self.installed}/demo/scripts/main.script",
                      (self.installed / "demo/demo.plymouth").read_text())

    def test_links_are_rejected_without_replacing_existing_theme(self):
        dest = self.theme(self.installed)
        source = self.theme(self.root / "input")
        external = self.root / "private"
        external.write_text("secret")
        for directory in (False, True):
            link = source / "link"
            link.symlink_to(self.root if directory else external)
            with self.assertRaises(helper.HelperError):
                self.install(source)
            link.unlink()
            self.assertEqual((dest / "scripts/main.script").read_text(), "original script")
            self.assertFalse((dest / "link").exists())

    def test_swap_to_symlink_during_copy_is_rejected(self):
        source = self.theme(self.root / "input")
        victim = source / "swap"
        victim.write_text("safe")
        external = self.root / "external"
        external.write_text("secret")
        real_open = os.open

        def swap(path, flags, *args, **kwargs):
            if path == "swap":
                victim.unlink()
                victim.symlink_to(external)
            return real_open(path, flags, *args, **kwargs)

        with patch.object(helper.os, "open", side_effect=swap), self.assertRaises(OSError):
            self.install(source)
        self.assertFalse((self.installed / "demo").exists())

    def test_copy_failure_preserves_previous_install(self):
        dest = self.theme(self.installed)
        source = self.theme(self.root / "input")
        with patch.object(helper.shutil, "copyfileobj", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.install(source)
        self.assertEqual((dest / "scripts/main.script").read_text(), "original script")
        self.assertEqual(list(self.installed.iterdir()), [dest])

    def test_publish_failure_restores_previous_install(self):
        dest = self.theme(self.installed)
        source = self.theme(self.root / "input")
        rename = Path.rename

        def fail_publish(path, target):
            if path.name == "demo" and path.parent.name == "new":
                raise OSError("publish failed")
            return rename(path, target)

        with patch.object(Path, "rename", fail_publish), self.assertRaises(OSError):
            self.install(source)
        self.assertEqual((dest / "scripts/main.script").read_text(), "original script")

    def test_invalid_metadata_preserves_previous_install(self):
        dest = self.theme(self.installed)
        source = self.theme(self.root / "input")
        (source / "scripts/main.script").unlink()
        with self.assertRaises(helper.HelperError):
            self.install(source)
        self.assertTrue((dest / "scripts/main.script").is_file())

    def test_failed_rollback_retains_backup(self):
        self.theme(self.installed)
        source = self.theme(self.root / "input")
        rename = Path.rename

        def fail(path, target):
            if path.parent.name == "new" or path.name == "previous":
                raise OSError("rename failed")
            return rename(path, target)

        with patch.object(Path, "rename", fail), self.assertRaisesRegex(helper.HelperError, "backup retained"):
            self.install(source)
        backups = list(self.installed.glob(".install-*/previous/scripts/main.script"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "original script")

    def test_hardlinks_and_symlinked_source_roots_are_rejected(self):
        source = self.theme(self.root / "input")
        os.link(source / "scripts/main.script", source / "linked")
        with self.assertRaises(helper.HelperError):
            self.install(source)
        (source / "linked").unlink()
        alias = self.root / "alias"
        alias.symlink_to(source, target_is_directory=True)
        with self.assertRaises(OSError):
            self.install(alias)

    def test_active_theme_reported_only_by_tool_is_protected(self):
        self.theme(self.installed)
        with patch.object(helper, "which", return_value="/usr/sbin/plymouth-set-default-theme"), \
                patch.object(helper.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="demo\n")):
            with self.assertRaises(helper.HelperError):
                helper.cmd_uninstall(SimpleNamespace(names=["demo"]))

    def test_active_theme_protected_by_config_and_alternatives(self):
        active = self.theme(self.installed)
        other = self.theme(self.installed, "other")
        for method in ("config", "alternatives"):
            if method == "config":
                self.conf.write_text("[Daemon]\nTheme=demo\n")
            else:
                self.conf.unlink()
                (self.installed / "default.plymouth").symlink_to(active / "demo.plymouth")
            with self.subTest(method=method), self.assertRaises(helper.HelperError):
                helper.cmd_uninstall(SimpleNamespace(names=["other", "demo"]))
            self.assertTrue(active.is_dir())
            self.assertTrue(other.is_dir())

    def test_inactive_theme_can_be_removed(self):
        dest = self.theme(self.installed)
        helper.cmd_uninstall(SimpleNamespace(names=["demo"]))
        self.assertFalse(dest.exists())


class ScanTests(FilesystemTest):
    def test_malformed_theme_does_not_hide_valid_themes(self):
        for name, content in (("good", "[Plymouth Theme]\nName=Good\n"),
                              ("bad", "invalid without section")):
            directory = self.root / name
            directory.mkdir()
            (directory / f"{name}.plymouth").write_text(content)
        with self.assertLogs("plymouth_configurator.themes", level="WARNING"):
            self.assertEqual([t.name for t in themes.load_theme_dirs(list(self.root.iterdir()))], ["good"])
        with patch.object(themes, "THEME_DIRS", [self.root]):
            with self.assertLogs("plymouth_configurator.themes", level="WARNING"):
                self.assertEqual([t.name for t in themes.scan_installed()], ["good"])


if __name__ == "__main__":
    unittest.main()
