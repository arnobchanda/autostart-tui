#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "textual>=0.86",
# ]
# ///
"""autostart-tui — manage Linux XDG autostart and launcher entries.

Two tabs:
    1. Autostart — entries from ~/.config/autostart/ and /etc/xdg/autostart/.
       Toggles Hidden= / X-GNOME-Autostart-enabled=.
    2. Launcher — entries from {~/.local/share, /usr/share, flatpak} /applications/.
       Toggles NoDisplay= (the freedesktop standard for hiding from launchers).

System files are never modified. Disabling a system-only entry creates a
user-side override copy and edits that instead. Re-enabling flips the
keys back without removing the override file (so state stays explicit).

If ~/.config/omarchy/current/theme/alacritty.toml exists, its palette is
loaded into a Textual theme so the TUI tracks `omarchy theme set ...`.
"""
from __future__ import annotations

import configparser
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.theme import Theme
from textual.widgets import DataTable, Footer, Header, TabbedContent, TabPane

# ---------- Paths ----------

AUTOSTART_USER = Path("~/.config/autostart").expanduser()
AUTOSTART_SYSTEM = Path("/etc/xdg/autostart")

LAUNCHER_DIRS_USER = [
    Path("~/.local/share/applications").expanduser(),
    Path("~/.local/share/flatpak/exports/share/applications").expanduser(),
]
LAUNCHER_DIRS_SYSTEM = [
    Path("/usr/share/applications"),
    Path("/var/lib/flatpak/exports/share/applications"),
]
LAUNCHER_USER_WRITE_DIR = LAUNCHER_DIRS_USER[0]  # where overrides go

THEME_FILE = Path("~/.config/omarchy/current/theme/alacritty.toml").expanduser()


# ---------- Model ----------

EntryKind = Literal["autostart", "launcher"]


@dataclass
class Entry:
    kind: EntryKind
    desktop_id: str
    name: str
    exec_cmd: str
    user_path: Path | None
    system_path: Path | None
    enabled: bool  # for launcher entries: "visible in launcher"

    @property
    def source(self) -> str:
        if self.user_path and self.system_path:
            return "user+system"
        return "user" if self.user_path else "system"


def _read_desktop(path: Path) -> configparser.RawConfigParser | None:
    cp = configparser.RawConfigParser(interpolation=None, strict=False)
    cp.optionxform = lambda s: s  # preserve key case
    try:
        cp.read(path, encoding="utf-8")
    except (configparser.Error, OSError, UnicodeDecodeError):
        return None
    if "Desktop Entry" not in cp:
        return None
    return cp


def _autostart_enabled(cp: configparser.RawConfigParser) -> bool:
    de = cp["Desktop Entry"]
    hidden = de.get("Hidden", "false").strip().lower() == "true"
    gnome_off = de.get("X-GNOME-Autostart-enabled", "true").strip().lower() == "false"
    return not (hidden or gnome_off)


def _launcher_visible(cp: configparser.RawConfigParser) -> bool:
    de = cp["Desktop Entry"]
    no_display = de.get("NoDisplay", "false").strip().lower() == "true"
    hidden = de.get("Hidden", "false").strip().lower() == "true"
    return not (no_display or hidden)


def _write_desktop(path: Path, cp: configparser.RawConfigParser) -> None:
    """Write a desktop file without configparser's "key = value" spacing."""
    with open(path, "w", encoding="utf-8") as f:
        for section in cp.sections():
            f.write(f"[{section}]\n")
            for k, v in cp[section].items():
                f.write(f"{k}={v}\n")
            f.write("\n")


def _merge_user_over_system(
    by_id: dict[str, Entry],
    p: Path,
    cp: configparser.RawConfigParser,
    kind: EntryKind,
    enabled: bool,
) -> None:
    de = cp["Desktop Entry"]
    did = p.stem
    existing = by_id.get(did)
    if existing:
        existing.user_path = p
        existing.enabled = enabled
        existing.name = de.get("Name", existing.name).strip() or existing.name
        existing.exec_cmd = de.get("Exec", existing.exec_cmd).strip()
    else:
        by_id[did] = Entry(
            kind=kind,
            desktop_id=did,
            name=de.get("Name", did).strip() or did,
            exec_cmd=de.get("Exec", "").strip(),
            user_path=p,
            system_path=None,
            enabled=enabled,
        )


def discover_autostart() -> list[Entry]:
    by_id: dict[str, Entry] = {}
    if AUTOSTART_SYSTEM.is_dir():
        for p in sorted(AUTOSTART_SYSTEM.glob("*.desktop")):
            cp = _read_desktop(p)
            if not cp:
                continue
            de = cp["Desktop Entry"]
            did = p.stem
            by_id[did] = Entry(
                kind="autostart",
                desktop_id=did,
                name=de.get("Name", did).strip() or did,
                exec_cmd=de.get("Exec", "").strip(),
                user_path=None,
                system_path=p,
                enabled=_autostart_enabled(cp),
            )
    if AUTOSTART_USER.is_dir():
        for p in sorted(AUTOSTART_USER.glob("*.desktop")):
            cp = _read_desktop(p)
            if not cp:
                continue
            _merge_user_over_system(by_id, p, cp, "autostart", _autostart_enabled(cp))
    return sorted(by_id.values(), key=lambda e: e.name.lower())


