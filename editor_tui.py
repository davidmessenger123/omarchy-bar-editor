#!/usr/bin/python3
"""
Omarchy Bar Editor — terminal UI.

A self-contained, stdlib-only curses application (htop-style) for editing the
Omarchy bar: layout across left/center/right, position/transparency/anchor,
idle timeouts, and the [bar] styling block in shell.toml. Mouse-aware.

All reads/writes go through shell_io.py (the plugin's hardened config
boundary); this file never touches ~/.config/omarchy directly.

Keys:
    hjkl / arrows     move cursor; at the left edge ← jumps to the settings
                      pane, → returns to the layout
    Tab               switch pane (layout <-> settings)
    Enter             edit selected setting / confirm / widget options
    Space             select a widget in the layout (highlighted ▸);
                      with a widget selected, ←/→ or h/l move it between
                      columns, ↑/↓ or j/k reorder it within its column
    e                 edit the focused widget's options
    c                 duplicate the focused widget
    a or +            add widget to the focused section
    x or Delete       remove selected widget
    - / =             move widget up / down within its section
    [ / ]             move widget to previous / next section
    p                 plugin enable/disable
    r                 reset bar to defaults (confirm)
    b                 toggle bar visibility
    Ctrl+S save  Ctrl+R reload  Ctrl+Z undo  Ctrl+Y redo
    Ctrl+P            profiles: Enter loads, x deletes (double), c saves as
    Esc               close overlay / quit (after confirming unsaved work)
"""

import contextlib
import copy
import curses
import importlib.util
import io
import json
import math
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import termios
import time
from collections import deque

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
SHELL_IO = os.path.join(PLUGIN_DIR, "shell_io.py")
OMARCHY_PATH = "/usr/bin/omarchy"
CATALOG_PATH = "/usr/bin/omarchy-plugin-catalog"

# Prefer in-process shell_io (same security boundary, no subprocess per call).
_shell_io = None
try:
    _spec = importlib.util.spec_from_file_location("omarchy_bar_editor_shell_io", SHELL_IO)
    if _spec and _spec.loader:
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _shell_io = _mod
except Exception:
    _shell_io = None

SECTIONS = ["left", "center", "right"]
SECTION_LABELS = {"left": "Left", "center": "Center", "right": "Right"}
POSITIONS = ["top", "bottom", "left", "right"]

DEFAULT_TOML = {
    "background": "#1a1b26",
    "background_alpha": 1.0,
    "text": "#c0caf5",
    "active": "#f7768e",
    "scale_with_font": True,
    "size_horizontal": 26,
    "size_vertical": 28,
}

CAT_COLORS = {
    "system": "blue", "time": "cyan", "media": "green", "audio": "red",
    "network": "cyan", "hardware": "yellow", "utilities": "magenta",
    "desktop": "blue", "compositor": "yellow", "info": "green",
    "ai": "red", "status": "green", "appearance": "yellow", "files": "green",
}
CAT_STD = ["red", "green", "yellow", "blue", "magenta", "cyan", "white"]

# named color pairs (initialized in _main)
PAIR = {}
PAIR_ORDER = ["red", "green", "yellow", "blue", "magenta", "cyan", "white", "dim"]
MAX_PROMPT_CHARS = 4096


def cat_pair(category):
    name = CAT_COLORS.get((category or "").lower())
    if name is None:
        total = sum((category or "").encode("utf-8", "replace"))
        name = CAT_STD[total % len(CAT_STD)]
    return PAIR.get(name, 0)


# ---------------------------------------------------------------------------
# config / catalog I/O
# ---------------------------------------------------------------------------

MAX_COMMAND_OUTPUT = 2 * 1024 * 1024
MAX_ERROR_OUTPUT = 64 * 1024


def _reject_json_constant(value):
    raise ValueError("invalid JSON constant")


def _json_load(value):
    return json.loads(value, parse_constant=_reject_json_constant)


def _trusted_executable(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    try:
        path = os.path.realpath(value)
        info = os.stat(path)
    except OSError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o022):
        return None
    if not os.access(path, os.X_OK):
        return None
    return path


def _command_environment():
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": os.environ.get("HOME", ""),
        "LANG": "C",
        "LC_ALL": "C",
    }
    for name in (
        "USER", "LOGNAME", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
        "XDG_STATE_HOME", "DISPLAY", "WAYLAND_DISPLAY", "XDG_CURRENT_DESKTOP", "TERM",
    ):
        value = os.environ.get(name, "")
        if value:
            env[name] = value
    return {key: value for key, value in env.items() if value and len(value) <= 4096 and not any(ord(char) < 32 or ord(char) == 127 for char in value)}


def _kill_process(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass


def run(cmd, payload=None, timeout=30):
    if not isinstance(cmd, (list, tuple)) or not cmd or len(cmd) > 32:
        return 1, "", "invalid command"
    executable = _trusted_executable(cmd[0])
    if executable is None:
        return 1, "", "untrusted command"
    command = [executable]
    for value in cmd[1:]:
        if (not isinstance(value, str) or not value or len(value) > 4096
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            return 1, "", "invalid command argument"
        command.append(value)
    encoded = None
    if payload is not None:
        try:
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            return 1, "", str(exc)
        if len(encoded) > MAX_COMMAND_OUTPUT:
            return 1, "", "input too large"
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if encoded is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            cwd="/",
            env=_command_environment(),
        )
    except OSError as exc:
        return 1, "", str(exc)
    try:
        timeout = max(1, min(120, float(timeout)))
    except (TypeError, ValueError, OverflowError):
        return 1, "", "invalid timeout"
    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr = bytearray()
    try:
        selector.register(process.stdout, selectors.EVENT_READ, stdout)
        selector.register(process.stderr, selectors.EVENT_READ, stderr)
        input_view = memoryview(encoded) if encoded is not None else None
        input_offset = 0
        if input_view is not None:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, "input")
        deadline = time.monotonic() + max(1, min(120, float(timeout)))
        overflow = False
        timed_out = False
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _kill_process(process)
                process.wait()
                break
            for key, _ in selector.select(min(0.25, remaining)):
                if key.data == "input":
                    try:
                        count = os.write(key.fileobj.fileno(), input_view[input_offset:])
                        input_offset += count
                        if input_offset >= len(input_view):
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                    except (BlockingIOError, InterruptedError):
                        continue
                    except OSError:
                        selector.unregister(key.fileobj)
                        try:
                            key.fileobj.close()
                        except OSError:
                            pass
                    continue
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target = key.data
                limit = MAX_COMMAND_OUTPUT if target is stdout else MAX_ERROR_OUTPUT
                if len(target) < limit:
                    target.extend(chunk[:limit - len(target)])
                if len(target) >= limit:
                    overflow = True
                    _kill_process(process)
                    break
            if overflow:
                process.wait()
                break
        if timed_out:
            return 124, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")
        if overflow:
            return 125, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")
    except Exception:
        _kill_process(process)
        try:
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return 1, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")
    finally:
        selector.close()
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass
        if encoded is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        _kill_process(process)
        process.wait()
    return process.returncode, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace")


def _invoke_shell_io(args, payload=None):
    """Call shell_io cmd_* in-process; fall back to subprocess if import failed."""
    if _shell_io is None:
        return run(["/usr/bin/python3", SHELL_IO, *args], payload)

    out_buf = io.StringIO()
    err_buf = io.StringIO()
    old_stdin = sys.stdin
    cwd = os.getcwd()
    try:
        if payload is not None:
            sys.stdin = io.StringIO(json.dumps(payload, ensure_ascii=False, allow_nan=False))
        # Mirror shell_io.main() preflight so the security boundary still runs.
        os.chdir("/")
        root = os.path.abspath(_shell_io.CONFIG_ROOT)
        _shell_io.verify_parent_dir(os.path.join(root, "x"), create=True)
        os.chdir(cwd)

        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            cmd = args[0]
            if cmd == "read":
                _shell_io.cmd_read()
            elif cmd == "write":
                _shell_io.cmd_write()
            elif cmd == "profiles":
                _shell_io.cmd_profiles(args[1:])
            else:
                raise SystemExit(f"unknown command: {cmd}")
        output = out_buf.getvalue()
        if len(output) > MAX_COMMAND_OUTPUT:
            raise RuntimeError("shell_io output is too large")
        return 0, output, err_buf.getvalue()
    except SystemExit as e:
        code = e.code
        if code is None or code == 0:
            output = out_buf.getvalue()
            if len(output) > MAX_COMMAND_OUTPUT:
                raise RuntimeError("shell_io output is too large")
            return 0, output, err_buf.getvalue()
        if isinstance(code, int):
            return code, out_buf.getvalue(), err_buf.getvalue() or str(code)
        return 1, out_buf.getvalue(), str(code)
    except Exception as e:
        return 1, out_buf.getvalue(), str(e)
    finally:
        sys.stdin = old_stdin
        try:
            os.chdir(cwd)
        except OSError:
            pass


def io_read():
    rc, out, err = _invoke_shell_io(["read"])
    if rc != 0 or not out or len(out) > MAX_COMMAND_OUTPUT:
        raise RuntimeError(err.strip() or "shell_io read failed")
    try:
        data = _json_load(out)
    except (json.JSONDecodeError, TypeError, RecursionError, ValueError) as exc:
        raise RuntimeError("shell_io returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("shell_io returned invalid data")
    return data


def io_write(shell, toml_values, base=None):
    payload = {}
    if shell is not None:
        if not isinstance(base, dict):
            raise RuntimeError("base shell configuration is missing")
        payload["shell"] = shell
        payload["base"] = base
    if toml_values is not None:
        payload["toml"] = {"values": toml_values}
    try:
        if len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_COMMAND_OUTPUT:
            raise RuntimeError("configuration payload is too large")
    except (TypeError, ValueError) as exc:
        raise RuntimeError("configuration payload is not serializable") from exc
    rc, out, err = _invoke_shell_io(["write"], payload)
    if rc != 0:
        raise RuntimeError(err.strip() or "shell_io write failed")
    if shell is None:
        return None
    try:
        result = json.loads(out)
    except (json.JSONDecodeError, TypeError, RecursionError, ValueError) as exc:
        raise RuntimeError("shell_io write returned invalid JSON") from exc
    if not isinstance(result, dict) or not isinstance(result.get("shell"), dict):
        raise RuntimeError("shell_io write did not return shell configuration")
    return result["shell"]


def _valid_id(value):
    text = str(value or "")
    return (bool(text) and len(text) <= 128 and text[0].isascii() and text[0].isalnum()
            and text not in ("constructor", "prototype", "__proto__")
            and all((char.isascii() and (char.isalnum() or char in "._-")) for char in text))


def _safe_text(value, limit=256):
    if not isinstance(value, str):
        return ""
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return ""
    return value[:limit]


def _valid_color(value):
    text = str(value or "")
    return len(text) == 7 and text[0] == "#" and all(
        char in "0123456789abcdefABCDEF" for char in text[1:]
    )


def _valid_profile_name(value):
    text = str(value or "").strip()
    return bool(text) and len(text) <= 64 and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,63}", text))


