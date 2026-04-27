"""Refresh controller for Bodhi Update Manager."""

from __future__ import annotations

import logging
import os
import random
import threading
from gettext import bindtextdomain, gettext as _, textdomain

from gi.repository import GLib

from bodhi_update.backends import get_registry
from bodhi_update.models import CONSTRAINT_NORMAL, UpdateItem

log = logging.getLogger(__name__)

APP_NAME = "bodhi-update-manager"
bindtextdomain(APP_NAME, "/usr/share/locale")
textdomain(APP_NAME)


class RefreshController:
    """Handle privileged refresh flow and cached update reload."""

    def __init__(self, window) -> None:
        self.window = window
        self._refresh_sentinel_path: str | None = None
        self._refresh_poll_source_id: int | None = None

    def poll_refresh_sentinel(self) -> bool:
        """Transition the UI to loading once refresh auth succeeds."""
        path = self._refresh_sentinel_path
        if path is None:
            return False

        if os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass
            self._refresh_sentinel_path = None
            self._refresh_poll_source_id = None
            GLib.idle_add(self.window.set_status, _("Loading updates..."))
            return False

        return True

    def stop_refresh_poller(self) -> None:
        """Stop the refresh sentinel poller."""
        src = self._refresh_poll_source_id
        if src is not None:
            GLib.source_remove(src)
            self._refresh_poll_source_id = None

    def cancel_refresh_sentinel(self) -> None:
        """Stop the poller and remove any leftover sentinel file."""
        self.stop_refresh_poller()
        path = self._refresh_sentinel_path
        if path:
            self._refresh_sentinel_path = None
            try:
                os.unlink(path)
            except OSError:
                pass

    def finish_refresh_ui(
        self,
        ok: bool,
        message: str,
        updates: list[UpdateItem],
        total_bytes: int,
    ) -> bool:
        """GTK-thread callback: update UI after refresh finishes."""
        log.info("Refresh finished. %d updates. Success: %s", len(updates), ok)

        self.window.set_refresh_busy(False)
        self.window.set_updates_loading(False)

        self.window.populate_store(updates)
        actionable = sum(1 for update in updates if getattr(
            update, "constraint", CONSTRAINT_NORMAL) == CONSTRAINT_NORMAL)
        self.window.update_count_status(actionable, total_bytes, cached=not ok)

        if not ok and message:
            current_status = self.window.get_status_text()
            self.window.set_status(
                _("%(current_status)s — Warning: %(message)s")
                % {
                    "current_status": current_status,
                    "message": message,
                }
            )

        return False

    def refresh_worker(self) -> None:
        """Background worker: refresh backends, then reload updates."""
        messages: list[str] = []
        backends = get_registry().get_all_backends()
        successful_backends = 0

        sentinel = self._refresh_sentinel_path

        for backend in backends:
            if backend.backend_id == "apt":
                ok, msg = backend.refresh(sentinel_path=sentinel)
            else:
                ok, msg = backend.refresh()

            if not ok and msg:
                messages.append(msg)

        self.stop_refresh_poller()

        if sentinel and os.path.exists(sentinel):
            try:
                os.unlink(sentinel)
            except OSError:
                pass
            GLib.idle_add(self.window.set_status, _("Loading updates..."))

        self._refresh_sentinel_path = None

        updates: list[UpdateItem] = []
        total_bytes = 0

        for backend in backends:
            try:
                backend_updates, backend_bytes = backend.get_updates()
                updates.extend(backend_updates)
                total_bytes += backend_bytes
                successful_backends += 1
            except (OSError, RuntimeError, ValueError) as exc:
                log.error(
                    "Backend %s get_updates failed: %s",
                    backend.display_name,
                    exc,
                )
                messages.append(
                    _("%(name)s get_updates failed. (%(exc)s)")
                    % {
                        "name": backend.display_name,
                        "exc": exc,
                    }
                )

        fatal_fail = successful_backends == 0 and len(backends) > 0
        final_msg = _("Package lists refreshed.")
        if messages:
            final_msg = " · ".join(messages)

        log.info("Finished querying backends. Total updates: %d", len(updates))

        GLib.idle_add(
            self.finish_refresh_ui,
            not fatal_fail,
            final_msg,
            updates,
            total_bytes,
        )

    def start_refresh(self) -> None:
        """Start the privileged refresh flow."""
        self.window.set_refresh_busy(True)
        self.window.set_updates_loading(True)
        self.window.set_status(_("Waiting for authorization..."))

        self._refresh_sentinel_path = (f"/tmp/bodup-refresh-{os.getpid()}-"
                                       f"{random.randint(0, 0xFFFFFF):06x}.ok")
        self._refresh_poll_source_id = GLib.timeout_add(
            100,
            self.poll_refresh_sentinel,
        )

        log.info("Starting background refresh for updates.")
        worker = threading.Thread(target=self.refresh_worker, daemon=True)
        worker.start()
