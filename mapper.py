#!/usr/bin/env python3
"""gamepad-mapper — map a game controller's buttons to macOS keyboard shortcuts.

Single-file CLI. Subcommands: run, list, switch, reset, calibrate, status, probe.
Mappings live in profiles/*.yaml and hot-reload while `run` is active, so an
agent (Claude / Codex) or you can edit a profile and see it take effect live.

Layer 1 (now): button -> keyboard shortcut, via AppleScript (System Events).
Layer 2 (later): the same dispatch supports action: shell / claude for agent hooks.
"""

import argparse
import json
import os
import re
import signal
import shlex
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
PROFILES_DIR = BASE / "profiles"
LAYOUTS_DIR = BASE / "layouts"
STATE_FILE = BASE / "state.json"
OSASCRIPT = "/usr/bin/osascript"

try:
    import yaml
except ImportError:
    yaml = None


# ---------------------------------------------------------------------------
# Key tables (AppleScript / System Events)
# ---------------------------------------------------------------------------
MODIFIERS = {
    "cmd": "command down", "command": "command down", "win": "command down",
    "shift": "shift down",
    "alt": "option down", "option": "option down", "opt": "option down",
    "ctrl": "control down", "control": "control down",
    "fn": "function down",
}

# named key -> macOS virtual key code
KEYCODES = {
    "return": 36, "enter": 76, "tab": 48, "space": 49,
    "delete": 51, "backspace": 51, "forwarddelete": 117,
    "escape": 53, "esc": 53,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
    "capslock": 57,
}

# Quartz path (precise virtual key codes) — needed for left/right-specific
# modifiers (rctrl/rshift/...) and pure-modifier "keys" (e.g. a bare right ctrl),
# neither of which AppleScript's `keystroke` can express. Requires Accessibility.
CHAR_KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7,
    "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16,
    "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23,
    "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29, "]": 30,
    "o": 31, "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38,
    "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45,
    "m": 46, ".": 47, "`": 50,
    "return": 36, "enter": 76, "tab": 48, "space": 49, "delete": 51,
    "backspace": 51, "escape": 53, "esc": 53, "left": 123, "right": 124, "down": 125,
    "up": 126, "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}

# modifier token -> virtual key code (distinguishes left/right)
MOD_KEYCODES = {
    "cmd": 55, "command": 55, "lcmd": 55, "win": 55, "rcmd": 54, "rcommand": 54,
    "shift": 56, "lshift": 56, "rshift": 60,
    "alt": 58, "option": 58, "opt": 58, "lalt": 58, "loption": 58,
    "ralt": 61, "roption": 61,
    "ctrl": 59, "control": 59, "lctrl": 59, "lcontrol": 59,
    "rctrl": 62, "rcontrol": 62,
}

# modifier keycode -> CGEvent flag mask
MOD_FLAGS = {
    55: 1 << 20, 54: 1 << 20,   # command
    56: 1 << 17, 60: 1 << 17,   # shift
    58: 1 << 19, 61: 1 << 19,   # option / alt
    59: 1 << 18, 62: 1 << 18,   # control
}

# tokens that force the precise-keycode (Quartz) path
_SIDE_MODS = {"rcmd", "rcommand", "lcmd", "rshift", "lshift",
              "ralt", "roption", "lalt", "loption",
              "rctrl", "rcontrol", "lctrl", "lcontrol"}

# Single keys AppleScript delivers unreliably; send these via Quartz instead.
# (Escape is the known case; safe because it's a plain key, unrelated to the
# Space-switching that makes Quartz fail for ctrl+arrow.)
_FORCE_QUARTZ_KEYS = {"escape", "esc"}


# ---------------------------------------------------------------------------
# Factory default — `reset` rewrites profiles/default.yaml from this, so a bad
# edit can never leave the system unusable.
# ---------------------------------------------------------------------------
FACTORY_DEFAULT = {
    "name": "default",
    "description": "出厂默认映射 (factory default)",
    "bindings": {
        "A": "return",
        "B": "escape",
        "X": "cmd+c",
        "Y": "cmd+v",
        "L": "cmd+[",
        "R": "cmd+]",
        "ZL": "cmd+shift+[",
        "ZR": "cmd+shift+]",
        "Minus": "cmd+tab",
        "Plus": "cmd+space",
        "dpad_up": "up",
        "dpad_down": "down",
        "dpad_left": "left",
        "dpad_right": "right",
        "Home": {"action": "none"},
        "Capture": {"action": "none"},
    },
}


def require_yaml():
    if yaml is None:
        sys.exit("PyYAML not installed. Run inside the project venv: "
                 ".venv/bin/python mapper.py ...")


class MapperError(Exception):
    """Base exception for user-fixable mapper configuration errors."""


class ShortcutError(MapperError):
    """Raised when a shortcut cannot be parsed safely."""


class QuartzUnavailableError(ShortcutError):
    """Raised when a valid shortcut needs Quartz but Quartz is unavailable."""


class ProfileError(MapperError):
    """Raised when a profile cannot be loaded or validated."""


class LayoutError(MapperError):
    """Raised when a layout cannot be loaded or validated."""


class SdlMappingError(MapperError):
    """Raised when an SDL mapping token is unsupported."""


def _warn(message):
    print(f"[mapper] 警告: {message}", file=sys.stderr)


def _env_enabled(name):
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _file_mtime(path):
    return path.stat().st_mtime if path.exists() else 0


def _load_yaml_file(path, label):
    require_yaml()
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception as e:
        raise ProfileError(f"{label} YAML 无法解析: {path}: {e}") from e
    if not isinstance(data, dict):
        raise ProfileError(f"{label} 顶层必须是 mapping: {path}")
    return data


