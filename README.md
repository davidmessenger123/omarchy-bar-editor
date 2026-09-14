# Omarchy Bar Editor

A terminal (TUI) editor for the Omarchy status bar: layout widgets across the
left/center/right sections, bar position and transparency, center anchor, idle
timeouts, and the `[bar]` styling block in `shell.toml`. Mouse-aware,
htop-style, and fully self-contained (stdlib `curses` only).

Click the **Bar Editor** icon in the bar (or launch it from the menu /
`~/.local/share/applications/omarchy-bar-editor.desktop`) to open the editor
in a real terminal window. Layout changes save to
`~/.config/omarchy/shell.json` and are hot-reloaded by the shell.

## Install

From the Omarchy plugin marketplace (via the `omarchy plugin` CLI), or
manually:

```sh
git clone https://github.com/davidmessenger123/omarchy-bar-editor.git \
  ~/.config/omarchy/plugins/davidjm.bar-editor
omarchy plugin enable davidjm.bar-editor
omarchy bar put davidjm.bar-editor
```

## Requirements

- [Omarchy](https://omarchy.org) (for the `omarchy` and
  `omarchy-plugin-catalog` commands) with the shell runtime.
- Python 3.8+ — the TUI needs nothing else.

## Files the editor manages

| File | Purpose |
|---|---|
| `~/.config/omarchy/shell.json` | Bar layout, widget options, idle |
| `~/.config/omarchy/shell.json.bak-editor` | Backup created on every save |
| `~/.config/omarchy/shell.toml` | `[bar]` colors, alpha, sizing |
| `~/.config/omarchy/shell.toml.bak-editor` | Backup created on every save |
| `~/.config/omarchy/bar-profiles/` | Saved profiles (`Ctrl+P`) |

All writes go through `shell_io.py` (atomic replace, symlink-safe), never from
the UI directly.

## Features

- Bar position, transparency, center anchor
- Screensaver / lock idle timeouts
- Widget layout editing for the left / center / right sections:
  add, remove, reorder (up/down), and move between sections
- Add-widget search overlay (filters the installed widget catalog)
- Per-widget option editing (`e`/`Enter`): schema-driven settings for any
  widget — dropdowns for enums, toggles for booleans, numeric/text prompts
- Duplicate a widget (`c`)
- `[bar]` styling in `shell.toml`: colors, background alpha, sizing,
  scale-with-font
- Undo/redo (100-step), save/reload
- Bar show/hide, plugin enable/disable, reset bar to defaults
- Bar profiles (`Ctrl+P`): save the current bar as a named profile, load or
  delete saved ones
- Mouse support: click rows to select, click a section to focus it, scroll to
  browse long lists, and **drag-and-drop** a widget onto another row or into a
  new section to move it
- Select-and-move: press Space to grab a widget, then use `←/→` (or `h/l`)
  to move it into a different column and `↑/↓` (or `j/k`) to reorder it within
  its column; Space again to drop

## Keys

| Key | Action |
|---|---|
| `Tab` | switch between the layout and settings panes |
| `hjkl` / arrows | move cursor; ← at the left edge enters the settings pane, → returns to the layout |
| `Space` | select a widget (marked with ▸); with a widget selected, `←/→` move it into the neighbouring column and `↑/↓` reorder it within its column |
| `Enter` | edit the selected setting, or edit the focused widget's options |
| `e` | edit the focused widget's options |
| `a` / `+` | add a widget to the focused section |
| `c` | duplicate the focused widget |
| `x` / `Delete` | remove the selected widget |
| `-` / `=` | move the widget up / down in its section |
| `[` / `]` | move the widget to the previous / next section |
| `p` | plugin enable/disable |
| `r` | reset the bar to defaults (confirm) |
| `b` | toggle bar visibility |
| `Ctrl+P` | profiles: `Enter` loads, `x` deletes (press twice), `c` saves as |
| `Ctrl+S` | save (shell reloads automatically) |
| `Ctrl+R` | reload from disk |
| `Ctrl+Z` / `Ctrl+Y` | undo / redo |
| `Esc` / `q` | close overlay, or quit (after confirming unsaved work) |

## Development

- `editor_tui.py` — the whole TUI (model + curses UI, stdlib only)
- `shell_io.py` — the only process that writes to `shell.json` / `shell.toml`;
  a hardened, non-UI boundary invoked via subprocess

Validate locally:

```sh
omarchy plugin validate .
python3 -m py_compile editor_tui.py
```

## License

MIT — see [LICENSE](LICENSE).