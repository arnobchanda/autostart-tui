# autostart-tui

A tiny [Textual](https://textual.textualize.io/) TUI that manages
everything that auto-runs on your Linux desktop: XDG autostart entries,
launcher menu visibility, and systemd `--user` services — plus a boot
impact view so you can see what's actually slowing your login.

```
+--------------------------------------------------+-------------------+
| Autostart [1]  Visibility [2]  Services [3]  Boot|  details panel    |
+--------------------------------------------------+                   |
|  󰍉   ● ON    user      99ms     1Password        |  Exec, override   |
|                                                  |  diff, diagnostics|
|  󰍉   ○ OFF   system    -        Discord          |                   |
|  ...                                             |                   |
+--------------------------------------------------+-------------------+
```

## Why

`gnome-session-properties` is dead. `stacer` is a 100MB GUI for a
50-line problem. KDE's autostart KCM needs all of KDE. This is a single
Python file run by [`uv`](https://docs.astral.sh/uv/) — installs in
seconds, no manual venv, no system packages.

## Quick start

```bash
git clone https://github.com/arnobchanda/autostart-tui.git
cd autostart-tui

mkdir -p ~/.local/bin ~/.local/share/applications
ln -sf "$PWD/autostart_tui.py"      ~/.local/bin/autostart-tui
ln -sf "$PWD/autostart-tui.desktop" ~/.local/share/applications/autostart-tui.desktop
update-desktop-database ~/.local/share/applications 2>/dev/null

autostart-tui
```

The shebang is `#!/usr/bin/env -S uv run --script`, so uv reads the
[PEP 723](https://peps.python.org/pep-0723/) inline dependencies at the
top of the file, provisions an isolated venv on first run, and caches
it. No `pip install`, no `pyproject.toml`.

## How to…

### …disable an app that runs at every login

1. Press `1` for the **Autostart** tab.
2. Use `j`/`k` (or arrows) to highlight the entry.
3. Press `Space` to toggle.

The entry is now `Hidden=true` in your user override. The system file
is never touched. Press `z` to undo.

### …hide an app from your launcher menu

1. Press `2` for the **Launcher Visibility** tab.
2. Find the entry (try `/` to search).
3. Press `Space` — `NoDisplay=true` is written to your user override.

Walker, rofi, fuzzel, GNOME, KDE — every launcher worth using respects
this flag.

### …enable or disable a systemd user service

1. Press `3` for the **Services** tab.
2. Highlight the unit, press `Space`.

Calls `systemctl --user enable/disable` under the hood. Note this
changes the **persistent** state for next login — it does NOT
start/stop the running unit.

### …see what's slowing your boot

1. Press `4` for the **Boot** tab.
2. Entries are sorted by `systemd-analyze blame` cost, biggest first.
3. The header shows: enabled cost · ms saved by what you've already
   disabled · count of entries we couldn't match to a unit.
4. Toggle entries off and watch the totals update live.

### …find out why an entry isn't showing in your launcher

Press `i` to open the details side-panel. The **Diagnostics** block
flags any of these XDG-spec drop conditions:

- Missing `Name` / `Exec` / `Type`
- `Exec=` binary not on `$PATH`
- `TryExec=` binary missing (spec mandates the entry be hidden)
- `OnlyShowIn=` doesn't match `$XDG_CURRENT_DESKTOP`
- `NotShowIn=` matches `$XDG_CURRENT_DESKTOP`

### …save your current setup and switch between configs

1. Get your toggles into the state you want.
2. Press `p` to open the **Profiles** picker.
3. Press `n`, type a name (e.g. `Work`), Enter.

Later, `p` → highlight the profile → `Enter` applies it. The diff
against current state is computed and toggled as a batch (with one
confirmation dialog if any critical entries would be disabled).

Profiles live at `~/.config/autostart-tui/profiles.json`. When the
current state matches a profile exactly, the banner shows
`profile: <name>`.

### …toggle a bunch of entries at once

1. Optionally `/electron` to filter the list.
2. Press `v` to enter **visual mode** at the current row.
3. `j`/`k`/`Shift+G` to extend the highlighted range.
4. `Space` toggles everything in the range. Critical entries trigger
   one batched confirmation.
5. `Esc` cancels without toggling.

### …edit a desktop file in your favourite editor

Press `e` on any entry. The TUI suspends, opens your `$EDITOR` (falls
back to `vi`) on the user override file. On exit, the TUI reloads. For
service unit files, `systemctl --user daemon-reload` runs automatically
after save.

### …reset an entry to system defaults

Press `x`. Deletes the user override, falls back to whatever the system
file says. Refused on service entries — use `systemctl --user revert
<unit>` for those.

### …undo a mistake

`z` reverts the last toggle. Single-step only — for bulk toggles or
profile applies, use a profile to get back to a known good state.

## Tabs

| Tab | Covers | Toggle changes |
|-----|--------|---------------|
| **1 Autostart** | `~/.config/autostart/` + `/etc/xdg/autostart/` | `Hidden=` / `X-GNOME-Autostart-enabled=` in your user override |
| **2 Launcher Visibility** | `~/.local/share/applications/` + `/usr/share/applications/` + flatpak exports | `NoDisplay=` in your user override |
| **3 Services** | `~/.config/systemd/user/` + `/etc/systemd/user/` + `/usr/lib/systemd/user/` | Shells out to `systemctl --user enable/disable` |
| **4 Boot** | Live view of tabs 1+3 with `systemd-analyze blame` cost | Inherits the toggle behaviour of the underlying kind |

System files are never modified — every change lands in your user
directory.

## Keybindings

| Key | Action |
|-----|--------|
| `Space` | Toggle the highlighted entry |
| `z` | Undo the last single toggle |
| `v` | Visual mode (range select); `Space` toggles all selected |
| `p` | Profile picker (Enter apply, `n` save, `d` delete) |
| `i` | Show / hide the details side-panel |
| `e` | Edit the user override in `$EDITOR` (falls back to `vi`) |
| `x` | Reset to system defaults (delete user override) |
| `Enter` | Preview the raw `.desktop` / unit file |
| `/` | Search (substring on Name) |
| `f` | Cycle state filter (all → on → off) |
| `s` | Cycle source filter (all → user → system) |
| `c` | Clear all filters |
| `1` / `2` / `3` / `4` | Jump to a tab |
| `Tab` / `→` / `l`, `Shift+Tab` / `←` / `h` | Next / previous tab |
| `↑`/`k`, `↓`/`j`, `g`/`Home`, `Shift+G`/`End`, `PgUp`/`PgDn` | Navigation |
| `r` | Reload from disk |
| `Ctrl+P` | Textual command palette |
| `q` / `Esc` | Quit (or cancel current action) |

## How it works under the hood

### XDG override shadowing (important)

When the same `.desktop` filename exists in both
`~/.local/share/applications/` and `/usr/share/applications/`, the
**user file fully shadows the system file** per the spec — fields are
not merged. A stub override with only `Hidden=false` will silently
disappear from launchers because it has no `Name`/`Exec`/`Type`.

To prevent this:

- Every override write copies the full system entry first, then flips
  the relevant keys.
- On startup, any pre-existing incomplete overrides are self-healed
  (missing keys backfilled from the system file).
- The override diff in the details pane shows only the keys *you*
  changed, not the full file.

### Architecture

Single Python file. Top half = pure functions (discovery + parsing +
writing). Bottom half = the Textual `App`. State is plain dataclasses
(`Entry`, `Profile`); disk is only re-read on explicit `r`. The Omarchy
theme loader uses `tomllib` (which is why Python ≥3.11).

### Scope

- **Covers**: XDG autostart, XDG launcher visibility, systemd `--user`
  services (enabled + disabled state).
- **Doesn't cover**: system-level systemd units, Hyprland `exec-once`,
  shell rc files, cron `@reboot`, static/masked/template units. Those
  have their own tools.

## Floating window on Omarchy / Hyprland

The `.desktop` Exec runs `omarchy-launch-or-focus-tui autostart-tui`,
which sets the Wayland app-id to `org.omarchy.autostart-tui`. Copy the
snippet from
[`contrib/hyprland-windowrules.conf`](contrib/hyprland-windowrules.conf)
into a sourced `~/.config/hypr/*.conf` and `hyprctl reload`.

> Hyprland 0.54 doesn't honour percentage values in `size` rules — use
> absolute pixels.

Not on Omarchy? Edit the `.desktop` `Exec=` line to whatever launches
your terminal, e.g.
`Exec=ghostty --gtk-single-instance=false --class=autostart-tui -e autostart-tui`
(set `Terminal=false`).

## License

MIT — see [LICENSE](LICENSE).