def discover_launcher() -> list[Entry]:
    by_id: dict[str, Entry] = {}
    for d in LAUNCHER_DIRS_SYSTEM:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.desktop")):
            cp = _read_desktop(p)
            if not cp:
                continue
            de = cp["Desktop Entry"]
            if de.get("Type", "Application").strip() != "Application":
                continue
            did = p.stem
            # First system source wins; subsequent ones don't override
            by_id.setdefault(did, Entry(
                kind="launcher",
                desktop_id=did,
                name=de.get("Name", did).strip() or did,
                exec_cmd=de.get("Exec", "").strip(),
                user_path=None,
                system_path=p,
                enabled=_launcher_visible(cp),
            ))
    for d in LAUNCHER_DIRS_USER:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.desktop")):
            cp = _read_desktop(p)
            if not cp:
                continue
            de = cp["Desktop Entry"]
            if de.get("Type", "Application").strip() != "Application":
                continue
            _merge_user_over_system(by_id, p, cp, "launcher", _launcher_visible(cp))
    return sorted(by_id.values(), key=lambda e: e.name.lower())


def toggle_autostart(entry: Entry) -> None:
    AUTOSTART_USER.mkdir(parents=True, exist_ok=True)
    if entry.user_path is None:
        assert entry.system_path is not None
        target = AUTOSTART_USER / f"{entry.desktop_id}.desktop"
        shutil.copy2(entry.system_path, target)
        entry.user_path = target
    cp = _read_desktop(entry.user_path)
    if cp is None:
        return
    new = not entry.enabled
    cp["Desktop Entry"]["Hidden"] = "false" if new else "true"
    cp["Desktop Entry"]["X-GNOME-Autostart-enabled"] = "true" if new else "false"
    _write_desktop(entry.user_path, cp)
    entry.enabled = new


def toggle_launcher(entry: Entry) -> None:
    LAUNCHER_USER_WRITE_DIR.mkdir(parents=True, exist_ok=True)
    if entry.user_path is None:
        assert entry.system_path is not None
        target = LAUNCHER_USER_WRITE_DIR / f"{entry.desktop_id}.desktop"
        shutil.copy2(entry.system_path, target)
        entry.user_path = target
    cp = _read_desktop(entry.user_path)
    if cp is None:
        return
    new_visible = not entry.enabled
    cp["Desktop Entry"]["NoDisplay"] = "false" if new_visible else "true"
    # If a system file used Hidden=true to suppress, clear it on the override.
    if not new_visible:
        cp["Desktop Entry"]["Hidden"] = "false"
    _write_desktop(entry.user_path, cp)
    entry.enabled = new_visible


# ---------- Omarchy theme integration ----------

def load_omarchy_theme() -> Theme | None:
    """Build a Textual Theme from the current omarchy theme's alacritty palette."""
    if not THEME_FILE.is_file():
        return None
    try:
        with open(THEME_FILE, "rb") as f:
            data = tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError):
        return None

    colors = data.get("colors", {})
    primary = colors.get("primary", {})
    normal = colors.get("normal", {})
    bright = colors.get("bright", {})
    selection = colors.get("selection", {})

    bg = primary.get("background", "#1e1e2e")
    fg = primary.get("foreground", "#cdd6f4")
    return Theme(
        name="omarchy",
        primary=normal.get("blue", "#89b4fa"),
        secondary=normal.get("magenta", "#cba6f7"),
        accent=bright.get("yellow", normal.get("yellow", "#fab387")),
        warning=normal.get("yellow", "#f9e2af"),
        error=normal.get("red", "#f38ba8"),
        success=normal.get("green", "#a6e3a1"),
        foreground=fg,
        background=bg,
        surface=selection.get("background", "#313244"),
        panel=normal.get("black", "#181825"),
        dark=True,
    )


# ---------- TUI ----------

