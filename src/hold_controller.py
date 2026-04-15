"""Hold/unhold controller for Bodhi Update Manager."""

from __future__ import annotations

import logging
import os
import random
import subprocess
import threading
from gettext import gettext as _

from gi.repository import GLib

from bodhi_update.backends import get_registry
from bodhi_update.install_commands import build_hold_argv
from bodhi_update.models import (
    CONSTRAINT_BLOCKED,
    CONSTRAINT_HELD,
    CONSTRAINT_NORMAL,
)
from bodhi_update.utils import format_size

log = logging.getLogger(__name__)


class HoldController:
    """Handle apt hold/unhold flow and APT row reload."""

    def __init__(self, window) -> None:
        self.window = window
        self._hold_sentinel_path: str | None = None
        self._hold_poll_source_id: int | None = None

    def poll_hold_sentinel(self, running_msg: str) -> bool:
        """GLib poller: switch status text once pkexec auth succeeds."""
        path = self._hold_sentinel_path
        if path is None:
            return False

        if os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass
            self._hold_sentinel_path = None
            self._hold_poll_source_id = None
            GLib.idle_add(self.window._set_status, running_msg)
            return False

        return True

    def stop_hold_poller(self) -> None:
        """Stop the hold sentinel poller without touching the file."""
        src = self._hold_poll_source_id
        if src is not None:
            GLib.source_remove(src)
            self._hold_poll_source_id = None

    def cancel_hold_sentinel(self) -> None:
        """Stop the poller and remove any leftover sentinel file."""
        self.stop_hold_poller()
        path = self._hold_sentinel_path
        if path:
            self._hold_sentinel_path = None
            try:
                os.unlink(path)
            except OSError:
                pass

    def reload_apt_rows(self) -> None:  # pylint: disable=too-many-locals
        """Re-query APT rows only, leaving non-APT rows intact."""
        non_apt = [
            list(row) for row in self.window.store
            if row[self.window.COL_BACKEND] != "apt"
        ]

        apt_updates = []
        apt_bytes = 0

        for backend in get_registry().get_all_backends():
            if backend.backend_id != "apt":
                continue
            try:
                items, total_bytes = backend.get_updates()
                apt_updates.extend(items)
                apt_bytes += total_bytes
            except (OSError, RuntimeError, ValueError):
                continue

        show_desc = self.window.prefs.get("show_descriptions", True)

        self.window.store.freeze_notify()
        try:
            self.window.store.clear()

            for row in non_apt:
                self.window.store.append(row)

            for update in apt_updates:
                constraint = update.constraint
                icon = self.window._category_icon(
                    update.category,
                    update.backend,
                    constraint,
                )
                pkg_markup = self.window._build_pkg_markup(
                    update.name,
                    update.description,
                    show_desc,
                    constraint,
                )
                size_str = format_size(update.size)
                self.window.store.append([
                    False,
                    pkg_markup,
                    update.installed_version,
                    update.candidate_version,
                    size_str,
                    update.origin,
                    update.name,
                    update.category,
                    update.backend,
                    icon,
                    update.size,
                    update.description or _("System package"),
                    constraint,
                ])
        finally:
            self.window.store.thaw_notify()

        non_apt_bytes = sum(
            row[self.window.COL_RAW_SIZE]
            for row in self.window.store
            if row[self.window.COL_BACKEND] != "apt"
        )
        actionable = sum(
            1
            for row in self.window.store
            if row[self.window.COL_HELD] == CONSTRAINT_NORMAL
        )
        self.window._update_count_status(
            actionable,
            apt_bytes + non_apt_bytes,
            cached=True,
        )

    def do_hold_toggle(self, pkg_name: str, hold: bool) -> None:
        """Run apt-mark hold/unhold via the privilege helper."""
        if self.window.refresh_in_progress or self.window.install_in_progress:
            return

        running_msg = _("Locking package...") if hold else _("Unlocking package...")

        sentinel = (
            f"/tmp/bodup-hold-{os.getpid()}-"
            f"{random.randint(0, 0xFFFFFF):06x}.ok"
        )
        self._hold_sentinel_path = sentinel
        self._hold_poll_source_id = GLib.timeout_add(
            100,
            self.poll_hold_sentinel,
            running_msg,
        )

        self.window._set_status(_("Waiting for authorization..."))

        def _worker() -> None:
            try:
                argv = build_hold_argv(
                    pkg_name,
                    hold=hold,
                    sentinel_path=sentinel,
                )
            except RuntimeError as exc:
                self.cancel_hold_sentinel()
                GLib.idle_add(self.window._set_status, str(exc))
                return

            result = subprocess.run(
                argv,
                capture_output=True,
                check=False,
            )

            sentinel_path = self._hold_sentinel_path
            if sentinel_path and os.path.exists(sentinel_path):
                try:
                    os.unlink(sentinel_path)
                except OSError:
                    pass
                self._hold_sentinel_path = None
                GLib.idle_add(self.window._set_status, running_msg)

            self.cancel_hold_sentinel()

            if result.returncode != 0:
                err_lines = (
                    (result.stderr or b"")
                    .decode(errors="replace")
                    .strip()
                    .splitlines()
                )
                msg = err_lines[0] if err_lines else _(
                    "apt-mark failed (unknown error)"
                )
                GLib.idle_add(self.window._set_status, msg)
                return

            if hold:
                status = _("Package '%(name)s' is now held.") % {"name": pkg_name}
            else:
                status = _(
                    "Package '%(name)s' is no longer held."
                ) % {"name": pkg_name}

            GLib.idle_add(self.reload_apt_rows)
            GLib.idle_add(self.window._set_status, status)
            GLib.timeout_add_seconds(
                3,
                self.window._restore_current_update_status,
            )

        threading.Thread(target=_worker, daemon=True).start()