# ---------------------------------------------------------------------------
# AppleScript senders
# ---------------------------------------------------------------------------
def _osa(script):
    subprocess.run([OSASCRIPT, "-e", script], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _needs_quartz(parts):
    """Use Quartz ONLY for what AppleScript can't express: left/right-specific
    modifiers and pure-modifier 'keys'. Everything else stays on AppleScript —
    Quartz-injected ctrl+arrow does NOT switch Spaces, while AppleScript does.
    """
    low = [p.lower() for p in parts]
    if any(p in _SIDE_MODS for p in low):
        return True
    if low and all(p in MOD_KEYCODES for p in low):
        return True
    if len(low) == 1 and low[0] in _FORCE_QUARTZ_KEYS:
        return True
    return False


def _is_modifier_token(token):
    return token.lower() in MODIFIERS or token.lower() in MOD_KEYCODES


def _is_key_token(token):
    low = token.lower()
    return low in KEYCODES or low in CHAR_KEYCODES or len(token) == 1


def parse_shortcut(combo, quartz_available=True):
    """Validate and classify a shortcut without silently dropping tokens."""
    parts = [p.strip() for p in str(combo).split("+") if p.strip()]
    if not parts:
        raise ShortcutError("快捷键不能为空")

    unknown = [p for p in parts if not _is_modifier_token(p) and not _is_key_token(p)]
    if unknown:
        raise ShortcutError(f"未知快捷键 token: {', '.join(unknown)}")

    non_mods = [p for p in parts if not _is_modifier_token(p)]
    pure_modifier = not non_mods
    if len(non_mods) > 1:
        raise ShortcutError(f"快捷键只能有一个主键: {combo}")
    if pure_modifier and not all(p.lower() in MOD_KEYCODES for p in parts):
        raise ShortcutError(f"纯修饰键只支持 Quartz 精确键码: {combo}")

    requires_quartz = _needs_quartz(parts)
    if requires_quartz and not quartz_available:
        raise QuartzUnavailableError(f"需要 Quartz/pyobjc 才能发送: {combo}")

    return {
        "parts": parts,
        "requires_quartz": requires_quartz,
        "pure_modifier": pure_modifier,
    }


def send_keycombo_quartz(Q, combo):
    """Send by exact virtual key code; supports L/R-specific and pure modifiers."""
    parts = [p.strip() for p in str(combo).split("+") if p.strip()]
    mods, main = [], None
    for p in parts:
        low = p.lower()
        if low in MOD_KEYCODES:
            mods.append(MOD_KEYCODES[low])
        elif low in CHAR_KEYCODES:
            main = CHAR_KEYCODES[low]
        elif len(p) == 1:
            main = CHAR_KEYCODES.get(p)
    tap = Q.kCGHIDEventTap

    def post(kc, down, flags):
        ev = Q.CGEventCreateKeyboardEvent(None, kc, down)
        Q.CGEventSetFlags(ev, flags)
        Q.CGEventPost(tap, ev)

    # Press modifiers while accumulating the flag mask, so every event reflects
    # all modifiers currently held. Needed for combos like rctrl+rshift, where
    # leaving flags unset made only the first modifier register.
    cur = 0
    for kc in mods:
        cur |= MOD_FLAGS.get(kc, 0)
        post(kc, True, cur)
    if main is not None:
        post(main, True, cur)
        post(main, False, cur)
    else:
        # pure-modifier chord (e.g. rctrl+rshift): hold briefly so a listener
        # such as typeless can see both modifiers held at once before release.
        time.sleep(0.04)
    # Release in reverse, clearing each flag as its key goes up.
    for kc in reversed(mods):
        cur &= ~MOD_FLAGS.get(kc, 0)
        post(kc, False, cur)


def send_shortcut(combo, Q=None):
    """combo like 'cmd+shift+4', 'return', 'rctrl+l', 'rctrl' (pure modifier)."""
    parsed = parse_shortcut(combo, quartz_available=Q is not None)
    parts = parsed["parts"]
    if parsed["requires_quartz"]:
        send_keycombo_quartz(Q, combo)
        return
    mods = [MODIFIERS[p.lower()] for p in parts[:-1] if p.lower() in MODIFIERS]
    main = parts[-1]
    low = main.lower()
    if low in KEYCODES:
        action = f"key code {KEYCODES[low]}"
    else:
        ch = main.replace("\\", "\\\\").replace('"', '\\"')
        action = f'keystroke "{ch}"'
    if mods:
        action += " using {" + ", ".join(mods) + "}"
    _osa(f'tell application "System Events" to {action}')


def type_text(text):
    ch = str(text).replace("\\", "\\\\").replace('"', '\\"')
    _osa(f'tell application "System Events" to keystroke "{ch}"')


def mouse_pos(Q):
    p = Q.CGEventGetLocation(Q.CGEventCreate(None))
    return p.x, p.y


def mouse_move(Q, x, y):
    # CGWarpMouseCursorPosition works without Accessibility permission,
    # so stick-driven cursor movement is usable out of the box.
    Q.CGWarpMouseCursorPosition((x, y))


def mouse_click(Q, button="left"):
    x, y = mouse_pos(Q)
    if button == "right":
        down, up, b = (Q.kCGEventRightMouseDown, Q.kCGEventRightMouseUp,
                       Q.kCGMouseButtonRight)
    else:
        down, up, b = (Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp,
                       Q.kCGMouseButtonLeft)
    for ev in (down, up):
        Q.CGEventPost(Q.kCGHIDEventTap,
                      Q.CGEventCreateMouseEvent(None, ev, (x, y), b))


def mouse_scroll(Q, dx=0, dy=0):
    """Scroll by pixel deltas. Positive dx scrolls right; positive dy scrolls up."""
    dx, dy = int(round(dx)), int(round(dy))
    if not dx and not dy:
        return
    ev = Q.CGEventCreateScrollWheelEvent(
        None, Q.kCGScrollEventUnitPixel, 2, dy, dx)
    Q.CGEventPost(Q.kCGHIDEventTap, ev)


def _validate_repeat(spec, where):
    if "repeat" not in spec:
        return
    repeat = spec["repeat"]
    if isinstance(repeat, bool):
        return
    if not isinstance(repeat, dict):
        raise ProfileError(f"{where}: repeat 必须是 bool 或 mapping")
    for key in ("delay", "interval"):
        if key in repeat:
            try:
                value = float(repeat[key])
            except (TypeError, ValueError) as e:
                raise ProfileError(f"{where}: repeat.{key} 必须是数字") from e
            if value <= 0:
                raise ProfileError(f"{where}: repeat.{key} 必须大于 0")


def _validate_timing_mapping(spec, where, keys):
    if not isinstance(spec, dict):
        raise ProfileError(f"{where} 必须是 mapping")
    for key in keys:
        if key in spec:
            try:
                value = float(spec[key])
            except (TypeError, ValueError) as e:
                raise ProfileError(f"{where}.{key} 必须是数字") from e
            if value <= 0:
                raise ProfileError(f"{where}.{key} 必须大于 0")


def _validate_shell_spec(spec, where):
    argv = spec.get("argv")
    cmd = spec.get("cmd")
    if argv is None and cmd is None:
        raise ProfileError(f"{where}: shell action 需要 argv 或 cmd")
    if argv is not None:
        if (not isinstance(argv, list) or not argv or
                not all(isinstance(v, str) and v for v in argv)):
            raise ProfileError(f"{where}: shell argv 必须是非空字符串列表")
    if cmd is not None and (not isinstance(cmd, str) or not cmd.strip()):
        raise ProfileError(f"{where}: shell cmd 必须是非空字符串")


def validate_action(spec, profile_name="<profile>", button_name="<button>",
                    quartz_available=True):
    where = f"profile '{profile_name}' binding '{button_name}'"
    if isinstance(spec, str):
        parse_shortcut(spec, quartz_available=quartz_available)
        return
    if not isinstance(spec, dict):
        raise ProfileError(f"{where}: binding 必须是字符串或 mapping")

    action = spec.get("action", "shortcut")
    if not isinstance(action, str):
        raise ProfileError(f"{where}: action 必须是字符串")

    if action == "shortcut":
        parse_shortcut(spec.get("keys", ""), quartz_available=quartz_available)
    elif action == "text":
        if "value" not in spec:
            raise ProfileError(f"{where}: text action 需要 value")
    elif action == "shell":
        _validate_shell_spec(spec, where)
    elif action in ("profile_next", "profile_prev", "mouse_click",
                    "mouse_rightclick", "none"):
        pass
    else:
        raise ProfileError(f"{where}: 未知 action: {action}")

    _validate_repeat(spec, where)


def validate_profile(profile, profile_name, quartz_available=True):
    if not isinstance(profile, dict):
        raise ProfileError(f"profile '{profile_name}' 顶层必须是 mapping")
    bindings = profile.get("bindings", {}) or {}
    if not isinstance(bindings, dict):
        raise ProfileError(f"profile '{profile_name}' 的 bindings 必须是 mapping")
    cleaned = dict(profile)
    cleaned_bindings = dict(bindings)
    errors = []
    warnings = []
    for button, spec in bindings.items():
        try:
            validate_action(spec, profile_name, button, quartz_available)
        except QuartzUnavailableError as e:
            cleaned_bindings.pop(button, None)
            warnings.append(f"profile '{profile_name}' binding '{button}' 已跳过: {e}")
        except ProfileError as e:
            errors.append(str(e))
        except ShortcutError as e:
            errors.append(f"profile '{profile_name}' binding '{button}': {e}")

    sticks = profile.get("sticks", {}) or {}
    if not isinstance(sticks, dict):
        errors.append(f"profile '{profile_name}' 的 sticks 必须是 mapping")
    else:
        for stick_name, stick_spec in sticks.items():
            if not isinstance(stick_spec, dict):
                errors.append(f"profile '{profile_name}' stick '{stick_name}' 必须是 mapping")
                continue
            for key in ("speed", "deadzone", "threshold", "repeat",
                        "deadzone_x", "deadzone_y",
                        "settle", "center_max", "center_stability",
                        "recenter_after", "active_threshold"):
                if key in stick_spec:
                    try:
                        float(stick_spec[key])
                    except (TypeError, ValueError):
                        errors.append(
                            f"profile '{profile_name}' stick '{stick_name}.{key}' 必须是数字")

    dpad_modes = profile.get("dpad_modes", {}) or {}
    if not isinstance(dpad_modes, dict):
        errors.append(f"profile '{profile_name}' 的 dpad_modes 必须是 mapping")
    else:
        toggle = dpad_modes.get("toggle", ["L", "R"])
        if (not isinstance(toggle, list) or len(toggle) != 2 or
                not all(isinstance(v, str) and v for v in toggle)):
            errors.append(f"profile '{profile_name}' 的 dpad_modes.toggle 必须是两个按键名")
        if "repeat" in dpad_modes:
            try:
                _validate_timing_mapping(
                    dpad_modes["repeat"],
                    f"profile '{profile_name}' 的 dpad_modes.repeat",
                    ("delay", "interval"),
                )
            except ProfileError as e:
                errors.append(str(e))
        for mode_name in ("default", "alternate"):
            mode = dpad_modes.get(mode_name, {}) or {}
            if not isinstance(mode, dict):
                errors.append(f"profile '{profile_name}' 的 dpad_modes.{mode_name} 必须是 mapping")
                continue
            for button, spec in mode.items():
                if button not in ("dpad_up", "dpad_down", "dpad_left", "dpad_right"):
                    errors.append(
                        f"profile '{profile_name}' 的 dpad_modes.{mode_name}.{button} 不是方向键")
                    continue
                try:
                    validate_action(spec, profile_name,
                                    f"dpad_modes.{mode_name}.{button}",
                                    quartz_available)
                except QuartzUnavailableError as e:
                    warnings.append(
                        f"profile '{profile_name}' dpad_modes.{mode_name}.{button} 已跳过: {e}")
                except ProfileError as e:
                    errors.append(str(e))
                except ShortcutError as e:
                    errors.append(
                        f"profile '{profile_name}' dpad_modes.{mode_name}.{button}: {e}")

    if errors:
        raise ProfileError("; ".join(errors))
    cleaned["bindings"] = cleaned_bindings
    if warnings:
        cleaned["_warnings"] = warnings
    return cleaned


def _shell_argv(spec):
    if spec.get("argv") is not None:
        return list(spec["argv"])
    return shlex.split(spec.get("cmd", ""))


def run_shell_action(spec, ctx):
    if not ctx.get("allow_shell_actions", False):
        _warn("shell action 已禁用；使用 --allow-shell-actions 或 "
              "GAMEPAD_MAPPER_ALLOW_SHELL=1 显式启用")
        return
    try:
        argv = _shell_argv(spec)
        if not argv:
            _warn("shell action argv/cmd 为空，已跳过")
            return
        subprocess.Popen(argv)
    except (OSError, ValueError) as e:
        _warn(f"shell action 启动失败: {e}")


DIRECTION_SHORTCUTS = {
    "dpad_up": "up",
    "dpad_down": "down",
    "dpad_left": "left",
    "dpad_right": "right",
}


def dpad_mode_spec(options, bindings, mode_name, dpad_name):
    modes = options.get("dpad_modes", {}) if options else {}
    mode = modes.get(mode_name, {}) if isinstance(modes, dict) else {}
    if isinstance(mode, dict) and dpad_name in mode:
        return mode[dpad_name]
    if mode_name == "default" and dpad_name in bindings:
        return bindings.get(dpad_name)
    return DIRECTION_SHORTCUTS.get(dpad_name)


def dpad_repeat_config(options):
    modes = options.get("dpad_modes", {}) if options else {}
    repeat = modes.get("repeat", {}) if isinstance(modes, dict) else {}
    if not isinstance(repeat, dict):
        repeat = {}
    return {
        "delay": float(repeat.get("delay", 0.35)),
        "interval": float(repeat.get("interval", 0.08)),
    }


STICK_AXES = {
    "left": ("leftx", "lefty"),
    "right": ("rightx", "righty"),
}


def _median(values):
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _stable_center(samples, center_max, stability):
    if not samples:
        return None
    center = _median(samples)
    if abs(center) > center_max:
        return None
    near = sum(1 for value in samples if abs(value - center) <= stability)
    if near / len(samples) < 0.7:
        return None
    return center


def _axis_debug_enabled():
    return _env_enabled("GAMEPAD_MAPPER_DEBUG_AXIS")


def _debug_axis(message):
    if _axis_debug_enabled():
        print(message, flush=True)


class AxisStartupFilter:
    """Suppress startup stick noise, then subtract small center offsets."""

    def __init__(self):
        self.axes = {}

    def configure(self, sticks, axes_map, now=None):
        now = time.time() if now is None else now
        self.axes.clear()
        for stick_name, axis_names in STICK_AXES.items():
            spec = sticks.get(stick_name)
            if not isinstance(spec, dict) or not spec.get("mode"):
                continue
            settle = float(spec.get("settle", 0.8))
            center_max = float(spec.get("center_max", 0.35))
            center_stability = float(spec.get("center_stability", 0.12))
            recenter_after = float(spec.get("recenter_after", 0.6))
            active_threshold = float(spec.get("active_threshold", 0.55))
            for axis_name in axis_names:
                if axis_name in axes_map:
                    self.axes[axis_name] = {
                        "ready_at": now + settle,
                        "center_max": center_max,
                        "center_stability": center_stability,
                        "recenter_after": recenter_after,
                        "active_threshold": active_threshold,
                        "offset": None,
                        "samples": [],
                        "recenter_armed": False,
                        "recenter_start": None,
                        "recenter_samples": [],
                    }

    def apply(self, axis_name, value, now=None):
        state = self.axes.get(axis_name)
        if state is None:
            return value
        now = time.time() if now is None else now
        if now < state["ready_at"]:
            state["samples"].append(value)
            return 0.0
        if state["offset"] is None:
            samples = state["samples"] or [value]
            center = _stable_center(
                samples, state["center_max"], state["center_stability"])
            state["offset"] = center if center is not None else 0.0
            state["samples"] = []
            _debug_axis(
                f"[mapper] axis center {axis_name}: "
                f"offset={state['offset']:+.4f} samples={len(samples)}"
            )
        value -= state["offset"]
        if abs(value) >= state["active_threshold"]:
            state["recenter_armed"] = True
            state["recenter_start"] = None
            state["recenter_samples"] = []
        elif state["recenter_armed"] and abs(value + state["offset"]) <= state["center_max"]:
            if state["recenter_start"] is None:
                state["recenter_start"] = now
                state["recenter_samples"] = []
            state["recenter_samples"].append(value + state["offset"])
            if now - state["recenter_start"] >= state["recenter_after"]:
                center = _stable_center(
                    state["recenter_samples"],
                    state["center_max"],
                    state["center_stability"],
                )
                if center is not None:
                    raw_value = value + state["offset"]
                    state["offset"] = center
                    value = raw_value - state["offset"]
                    _debug_axis(
                        f"[mapper] axis recenter {axis_name}: "
                        f"offset={state['offset']:+.4f} "
                        f"samples={len(state['recenter_samples'])}"
                    )
                state["recenter_armed"] = False
                state["recenter_start"] = None
                state["recenter_samples"] = []
        else:
            state["recenter_start"] = None
            state["recenter_samples"] = []
        return max(-1.0, min(1.0, value))


def run_action(spec, ctx):
    """spec is a str (shortcut) or dict {action: ...}."""
    if spec is None:
        return
    if isinstance(spec, str):
        try:
            send_shortcut(spec, ctx.get("mouse"))
        except ShortcutError as e:
            _warn(str(e))
        return
    if isinstance(spec, dict):
        action = spec.get("action", "shortcut")
        if action == "shortcut":
            try:
                send_shortcut(spec.get("keys", ""), ctx.get("mouse"))
            except ShortcutError as e:
                _warn(str(e))
        elif action == "text":
            type_text(spec.get("value", ""))
        elif action == "shell":
            run_shell_action(spec, ctx)
        elif action in ("profile_next", "profile_prev"):
            ctx["cycle"](action)
        elif action == "mouse_click":
            if ctx.get("mouse") is not None:
                mouse_click(ctx["mouse"], "left")
        elif action == "mouse_rightclick":
            if ctx.get("mouse") is not None:
                mouse_click(ctx["mouse"], "right")
        elif action == "none":
            pass


# ---------------------------------------------------------------------------
# State / profiles / layouts
# ---------------------------------------------------------------------------
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (OSError, json.JSONDecodeError) as e:
            _warn(f"state.json 无法读取，使用 default: {e}")
    return {"active": "default"}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def profile_path(name):
    return PROFILES_DIR / f"{name}.yaml"


def load_profile(name):
    return _load_yaml_file(profile_path(name), "profile")


def load_profile_checked(name, quartz_available=True):
    return validate_profile(load_profile(name), name, quartz_available)


def reload_profile_into(name, bindings, sticks, mtimes, quartz_available=True,
                        errors_seen=None, logger=print, options=None):
    pf = profile_path(name)
    state_mtime = _file_mtime(STATE_FILE)
    profile_mtime = _file_mtime(pf)
    try:
        prof = load_profile_checked(name, quartz_available=quartz_available)
    except ProfileError as e:
        mtimes["state"] = state_mtime
        mtimes["profile"] = profile_mtime
        error_key = (str(pf), profile_mtime, str(e))
        if errors_seen is None or errors_seen.get(str(pf)) != error_key:
            if errors_seen is not None:
                errors_seen[str(pf)] = error_key
            logger(f"[mapper] profile '{name}' 未加载，继续使用上一份可用配置: {e}")
        return False

    bindings.clear()
    bindings.update(prof.get("bindings", {}) or {})
    sticks.clear()
    sticks.update(prof.get("sticks", {}) or {})
    if options is not None:
        options.clear()
        options.update({
            "dpad_modes": prof.get("dpad_modes", {}) or {},
        })
    mtimes["state"] = state_mtime
    mtimes["profile"] = profile_mtime
    if errors_seen is not None:
        errors_seen.pop(str(pf), None)
    for warning in prof.get("_warnings", []):
        logger(f"[mapper] 警告: {warning}")
    logger(f"[mapper] 已加载 profile '{name}' ({len(bindings)} 个绑定)")
    return True


def list_profiles():
    return sorted(f.stem for f in PROFILES_DIR.glob("*.yaml"))


def load_layout_for(device_name):
    """Pick the layout whose device_match is a substring of the device name."""
    require_yaml()
    generic = None
    for f in sorted(LAYOUTS_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(f.read_text()) or {}
        except Exception as e:
            _warn(f"布局 YAML 无法解析，已跳过 {f}: {e}")
            continue
        if not isinstance(data, dict):
            _warn(f"布局顶层必须是 mapping，已跳过 {f}")
            continue
        match = data.get("device_match", "")
        if match and match.lower() in device_name.lower():
            return data
        if f.stem == "generic":
            generic = data
    return generic or {}


# ---------------------------------------------------------------------------
# pygame helpers
# ---------------------------------------------------------------------------
def _init_pygame():
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    os.environ.setdefault("SDL_JOYSTICK_HIDAPI", "1")
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    # let Python handle SIGINT (Ctrl-C) instead of SDL swallowing it
    os.environ.setdefault("SDL_NO_SIGNAL_HANDLERS", "1")
    import pygame
    pygame.init()
    pygame.joystick.init()
    return pygame


def _open_controller(pygame, wait=True):
    """Return an initialised joystick, optionally waiting for one to connect."""
    while True:
        pygame.event.pump()
        if pygame.joystick.get_count() > 0:
            joy = pygame.joystick.Joystick(0)
            joy.init()
            return joy
        if not wait:
            return None
        print("等待手柄连接... (Ctrl-C 取消)", end="\r", flush=True)
        time.sleep(0.5)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_probe(args):
    """Print raw device info — used to build/verify a layout."""
    pygame = _init_pygame()
    joy = _open_controller(pygame, wait=False)
    if joy is None:
        print("没有检测到手柄。请确认手柄已连接（USB 或蓝牙）。")
        return
    print(f"设备名     : {joy.get_name()}")
    try:
        print(f"GUID       : {joy.get_guid()}")
    except Exception as e:
        print(f"GUID       : 无法读取 ({e})")
    print(f"按键数     : {joy.get_numbuttons()}")
    print(f"摇杆轴数   : {joy.get_numaxes()}")
    print(f"方向键(hat): {joy.get_numhats()}")
    print("\n现在按手柄上的任意键，看它对应的编号（10 秒，Ctrl-C 结束）...")
    n = joy.get_numbuttons()
    prev = [joy.get_button(i) for i in range(n)]
    end = time.time() + 10
    try:
        while time.time() < end:
            pygame.event.pump()
            for i in range(n):
                cur = joy.get_button(i)
                if cur and not prev[i]:
                    print(f"  button {i} 按下")
                    end = time.time() + 10
                prev[i] = cur
            for h in range(joy.get_numhats()):
                hx, hy = joy.get_hat(h)
                if (hx, hy) != (0, 0):
                    print(f"  hat {h} = {(hx, hy)}")
                    end = time.time() + 10
            time.sleep(0.02)
    except KeyboardInterrupt:
        pass


def cmd_calibrate(args):
    """Interactively learn this controller's button indices -> Nintendo names."""
    pygame = _init_pygame()
    joy = _open_controller(pygame, wait=True)
    print(f"\n校准手柄: {joy.get_name()}")
    print("依次按下提示的按键。每个键有 8 秒，不按则跳过。\n")
    sequence = ["A", "B", "X", "Y", "L", "R", "ZL", "ZR",
                "Minus", "Plus", "L3", "R3", "Home", "Capture"]
    n = joy.get_numbuttons()
    buttons = {}
    prev = [joy.get_button(i) for i in range(n)]
    for label in sequence:
        print(f"  按下 [{label}] ...", end="", flush=True)
        idx = None
        end = time.time() + 8
        while time.time() < end and idx is None:
            pygame.event.pump()
            for i in range(n):
                cur = joy.get_button(i)
                if cur and not prev[i] and i not in buttons:
                    idx = i
                prev[i] = cur
            time.sleep(0.01)
        if idx is None:
            print(" 跳过")
        else:
            buttons[idx] = label
            print(f" -> button {idx}")

    # dpad detection (8s)
    print("  拨动方向键 (上下左右) ...", end="", flush=True)
    hats = joy.get_numhats() > 0
    layout = {
        "device_match": joy.get_name(),
        "note": "generated by `mapper.py calibrate`",
        "buttons": {i: name for i, name in buttons.items()},
    }
    if hats:
        layout["dpad"] = "hat"
        print(" 使用 hat (自动)")
    else:
        # try to capture dpad as buttons
        dpad = {}
        for label in ["dpad_up", "dpad_down", "dpad_left", "dpad_right"]:
            end = time.time() + 4
            idx = None
            while time.time() < end and idx is None:
                pygame.event.pump()
                for i in range(n):
                    cur = joy.get_button(i)
                    if cur and not prev[i] and i not in buttons and i not in dpad.values():
                        idx = i
                    prev[i] = cur
                time.sleep(0.01)
            if idx is not None:
                dpad[label] = idx
        layout["dpad"] = "buttons"
        layout["buttons"].update({v: k for k, v in dpad.items()})
        print(" 使用 buttons")

    slug = "".join(c if c.isalnum() else "-" for c in joy.get_name().lower()).strip("-")
    out = LAYOUTS_DIR / f"{slug}.yaml"
    require_yaml()
    out.write_text(yaml.safe_dump(layout, allow_unicode=True, sort_keys=False))
    print(f"\n已写入布局: {out}")


def parse_sdl_mapping_token(raw):
    """Parse a compact SDL GameController mapping token."""
    if raw is None:
        raise SdlMappingError("empty token")
    token = str(raw).strip()
    if not token:
        raise SdlMappingError("empty token")

    m = re.fullmatch(r"b(\d+)", token)
    if m:
        return {"kind": "button", "index": int(m.group(1))}

    m = re.fullmatch(r"([+-]?)a(\d+)", token)
    if m:
        sign = -1 if m.group(1) == "-" else 1
        return {"kind": "axis", "index": int(m.group(2)), "sign": sign}

    m = re.fullmatch(r"h(\d+)\.(\d+)", token)
    if m:
        return {"kind": "hat", "index": int(m.group(1)), "value": int(m.group(2))}

    raise SdlMappingError(f"unsupported SDL mapping token: {token}")


def cmd_automap(args):
    """Auto-generate a layout from SDL's GameController DB (no key presses)."""
    require_yaml()
    pygame = _init_pygame()
    from pygame._sdl2 import controller
    controller.init()
    if controller.get_count() == 0:
        print("SDL 不认识当前手柄，无法自动映射。请改用: mapper.py calibrate")
        return
    c = controller.Controller(0)
    name = c.name
    mapping = c.get_mapping()
    # SDL names face buttons by position (south=a, east=b). Nintendo controllers
    # print swapped labels (south=B, east=A), so swap to match the printed label.
    # SDL (with its default button-label hint) already reports a/b/x/y by the
    # label PRINTED on the pad — verified on Switch Pro — so map straight through.
    # An earlier "Nintendo swap" here double-flipped all four face buttons.
    face = {"a": "A", "b": "B", "x": "X", "y": "Y"}
    sdl_to_name = dict(face, **{
        "back": "Minus", "guide": "Home", "start": "Plus",
        "leftstick": "L3", "rightstick": "R3",
        "leftshoulder": "L", "rightshoulder": "R",
        "dpup": "dpad_up", "dpdown": "dpad_down",
        "dpleft": "dpad_left", "dpright": "dpad_right",
        "misc1": "Capture",
        "lefttrigger": "ZL", "righttrigger": "ZR",
    })
    buttons, axis_buttons, axis_button_dirs = {}, {}, {}
    dpad_hat = None
    warnings = []
    for sdl_name, target in sdl_to_name.items():
        raw = mapping.get(sdl_name)
        if not raw:
            continue
        try:
            token = parse_sdl_mapping_token(raw)
        except SdlMappingError as e:
            warnings.append(f"{sdl_name}={raw}: {e}")
            continue
        if token["kind"] == "button":
            buttons[token["index"]] = target
        elif token["kind"] == "axis":
            axis_buttons[token["index"]] = target
            if token["sign"] != 1:
                axis_button_dirs[token["index"]] = token["sign"]
        elif token["kind"] == "hat" and sdl_name.startswith("dp"):
            if dpad_hat is None:
                dpad_hat = token["index"]
            elif dpad_hat != token["index"]:
                warnings.append(f"{sdl_name}={raw}: 多个 hat index 暂不支持")
        else:
            warnings.append(f"{sdl_name}={raw}: 该目标暂不支持 hat 映射")

    axes, axis_signs = {}, {}
    for sdl_axis in ("leftx", "lefty", "rightx", "righty"):
        raw = mapping.get(sdl_axis)
        if not raw:
            continue
        try:
            token = parse_sdl_mapping_token(raw)
        except SdlMappingError as e:
            warnings.append(f"{sdl_axis}={raw}: {e}")
            continue
        if token["kind"] != "axis":
            warnings.append(f"{sdl_axis}={raw}: 摇杆轴需要 axis token")
            continue
        axes[sdl_axis] = token["index"]
        if token["sign"] != 1:
            axis_signs[sdl_axis] = token["sign"]

    layout = {
        "device_match": name,
        "note": "auto-generated from SDL GameController DB (mapper.py automap)",
        "dpad": "hat" if dpad_hat is not None else "buttons",
        "buttons": dict(sorted(buttons.items())),
    }
    if dpad_hat is not None:
        layout["hat"] = dpad_hat
    if axis_buttons:
        layout["axis_buttons"] = dict(sorted(axis_buttons.items()))
        layout["axis_threshold"] = 0.5
    if axis_button_dirs:
        layout["axis_button_dirs"] = dict(sorted(axis_button_dirs.items()))
    if axes:
        layout["axes"] = axes
    if axis_signs:
        layout["axis_signs"] = axis_signs
    slug = "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-")
    out = LAYOUTS_DIR / f"{slug}.yaml"
    LAYOUTS_DIR.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(layout, allow_unicode=True, sort_keys=False))
    print(f"已生成布局: {out}")
    print(f"  设备     : {name}")
    print(f"  面键映射 : a/b/x/y 按手柄标签直通")
    print(f"  按钮 {len(buttons)} 个 / 扳机轴 {len(axis_buttons)} 个")
    for warning in warnings:
        print(f"  警告     : {warning}")


