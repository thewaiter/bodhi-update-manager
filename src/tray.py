"""Integrated tray indicator for the Update Manager.

Owns one tray icon per application instance. Actions lazily create the
UpdateManagerWindow via get_or_create_window() — the window is not
pre-created in tray mode, so GTK can't implicitly show it at startup.

Indicator backend priority:
  1. Gtk.StatusIcon         — preferred on Bodhi/Moksha; badge fully supported
  2. AyatanaAppIndicator3   — fallback on desktops with AppIndicator support
  3. AppIndicator3          — classic libappindicator fallback
"""

from __future__ import annotations

import json
import os
import threading
from typing import TYPE_CHECKING

import gi

gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gtk", "3.0")

# ---------------------------------------------------------------------------
# AppIndicator backend detection (must come after gi.require_version calls)
# ---------------------------------------------------------------------------

_APP_INDICATOR = None  # type: ignore[assignment]  # pylint: disable=invalid-name
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as _APP_INDICATOR  # type: ignore[assignment]
except (ValueError, ImportError):
    pass

if _APP_INDICATOR is None:
    try:
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3 as _APP_INDICATOR  # type: ignore[assignment]
    except (ValueError, ImportError):
        pass

from gi.repository import GdkPixbuf, GLib, Gtk  # noqa: E402

if TYPE_CHECKING:
    from bodhi_update.app import UpdateManagerApplication

# ---------------------------------------------------------------------------
# Badge-dot helper
# ---------------------------------------------------------------------------

# Severity → (fill RGBA, outline RGBA)
_SEVERITY_COLORS = {
    "high": (
        (220, 60, 60, 255), (120, 220, 120, 255)),  # red fill   / green ring
    "medium": (
        (246, 195, 66, 255), (120, 220, 120, 255)),  # amber fill / green ring
    "low": (
        (80, 210, 230, 255), (120, 220, 120, 255)),  # cyan fill  / green ring
}

# Keep this list small: core platform plumbing only.
_MEDIUM_PREFIXES = (
    "linux-",
    "systemd",
    "libc",
    "glibc",
    "dbus",
    "openssl",
    "gnupg",
    "apt",
    "dpkg",
    "bash",
    "coreutils",
    "util-linux",
    "sudo",
    "moksha",
    "bodhi-",
)


def _pkg_severity(name: str, category: str, backend: str) -> str:
    """Return 'high', 'medium', or 'low' for a single update item."""
    if category in ("security", "kernel"):
        return "high"
    if backend == "apt" and name.startswith(_MEDIUM_PREFIXES):
        return "medium"
    return "low"


def _read_pref(key: str, default: bool = True) -> bool:
    """Read a single boolean preference from the shared prefs file."""
    config_home = os.environ.get("XDG_CONFIG_HOME",
                                 os.path.expanduser("~/.config"))
    path = os.path.join(config_home, "bodhi-update-manager", "prefs.json")

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return bool(data.get(key, default))
    except (OSError, json.JSONDecodeError, AttributeError):
        # File missing, bad JSON, or non-dict data.
        pass

    return default


def _write_pixel(pixels: bytearray, p: int, color: tuple,
                 n_channels: int) -> None:
    """Write a single RGBA (or RGB) pixel at byte offset *p*."""
    pixels[p] = color[0]
    pixels[p + 1] = color[1]
    pixels[p + 2] = color[2]
    if n_channels == 4:
        pixels[p + 3] = color[3]


def _badge_dot_geometry(width: int) -> tuple:
    """Return (radius, cx, cy, r2_outer, r2_inner) for the badge dot."""
    radius = max(2, width // 12)
    cx = width - radius - 1
    cy = radius + 1
    r2_outer = (radius + 1) * (radius + 1)
    r2_inner = radius * radius
    return radius, cx, cy, r2_outer, r2_inner


def _add_badge_dot(  # pylint: disable=too-many-locals
        pixbuf: GdkPixbuf.Pixbuf,
        severity: str = "medium") -> GdkPixbuf.Pixbuf:
    """Draw a small status dot in the top-right corner. Color reflects severity."""
    fill, outline = _SEVERITY_COLORS.get(severity, _SEVERITY_COLORS["medium"])

    width = pixbuf.get_width()
    height = pixbuf.get_height()
    pixels = bytearray(pixbuf.get_pixels())
    rowstride = pixbuf.get_rowstride()
    n_channels = pixbuf.get_n_channels()

    _radius, cx, cy, r2_outer, r2_inner = _badge_dot_geometry(width)

    for y in range(height):
        for x in range(width):
            dx = x - cx
            dy = y - cy
            dist2 = dx * dx + dy * dy
            if dist2 > r2_outer:
                continue
            p = y * rowstride + x * n_channels
            color = outline if dist2 > r2_inner else fill
            _write_pixel(pixels, p, color, n_channels)

    return GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(bytes(pixels)),
        pixbuf.get_colorspace(),
        pixbuf.get_has_alpha(),
        pixbuf.get_bits_per_sample(),
        width,
        height,
        rowstride,
    )


