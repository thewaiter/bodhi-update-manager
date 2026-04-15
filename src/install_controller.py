"""Install/auth controller for Bodhi Update Manager."""

from __future__ import annotations

import logging
import os
import random
from gettext import gettext as _

from gi.repository import GLib, Vte

from bodhi_update.install_commands import build_deb_install_argv
from bodhi_update.utils import find_privilege_tool

log = logging.getLogger(__name__)


class InstallController:
    """Handle install/auth flow and VTE-driven progress UI."""

    def __init__(self, window) -> None:
        self.window = window
        self.install_state: str = "IDLE"
        self.install_output_started = False
        self.install_pulse_source_id: int | None = None
        self._auth_sentinel_path: str | None = None
        self._auth_poll_source_id: int | None = None
        self._active_privilege_tool: str | None = None

    def _pulse_install_progress(self) -> bool:
        if not self.window.install_in_progress or not self.install_output_started:
            self.install_pulse_source_id = None
            return False

        self.window.install_progress.pulse()
        return True

    def start_install_progress(self, title: str) -> None:
        """Prepare the install UI and enter AUTH_PENDING state."""
        self.install_state = "AUTH_PENDING"
        self.window._set_install_busy(True)
        self.install_output_started = False
        self._active_privilege_tool = None
        self._auth_sentinel_path = None

        if self._auth_poll_source_id is not None:
            GLib.source_remove(self._auth_poll_source_id)
            self._auth_poll_source_id = None

        self.window.stack.set_visible_child_name("install")
        self.window.install_title_label.set_markup(
            f"<b>{GLib.markup_escape_text(title)}</b>"
        )
        self.window.install_phase_label.set_text(_("Waiting for authentication..."))
        self.window.install_progress.set_fraction(0.0)
        self.window.install_progress.set_show_text(True)
        self.window.install_progress.set_text(_("Waiting for authentication..."))
        self.window._set_status(_("Waiting for authorization..."))

        self.window.install_details_revealer.set_reveal_child(False)
        self.window.show_details_button.set_active(False)
        self.window.show_details_button.set_label(_("Show Details"))

        if self.install_pulse_source_id is not None:
            GLib.source_remove(self.install_pulse_source_id)
            self.install_pulse_source_id = None

        try:
            self.window.install_terminal.reset(True, True)
        except (AttributeError, TypeError, RuntimeError):
            pass

    def mark_install_running(self) -> None:
        """Transition install UI from AUTH_PENDING to RUNNING."""
        if self.install_state != "AUTH_PENDING":
            return

        self.install_state = "RUNNING"
        self.install_output_started = True
        self.window.install_phase_label.set_text(
            _("This may take a few minutes.")
        )
        self.window.install_progress.set_text(_("Installing updates..."))
        self.window._set_status(_("Installing updates..."))

        self.window.install_details_revealer.set_reveal_child(True)
        self.window.show_details_button.set_active(True)
        self.window.show_details_button.set_label(_("Hide Details"))
        self.window.install_terminal.grab_focus()

        if self.install_pulse_source_id is None:
            self.install_pulse_source_id = GLib.timeout_add(
                150, self._pulse_install_progress
            )

    def on_spawn_complete(self, _terminal, pid, error, _user_data=None) -> None:
        """VTE spawn_async callback for hard spawn failures."""
        if error is not None:
            log.error("Spawn failed: %s", error.message)
            self.install_state = "FAILED"
            self.cancel_auth_sentinel()
            self.window._set_install_busy(False)
            self.window.install_progress.set_fraction(0.0)
            self.window.install_progress.set_text(_("Failed"))
            self.window.install_phase_label.set_text(
                _("Failed to start installation. See Details below.")
            )
            self.window.install_details_revealer.set_reveal_child(True)
            self.window.show_details_button.set_active(True)
            self.window.show_details_button.set_label(_("Hide Details"))
            self.window._set_status(_("Failed to start installation."))
            return

        log.info("Install process spawned (pid %s).", pid)

    def spawn_install_command(self, argv: list[str]) -> None:
        """Spawn argv directly in the VTE terminal."""
        envv = [f"{key}={value}" for key, value in os.environ.items()]

        self.window.install_terminal.spawn_async(
            Vte.PtyFlags.DEFAULT,
            os.getcwd(),
            argv,
            envv,
            GLib.SpawnFlags.DEFAULT,
            None,
            None,
            -1,
            None,
            self.on_spawn_complete,
            None,
        )

    def handle_terminal_auth_fallback(self) -> None:
        """Update the UI for sudo/doas auth inside the VTE."""
        msg = _(
            "Enter your password in the terminal below."
            " For security, nothing will appear while typing."
        )

        log.info("Terminal auth in use — revealing VTE for password entry.")

        self.window.install_details_revealer.set_reveal_child(True)
        self.window.show_details_button.set_active(True)
        self.window.show_details_button.set_label(_("Hide Details"))

        self.window.install_phase_label.set_text(msg)
        self.window.install_progress.set_text(_("Waiting for authentication..."))
        self.window._set_status(_("Waiting for authorization..."))
        self.window.install_terminal.grab_focus()

    def poll_auth_sentinel(self) -> bool:
        """GLib poller: transition to RUNNING when auth sentinel appears."""
        if self.install_state != "AUTH_PENDING":
            self._auth_poll_source_id = None
            return False

        path = self._auth_sentinel_path
        if path and os.path.exists(path):
            log.info("Auth sentinel found — transitioning to RUNNING.")
            try:
                os.unlink(path)
            except OSError:
                pass
            self._auth_sentinel_path = None
            self._auth_poll_source_id = None
            GLib.idle_add(self.mark_install_running)
            return False

        return True

    def cancel_auth_sentinel(self) -> None:
        """Stop the sentinel poller and clean up any leftover file."""
        if self._auth_poll_source_id is not None:
            GLib.source_remove(self._auth_poll_source_id)
            self._auth_poll_source_id = None

        path = self._auth_sentinel_path
        if path:
            self._auth_sentinel_path = None
            try:
                os.unlink(path)
            except OSError:
                pass

    def launch_install(self, argv: list[str], title: str) -> None:
        """Launch an install command with the correct auth flow."""
        log.info("Starting installation: %s", title)
        log.debug("Command: %s", argv)

        self.start_install_progress(title)
        self._active_privilege_tool = find_privilege_tool()

        if self._active_privilege_tool == "pkexec":
            sentinel = (
                f"/tmp/bodup-auth-{os.getpid()}-"
                f"{random.randint(0, 0xFFFFFF):06x}.ok"
            )
            self._auth_sentinel_path = sentinel
            guarded_argv = [argv[0], argv[1], "--sentinel", sentinel, *argv[2:]]
            self.spawn_install_command(guarded_argv)
            self._auth_poll_source_id = GLib.timeout_add(
                100, self.poll_auth_sentinel
            )
        else:
            self.spawn_install_command(argv)
            self.handle_terminal_auth_fallback()
            GLib.idle_add(self.mark_install_running)

    def launch_deb_install(self, deb_path: str, title: str) -> None:
        """Build argv for a local .deb and launch it."""
        argv = build_deb_install_argv(deb_path)
        self.launch_install(argv, title)

    def finish_install_success(self) -> None:
        """Update the UI for a successful install."""
        log.info("Installation completed successfully.")
        self.install_state = "COMPLETE"
        self.cancel_auth_sentinel()
        self.window._set_install_busy(False)
        self.window.install_progress.set_fraction(1.0)
        self.window.install_progress.set_text(_("Complete"))
        self.window.install_phase_label.set_text(
            _("Updates installed successfully.")
        )
        self.window._set_status(_("Ready"))

    def finish_install_failure(self, exit_code: int) -> None:
        """Update the UI for a failed install."""
        log.error("Installation failed with exit code: %s", exit_code)
        self.install_state = "FAILED"
        self.cancel_auth_sentinel()
        self.window._set_install_busy(False)
        self.window.install_progress.set_fraction(0.0)
        self.window.install_progress.set_text(_("Failed"))
        self.window.install_phase_label.set_text(
            _("Update failed. Exit code: %(exit_code)s. See Details below.")
            % {"exit_code": exit_code}
        )
        self.window.install_details_revealer.set_reveal_child(True)
        self.window.show_details_button.set_active(True)
        self.window.show_details_button.set_label(_("Hide Details"))
        self.window._set_status(
            _("Update failed. Exit code: %(exit_code)s")
            % {"exit_code": exit_code}
        )
