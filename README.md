# autostart-tui

A tiny [Textual](https://textual.textualize.io/) TUI to manage Linux
desktop autostart and launcher entries. The Linux answer to "where's the
equivalent of Windows Task Manager's Startup tab?" — with a bonus second
tab for hiding apps from your launcher (walker, rofi, fuzzel, GNOME,
KDE, etc.).

## Why

`gnome-session-properties` is dead. `stacer` is a 100MB GUI for a 50-line
problem. KDE's autostart KCM needs all of KDE. This is a single Python
file, run by [`uv`](https://docs.astral.sh/uv/) with [Textual](https://textual.textualize.io/)
for the UI — installs in seconds, no manual venv, no system-level
packages, and follows your current Omarchy theme automatically.

## Two tabs

### 1. Autostart

Lists entries from `~/.config/autostart/` and `/etc/xdg/autostart/` and
lets you toggle them on/off. Disabling writes:

```
Hidden=true
X-GNOME-Autostart-enabled=false
```

System-only entries are never edited — toggling creates a user-side
override in `~/.config/autostart/` and edits *that*. Re-enabling flips
the keys back; the override file stays so the state is explicit and
easy to find later.

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
overrides land in `~/.local/share/applications/`.

> **About overrides**: per the XDG Desktop Entry Spec, a user `.desktop`
> file with the same name as a system one **fully shadows** the system
> file — fields are not merged. To keep entries from silently
> disappearing from launchers, the toggle copies the whole system entry
> into the user override on first write, and backfills any missing keys
> on subsequent writes. On startup, the TUI also scans for any existing
> incomplete overrides (e.g. left behind by older versions or other
> tools) and repairs them automatically. The override panel still only
> shows the keys the user actually changes.
>
> Press `x` to reset the highlighted entry to system defaults — this
> deletes the user override file and re-reads the entry from the
> system file.

## Features

- **Two tabs** for autostart entries and launcher visibility (`1`/`2`,
  arrow keys, `h`/`l`, or `Tab` to switch)
- **Filters**: cycle by state (`f`: all → on → off) and source (`s`:
  all → user → system); `c` clears both. Active filters surface in the
  header subtitle.
- **Desktop-file preview**: press `Enter` on any row to open a modal
  showing the raw `.desktop` file with INI syntax highlighting and line
  numbers
- **Inline editor**: press `e` to edit the user override in-place
  (`Ctrl+S` to save, `Esc` to cancel) without leaving the TUI
- **Details side-panel** (`i`) showing Exec, file paths, override
  effect, and a parsed key-level diff against the system file
- **Diagnostics**: when something is wrong with an entry — missing
  `Exec` / `TryExec` binary, `OnlyShowIn` / `NotShowIn` mismatch with
  `$XDG_CURRENT_DESKTOP`, missing required keys — the details pane
  shows a red Diagnostics block explaining exactly why a launcher
  would drop the entry
- **Search** (`/`) — substring match on Name + Exec, layered on top of
  state and source filters
- **Undo** (`z`) — reverts the last toggle, with a short toast
- **Live details strip** under the table — the highlighted row's
  `Exec=` command and source path stay visible at all times
- **Async startup**: UI paints immediately with loading spinners; the
  ~150-file disk scan runs in a worker thread
- **Omarchy theme integration**: reads
  `~/.config/omarchy/current/theme/alacritty.toml` and builds a matching
  Textual theme on each launch. `omarchy theme set <name>` and the TUI
  tracks. Falls back to Textual's default dark theme on non-Omarchy
  systems.
- **Floating window** on Omarchy/Hyprland via
  `omarchy-launch-or-focus-tui` and a matching `windowrule`
- **Non-destructive everywhere**: system files in `/etc/xdg/autostart/`
  and `/usr/share/applications/` are never modified. User overrides
  always go to `~/.config/autostart/` or `~/.local/share/applications/`.

## Install

```bash
# Clone wherever you keep source checkouts
git clone https://github.com/arnobchanda/autostart-tui.git
cd autostart-tui

# Symlink so edits in the repo apply immediately
mkdir -p ~/.local/bin ~/.local/share/applications
ln -sf "$PWD/autostart_tui.py"      ~/.local/bin/autostart-tui
ln -sf "$PWD/autostart-tui.desktop" ~/.local/share/applications/autostart-tui.desktop

update-desktop-database ~/.local/share/applications 2>/dev/null
```

Then run `autostart-tui` in a terminal, or launch **Autostart Manager**
from your app launcher.

The shebang line is `#!/usr/bin/env -S uv run --script`, so [uv] picks
up the [PEP 723] inline metadata at the top of the file
(`requires-python = ">=3.11"`, `dependencies = ["textual>=0.86"]`),
provisions an isolated Python and venv on first run, and caches both.
No `pip install`, no `pyproject.toml`, no manual venv.

[uv]: https://docs.astral.sh/uv/
[PEP 723]: https://peps.python.org/pep-0723/

### Floating window on Omarchy / Hyprland

The `.desktop` Exec line runs `omarchy-launch-or-focus-tui autostart-tui`,
which uses `xdg-terminal-exec` under the hood and sets the window's
Wayland app-id to `org.omarchy.autostart-tui`. Copy the snippet from
[`contrib/hyprland-windowrules.conf`](contrib/hyprland-windowrules.conf)
into a sourced `~/.config/hypr/*.conf` and `hyprctl reload` to make the
window float, size to 1550×900, and center on screen.

> **Note**: Hyprland 0.54 doesn't honour percentage values in `size`
> rules — every working `size` rule in Omarchy's defaults uses absolute
> pixels. Tweak the numbers for your display.

If you don't use Omarchy: change `Exec=` to whatever launches your
preferred terminal with this script, e.g.
`Exec=ghostty --gtk-single-instance=false --class=autostart-tui -e autostart-tui`
(set `Terminal=false`), or `Exec=autostart-tui` with `Terminal=true` to
let your launcher pick a terminal for you.

## Keybindings

| Key | Action |
|-----|--------|
| `↑` / `k` | Move up |
| `↓` / `j` | Move down |
| `g` / `Home` | Jump to top |
| `Shift+G` / `End` | Jump to bottom |
| `PgUp` / `PgDn` | Page up / down |
| `Space` | Toggle the highlighted entry |
| `z` | Undo the last toggle |
| `i` | Show / hide the details side-panel |
| `/` | Focus the search box (substring match on Name + Exec) |
| `e` | Open the user override in an inline editor (`Ctrl+S` save, `Esc` cancel) |
| `x` | Reset to system defaults — delete the user override for this entry |
| `Enter` | Open `.desktop` file preview (Esc/q/Enter to close) |
| `f` | Cycle state filter (all → on → off) |
| `s` | Cycle source filter (all → user → system) |
| `c` | Clear both filters |
| `1` / `2` | Jump to Autostart / Launcher tab |
| `Tab` / `→` / `l` | Next tab |
| `Shift+Tab` / `←` / `h` | Previous tab |
| `r` | Reload from disk |
| `Ctrl+P` | Open Textual's command palette |
| `q` / `Esc` | Quit |
| _click_ | Move cursor (mouse supported via Textual) |

## Architecture

Single Python file with three concerns:

| Concern | Where |
|---------|-------|
| Reading + parsing `.desktop` files | `discover_autostart()` / `discover_launcher()` |
| Writing non-destructive toggles | `toggle_autostart()` / `toggle_launcher()` |
| TUI shell, filters, preview, theme | `AutostartApp(App)` |

State is plain dataclasses (`Entry`). The TUI keeps an in-memory
`dict[EntryKind, list[Entry]]` and refreshes the visible `DataTable`
from that list (filtered) after every action. Disk is only re-read on
explicit `r` reload.

The Omarchy theme loader reads the alacritty palette via stdlib
`tomllib` and registers a Textual `Theme` — that's why we require
Python 3.11+.

## Scope

- Covers: XDG desktop-file autostart (most user apps incl. Remmina,
  Nextcloud, 1Password, fcitx, walker) and launcher visibility
  (everything with a `.desktop` file).
- Does **not** cover: systemd user units, Hyprland `exec-once`, shell
  rc files, cron `@reboot`. Those have their own management tools
  (`systemctl --user`, edit the conf, edit the file).

## License

MIT — see [LICENSE](LICENSE).
