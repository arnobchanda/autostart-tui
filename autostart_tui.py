#!/usr/bin/env python3
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
import curses
import shutil
from dataclasses import dataclass
from pathlib import Path

USER_DIR = Path("~/.config/autostart").expanduser()
SYSTEM_DIR = Path("/etc/xdg/autostart")


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
    cp.optionxform = lambda s: s  # preserve case
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

    # Hand-write to avoid configparser's "key = value" spacing — the desktop
    # spec accepts it but most tools and humans expect "key=value".
    with open(entry.user_path, "w", encoding="utf-8") as f:
        for section in cp.sections():
            f.write(f"[{section}]\n")
            for k, v in cp[section].items():
                f.write(f"{k}={v}\n")
            f.write("\n")

    entry.enabled = new_enabled


# ---------- TUI ----------

HELP = " ↑/↓ or j/k: move │ Space/Enter: toggle │ r: reload │ q: quit "


def _draw(stdscr, entries: list[Entry], idx: int, status: str) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()

    title = " autostart-tui — XDG autostart manager "
    stdscr.attron(curses.A_REVERSE)
    stdscr.addnstr(0, 0, title.ljust(w), w)
    stdscr.attroff(curses.A_REVERSE)

    header = f"  {'STATE':<6}  {'SOURCE':<12}  {'NAME':<28}  EXEC"
    stdscr.addnstr(2, 0, header, w, curses.A_BOLD)

    list_top = 4
    list_h = max(1, h - list_top - 3)
    start = max(0, idx - list_h // 2)
    start = min(start, max(0, len(entries) - list_h))

    for i, e in enumerate(entries[start:start + list_h]):
        row_idx = start + i
        y = list_top + i
        state = " ● ON" if e.enabled else " ○ OFF"
        line = f"  {state:<6}  {e.source:<12}  {e.name[:28]:<28}  {e.exec_cmd}"
        if row_idx == idx:
            stdscr.attron(curses.A_REVERSE)
            stdscr.addnstr(y, 0, line.ljust(w), w)
            stdscr.attroff(curses.A_REVERSE)
        else:
            color = curses.color_pair(1 if e.enabled else 2)
            stdscr.attron(color)
            stdscr.addnstr(y, 0, line, w)
            stdscr.attroff(color)

    if status:
        stdscr.addnstr(h - 2, 0, status, w - 1, curses.A_DIM)
    stdscr.attron(curses.A_REVERSE)
    stdscr.addnstr(h - 1, 0, HELP.ljust(w), w)
    stdscr.attroff(curses.A_REVERSE)
    stdscr.refresh()


def _run(stdscr) -> None:
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_RED, -1)

    entries = discover()
    idx = 0
    status = f"loaded {len(entries)} entries"

    while True:
        _draw(stdscr, entries, idx, status)
        ch = stdscr.getch()

        if ch in (ord("q"), 27):
            break
        if ch in (curses.KEY_UP, ord("k")):
            idx = max(0, idx - 1)
            status = ""
        elif ch in (curses.KEY_DOWN, ord("j")):
            idx = min(max(0, len(entries) - 1), idx + 1)
            status = ""
        elif ch in (curses.KEY_HOME, ord("g")):
            idx = 0
        elif ch in (curses.KEY_END, ord("G")):
            idx = max(0, len(entries) - 1)
        elif ch in (ord(" "), 10, 13):
            if entries:
                e = entries[idx]
                toggle(e)
                status = f"{'enabled' if e.enabled else 'disabled'}: {e.name}"
        elif ch == ord("r"):
            entries = discover()
            idx = min(idx, max(0, len(entries) - 1))
            status = f"reloaded ({len(entries)} entries)"


def main() -> None:
    curses.wrapper(_run)


if __name__ == "__main__":
    main()
