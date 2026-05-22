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
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import (
    DataTable,
    Footer,
    Input,
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
ProgressFn = Callable[[], None] | None


@dataclass
class Entry:
    kind: EntryKind
    desktop_id: str
    name: str
    exec_cmd: str
    icon_name: str  # freedesktop Icon= field, used to pick a glyph
    user_path: Path | None
    system_path: Path | None
    enabled: bool  # for launcher entries: "visible in launcher"
    # Populated after discovery: best-match boot time (in milliseconds) from
    # systemd-analyze blame, or None if no matching unit was found.
    boot_ms: int | None = field(default=None)

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
        existing.icon_name = de.get("Icon", existing.icon_name).strip() or existing.icon_name
    else:
        by_id[did] = Entry(
            kind=kind,
            desktop_id=did,
            name=de.get("Name", did).strip() or did,
            exec_cmd=de.get("Exec", "").strip(),
            icon_name=de.get("Icon", "").strip(),
            user_path=p,
            system_path=None,
            enabled=enabled,
        )


def discover_autostart(progress: ProgressFn = None) -> list[Entry]:
    by_id: dict[str, Entry] = {}
    if AUTOSTART_SYSTEM.is_dir():
        for p in sorted(AUTOSTART_SYSTEM.glob("*.desktop")):
            cp = _read_desktop(p)
            if progress:
                progress()
            if not cp:
                continue
            de = cp["Desktop Entry"]
            did = p.stem
            by_id[did] = Entry(
                kind="autostart",
                desktop_id=did,
                name=de.get("Name", did).strip() or did,
                exec_cmd=de.get("Exec", "").strip(),
                icon_name=de.get("Icon", "").strip(),
                user_path=None,
                system_path=p,
                enabled=_autostart_enabled(cp),
            )
    if AUTOSTART_USER.is_dir():
        for p in sorted(AUTOSTART_USER.glob("*.desktop")):
            cp = _read_desktop(p)
            if progress:
                progress()
            if not cp:
                continue
            _merge_user_over_system(by_id, p, cp, "autostart", _autostart_enabled(cp))
    return sorted(by_id.values(), key=lambda e: e.name.lower())


def discover_launcher(progress: ProgressFn = None) -> list[Entry]:
    by_id: dict[str, Entry] = {}
    for d in LAUNCHER_DIRS_SYSTEM:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.desktop")):
            cp = _read_desktop(p)
            if progress:
                progress()
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
                icon_name=de.get("Icon", "").strip(),
                user_path=None,
                system_path=p,
                enabled=_launcher_visible(cp),
            ))
    for d in LAUNCHER_DIRS_USER:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.desktop")):
            cp = _read_desktop(p)
            if progress:
                progress()
            if not cp:
                continue
            de = cp["Desktop Entry"]
            if de.get("Type", "Application").strip() != "Application":
                continue
            _merge_user_over_system(by_id, p, cp, "launcher", _launcher_visible(cp))
    return sorted(by_id.values(), key=lambda e: e.name.lower())


def _count_desktop_files() -> int:
    total = 0
    for d in (AUTOSTART_SYSTEM, AUTOSTART_USER, *LAUNCHER_DIRS_SYSTEM, *LAUNCHER_DIRS_USER):
        if d.is_dir():
            total += sum(1 for _ in d.glob("*.desktop"))
    return total


def toggle_autostart(entry: Entry) -> None:
    path = ensure_user_override(entry)
    cp = _read_desktop(path)
    if cp is None:
        return
    new = not entry.enabled
    cp["Desktop Entry"]["Hidden"] = "false" if new else "true"
    cp["Desktop Entry"]["X-GNOME-Autostart-enabled"] = "true" if new else "false"
    _write_desktop(path, cp)
    entry.enabled = new


def toggle_launcher(entry: Entry) -> None:
    path = ensure_user_override(entry)
    cp = _read_desktop(path)
    if cp is None:
        return
    new_visible = not entry.enabled
    if new_visible:
        # Showing: clear BOTH Hidden and NoDisplay, because either being
        # true (in this override or inherited from the system file) would
        # keep the entry hidden.
        cp["Desktop Entry"]["Hidden"] = "false"
        cp["Desktop Entry"]["NoDisplay"] = "false"
    else:
        # Hiding: Hidden=true is the XDG-semantic "user deleted this entry"
        # flag. Clear NoDisplay so the state is unambiguous if we toggle
        # again later.
        cp["Desktop Entry"]["Hidden"] = "true"
        cp["Desktop Entry"]["NoDisplay"] = "false"
    _write_desktop(path, cp)
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


# ---------- Risk / criticality ----------

# Substrings that mark an entry as "critical to session" — disabling these
# from the TUI pops a confirmation dialog first, since silently breaking
# audio / input / secrets / portals tends to cost a reboot to recover.
CRITICAL_PATTERNS: list[str] = [
    "pipewire",
    "wireplumber",
    "pulseaudio",
    "keyring",
    "secret",
    "fcitx",
    "ibus",
    "input-method",
    "xdg-desktop-portal",
    "polkit",
    "wayland-session",
    "gnome-session",
    "walker",
    "hyprland",
]


