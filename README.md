# Omarchy Bar Editor

A native Omarchy shell plugin that visually edits the status bar: layout
widgets across the left/center/right sections, bar position and transparency,
center anchor, idle timeouts, `[bar]` styling in `shell.toml`, and plugins.

Click the **Bar Editor** icon in the bar to open a full-screen editor. Layout
changes save to `~/.config/omarchy/shell.json` and are hot-reloaded by the
shell.

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

- [Omarchy](https://omarchy.org) (for the shell, `omarchy` and
  `omarchy-plugin-catalog` commands), plus its Qt Quick shell runtime.

## Files the editor manages

| File | Purpose |
|---|---|
| `~/.config/omarchy/shell.json` | Bar layout, widget options, idle, plugins |
| `~/.config/omarchy/shell.json.bak-editor` | Backup created on every save |
| `~/.config/omarchy/shell.toml` | `[bar]` colors, alpha, sizing |
| `~/.config/omarchy/shell.toml.bak-editor` | Backup created on every save |

All writes go through `shell_io.py` (atomic replace, symlink-safe), never done
from QML directly.

## Features

- Bar position, transparency, center anchor
- Screensaver / lock idle timeouts
- Widget layout editing for the left / center / right sections:
  add, remove, reorder (up/down), and move between sections
- Add-widget global search with per-section "Add widget" dropdowns
- `[bar]` styling in `shell.toml`: colors, background alpha, sizing, scale-with-font
- Undo/redo (100-step), save/reload, reset-to-defaults with confirmation
- Bar show/hide, plugin enable/disable

## Development

- `BarEditor.qml` — the bar widget and full-screen editor UI
- `Editor.js` — pure QML logic helpers (`.pragma library`)
- `shell_io.py` — the only process that writes to `shell.json` / `shell.toml`;
  a hardened, non-QML boundary invoked via `QProcess`

Validate locally:

```sh
omarchy plugin validate .
```

## License

MIT — see [LICENSE](LICENSE).