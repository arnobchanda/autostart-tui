# autostart-tui

A tiny [Textual](https://textual.textualize.io/) TUI to manage Linux
desktop autostart and launcher entries. The Linux answer to "where's the
equivalent of Windows Task Manager's Startup tab?" — with a bonus second
tab for hiding apps from your launcher (walker, rofi, fuzzel, GNOME,
KDE, etc.).

## Why

`gnome-session-properties` is dead. `stacer` is a 100MB GUI for a 50-line
problem. KDE's autostart KCM needs all of KDE. This is a single Python
file, run by `uv` with Textual for the UI — installs in seconds, no
manual venv, no system-level packages.

## Two tabs

### 1. Autostart

Lists entries from `~/.config/autostart/` and `/etc/xdg/autostart/`
and lets you toggle them on/off. Disabling sets:

```
Hidden=true
X-GNOME-Autostart-enabled=false
```

System-only entries are never edited — toggling creates a user-side
override in `~/.config/autostart/` instead. Re-enabling flips the keys
back, the override file stays so you can see the state at a glance.

### 2. Launcher

Lists `.desktop` files from the standard application directories:
- `~/.local/share/applications/`
- `/usr/share/applications/`
- `~/.local/share/flatpak/exports/share/applications/`
- `/var/lib/flatpak/exports/share/applications/`

Toggling flips `NoDisplay=`, the freedesktop standard for hiding an
entry from launchers. Every launcher worth using respects it (walker,
rofi, fuzzel, GNOME menu, KDE Kicker, …).

Same non-destructive rule: system files stay untouched; user-side
overrides go in `~/.local/share/applications/`.

## Omarchy theme integration

On startup the app reads `~/.config/omarchy/current/theme/alacritty.toml`
and builds a matching Textual theme from its palette. Switch themes with
`omarchy theme set <name>` and the TUI tracks on next launch. Falls back
to Textual's default dark theme if the file isn't there (i.e. on non-
omarchy systems).

## Install

```bash
git clone git@github.com:arnobchanda/autostart-tui.git ~/Dev/personal/autostart-tui
cd ~/Dev/personal/autostart-tui

# Symlink so edits in the repo apply immediately
ln -sf "$PWD/autostart_tui.py"      ~/.local/bin/autostart-tui
ln -sf "$PWD/autostart-tui.desktop" ~/.local/share/applications/autostart-tui.desktop

update-desktop-database ~/.local/share/applications 2>/dev/null
```

Then run `autostart-tui` in a terminal, or launch **Autostart Manager**
from your app launcher.

The shebang line is `#!/usr/bin/env -S uv run --script`, so [uv] picks
up the [PEP 723] inline metadata block at the top of the file
(`requires-python = ">=3.11"`, `dependencies = ["textual>=0.86"]`),
provisions an isolated Python and venv on first run, and caches both.
No `pip install`, no `pyproject.toml`, no manual venv.

[uv]: https://docs.astral.sh/uv/
[PEP 723]: https://peps.python.org/pep-0723/

### Floating window on Hyprland

The `.desktop` Exec line runs `ghostty --class=autostart-tui -e
autostart-tui`, so the terminal window gets a dedicated WM class.
Copy the snippet from `contrib/hyprland-windowrules.conf` into a
sourced `~/.config/hypr/*.conf` and `hyprctl reload` to make the
window float, size to 60×70%, and center on screen.

If you don't use Hyprland: set `Terminal=true` and remove the ghostty
prefix from `Exec=` to fall back to whatever terminal your launcher
picks.

## Keys

| Key | Action |
|-----|--------|
| `↑` / `k` | Move up |
| `↓` / `j` | Move down |
| `g` / `Home` | Jump to top |
| `Shift+G` / `End` | Jump to bottom |
| `Space` / `Enter` | Toggle entry |
| `1` | Switch to Autostart tab |
| `2` | Switch to Launcher tab |
| `Tab` | Cycle tabs |
| `r` | Reload from disk |
| `q` / `Esc` | Quit |
| _click_ | Move cursor (mouse supported via Textual) |

## Scope

- Covers: XDG desktop-file autostart (most user apps incl. Remmina,
  Nextcloud, 1Password, fcitx, walker) and launcher visibility
  (everything with a `.desktop` file).
- Does **not** cover: systemd user units, Hyprland `exec-once`, shell
  rc files, cron `@reboot`. Those have their own management tools
  (`systemctl --user`, edit the conf, edit the file).

## License

MIT
