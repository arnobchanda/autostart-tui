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
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Label,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

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

StateFilter = Literal["all", "on", "off"]
SourceFilter = Literal["all", "user", "system"]

STATE_CYCLE: dict[StateFilter, StateFilter] = {"all": "on", "on": "off", "off": "all"}
SOURCE_CYCLE: dict[SourceFilter, SourceFilter] = {
    "all": "user",
    "user": "system",
    "system": "all",
}


class DesktopFilePreview(ModalScreen[None]):
    """Modal dialog showing the raw contents of a .desktop file."""

    BINDINGS = [
        Binding("escape,q,enter", "dismiss", "Close"),
    ]

    DEFAULT_CSS = """
    DesktopFilePreview {
        align: center middle;
    }

    #preview-dialog {
        width: 90%;
        height: 80%;
        background: $surface;
        border: thick $primary;
        padding: 0;
    }

    #preview-title {
        background: $primary 40%;
        color: $foreground;
        text-style: bold;
        padding: 0 2;
        height: 1;
        width: 100%;
    }

    #preview-path {
        background: $panel;
        color: $accent;
        padding: 0 2;
        height: 1;
        width: 100%;
    }

    #preview-area {
        height: 1fr;
        border: none;
    }
    """

    def __init__(self, name: str, path: Path, content: str) -> None:
        super().__init__()
        self._title = name
        self._path = path
        self._content = content

    def compose(self) -> ComposeResult:
        with Vertical(id="preview-dialog"):
            yield Label(self._title, id="preview-title")
            yield Label(str(self._path), id="preview-path")
            yield TextArea.code_editor(
                self._content,
                language="ini",
                read_only=True,
                id="preview-area",
                show_line_numbers=True,
            )


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

    #exec-preview {
        height: auto;
        min-height: 2;
        max-height: 4;
        padding: 0 1;
        background: $panel;
        color: $accent;
        border-top: solid $primary 40%;
    }

    Header {
        background: $primary 40%;
    }

    Footer {
        background: $panel;
    }
    """

    BINDINGS = [
        Binding("space", "toggle", "Toggle"),
        Binding("enter", "preview", "Preview"),
        Binding("f", "cycle_state", "State filter"),
        Binding("s", "cycle_source", "Source filter"),
        Binding("c", "clear_filters", "Clear filters"),
        Binding("r", "reload", "Reload"),
        Binding("1", "show_tab('autostart')", "Autostart"),
        Binding("2", "show_tab('launcher')", "Launcher"),
        Binding("tab,right,l", "next_tab", "Next tab", show=False),
        Binding("shift+tab,left,h", "prev_tab", "Prev tab", show=False),
        Binding("q,escape", "quit", "Quit"),
        Binding("j,down", "down", "Down", show=False),
        Binding("k,up", "up", "Up", show=False),
        Binding("g,home", "top", "Top", show=False),
        Binding("shift+g,end", "bottom", "Bottom", show=False),
        Binding("pageup", "page_up", "PgUp", show=False),
        Binding("pagedown", "page_down", "PgDn", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.entries: dict[EntryKind, list[Entry]] = {"autostart": [], "launcher": []}
        self.state_filter: StateFilter = "all"
        self.source_filter: SourceFilter = "all"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with TabbedContent(initial="autostart-tab"):
            with TabPane("Autostart [1]", id="autostart-tab"):
                yield DataTable(id="autostart-table", cursor_type="row", zebra_stripes=True)
            with TabPane("Launcher [2]", id="launcher-tab"):
                yield DataTable(id="launcher-table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="exec-preview", markup=True)
        yield Footer()

    def on_mount(self) -> None:
        theme = load_omarchy_theme()
        if theme is not None:
            self.register_theme(theme)
            self.theme = "omarchy"

        for tid in ("#autostart-table", "#launcher-table"):
            table = self.query_one(tid, DataTable)
            table.add_column("State", width=8)
            table.add_column("Source", width=14)
            table.add_column("Name")

        self._reload("autostart")
        self._reload("launcher")
        self._active_table().focus()
        self._update_preview()

    # --- actions ---

    def action_toggle(self) -> None:
        entry = self._current_entry()
        if entry is None:
            return
        kind = entry.kind
        (toggle_autostart if kind == "autostart" else toggle_launcher)(entry)
        verb = "Enabled" if entry.enabled else ("Disabled" if kind == "autostart" else "Hidden")
        if kind == "launcher" and entry.enabled:
            verb = "Shown"
        self.notify(
            f"{verb}: {entry.name}",
            severity="information" if entry.enabled else "warning",
            timeout=2.0,
        )
        # If a filter is active, the entry may have moved in/out of the view.
        if self.state_filter == "all" and self.source_filter == "all":
            self._refresh_row(self._active_table(), self._active_table().cursor_row, entry)
        else:
            self._populate(kind)

    def action_preview(self) -> None:
        entry = self._current_entry()
        if entry is None:
            return
        path = entry.user_path or entry.system_path
        if path is None or not path.is_file():
            self.notify("No file to preview", severity="warning", timeout=1.5)
            return
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            self.notify(f"Read error: {exc}", severity="error", timeout=2.0)
            return
        self.push_screen(DesktopFilePreview(entry.name, path, content))

    def action_reload(self) -> None:
        self._reload("autostart")
        self._reload("launcher")
        n = len(self.entries["autostart"]) + len(self.entries["launcher"])
        self.notify(f"Reloaded ({n} entries total)", timeout=1.5)

    def action_cycle_state(self) -> None:
        self.state_filter = STATE_CYCLE[self.state_filter]
        self._populate(self._active_kind())
        self._populate(self._other_kind())
        self.notify(f"State filter: {self.state_filter}", timeout=1.0)
        self._update_subtitle()

    def action_cycle_source(self) -> None:
        self.source_filter = SOURCE_CYCLE[self.source_filter]
        self._populate(self._active_kind())
        self._populate(self._other_kind())
        self.notify(f"Source filter: {self.source_filter}", timeout=1.0)
        self._update_subtitle()

    def action_clear_filters(self) -> None:
        self.state_filter = "all"
        self.source_filter = "all"
        self._populate("autostart")
        self._populate("launcher")
        self.notify("Filters cleared", timeout=1.0)
        self._update_subtitle()

    def action_show_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = f"{tab_id}-tab"
        self._active_table().focus()
        self._update_preview()

    def action_next_tab(self) -> None:
        tabs = self.query_one(TabbedContent)
        tabs.active = "launcher-tab" if tabs.active == "autostart-tab" else "autostart-tab"
        self._active_table().focus()
        self._update_preview()

    def action_prev_tab(self) -> None:
        # With only two tabs, "prev" == "next" — kept separate for clarity
        # so binding labels can differ if a third tab is added later.
        self.action_next_tab()

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

    def action_page_up(self) -> None:
        self._active_table().action_page_up()

    def action_page_down(self) -> None:
        self._active_table().action_page_down()

    # --- events ---

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._update_preview()

    # --- helpers ---

    def _active_kind(self) -> EntryKind:
        tabs = self.query_one(TabbedContent)
        return "launcher" if tabs.active == "launcher-tab" else "autostart"

    def _other_kind(self) -> EntryKind:
        return "launcher" if self._active_kind() == "autostart" else "autostart"

    def _active_table(self) -> DataTable:
        return self.query_one(f"#{self._active_kind()}-table", DataTable)

    def _filtered(self, kind: EntryKind) -> list[Entry]:
        rs = self.entries[kind]
        if self.state_filter == "on":
            rs = [e for e in rs if e.enabled]
        elif self.state_filter == "off":
            rs = [e for e in rs if not e.enabled]
        if self.source_filter == "user":
            rs = [e for e in rs if e.user_path is not None]
        elif self.source_filter == "system":
            rs = [e for e in rs if e.user_path is None and e.system_path is not None]
        return rs

    def _reload(self, kind: EntryKind) -> None:
        """Re-read from disk and repopulate the table."""
        self.entries[kind] = discover_autostart() if kind == "autostart" else discover_launcher()
        self._populate(kind)

    def _populate(self, kind: EntryKind) -> None:
        """Refresh the table view from the in-memory entries + current filters."""
        table = self.query_one(f"#{kind}-table", DataTable)
        cursor_row = table.cursor_row if table.row_count else 0
        table.clear()
        for e in self._filtered(kind):
            table.add_row(*_row_cells(e), key=e.desktop_id)
        if table.row_count:
            table.move_cursor(row=min(cursor_row, table.row_count - 1))

    def _update_subtitle(self) -> None:
        bits: list[str] = []
        if self.state_filter != "all":
            bits.append(f"state={self.state_filter}")
        if self.source_filter != "all":
            bits.append(f"source={self.source_filter}")
        self.sub_title = "filters: " + ", ".join(bits) if bits else "autostart + launcher manager"

    def _refresh_row(self, table: DataTable, row_idx: int, entry: Entry) -> None:
        for col_idx, value in enumerate(_row_cells(entry)):
            table.update_cell_at((row_idx, col_idx), value)
        self._update_preview()

    def _current_entry(self) -> Entry | None:
        kind = self._active_kind()
        table = self.query_one(f"#{kind}-table", DataTable)
        if not table.row_count:
            return None
        try:
            row_key = table.coordinate_to_cell_key((table.cursor_row, 0)).row_key
        except Exception:
            return None
        return next((e for e in self.entries[kind] if e.desktop_id == row_key.value), None)

    def _update_preview(self) -> None:
        preview = self.query_one("#exec-preview", Static)
        entry = self._current_entry()
        if entry is None:
            preview.update("")
            return
        cmd = entry.exec_cmd or "[dim](no Exec= field)[/]"
        path = entry.user_path or entry.system_path
        path_str = str(path) if path else ""
        preview.update(
            f"[b]$[/b] {cmd}\n[dim]{path_str}[/]"
        )


def _row_cells(e: Entry) -> tuple[str, str, str]:
    on_label = " ● ON " if e.kind == "autostart" else " ● SHOW"
    off_label = " ○ OFF" if e.kind == "autostart" else " ○ HIDE"
    state = (
        f"[bold green]{on_label}[/]" if e.enabled else f"[bold red]{off_label}[/]"
    )
    name = e.name if e.enabled else f"[dim]{e.name}[/]"
    return state, e.source, name


def main() -> None:
    AutostartApp().run()


if __name__ == "__main__":
    main()
