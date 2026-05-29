import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mapper


class ShortcutValidationTests(unittest.TestCase):
    def test_valid_existing_shortcuts(self):
        self.assertFalse(mapper.parse_shortcut("cmd+shift+[")["requires_quartz"])
        self.assertFalse(mapper.parse_shortcut("ctrl+right")["requires_quartz"])
        self.assertTrue(mapper.parse_shortcut("rctrl+rshift")["requires_quartz"])

    def test_unknown_modifier_is_rejected(self):
        with self.assertRaises(mapper.ShortcutError):
            mapper.parse_shortcut("cmnd+q")

    def test_quartz_required_shortcut_without_quartz_is_rejected(self):
        with self.assertRaises(mapper.ShortcutError):
            mapper.parse_shortcut("rctrl+l", quartz_available=False)

    def test_multiple_main_keys_are_rejected(self):
        with self.assertRaises(mapper.ShortcutError):
            mapper.parse_shortcut("cmd+a+b")


class ProfileValidationTests(unittest.TestCase):
    def setUp(self):
        self.old_profiles = mapper.PROFILES_DIR
        self.old_state = mapper.STATE_FILE
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        mapper.PROFILES_DIR = base / "profiles"
        mapper.STATE_FILE = base / "state.json"
        mapper.PROFILES_DIR.mkdir()

    def tearDown(self):
        mapper.PROFILES_DIR = self.old_profiles
        mapper.STATE_FILE = self.old_state
        self.tmp.cleanup()

    def write_profile(self, name, text):
        path = mapper.PROFILES_DIR / f"{name}.yaml"
        path.write_text(text)
        return path

    def test_invalid_shortcut_rejects_profile(self):
        self.write_profile("bad", "bindings:\n  A: cmnd+q\n")
        with self.assertRaises(mapper.ProfileError):
            mapper.load_profile_checked("bad")

    def test_quartz_unavailable_skips_only_that_binding(self):
        self.write_profile("partial", "bindings:\n  A: return\n  B: rctrl+l\n")
        prof = mapper.load_profile_checked("partial", quartz_available=False)
        self.assertEqual(prof["bindings"], {"A": "return"})
        self.assertIn("B", prof["_warnings"][0])

    def test_reload_keeps_last_known_good_profile_on_bad_yaml(self):
        path = self.write_profile(
            "default",
            "bindings:\n  A: return\nsticks:\n  left:\n    mode: mouse\n",
        )
        bindings, sticks, mtimes = {}, {}, {}
        logs = []
        self.assertTrue(mapper.reload_profile_into(
            "default", bindings, sticks, mtimes, logger=logs.append))
        self.assertEqual(bindings, {"A": "return"})

        path.write_text("bindings:\n  A: [unterminated\n")
        self.assertFalse(mapper.reload_profile_into(
            "default", bindings, sticks, mtimes,
            errors_seen={}, logger=logs.append))
        self.assertEqual(bindings, {"A": "return"})
        self.assertIn("继续使用上一份可用配置", logs[-1])

    def test_reload_carries_dpad_modes(self):
        self.write_profile(
            "default",
            "bindings:\n"
            "  dpad_up: up\n"
            "dpad_modes:\n"
            "  toggle: [L, R]\n"
            "  repeat:\n"
            "    delay: 0.2\n"
            "    interval: 0.05\n"
            "  alternate:\n"
            "    dpad_up: ctrl+tab\n",
        )
        bindings, sticks, mtimes, options = {}, {}, {}, {}
        self.assertTrue(mapper.reload_profile_into(
            "default", bindings, sticks, mtimes, logger=lambda _: None,
            options=options))
        self.assertEqual(bindings["dpad_up"], "up")
        self.assertEqual(options["dpad_modes"]["alternate"]["dpad_up"], "ctrl+tab")
        self.assertEqual(
            mapper.dpad_repeat_config(options),
            {"delay": 0.2, "interval": 0.05},
        )

    def test_invalid_dpad_repeat_rejects_profile(self):
        self.write_profile(
            "bad_repeat",
            "bindings:\n"
            "  dpad_up: up\n"
            "dpad_modes:\n"
            "  repeat:\n"
            "    delay: nope\n",
        )
        with self.assertRaises(mapper.ProfileError):
            mapper.load_profile_checked("bad_repeat")


class DpadModeTests(unittest.TestCase):
    def test_dpad_mode_spec_defaults_to_bindings(self):
        self.assertEqual(
            mapper.dpad_mode_spec({}, {"dpad_up": "up"}, "default", "dpad_up"),
            "up",
        )

    def test_dpad_mode_spec_uses_alternate_mode(self):
        options = {
            "dpad_modes": {
                "alternate": {
                    "dpad_right": "ctrl+tab",
                }
            }
        }
        self.assertEqual(
            mapper.dpad_mode_spec(options, {}, "alternate", "dpad_right"),
            "ctrl+tab",
        )

    def test_dpad_repeat_config_defaults(self):
        self.assertEqual(
            mapper.dpad_repeat_config({}),
            {"delay": 0.35, "interval": 0.08},
        )


