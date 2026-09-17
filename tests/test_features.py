from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from plymouth_configurator import bootstrap, helper, themes
from plymouth_configurator.picture_theme import create_picture_theme, set_picture_fit


class BootstrapTests(unittest.TestCase):
    def test_distro_manager_is_preferred_over_unrelated_installed_tools(self):
        with patch.object(bootstrap.platform, "freedesktop_os_release", return_value={"ID": "fedora"}), \
                patch.object(bootstrap, "find_tool", side_effect=lambda name: "/usr/bin/" + name), \
                patch.object(Path, "exists", return_value=False):
            plan = bootstrap.native_install_plan()
        self.assertEqual(plan.manager, "dnf")
        self.assertIn("plymouth-plugin-script", plan.command)

    def test_derivatives_follow_id_like(self):
        with patch.object(bootstrap.platform, "freedesktop_os_release", return_value={"ID": "mint", "ID_LIKE": "ubuntu debian"}), \
                patch.object(bootstrap, "find_tool", side_effect=lambda name: "/usr/bin/" + name), \
                patch.object(Path, "exists", return_value=False):
            self.assertEqual(bootstrap.native_install_plan().manager, "apt-get")

    def test_absent_manager_and_immutable_system_return_no_plan(self):
        with patch.object(bootstrap, "find_tool", return_value=None):
            self.assertIsNone(bootstrap.native_install_plan())
        with patch.object(Path, "exists", return_value=True):
            self.assertIsNone(bootstrap.native_install_plan())

    def test_presence_requires_client_and_daemon(self):
        with patch.object(bootstrap, "find_tool", side_effect=lambda name: "/bin/plymouth" if name == "plymouth" else None):
            self.assertFalse(bootstrap.plymouth_present())

    def test_native_installer_verifies_result_and_does_not_silently_build_source(self):
        plan = bootstrap.InstallPlan("apt-get", ("/usr/bin/apt-get", "install", "-y", "plymouth"))
        run = Mock()
        with patch.object(bootstrap, "native_install_plan", return_value=plan), \
                patch.object(bootstrap, "plymouth_present", return_value=False):
            with self.assertRaisesRegex(ValueError, "not both available"):
                bootstrap.install_native(run)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], list(plan.command))
        self.assertEqual(run.call_args.kwargs["env"]["DEBIAN_FRONTEND"], "noninteractive")

    def test_binary_package_must_match_manager(self):
        plan = bootstrap.InstallPlan("apt-get", ("/usr/bin/apt-get",))
        with patch.object(bootstrap, "native_install_plan", return_value=plan):
            self.assertEqual(bootstrap.binary_install_command(Path("/tmp/plymouth.deb"))[-1], "/tmp/plymouth.deb")
            with self.assertRaises(ValueError):
                bootstrap.binary_install_command(Path("/tmp/plymouth.rpm"))

    def test_source_preflight_fails_before_running_commands(self):
        run = Mock()
        with patch.object(bootstrap, "find_tool", return_value=None):
            with self.assertRaisesRegex(ValueError, "Source installation requires"):
                bootstrap.install_source(run)
        run.assert_not_called()

    def test_source_uses_fixed_upstream_release_and_checks_build_before_install(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            if "compile" in command:
                raise RuntimeError("build failed")

        with patch.object(bootstrap, "find_tool", side_effect=lambda name: "/usr/bin/" + name):
            with self.assertRaisesRegex(RuntimeError, "build failed"):
                bootstrap.install_source(run)
        self.assertIn(bootstrap.SOURCE_URL, calls[0])
        self.assertIn(bootstrap.SOURCE_REF, calls[0])
        self.assertFalse(any("install" in cmd for cmd in calls))


class PictureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root / "My photo.png"
        Image.new("RGBA", (80, 40), (255, 0, 0, 128)).save(self.image)

    def test_generated_theme_is_self_contained_and_installable(self):
        theme = create_picture_theme(self.image, self.root / "cache")
        info = themes.load_theme(theme)
        self.assertIsNotNone(info)
        self.assertEqual(info.background_image, theme / "background.png")
        self.assertIsNone(info.static_image)
        self.assertEqual(info.frames, [])
        with Image.open(info.background_image) as image:
            self.assertEqual(image.mode, "RGB")
            self.assertEqual(image.getpixel((0, 0)), (128, 0, 0))
        installed = self.root / "installed"
        with patch.object(helper, "THEME_DIR", installed), patch.object(helper, "uses_alternatives", return_value=False):
            helper.cmd_install(SimpleNamespace(sources=[str(theme)]))
        config = (installed / theme.name / f"{theme.name}.plymouth").read_text()
        self.assertIn(f"ScriptFile={installed}/{theme.name}/picture.script", config)

    def test_fill_fit_and_password_callbacks(self):
        theme = create_picture_theme(self.image, self.root / "cache")
        script = (theme / "picture.script").read_text()
        self.assertIn("fill_screen = 1;", script)
        self.assertNotIn("@FILL_SCREEN@", script)
        for callback in ("SetDisplayPasswordFunction", "SetDisplayQuestionFunction",
                         "SetDisplayMessageFunction", "SetHideMessageFunction"):
            self.assertIn(callback, script)
        set_picture_fit(theme, False)
        self.assertIn("fill_screen = 0;", (theme / "picture.script").read_text())
        self.assertTrue(themes.load_theme(theme).background_fit)

    def test_exif_orientation_is_applied(self):
        exif = Image.Exif()
        exif[274] = 6
        source = self.root / "portrait.jpg"
        Image.new("RGB", (80, 40)).save(source, exif=exif)
        theme = create_picture_theme(source, self.root / "cache")
        with Image.open(theme / "background.png") as converted:
            self.assertEqual(converted.size, (40, 80))

    def test_invalid_image_cleans_up(self):
        self.image.write_text("not an image")
        with self.assertRaises(OSError):
            create_picture_theme(self.image, self.root / "cache")
        self.assertEqual(list((self.root / "cache").iterdir()), [])

    def test_same_picture_creates_distinct_themes(self):
        first = create_picture_theme(self.image, self.root / "cache")
        second = create_picture_theme(self.image, self.root / "cache")
        self.assertNotEqual(first.name, second.name)
        self.assertTrue(first.is_dir())

    def test_install_and_apply_only_applies_after_successful_install(self):
        theme = create_picture_theme(self.image, self.root / "cache")
        installed = self.root / "installed"

        def apply(args):
            self.assertTrue((installed / args.name / "picture.script").is_file())
            self.assertFalse(args.rebuild)

        with patch.object(helper, "THEME_DIR", installed), \
                patch.object(helper, "uses_alternatives", return_value=False), \
                patch.object(helper, "cmd_set_default", side_effect=apply) as apply_mock:
            helper.cmd_install(SimpleNamespace(sources=[str(theme)], apply=True, rebuild=False))
            apply_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
