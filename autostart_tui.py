#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "textual>=0.86",
# ]
# ///
"""autostart-tui — manage Linux XDG autostart entries from a TUI.

Lists desktop autostart entries from the standard XDG locations and lets
you enable/disable them with a keypress. The Linux answer to Windows
Task Manager's Startup tab.

Sources scanned:
    ~/.config/autostart/    (user)
    /etc/xdg/autostart/     (system)

Disabling a system-only entry creates a user-side override file with
Hidden=true and X-GNOME-Autostart-enabled=false so the system file is
never modified.
"""
from __future__ import annotations

import configparser
import shutil
from dataclasses import dataclass
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Header

USER_DIR = Path("~/.config/autostart").expanduser()
SYSTEM_DIR = Path("/etc/xdg/autostart")


# ---------- Model ----------

@dataclass
class Entry:
    desktop_id: str
    name: str
    exec_cmd: str
    comment: str
    user_path: Path | None
    system_path: Path | None
    enabled: bool

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


def _is_enabled(cp: configparser.RawConfigParser) -> bool:
    de = cp["Desktop Entry"]
    hidden = de.get("Hidden", "false").strip().lower() == "true"
    gnome_off = de.get("X-GNOME-Autostart-enabled", "true").strip().lower() == "false"
    return not (hidden or gnome_off)


def discover() -> list[Entry]:
    by_id: dict[str, Entry] = {}

    if SYSTEM_DIR.is_dir():
        for p in sorted(SYSTEM_DIR.glob("*.desktop")):
            cp = _read_desktop(p)
            if not cp:
                continue
            de = cp["Desktop Entry"]
            did = p.stem
            by_id[did] = Entry(
                desktop_id=did,
                name=de.get("Name", did).strip(),
                exec_cmd=de.get("Exec", "").strip(),
                comment=de.get("Comment", "").strip(),
                user_path=None,
                system_path=p,
                enabled=_is_enabled(cp),
            )

    if USER_DIR.is_dir():
        for p in sorted(USER_DIR.glob("*.desktop")):
            cp = _read_desktop(p)
            if not cp:
                continue
            de = cp["Desktop Entry"]
            did = p.stem
            enabled = _is_enabled(cp)
            existing = by_id.get(did)
            if existing:
                existing.user_path = p
                existing.enabled = enabled
                existing.name = de.get("Name", existing.name).strip()
                existing.exec_cmd = de.get("Exec", existing.exec_cmd).strip()
                existing.comment = de.get("Comment", existing.comment).strip()
            else:
                by_id[did] = Entry(
                    desktop_id=did,
                    name=de.get("Name", did).strip(),
                    exec_cmd=de.get("Exec", "").strip(),
                    comment=de.get("Comment", "").strip(),
                    user_path=p,
                    system_path=None,
                    enabled=enabled,
                )

    return sorted(by_id.values(), key=lambda e: e.name.lower())


def toggle(entry: Entry) -> None:
    """Flip an entry's enabled state, creating a user override if needed."""
    USER_DIR.mkdir(parents=True, exist_ok=True)

    if entry.user_path is None:
        assert entry.system_path is not None
        target = USER_DIR / f"{entry.desktop_id}.desktop"
        shutil.copy2(entry.system_path, target)
        entry.user_path = target

    cp = _read_desktop(entry.user_path)
    if cp is None:
        return

    new_enabled = not entry.enabled
    cp["Desktop Entry"]["Hidden"] = "false" if new_enabled else "true"
    cp["Desktop Entry"]["X-GNOME-Autostart-enabled"] = "true" if new_enabled else "false"

    # Hand-write to avoid configparser's "key = value" spacing.
    with open(entry.user_path, "w", encoding="utf-8") as f:
        for section in cp.sections():
            f.write(f"[{section}]\n")
            for k, v in cp[section].items():
                f.write(f"{k}={v}\n")
            f.write("\n")

    entry.enabled = new_enabled


# ---------- TUI ----------

class AutostartApp(App):
    """Textual app: a table of autostart entries with toggle support."""

    TITLE = "autostart-tui"
    SUB_TITLE = "XDG autostart manager"

    CSS = """
    Screen {
        background: $surface;
    }

    DataTable {
        height: 1fr;
    }

    DataTable > .datatable--header {
        text-style: bold;
        background: $primary 40%;
    }

    DataTable > .datatable--cursor {
        background: $accent 60%;
        color: $text;
    }
    """

    BINDINGS = [
        Binding("space,enter", "toggle", "Toggle"),
        Binding("r", "reload", "Reload"),
        Binding("q,escape", "quit", "Quit"),
        Binding("j", "down", "Down", show=False),
        Binding("k", "up", "Up", show=False),
        Binding("g,home", "top", "Top", show=False),
        Binding("shift+g,end", "bottom", "Bottom", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.entries: list[Entry] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield DataTable(cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("State", width=7)
        table.add_column("Source", width=14)
        table.add_column("Name", width=30)
        table.add_column("Exec")
        self._reload()

    # --- actions ---

    def action_toggle(self) -> None:
        table = self.query_one(DataTable)
        if not self.entries:
            return
        cursor_row = table.cursor_row
        row_key = table.coordinate_to_cell_key((cursor_row, 0)).row_key
        entry = next((e for e in self.entries if e.desktop_id == row_key.value), None)
        if entry is None:
            return
        toggle(entry)
        self.notify(
            f"{'Enabled' if entry.enabled else 'Disabled'}: {entry.name}",
            severity="information" if entry.enabled else "warning",
            timeout=2.0,
        )
        self._refresh_row(table, cursor_row, entry)

    def action_reload(self) -> None:
        self._reload()
        self.notify(f"Reloaded ({len(self.entries)} entries)", timeout=1.5)

    def action_down(self) -> None:
        self.query_one(DataTable).action_cursor_down()

    def action_up(self) -> None:
        self.query_one(DataTable).action_cursor_up()

    def action_top(self) -> None:
        self.query_one(DataTable).move_cursor(row=0)

    def action_bottom(self) -> None:
        table = self.query_one(DataTable)
        if table.row_count:
            table.move_cursor(row=table.row_count - 1)

    # --- helpers ---

    def _reload(self) -> None:
        self.entries = discover()
        table = self.query_one(DataTable)
        cursor_row = table.cursor_row if table.row_count else 0
        table.clear()
        for e in self.entries:
            table.add_row(*_row_cells(e), key=e.desktop_id)
        if table.row_count:
            table.move_cursor(row=min(cursor_row, table.row_count - 1))

    def _refresh_row(self, table: DataTable, row_idx: int, entry: Entry) -> None:
        cells = _row_cells(entry)
        for col_idx, value in enumerate(cells):
            table.update_cell_at((row_idx, col_idx), value)


def _row_cells(e: Entry) -> tuple[str, str, str, str]:
    state = "[bold green]● ON[/]" if e.enabled else "[bold red]○ OFF[/]"
    name = e.name if e.enabled else f"[dim]{e.name}[/]"
    exec_cmd = e.exec_cmd if e.enabled else f"[dim]{e.exec_cmd}[/]"
    return state, e.source, name, exec_cmd


def main() -> None:
    AutostartApp().run()


if __name__ == "__main__":
    main()
