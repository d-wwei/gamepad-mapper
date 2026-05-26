# Audit Fix Spec

## Goal

Fix the defects found in the code audit while preserving the current single-file CLI and existing profile/layout compatibility.

Primary outcomes:
- No profile edit can silently trigger a weaker or different shortcut than requested.
- A malformed profile/layout cannot crash a running mapper.
- Shell actions are explicit, auditable, and disabled by default.
- `automap` handles common SDL mapping token formats instead of only simple `bN`/`aN`.
- Installation docs match the Python versions that actually work with current dependencies.

## Scope

In scope:
- `mapper.py`
- `README.md`
- Profile/layout validation behavior
- Minimal tests or testable helper functions for parser/validation paths

Out of scope:
- GUI/profile editor
- Background service manager
- Cross-platform support outside macOS
- Rewriting the CLI into a package

## Requirements

### 1. Disable Unsafe Shell Actions By Default

Current issue:
- `run_action()` executes `{action: shell, cmd: "..."}` with `subprocess.Popen(..., shell=True)`.
- Any writable profile can become arbitrary shell execution once a mapped button is pressed.

Required behavior:
- Shell actions must be disabled by default.
- If a shell action is encountered while disabled, mapper must log a clear warning and skip it.
- Enabling shell actions must require explicit runtime opt-in, for example:
  - `./gamepad-mapper run --allow-shell-actions`
  - or `GAMEPAD_MAPPER_ALLOW_SHELL=1 ./gamepad-mapper run`
- Prefer a new safer schema:

```yaml
bindings:
  ZR:
    action: shell
    argv: ["open", "-a", "Terminal"]
```

Compatibility:
- Existing `{action: shell, cmd: "..."}` may remain supported only when shell actions are explicitly enabled.
- If `cmd` is retained, avoid `shell=True` unless a separate stronger opt-in exists, for example `--allow-shell-string`.
- Recommended implementation: support `argv` first; mark `cmd` as legacy in docs.

Acceptance criteria:
- Bandit no longer reports a high-severity `shell=True` issue on the default path.
- Pressing a shell-bound button without opt-in does not execute anything.
- Pressing an `argv` shell action with opt-in executes the expected command.

### 2. Add Strict Shortcut/Profile Validation

Current issue:
- Unknown modifier tokens are silently ignored.
- Example: `cmnd+q` becomes plain `q`; `rctrl+l` without Quartz can become plain `l`.

Required behavior:
- Validate every binding before activating a profile.
- Reject or skip invalid bindings with clear diagnostics that include profile name and button name.
- Unknown shortcut tokens must not silently degrade.
- If a shortcut requires Quartz but Quartz is unavailable, mapper must skip that binding and warn instead of sending a partial shortcut.

Validation rules:
- Shortcut string grammar: `modifier+...+key`, pure modifier chord, or single key.
- Valid modifiers: existing aliases in `MODIFIERS` and `MOD_KEYCODES`.
- Valid keys: `KEYCODES`, `CHAR_KEYCODES`, or a single printable character.
- Unknown tokens are errors.
- Multiple non-modifier keys in one combo are errors.
- Empty strings are errors except `action: none`.

Acceptance criteria:
- `cmnd+q` is reported invalid and never sends `q`.
- `rctrl+l` with no Quartz is reported unavailable and never sends `l`.
- Valid existing profiles load without warnings.

### 3. Make Hot Reload Fault-Tolerant

Current issue:
- A malformed YAML profile throws and can terminate `run`.

Required behavior:
- Hot reload must use "last known good" profile semantics.
- On YAML parse error or validation error:
  - keep the currently active bindings/sticks unchanged;
  - print an error once per file mtime/version;
  - keep the mapper running.
- CLI commands like `list` may still show an error, but should not crash with a traceback.

Acceptance criteria:
- While `run` is active, replacing the active profile with malformed YAML does not stop the process.
- Restoring valid YAML reloads successfully.
- The error message identifies the file and parse/validation problem.

### 4. Parse SDL Mapping Tokens More Completely

Current issue:
- `automap` only recognizes `bN` and `aN`.
- SDL mappings can include hat tokens and axis variants.

Required behavior:
- Introduce a small SDL token parser helper.
- Recognize at least:
  - `bN` buttons
  - `aN` axes
  - `+aN` / `-aN` axis direction tokens
  - `hH.V` hat tokens, where possible
- For unsupported tokens, skip with a warning instead of silently omitting.
- Generated layout must preserve the existing Switch Pro output.

Acceptance criteria:
- Current Bluetooth Switch Pro `automap` output remains byte-equivalent or semantically equivalent.
- Hat-based D-pad mappings can produce a usable layout.
- Unsupported SDL tokens are surfaced in output.

### 5. Correct Python Version / Dependency Documentation

Current issue:
- README says Python 3.9+, but Python 3.14 attempted to build pygame from source and failed without SDL headers.

Required behavior:
- Document tested Python range, currently `3.9-3.13`.
- Mention Python 3.14 may require pygame wheels or SDL build dependencies.
- Prefer installing with a known supported interpreter:

```sh
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Acceptance criteria:
- README no longer implies all future Python 3.x versions work.
- Install failure mode for Python 3.14 is explained.

## Suggested Implementation Plan

1. Add pure helper functions:
   - `parse_shortcut(combo) -> ParsedShortcut | ValidationError`
   - `validate_action(spec, *, quartz_available) -> ValidationResult`
   - `load_profile_checked(name, *, quartz_available) -> ProfileLoadResult`
   - `parse_sdl_mapping_token(raw) -> MappingToken`

2. Refactor `reload_bindings()`:
   - call checked loader;
   - update active bindings only on success;
   - keep last known good config on failure.

3. Refactor shell execution:
   - pass runtime security options into `ctx`;
   - implement `argv` execution with `shell=False`;
   - gate legacy `cmd` behavior behind explicit opt-in.

4. Update `cmd_automap()`:
   - use the SDL token parser;
   - support hats/axis direction tokens;
   - print warnings for skipped tokens.

5. Update docs:
   - shell action safety section;
   - Python version guidance;
   - validation/error behavior.

6. Add tests:
   - shortcut validation: valid combos, typos, pure modifiers, Quartz-required combos;
   - profile loader: malformed YAML preserves last-known-good behavior via helper tests;
   - SDL parser: `b0`, `a4`, `+a2`, `-a2`, `h0.1`.

## Regression Checklist

- `./gamepad-mapper list`
- `./gamepad-mapper status`
- `./gamepad-mapper automap`
- `./gamepad-mapper run` starts with current Bluetooth Switch Pro Controller
- Existing profiles load cleanly:
  - `default`
  - `coding`
  - `browsing`
  - `codex+typeless`
- `pip-audit -r requirements.txt`
- `bandit -r mapper.py`

## Non-Goals / Compatibility Notes

- Do not remove existing shortcut names or button labels.
- Do not require Quartz for normal AppleScript shortcuts like `ctrl+right`.
- Do not change the generated Switch Pro layout unless the SDL parser requires a semantically equivalent representation.
- Do not execute legacy shell strings by default.