def load_catalog():
    rc, out, err = run([CATALOG_PATH], timeout=10)
    if rc != 0 or not out or len(out) > MAX_COMMAND_OUTPUT:
        return None
    try:
        entries = _json_load(out)
    except (json.JSONDecodeError, TypeError, RecursionError, ValueError):
        return None
    if not isinstance(entries, list) or len(entries) > 10000:
        return None
    result = {}
    for entry in entries:
        if not isinstance(entry, dict) or not _valid_id(entry.get("id")):
            continue
        item = dict(entry)
        item["id"] = str(entry["id"])
        kinds = item.get("kinds")
        item["kinds"] = [str(kind)[:64] for kind in kinds[:32]] if isinstance(kinds, list) else []
        result[item["id"]] = item
    return result


def load_plugin_states():
    rc, out, err = run([OMARCHY_PATH, "plugin", "list", "--json"], timeout=10)
    if rc != 0 or not out or len(out) > MAX_COMMAND_OUTPUT:
        return None
    try:
        entries = _json_load(out)
    except (json.JSONDecodeError, TypeError, RecursionError, ValueError):
        return None
    if not isinstance(entries, list) or len(entries) > 10000:
        return None
    result = {}
    for entry in entries:
        if not isinstance(entry, dict) or not _valid_id(entry.get("id")):
            continue
        enabled = entry.get("enabled")
        result[str(entry["id"])] = enabled if isinstance(enabled, bool) else None
    return result


def load_profiles():
    rc, out, err = _invoke_shell_io(["profiles", "list"])
    if rc != 0 or not out or len(out) > MAX_COMMAND_OUTPUT:
        return None
    try:
        names = _json_load(out)
    except (json.JSONDecodeError, TypeError, RecursionError, ValueError):
        return None
    if not isinstance(names, list):
        return None
    return [str(name) for name in names if isinstance(name, str) and len(name) <= 64]


# ---------------------------------------------------------------------------
# pure helpers (mirror Editor.js)
# ---------------------------------------------------------------------------

def ensure_cfg(cfg):
    if not isinstance(cfg, dict):
        cfg = {}
    bar = cfg.get("bar")
    if not isinstance(bar, dict):
        bar = {}
        cfg["bar"] = bar
    layout = bar.get("layout")
    if not isinstance(layout, dict):
        layout = {}
        bar["layout"] = layout
    for sec in SECTIONS:
        if not isinstance(layout.get(sec), list):
            layout[sec] = []
    cfg["version"] = 1
    return cfg


def widget_info(catalog, wid):
    if not isinstance(catalog, dict) or not isinstance(wid, str):
        return {}
    value = catalog.get(wid, {})
    return value if isinstance(value, dict) else {}


def display_name(catalog, wid):
    info = widget_info(catalog, wid)
    bw = info.get("barWidget") if isinstance(info.get("barWidget"), dict) else {}
    value = bw.get("displayName") or info.get("name") or wid
    return str(value)[:256]


def category_of(catalog, wid):
    info = widget_info(catalog, wid)
    bw = info.get("barWidget") if isinstance(info.get("barWidget"), dict) else {}
    return str(bw.get("category") or "Utilities")[:64]


def allow_multiple(catalog, wid):
    info = widget_info(catalog, wid)
    bw = info.get("barWidget") if isinstance(info.get("barWidget"), dict) else {}
    v = bw.get("allowMultiple")
    return True if v is None else v is True


def is_bar_widget(info):
    return isinstance(info, dict) and "bar-widget" in (info.get("kinds") or [])


def hosts(catalog):
    result = ["omarchy.bar"]
    ids = []
    for wid, info in (catalog or {}).items():
        if wid == "omarchy.bar" or not _valid_id(wid) or not isinstance(info, dict):
            continue
        kinds = info.get("kinds") if isinstance(info.get("kinds"), list) else []
        if "bar" not in kinds or not isinstance(info.get("barPath"), str):
            continue
        if len(info["barPath"]) > 4096 or "\x00" in info["barPath"]:
            continue
        ids.append(wid)
    return result + sorted(ids)


def entry_id(entry):
    value = entry.get("id") if isinstance(entry, dict) else entry
    return value if _valid_id(value) else ""


def _num_str(values, key, default):
    if not isinstance(values, dict):
        return default
    value = values.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)[:256]


def schema_type(field, value=None):
    t = str(field.get("type") or "").lower()
    if t == "enum":
        return "enum"
    if t in ("bool", "boolean"):
        return "bool"
    if t in ("int", "integer"):
        return "int"
    if t in ("number", "float", "double"):
        return "num"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "num"
    return "text"


# ---------------------------------------------------------------------------
# application model
# ---------------------------------------------------------------------------

