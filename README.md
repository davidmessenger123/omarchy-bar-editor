# Omarchy Bar Editor

A GTK4 desktop app that visually edits the Omarchy status bar: layout,
widgets, per-widget options, bar styling, idle timeouts, and plugins.

## Requirements

- Python 3 and PyGObject (`python-gobject`)
- GTK 4 (`gtk4`)
- [Omarchy](https://omarchy.org) (for the `omarchy` and `omarchy-plugin-catalog` commands)

Arch Linux:

```sh
sudo pacman -S --needed python-gobject gtk4 omarchy
```

## Install

Clone the repo, then symlink (or copy) the script onto your PATH and install
the desktop entry:

```sh
git clone https://github.com/davidmessenger123/omarchy-bar-editor.git ~/Work/omarchy-bar-editor
ln -sf ~/Work/omarchy-bar-editor/omarchy-bar-editor ~/.local/bin/omarchy-bar-editor
mkdir -p ~/.local/share/applications
cp omarchy-bar-editor.desktop ~/.local/share/applications/  # edit the Exec line's HOME path as needed
```

Run it with `omarchy-bar-editor`, or launch it from your app menu as "Omarchy
Bar Editor".

### Option: copy instead of symlink

If you'd rather not keep the checkout around, copy the script instead of
symlinking it. The in-app Update button only works when the script is running
from a git checkout (symlinked or not).

## Update

- Click **Update** in the app's header bar: it fetches `origin/main`, pulls
  the latest commits into the checkout, and restarts itself to apply them.
- Or manually: `git -C ~/Work/omarchy-bar-editor pull origin main`.

Changes to `shell.json` are hot-reloaded by the Omarchy shell on save, so most
edits apply live.

## Files the editor manages

| File | Purpose |
|---|---|
| `~/.config/omarchy/shell.json` | Bar layout, widget options, idle, plugins |
| `~/.config/omarchy/shell.json.bak-editor` | Backup created on every save |
| `~/.config/omarchy/shell.toml` | `[bar]` colors, alpha, sizing |
| `~/.config/omarchy/shell.toml.bak-editor` | Backup created on every save |
| `~/.config/omarchy/bar-profiles/*.json` | Named layout profiles |
| `~/.local/state/omarchy/toggles/bar-off` | Bar show/hide flag |

## Features

- Bar position, transparency, center anchor, and active bar host
- Screensaver / lock idle timeouts
- Widget layout editing for the left / center / right sections:
  add, remove, reorder, move, and drag-and-drop between sections
- Per-widget options with typed editors (boolean, integer, enum, multiselect, text)
- `[bar]` styling in `shell.toml`: colors, background alpha, sizing, scale-with-font
- Plugin enable/disable (`plugins[]` / `disabledPlugins[]`)
- Global cross-section search, undo/redo, named layout profiles, save diff
- In-app update (git pull in the checkout + self-restart)

## License

MIT — see [LICENSE](LICENSE).