def cmd_run(args):
    pygame = _init_pygame()
    joy = _open_controller(pygame, wait=True)
    name = joy.get_name()
    layout = load_layout_for(name)
    if not layout:
        print(f"找不到匹配 '{name}' 的布局，请先运行: mapper.py calibrate")
        return

    try:
        raw_buttons = layout.get("buttons", {}) or {}
        # map: button index -> binding name
        idx_to_name = {int(k): v for k, v in raw_buttons.items()}
        dpad_mode = layout.get("dpad", "hat")
        dpad_hat = int(layout.get("hat", 0))
        # analog triggers reported as axes (e.g. Switch ZL/ZR -> a4/a5)
        axis_buttons = {int(k): v for k, v in (layout.get("axis_buttons") or {}).items()}
        axis_button_dirs = {
            int(k): int(v) for k, v in (layout.get("axis_button_dirs") or {}).items()
        }
        axis_threshold = float(layout.get("axis_threshold", 0.5))
        # analog stick axis indices (leftx/lefty/rightx/righty -> axis number)
        axes_map = {k: int(v) for k, v in (layout.get("axes") or {}).items()}
        axis_signs = {k: int(v) for k, v in (layout.get("axis_signs") or {}).items()}
    except (TypeError, ValueError) as e:
        print(f"布局格式错误，请重新运行 automap/calibrate: {e}")
        return

    # Quartz powers stick->mouse, clicks, and exact keycodes. Bindings that need
    # Quartz are skipped if pyobjc is unavailable; ordinary AppleScript
    # shortcuts still work.
    try:
        import Quartz as _Q
    except ImportError:
        _Q = None

    state = {"active": load_state().get("active", "default")}
    bindings = {}
    sticks = {}
    profile_options = {}
    axis_filter = AxisStartupFilter()
    mtimes = {}
    errors_seen = {}

    def reload_bindings():
        ok = reload_profile_into(
            state["active"], bindings, sticks, mtimes,
            quartz_available=_Q is not None,
            errors_seen=errors_seen,
            options=profile_options,
        )
        if ok:
            axis_filter.configure(sticks, axes_map)
        return ok

    def cycle(direction):
        profs = list_profiles()
        if not profs:
            return
        i = profs.index(state["active"]) if state["active"] in profs else 0
        i = (i + (1 if direction == "profile_next" else -1)) % len(profs)
        state["active"] = profs[i]
        save_state({"active": state["active"]})
        reload_bindings()

    ctx = {
        "cycle": cycle,
        "mouse": _Q,
        "allow_shell_actions": (
            args.allow_shell_actions or _env_enabled("GAMEPAD_MAPPER_ALLOW_SHELL")
        ),
    }
    reload_bindings()
    print(f"[mapper] 监听中: {name}. 改 profile 会自动热重载。Ctrl-C 退出。")

    n = joy.get_numbuttons()
    prev_btn = [0] * n
    btn_press_t = [0.0] * n
    btn_last_fire = [0.0] * n
    prev_hat = (0, 0)
    prev_axis = {a: False for a in axis_buttons}
    last_check = 0.0
    prev_loop = time.time()
    rstick = {"dir": None, "t": 0.0}   # right-stick-as-dpad repeat state
    scroll_accum = {"x": 0.0, "y": 0.0}
    dpad_state = {"mode": "default"}
    hat_press_t = {}
    hat_last_fire = {}
    combo_pending = {}
    combo_window = 0.12

    def axval(name):
        idx = axes_map.get(name)
        sign = axis_signs.get(name, 1)
        if idx is None:
            return 0.0
        return axis_filter.apply(name, joy.get_axis(idx) * sign)

    def dpad_spec(dpad_name):
        return dpad_mode_spec(
            profile_options, bindings, dpad_state["mode"], dpad_name)

    def dpad_toggle_combo():
        modes = profile_options.get("dpad_modes", {}) or {}
        return tuple(modes.get("toggle", ["L", "R"]))

    def toggle_dpad_mode():
        dpad_state["mode"] = (
            "alternate" if dpad_state["mode"] == "default" else "default"
        )
        print(f"[mapper] 方向键模式: {dpad_state['mode']}")

    # register AFTER pygame.init so this overrides SDL's own signal handlers
    stop = {"flag": False}

    def _request_stop(*_):
        stop["flag"] = True

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    try:
        while not stop["flag"]:
            pygame.event.pump()
            now = time.time()
            dt = now - prev_loop
            prev_loop = now

            # hot reload (every ~0.5s)
            if now - last_check > 0.5:
                last_check = now
                if STATE_FILE.exists() and STATE_FILE.stat().st_mtime != mtimes.get("state"):
                    state["active"] = load_state().get("active", "default")
                    reload_bindings()
                pf = profile_path(state["active"])
                if pf.exists() and pf.stat().st_mtime != mtimes.get("profile"):
                    reload_bindings()

            # buttons: rising edge, plus optional hold-to-repeat
            for i in range(n):
                cur = joy.get_button(i)
                bname = idx_to_name.get(i)
                if bname in DIRECTION_SHORTCUTS:
                    spec = dpad_spec(bname)
                else:
                    spec = bindings.get(bname) if bname else None
                if cur and not prev_btn[i]:
                    combo = dpad_toggle_combo()
                    if bname in combo:
                        other = combo[1] if bname == combo[0] else combo[0]
                        other_pending = combo_pending.get(other)
                        if (other_pending is not None and
                                now - other_pending["t"] <= combo_window):
                            combo_pending.pop(other, None)
                            toggle_dpad_mode()
                        else:
                            combo_pending[bname] = {
                                "idx": i,
                                "spec": spec,
                                "t": now,
                            }
                        btn_press_t[i] = now
                        btn_last_fire[i] = now
                        prev_btn[i] = cur
                        continue
                    if spec is not None:
                        run_action(spec, ctx)
                    btn_press_t[i] = now
                    btn_last_fire[i] = now
                elif cur and isinstance(spec, dict) and spec.get("repeat"):
                    rp = spec["repeat"]
                    delay = rp.get("delay", 0.35) if isinstance(rp, dict) else 0.35
                    interval = rp.get("interval", 0.045) if isinstance(rp, dict) else 0.045
                    if now - btn_press_t[i] >= delay and now - btn_last_fire[i] >= interval:
                        run_action(spec, ctx)
                        btn_last_fire[i] = now
                elif cur and bname in DIRECTION_SHORTCUTS:
                    repeat = dpad_repeat_config(profile_options)
                    if (now - btn_press_t[i] >= repeat["delay"] and
                            now - btn_last_fire[i] >= repeat["interval"]):
                        run_action(spec, ctx)
                        btn_last_fire[i] = now
                prev_btn[i] = cur

            for bname, pending in list(combo_pending.items()):
                idx = pending["idx"]
                if now - pending["t"] >= combo_window or not prev_btn[idx]:
                    if pending["spec"] is not None:
                        run_action(pending["spec"], ctx)
                    combo_pending.pop(bname, None)

            # dpad via hat (rising edge per direction)
            if dpad_mode == "hat" and joy.get_numhats() > dpad_hat:
                hat = joy.get_hat(dpad_hat)
                hx, hy = hat
                hat_dirs = {
                    "dpad_up": hy == 1,
                    "dpad_down": hy == -1,
                    "dpad_left": hx == -1,
                    "dpad_right": hx == 1,
                }
                prev_hat_dirs = {
                    "dpad_up": prev_hat[1] == 1,
                    "dpad_down": prev_hat[1] == -1,
                    "dpad_left": prev_hat[0] == -1,
                    "dpad_right": prev_hat[0] == 1,
                }
                repeat = dpad_repeat_config(profile_options)
                for dpad_name, pressed in hat_dirs.items():
                    if pressed and not prev_hat_dirs[dpad_name]:
                        run_action(dpad_spec(dpad_name), ctx)
                        hat_press_t[dpad_name] = now
                        hat_last_fire[dpad_name] = now
                    elif pressed:
                        if (now - hat_press_t.get(dpad_name, now) >= repeat["delay"] and
                                now - hat_last_fire.get(dpad_name, 0) >= repeat["interval"]):
                            run_action(dpad_spec(dpad_name), ctx)
                            hat_last_fire[dpad_name] = now
                    else:
                        hat_press_t.pop(dpad_name, None)
                        hat_last_fire.pop(dpad_name, None)
                prev_hat = hat

            # analog triggers as buttons (rising edge over threshold)
            for a, aname in axis_buttons.items():
                direction = axis_button_dirs.get(a, 1)
                pressed = joy.get_axis(a) * direction > axis_threshold
                if pressed and not prev_axis[a]:
                    if aname in bindings:
                        run_action(bindings[aname], ctx)
                prev_axis[a] = pressed

            # left stick -> mouse cursor (warp; no Accessibility permission needed)
            ls = sticks.get("left")
            if ls and ls.get("mode") == "mouse" and _Q is not None and axes_map:
                dz = float(ls.get("deadzone", 0.15))
                speed = float(ls.get("speed", 900))
                lx, ly = axval("leftx"), axval("lefty")
                if abs(lx) < dz:
                    lx = 0.0
                if abs(ly) < dz:
                    ly = 0.0
                if lx or ly:
                    cx, cy = mouse_pos(_Q)
                    mouse_move(_Q, cx + lx * speed * dt, cy + ly * speed * dt)

            # right stick -> arrow keys (by direction, with auto-repeat)
            rs = sticks.get("right")
            if rs and rs.get("mode") == "dpad" and axes_map:
                thr = float(rs.get("threshold", 0.6))
                rep = float(rs.get("repeat", 0.13))
                rx, ry = axval("rightx"), axval("righty")
                direction = None
                if max(abs(rx), abs(ry)) > thr:
                    if abs(rx) >= abs(ry):
                        direction = "right" if rx > 0 else "left"
                    else:
                        direction = "down" if ry > 0 else "up"
                if direction:
                    if direction != rstick["dir"] or now - rstick["t"] >= rep:
                        send_shortcut(direction, _Q)
                        rstick["dir"] = direction
                        rstick["t"] = now
                else:
                    rstick["dir"] = None
            elif rs and rs.get("mode") == "scroll" and _Q is not None and axes_map:
                dz = float(rs.get("deadzone", 0.18))
                dz_x = float(rs.get("deadzone_x", dz))
                dz_y = float(rs.get("deadzone_y", dz))
                speed = float(rs.get("speed", 900))
                rx, ry = axval("rightx"), axval("righty")
                if rs.get("horizontal") is False:
                    rx = 0.0
                if rs.get("vertical") is False:
                    ry = 0.0
                if abs(rx) < dz_x:
                    rx = 0.0
                if abs(ry) < dz_y:
                    ry = 0.0
                if rs.get("invert_x"):
                    rx = -rx
                if rs.get("invert_y"):
                    ry = -ry
                if rx or ry:
                    scroll_accum["x"] += rx * speed * dt
                    scroll_accum["y"] += -ry * speed * dt
                    dx, dy = int(scroll_accum["x"]), int(scroll_accum["y"])
                    if dx or dy:
                        mouse_scroll(_Q, dx, dy)
                        scroll_accum["x"] -= dx
                        scroll_accum["y"] -= dy
                else:
                    scroll_accum["x"] = 0.0
                    scroll_accum["y"] = 0.0

            time.sleep(0.008)
    except KeyboardInterrupt:
        print("\n[mapper] 已停止。")


