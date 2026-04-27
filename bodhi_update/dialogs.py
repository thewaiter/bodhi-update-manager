""" Dialogs used by the class UpdateManagerApplication. """

# flake8: noqa: E402
from __future__ import annotations

from dataclasses import dataclass
from gettext import bindtextdomain, gettext as _, textdomain
from typing import Dict, List, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

APP_NAME = "bodhi-update-manager"
bindtextdomain(APP_NAME, "/usr/share/locale")
textdomain(APP_NAME)

from bodhi_update._version import __version__
ABOUT_TEXT = _(
    """Update Manager

A lightweight graphical update manager for Debian based distros."""
)

GPL_SHORT = _(
    """This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>."""
)


class AboutDialog(Gtk.Dialog):
    """About dialog for Bodhi Update Manager"""

    PAGES = {
        "update": ABOUT_TEXT,
        "website": _(
            """Website

https://github.com/flux-abyss/bodhi-update-manager"""
        ),
        "credits": _(
            """Credits

Lead Developer:
    Joseph “flux.abyss” Wiley

Contributors:
    Robert “ylee” Wiley
    Diego “diekrz2” K."""
        ),
        "license": _(
            """Copyright © 2026 Joseph “flux.abyss” Wiley

"""
        ) + GPL_SHORT,
    }

    BUTTONS = [
        ("update", _("Update Manager")),
        ("website", _("Website")),
        ("credits", _("Credits")),
        ("license", _("License")),
    ]

    def __init__(self, parent) -> None:
        super().__init__(
            title=_("About"),
            transient_for=parent,
            modal=True,
            destroy_with_parent=True
        )
        self.set_border_width(10)
        self.set_default_size(600, 400)
        self.add_button(Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE)

        self._build_ui()
        self._set_text(self.PAGES["update"])

    def _build_ui(self) -> None:
        content = self.get_content_area()

        outer_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        content.pack_start(outer_box, True, True, 0)

        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left_box.set_size_request(160, -1)
        outer_box.pack_start(left_box, False, False, 0)

        icon = Gtk.Image.new_from_icon_name("bodhi-update-manager",
                                            Gtk.IconSize.DIALOG)
        icon.set_pixel_size(200)
        left_box.pack_start(icon, False, False, 0)

        version_label = Gtk.Label()
        version_label.set_markup(f"<b>{_('Version:')}</b> {__version__}")
        version_label.set_justify(Gtk.Justification.CENTER)
        left_box.pack_start(version_label, False, False, 0)

        spacer = Gtk.Box()
        spacer.set_size_request(-1, 10)
        left_box.pack_start(spacer, False, False, 0)

        for key, label in self.BUTTONS:
            btn = Gtk.Button(label=label)
            btn.set_hexpand(False)
            btn.connect("clicked", self._on_about_button_clicked, key)
            left_box.pack_start(btn, False, False, 0)

        left_box.pack_start(Gtk.Box(), True, True, 0)

        right_frame = Gtk.Frame()
        outer_box.pack_start(right_frame, True, True, 0)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        right_frame.add(scrolled)

        self.textview = Gtk.TextView()
        self.textview.set_editable(False)
        self.textview.set_cursor_visible(False)
        self.textview.set_monospace(False)
        self.textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.textview.set_left_margin(10)
        self.textview.set_right_margin(10)
        self.textview.set_top_margin(10)
        self.textview.set_bottom_margin(10)
        scrolled.add(self.textview)

    def _set_text(self, text: str) -> None:
        buffer_ = self.textview.get_buffer()
        buffer_.set_text(text)

    def _on_about_button_clicked(self, _button, key: str) -> None:
        self._set_text(self.PAGES[key])


@dataclass
class PreferencesLabels:
    """All translatable label strings for PreferencesDialog."""

    title: str
    notifications_label: str
    held_label: str
    cancel_label: str
    apply_label: str


@dataclass
class PreferencesState:
    """Current pref values used to initialise PreferencesDialog widgets."""

    show_notifications: bool
    show_held_packages: bool
    backend_states: List[Tuple[str, str, bool]]


class PreferencesDialog(Gtk.Dialog):
    """Preferences dialog for Bodhi Update Manager."""

    def __init__(
        self,
        parent: Gtk.Window,
        labels: PreferencesLabels,
        state: PreferencesState,
    ) -> None:
        """
        state.backend_states: list of (backend_id, display_label, is_enabled) tuples.
        """
        super().__init__(
            title=labels.title,
            transient_for=parent,
            modal=True,
        )

        self.add_button(labels.cancel_label, Gtk.ResponseType.CANCEL)
        self.add_button(labels.apply_label, Gtk.ResponseType.APPLY)

        self._backend_checks: Dict[str, Gtk.CheckButton] = {}

        content = self.get_content_area()
        content.set_spacing(8)
        content.set_border_width(8)

        # --- General options ---

        self.notif_check = Gtk.CheckButton(label=labels.notifications_label)
        self.notif_check.set_active(state.show_notifications)
        content.pack_start(self.notif_check, False, False, 0)

        self.held_check = Gtk.CheckButton(label=labels.held_label)
        self.held_check.set_active(state.show_held_packages)
        content.pack_start(self.held_check, False, False, 0)

        # --- Backend section (only if any backends exist) ---

        if state.backend_states:
            sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            content.pack_start(sep, False, False, 6)

            backend_label = Gtk.Label(label=_("Backends"))
            backend_label.set_xalign(0)
            backend_label.get_style_context().add_class("heading")
            content.pack_start(backend_label, False, False, 0)

            for backend_id, label, enabled in state.backend_states:
                check = Gtk.CheckButton(label=label)
                check.set_active(enabled)
                content.pack_start(check, False, False, 0)
                self._backend_checks[backend_id] = check

        self.show_all()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_values(self) -> dict:
        """Return dialog values as a plain dict."""
        return {
            "show_notifications": self.notif_check.get_active(),
            "show_held_packages": self.held_check.get_active(),
            "backend_visibility": {
                backend_id: check.get_active()
                for backend_id, check in self._backend_checks.items()
            },
        }
