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
import signal
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
PROFILES_DIR = BASE / "profiles"
LAYOUTS_DIR = BASE / "layouts"
STATE_FILE = BASE / "state.json"

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


# ---------------------------------------------------------------------------
# AppleScript senders
# ---------------------------------------------------------------------------
def _osa(script):
    subprocess.run(["osascript", "-e", script], check=False,
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
    parts = [p.strip() for p in str(combo).split("+") if p.strip()]
    if not parts:
        return
    if Q is not None and _needs_quartz(parts):
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


def run_action(spec, ctx):
    """spec is a str (shortcut) or dict {action: ...}."""
    if spec is None:
        return
    if isinstance(spec, str):
        send_shortcut(spec, ctx.get("mouse"))
        return
    if isinstance(spec, dict):
        action = spec.get("action", "shortcut")
        if action == "shortcut":
            send_shortcut(spec.get("keys", ""), ctx.get("mouse"))
        elif action == "text":
            type_text(spec.get("value", ""))
        elif action == "shell":
            subprocess.Popen(spec.get("cmd", ""), shell=True)
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
        except Exception:
            pass
    return {"active": "default"}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def profile_path(name):
    return PROFILES_DIR / f"{name}.yaml"


def load_profile(name):
    require_yaml()
    p = profile_path(name)
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text()) or {}


def list_profiles():
    return sorted(f.stem for f in PROFILES_DIR.glob("*.yaml"))


def load_layout_for(device_name):
    """Pick the layout whose device_match is a substring of the device name."""
    require_yaml()
    generic = None
    for f in sorted(LAYOUTS_DIR.glob("*.yaml")):
        data = yaml.safe_load(f.read_text()) or {}
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
    except Exception:
        pass
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
    buttons, axis_buttons = {}, {}
    for sdl_name, target in sdl_to_name.items():
        raw = mapping.get(sdl_name)
        if not raw:
            continue
        if raw.startswith("b"):
            buttons[int(raw[1:])] = target
        elif raw.startswith("a"):
            axis_buttons[int(raw[1:])] = target
    axes = {}
    for sdl_axis in ("leftx", "lefty", "rightx", "righty"):
        raw = mapping.get(sdl_axis)
        if raw and raw.startswith("a"):
            axes[sdl_axis] = int(raw[1:])
    layout = {
        "device_match": name,
        "note": "auto-generated from SDL GameController DB (mapper.py automap)",
        "dpad": "buttons",
        "buttons": dict(sorted(buttons.items())),
    }
    if axis_buttons:
        layout["axis_buttons"] = dict(sorted(axis_buttons.items()))
        layout["axis_threshold"] = 0.5
    if axes:
        layout["axes"] = axes
    slug = "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-")
    out = LAYOUTS_DIR / f"{slug}.yaml"
    LAYOUTS_DIR.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(layout, allow_unicode=True, sort_keys=False))
    print(f"已生成布局: {out}")
    print(f"  设备     : {name}")
    print(f"  面键映射 : a/b/x/y 按手柄标签直通")
    print(f"  按钮 {len(buttons)} 个 / 扳机轴 {len(axis_buttons)} 个")


def cmd_run(args):
    pygame = _init_pygame()
    joy = _open_controller(pygame, wait=True)
    name = joy.get_name()
    layout = load_layout_for(name)
    if not layout:
        print(f"找不到匹配 '{name}' 的布局，请先运行: mapper.py calibrate")
        return

    raw_buttons = layout.get("buttons", {}) or {}
    # map: button index -> binding name
    idx_to_name = {int(k): v for k, v in raw_buttons.items()}
    dpad_mode = layout.get("dpad", "hat")
    # analog triggers reported as axes (e.g. Switch ZL/ZR -> a4/a5)
    axis_buttons = {int(k): v for k, v in (layout.get("axis_buttons") or {}).items()}
    axis_threshold = float(layout.get("axis_threshold", 0.5))
    # analog stick axis indices (leftx/lefty/rightx/righty -> axis number)
    axes_map = {k: int(v) for k, v in (layout.get("axes") or {}).items()}

    # Quartz powers stick->mouse and L3/R3 clicks; optional, so everything else
    # still works if pyobjc is unavailable.
    try:
        import Quartz as _Q
    except ImportError:
        _Q = None

    state = {"active": load_state().get("active", "default")}
    bindings = {}
    sticks = {}
    mtimes = {}

    def reload_bindings():
        prof = load_profile(state["active"])
        bindings.clear()
        bindings.update(prof.get("bindings", {}) or {})
        sticks.clear()
        sticks.update(prof.get("sticks", {}) or {})
        mtimes["state"] = STATE_FILE.stat().st_mtime if STATE_FILE.exists() else 0
        pf = profile_path(state["active"])
        mtimes["profile"] = pf.stat().st_mtime if pf.exists() else 0
        print(f"[mapper] 已加载 profile '{state['active']}' "
              f"({len(bindings)} 个绑定)")

    def cycle(direction):
        profs = list_profiles()
        if not profs:
            return
        i = profs.index(state["active"]) if state["active"] in profs else 0
        i = (i + (1 if direction == "profile_next" else -1)) % len(profs)
        state["active"] = profs[i]
        save_state({"active": state["active"]})
        reload_bindings()

    ctx = {"cycle": cycle, "mouse": _Q}
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

    def axval(name):
        idx = axes_map.get(name)
        return joy.get_axis(idx) if idx is not None else 0.0

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
                spec = bindings.get(bname) if bname else None
                if cur and not prev_btn[i]:
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
                prev_btn[i] = cur

            # dpad via hat (rising edge per direction)
            if dpad_mode == "hat" and joy.get_numhats() > 0:
                hat = joy.get_hat(0)
                if hat != prev_hat:
                    hx, hy = hat
                    if hy == 1 and prev_hat[1] != 1:
                        run_action(bindings.get("dpad_up"), ctx)
                    if hy == -1 and prev_hat[1] != -1:
                        run_action(bindings.get("dpad_down"), ctx)
                    if hx == -1 and prev_hat[0] != -1:
                        run_action(bindings.get("dpad_left"), ctx)
                    if hx == 1 and prev_hat[0] != 1:
                        run_action(bindings.get("dpad_right"), ctx)
                    prev_hat = hat

            # analog triggers as buttons (rising edge over threshold)
            for a, aname in axis_buttons.items():
                pressed = joy.get_axis(a) > axis_threshold
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

            time.sleep(0.008)
    except KeyboardInterrupt:
        print("\n[mapper] 已停止。")


def cmd_list(args):
    active = load_state().get("active", "default")
    for p in list_profiles():
        prof = load_profile(p)
        mark = "*" if p == active else " "
        print(f"{mark} {p:<12} {prof.get('description', '')}")


def cmd_switch(args):
    if not profile_path(args.name).exists():
        sys.exit(f"profile 不存在: {args.name} (可用: {', '.join(list_profiles())})")
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
    sub.add_parser("run", help="启动监听（常驻）")
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