class Model:
    def __init__(self):
        self.catalog = {}
        self.states = {}
        self.cfg = {}
        self.disk_cfg = {}
        self.toml = {}
        self.dirty = False
        self.status = ""

        self.position = "top"
        self.transparent = False
        self.anchor = ""
        self.host = "omarchy.bar"
        self.font_family = ""
        self.screensaver = 0
        self.lock = 0
        self.bg = DEFAULT_TOML["background"]
        self.text_col = DEFAULT_TOML["text"]
        self.active = DEFAULT_TOML["active"]
        self.alpha = 100
        self.size_h = 26
        self.size_v = 28
        self.scale_font = True

        self.layout = {sec: [] for sec in SECTIONS}

        self._profiles = []
        self._meta = {}

        self.undo_limit = 100
        self.undo = deque(maxlen=self.undo_limit)
        self.redo = deque(maxlen=self.undo_limit)

    # -- snapshots ---------------------------------------------------------
    def snapshot(self):
        snap = {}
        for k in (
            "cfg", "disk_cfg", "toml", "position", "transparent", "anchor", "host",
            "font_family", "screensaver", "lock", "bg", "text_col",
            "active", "alpha", "size_h", "size_v", "scale_font", "layout",
        ):
            v = getattr(self, k)
            if k in ("cfg", "disk_cfg", "layout", "toml"):
                snap[k] = copy.deepcopy(v)
            else:
                snap[k] = v
        return snap

    def restore(self, snap):
        for k, v in snap.items():
            setattr(self, k, v)

    def push_history(self):
        self.undo.append(self.snapshot())
        self.redo.clear()

    def undo_step(self):
        if not self.undo:
            return False
        self.redo.append(self.snapshot())
        self.restore(self.undo.pop())
        self.mark_dirty()
        return True

    def redo_step(self):
        if not self.redo:
            return False
        self.undo.append(self.snapshot())
        self.restore(self.redo.pop())
        self.mark_dirty()
        return True

    # -- load / save -------------------------------------------------------
    def meta(self, wid):
        hit = self._meta.get(wid)
        if hit is None:
            hit = (display_name(self.catalog, wid), category_of(self.catalog, wid))
            self._meta[wid] = hit
        return hit

    def load(self):
        old = self.snapshot()
        old_catalog = copy.deepcopy(self.catalog)
        old_states = dict(self.states)
        old_profiles = list(self._profiles)
        old_meta = dict(self._meta)
        try:
            data = io_read()
            raw_cfg = data.get("cfg")
            if not isinstance(raw_cfg, dict):
                raise RuntimeError("configuration is not an object")
            raw_toml = data.get("toml")
            cfg = ensure_cfg(copy.deepcopy(raw_cfg))
            toml = copy.deepcopy(raw_toml) if isinstance(raw_toml, dict) else {}
            catalog = load_catalog()
            states = load_plugin_states()
            profiles = data.get("profiles")
            if not isinstance(profiles, list):
                profiles = load_profiles()
            if profiles is None:
                profiles = old_profiles
            profiles = [str(n)[:64] for n in profiles if isinstance(n, str) and len(n) <= 64]
            loaded = dict(old)
            loaded["cfg"] = cfg
            loaded["disk_cfg"] = copy.deepcopy(raw_cfg)
            loaded["toml"] = toml
            self.restore(loaded)
            if catalog is not None:
                self.catalog = catalog
            if states is not None:
                self.states = states
            self._profiles = profiles
            self.apply_from_cfg()
            self.undo.clear()
            self.redo.clear()
            self._meta = {}
        except Exception:
            self.restore(old)
            self.catalog = old_catalog
            self.states = old_states
            self._profiles = old_profiles
            self._meta = old_meta
            raise

    def apply_from_cfg(self):
        bar = self.cfg.get("bar") if isinstance(self.cfg.get("bar"), dict) else {}
        position = bar.get("position")
        self.position = position if position in POSITIONS else "top"
        self.transparent = bar.get("transparent") is True
        self.anchor = str(bar.get("centerAnchor") or "")[:256]
        host = bar.get("host")
        self.host = str(host)[:128] if isinstance(host, str) and host else "omarchy.bar"
        font = bar.get("fontFamily")
        self.font_family = str(font)[:256] if isinstance(font, str) else ""
        self.scale_font = bar.get("scaleWithFont") is not False

        idle = self.cfg.get("idle") if isinstance(self.cfg.get("idle"), dict) else {}
        try:
            self.screensaver = max(0, min(86400, int(idle.get("screensaver", 0) or 0)))
        except (TypeError, ValueError):
            self.screensaver = 0
        try:
            self.lock = max(0, min(86400, int(idle.get("lock", 0) or 0)))
        except (TypeError, ValueError):
            self.lock = 0

        tv = self.toml.get("values") if isinstance(self.toml.get("values"), dict) else {}
        self.bg = _num_str(tv, "background", DEFAULT_TOML["background"])
        self.text_col = _num_str(tv, "text", DEFAULT_TOML["text"])
        self.active = _num_str(tv, "active", DEFAULT_TOML["active"])
        try:
            alpha = float(tv.get("background_alpha", 1.0))
            if not math.isfinite(alpha):
                raise ValueError
            self.alpha = int(max(0.0, min(1.0, alpha)) * 100)
        except (TypeError, ValueError):
            self.alpha = 100
        try:
            self.size_h = max(1, min(1000, int(tv.get("size_horizontal", 26))))
        except (TypeError, ValueError):
            self.size_h = 26
        try:
            self.size_v = max(1, min(1000, int(tv.get("size_vertical", 28))))
        except (TypeError, ValueError):
            self.size_v = 28
        sf = _num_str(tv, "scale_with_font", "true")
        self.scale_font = str(sf).strip().lower() not in ("false", "0", "no", "off")

        layout = bar.get("layout") if isinstance(bar.get("layout"), dict) else {}
        count = 0
        for sec in SECTIONS:
            raw = layout.get(sec) if isinstance(layout.get(sec), list) else []
            self.layout[sec] = []
            for entry in raw:
                if count >= 1000:
                    break
                if isinstance(entry, dict):
                    item = copy.deepcopy(entry)
                    wid = entry_id(item)
                else:
                    wid = entry_id(entry)
                    item = {"id": wid}
                if wid:
                    self.layout[sec].append(item)
                    count += 1
        self.dirty = False
        self.status = "Ready"

    def gather_cfg(self):
        cfg = ensure_cfg(copy.deepcopy(self.cfg))
        bar = cfg["bar"]
        layout = bar.setdefault("layout", {})
        for sec in SECTIONS:
            entries = []
            for entry in self.layout.get(sec, []):
                if not isinstance(entry, dict):
                    entry = {"id": entry}
                if not _valid_id(entry.get("id")):
                    continue
                entries.append(copy.deepcopy(entry))
            layout[sec] = entries[:1000]

        def put(dst, value, default):
            value = _safe_text(value, 256) if isinstance(value, str) else value
            if value != default:
                bar[dst] = value
            elif dst in bar:
                del bar[dst]

        position = self.position if self.position in POSITIONS else "top"
        host = _safe_text(self.host, 128)
        if not _valid_id(host):
            host = "omarchy.bar"
        put("position", position, "top")
        put("transparent", bool(self.transparent), False)
        put("centerAnchor", _safe_text(self.anchor, 256), "")
        put("host", host, "omarchy.bar")
        put("fontFamily", _safe_text(self.font_family, 256), "")
        put("scaleWithFont", bool(self.scale_font), True)

        if self.screensaver or self.lock or "idle" in cfg:
            idle = cfg.get("idle")
            if not isinstance(idle, dict):
                idle = {}
                cfg["idle"] = idle
            idle["screensaver"] = max(0, min(86400, int(self.screensaver)))
            idle["lock"] = max(0, min(86400, int(self.lock)))
        return cfg

    def gather_toml(self):
        colors = []
        for value in (self.bg, self.text_col, self.active):
            if not _valid_color(value):
                raise ValueError("invalid color")
            colors.append(value)
        alpha = float(self.alpha) / 100.0
        if not math.isfinite(alpha):
            raise ValueError("invalid alpha")
        return {
            "background": colors[0],
            "background_alpha": max(0.0, min(1.0, alpha)),
            "text": colors[1],
            "active": colors[2],
            "scale_with_font": bool(self.scale_font),
            "size_horizontal": max(1, min(1000, int(self.size_h))),
            "size_vertical": max(1, min(1000, int(self.size_v))),
        }

    def save(self):
        written = io_write(self.gather_cfg(), self.gather_toml(), self.disk_cfg)
        if written is not None:
            self.cfg = ensure_cfg(copy.deepcopy(written))
            self.disk_cfg = copy.deepcopy(written)
        self.dirty = False
        self.status = "Saved — the shell reloads automatically"

    def reset_bar(self, force=False):
        if self.dirty and not force:
            self.status = "Unsaved changes — confirm reset"
            return False
        rc, out, err = run([OMARCHY_PATH, "bar", "defaults"], timeout=15)
        if rc != 0:
            self.status = "Reset failed"
            return False
        self.load()
        self.status = "Reset to default Omarchy bar"
        return True

    def toggle_bar(self):
        rc, out, err = run([OMARCHY_PATH, "toggle", "bar"])
        self.status = "Bar visibility toggled" if rc == 0 else "Toggle failed"

    def reload(self, force=False):
        if self.dirty and not force:
            self.status = "Unsaved changes — confirm reload"
            return False
        self.load()
        self.status = "Reloaded from disk"
        return True

    def plugin_set(self, pid, enable):
        if not _valid_id(pid) or not isinstance(enable, bool) or self.states.get(pid) is None:
            self.status = "Plugin state is unknown"
            return False
        rc, out, err = run(
            [OMARCHY_PATH, "plugin", "enable" if enable else "disable", str(pid)])
        if rc != 0:
            self.status = f"Could not {'enable' if enable else 'disable'} {pid}"
            return False
        states = load_plugin_states()
        if states is None:
            self.states = dict(self.states)
            self.states[pid] = enable
        else:
            self.states = states
        self.status = f"{pid} {'enabled' if enable else 'disabled'}"
        return True

    # -- layout edits ------------------------------------------------------
    def mark_dirty(self):
        self.dirty = True
        self.status = "Unsaved changes"

    def add_widget(self, sec, wid):
        if sec not in SECTIONS or not _valid_id(wid) or len(self.layout[sec]) >= 1000:
            return False
        self.push_history()
        self.layout[sec].append({"id": str(wid)})
        self.mark_dirty()
        return True

    def remove_widget(self, sec, idx):
        if sec not in SECTIONS or not (0 <= idx < len(self.layout[sec])):
            return False
        self.push_history()
        del self.layout[sec][idx]
        self.mark_dirty()
        return True

    def move_widget(self, sec, idx, delta):
        if sec not in SECTIONS or delta not in (-1, 1):
            return False
        lst = self.layout[sec]
        target = idx + delta
        if not (0 <= idx < len(lst)) or not (0 <= target < len(lst)):
            return False
        self.push_history()
        lst[idx], lst[target] = lst[target], lst[idx]
        self.mark_dirty()
        return True

    def move_section(self, sec, idx, target_sec):
        if sec not in SECTIONS or target_sec not in SECTIONS or target_sec == sec:
            return False
        if not (0 <= idx < len(self.layout[sec])) or len(self.layout[target_sec]) >= 1000:
            return False
        self.push_history()
        item = self.layout[sec].pop(idx)
        self.layout[target_sec].append(item)
        self.mark_dirty()
        return True

    def drop_widget(self, src_sec, src_idx, dst_sec, dst_idx):
        if src_sec not in SECTIONS or dst_sec not in SECTIONS:
            return None
        src = self.layout[src_sec]
        dst = self.layout[dst_sec]
        if not (0 <= src_idx < len(src)) or len(dst) >= 1000:
            return None
        try:
            target = int(dst_idx)
        except (TypeError, ValueError):
            return None
        self.push_history()
        item = src.pop(src_idx)
        if src_sec == dst_sec and src_idx < target:
            target -= 1
        pos = max(0, min(target, len(dst)))
        dst.insert(pos, item)
        self.mark_dirty()
        return pos

    def duplicate_widget(self, sec, idx):
        if sec not in SECTIONS or not (0 <= idx < len(self.layout[sec])) or len(self.layout[sec]) >= 1000:
            return False
        self.push_history()
        item = copy.deepcopy(self.layout[sec][idx])
        self.layout[sec].append(item)
        self.mark_dirty()
        return True

    def widget_schema(self, wid):
        info = widget_info(self.catalog, wid)
        bw = info.get("barWidget") if isinstance(info.get("barWidget"), dict) else {}
        schema = bw.get("schema")
        if isinstance(schema, dict):
            schema = [schema]
        if not isinstance(schema, list):
            return []
        result = []
        for raw in schema[:256]:
            if not isinstance(raw, dict) or not _valid_id(raw.get("key")):
                continue
            field_type = str(raw.get("type") or "text").lower()
            if field_type not in ("string", "text", "enum", "bool", "boolean", "int", "integer", "number", "float", "double"):
                continue
            field = {
                "key": str(raw["key"]),
                "type": field_type,
                "label": _safe_text(raw.get("label"), 256) or str(raw["key"]),
                "options": [],
            }
            options = raw.get("options")
            if isinstance(options, list):
                for option in options[:256]:
                    if isinstance(option, (str, int, float, bool)) and not (isinstance(option, float) and not math.isfinite(option)):
                        field["options"].append(str(option)[:256])
            default = raw.get("defaultValue")
            if default is not None:
                if field_type == "bool" and not isinstance(default, bool):
                    continue
                if field_type in ("int", "integer") and (isinstance(default, bool) or not isinstance(default, (int, float)) or not math.isfinite(float(default))):
                    continue
                if field_type in ("number", "float", "double") and (isinstance(default, bool) or not isinstance(default, (int, float)) or not math.isfinite(float(default))):
                    continue
                if field_type in ("string", "text", "enum") and not isinstance(default, str):
                    continue
                field["defaultValue"] = str(default)[:4096]
            bounds = []
            for bound in ("min", "max"):
                value = raw.get(bound)
                if value is None:
                    bounds.append(None)
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                    bounds.append(None)
                    continue
                bounds.append(value)
            if bounds[0] is not None and bounds[1] is not None and bounds[0] > bounds[1]:
                continue
            field["min"], field["max"] = bounds
            result.append(field)
        return result

    def widget_options(self, sec, idx):
        if sec not in SECTIONS or not (0 <= idx < len(self.layout[sec])):
            return []
        entry = self.layout[sec][idx]
        if not isinstance(entry, dict):
            entry = {}
        wid = entry_id(entry)
        opts = []
        for field in self.widget_schema(wid):
            key = field["key"]
            value = entry.get(key, field.get("defaultValue"))
            options = field.get("options") or []
            opts.append({
                "key": key,
                "label": _safe_text(field.get("label"), 256) or key,
                "type": schema_type(field, value),
                "enum": options,
                "min": field.get("min"),
                "max": field.get("max"),
                "value": value,
            })
        return opts

    def apply_widget_option(self, sec, idx, key, value):
        if sec not in SECTIONS or not (0 <= idx < len(self.layout[sec])) or not _valid_id(key):
            return False
        field = next((item for item in self.widget_schema(entry_id(self.layout[sec][idx]))
                      if item.get("key") == key), None)
        if field is None:
            return False
        kind = schema_type(field, value)
        if kind == "enum":
            if not isinstance(value, (str, int, float, bool)) or isinstance(value, float) and not math.isfinite(value):
                return False
            if str(value) not in (field.get("options") or []):
                return False
            value = str(value)
        elif kind == "bool":
            if not isinstance(value, bool):
                return False
        elif kind == "int":
            if isinstance(value, bool):
                return False
            try:
                number = float(value)
                if not math.isfinite(number) or number != int(number):
                    return False
                value = int(number)
            except (TypeError, ValueError, OverflowError):
                return False
        elif kind == "num":
            if isinstance(value, bool):
                return False
            try:
                value = float(value)
                if not math.isfinite(value):
                    return False
            except (TypeError, ValueError, OverflowError):
                return False
        else:
            value = _safe_text(value, 4096)
            if not value:
                return False
        if kind in ("int", "num"):
            try:
                if field.get("min") is not None:
                    value = max(value, field["min"])
                if field.get("max") is not None:
                    value = min(value, field["max"])
            except (TypeError, ValueError, OverflowError):
                return False
        entry = self.layout[sec][idx]
        if not isinstance(entry, dict):
            entry = {"id": entry_id(entry)}
            self.layout[sec][idx] = entry
        self.push_history()
        entry[key] = value
        self.mark_dirty()
        return True

    def refresh_profiles(self):
        profiles = load_profiles()
        if profiles is not None:
            self._profiles = profiles
        return self._profiles

    def save_profile(self, name):
        if not _valid_profile_name(name):
            raise ValueError("invalid profile name")
        payload = {
            "shell": self.gather_cfg(),
            "toml": {"values": self.gather_toml()},
        }
        rc, out, err = _invoke_shell_io(["profiles", "save", str(name)], payload)
        if rc != 0:
            raise RuntimeError(err.strip() or "profile save failed")

    def load_profile(self, name, force=False):
        if self.dirty and not force:
            raise RuntimeError("unsaved changes require confirmation")
        if not _valid_profile_name(name):
            raise ValueError("invalid profile name")
        rc, out, err = _invoke_shell_io(["profiles", "load", str(name)])
        if rc != 0:
            raise RuntimeError(err.strip() or f"no profile named {name}")
        try:
            data = _json_load(out) if isinstance(out, str) else out
        except (json.JSONDecodeError, TypeError, RecursionError, ValueError):
            raise RuntimeError("corrupt profile")
        if not isinstance(data, dict) or not isinstance(data.get("shell"), dict):
            raise RuntimeError("corrupt profile")
        toml = data.get("toml")
        if not isinstance(toml, dict) or not isinstance(toml.get("values", {}), dict):
            raise RuntimeError("corrupt profile")
        old = self.snapshot()
        try:
            self.cfg = ensure_cfg(copy.deepcopy(data["shell"]))
            self.toml = {"values": copy.deepcopy(toml.get("values", {})), "exists": True}
            self.apply_from_cfg()
        except Exception:
            self.restore(old)
            raise
        self.undo.clear()
        self.redo.clear()
        self.mark_dirty()
        self.status = "Loaded profile — save to apply"

    def delete_profile(self, name):
        if not _valid_profile_name(name):
            raise ValueError("invalid profile name")
        rc, out, err = _invoke_shell_io(["profiles", "delete", str(name)])
        if rc != 0:
            raise RuntimeError(err.strip() or f"no profile named {name}")

    # -- display data ------------------------------------------------------
    def add_options(self):
        placed = {}
        for sec in SECTIONS:
            for entry in self.layout[sec]:
                wid = entry_id(entry)
                if wid:
                    placed[wid] = placed.get(wid, 0) + 1
        out = []
        for wid, info in (self.catalog or {}).items():
            if not is_bar_widget(info) or not _valid_id(wid):
                continue
            state = self.states.get(wid)
            if state is False:
                continue
            if not allow_multiple(self.catalog, wid) and placed.get(wid, 0) > 0:
                continue
            bw = info.get("barWidget") if isinstance(info.get("barWidget"), dict) else {}
            display = _safe_text(bw.get("displayName") or info.get("name") or wid, 256)
            if state is None:
                display += " (state unknown)"
            out.append({
                "id": wid,
                "name": display[:256],
                "category": _safe_text(bw.get("category") or "Utilities", 64),
                "desc": _safe_text(bw.get("description") or info.get("description") or "", 1024),
                "state": state,
            })
        out.sort(key=lambda r: r["name"].lower())
        return out

    def plugins(self):
        out = []
        for wid, info in (self.catalog or {}).items():
            kinds = info.get("kinds") if isinstance(info, dict) and isinstance(info.get("kinds"), list) else []
            if "bar-widget" not in kinds and "overlay" not in kinds:
                continue
            if wid == "davidjm.bar-editor" or not _valid_id(wid):
                continue
            bw = info.get("barWidget") if isinstance(info.get("barWidget"), dict) else {}
            state = self.states.get(wid)
            out.append({
                "id": wid,
                "name": _safe_text(bw.get("displayName") or info.get("name") or wid, 256),
                "enabled": state if state in (True, False) else None,
            })
        out.sort(key=lambda r: r["name"].lower())
        return out