class AxisStartupFilterTests(unittest.TestCase):
    def test_default_settle_filter_subtracts_stable_center_offset(self):
        filt = mapper.AxisStartupFilter()
        filt.configure(
            {"left": {"mode": "mouse", "settle": 0.5}},
            {"leftx": 0, "lefty": 1},
            now=5.0,
        )

        self.assertEqual(filt.apply("lefty", 0.12, now=5.2), 0.0)
        self.assertEqual(filt.apply("lefty", 0.12, now=5.6), 0.0)

    def test_center_offset_can_be_disabled(self):
        filt = mapper.AxisStartupFilter()
        filt.configure(
            {"left": {"mode": "mouse", "settle": 0.5, "center_max": 0}},
            {"leftx": 0, "lefty": 1},
            now=5.0,
        )

        self.assertEqual(filt.apply("lefty", 0.12, now=5.2), 0.0)
        self.assertEqual(filt.apply("lefty", 0.12, now=5.6), 0.12)

    def test_suppresses_axes_until_settled_then_subtracts_center_offset(self):
        filt = mapper.AxisStartupFilter()
        filt.configure(
            {"left": {"mode": "mouse", "settle": 0.5, "center_max": 0.3}},
            {"leftx": 0, "lefty": 1},
            now=10.0,
        )

        self.assertEqual(filt.apply("lefty", 0.22, now=10.2), 0.0)
        self.assertEqual(filt.apply("lefty", 0.24, now=10.4), 0.0)
        self.assertAlmostEqual(filt.apply("lefty", 0.22, now=10.6), -0.01)
        self.assertAlmostEqual(filt.apply("lefty", 0.30, now=10.7), 0.07)

    def test_center_offset_uses_settle_window_median(self):
        filt = mapper.AxisStartupFilter()
        filt.configure(
            {"right": {"mode": "scroll", "settle": 0.5, "center_max": 0.3}},
            {"rightx": 2, "righty": 3},
            now=30.0,
        )

        for value in (0.04, 0.05, 0.90, 0.04, 0.05):
            self.assertEqual(filt.apply("rightx", value, now=30.2), 0.0)
        self.assertAlmostEqual(filt.apply("rightx", 0.07, now=30.6), 0.02)

    def test_large_initial_axis_value_is_not_treated_as_center(self):
        filt = mapper.AxisStartupFilter()
        filt.configure(
            {"right": {"mode": "scroll", "settle": 0.1, "center_max": 0.3}},
            {"rightx": 2, "righty": 3},
            now=20.0,
        )

        self.assertEqual(filt.apply("righty", 0.8, now=20.2), 0.8)

    def test_unstable_initial_axis_values_are_not_treated_as_center(self):
        filt = mapper.AxisStartupFilter()
        filt.configure(
            {
                "left": {
                    "mode": "mouse",
                    "settle": 0.5,
                    "center_max": 0.35,
                    "center_stability": 0.05,
                }
            },
            {"leftx": 0, "lefty": 1},
            now=40.0,
        )

        for value in (-0.25, 0.25, -0.25, 0.25):
            self.assertEqual(filt.apply("lefty", value, now=40.2), 0.0)
        self.assertEqual(filt.apply("lefty", 0.25, now=40.6), 0.25)

    def test_recenters_after_strong_movement_returns_to_stable_drift(self):
        filt = mapper.AxisStartupFilter()
        filt.configure(
            {
                "left": {
                    "mode": "mouse",
                    "settle": 0.1,
                    "center_max": 0.35,
                    "center_stability": 0.05,
                    "recenter_after": 0.2,
                    "active_threshold": 0.55,
                }
            },
            {"leftx": 0, "lefty": 1},
            now=50.0,
        )

        self.assertEqual(filt.apply("lefty", 0.06, now=50.05), 0.0)
        self.assertEqual(filt.apply("lefty", 0.06, now=50.2), 0.0)
        self.assertGreater(filt.apply("lefty", 0.9, now=50.3), 0.55)
        self.assertAlmostEqual(filt.apply("lefty", 0.25, now=50.4), 0.19)
        self.assertAlmostEqual(filt.apply("lefty", 0.25, now=50.5), 0.19)
        self.assertEqual(filt.apply("lefty", 0.25, now=50.7), 0.0)


class SdlMappingParserTests(unittest.TestCase):
    def test_parse_buttons_axes_and_hats(self):
        self.assertEqual(
            mapper.parse_sdl_mapping_token("b0"),
            {"kind": "button", "index": 0},
        )
        self.assertEqual(
            mapper.parse_sdl_mapping_token("a4"),
            {"kind": "axis", "index": 4, "sign": 1},
        )
        self.assertEqual(
            mapper.parse_sdl_mapping_token("+a2"),
            {"kind": "axis", "index": 2, "sign": 1},
        )
        self.assertEqual(
            mapper.parse_sdl_mapping_token("-a2"),
            {"kind": "axis", "index": 2, "sign": -1},
        )
        self.assertEqual(
            mapper.parse_sdl_mapping_token("h0.1"),
            {"kind": "hat", "index": 0, "value": 1},
        )

    def test_unsupported_token_is_rejected(self):
        with self.assertRaises(mapper.SdlMappingError):
            mapper.parse_sdl_mapping_token("x1")


class ShellActionTests(unittest.TestCase):
    def test_shell_action_disabled_by_default(self):
        with mock.patch("mapper.subprocess.Popen") as popen:
            mapper.run_shell_action(
                {"action": "shell", "argv": ["echo", "hello"]},
                {"allow_shell_actions": False},
            )
        popen.assert_not_called()

    def test_shell_action_uses_argv_without_shell(self):
        with mock.patch("mapper.subprocess.Popen") as popen:
            mapper.run_shell_action(
                {"action": "shell", "argv": ["echo", "hello"]},
                {"allow_shell_actions": True},
            )
        popen.assert_called_once_with(["echo", "hello"])

    def test_legacy_cmd_is_split_without_shell(self):
        with mock.patch("mapper.subprocess.Popen") as popen:
            mapper.run_shell_action(
                {"action": "shell", "cmd": "echo 'hello world'"},
                {"allow_shell_actions": True},
            )
        popen.assert_called_once_with(["echo", "hello world"])


if __name__ == "__main__":
    unittest.main()