# ---------------------------------------------------------------------------
# Tray implementation
# ---------------------------------------------------------------------------


class TrayIcon:
    """System-tray icon that operates on the application window.

    Receives the application object (not the window) so the window can be
    created lazily on first use.  Call :meth:`destroy` when the app exits.
    """

    _ICON_NAME = "bodhi-update-manager"
    _ICON_SIZE = 22  # px — standard system-tray icon size

    # Background poll interval (seconds).
    _POLL_INTERVAL = 15 * 60  # 15 minutes
    _INITIAL_DELAY = 5  # seconds after startup before first check

    def __init__(self, app: "UpdateManagerApplication") -> None:
        """Initialise the tray icon and schedule the first background poll."""
        self._app = app
        self._status_icon = None  # Gtk.StatusIcon handle (preferred)
        self._indicator = None  # AppIndicator3 handle (fallback)
        self._poll_source_id: int | None = None
        self._last_count: int = 0  # most recent badge count from set_update_count()

        menu = self._build_menu()

        # Prefer Gtk.StatusIcon (badge supported); fall back to AppIndicator.
        try:
            icon = Gtk.StatusIcon()
            icon.set_from_icon_name(self._ICON_NAME)
            icon.set_tooltip_text("Update Manager")
            icon.set_visible(True)
            icon.connect("activate", lambda _: self._toggle_window())
            icon.connect("popup-menu", self._on_status_icon_popup)
            self._status_icon = icon
            self._menu = menu  # keep menu alive as long as the icon is alive
        except (AttributeError, TypeError, RuntimeError):
            # StatusIcon unavailable; fall back to AppIndicator.
            if _APP_INDICATOR is not None:
                self._indicator = _APP_INDICATOR.Indicator.new(
                    self._ICON_NAME,
                    self._ICON_NAME,
                    _APP_INDICATOR.IndicatorCategory.APPLICATION_STATUS,
                )
                self._indicator.set_status(
                    _APP_INDICATOR.IndicatorStatus.ACTIVE)
                self._indicator.set_menu(menu)

        GLib.timeout_add_seconds(self._INITIAL_DELAY, self._on_poll_timer)

    # ------------------------------------------------------------------
    # Menu construction
    # ------------------------------------------------------------------

    def _build_menu(self) -> Gtk.Menu:
        """Build and return the right-click tray context menu."""
        menu = Gtk.Menu()

        show_item = Gtk.MenuItem(label="Show / Hide")
        show_item.connect("activate", lambda _: self._toggle_window())
        menu.append(show_item)

        refresh_item = Gtk.MenuItem(label="Check for Updates")
        refresh_item.connect("activate", lambda _: self._check_updates())
        menu.append(refresh_item)

        menu.append(Gtk.SeparatorMenuItem())

        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda _: self._quit())
        menu.append(quit_item)

        menu.show_all()
        return menu

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _show_window(self) -> None:
        """Lazily create the window (if needed) and make it visible."""
        win = self._app.get_or_create_window()
        win.show_all()
        win.present()

    def _toggle_window(self) -> None:
        """Toggle window visibility.

        When opening the window while the badge indicates pending updates,
        schedule a background refresh so the visible data matches the tray
        state.  The window is always shown immediately using cached data.
        """
        win = self._app.get_or_create_window()
        if win.get_visible():
            win.hide()
        else:
            win.show_all()
            win.present()
            if self._last_count > 0:
                GLib.idle_add(self._maybe_trigger_refresh, win)

    def _maybe_trigger_refresh(self, win: object) -> bool:
        """Idle callback: start a background refresh if one isn't already running."""
        if not getattr(win, "refresh_in_progress", False) and \
                not getattr(win, "install_in_progress", False):
            win.on_check_updates(None)  # type: ignore[union-attr]
        return False

    def _check_updates(self) -> None:
        """Show the window and trigger an update check."""
        win = self._app.get_or_create_window()
        if not win.get_visible():
            win.show_all()
            win.present()
        win.on_check_updates(None)

    def _quit(self) -> None:
        """Quit the application, releasing the hold() if in tray-only mode."""
        self._app.quit_from_tray()

    # ------------------------------------------------------------------
    # StatusIcon popup helper
    # ------------------------------------------------------------------

    def _on_status_icon_popup(self, status_icon: Gtk.StatusIcon, button: int,
                              time: int) -> None:
        """Show context menu at the StatusIcon position."""
        self._menu.popup(
            None,
            None,
            Gtk.StatusIcon.position_menu,
            status_icon,
            button,
            time,
        )

    # ------------------------------------------------------------------
    # Background update-count polling
    # ------------------------------------------------------------------

    def _on_poll_timer(self) -> bool:
        """GLib timer callback: start a daemon thread to query cached updates."""
        if _read_pref("show_notifications"):
            threading.Thread(target=self._poll_worker, daemon=True).start()
        # Re-arm: one-shot source reschedules itself after each poll.
        self._poll_source_id = GLib.timeout_add_seconds(self._POLL_INTERVAL,
                                                        self._on_poll_timer)
        return False  # remove the current one-shot source

    def _poll_worker(self) -> None:
        """Read cached update state from all backends (no refresh/privilege tool).

        Runs on a daemon thread; posts badge update back to the main loop via GLib.idle_add.
        """
        try:
            from bodhi_update.backends import get_registry, initialize_registry  # noqa: PLC0415
            from bodhi_update.models import CONSTRAINT_HELD, CONSTRAINT_BLOCKED  # noqa: PLC0415
            initialize_registry()  # idempotent
            count = 0
            severity = "low"
            for backend in get_registry().get_all_backends():
                try:
                    updates, _ = backend.get_updates()
                    for u in updates:
                        if getattr(u, "constraint", None) in (CONSTRAINT_HELD, CONSTRAINT_BLOCKED):
                            continue
                        count += 1
                        s = _pkg_severity(
                            getattr(u, "name", "") or "",
                            getattr(u, "category", "") or "",
                            getattr(u, "backend", "") or "",
                        )
                        if s == "high":
                            severity = "high"
                        elif s == "medium" and severity != "high":
                            severity = "medium"
                except (OSError, RuntimeError, ValueError, AttributeError):
                    # Skip failed backends and keep going.
                    continue
            GLib.idle_add(self.set_update_count, count, severity)
        except (ImportError, OSError, RuntimeError, AttributeError):
            # Don't let a background check take down the tray.
            pass

    # ------------------------------------------------------------------
    # Badge update
    # ------------------------------------------------------------------

    def set_update_count(self, count: int, severity: str = "medium") -> None:
        """Update or clear the badge dot. AppIndicator path degrades gracefully (no pixbuf)."""
        self._last_count = count  # persist so _toggle_window can read it
        if self._status_icon is None and self._indicator is None:
            return

        if count == 0:
            tooltip = "Update Manager"
        elif severity == "high":
            tooltip = "Update Manager - Security updates available"
        elif severity == "medium":
            tooltip = "Update Manager - Important updates available"
        else:
            tooltip = "Update Manager - Updates available"

        try:
            if self._status_icon is not None:
                theme = Gtk.IconTheme.get_default()
                pixbuf = theme.load_icon(self._ICON_NAME, self._ICON_SIZE, 0)
                if count > 0 and _read_pref("show_notifications"):
                    pixbuf = _add_badge_dot(pixbuf, severity)
                self._status_icon.set_from_pixbuf(pixbuf)
                self._status_icon.set_tooltip_text(tooltip)

            # AppIndicator doesn't support pixbuf badges; just keep the icon name current.
            if self._indicator is not None:
                self._indicator.set_icon_full(self._ICON_NAME, tooltip)
        except (AttributeError, TypeError, GLib.Error):
            pass

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def destroy(self) -> None:
        """Remove the tray icon and stop background polling."""
        if self._poll_source_id is not None:
            GLib.source_remove(self._poll_source_id)
            self._poll_source_id = None
        if self._status_icon is not None:
            self._status_icon.set_visible(False)
            self._status_icon = None
        if self._indicator is not None:
            self._indicator.set_status(_APP_INDICATOR.IndicatorStatus.PASSIVE)
            self._indicator = None