def is_critical(e: Entry) -> bool:
    # Only meaningful for autostart entries. Hiding a launcher entry just
    # removes it from app-menu listings — the underlying app isn't affected,
    # so there's no session-breaking risk.
    if e.kind != "autostart":
        return False
    hay = f"{e.desktop_id} {e.exec_cmd}".lower()
    return any(p in hay for p in CRITICAL_PATTERNS)


# ---------- Boot times (systemd-analyze) ----------

_BLAME_LINE_RE = re.compile(r"\s*([\d.]+)(ms|s|min)\s+(.+)")


def _parse_blame_output(out: str) -> dict[str, int]:
    times: dict[str, int] = {}
    for line in out.splitlines():
        m = _BLAME_LINE_RE.match(line)
        if not m:
            continue
        val, unit, name = m.groups()
        ms = float(val)
        if unit == "s":
            ms *= 1000
        elif unit == "min":
            ms *= 60_000
        times[name.strip().lower()] = int(ms)
    return times


def load_boot_times() -> dict[str, int]:
    """Best-effort scrape of systemd-analyze blame for both --user and the
    system instance. Silently returns {} if systemd isn't there."""
    times: dict[str, int] = {}
    for argv in (
        ["systemd-analyze", "blame", "--user"],
        ["systemd-analyze", "blame"],
    ):
        try:
            res = subprocess.run(
                argv, capture_output=True, timeout=4, check=False
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        if res.returncode != 0:
            continue
        times.update(_parse_blame_output(res.stdout.decode(errors="replace")))
    return times


def match_boot_time(entry: Entry, boot: dict[str, int]) -> int | None:
    """Heuristic: try desktop_id, name, and Exec basename against known
    unit names with common suffixes including the XDG autostart wrapper
    pattern systemd uses (`app-<id>@autostart.service`, with `-` escaped
    to `\\x2d` in the parts derived from the entry name)."""
    if not boot:
        return None
    did = entry.desktop_id
    did_l = did.lower()
    candidates: list[str] = []
    # XDG autostart wrappers
    for variant in {did, did_l}:
        candidates.append(f"app-{variant}@autostart.service")
        candidates.append(f"app-{variant.replace('-', r'\x2d')}@autostart.service")
    # Direct unit names
    candidates += [did_l, did_l + ".service", did_l + ".target"]
    # Binary basename from Exec
    if entry.exec_cmd:
        bin_name = entry.exec_cmd.split()[0].split("/")[-1].lower()
        if bin_name and bin_name not in ("env", "sh", "bash"):
            candidates += [bin_name, bin_name + ".service"]
    # Slugified display name
    slug = re.sub(r"[^a-z0-9]+", "-", entry.name.lower()).strip("-")
    if slug:
        candidates += [slug, slug + ".service"]
    for key in candidates:
        if key.lower() in boot:
            return boot[key.lower()]
    return None


# ---------- Override file helpers ----------

def ensure_user_override(entry: Entry) -> Path:
    """Make sure entry.user_path exists and is a complete entry.

    Per the XDG Desktop Entry Spec, when the same desktop ID exists in
    both ~/.local/share/applications/ and /usr/share/applications/, the
    user file FULLY shadows the system file — fields are not merged. So
    if a user override is missing Name/Exec/Type, launchers will silently
    drop the entry. We guarantee the user file is a full copy of the
    system file (plus whatever keys the user has already overridden) so
    a partial override can never break the entry."""
    target_dir = AUTOSTART_USER if entry.kind == "autostart" else LAUNCHER_USER_WRITE_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{entry.desktop_id}.desktop"

    if entry.user_path is None:
        assert entry.system_path is not None
        shutil.copy2(entry.system_path, target)
        entry.user_path = target
    elif entry.system_path is not None:
        _backfill_missing_keys(entry.user_path, entry.system_path)

    return entry.user_path


def _backfill_missing_keys(user_path: Path, system_path: Path) -> None:
    """Add any [Desktop Entry] keys present in system_path but missing
    from user_path. Keys already set in the user file are preserved —
    this only fills gaps so launchers see a complete entry."""
    user_cp = _read_desktop(user_path)
    sys_cp = _read_desktop(system_path)
    if user_cp is None or sys_cp is None:
        return
    user_de = user_cp["Desktop Entry"]
    sys_de = sys_cp["Desktop Entry"]
    changed = False
    for k, v in sys_de.items():
        if k not in user_de:
            user_de[k] = v
            changed = True
    if changed:
        _write_desktop(user_path, user_cp)


def _desktop_entry_keys(path: Path) -> dict[str, str]:
    """Return {key: value} from a .desktop file's [Desktop Entry] section,
    preserving case. Empty dict if the file can't be read."""
    cp = _read_desktop(path)
    if cp is None:
        return {}
    return dict(cp["Desktop Entry"].items())


def _truncate_value(s: str, limit: int = 80) -> str:
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _summarize_override_effect(
    entry: Entry, sys_keys: dict[str, str], usr_keys: dict[str, str]
) -> str:
    """Plain-English one-liner describing what the override does, so the
    user doesn't have to reverse-engineer +/- lines to figure out intent."""
    hidden = usr_keys.get("Hidden", sys_keys.get("Hidden", "false")).lower() == "true"
    no_display = (
        usr_keys.get("NoDisplay", sys_keys.get("NoDisplay", "false")).lower() == "true"
    )
    gnome_off = (
        usr_keys.get("X-GNOME-Autostart-enabled", "true").lower() == "false"
    )
    if entry.kind == "autostart":
        if hidden or gnome_off:
            return "[yellow]Effect:[/] disables this autostart entry"
        return "[green]Effect:[/] keeps this autostart entry enabled"
    # launcher
    if hidden or no_display:
        return "[yellow]Effect:[/] hides this entry from app launchers"
    return "[green]Effect:[/] keeps this entry visible in launchers"


def render_override_diff(entry: Entry) -> str:
    """Show what the user override actually changes vs the system file.

    .desktop files are key=value records, not free-form text, so a line
    diff produces a lot of noise when the override is minimal (most of
    the system file's keys appear as "removed" even though the launcher
    would still fall back to them). We instead diff the parsed maps and
    surface:
        * a plain-English summary of the override's net effect
        * keys the override modifies (red old → green new)
        * keys the override adds (green +)
        * a compact one-line summary of system-only keys
    """
    if entry.user_path is None or entry.system_path is None:
        return ""
    sys_keys = _desktop_entry_keys(entry.system_path)
    usr_keys = _desktop_entry_keys(entry.user_path)

    out: list[str] = [_summarize_override_effect(entry, sys_keys, usr_keys), ""]

    # Modified keys first — most relevant to "what did the override change".
    for k in sorted(usr_keys):
        uv = usr_keys[k]
        sv = sys_keys.get(k)
        if sv is None:
            continue
        if sv != uv:
            out.append(f"[red]- {k}={_truncate_value(sv)}[/]")
            out.append(f"[green]+ {k}={_truncate_value(uv)}[/]")

    # New keys introduced by the override.
    for k in sorted(usr_keys):
        if k not in sys_keys:
            out.append(f"[green]+ {k}={_truncate_value(usr_keys[k])}[/]")

    # Keys in system but absent from the override — compact summary line.
    only_system = sorted(set(sys_keys) - set(usr_keys))
    if only_system:
        shown = only_system[:6]
        more = "" if len(only_system) <= 6 else f" (+{len(only_system) - 6} more)"
        out.append(
            f"[dim]system-only: {', '.join(shown)}{more}[/]"
        )

    if not out:
        out.append("[dim italic]override matches system exactly[/]")
    return "\n".join(out)


# ---------- Glyph mapping ----------

# Substring → nerd-font glyph. Checked against the freedesktop Icon= value,
# longest substring first so brand-specific keys win over generic categories.
ICON_GLYPH_MAP: list[tuple[str, str]] = [
    # Specific brands / known icon names
    ("preferences-desktop-startup", "󱓞"),
    ("multimedia-volume-control", "󰕾"),
    ("system-software-update", "󰚰"),
    ("system-file-manager", "󰉋"),
    ("accessories-text-editor", "󰷈"),
    ("input-keyboard", "󰌌"),
    ("input-method", "󰌌"),
    ("gnome-disks", "󰋊"),
    ("disk-utility", "󰋊"),
    ("snapper", "󰋊"),
    ("at-spi", "󰠾"),
    ("geoclue", "󰍎"),
    ("keyring", "󰌾"),
    ("password", "󰌾"),
    ("1password", "󰢁"),
    ("nextcloud", "󰒖"),
    ("remmina", "󰢹"),
    ("walker", ""),
    ("fcitx", "󰌌"),
    ("spotify", "󰓇"),
    ("discord", "󰙯"),
    ("slack", "󰒱"),
    ("github", "󰊤"),
    ("firefox", ""),
    ("chrome", ""),
    ("chromium", ""),
    ("brave", ""),
    ("vscode", "󰨞"),
    ("obsidian", "󱓧"),
    ("notion", "󰇈"),
    ("docker", ""),
    ("limine", "󰋊"),
    ("tracker", "󰈚"),
    ("user-dirs", "󰉋"),
    ("ghostty", "󰊠"),
    ("alacritty", ""),
    ("battery", "󰂀"),
    ("bluetooth", "󰂯"),
    ("network", "󰖩"),
    # Category fallbacks (freedesktop standard icon names)
    ("file-manager", "󰉋"),
    ("text-editor", "󰷈"),
    ("image-viewer", "󰋩"),
    ("media-player", "󰐊"),
    ("web-browser", "󰖟"),
    ("system-monitor", "󰓅"),
    ("preferences", "󰒓"),
    ("calculator", "󰪚"),
    ("calendar", "󰸗"),
    ("terminal", ""),
    ("settings", "󰒓"),
    ("development", "󰨞"),
    ("internet", "󰖟"),
    ("browser", "󰖟"),
    ("messaging", "󰭹"),
    ("graphics", "󰋩"),
    ("document", "󰈙"),
    ("office", "󰈙"),
    ("system", "󰒓"),
    ("audio", "󰓃"),
    ("music", "󰝚"),
    ("video", "󰕧"),
    ("image", "󰋩"),
    ("mail", "󰇮"),
    ("chat", "󰭹"),
    ("game", "󰊗"),
    ("print", "󰐪"),
    ("pdf", "󰈦"),
    ("headset", "󰋎"),
    ("camera", "󰄄"),
]

DEFAULT_GLYPH = "󰍹"  # monitor


def _boot_cell(boot_ms: int | None) -> str:
    """6-cell block bar + ms label, colored by speed bucket. Empty if no data."""
    if boot_ms is None:
        return ""
    bar_width = 6
    # 800 ms → full bar; anything slower still saturates at full.
    filled = min(bar_width, max(1, int(bar_width * boot_ms / 800)))
    bar = "█" * filled + "░" * (bar_width - filled)
    if boot_ms < 100:
        color = "green"
    elif boot_ms < 400:
        color = "yellow"
    else:
        color = "red"
    return f"[{color}]{bar}[/] {boot_ms}ms"


def icon_to_glyph(icon_name: str) -> str:
    if not icon_name:
        return DEFAULT_GLYPH
    low = icon_name.lower()
    for key, glyph in ICON_GLYPH_MAP:
        if key in low:
            return glyph
    return DEFAULT_GLYPH


# ---------- TUI ----------

# Block-letter banner for "autostart-tui". 2 lines tall, ~50 chars wide.
BANNER_TITLE = (
    "▄▀█ █░█ ▀█▀ █▀█ ▄▄ █▀ ▀█▀ ▄▀█ █▀█ ▀█▀ ▄▄ ▀█▀ █░█ █\n"
    "█▀█ █▄█ ░█░ █▄█ ░░ ▄█ ░█░ █▀█ █▀▄ ░█░ ░░ ░█░ █▄█ █"
)


class Banner(Vertical):
    """Header replacement: stylized title plus a one-line stats panel."""

    DEFAULT_CSS = """
    Banner {
        height: 4;
        padding: 1 2 0 2;
        background: $surface;
    }
    Banner > #banner-title {
        color: $accent;
        text-style: bold;
        height: 2;
    }
    Banner > #banner-stats {
        color: $foreground 70%;
        height: 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(BANNER_TITLE, id="banner-title")
        yield Static("", id="banner-stats", markup=True)

    def set_stats(self, stats: str) -> None:
        self.query_one("#banner-stats", Static).update(stats)


StateFilter = Literal["all", "on", "off"]
SourceFilter = Literal["all", "user", "system"]

STATE_CYCLE: dict[StateFilter, StateFilter] = {"all": "on", "on": "off", "off": "all"}
SOURCE_CYCLE: dict[SourceFilter, SourceFilter] = {
    "all": "user",
    "user": "system",
    "system": "all",
}


class RiskConfirm(ModalScreen[bool]):
    """Modal asking the user to confirm disabling a critical entry."""

    BINDINGS = [
        Binding("y", "confirm", "Yes — disable"),
        Binding("n,escape,q", "cancel", "No — cancel"),
        Binding("enter", "confirm", "", priority=True),
    ]

    DEFAULT_CSS = """
    RiskConfirm {
        align: center middle;
    }
    #risk-dialog {
        width: 64;
        height: auto;
        background: $surface;
        border: thick $warning;
        padding: 1 2;
    }
    #risk-title {
        color: $warning;
        text-style: bold;
        height: 1;
    }
    #risk-body {
        color: $foreground;
        margin: 1 0;
        height: auto;
    }
    #risk-keys {
        color: $foreground 70%;
        height: 1;
        text-align: center;
    }
    """

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name

    def compose(self) -> ComposeResult:
        with Vertical(id="risk-dialog"):
            yield Label("⚠  Disable a session-critical entry?", id="risk-title")
            yield Static(
                f"You're about to disable [bold]{self._name}[/].\n\n"
                "This entry looks like it's part of session plumbing "
                "(audio, secrets, input method, portal, etc.). "
                "Disabling it may break the next login and require "
                "a manual fix from a TTY.",
                id="risk-body",
                markup=True,
            )
            yield Static("[bold]y[/]  yes, disable     [bold]n[/]  cancel", id="risk-keys")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class DesktopFileEditor(ModalScreen[bool]):
    """Modal that lets the user edit the .desktop file in a TextArea."""

    BINDINGS = [
        Binding("ctrl+s", "save", "Save"),
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    DEFAULT_CSS = """
    DesktopFileEditor {
        align: center middle;
    }
    #edit-dialog {
        width: 90%;
        height: 80%;
        background: $surface;
        border: thick $accent;
        padding: 0;
    }
    #edit-title {
        background: $accent 40%;
        color: $foreground;
        text-style: bold;
        padding: 0 2;
        height: 1;
    }
    #edit-path {
        background: $panel;
        color: $accent;
        padding: 0 2;
        height: 1;
    }
    #edit-area {
        height: 1fr;
        border: none;
    }
    #edit-help {
        background: $panel;
        color: $foreground 70%;
        padding: 0 2;
        height: 1;
    }
    """

    def __init__(self, name: str, path: Path, content: str) -> None:
        super().__init__()
        self._name = name
        self._path = path
        self._content = content

    def compose(self) -> ComposeResult:
        with Vertical(id="edit-dialog"):
            yield Label(f"  {self._name}", id="edit-title")
            yield Label(str(self._path), id="edit-path")
            yield TextArea.code_editor(
                self._content,
                language="ini",
                id="edit-area",
                show_line_numbers=True,
            )
            yield Label(
                "Ctrl+S to save   ·   Esc to cancel",
                id="edit-help",
            )

    def action_save(self) -> None:
        new_text = self.query_one("#edit-area", TextArea).text
        try:
            self._path.write_text(new_text, encoding="utf-8")
        except OSError as exc:
            self.notify(f"Save failed: {exc}", severity="error", timeout=3.0)
            return
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class DesktopFilePreview(ModalScreen[None]):
    """Modal dialog showing the raw contents of a .desktop file."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("q", "dismiss", "Close", show=False),
        # priority=True so we win over TextArea's own enter binding.
        Binding("enter", "dismiss", "Close", priority=True),
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

    #main-row {
        height: 1fr;
    }

    #main-tabs {
        width: 1fr;
    }

    #details-pane {
        width: 50;
        background: $surface;
        border-left: solid $primary 40%;
        padding: 1 2;
    }

    #details-pane.-hidden {
        display: none;
    }

    #details-content {
        height: auto;
    }

    #search-input {
        background: $surface;
        border: tall $primary 50%;
        margin: 0 0 0 0;
    }

    #search-input.-hidden {
        display: none;
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
        Binding("z", "undo", "Undo"),
        Binding("i", "toggle_details", "Details"),
        Binding("slash", "search", "Search"),
        Binding("e", "edit", "Edit file"),
        # DataTable's own enter binding fires RowSelected — we listen for
        # that event (see on_data_table_row_selected) instead of binding
        # enter directly. Keeping a non-firing binding here so Footer
        # still shows "Preview" as a discoverable key, and so the command
        # palette (Ctrl+P) isn't pre-empted by a priority binding.
        Binding("enter", "preview", "Preview"),
        Binding("f", "cycle_state", "State filter"),
        Binding("s", "cycle_source", "Source filter"),
        Binding("c", "clear_filters", "Clear filters"),
        Binding("r", "reload", "Reload"),
        Binding("1", "show_tab('autostart')", "Autostart"),
        Binding("2", "show_tab('launcher')", "Launcher"),
        Binding("tab,right,l", "next_tab", "Next tab", show=False),
        Binding("shift+tab,left,h", "prev_tab", "Prev tab", show=False),
        Binding("q", "quit", "Quit"),
        Binding("escape", "escape", "Quit / cancel search", show=False),
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
        # Resolved accent color used by row icons. Overridden in on_mount once
        # the theme is registered. Default works for Catppuccin Mocha derivatives.
        self._accent_color: str = "#fab387"
        # Track the most recent toggle so the user can press `z` to undo it.
        self._last_toggle_id: tuple[EntryKind, str] | None = None
        # Live name search (case-insensitive substring). Empty = no filter.
        self.search_query: str = ""

    def compose(self) -> ComposeResult:
        yield Banner()
        yield Input(
            placeholder="search by name (Esc to clear, Enter to confirm)",
            id="search-input",
            classes="-hidden",
            disabled=True,  # also prevents focus until action_search shows it
        )
        with Horizontal(id="main-row"):
            with TabbedContent(initial="autostart-tab", id="main-tabs"):
                with TabPane("󱓞  Autostart [1]", id="autostart-tab"):
                    yield DataTable(
                        id="autostart-table", cursor_type="row", zebra_stripes=True
                    )
                with TabPane("󰀻  Launcher [2]", id="launcher-tab"):
                    yield DataTable(
                        id="launcher-table", cursor_type="row", zebra_stripes=True
                    )
            with VerticalScroll(id="details-pane"):
                yield Static(
                    "[dim italic]Loading…[/]", id="details-content", markup=True
                )
        yield Static("", id="exec-preview", markup=True)
        yield Footer()

    def on_mount(self) -> None:
        theme = load_omarchy_theme()
        if theme is not None:
            self.register_theme(theme)
            self.theme = "omarchy"
            self._accent_color = theme.accent

        for tid in ("#autostart-table", "#launcher-table"):
            table = self.query_one(tid, DataTable)
            table.add_column(" ", width=3)  # icon glyph
            table.add_column("State", width=8)
            table.add_column("Source", width=14)
            table.add_column("Boot", width=15)
            table.add_column("Name")
            table.loading = True  # built-in spinner overlay

        self._active_table().focus()
        self.query_one("#exec-preview", Static).update(
            "[dim italic]Scanning desktop entries…[/]"
        )
        self._refresh_banner()
        self._discover_all()

    # --- actions ---

    def action_toggle(self) -> None:
        entry = self._current_entry()
        if entry is None:
            return
        # If we're about to disable a session-critical entry, ask first.
        if entry.enabled and is_critical(entry):
            def on_confirm(confirmed: bool | None) -> None:
                if confirmed:
                    self._apply_toggle(entry)
            self.push_screen(RiskConfirm(entry.name), on_confirm)
            return
        self._apply_toggle(entry)

    def _apply_toggle(self, entry: Entry) -> None:
        kind = entry.kind
        (toggle_autostart if kind == "autostart" else toggle_launcher)(entry)
        verb = "Enabled" if entry.enabled else ("Disabled" if kind == "autostart" else "Hidden")
        if kind == "launcher" and entry.enabled:
            verb = "Shown"
        self._last_toggle_id = (kind, entry.desktop_id)
        self.notify(
            f"{verb}: {entry.name}  ·  press z to undo",
            severity="information" if entry.enabled else "warning",
            timeout=4.0,
        )
        no_filter = (
            self.state_filter == "all"
            and self.source_filter == "all"
            and not self.search_query
        )
        if no_filter:
            t = self._active_table()
            self._pulse_row(t, t.cursor_row, entry)
        else:
            self._populate(kind)
        self._refresh_banner()
        self._update_details()

    def action_undo(self) -> None:
        if self._last_toggle_id is None:
            self.notify("Nothing to undo", severity="warning", timeout=1.0)
            return
        kind, did = self._last_toggle_id
        entry = next((e for e in self.entries[kind] if e.desktop_id == did), None)
        if entry is None:
            self.notify("Entry no longer present", severity="warning", timeout=1.0)
            self._last_toggle_id = None
            return
        (toggle_autostart if kind == "autostart" else toggle_launcher)(entry)
        self._last_toggle_id = None
        self.notify(f"Undone: {entry.name}", timeout=2.0)
        no_filter = (
            self.state_filter == "all"
            and self.source_filter == "all"
            and not self.search_query
        )
        if no_filter:
            t = self.query_one(f"#{kind}-table", DataTable)
            for row_idx in range(t.row_count):
                key = t.coordinate_to_cell_key((row_idx, 0)).row_key
                if key.value == did:
                    self._pulse_row(t, row_idx, entry)
                    break
        else:
            self._populate(kind)
        self._refresh_banner()
        self._update_details()

    def action_toggle_details(self) -> None:
        pane = self.query_one("#details-pane")
        pane.toggle_class("-hidden")

    def action_edit(self) -> None:
        entry = self._current_entry()
        if entry is None:
            return
        # Always edit the user-side override. If the entry is system-only,
        # create the override first (a copy of the system file).
        try:
            path = ensure_user_override(entry)
        except OSError as exc:
            self.notify(f"Cannot create override: {exc}", severity="error", timeout=3.0)
            return
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            self.notify(f"Read failed: {exc}", severity="error", timeout=3.0)
            return

        def on_done(saved: bool | None) -> None:
            if not saved:
                return
            # The file may have changed enabled-state, name, exec, etc.
            # Easiest correct refresh: re-read everything.
            self.notify("Saved · reloading", timeout=1.5)
            self.action_reload()

        self.push_screen(DesktopFileEditor(entry.name, path, content), on_done)

    def action_search(self) -> None:
        inp = self.query_one("#search-input", Input)
        inp.disabled = False
        inp.remove_class("-hidden")
        inp.focus()

    def action_escape(self) -> None:
        # When search input has focus: clear the filter and hide it.
        # Otherwise behave like quit.
        inp = self.query_one("#search-input", Input)
        if inp.has_focus or not inp.has_class("-hidden"):
            self._close_search(clear=True)
        else:
            self.exit()

    def _close_search(self, clear: bool) -> None:
        inp = self.query_one("#search-input", Input)
        if clear:
            inp.value = ""
            self.search_query = ""
            self._populate("autostart")
            self._populate("launcher")
            self._refresh_banner()
        inp.add_class("-hidden")
        inp.disabled = True  # take it out of the focus chain again
        self._active_table().focus()

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
        for tid in ("#autostart-table", "#launcher-table"):
            self.query_one(tid, DataTable).loading = True
        self.notify("Reloading…", timeout=1.0)
        self._discover_all()

    def action_cycle_state(self) -> None:
        self.state_filter = STATE_CYCLE[self.state_filter]
        self._populate(self._active_kind())
        self._populate(self._other_kind())
        self.notify(f"State filter: {self.state_filter}", timeout=1.0)
        self._refresh_banner()

    def action_cycle_source(self) -> None:
        self.source_filter = SOURCE_CYCLE[self.source_filter]
        self._populate(self._active_kind())
        self._populate(self._other_kind())
        self.notify(f"Source filter: {self.source_filter}", timeout=1.0)
        self._refresh_banner()

    def action_clear_filters(self) -> None:
        self.state_filter = "all"
        self.source_filter = "all"
        self.search_query = ""
        inp = self.query_one("#search-input", Input)
        inp.value = ""
        inp.add_class("-hidden")
        inp.disabled = True
        self._populate("autostart")
        self._populate("launcher")
        self.notify("Filters cleared", timeout=1.0)
        self._refresh_banner()

    def action_show_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = f"{tab_id}-tab"
        self._active_table().focus()
        self._update_preview()
        self._update_details()

    def action_next_tab(self) -> None:
        tabs = self.query_one(TabbedContent)
        tabs.active = "launcher-tab" if tabs.active == "autostart-tab" else "autostart-tab"
        self._active_table().focus()
        self._update_preview()
        self._update_details()

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
        self._update_details()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "search-input":
            return
        self.search_query = event.value
        self._populate("autostart")
        self._populate("launcher")
        self._refresh_banner()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "search-input":
            return
        # Enter: confirm filter, hide input, return focus to the table
        # (keep the search active so the user can navigate results).
        self._close_search(clear=False)

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        # Fires on mouse clicks AND keyboard switches, so it catches what
        # the action_* keybindings can't (clicking a tab header with the
        # mouse never triggers action_show_tab).
        self._active_table().focus()
        self._update_preview()
        self._update_details()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Fires when DataTable has focus and the user presses Enter (or
        # double-clicks a row). Open the preview modal from here so the
        # command palette and other screens don't get hijacked.
        self.action_preview()

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
        if self.search_query:
            q = self.search_query.lower()
            rs = [e for e in rs if q in e.name.lower()]
        return rs

    def _reload(self, kind: EntryKind) -> None:
        """Synchronous re-read (kept for explicit single-kind refreshes)."""
        self.entries[kind] = discover_autostart() if kind == "autostart" else discover_launcher()
        self._populate(kind)

    @work(thread=True, exclusive=True, group="discover")
    def _discover_all(self) -> None:
        """Run both discovery passes off the main thread so the UI stays
        responsive while we read ~150 .desktop files. Updates a counter in
        the preview strip every few files so the user sees progress instead
        of a generic spinner."""
        total = max(1, _count_desktop_files())
        scanned = [0]

        def progress() -> None:
            scanned[0] += 1
            # Throttle UI updates: every 5 files or on the final tick.
            if scanned[0] % 5 == 0 or scanned[0] == total:
                self.call_from_thread(self._set_scan_progress, scanned[0], total)

        autostart = discover_autostart(progress)
        launcher = discover_launcher(progress)
        # Scrape systemd-analyze blame in the same worker so we don't block
        # the UI later. Best-effort: silently skipped if systemd isn't there.
        boot = load_boot_times()
        for e in (*autostart, *launcher):
            e.boot_ms = match_boot_time(e, boot)
        self.call_from_thread(self._on_discovery_done, autostart, launcher)

    def _set_scan_progress(self, current: int, total: int) -> None:
        bar_width = 20
        filled = int(bar_width * current / total)
        bar = "█" * filled + "░" * (bar_width - filled)
        self.query_one("#exec-preview", Static).update(
            f"[dim italic]Scanning desktop entries…  [/]"
            f"[{self._accent_color}]{bar}[/]  [dim]{current}/{total}[/]"
        )

    def _on_discovery_done(
        self, autostart: list[Entry], launcher: list[Entry]
    ) -> None:
        self.entries["autostart"] = autostart
        self.entries["launcher"] = launcher
        for kind, tid in (("autostart", "#autostart-table"), ("launcher", "#launcher-table")):
            table = self.query_one(tid, DataTable)
            self._populate(kind)
            table.loading = False
        self._update_preview()
        self._update_details()
        self._refresh_banner()

    def _populate(self, kind: EntryKind) -> None:
        """Refresh the table view from the in-memory entries + current filters."""
        table = self.query_one(f"#{kind}-table", DataTable)
        cursor_row = table.cursor_row if table.row_count else 0
        table.clear()
        for e in self._filtered(kind):
            table.add_row(*self._row_cells(e), key=e.desktop_id)
        if table.row_count:
            table.move_cursor(row=min(cursor_row, table.row_count - 1))

    def _refresh_banner(self) -> None:
        """Rewrite the banner stats line: counts + active filters."""
        a = self.entries["autostart"]
        l_ = self.entries["launcher"]
        a_on = sum(1 for e in a if e.enabled)
        l_vis = sum(1 for e in l_ if e.enabled)
        parts: list[str] = []
        if a:
            parts.append(
                f"[bold]Autostart[/]  {a_on} on  [dim]·[/]  {len(a) - a_on} off"
            )
        if l_:
            parts.append(
                f"[bold]Launcher[/]  {l_vis} visible  [dim]·[/]  {len(l_) - l_vis} hidden"
            )
        if not parts:
            parts.append("[dim italic]loading…[/]")
        filter_bits: list[str] = []
        if self.state_filter != "all":
            filter_bits.append(f"state={self.state_filter}")
        if self.source_filter != "all":
            filter_bits.append(f"source={self.source_filter}")
        if self.search_query:
            filter_bits.append(f'name="{self.search_query}"')
        if filter_bits:
            parts.append("[bold yellow]filters:[/] " + ", ".join(filter_bits))
        self.query_one(Banner).set_stats("   [dim]│[/]   ".join(parts))

    def _refresh_row(self, table: DataTable, row_idx: int, entry: Entry) -> None:
        for col_idx, value in enumerate(self._row_cells(entry)):
            table.update_cell_at((row_idx, col_idx), value)
        self._update_preview()

    def _pulse_row(self, table: DataTable, row_idx: int, entry: Entry) -> None:
        """Briefly invert the row in green/red after a toggle, then settle."""
        flash_color = "bright_green" if entry.enabled else "bright_red"
        for col_idx, value in enumerate(self._row_cells(entry)):
            table.update_cell_at(
                (row_idx, col_idx), f"[reverse bold {flash_color}]{value}[/]"
            )
        self._update_preview()
        # 220 ms is short enough to feel like an animation but long enough
        # to register as feedback that "something changed".
        self.set_timer(0.22, lambda: self._settle_row(table, row_idx, entry))

    def _settle_row(self, table: DataTable, row_idx: int, entry: Entry) -> None:
        # Verify the row is still where we left it (no reload happened in
        # the meantime) before writing back the normal cells.
        if row_idx >= table.row_count:
            return
        try:
            row_key = table.coordinate_to_cell_key((row_idx, 0)).row_key
        except Exception:
            return
        if row_key.value == entry.desktop_id:
            self._refresh_row(table, row_idx, entry)

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

    def _update_details(self) -> None:
        widget = self.query_one("#details-content", Static)
        entry = self._current_entry()
        if entry is None:
            widget.update("[dim italic]No entry selected[/]")
            return
        widget.update(self._format_details(entry))

    def _format_details(self, e: Entry) -> str:
        glyph = icon_to_glyph(e.icon_name)
        if e.kind == "autostart":
            state = (
                "[bold green]● ENABLED[/]" if e.enabled else "[bold red]○ DISABLED[/]"
            )
        else:
            state = (
                "[bold green]● VISIBLE[/]" if e.enabled else "[bold red]○ HIDDEN[/]"
            )
        # Pull Comment / Categories from whichever .desktop file we have
        path = e.user_path or e.system_path
        comment = ""
        categories = ""
        if path is not None:
            cp = _read_desktop(path)
            if cp is not None:
                de = cp["Desktop Entry"]
                comment = de.get("Comment", "").strip()
                categories = de.get("Categories", "").strip().rstrip(";")
        lines: list[str] = [
            f"[bold {self._accent_color}]{glyph}  {e.name}[/]",
            "",
            state,
            f"[bold]Source:[/]  {e.source}",
        ]
        if e.icon_name:
            lines.append(f"[bold]Icon:[/]    [dim]{e.icon_name}[/]")
        if comment:
            lines += ["", "[bold]Comment[/]", comment]
        if categories:
            cat_pretty = " · ".join(c for c in categories.split(";") if c)
            lines += ["", "[bold]Categories[/]", f"[dim]{cat_pretty}[/]"]
        if e.exec_cmd:
            lines += ["", "[bold]Exec[/]", f"[dim]{e.exec_cmd}[/]"]
        if e.boot_ms is not None:
            lines += [
                "",
                "[bold]Boot cost[/]",
                _boot_cell(e.boot_ms),
            ]
        if is_critical(e):
            lines += [
                "",
                "[bold yellow]⚠ Session-critical[/]",
                "[dim]Disabling this may break the next login.[/]",
            ]
        if e.user_path:
            lines += ["", "[bold]User file[/]", f"[dim]{e.user_path}[/]"]
        if e.system_path:
            lines += ["", "[bold]System file[/]", f"[dim]{e.system_path}[/]"]
        if e.user_path and e.system_path:
            diff = render_override_diff(e)
            if diff:
                lines += ["", "[bold]Override diff[/]", diff]
        return "\n".join(lines)


    def _row_cells(self, e: Entry) -> tuple[str, str, str, str, str]:
        on_label = " ● ON " if e.kind == "autostart" else " ● SHOW"
        off_label = " ○ OFF" if e.kind == "autostart" else " ○ HIDE"
        state = (
            f"[bold green]{on_label}[/]" if e.enabled else f"[bold red]{off_label}[/]"
        )
        glyph = icon_to_glyph(e.icon_name)
        icon = (
            f"[bold {self._accent_color}]{glyph}[/]"
            if e.enabled
            else f"[dim]{glyph}[/]"
        )
        # Source colors: user = cyan (the user's own choice), system = dim
        # (background plumbing), user+system = magenta (user overrides system).
        source_color = {
            "user": "cyan",
            "system": "gray50",
            "user+system": "magenta",
        }.get(e.source, "white")
        source = f"[{source_color}]{e.source}[/]"
        if not e.enabled:
            source = f"[dim]{source}[/]"
        boot = _boot_cell(e.boot_ms)
        # Prepend a warning glyph to critical entries — visible reminder
        # before you press Space.
        prefix = "[bold yellow]⚠[/] " if is_critical(e) else ""
        name = (
            f"{prefix}{e.name}" if e.enabled else f"[dim]{prefix}{e.name}[/]"
        )
        return icon, state, source, boot, name


def main() -> None:
    AutostartApp().run()


if __name__ == "__main__":
    main()