def cmd_list(args):
    active = load_state().get("active", "default")
    for p in list_profiles():
        try:
            prof = load_profile_checked(p, quartz_available=True)
            desc = prof.get("description", "")
        except ProfileError as e:
            desc = f"(invalid: {e})"
        mark = "*" if p == active else " "
        print(f"{mark} {p:<12} {desc}")


def cmd_switch(args):
    if not profile_path(args.name).exists():
        sys.exit(f"profile 不存在: {args.name} (可用: {', '.join(list_profiles())})")
    try:
        load_profile_checked(args.name, quartz_available=True)
    except ProfileError as e:
        sys.exit(f"profile 无效，未切换: {args.name}: {e}")
    save_state({"active": args.name})
    print(f"已切换到 profile '{args.name}'（若 mapper 正在运行会自动生效）")


def cmd_reset(args):
    require_yaml()
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    profile_path("default").write_text(
        yaml.safe_dump(FACTORY_DEFAULT, allow_unicode=True, sort_keys=False))
    save_state({"active": "default"})
    print("已恢复出厂设置：default.yaml 重写为工厂默认，并激活 default。")


def cmd_status(args):
    active = load_state().get("active", "default")
    print(f"当前 profile : {active}")
    print(f"可用 profile : {', '.join(list_profiles()) or '(无)'}")
    try:
        pygame = _init_pygame()
        joy = _open_controller(pygame, wait=False)
        if joy:
            name = joy.get_name()
            layout = load_layout_for(name)
            print(f"手柄连接     : 是 — {name}")
            print(f"匹配布局     : {'是' if layout else '否（需 calibrate）'}")
        else:
            print("手柄连接     : 否")
    except Exception as e:
        print(f"手柄检测失败 : {e}")


def main():
    parser = argparse.ArgumentParser(prog="mapper.py", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="启动监听（常驻）")
    run.add_argument("--allow-shell-actions", action="store_true",
                     help="允许 profile 执行 shell action（默认禁用）")
    sub.add_parser("list", help="列出所有 profile")
    sw = sub.add_parser("switch", help="切换 profile")
    sw.add_argument("name")
    sub.add_parser("reset", help="恢复出厂默认")
    sub.add_parser("calibrate", help="校准手柄按键布局 (手动按键)")
    sub.add_parser("automap", help="从 SDL 数据库自动生成布局 (无需按键)")
    sub.add_parser("status", help="查看当前状态")
    sub.add_parser("probe", help="打印手柄原始信息（调试）")

    args = parser.parse_args()
    {
        "run": cmd_run, "list": cmd_list, "switch": cmd_switch,
        "reset": cmd_reset, "calibrate": cmd_calibrate,
        "automap": cmd_automap,
        "status": cmd_status, "probe": cmd_probe,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
