# autostart-tui

A tiny ncurses TUI to manage Linux XDG autostart entries — the answer to
"where's the Linux equivalent of Windows Task Manager's Startup tab?"

Lists desktop autostart entries from the standard XDG locations and lets
you flip them on/off with a keypress.

## Why

`gnome-session-properties` is dead. `stacer` is a 100MB GUI for a 50-line
problem. KDE's autostart KCM needs all of KDE. This is a single Python
file using only the standard library.

## What it sees

| Path | Meaning |
|------|---------|
| `~/.config/autostart/` | User-level autostart entries |
| `/etc/xdg/autostart/`  | System-level autostart entries |

User entries override system entries with the same name (XDG spec).

## How it disables things

For a **user entry**, the in-place file gets:

```
Hidden=true
X-GNOME-Autostart-enabled=false
```

For a **system-only entry**, the system file is never touched. A user-side
override copy is written to `~/.config/autostart/<id>.desktop` with the
same two keys set to disable. Re-enabling flips both keys back to their
"on" values.

This means every change is non-destructive and easy to undo by hand
(just delete the file in `~/.config/autostart/` or flip the keys back).

## Install

```bash
git clone git@github.com:arnobchanda/autostart-tui.git
cd autostart-tui
install -Dm755 autostart_tui.py ~/.local/bin/autostart-tui
```

Then run `autostart-tui`. Requires Python 3.10+.

## Keys

| Key | Action |
|-----|--------|
| `↑` / `k` | Move up |
| `↓` / `j` | Move down |
| `g` / `Home` | Jump to top |
| `G` / `End` | Jump to bottom |
| `Space` / `Enter` | Toggle entry |
| `r` | Reload from disk |
| `q` / `Esc` | Quit |

## Scope

- Covers: XDG desktop-file autostart (the mechanism most user apps use,
  including the Remmina applet, Nextcloud, 1Password, walker, fcitx,
  etc.)
- Does **not** cover: systemd user units, Hyprland `exec-once`, shell
  rc files, cron `@reboot`. Those have their own management tools
  (`systemctl --user`, edit the conf, edit the file).

## License

MIT