# ---------------------------------------------------------------------------
# curses application
# ---------------------------------------------------------------------------

class BarEditorTUI:
    def __init__(self, stdscr, model):
        self.s = stdscr
        self.model = model
        self.max_y, self.max_x = self.s.getmaxyx()

        self.side = "layout"          # "layout" | "settings"
        self.sec_i = 0
        self.lay_i = 0
        self.lay_scroll = {sec: 0 for sec in SECTIONS}
        self.sel_sec = None           # widget carried for cross-column move
        self.sel_idx = -1
        self.set_i = 0
        self.set_scroll = 0
        self.row_rects = [[] for _ in SECTIONS]
        self.settings_rect = (0, 0, 0, 0)
        self.layout_rect = (0, 0, 0, 0)
        self.search_rect = (0, 0, 0)
        self.search = ""
        self.search_active = False
        self.drag_sec = None        # mouse drag-and-drop state
        self.drag_idx = -1
        self.drag_oy = self.drag_ox = -1
        self.pending_delete = None  # double-press x to delete a profile

        self.ov = None
        self.prompt = None
        self.prompt_text = ""

        self.need_refresh = True
        self.running = True

    # ------------------------------------------------------------------
    # scribes
    # ------------------------------------------------------------------
    def _put(self, y, x, text, attr=0, pair=0):
        if y < 0 or x < 0:
            return
        maxw = max(0, self.max_x - x)
        if maxw == 0:
            return
        try:
            if pair:
                self.s.addnstr(y, x, text[:maxw], maxw, attr | curses.color_pair(pair))
            else:
                self.s.addnstr(y, x, text[:maxw], maxw, attr)
        except curses.error:
            pass

    def _box(self, top, left, right, bottom, title, pair=0):
        if right <= left or bottom <= top:
            return
        pad = max(0, right - left - 1)
        self._put(top, left, "┌" + "─" * pad + "┐", 0, pair)
        t = f" {title[: max(0, right - left - 3)]} "
        self._put(top, left + 2, t, 0, pair)
        for y in range(top + 1, bottom):
            self._put(y, left, "│", 0, pair)
            self._put(y, right, "│", 0, pair)
        self._put(bottom, left, "└" + "─" * pad + "┘", 0, pair)

    # ------------------------------------------------------------------
    # drawing
    # ------------------------------------------------------------------
    def geometry(self):
        h, w = self.max_y, self.max_x
        body_top, body_bot = 1, max(1, h - 1)
        settings_w = max(26, min(40, w // 3))
        settings_w = min(settings_w, w - 40 if w >= 40 else w - 1)
        if settings_w < 10:
            settings_w = max(8, w // 4)
        return {
            "settings": (body_top, 0,
                         min(settings_w, w - 1), body_bot - body_top),
            "layout": (body_top, min(settings_w + 1, w - 1),
                       max(1, w - settings_w - 2), body_bot - body_top),
        }

    def draw(self):
        self.max_y, self.max_x = self.s.getmaxyx()
        if self.max_y < 6 or self.max_x < 30:
            self.s.erase()
            self._put(0, 0, "Terminal too small")
            try:
                self.s.refresh()
            except curses.error:
                pass
            self.need_refresh = False
            return
        self.s.erase()
        g = self.geometry()
        self.draw_header()
        self.draw_settings(g)
        self.draw_layout(g)
        self.draw_status()
        if self.ov:
            self.ov.draw(self.s)
        elif self.prompt:
            self.draw_prompt()
        self.s.refresh()
        self.need_refresh = False

    def draw_header(self):
        w = self.max_x
        title = " OMARCHY  BAR EDITOR "
        hints = "  ←/→/↑/↓ move · a add · e opts · c dup · p plugins "  # noqa: E501
        self._put(0, 0, " " * w, 0)
        self._put(0, 1, title, curses.A_BOLD, PAIR.get("cyan", 0))
        pad = max(0, w - len(title) - 3 - len(hints))
        if pad > 0:
            self._put(0, 2 + len(title), "·" * pad, 0, PAIR.get("dim", 0))
        if w - 1 - len(hints) >= 0:
            self._put(0, w - 1 - len(hints), hints, 0, PAIR.get("dim", 0))

    def settings_rows(self):
        m = self.model
        return [
            ("position", "Position", m.position),
            ("transparent", "Transparent bar", "on" if m.transparent else "off"),
            ("anchor", "Center anchor widget", m.anchor or "(none)"),
            ("host", "Bar host", m.host),
            ("font", "Font family", m.font_family or "(system)"),
            ("--", "─", ""),
            ("screensaver", "Screensaver (s)", str(m.screensaver)),
            ("lock", "Lock (s)", str(m.lock)),
            ("--", "─", ""),
            ("bg", "Background", m.bg),
            ("text", "Text", m.text_col),
            ("active", "Active", m.active),
            ("alpha", "Background alpha (%)", f"{m.alpha}%"),
            ("size_h", "Horizontal size (px)", str(m.size_h)),
            ("size_v", "Vertical size (px)", str(m.size_v)),
            ("scale_font", "Scale with font", "on" if m.scale_font else "off"),
        ]

    def draw_settings(self, g):
        top, left, width, height = g["settings"]
        right = min(self.max_x - 1, left + width - 1)
        self.settings_rect = (top, left, right, top + height - 1)
        self._box(top, left, right, top + height - 1, "SETTINGS")
        rows = self.settings_rows()
        inner_h = max(1, height - 2)

        if self.set_i < self.set_scroll:
            self.set_scroll = self.set_i
        if self.set_i >= self.set_scroll + inner_h:
            self.set_scroll = self.set_i - inner_h + 1
        self.set_scroll = max(0, min(self.set_scroll, max(0, len(rows) - inner_h)))

        for off in range(inner_h):
            idx = self.set_scroll + off
            y = top + 1 + off
            if idx >= len(rows):
                break
            kind, label, value = rows[idx]
            usable = max(0, right - left - 2)
            if kind == "--":
                self._put(y, left + 1, "─" * usable, 0, PAIR.get("dim", 0))
                continue
            selected = (self.side == "settings" and idx == self.set_i and not self.ov)
            name_w = min(19, max(10, usable - 2))
            val = str(value)
            line = label.ljust(name_w)[:name_w] + " " + val[: max(0, usable - name_w - 1)]
            if selected:
                self._put(y, left + 1, line, curses.A_BOLD, PAIR.get("accent", 0))
            else:
                self._put(y, left + 1, line)
            if kind in ("position", "host"):
                self._put(y, right - 1, "▼", 0, PAIR.get("dim", 0))
            elif kind in ("transparent", "scale_font"):
                on = value == "on"
                cell = " ✓" if on else "  "
                self._put(y, right - 2, cell, 0,
                          PAIR.get("green", 0) if on else PAIR.get("dim", 0))

    def draw_layout(self, g):
        top, left, width, height = g["layout"]
        right = min(self.max_x - 1, left + width - 1)
        if right <= left:
            return
        self.layout_rect = (top, left, right, top + height - 1)
        self._box(top, left, right, top + height - 1, "BAR  LAYOUT")

        sy = top + 1
        self._put(sy, left + 1, "Search ", 0, PAIR.get("dim", 0))
        sx = left + 8
        sw = max(1, right - sx - 1)
        self.search_rect = (sy, sx, sx + sw)
        self._put(sy, sx, " " * sw)
        txt = (self.search + "▏") if self.search_active else (self.search or "filter…")
        self._put(sy, sx, txt, 0, PAIR.get("dim", 0) if not self.search else 0)

        inner_top = sy + 1
        inner_bot = top + height - 2
        inner_h = inner_bot - inner_top
        if inner_h < 3:
            return
        sec_w = max(8, (width - 4) // 3)
        x0 = left + 1
        self.row_rects = [[] for _ in SECTIONS]
        for si, sec in enumerate(SECTIONS):
            x1 = x0 + sec_w if si < 2 else right - 1
            self._box(inner_top, x0, x1, inner_bot,
                      f"{SECTION_LABELS[sec].upper()} ({len(self.model.layout[sec])})",
                      PAIR.get("dim", 0))
            self.draw_section(sec, si, inner_top + 1, x0 + 1, x1 - 1, inner_h - 2)
            x0 = x1 + 1

    def draw_section(self, sec, si, top, left, right, height):
        lst = self.model.layout[sec]
        hay = self.search.lower()
        if hay:
            def row_key(e):
                ident = entry_id(e)
                name, cat = self.model.meta(ident)
                return (str(ident) + " " + name + " " + cat).lower()
            shown = [i for i, e in enumerate(lst) if hay in row_key(e)]
        else:
            shown = list(range(len(lst)))

        scroll = self.lay_scroll[sec]
        if self.side == "layout" and si == self.sec_i and not self.ov:
            if self.lay_i < scroll:
                scroll = self.lay_i
            if self.lay_i >= scroll + height:
                scroll = self.lay_i - height + 1
        scroll = max(0, min(scroll, max(0, len(shown) - height)))
        self.lay_scroll[sec] = scroll

        rects = []
        for off in range(height):
            y = top + off
            idx = scroll + off
            if idx >= len(shown):
                if off == height - 1:
                    selected = (self.side == "layout" and si == self.sec_i
                                and self.lay_i >= len(shown) and not self.ov)
                    attr = curses.A_REVERSE if selected else 0
                    self._put(y, left, "+ Add widget", attr, PAIR.get("green", 0))
                    rects.append(("add", si, y, left, right))
                break
            real = shown[idx]
            ident = entry_id(lst[real])
            selected = (self.side == "layout" and si == self.sec_i
                        and self.lay_i == real and not self.ov)
            carried = (self.sel_sec == sec and self.sel_idx == real)
            usable = max(1, right - left + 1)
            name, cat = self.model.meta(ident)
            mark = "▸" if carried else " "
            line = mark + " " + name
            if len(line) > usable:
                line = ((mark + " " + name)[:max(1, usable - 1)] + "…")[:usable]
            if carried:
                self._put(y, left, line, curses.A_BOLD, PAIR.get("accent", 0))
            elif selected:
                self._put(y, left, line, curses.A_REVERSE)
            else:
                self._put(y, left, line, 0, cat_pair(cat))
            rects.append(("row", real, y, left, right))
        self.row_rects[si] = rects

    def draw_status(self):
        w = self.max_x
        m = self.model
        self._put(self.max_y - 1, 0, " " * w)
        dotpair = PAIR.get("urgent" if m.dirty else "green", 0)
        self._put(self.max_y - 1, 1, "●", curses.A_BOLD, dotpair)
        self._put(self.max_y - 1, 3, m.status, 0,
                  PAIR.get("urgent" if m.dirty else "dim", 0))
        hints = "Ctrl+S save · Ctrl+R reload · Ctrl+Z undo · Ctrl+Y redo · Ctrl+P profiles · Esc quit"
        if w - 1 - len(hints) > 25:
            self._put(self.max_y - 1, w - 1 - len(hints), hints, 0, PAIR.get("dim", 0))

    # ------------------------------------------------------------------
    # prompt
    # ------------------------------------------------------------------
    def open_prompt(self, title, initial, kind, on_done):
        if kind not in ("confirm", "int", "num", "hex", "text"):
            return
        self.prompt = {"title": str(title)[:256], "kind": kind, "on_done": on_done}
        self.prompt_text = str(initial or "")[:MAX_PROMPT_CHARS]
        self.need_refresh = True

    def draw_prompt(self):
        h, w = self.max_y, self.max_x
        ph = 4 if self.prompt["kind"] == "confirm" else 3
        pw = min(60, max(24, w - 8))
        top = max(1, (h - ph) // 2)
        left = max(1, (w - pw) // 2)
        self._fill_box(top, left, top + ph - 1, left + pw - 1, self.prompt["title"])
        body = self.prompt_text + ("█" if self.prompt["kind"] != "confirm" else "")
        self._put(top + 1, left + 2, body[: max(0, pw - 4)])
        if self.prompt["kind"] == "confirm":
            self._put(top + 2, left + 2, "y/Y confirm · n/N or Esc cancel", 0,
                      PAIR.get("dim", 0))

    def _fill_box(self, top, left, bottom, right, title):
        self._box(top, left, right, bottom, title)
        for y in range(top + 1, bottom):
            self._put(y, left + 1, " " * max(0, right - left - 1))

    def prompt_finish(self):
        p = self.prompt
        kind = p["kind"]
        value = self.prompt_text.strip()
        if kind == "int":
            try:
                value = int(value)
            except ValueError:
                self.model.status = "Not a number"
                self.prompt = None
                self.need_refresh = True
                return
        elif kind == "num":
            try:
                value = float(value)
                if not math.isfinite(value):
                    raise ValueError
            except ValueError:
                self.model.status = "Not a number"
                self.prompt = None
                self.need_refresh = True
                return
        elif kind == "hex":
            value = value.lstrip("#")
            if len(value) not in (3, 6) or any(c not in "0123456789abcdefABCDEF" for c in value):
                self.model.status = "Bad color — use RRGGBB"
                self.prompt = None
                self.need_refresh = True
                return
            if len(value) == 3:
                value = "".join(c * 2 for c in value)
            value = "#" + value
        elif kind == "text":
            if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
                self.model.status = "Invalid text"
                self.prompt = None
                self.need_refresh = True
                return
        try:
            p["on_done"](value)
        except Exception as exc:
            self.model.status = f"Action failed: {exc}"
            self.prompt = None
            self.prompt_text = ""
            self.need_refresh = True
            return
        self.prompt = None
        self.prompt_text = ""
        self.need_refresh = True

    # ------------------------------------------------------------------
    # key handling
    # ------------------------------------------------------------------
    def key(self, ch):
        ctrl = ch & 0x1F if 0 <= ch <= 0x1F else None
        if self.prompt:
            self.key_prompt(ch)
            return
        if self.ov:
            try:
                self.ov.key(self.s, ch)
            except Exception as exc:
                self.model.status = f"Action failed: {exc}"
            self.need_refresh = True
            return
        if self.search_active and self.side == "layout":
            self.key_search(ch)
            return

        if ctrl == 19:  # Ctrl+S
            try:
                self.model.save()
            except Exception as e:
                self.model.status = f"Save failed: {e}"
        elif ctrl == 18:  # Ctrl+R
            if self.model.dirty:
                self.open_prompt("Discard unsaved changes and reload?", "", "confirm",
                                 lambda v: self._do_reload())
            else:
                try:
                    self.model.reload()
                except Exception as e:
                    self.model.status = f"Reload failed: {e}"
        elif ctrl == 26:  # Ctrl+Z
            self.model.undo_step()
        elif ctrl == 25:  # Ctrl+Y
            self.model.redo_step()
        elif ch in (27, ord("q"), ord("Q")):
            self.action_quit()
        elif ch in (ord("\t"), curses.KEY_BTAB):
            self.side = "settings" if self.side == "layout" else "layout"
            self.need_refresh = True
        elif ctrl == 16:  # Ctrl+P
            self.open_profiles()
        elif ch == ord("p"):
            self.open_plugins()
        elif ch in (ord("r"), ord("R")):
            self.action_reset()
        elif ch == ord("b"):
            self.model.toggle_bar()
        elif self.side == "layout":
            self.key_layout(ch)
        else:
            self.key_settings(ch)
        self.need_refresh = True

    def key_prompt(self, ch):
        if self.prompt["kind"] == "confirm":
            if ch in (ord("y"), ord("Y")):
                self.prompt_finish()
            elif ch in (ord("n"), ord("N"), 27):
                self.prompt = None
            self.need_refresh = True
            return
        if ch == 27:
            self.prompt = None
        elif ch in (10, ord("\n"), 13):
            self.prompt_finish()
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            self.prompt_text = self.prompt_text[:-1]
        elif 32 <= ch <= 0x10FFFF and len(self.prompt_text) < MAX_PROMPT_CHARS:
            try:
                self.prompt_text += chr(ch)
            except ValueError:
                pass
        self.need_refresh = True

    def key_search(self, ch):
        if ch == 27:
            self.search_active = False
            self.search = ""
        elif ch in (10, ord("\n"), 13):
            self.search_active = False
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            self.search = self.search[:-1]
        elif 32 <= ch <= 0x10FFFF and len(self.search) < 128:
            try:
                self.search += chr(ch)
            except ValueError:
                pass
        self.need_refresh = True

    def key_layout(self, ch):
        sec = SECTIONS[self.sec_i]
        lst = self.model.layout[sec]
        if ch == ord("/"):
            self.search_active = True
            self.search = ""
            self.need_refresh = True
            return
        mod = []
        if ch in (curses.KEY_UP, ord("k"), ord("K")) and self.sel_sec is not None:
            if self._move_selected_vertical(-1):
                return
        elif ch in (curses.KEY_DOWN, ord("j"), ord("J")) and self.sel_sec is not None:
            if self._move_selected_vertical(1):
                return
        elif ch in (curses.KEY_UP, ord("k"), ord("K")):
            if self.lay_i > 0:
                self.lay_i -= 1
        elif ch in (curses.KEY_DOWN, ord("j"), ord("J")):
            if self.lay_i < len(lst) - 1:
                self.lay_i += 1
        elif ch in (curses.KEY_LEFT, ord("h"), ord("H")) and self.sel_sec is not None:
            if self._move_selected(-1):
                return
        elif ch in (curses.KEY_RIGHT, ord("l"), ord("L")) and self.sel_sec is not None:
            if self._move_selected(1):
                return
        elif ch in (curses.KEY_LEFT, ord("h"), ord("H")):
            if self.sec_i > 0:
                self.sec_i -= 1
                self.lay_i = min(self.lay_i, max(0, len(self.model.layout[SECTIONS[self.sec_i]]) - 1))
            else:
                self.side = "settings"
        elif ch in (curses.KEY_RIGHT, ord("l"), ord("L")):
            if self.sec_i < len(SECTIONS) - 1:
                self.sec_i += 1
                self.lay_i = min(self.lay_i, max(0, len(self.model.layout[SECTIONS[self.sec_i]]) - 1))
        elif ch in (ord(" "),):
            self._toggle_select(sec, self.lay_i)
        elif ch in (ord("a"), ord("A"), ord("+")):
            self.open_add(sec)
        elif ch in (curses.KEY_DC, ord("x"), ord("X"), ord("d"), ord("D")):
            self.model.remove_widget(sec, self.lay_i)
            if self.sel_sec == sec and self.sel_idx == self.lay_i:
                self.sel_sec, self.sel_idx = None, -1
            self.lay_i = max(0, min(self.lay_i, max(0, len(self.model.layout[sec]) - 1)))
        elif ch in (ord("-"), ord("_")):
            self.model.move_widget(sec, self.lay_i, -1)
        elif ch == ord("="):
            self.model.move_widget(sec, self.lay_i, 1)
        elif ch in (ord("["), ord("{")):
            if self.sec_i > 0:
                self.model.move_section(sec, self.lay_i, SECTIONS[self.sec_i - 1])
                self.sec_i -= 1
                self.lay_i = min(self.lay_i, max(0, len(self.model.layout[SECTIONS[self.sec_i]]) - 1))
        elif ch in (ord("]"), ord("}")):
            if self.sec_i < len(SECTIONS) - 1:
                self.model.move_section(sec, self.lay_i, SECTIONS[self.sec_i + 1])
                self.sec_i += 1
                self.lay_i = min(self.lay_i, max(0, len(self.model.layout[SECTIONS[self.sec_i]]) - 1))
        elif ch in (10, ord("\n"), 13):
            self.open_widget_options(sec, self.lay_i)
        elif ch in (ord("e"), ord("E")):
            self.open_widget_options(sec, self.lay_i)
        elif ch in (ord("c"), ord("C")):
            self._duplicate(sec, self.lay_i)
        self.need_refresh = True

    def _toggle_select(self, sec, idx):
        if not (0 <= idx < len(self.model.layout[sec])):
            return
        if self.sel_sec == sec and self.sel_idx == idx:
            self.sel_sec, self.sel_idx = None, -1
            self.model.status = "Selection cleared"
        else:
            self.sel_sec, self.sel_idx = sec, idx
            self.model.status = "Selected — ←/→ column · ↑/↓ reorder · Space drop"
        self.need_refresh = True

    def _move_selected(self, delta):
        if self.sel_sec is None:
            return False
        src_idx = SECTIONS.index(self.sel_sec)
        dst_idx = src_idx + delta
        if not (0 <= dst_idx < len(SECTIONS)):
            return False
        if not (0 <= self.sel_idx < len(self.model.layout[self.sel_sec])):
            self.sel_sec, self.sel_idx = None, -1
            return False
        target = SECTIONS[dst_idx]
        self.model.move_section(self.sel_sec, self.sel_idx, target)
        self.sel_sec, self.sel_idx = target, len(self.model.layout[target]) - 1
        self.sec_i, self.lay_i = dst_idx, self.sel_idx
        self.model.status = "Moved to " + SECTION_LABELS[target]
        return True

    def _move_selected_vertical(self, delta):
        if self.sel_sec is None:
            return False
        idx = self.sel_idx
        target = idx + delta
        lst = self.model.layout[self.sel_sec]
        if not (0 <= idx < len(lst)) or not (0 <= target < len(lst)):
            return False
        self.model.move_widget(self.sel_sec, idx, delta)
        self.sel_idx = target
        self.sec_i, self.lay_i = SECTIONS.index(self.sel_sec), target
        self.model.status = "Moved within " + SECTION_LABELS[self.sel_sec]
        return True

    def key_settings(self, ch):
        rows = self.settings_rows()
        self.set_i = max(0, min(len(rows) - 1, self.set_i))
        if ch in (curses.KEY_UP, ord("k"), ord("K")):
            if self.set_i > 0:
                self.set_i -= 1
        elif ch in (curses.KEY_DOWN, ord("j"), ord("J")):
            if self.set_i < len(rows) - 1:
                self.set_i += 1
        elif ch in (curses.KEY_RIGHT, ord("l"), ord("L")):
            self.side = "layout"
            self.sec_i = 0
            self.lay_i = min(self.lay_i, max(0, len(self.model.layout["left"]) - 1))
        elif ch in (10, ord("\n"), 13, ord(" ")):
            self.edit_setting(rows[self.set_i][0])
        self.need_refresh = True

    def edit_setting(self, kind):
        if kind == "--":
            return
        m = self.model
        def f(attr):
            def setter(val):
                if attr in ("screensaver", "lock", "alpha", "size_h", "size_v"):
                    try:
                        val = int(val)
                    except (TypeError, ValueError):
                        return
                    if attr in ("screensaver", "lock"):
                        val = max(0, min(86400, val))
                    elif attr == "alpha":
                        val = max(0, min(100, val))
                    else:
                        val = max(1, min(1000, val))
                elif attr in ("anchor", "font_family"):
                    val = _safe_text(val, 256)
                    if not val and attr == "anchor":
                        val = ""
                if val == getattr(m, attr):
                    return
                m.push_history()
                setattr(m, attr, val)
                m.mark_dirty()
                self.need_refresh = True
            return setter

        if kind == "position":
            self.open_dropdown("Bar position", POSITIONS, m.position, f("position"))
        elif kind == "host":
            self.open_dropdown("Bar host", hosts(m.catalog), m.host, f("host"))
        elif kind == "transparent":
            self._set_toggle("transparent", not m.transparent)
        elif kind == "scale_font":
            self._set_toggle("scale_font", not m.scale_font)
        elif kind == "anchor":
            self.open_prompt("Center anchor widget", m.anchor, "text", f("anchor"))
        elif kind == "font":
            self.open_prompt("Font family", m.font_family, "text", f("font_family"))
        elif kind == "screensaver":
            self.open_prompt("Screensaver seconds", str(m.screensaver), "int", f("screensaver"))
        elif kind == "lock":
            self.open_prompt("Lock seconds", str(m.lock), "int", f("lock"))
        elif kind == "bg":
            self.open_prompt("Background color (RRGGBB)", m.bg, "hex", f("bg"))
        elif kind == "text":
            self.open_prompt("Text color (RRGGBB)", m.text_col, "hex", f("text_col"))
        elif kind == "active":
            self.open_prompt("Active color (RRGGBB)", m.active, "hex", f("active"))
        elif kind == "alpha":
            self.open_prompt("Background alpha (0-100)", str(m.alpha), "int", f("alpha"))
        elif kind == "size_h":
            self.open_prompt("Horizontal size (px)", str(m.size_h), "int", f("size_h"))
        elif kind == "size_v":
            self.open_prompt("Vertical size (px)", str(m.size_v), "int", f("size_v"))

    def _set_toggle(self, attr, val):
        value = bool(val)
        if getattr(self.model, attr) == value:
            return
        self.model.push_history()
        setattr(self.model, attr, value)
        self.model.mark_dirty()
        self.need_refresh = True

    # ------------------------------------------------------------------
    # overlays / actions
    # ------------------------------------------------------------------
    def open_dropdown(self, title, options, current, on_pick):
        rows = [{"value": v, "label": v} for v in options]
        self.ov = ListOverlay(
            title, rows,
            lambda r: self._pick_dropdown(on_pick, r.get("value", "")),
            key={"label": "label"}, preselect=current)
        self.need_refresh = True

    def open_add(self, sec):
        opts = self.model.add_options()
        self.ov = ListOverlay(
            f"ADD WIDGET → {SECTION_LABELS[sec].upper()}", opts,
            key={"name": "name", "id": "id", "category": "category"},
            on_activate=lambda r: self._do_add(sec, r),
            empty="No matching widgets",
        )
        self.need_refresh = True

    def _do_add(self, sec, opt):
        self.model.add_widget(sec, opt["id"])
        self.ov = None
        self.need_refresh = True

    def _pick_dropdown(self, on_pick, value):
        self.ov = None
        on_pick(value)
        self.need_refresh = True

    def open_plugins(self):
        self.ov = ListOverlay(
            "PLUGINS — toggle with Enter", self.model.plugins(),
            key={"name": "name", "id": "id"},
            checkbox=True,
            on_activate=lambda r: self._toggle_plugin(r),
        )
        self.need_refresh = True

    def _toggle_plugin(self, row):
        if row.get("enabled") is None:
            self.model.status = "Plugin state is unknown"
            self.need_refresh = True
            return
        self.model.plugin_set(row.get("id"), not row["enabled"])
        if self.ov:
            self.ov.rows = self.model.plugins()
        self.need_refresh = True

    def open_widget_options(self, sec, idx):
        lst = self.model.layout[sec]
        if not (0 <= idx < len(lst)):
            return
        wid = entry_id(lst[idx])
        opts = self.model.widget_options(sec, idx)
        if not opts:
            self.model.status = f"{display_name(self.model.catalog, wid)} has no options"
            self.need_refresh = True
            return
        self.ov = ListOverlay(
            f"OPTIONS — {display_name(self.model.catalog, wid)}", opts,
            key={"label": "label", "value": "value"},
            on_activate=lambda r: self._edit_widget_option(sec, idx, r),
            empty="No options",
        )
        self.need_refresh = True

    def _edit_widget_option(self, sec, idx, row):
        otype = row.get("type") or "text"
        key = row["key"]
        if otype == "enum":
            opts = list(row.get("enum") or [])
            if not opts:
                opts = [""]
            self.open_dropdown(row["label"], opts, row.get("value"),
                               lambda v: self._apply_option(sec, idx, key, v))
            return
        if otype == "bool":
            self._apply_option(sec, idx, key, not bool(row.get("value")))
            return

        def small_apply(raw):
            v = raw.strip()
            if otype == "int":
                try:
                    v = int(v)
                except ValueError:
                    self.model.status = "Not an integer"
                    self.need_refresh = True
                    return
            else:
                try:
                    v = float(v)
                except ValueError:
                    self.model.status = "Not a number"
                    self.need_refresh = True
                    return
            lo = row.get("min")
            hi = row.get("max")
            if lo is not None:
                v = max(v, lo)
            if hi is not None:
                v = min(v, hi)
            self._apply_option(sec, idx, key, v)

        if otype in ("int", "num"):
            self.ov = None
            self.open_prompt(row["label"], str(row.get("value") or 0), "num",
                             small_apply)
            return
        self.ov = None
        self.open_prompt(row["label"], str(row.get("value") or ""), "text",
                         lambda v: self._apply_option(sec, idx, key, v))

    def _apply_option(self, sec, idx, key, value):
        self.model.apply_widget_option(sec, idx, key, value)
        self.model.status = f"{key} → {value}"
        self.need_refresh = True
        if self.ov and isinstance(self.ov, ListOverlay):
            self.ov.rows = self.model.widget_options(sec, idx)
            self.ov.sel = min(self.ov.sel, max(0, len(self.ov.rows) - 1))
        else:
            self.open_widget_options(sec, idx)

    def _duplicate(self, sec, idx):
        lst = self.model.layout[sec]
        if not (0 <= idx < len(lst)):
            return
        name = display_name(self.model.catalog, entry_id(lst[idx]))
        self.model.duplicate_widget(sec, idx)
        self.lay_i = len(self.model.layout[sec]) - 1
        self.model.status = "Duplicated " + name

    def open_profiles(self):
        self.pending_delete = None
        self.ov = ListOverlay(
            "PROFILES — Enter load · x delete · c save-as", self._profile_rows(),
            key={"label": "label"},
            on_activate=self._load_profile,
            on_delete=self._delete_profile,
            on_save=self._prompt_save_profile,
            empty="No saved profiles",
        )
        self.need_refresh = True

    def _profile_rows(self):
        return [{"name": n, "label": n} for n in self.model.refresh_profiles()]

    def _load_profile(self, row):
        name = row.get("name") if isinstance(row, dict) else ""
        if not _valid_profile_name(name):
            self.model.status = "Invalid profile name"
            return
        if self.model.dirty:
            self.open_prompt("Discard unsaved changes and load profile?", "", "confirm",
                             lambda v: self._do_load_profile(name))
            return
        self._do_load_profile(name)

    def _do_load_profile(self, name):
        try:
            self.model.load_profile(name, force=True)
        except Exception as e:
            self.model.status = f"{e}" if str(e) else "Profile load failed"
        self.ov = None
        self.need_refresh = True

    def _prompt_save_profile(self):
        self.ov = None
        self.open_prompt("Save profile as:", "", "text", self._do_save_profile)

    def _do_save_profile(self, name):
        if not name:
            self.model.status = "Profile name required"
            self.open_profiles()
            self.need_refresh = True
            return
        try:
            self.model.save_profile(name)
            self.model.status = f"Profile {name} saved"
        except Exception as e:
            self.model.status = f"Save failed: {e}"
        self.open_profiles()
        self.need_refresh = True

    def _delete_profile(self, row):
        name = row.get("name") if isinstance(row, dict) else ""
        if not _valid_profile_name(name):
            self.model.status = "Invalid profile name"
            return
        self.open_prompt(f"Delete profile {name}?", "", "confirm",
                         lambda v: self._do_delete_profile(name))
        self.need_refresh = True

    def _do_delete_profile(self, name):
        try:
            self.model.delete_profile(name)
            self.model.status = f"Profile {name} deleted"
        except Exception as e:
            self.model.status = f"Delete failed: {e}"
        self.pending_delete = None
        self.ov = None
        self.need_refresh = True

    def action_reset(self):
        self.open_prompt("Reset bar to Omarchy defaults?", "", "confirm",
                         lambda v: self._do_reset())

    def _do_reset(self):
        try:
            self.model.reset_bar(force=True)
        except Exception as e:
            self.model.status = f"Reset failed: {e}"
        self.need_refresh = True

    def action_quit(self):
        if self.model.dirty:
            self.open_prompt("Discard unsaved changes and quit?", "", "confirm",
                             lambda v: self._done())
        else:
            self.running = False

    def _do_reload(self):
        try:
            self.model.reload(force=True)
        except Exception as e:
            self.model.status = f"Reload failed: {e}"
        self.need_refresh = True

    def _done(self):
        self.running = False

    # ------------------------------------------------------------------
    # mouse
    # ------------------------------------------------------------------
    def mouse(self, event):
        try:
            _, mx, my, _, bstate = event
        except (TypeError, ValueError):
            try:
                mx, my, bstate = event.x, event.y, event.bstate
            except AttributeError:
                return
        if mx < 0 or mx >= self.max_x or my < 0 or my >= self.max_y:
            return
        if self.prompt or self.ov:
            if self.ov:
                try:
                    self.ov.mouse(self.s, my, mx, bstate)
                except Exception as exc:
                    self.model.status = f"Action failed: {exc}"
                self.need_refresh = True
            return

        # drag-and-drop: press on a layout row, release over another row
        # moves the widget there (over the same row is a plain click)
        if bstate & curses.BUTTON1_PRESSED:
            self.drag_sec, self.drag_idx = self._row_at(my, mx)
            self.drag_oy, self.drag_ox = my, mx
            return
        if bstate & curses.BUTTON1_RELEASED:
            if self.drag_sec is not None and self.drag_ox >= 0:
                src = (self.drag_sec, self.drag_idx)
                self.drag_sec, self.drag_idx = None, -1
                dst = self._row_at(my, mx)
                if dst[0] and dst != src:
                    self._complete_drag(src, dst)
                    return
            return

        st = self.settings_rect
        if st[0] <= my <= st[3] and st[1] <= mx <= st[2]:
            self.side = "settings"
            rows = self.settings_rows()
            ridx = self.set_scroll + (my - st[0] - 1)
            if 0 <= self.set_scroll <= len(rows) - 1 and 0 <= ridx < len(rows):
                self.set_i = ridx
                if bstate & curses.BUTTON1_CLICKED and rows[ridx][0] != "--":
                    self.edit_setting(rows[ridx][0])
            self.need_refresh = True
            return

        for si, rects in enumerate(self.row_rects):
            for kind, real, ry, lx, rx in rects:
                if ry == my and lx <= mx <= rx:
                    self.side = "layout"
                    self.sec_i = si
                    if kind == "add":
                        self.lay_i = len(self.model.layout[SECTIONS[si]])
                        if bstate & curses.BUTTON1_CLICKED:
                            self.open_add(SECTIONS[si])
                    else:
                        self.lay_i = real
                    self.need_refresh = True
                    return

        # scroll wheels
        if bstate & (curses.BUTTON4_PRESSED | curses.BUTTON5_PRESSED):
            self.scroll_wheel(bstate)

    def scroll_wheel(self, bstate):
        if self.side == "settings":
            self.set_i += 1 if bstate & curses.BUTTON5_PRESSED else -1
            self.set_i = max(0, min(len(self.settings_rows()) - 1, self.set_i))
        else:
            sec = SECTIONS[self.sec_i]
            self.lay_i += 1 if bstate & curses.BUTTON5_PRESSED else -1
            self.lay_i = max(0, min(len(self.model.layout[sec]) - 1, self.lay_i))
        self.need_refresh = True

    def _row_at(self, my, mx):
        st = self.settings_rect
        if st[0] <= my <= st[3] and st[1] <= mx <= st[2]:
            return (None, -1)
        for si, rects in enumerate(self.row_rects):
            for kind, real, ry, lx, rx in rects:
                if ry == my and lx <= mx <= rx:
                    if kind == "add":
                        return (SECTIONS[si], len(self.model.layout[SECTIONS[si]]))
                    return (SECTIONS[si], real)
        return (None, -1)

    def _complete_drag(self, src, dst):
        src_sec, src_idx = src
        dst_sec, dst_idx = dst
        if src_sec is None or dst_sec is None:
            return
        if src_sec == dst_sec and src_idx == dst_idx:
            return
        pos = self.model.drop_widget(src_sec, src_idx, dst_sec, dst_idx)
        if pos is None:
            return
        self.sec_i = SECTIONS.index(dst_sec)
        self.lay_i = pos
        if self.sel_sec == src_sec and self.sel_idx == src_idx:
            self.sel_sec, self.sel_idx = dst_sec, pos
        name = display_name(self.model.catalog, entry_id(self.model.layout[dst_sec][pos]))
        self.model.status = "Moved " + name
        self.need_refresh = True


class ListOverlay:
    """Generic searchable list overlay (add-widget, dropdown, plugins)."""

    def __init__(self, title, rows, on_activate=None, key=None, empty="No items",
                 preselect=None, checkbox=False, on_delete=None, on_save=None):
        self.title = title
        self.rows = rows
        self.on_activate = on_activate or (lambda r: None)
        self.fields = key or {"label": "label"}
        self.empty = empty
        self.checkbox = checkbox
        self.on_delete = on_delete
        self.on_save = on_save
        self.filter = ""
        self.sel = 0
        self.scroll = 0
        self.filtering = True
        self.need_close = False
        if preselect:
            for i, r in enumerate(rows):
                if r.get("value") == preselect or r.get("id") == preselect:
                    self.sel = i
                    break

    def filtered(self):
        f = self.filter.lower()
        if not f:
            return self.rows
        return [r for r in self.rows if f in self._hay(r)]

    def _hay(self, r):
        s = ""
        for v in (self.fields or {}).values():
            s += " " + str(r.get(v, ""))
        return s.lower()

    def _label(self, r):
        parts = [str(r.get(k)) for k in (self.fields or {}).values() if r.get(k) is not None]
        return " · ".join(parts)

    def _put(self, s, y, x, text, attr=0):
        try:
            s.addnstr(y, x, text, max(0, s.getmaxyx()[1] - x), attr)
        except curses.error:
            pass

    def draw(self, s):
        h, w = s.getmaxyx()
        if h < 6 or w < 20:
            return
        ph = max(4, min(18, h - 2))
        pw = max(20, min(72, w - 2))
        pw = min(pw, w - 2)
        top = max(0, min(h - ph - 1, (h - ph) // 2))
        left = max(0, min(w - pw - 1, (w - pw) // 2))
        bot = top + ph - 1

        # body
        self._put(s, top, left, "┌" + "─" * (pw - 2) + "┐")
        self._put(s, top + 1, left, f"│ {self.title[: pw - 4].ljust(pw - 4)} │")
        self._put(s, top + 2, left, f"│ {('Search: ' + self.filter + '▏').ljust(pw - 4)[: pw - 4]} │")

        items = self.filtered()
        body_h = ph - 4
        if self.sel < self.scroll:
            self.scroll = self.sel
        if self.sel >= self.scroll + body_h:
            self.scroll = self.sel - body_h + 1
        self.scroll = max(0, min(self.scroll, max(0, len(items) - body_h)))

        for i in range(body_h):
            idx = self.scroll + i
            y = top + 3 + i
            if idx >= len(items):
                break
            r = items[idx]
            mark = "▸" if idx == self.sel else " "
            cb = ("?" if r.get("enabled") is None else ("✓" if r.get("enabled") else "·")) if self.checkbox else ""
            line = f"│ {mark} {cb} {self._label(r)}".ljust(pw - 1) + "│"
            if idx == self.sel:
                self._put(s, y, left, line, curses.A_REVERSE)
            else:
                self._put(s, y, left, line)
        if not items:
            self._put(s, top + 3, left, f"│ {self.empty}".ljust(pw - 1) + "│")
        self._put(s, bot, left, "└" + "─" * (pw - 2) + "┘")
        try:
            s.refresh()
        except curses.error:
            pass

    def key(self, s, ch):
        items = self.filtered()
        if ch == 27:
            if self.filter:
                self.filter = ""
                self.sel = 0
            else:
                self.need_close = True
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            self.filter = self.filter[:-1]
            self.sel = min(self.sel, max(0, len(items) - 1))
        elif ch in (10, ord("\n"), 13):
            if items and 0 <= self.sel < len(items):
                self.on_activate(items[self.sel])
        elif ch in (ord("x"), ord("X"), ord("d"), ord("D")) and self.on_delete:
            if items and 0 <= self.sel < len(items):
                self.on_delete(items[self.sel])
        elif ch in (ord("c"), ord("C"), ord("s"), ord("S")) and self.on_save:
            self.on_save()
        elif ch in (curses.KEY_UP, ord("k"), ord("K")):
            if self.sel > 0:
                self.sel -= 1
        elif ch in (curses.KEY_DOWN, ord("j"), ord("J")):
            self.sel = min(max(0, len(items) - 1), self.sel + 1)
        elif 32 <= ch <= 0x10FFFF and len(self.filter) < 256:
            try:
                self.filter += chr(ch)
            except ValueError:
                pass
            self.sel = min(self.sel, max(0, len(items) - 1))

    def mouse(self, s, my, mx, bstate):
        if bstate & (curses.BUTTON4_PRESSED | curses.BUTTON5_PRESSED):
            d = 1 if bstate & curses.BUTTON5_PRESSED else -1
            self.sel = max(0, min(len(self.filtered()) - 1, self.sel + d))
            return
        if bstate & curses.BUTTON1_CLICKED:
            # row under the cursor: approximated from the box geometry
            # re-drawn top is derived again (cheap) — handled by caller via key
            pass


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def init_colors():
    curses.use_default_colors()
    bg = -1
    curses.start_color()
    for i, name in enumerate(PAIR_ORDER, start=1):
        fg = i if name != "dim" else 8
        try:
            curses.init_pair(i, fg, bg)
            PAIR[name] = i
        except curses.error:
            PAIR[name] = 0
    FAKE = {"accent": "cyan", "urgent": "red"}
    for k, v in FAKE.items():
        PAIR.setdefault(k, PAIR.get(v, 0))


def _run_app(stdscr):
    if curses.has_colors():
        init_colors()
    try:
        curses.mousemask(
            curses.BUTTON1_CLICKED | curses.BUTTON1_DOUBLE_CLICKED |
            curses.BUTTON1_PRESSED | curses.BUTTON1_RELEASED |
            curses.BUTTON4_PRESSED | curses.BUTTON5_PRESSED)
    except curses.error:
        pass

    model = Model()
    try:
        model.load()
    except Exception as e:
        model.status = f"Load failed: {e}"

    app = BarEditorTUI(stdscr, model)
    while app.running:
        if app.need_refresh:
            app.draw()
        ch = stdscr.getch()
        if ch == curses.KEY_MOUSE:
            try:
                app.mouse(curses.getmouse())
            except curses.error:
                pass
            continue
        if ch == -1:
            continue
        app.key(ch)
        if app.ov and getattr(app.ov, "need_close", False):
            app.ov = None
            app.need_refresh = True


def _main(stdscr):
    old_attrs = None
    fd = None
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    try:
        fd = sys.stdin.fileno()
        old_attrs = termios.tcgetattr(fd)
        attrs = termios.tcgetattr(fd)
        attrs[0] &= ~(termios.IXON | termios.IXOFF)
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except (termios.error, OSError, ValueError):
        old_attrs = None
    try:
        if _shell_io is not None:
            with _shell_io.instance_lock():
                _run_app(stdscr)
        else:
            _run_app(stdscr)
    finally:
        if old_attrs is not None and fd is not None:
            try:
                termios.tcsetattr(fd, termios.TCSANOW, old_attrs)
            except (termios.error, OSError):
                pass


def run_tui():
    curses.wrapper(_main)


if __name__ == "__main__":
    try:
        run_tui()
    except KeyboardInterrupt:
        pass
    except Exception:
        import traceback
        print(file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)