class AutostartApp(App):
    TITLE = "autostart-tui"
    SUB_TITLE = "autostart + launcher manager"

    CSS = """
    Screen {
        background: $background;
    }

    TabbedContent {
        height: 1fr;
    }

    DataTable {
        height: 1fr;
        background: $surface;
    }

    DataTable > .datatable--header {
        text-style: bold;
        background: $primary 30%;
    }

    DataTable > .datatable--cursor {
        background: $accent 50%;
        color: $foreground;
    }

    Header {
        background: $primary 40%;
    }

    Footer {
        background: $panel;
    }
    """

    BINDINGS = [
        Binding("space,enter", "toggle", "Toggle"),
        Binding("r", "reload", "Reload"),
        Binding("1", "show_tab('autostart')", "Autostart"),
        Binding("2", "show_tab('launcher')", "Launcher"),
        Binding("tab", "next_tab", "Switch tab", show=False),
        Binding("q,escape", "quit", "Quit"),
        Binding("j", "down", "Down", show=False),
        Binding("k", "up", "Up", show=False),
        Binding("g,home", "top", "Top", show=False),
        Binding("shift+g,end", "bottom", "Bottom", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.entries: dict[EntryKind, list[Entry]] = {"autostart": [], "launcher": []}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with TabbedContent(initial="autostart-tab"):
            with TabPane("Autostart [1]", id="autostart-tab"):
                yield DataTable(id="autostart-table", cursor_type="row", zebra_stripes=True)
            with TabPane("Launcher [2]", id="launcher-tab"):
                yield DataTable(id="launcher-table", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        theme = load_omarchy_theme()
        if theme is not None:
            self.register_theme(theme)
            self.theme = "omarchy"

        for kind, tid in (("autostart", "#autostart-table"), ("launcher", "#launcher-table")):
            table = self.query_one(tid, DataTable)
            table.add_column("State", width=7)
            table.add_column("Source", width=14)
            table.add_column("Name", width=34)
            table.add_column("Exec")

        self._reload("autostart")
        self._reload("launcher")

    # --- actions ---

    def action_toggle(self) -> None:
        kind = self._active_kind()
        table = self._active_table()
        entries = self.entries[kind]
        if not entries or table.row_count == 0:
            return
        cursor_row = table.cursor_row
        row_key = table.coordinate_to_cell_key((cursor_row, 0)).row_key
        entry = next((e for e in entries if e.desktop_id == row_key.value), None)
        if entry is None:
            return
        (toggle_autostart if kind == "autostart" else toggle_launcher)(entry)
        verb = "Enabled" if entry.enabled else ("Disabled" if kind == "autostart" else "Hidden")
        if kind == "launcher" and entry.enabled:
            verb = "Shown"
        self.notify(
            f"{verb}: {entry.name}",
            severity="information" if entry.enabled else "warning",
            timeout=2.0,
        )
        self._refresh_row(table, cursor_row, entry)

    def action_reload(self) -> None:
        self._reload("autostart")
        self._reload("launcher")
        n = len(self.entries["autostart"]) + len(self.entries["launcher"])
        self.notify(f"Reloaded ({n} entries total)", timeout=1.5)

    def action_show_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = f"{tab_id}-tab"

    def action_next_tab(self) -> None:
        tabs = self.query_one(TabbedContent)
        tabs.active = "launcher-tab" if tabs.active == "autostart-tab" else "autostart-tab"

    def action_down(self) -> None:
        self._active_table().action_cursor_down()

    def action_up(self) -> None:
        self._active_table().action_cursor_up()

    def action_top(self) -> None:
        self._active_table().move_cursor(row=0)

    def action_bottom(self) -> None:
        t = self._active_table()
        if t.row_count:
            t.move_cursor(row=t.row_count - 1)

    # --- helpers ---

    def _active_kind(self) -> EntryKind:
        tabs = self.query_one(TabbedContent)
        return "launcher" if tabs.active == "launcher-tab" else "autostart"

    def _active_table(self) -> DataTable:
        kind = self._active_kind()
        return self.query_one(f"#{kind}-table", DataTable)

    def _reload(self, kind: EntryKind) -> None:
        self.entries[kind] = discover_autostart() if kind == "autostart" else discover_launcher()
        table = self.query_one(f"#{kind}-table", DataTable)
        cursor_row = table.cursor_row if table.row_count else 0
        table.clear()
        for e in self.entries[kind]:
            table.add_row(*_row_cells(e), key=e.desktop_id)
        if table.row_count:
            table.move_cursor(row=min(cursor_row, table.row_count - 1))

    def _refresh_row(self, table: DataTable, row_idx: int, entry: Entry) -> None:
        for col_idx, value in enumerate(_row_cells(entry)):
            table.update_cell_at((row_idx, col_idx), value)


def _row_cells(e: Entry) -> tuple[str, str, str, str]:
    on_label = "● ON " if e.kind == "autostart" else "● SHOW"
    off_label = "○ OFF" if e.kind == "autostart" else "○ HIDE"
    state = (
        f"[bold green]{on_label}[/]" if e.enabled else f"[bold red]{off_label}[/]"
    )
    name = e.name if e.enabled else f"[dim]{e.name}[/]"
    exec_cmd = e.exec_cmd if e.enabled else f"[dim]{e.exec_cmd}[/]"
    return state, e.source, name, exec_cmd


def main() -> None:
    AutostartApp().run()


if __name__ == "__main__":
    main()
