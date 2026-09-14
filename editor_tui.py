#!/usr/bin/env python3
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
    Enter             edit selected setting / confirm
    Space             select a widget in the layout (highlighted ▸);
                      with a widget selected, ←/→ or h/l move it between
                      columns, ↑/↓ or j/k reorder it within its column
    a or +            add widget to the focused section
    x or Delete       remove selected widget
    - / =             move widget up / down within its section
    [ / ]             move widget to previous / next section
    p                 plugin enable/disable
    Ctrl+S save  Ctrl+R reload  Ctrl+Z undo  Ctrl+Y redo
    Esc               close overlay / quit (after confirming unsaved work)
"""

import curses
import json
import os
import subprocess
import sys
import termios

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
SHELL_IO = os.path.join(PLUGIN_DIR, "shell_io.py")

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


def cat_pair(category):
    name = CAT_COLORS.get((category or "").lower())
    if name is None:
        name = CAT_STD[hash(category or "") % len(CAT_STD)]
    return PAIR.get(name, 0)


# ---------------------------------------------------------------------------
# config / catalog I/O
# ---------------------------------------------------------------------------

def run(cmd, payload=None):
    try:
        p = subprocess.run(
            cmd,
            input=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)
    return p.returncode, p.stdout, p.stderr


def io_read():
    rc, out, err = run([sys.executable, SHELL_IO, "read"])
    if rc != 0 or not out:
        raise RuntimeError(err.strip() or "shell_io read failed")
    return json.loads(out)


def io_write(shell, toml_values):
    payload = {}
    if shell is not None:
        payload["shell"] = shell
    if toml_values is not None:
        payload["toml"] = {"values": toml_values}
    rc, out, err = run([sys.executable, SHELL_IO, "write"], payload)
    if rc != 0:
        raise RuntimeError(err.strip() or "shell_io write failed")


def load_catalog():
    rc, out, err = run(["omarchy-plugin-catalog"])
    if rc != 0 or not out:
        return {}
    try:
        entries = json.loads(out)
    except json.JSONDecodeError:
        return {}
    return {e["id"]: e for e in entries if e and e.get("id")}


def load_plugin_states():
    rc, out, err = run(["omarchy", "plugin", "list", "--json"])
    if rc != 0 or not out:
        return {}
    try:
        entries = json.loads(out)
    except json.JSONDecodeError:
        return {}
    return {e["id"]: e["enabled"] for e in entries if e and e.get("enabled") is not None}


# ---------------------------------------------------------------------------
# pure helpers (mirror Editor.js)
# ---------------------------------------------------------------------------

def ensure_cfg(cfg):
    if not isinstance(cfg, dict):
        cfg = {}
    cfg.setdefault("bar", {})
    layout = cfg["bar"].setdefault("layout", {})
    for sec in SECTIONS:
        if not isinstance(layout.get(sec), list):
            layout[sec] = []
    cfg["version"] = 1
    return cfg


def widget_info(catalog, wid):
    return (catalog or {}).get(wid, {}) or {}


def display_name(catalog, wid):
    info = widget_info(catalog, wid)
    bw = info.get("barWidget") or {}
    return bw.get("displayName") or info.get("name") or wid


def category_of(catalog, wid):
    bw = widget_info(catalog, wid).get("barWidget") or {}
    return bw.get("category") or "Utilities"


def allow_multiple(catalog, wid):
    bw = widget_info(catalog, wid).get("barWidget") or {}
    v = bw.get("allowMultiple")
    return True if v is None else bool(v)


def is_bar_widget(info):
    return bool(info) and "bar-widget" in (info.get("kinds") or [])


def hosts(catalog):
    result = ["omarchy.bar"]
    ids = []
    for wid, info in (catalog or {}).items():
        if wid == "omarchy.bar":
            continue
        if "bar" not in (info.get("kinds") or []):
            continue
        if not info.get("barPath"):
            continue
        ids.append(wid)
    return result + sorted(ids)


def entry_id(entry):
    return entry.get("id") if isinstance(entry, dict) else entry


def _num_str(values, key, default):
    v = values.get(key)
    if v is None:
        return default
    if isinstance(v, bool):
        return str(v).lower()
    return str(v)


# ---------------------------------------------------------------------------
# application model
# ---------------------------------------------------------------------------

class Model:
    def __init__(self):
        self.catalog = {}
        self.states = {}
        self.cfg = {}
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

        self.undo = []
        self.redo = []
        self.undo_limit = 100

    # -- snapshots ---------------------------------------------------------
    def snapshot(self):
        return {
            k: (json.loads(json.dumps(v)) if isinstance(v, (dict, list)) else v)
            for k, v in self.__dict__.items()
            if k in (
                "cfg", "toml", "position", "transparent", "anchor", "host",
                "font_family", "screensaver", "lock", "bg", "text_col",
                "active", "alpha", "size_h", "size_v", "scale_font", "layout",
            )
        }

    def restore(self, snap):
        for k, v in snap.items():
            setattr(self, k, v)

    def push_history(self):
        self.undo.append(self.snapshot())
        self.redo.clear()
        if len(self.undo) > self.undo_limit:
            self.undo.pop(0)

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
    def load(self):
        data = io_read()
        self.cfg = ensure_cfg(data.get("cfg") or {})
        self.toml = dict(data.get("toml") or {})
        try:
            self.catalog = load_catalog()
            self.states = load_plugin_states()
        except Exception:
            self.catalog, self.states = {}, {}
        self.apply_from_cfg()

    def apply_from_cfg(self):
        bar = self.cfg.get("bar") or {}
        self.position = bar.get("position") or "top"
        self.transparent = bar.get("transparent") is True
        self.anchor = bar.get("centerAnchor") or ""
        self.host = bar.get("host") or "omarchy.bar"
        self.font_family = bar.get("fontFamily") or ""
        self.scale_font = bar.get("scaleWithFont") is not False

        idle = self.cfg.get("idle") or {}
        self.screensaver = idle.get("screensaver") or 0
        self.lock = idle.get("lock") or 0

        tv = self.toml.get("values") or {}
        self.bg = _num_str(tv, "background", DEFAULT_TOML["background"])
        self.text_col = _num_str(tv, "text", DEFAULT_TOML["text"])
        self.active = _num_str(tv, "active", DEFAULT_TOML["active"])
        try:
            self.alpha = int(float(tv.get("background_alpha", 1.0)) * 100)
        except (TypeError, ValueError):
            self.alpha = 100
        self.alpha = max(0, min(100, self.alpha))
        try:
            self.size_h = int(tv.get("size_horizontal", 26))
            self.size_v = int(tv.get("size_vertical", 28))
        except (TypeError, ValueError):
            self.size_h, self.size_v = 26, 28
        sf = _num_str(tv, "scale_with_font", "true")
        self.scale_font = str(sf).strip().lower() != "false"

        for sec in SECTIONS:
            raw = (bar.get("layout") or {}).get(sec) or []
            self.layout[sec] = [e if isinstance(e, dict) else {"id": e} for e in raw]

        self.dirty = False
        self.status = "Ready"

    def gather_cfg(self):
        cfg = ensure_cfg(self.cfg)
        bar = cfg["bar"]
        layout = bar.setdefault("layout", {})
        for sec in SECTIONS:
            layout[sec] = [dict(e) if isinstance(e, dict) else {"id": e} for e in self.layout[sec]]

        def put(dst, val, default):
            if val != default:
                bar[dst] = val
            elif dst in bar:
                del bar[dst]

        put("position", self.position or "top", "top")
        put("transparent", bool(self.transparent), False)
        put("centerAnchor", self.anchor or "", "")
        put("host", self.host or "omarchy.bar", "omarchy.bar")
        put("fontFamily", self.font_family or "", "")
        put("scaleWithFont", self.scale_font, True)

        if self.screensaver or self.lock or "idle" in cfg:
            idle = cfg.setdefault("idle", {})
            idle["screensaver"] = self.screensaver
            idle["lock"] = self.lock
        return cfg

    def gather_toml(self):
        return {
            "background": self.bg,
            "background_alpha": self.alpha / 100.0,
            "text": self.text_col,
            "active": self.active,
            "scale_with_font": self.scale_font,
            "size_horizontal": self.size_h,
            "size_vertical": self.size_v,
        }

    def save(self):
        io_write(self.gather_cfg(), self.gather_toml())
        self.dirty = False
        self.status = "Saved — the shell reloads automatically"

    def reset_bar(self):
        rc, out, err = run(["omarchy", "bar", "defaults"])
        if rc != 0:
            self.status = "Reset failed"
            return
        self.load()
        self.status = "Reset to default Omarchy bar"

    def toggle_bar(self):
        rc, out, err = run(["omarchy", "toggle", "bar"])
        self.status = "Bar visibility toggled" if rc == 0 else "Toggle failed"

    def reload(self):
        self.load()
        self.status = "Reloaded from disk"

    def plugin_set(self, pid, enable):
        rc, out, err = run(
            ["omarchy", "plugin", "enable" if enable else "disable", pid])
        if rc == 0:
            try:
                self.states = load_plugin_states()
            except Exception:
                pass
            self.status = f"{pid} {'enabled' if enable else 'disabled'}"
        else:
            self.status = f"Could not {'enable' if enable else 'disable'} {pid}"

    # -- layout edits ------------------------------------------------------
    def mark_dirty(self):
        self.dirty = True
        self.status = "Unsaved changes"

    def add_widget(self, sec, wid):
        self.push_history()
        self.layout[sec].append({"id": wid})
        self.mark_dirty()

    def remove_widget(self, sec, idx):
        if not (0 <= idx < len(self.layout[sec])):
            return
        self.push_history()
        del self.layout[sec][idx]
        self.mark_dirty()

    def move_widget(self, sec, idx, delta):
        lst = self.layout[sec]
        target = idx + delta
        if not (0 <= idx < len(lst)) or not (0 <= target < len(lst)):
            return
        self.push_history()
        lst[idx], lst[target] = lst[target], lst[idx]
        self.mark_dirty()

    def move_section(self, sec, idx, target_sec):
        if not target_sec or target_sec == sec:
            return
        if not (0 <= idx < len(self.layout[sec])):
            return
        self.push_history()
        item = self.layout[sec].pop(idx)
        self.layout[target_sec].append(item)
        self.mark_dirty()

    # -- display data ------------------------------------------------------
    def add_options(self):
        placed = {}
        for sec in SECTIONS:
            for e in self.layout[sec]:
                wid = entry_id(e)
                if wid:
                    placed[wid] = placed.get(wid, 0) + 1
        out = []
        for wid, info in (self.catalog or {}).items():
            if not is_bar_widget(info):
                continue
            if self.states.get(wid) is False:
                continue
            if not allow_multiple(self.catalog, wid) and placed.get(wid, 0) > 0:
                continue
            bw = info.get("barWidget") or {}
            out.append({
                "id": wid,
                "name": bw.get("displayName") or info.get("name") or wid,
                "category": bw.get("category") or "Utilities",
                "desc": bw.get("description") or info.get("description") or "",
            })
        out.sort(key=lambda r: r["name"].lower())
        return out

    def plugins(self):
        out = []
        for wid, info in (self.catalog or {}).items():
            if "bar-widget" not in (info.get("kinds") or []) and \
               "overlay" not in (info.get("kinds") or []):
                continue
            if wid == "davidjm.bar-editor":
                continue
            bw = info.get("barWidget") or {}
            out.append({
                "id": wid,
                "name": bw.get("displayName") or info.get("name") or wid,
                "enabled": self.states.get(wid) is not False,
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
        self.search = ""
        self.search_active = False

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
            self.s.addnstr(0, 0, "Terminal too small", 30)
            self.s.refresh()
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
        hints = "  ←/→ pane/column · Tab pane · a add · x remove · space select · p plugins "
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
        shown = [
            i for i, e in enumerate(lst) if not hay or
            hay in (str(entry_id(e)) + " " + display_name(self.model.catalog, entry_id(e))).lower()
        ]

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
            name = display_name(self.model.catalog, ident)
            mark = "▸" if carried else " "
            line = mark + " " + name
            if len(line) > usable:
                line = (mark + " " + name)[: usable - 1] + "…"
            if carried:
                self._put(y, left, line, curses.A_BOLD, PAIR.get("accent", 0))
            elif selected:
                self._put(y, left, line, curses.A_REVERSE)
            else:
                self._put(y, left, line, 0, cat_pair(category_of(self.model.catalog, ident)))
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
        hints = "Ctrl+S save · Ctrl+R reload · Ctrl+Z undo · Ctrl+Y redo · Esc quit"
        if w - 1 - len(hints) > 25:
            self._put(self.max_y - 1, w - 1 - len(hints), hints, 0, PAIR.get("dim", 0))

    # ------------------------------------------------------------------
    # prompt
    # ------------------------------------------------------------------
    def open_prompt(self, title, initial, kind, on_done):
        self.prompt = {"title": title, "kind": kind, "on_done": on_done}
        self.prompt_text = initial
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
        text = self.prompt_text.strip()
        if kind == "int":
            try:
                text = int(text)
            except ValueError:
                self.model.status = "Not a number"
                self.prompt = None
                self.need_refresh = True
                return
        elif kind == "hex":
            text = text.strip().lstrip("#")
            if len(text) not in (3, 6) or any(c not in "0123456789abcdefABCDEF" for c in text):
                self.model.status = "Bad color — use RRGGBB"
                self.prompt = None
                self.need_refresh = True
                return
            if len(text) == 3:
                text = "".join(c * 2 for c in text)
            text = "#" + text
        p["on_done"](text)
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
            self.ov.key(self.s, ch)
            return

        if ctrl == 19:  # Ctrl+S
            try:
                self.model.save()
            except Exception as e:
                self.model.status = f"Save failed: {e}"
        elif ctrl == 18:  # Ctrl+R
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
        elif ch == ord("p"):
            self.open_plugins()
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
        elif 32 <= ch <= 0x10FFFF:
            try:
                self.prompt_text += chr(ch)
            except ValueError:
                pass
        self.need_refresh = True

    def key_layout(self, ch):
        sec = SECTIONS[self.sec_i]
        lst = self.model.layout[sec]
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
        m = self.model
        def f(attr):
            def setter(val):
                if attr in ("screensaver", "lock", "alpha", "size_h", "size_v"):
                    try:
                        val = int(val)
                    except (TypeError, ValueError):
                        return
                    if attr == "alpha":
                        val = max(0, min(100, val))
                cur = getattr(m, attr)
                if val != cur:
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
        self.model.push_history()
        setattr(self.model, attr, bool(val))
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
        on_pick(value)
        self.ov = None
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
        self.model.plugin_set(row["id"], not row["enabled"])
        if self.ov:
            self.ov.rows = self.model.plugins()
        self.need_refresh = True

    def action_quit(self):
        if self.model.dirty:
            self.open_prompt("Discard unsaved changes and quit?", "", "confirm",
                             lambda v: self._done())
        else:
            self.running = False

    def _done(self):
        self.running = False

    # ------------------------------------------------------------------
    # mouse
    # ------------------------------------------------------------------
    def mouse(self, event):
        try:
            mx, my, bstate = event.x, event.y, event.bstate
        except AttributeError:
            return
        if mx < 0 or mx >= self.max_x or my < 0 or my >= self.max_y:
            return
        if self.prompt or self.ov:
            if self.ov:
                self.ov.mouse(self.s, my, mx, bstate)
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


class ListOverlay:
    """Generic searchable list overlay (add-widget, dropdown, plugins)."""

    def __init__(self, title, rows, on_activate=None, key=None, empty="No items",
                 preselect=None, checkbox=False):
        self.title = title
        self.rows = rows
        self.on_activate = on_activate or (lambda r: None)
        self.fields = key or {"label": "label"}
        self.empty = empty
        self.checkbox = checkbox
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

    def draw(self, s):
        h, w = s.getmaxyx()
        ph = max(6, min(18, h - 6))
        pw = max(30, min(min(72, w - 8), w - 1))
        pw = min(pw, w - 1)
        top = max(1, (h - ph) // 2)
        left = max(1, (w - pw) // 2)
        bot = top + ph - 1

        # body
        s.addstr(top, left, "┌" + "─" * (pw - 2) + "┐")
        s.addnstr(top + 1, left, f"│ {self.title[: pw - 4].ljust(pw - 4)} │", w, 0)
        s.addnstr(top + 2, left, f"│ {('Search: ' + self.filter + '▏').ljust(pw - 4)[: pw - 4]} │", w, 0)

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
            cb = ("✓" if r.get("enabled") else "·") if self.checkbox else ""
            line = f"│ {mark} {cb} {self._label(r)}".ljust(pw - 1) + "│"
            try:
                if idx == self.sel:
                    s.addnstr(y, left, line, w, curses.A_REVERSE)
                else:
                    s.addnstr(y, left, line, w, 0)
            except curses.error:
                pass
        if not items:
            s.addnstr(top + 3, left, f"│ {self.empty}".ljust(pw - 1) + "│", w, 0)
        s.addnstr(bot, left, "└" + "─" * (pw - 2) + "┘", w, 0)
        s.refresh()

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
        elif ch in (curses.KEY_UP, ord("k"), ord("K")):
            if self.sel > 0:
                self.sel -= 1
        elif ch in (curses.KEY_DOWN, ord("j"), ord("J")):
            self.sel = min(len(items) - 1, self.sel + 1)
        elif 32 <= ch <= 0x10FFFF:
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


def _main(stdscr):
    curses.curs_set(0)
    # disable IXON so Ctrl+S / Ctrl+Q reach curses (not swallowed as XON/XOFF)
    try:
        fd = sys.stdin.fileno()
        attrs = termios.tcgetattr(fd)
        attrs[0] &= ~(termios.IXON | termios.IXOFF)
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except termios.error:
        pass
    if curses.has_colors():
        init_colors()
    curses.mousemask(
        curses.BUTTON1_CLICKED | curses.BUTTON1_DOUBLE_CLICKED |
        curses.BUTTON4_PRESSED | curses.BUTTON5_PRESSED)

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