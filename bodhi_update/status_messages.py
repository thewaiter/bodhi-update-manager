"""Status/count message helpers for Bodhi Update Manager."""

from __future__ import annotations

from dataclasses import dataclass, field
from gettext import bindtextdomain, gettext as _, ngettext as N_, textdomain
from typing import Iterable
from bodhi_update.models import CONSTRAINT_BLOCKED, CONSTRAINT_HELD
from bodhi_update.utils import format_size, reboot_required

APP_NAME = "bodhi-update-manager"
bindtextdomain(APP_NAME, "/usr/share/locale")
textdomain(APP_NAME)


@dataclass
class CountStatusOptions:
    """Display-decoration options for format_update_count_status."""

    cached: bool = False
    has_unknown_size: bool = False
    extras: list[str] = field(default_factory=list)
    hidden_held: int = 0


def ready_status_text() -> str:
    """Return the idle-ready status string."""
    return _("Restart required.") if reboot_required() else _("Ready")


def with_restart_suffix(message: str) -> str:
    """Append restart-required notice when needed."""
    if reboot_required() and "Restart required" not in message:
        return _("%(message)s  Restart required.") % {"message": message}
    return message


def format_update_count_status(
    count: int,
    total_bytes: int,
    opts: CountStatusOptions | None = None,
) -> str:
    """Return the main update count status message."""
    if opts is None:
        opts = CountStatusOptions()

    if count == 0:
        return (
            _("System is up to date. No pending updates in cached package data.")
            if opts.cached
            else _("System is up to date.")
        )

    if opts.has_unknown_size:
        size_str = f"{format_size(total_bytes)}+" if total_bytes > 0 else _("Unknown")
    else:
        size_str = format_size(total_bytes)

    message = N_(
        "%(count)d update available · Download: %(size)s",
        "%(count)d updates available · Download: %(size)s",
        count,
    ) % {
        "count": count,
        "size": size_str,
    }

    if opts.cached:
        message = _(
            "%(message)s · Cached data — refresh to check for newer updates"
        ) % {"message": message}

    if opts.extras:
        message = _("%(message)s (includes %(extras)s)") % {
            "message": message,
            "extras": ", ".join(opts.extras),
        }

    if opts.hidden_held:
        hint = N_(
            "%(n)d held/blocked package hidden",
            "%(n)d held/blocked packages hidden",
            opts.hidden_held,
        ) % {"n": opts.hidden_held}
        message = f"{message} · {hint}"

    return message


def format_selected_count_status(
    selected_count: int,
    known_bytes: int,
    *,
    has_known: bool,
    has_unknown: bool,
) -> str:
    """Return the selected-package status message."""
    if selected_count == 0:
        return ""

    if has_known and has_unknown:
        dl_part = f"{format_size(known_bytes)}+"
    elif has_known:
        dl_part = format_size(known_bytes)
    else:
        dl_part = _("Unknown")

    return N_(
        "%(count)d update selected · Download: %(size)s",
        "%(count)d updates selected · Download: %(size)s",
        selected_count,
    ) % {
        "count": selected_count,
        "size": dl_part,
    }


def hidden_held_count(rows: Iterable, col_held: int) -> int:
    """Return number of held/blocked rows."""
    return sum(1 for row in rows
               if row[col_held] in (CONSTRAINT_HELD, CONSTRAINT_BLOCKED))
