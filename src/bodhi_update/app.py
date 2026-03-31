"""GTK3 GUI for the Update Manager with embedded VTE install view."""

from __future__ import annotations

import json
import logging
import os
import random
import subprocess
import sys
import threading
from typing import Dict, List

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("bodhi-update-manager")

# gi.require_version() must be called before any gi.repository imports.
import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gio, GLib, Gtk, Pango, Vte  # noqa: E402

from bodhi_update._version import __version__  # noqa: E402
from bodhi_update.backends import get_registry, initialize_registry  # noqa: E402
from bodhi_update.install_commands import build_deb_install_argv  # noqa: E402
from bodhi_update.models import UpdateItem  # noqa: E402
from bodhi_update.utils import (  # noqa: E402
    find_privilege_tool,
    format_size,
    reboot_required,
)

# Localization with gettext

import gettext

APP_NAME = "bodhi-update-manager"
LOCALE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "locale"
)

gettext.bindtextdomain(APP_NAME, "/usr/share/locale")
gettext.textdomain(APP_NAME)
_ = gettext.gettext
ngettext = gettext.ngettext

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


class UpdateManagerWindow(Gtk.Window):
    COL_SELECTED = 0
    COL_PACKAGE = 1
    COL_INSTALLED = 2
    COL_NEW = 3
    COL_SIZE = 4
    COL_REPO = 5
    COL_RAW_NAME = 6
    COL_CATEGORY = 7
    COL_BACKEND = 8
    COL_ICON = 9       # GTK icon-name (symbolic)
    COL_RAW_SIZE = 10  # Raw byte count for exact size summation
    COL_DESC = 11      # Raw description text (for reliable toggle of pkg markup)

    def __init__(self, deb_path: str | None = None) -> None:
        super().__init__(title=_("Update Manager"))
        self.set_default_size(1100, 700)
        self.set_icon_name("bodhi-update-manager")
        self.set_position(Gtk.WindowPosition.CENTER)

        self.refresh_in_progress = False
        self.install_in_progress = False
        self.install_output_started = False
        self.install_pulse_source_id: int | None = None
        # Sentinel file + poller for pkexec auth handshake.
        self._auth_sentinel_path: str | None = None
        self._auth_poll_source_id: int | None = None
        # Explicit install/auth state machine.
        # Valid transitions: IDLE → AUTH_PENDING → RUNNING → COMPLETE | FAILED
        self.install_state: str = "IDLE"

        self.prefs = self._load_prefs()
        # Guard flag used by _set_show_descriptions() to suppress menu re-entry.
        self._syncing_desc = False

        # Show a minimal window immediately so the desktop feels responsive,
        # then let the event loop schedule the full heavy build.
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(self.main_box)
        self.show_all()

        GLib.idle_add(self._build_full_ui, deb_path)

    def _build_full_ui(self, deb_path: str | None) -> bool:
        """Heavy UI + registry initialisation, deferred via GLib.idle_add.

        Builds every widget, wires signals, then shows the completed window.
        Must return False so GLib does not re-schedule it.
        """
        initialize_registry()

        self.store = Gtk.ListStore(bool, str, str, str, str, str, str, str, str, str, int, str)
        self.filter_model = self.store.filter_new()
        self.filter_model.set_visible_func(self._category_filter_func)

        self.outer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.outer_box.set_border_width(8)

        self._build_menubar()
        self.main_box.pack_start(self.outer_box, True, True, 0)

        self._build_toolbar()
        self._build_reboot_bar()
        self._build_stack()
        self._build_status()

        if deb_path is not None:
            # .deb mode: skip the update list and go straight to the install screen.
            self.show_all()
            self.install_details_revealer.set_reveal_child(False)
            self.reboot_info_bar.hide()
            self._launch_deb_install(deb_path)
        else:
            self.show_all()
            self.install_details_revealer.set_reveal_child(False)
            self.reboot_info_bar.hide()
            GLib.idle_add(self._load_cached_updates_on_startup)

        return False

    # ------------------------------------------------------------------ #
    # Widget construction                                                  #
    # ------------------------------------------------------------------ #

    def _build_menubar(self) -> None:
        menubar = Gtk.MenuBar()

        # File Menu
        file_menu = Gtk.Menu()
        file_item = Gtk.MenuItem(label=_("File"))
        file_item.set_submenu(file_menu)

        self.refresh_menu_item = Gtk.MenuItem(label=_("Refresh"))
        self.refresh_menu_item.connect("activate", lambda _: self.on_check_updates(None))
        file_menu.append(self.refresh_menu_item)

        self.install_sel_menu_item = Gtk.MenuItem(label=_("Install Selected"))
        self.install_sel_menu_item.connect("activate", lambda _: self.on_install_selected(None))
        file_menu.append(self.install_sel_menu_item)

        file_menu.append(Gtk.SeparatorMenuItem())

        self.select_all_menu_item = Gtk.MenuItem(label=_("Select All"))
        self.select_all_menu_item.connect("activate", lambda _: self.on_select_all(None))
        file_menu.append(self.select_all_menu_item)

        self.clear_menu_item = Gtk.MenuItem(label=_("Clear"))
        self.clear_menu_item.connect("activate", lambda _: self.on_clear_selection(None))
        file_menu.append(self.clear_menu_item)

        file_menu.append(Gtk.SeparatorMenuItem())

        quit_item = Gtk.MenuItem(label=_("Quit"))
        quit_item.connect("activate", lambda _: self.get_application().quit())
        file_menu.append(quit_item)

        menubar.append(file_item)

        # Edit Menu
        edit_menu = Gtk.Menu()
        edit_item = Gtk.MenuItem(label=_("Edit"))
        edit_item.set_submenu(edit_menu)

        pref_item = Gtk.MenuItem(label=_("Preferences"))
        pref_item.connect("activate", lambda _: self._show_preferences_dialog())
        edit_menu.append(pref_item)

        menubar.append(edit_item)

        # View Menu
        view_menu = Gtk.Menu()
        view_item = Gtk.MenuItem(label=_("View"))
        view_item.set_submenu(view_menu)

        self.show_desc_menu_item = Gtk.CheckMenuItem(label=_("Show Descriptions"))
        self.show_desc_menu_item.set_active(self.prefs.get("show_descriptions", True))
        self.show_desc_menu_item.connect("toggled", self.on_toggle_descriptions)
        view_menu.append(self.show_desc_menu_item)

        menubar.append(view_item)

        # Help Menu
        help_menu = Gtk.Menu()
        help_item = Gtk.MenuItem(label=_("Help"))
        help_item.set_submenu(help_menu)

        about_item = Gtk.MenuItem(label=_("About"))
        about_item.connect("activate", lambda _: self._show_about_dialog())
        help_menu.append(about_item)

        menubar.append(help_item)
        self.main_box.pack_start(menubar, False, False, 0)

    def _build_toolbar(self) -> None:
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self.clear_button = Gtk.Button(label=_("Clear"))
        self.clear_button.connect("clicked", self.on_clear_selection)
        toolbar.pack_start(self.clear_button, False, False, 0)

        self.select_all_button = Gtk.Button(label=_("Select All"))
        self.select_all_button.connect("clicked", self.on_select_all)
        toolbar.pack_start(self.select_all_button, False, False, 0)

        self.check_button = Gtk.Button(label=_("Refresh"))
        self.check_button.connect("clicked", self.on_check_updates)
        toolbar.pack_start(self.check_button, False, False, 0)

        self.install_selected_button = Gtk.Button(label=_("Install Selected"))
        self.install_selected_button.connect("clicked", self.on_install_selected)
        toolbar.pack_start(self.install_selected_button, False, False, 0)

        spacer = Gtk.Box()
        toolbar.pack_start(spacer, True, True, 0)

        self.category_combo = Gtk.ComboBoxText()
        self.category_combo.append("all", _("All"))
        self.category_combo.append("security", _("Security"))
        self.category_combo.append("kernel", _("Kernel"))
        self.category_combo.append("system", _("System"))
        # Optional backends: only add a filter entry when discovered.
        _registered_ids = {b.backend_id for b in get_registry().get_all_backends()}
        if "snap" in _registered_ids:
            self.category_combo.append("snap", "Snap")
        if "flatpak" in _registered_ids:
            self.category_combo.append("flatpak", "Flatpak")
        self.category_combo.set_active_id("all")
        self.category_combo.connect("changed", self.on_category_changed)
        toolbar.pack_start(self.category_combo, False, False, 0)

        self.outer_box.pack_start(toolbar, False, False, 0)

    def _build_reboot_bar(self) -> None:
        """Build the reboot-required InfoBar.  Hidden until a restart is needed."""
        self.reboot_info_bar = Gtk.InfoBar()
        self.reboot_info_bar.set_message_type(Gtk.MessageType.WARNING)
        self.reboot_info_bar.set_show_close_button(False)
        # set_no_show_all prevents show_all() from revealing this widget.
        self.reboot_info_bar.set_no_show_all(True)

        label = Gtk.Label(label=_("A system restart is required to complete the update."))
        label.show()
        self.reboot_info_bar.get_content_area().add(label)

        self.reboot_info_bar.add_button(_("Restart Now"), Gtk.ResponseType.ACCEPT)
        self.reboot_info_bar.connect("response", self._on_reboot_bar_response)

        self.outer_box.pack_start(self.reboot_info_bar, False, False, 0)

    def _build_stack(self) -> None:
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.stack.set_transition_duration(200)
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)

        self._build_updates_page()
        self._build_install_page()

        self.stack.add_named(self.updates_page, "updates")
        self.stack.add_named(self.install_page, "install")
        self.stack.set_visible_child_name("updates")

        self.outer_box.pack_start(self.stack, True, True, 0)

    def _build_updates_page(self) -> None:
        self.updates_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.updates_page.set_hexpand(True)
        self.updates_page.set_vexpand(True)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)

        self.tree = Gtk.TreeView(model=self.filter_model)
        self.tree.set_headers_visible(True)
        self.tree.set_vexpand(True)
        self.tree.set_hexpand(True)
        self.tree.set_enable_search(True)
        # Both flags together: fixed height allows a faster rendering path;
        # all columns MUST use FIXED sizing for this mode to work correctly.
        self.tree.set_fixed_height_mode(True)
        self.tree.set_hover_selection(False)

        # Type icon column (leftmost) — symbolic GTK icons
        icon_renderer = Gtk.CellRendererPixbuf()
        icon_renderer.set_property("xalign", 0.5)

        icon_column = Gtk.TreeViewColumn(_("Type"), icon_renderer)
        icon_column.add_attribute(icon_renderer, "icon-name", self.COL_ICON)

        icon_column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        icon_column.set_fixed_width(70)
        icon_column.set_resizable(False)

        self.tree.append_column(icon_column)

        # Checkbox "Upgrade" column.
        toggle_renderer = Gtk.CellRendererToggle()
        toggle_renderer.set_property("activatable", True)
        toggle_renderer.connect("toggled", self.on_toggle_selected)
        toggle_column = Gtk.TreeViewColumn(_("Upgrade"), toggle_renderer, active=self.COL_SELECTED)
        toggle_column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        toggle_column.set_fixed_width(90)
        self.tree.append_column(toggle_column)

        # Package column — always uses Pango markup so the name stays bold.
        # The markup string stored in COL_PACKAGE is regenerated when the
        # Show Descriptions preference changes (see on_toggle_descriptions).
        self.pkg_renderer = Gtk.CellRendererText()
        self.pkg_renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
        self.pkg_renderer.set_property("ellipsize-set", True)
        self.pkg_column = Gtk.TreeViewColumn(_("Package"), self.pkg_renderer, markup=self.COL_PACKAGE)
        self.pkg_column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        self.pkg_column.set_resizable(True)
        self.pkg_column.set_expand(True)
        self.pkg_column.set_min_width(220)
        self.pkg_column.set_alignment(0.0)
        self.tree.append_column(self.pkg_column)

        self.tree.append_column(
            self._make_text_column(
                _("Installed"), self.COL_INSTALLED, expand=False, min_width=150
            )
        )
        self.tree.append_column(
            self._make_text_column(
                _("New"), self.COL_NEW, expand=False, min_width=150
            )
        )
        self.tree.append_column(
            self._make_text_column(
                _("Size"), self.COL_SIZE, expand=False, min_width=100
            )
        )
        self.tree.append_column(
            self._make_text_column(
                _("Repository"), self.COL_REPO, expand=True, min_width=180
            )
        )

        scroller.add(self.tree)
        self.updates_page.pack_start(scroller, True, True, 0)

    def _build_install_page(self) -> None:
        self.install_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.install_page.set_hexpand(True)
        self.install_page.set_vexpand(True)

        self.install_title_label = Gtk.Label()
        self.install_title_label.set_xalign(0.0)
        self.install_title_label.set_markup("<b>%s</b>" % _("Installing updates..."))
        self.install_page.pack_start(self.install_title_label, False, False, 0)

        self.install_phase_label = Gtk.Label()
        self.install_phase_label.set_xalign(0.0)
        self.install_phase_label.set_text(_("Waiting for authentication..."))
        self.install_page.pack_start(self.install_phase_label, False, False, 0)

        self.install_progress = Gtk.ProgressBar()
        self.install_progress.set_hexpand(True)
        self.install_progress.set_show_text(True)
        self.install_progress.set_fraction(0.0)
        self.install_progress.set_text(_("Waiting for authentication..."))
        self.install_page.pack_start(self.install_progress, False, False, 0)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.install_page.pack_start(controls, False, False, 0)

        self.show_details_button = Gtk.ToggleButton(label=_("Show Details"))
        self.show_details_button.connect("toggled", self.on_toggle_details)
        controls.pack_start(self.show_details_button, False, False, 0)

        self.back_to_updates_button = Gtk.Button(label=_("Back to Updates"))
        self.back_to_updates_button.set_sensitive(False)
        self.back_to_updates_button.connect("clicked", self.on_back_to_updates)
        controls.pack_end(self.back_to_updates_button, False, False, 0)

        self.install_details_revealer = Gtk.Revealer()
        self.install_details_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.install_details_revealer.set_transition_duration(180)
        self.install_details_revealer.set_hexpand(True)
        self.install_details_revealer.set_vexpand(True)

        terminal_scroller = Gtk.ScrolledWindow()
        terminal_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        terminal_scroller.set_hexpand(True)
        terminal_scroller.set_vexpand(True)

        self.install_terminal = Vte.Terminal()
        self.install_terminal.set_hexpand(True)
        self.install_terminal.set_vexpand(True)
        self.install_terminal.set_scrollback_lines(10000)
        self.install_terminal.set_cursor_blink_mode(Vte.CursorBlinkMode.ON)
        self.install_terminal.set_font(Pango.FontDescription("monospace 10"))
        self.install_terminal.connect("child-exited", self.on_install_child_exited)
        self.install_terminal.connect(
            "contents-changed", self.on_install_terminal_contents_changed
        )

        terminal_scroller.add(self.install_terminal)
        self.install_details_revealer.add(terminal_scroller)
        self.install_page.pack_start(self.install_details_revealer, True, True, 0)

    def _build_status(self) -> None:
        self.status_label = Gtk.Label()
        self.status_label.set_xalign(0.0)
        self.outer_box.pack_start(self.status_label, False, False, 0)
        self._set_status(self._ready_status_text())

    # ------------------------------------------------------------------ #
    # Dialogs                                                              #
    # ------------------------------------------------------------------ #

    def _show_preferences_dialog(self) -> None:
        dialog = Gtk.Dialog(
            title=_("Preferences"),
            transient_for=self,
            flags=Gtk.DialogFlags.MODAL,
        )
        dialog.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
        dialog.add_button(_("Apply"), Gtk.ResponseType.APPLY)

        box = dialog.get_content_area()
        box.set_spacing(8)
        box.set_border_width(8)

        # Optional backend visibility — only show toggles for registered backends.
        _registered_ids = {b.backend_id for b in get_registry().get_all_backends()}

        snap_check: Gtk.CheckButton | None = None
        flatpak_check: Gtk.CheckButton | None = None

        if "snap" in _registered_ids:
            snap_check = Gtk.CheckButton(label=_("Show Snap updates"))
            snap_check.set_active(self.prefs.get("show_snap", True))
            box.pack_start(snap_check, False, False, 0)

        if "flatpak" in _registered_ids:
            flatpak_check = Gtk.CheckButton(label=_("Show Flatpak updates"))
            flatpak_check.set_active(self.prefs.get("show_flatpak", True))
            box.pack_start(flatpak_check, False, False, 0)

        dialog.show_all()
        response = dialog.run()

        if response == Gtk.ResponseType.APPLY:
            changed = False

            if snap_check is not None:
                new_val = snap_check.get_active()
                if self.prefs.get("show_snap", True) != new_val:
                    self.prefs["show_snap"] = new_val
                    changed = True
            if flatpak_check is not None:
                new_val = flatpak_check.get_active()
                if self.prefs.get("show_flatpak", True) != new_val:
                    self.prefs["show_flatpak"] = new_val
                    changed = True
            if changed:
                self._save_prefs()
                self.filter_model.refilter()
                self._set_status(_("Preferences saved."))

        dialog.destroy()

    def _show_about_dialog(self) -> None:
        # pylint: disable=too-many-locals
        """Display About Dialog"""
        dialog = Gtk.Dialog(
            title=_("About"),
            transient_for=self,
            flags=Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
        )
        dialog.set_border_width(10)
        dialog.set_default_size(600, 400)
        dialog.add_button(Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE)

        content = dialog.get_content_area()

        outer_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        content.pack_start(outer_box, True, True, 0)

        # Left side
        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left_box.set_size_request(160, -1)
        outer_box.pack_start(left_box, False, False, 0)

        icon = Gtk.Image.new_from_icon_name(
            "bodhi-update-manager", Gtk.IconSize.DIALOG
        )
        icon.set_pixel_size(200)
        left_box.pack_start(icon, False, False, 0)

        version_label = Gtk.Label()
        version_label.set_markup(f"<b>{_('Version:')}</b> {__version__}")
        version_label.set_justify(Gtk.Justification.CENTER)
        left_box.pack_start(version_label, False, False, 0)

        spacer = Gtk.Box()
        spacer.set_size_request(-1, 10)
        left_box.pack_start(spacer, False, False, 0)

        # Right side
        right_frame = Gtk.Frame()
        outer_box.pack_start(right_frame, True, True, 0)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        right_frame.add(scrolled)

        textview = Gtk.TextView()
        textview.set_editable(False)
        textview.set_cursor_visible(False)
        textview.set_monospace(False)
        textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        textview.set_left_margin(10)
        textview.set_right_margin(10)
        textview.set_top_margin(10)
        textview.set_bottom_margin(10)
        scrolled.add(textview)

        pages = {
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

        buttons = [
            ("update", _("Update Manager")),
            ("website", _("Website")),
            ("credits", _("Credits")),
            ("license", _("License")),
        ]

        def set_text(text: str) -> None:
            buffer_ = textview.get_buffer()
            buffer_.set_text(text)

        def on_about_button_clicked(_button, key: str) -> None:
            set_text(pages[key])

        for key, label in buttons:
            btn = Gtk.Button(label=label)
            btn.set_hexpand(False)
            btn.connect("clicked", on_about_button_clicked, key)
            left_box.pack_start(btn, False, False, 0)

        left_box.pack_start(Gtk.Box(), True, True, 0)

        set_text(pages["update"])

        dialog.show_all()
        dialog.run()
        dialog.destroy()


    def _on_show_descriptions_toggled(self, check: Gtk.CheckButton) -> None:
        """Prefs dialog checkbox — delegate to the shared helper."""
        self._set_show_descriptions(check.get_active())

    def _set_show_descriptions(self, enabled: bool) -> None:
        """Single source of truth for the show-descriptions preference.

        Updates the pref, persists it, syncs the View-menu CheckMenuItem
        (blocking its toggled signal via a flag to avoid recursion), and
        applies the markup refresh immediately.
        """
        self.prefs["show_descriptions"] = enabled
        self._save_prefs()
        # Set the guard flag so on_toggle_descriptions ignores this programmatic change.
        self._syncing_desc = True
        try:
            self.show_desc_menu_item.set_active(enabled)
        finally:
            self._syncing_desc = False
        self._apply_show_descriptions()

    def _apply_show_descriptions(self) -> None:
        """Rebuild COL_PACKAGE markup for all rows using the current pref.

        Only the markup string is updated — selection state, versions, and
        all other columns are untouched.
        """
        show_desc = self.prefs.get("show_descriptions", True)
        self.store.freeze_notify()
        try:
            for row in self.store:
                name = row[self.COL_RAW_NAME]
                desc = row[self.COL_DESC]
                row[self.COL_PACKAGE] = self._build_pkg_markup(name, desc, show_desc)
        finally:
            self.store.thaw_notify()

    # ------------------------------------------------------------------ #
    # Preferences persistence                                              #
    # ------------------------------------------------------------------ #

    def _get_prefs_path(self) -> str:
        config_home = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
        return os.path.join(config_home, "bodhi-update-manager", "prefs.json")

    def _load_prefs(self) -> Dict[str, bool]:
        defaults: Dict[str, bool] = {
            "show_descriptions": True,
            "show_snap": True,
            "show_flatpak": True,
        }
        path = self._get_prefs_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    defaults.update(json.load(f))
            except Exception:  # pylint: disable=broad-except
                # Ignore I/O or parse errors; prefs are non-critical
                pass
        return defaults

    def _save_prefs(self) -> None:
        path = self._get_prefs_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.prefs, f)
        except Exception:  # pylint: disable=broad-except
            # Ignore I/O errors; prefs are non-critical
            pass

    # ------------------------------------------------------------------ #
    # Widget helpers                                                       #
    # ------------------------------------------------------------------ #

    def _make_text_column(
        self,
        title: str,
        model_column: int,
        *,
        expand: bool,
        min_width: int,
    ) -> Gtk.TreeViewColumn:
        renderer = Gtk.CellRendererText()
        renderer.set_property("xalign", 0.0)
        renderer.set_property("ellipsize-set", True)

        column = Gtk.TreeViewColumn(title, renderer, text=model_column)
        column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        column.set_resizable(True)
        column.set_expand(expand)
        column.set_min_width(min_width)
        column.set_alignment(0.0)
        return column

    def _ready_status_text(self) -> str:
        return _("Restart required.") if reboot_required() else _("Ready")

    # ------------------------------------------------------------------ #
    # State management                                                     #
    # ------------------------------------------------------------------ #

    def _update_action_sensitivity(self) -> None:
        is_updates = self.stack.get_visible_child_name() == "updates"
        sensitive = not self.refresh_in_progress and not self.install_in_progress and is_updates

        self.check_button.set_sensitive(sensitive)
        self.install_selected_button.set_sensitive(sensitive)
        self.clear_button.set_sensitive(sensitive)
        self.select_all_button.set_sensitive(sensitive)

        if hasattr(self, "refresh_menu_item"):
            self.refresh_menu_item.set_sensitive(sensitive)
            self.install_sel_menu_item.set_sensitive(sensitive)
            self.select_all_menu_item.set_sensitive(sensitive)
            self.clear_menu_item.set_sensitive(sensitive)
            self.show_desc_menu_item.set_sensitive(sensitive)

    def _set_refresh_busy(self, busy: bool) -> None:
        self.refresh_in_progress = busy
        self._update_action_sensitivity()

    def _set_install_busy(self, busy: bool) -> None:
        self.install_in_progress = busy
        self._update_action_sensitivity()
        self.back_to_updates_button.set_sensitive(not busy)
        self.show_details_button.set_sensitive(True)

    def _set_status(self, message: str) -> None:
        if reboot_required() and "Restart required" not in message:
            message = _("%(message)s  Restart required.") % {
						"message": message }
        self.status_label.set_text(message)

    def _update_count_status(
        self, count: int, total_bytes: int, *, cached: bool = False
    ) -> None:
        if count == 0:
            self._set_status(
                _("System is up to date. Cached package data shown.")
                if cached
                else _("System is up to date.")
            )
            return

        has_unknown_size = any(
            row[self.COL_RAW_SIZE] == 0 and row[self.COL_BACKEND] != "apt"
            for row in self.store
        )
        if has_unknown_size:
            size_str = f"{format_size(total_bytes)}+" if total_bytes > 0 else _("Unknown")
        else:
            size_str = format_size(total_bytes)
       
        message = ngettext(
		    "%(count)d update available · Download: %(size)s",
		    "%(count)d updates available · Download: %(size)s",
		    count
        ) % {
		    "count": count,
		    "size": size_str
        }
        if cached:
            message = _("%(message)s · Cached package data") % {"message": message}
				
        # Give a lightweight hint if optional backends found anything.
        extras = []
        for backend, label in (
            ("snap", "Snap"),
            ("flatpak", "Flatpak"),
        ):
            if any(row[self.COL_BACKEND] == backend for row in self.store):
                extras.append(label)

        if extras:
            message = _("%(message)s (includes %(extras)s)") % {
        "message": message,
        "extras": ", ".join(extras)
		}
        self._set_status(message)

    def _refresh_selection_status(self) -> None:
        """Update the status bar to reflect the current checkbox selection.

        If nothing is selected the status bar is left unchanged.
        Otherwise shows the selected count and a download summary:
          - unknown-only  →  N selected · Download: —
          - known-only    →  N selected · Download: 42.2 KB
          - mixed         →  N selected · Download: 42.2 KB+
        A backend is considered "size-reporting" when its raw size > 0.
        """
        total_selected = 0
        known_bytes = 0        # sum of raw sizes from size-reporting rows
        has_known = False      # any selected row has a real byte count
        has_unknown = False    # any selected row has no reported size

        for row in self.store:
            if not row[self.COL_SELECTED]:
                continue
            total_selected += 1
            raw = row[self.COL_RAW_SIZE]
            if raw > 0:
                has_known = True
                known_bytes += raw
            else:
                # raw == 0 and backend != "apt" means size is unknown, not zero.
                # APT rows with size == 0 are genuinely zero-byte (rare/meta pkgs).
                b_id = row[self.COL_BACKEND]
                if b_id != "apt":
                    has_unknown = True
                # APT size==0 contributes neither known nor unknown (truly free).

        if total_selected == 0:
            return

        if has_known and has_unknown:
            dl_part = f"{format_size(known_bytes)}+"
        elif has_known:
            dl_part = format_size(known_bytes)
        else:
            dl_part = _("Unknown")

        message = ngettext(
		    "%(count)d update selected · Download: %(size)s",
		    "%(count)d updates selected · Download: %(size)s",
		    total_selected
	    ) % {
		    "count": total_selected,
		    "size": dl_part
	    }

        self._set_status(message)
	
    # ------------------------------------------------------------------ #
    # Store / data helpers                                                 #
    # ------------------------------------------------------------------ #

    def _category_filter_func(
        self, model: Gtk.TreeModel, iter_: Gtk.TreeIter, _data: object
    ) -> bool:
        row_backend = model[iter_][self.COL_BACKEND]
        # Hide rows whose backend is disabled in Preferences.
        if row_backend == "snap" and not self.prefs.get("show_snap", True):
            return False
        if row_backend == "flatpak" and not self.prefs.get("show_flatpak", True):
            return False
        category_id = self.category_combo.get_active_id()
        if not category_id or category_id == "all":
            return True
        row_category = model[iter_][self.COL_CATEGORY]
        return row_category == category_id

    def _clear_store(self) -> None:
        self.store.clear()

    @staticmethod
    def _category_icon(category: str, backend: str) -> str:
        """Return GTK symbolic icon name for category/backend."""
        if category == "security":
            return "security-high-symbolic"
        if category == "kernel":
            return "applications-system-symbolic"
        if category == "snap" or backend == "snap":
            return "package-x-generic-symbolic"
        if category == "flatpak" or backend == "flatpak":
            return "package-x-generic-symbolic"
        return "software-update-available-symbolic"

    @staticmethod
    def _build_pkg_markup(name: str, description: str, show_desc: bool) -> str:
        """Return Pango markup for the Package column.

        The package name is always rendered bold.  When *show_desc* is True a
        second line containing the description is appended in a smaller style.
        Both inputs are escaped so that any special characters in real package
        names or summaries cannot break the markup.
        """
        name_esc = GLib.markup_escape_text(name)
        markup = f"<b>{name_esc}</b>"
        if show_desc:
            desc_esc = GLib.markup_escape_text(description or _("System package"))
            markup += f"\n<small>{desc_esc}</small>"
        return markup

    def _populate_store(self, updates: List[UpdateItem]) -> None:
        # Freeze signal emission while batch-populating to avoid per-row
        # redraws, which is especially noticeable with large update lists.
        self.store.freeze_notify()
        try:
            self.store.clear()
            show_desc = self.prefs.get("show_descriptions", True)
            for update in updates:
                icon = self._category_icon(update.category, update.backend)
                pkg_markup = self._build_pkg_markup(update.name, update.description, show_desc)
                size_str = (
                    _("N/A")
                    if update.size == 0 and update.backend != "apt"
                    else format_size(update.size)
                )
                self.store.append(
                    [
                        False,           # COL_SELECTED
                        pkg_markup,      # COL_PACKAGE  (Pango markup, rebuilt on toggle)
                        update.installed_version,  # COL_INSTALLED
                        update.candidate_version,  # COL_NEW
                        size_str,        # COL_SIZE     (formatted string)
                        update.origin,   # COL_REPO
                        update.name,     # COL_RAW_NAME (plain name for install routing)
                        update.category,  # COL_CATEGORY
                        update.backend,  # COL_BACKEND
                        icon,            # COL_ICON
                        update.size,     # COL_RAW_SIZE (bytes; 0 for non-reporting backends)
                        update.description or _("System package"),  # COL_DESC (raw, for toggle)
                    ]
                )
        finally:
            self.store.thaw_notify()

    def _selected_package_names(self) -> Dict[str, List[str]]:
        """Return a mapping of backend_id -> [list of selected raw package names]."""
        grouped: Dict[str, List[str]] = {}
        for row in self.filter_model:
            if row[self.COL_SELECTED]:
                b_id = row[self.COL_BACKEND]
                grouped.setdefault(b_id, []).append(row[self.COL_RAW_NAME])
        return grouped

    def _load_cached_updates_on_startup(self) -> None:
        updates: List[UpdateItem] = []
        total_bytes = 0
        error_msgs = []

        enabled_backends = get_registry().get_all_backends()

        for backend in enabled_backends:
            try:
                b_updates, b_bytes = backend.get_updates()
                updates.extend(b_updates)
                total_bytes += b_bytes
            except Exception as exc:  # pylint: disable=broad-except
                error_msgs.append(f"{backend.display_name}: {exc}")

        if error_msgs and not updates:
            self._clear_store()
            self._set_status(_("Failed to read cached package information."))
            return

        self._populate_store(updates)
        self._update_count_status(len(updates), total_bytes, cached=True)

    # ------------------------------------------------------------------ #
    # Refresh flow                                                         #
    # ------------------------------------------------------------------ #

    def _finish_refresh_ui(
        self,
        ok: bool,
        message: str,
        updates: List[UpdateItem],
        total_bytes: int,
    ) -> bool:
        log.info(_("Refresh finished. %d updates. Success: %s"), len(updates), ok)
        self._set_refresh_busy(False)

        # Always populate the store, even on fatal failure
        self._populate_store(updates)

        # Always update the count status so the total "N updates available" is shown.
        # If the refresh failed the displayed data comes from the prior cache.
        self._update_count_status(len(updates), total_bytes, cached=(not ok))

        if not ok and message:
            # Append the failure message to the status rather than overwriting the count
            current_status = self.status_label.get_text()
            self._set_status(_("%(current_status)s — Warning: %(message)s") %
            {"current_status":current_status,
            "message":message})

        return False

    def _refresh_worker(self) -> None:
        messages = []
        backends = get_registry().get_all_backends()

        # Track which backends fully succeeded
        successful_backends = 0

        for backend in backends:
            ok, msg = backend.refresh()
            if not ok and msg:
                messages.append(msg)

        updates: List[UpdateItem] = []
        total_bytes = 0

        for backend in backends:
            try:
                b_updates, b_bytes = backend.get_updates()
                updates.extend(b_updates)
                total_bytes += b_bytes
                successful_backends += 1
            except Exception as exc:  # pylint: disable=broad-except
                log.error("Backend %s get_updates failed: %s", backend.display_name, exc)
                messages.append(f"{backend.display_name} get_updates failed. ({exc})")

        # Only hard-fail if NO enabled backend succeeded
        fatal_fail = (successful_backends == 0 and len(backends) > 0)

        final_msg = _("Package lists refreshed.")
        if messages:
            final_msg = " · ".join(messages)

        log.info(_("Finished querying backends. Total updates: %d"), len(updates))

        GLib.idle_add(  # type: ignore[call-arg]
            self._finish_refresh_ui,
            not fatal_fail, final_msg, updates, total_bytes
        )

    # ------------------------------------------------------------------ #
    # Install flow                                                         #
    # ------------------------------------------------------------------ #

    def _pulse_install_progress(self) -> bool:
        if not self.install_in_progress or not self.install_output_started:
            self.install_pulse_source_id = None
            return False

        self.install_progress.pulse()
        return True

    def _start_install_progress(self, title: str) -> None:
        self.install_state = "AUTH_PENDING"
        self._set_install_busy(True)
        self.install_output_started = False
        self._active_privilege_tool = None
        # Reset sentinel/poller state for fresh install.
        self._auth_sentinel_path = None
        if self._auth_poll_source_id is not None:
            GLib.source_remove(self._auth_poll_source_id)
            self._auth_poll_source_id = None
        self.stack.set_visible_child_name("install")

        self.install_title_label.set_markup(
            "<b>%s</b>" % GLib.markup_escape_text(title)
        )
        self.install_phase_label.set_text(_("Waiting for authentication..."))
        self.install_progress.set_fraction(0.0)
        self.install_progress.set_show_text(True)
        self.install_progress.set_text(_("Waiting for authentication..."))
        self._set_status(_("Ready"))

        self.install_details_revealer.set_reveal_child(False)
        self.show_details_button.set_active(False)
        self.show_details_button.set_label(_("Show Details"))

        if self.install_pulse_source_id is not None:
            GLib.source_remove(self.install_pulse_source_id)
            self.install_pulse_source_id = None

        try:
            self.install_terminal.reset(True, True)
        except Exception:
            pass

    def _mark_install_running(self) -> None:
        """Transition the install UI from AUTH_PENDING to RUNNING.

        Single gate: called by the sentinel poller (pkexec) or the VTE marker
        watcher (sudo/doas) once auth is confirmed.  Idempotent.
        """
        if self.install_state != "AUTH_PENDING":
            return

        self.install_state = "RUNNING"
        self.install_output_started = True
        self.install_phase_label.set_text(
            _("This may take a few minutes.")
        )
        self.install_progress.set_text(_("Installing updates..."))
        self._set_status(_("Installing updates..."))

        self.install_details_revealer.set_reveal_child(True)
        self.show_details_button.set_active(True)
        self.show_details_button.set_label(_("Hide Details"))
        self.install_terminal.grab_focus()

        if self.install_pulse_source_id is None:
            self.install_pulse_source_id = GLib.timeout_add(
                150, self._pulse_install_progress
            )

    def _on_spawn_complete(self, terminal, pid, error, user_data=None):
        """VTE spawn_async callback — (terminal, pid, error, user_data).

        Fires when the privilege tool process is exec'd by VTE's PTY layer.
        Used to catch hard spawn failures (e.g. pkexec binary not found).
        Auth success is detected by the sentinel poller (pkexec) or the VTE
        marker watcher (sudo/doas), not here.
        """
        if error is not None:
            log.error(_("Spawn failed: %s"), error.message)
            self.install_state = "FAILED"
            self._cancel_auth_sentinel()
            self._set_install_busy(False)
            self.install_progress.set_fraction(0.0)
            self.install_progress.set_text(_("Failed"))
            self.install_phase_label.set_text(
                _("Failed to start installation. See Details below.")
            )
            self.install_details_revealer.set_reveal_child(True)
            self.show_details_button.set_active(True)
            self.show_details_button.set_label(_("Hide Details"))
            self._set_status(_("Failed to start installation."))
            return

        log.info(_("Install process spawned (pid %s)."), pid)


    def _spawn_install_command(self, argv: list[str]) -> None:
        """Spawn *argv* directly in the embedded VTE terminal.

        No shell is involved: the first element of *argv* is exec'd directly
        by VTE's PTY layer. For privileged APT operations this means:
            GUI → pkexec → /usr/libexec/bodhi-update-manager-root → apt-get
        """
        envv = [f"{k}={v}" for k, v in os.environ.items()]

        self.install_terminal.spawn_async(
            Vte.PtyFlags.DEFAULT,
            os.getcwd(),
            argv,
            envv,
            GLib.SpawnFlags.DEFAULT,
            None,
            None,
            -1,
            None,
            self._on_spawn_complete,
            None,
        )

    def _handle_terminal_auth_fallback(self) -> None:
        """Update the UI when terminal authentication (sudo/doas) is used instead of pkexec."""
        msg = _(
            "Enter your password in the terminal below."
            " For security, nothing will appear while typing."
        )

        log.info(_("Terminal auth in use — revealing VTE for password entry."))

        self.install_details_revealer.set_reveal_child(True)
        self.show_details_button.set_active(True)
        self.show_details_button.set_label(_("Hide Details"))

        self.install_phase_label.set_text(msg)
        self.install_progress.set_text(_("Waiting for authentication..."))
        self._set_status(_("Ready"))

        self.install_terminal.grab_focus()

    def _poll_auth_sentinel(self) -> bool:
        """GLib timeout callback: check if the sentinel file has appeared.

        Called every 100 ms while waiting for pkexec auth.  When the root
        helper writes the sentinel file we delete it and transition to RUNNING.
        Returns False (stopping the source) once no longer in AUTH_PENDING.
        """
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
            GLib.idle_add(self._mark_install_running)
            return False

        return True

    def _cancel_auth_sentinel(self) -> None:
        """Stop the sentinel poller and clean up any leftover sentinel file."""
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

    def _launch_install(self, argv: list[str], title: str) -> None:
        log.info(_("Starting installation: %s"), title)
        log.debug(_("Command: %s"), argv)

        self._start_install_progress(title)
        # Store the tool used for THIS install so callbacks don't re-lookup.
        self._active_privilege_tool = find_privilege_tool()

        if self._active_privilege_tool == "pkexec":
            # Pass the sentinel path as a CLI argument to bypass pkexec's
            # environment variable stripping.
            sentinel = f"/tmp/bodup-auth-{os.getpid()}-{random.randint(0, 0xFFFFFF):06x}.ok"
            self._auth_sentinel_path = sentinel
            # Insert --sentinel <path> immediately after the helper path.
            # argv layout: [pkexec, helper, subcommand, ...]
            # becomes:     [pkexec, helper, --sentinel, path, subcommand, ...]
            guarded_argv = [argv[0], argv[1], "--sentinel", sentinel, *argv[2:]]
            self._spawn_install_command(guarded_argv)
            self._auth_poll_source_id = GLib.timeout_add(100, self._poll_auth_sentinel)
        else:
            # sudo/doas: auth happens inside the VTE — reveal it immediately,
            # then transition to RUNNING right away (no marker needed).
            self._spawn_install_command(argv)
            self._handle_terminal_auth_fallback()
            GLib.idle_add(self._mark_install_running)

    def _launch_deb_install(self, deb_path: str) -> None:
        """Switch to the install screen and install a local .deb file."""
        deb_name = os.path.basename(deb_path)

        try:
            argv = build_deb_install_argv(deb_path)
        except (RuntimeError, ValueError, FileNotFoundError) as exc:
            # Show a minimal install page so the error is visible, then bail.
            self._start_install_progress(_("Installing %(deb_name)s...")
            % {"deb_name": deb_name})
            self._set_install_busy(False)
            self.install_progress.set_fraction(0.0)
            self.install_progress.set_text(_("Failed"))
            self.install_phase_label.set_text(str(exc))
            self._set_status(_("Validation failed: %(exc)s") %
            {"exc": exc})
            return

        self._launch_install(argv, _("Installing %(deb_name)s...") %
        {"deb_name": deb_name})

    def _finish_install_success(self) -> None:
        log.info(_("Installation completed successfully."))
        self.install_state = "COMPLETE"
        self._cancel_auth_sentinel()
        self._set_install_busy(False)
        self.install_progress.set_fraction(1.0)
        self.install_progress.set_text(_("Complete"))
        self.install_phase_label.set_text(_("Updates installed successfully."))
        self._set_status(_("Ready"))

        # Show the reboot banner if the system has flagged a restart is needed.
        if reboot_required():
            self.reboot_info_bar.show()

    def _finish_install_failure(self, exit_code: int) -> None:
        log.error(_("Installation failed with exit code: %s"), exit_code)
        self.install_state = "FAILED"
        self._cancel_auth_sentinel()
        self._set_install_busy(False)
        self.install_progress.set_fraction(0.0)
        self.install_progress.set_text(_("Failed"))
        self.install_phase_label.set_text(
            _("Update failed. Exit code: %(exit_code)s. See Details below.")
        % {"exit_code":exit_code})

        # Always reveal the terminal so the error output is visible.
        self.install_details_revealer.set_reveal_child(True)
        self.show_details_button.set_active(True)
        self.show_details_button.set_label(_("Hide Details"))
        self._set_status(_("Update failed. Exit code: %(exit_code)s") %
        {"exit_code":exit_code})

    def _terminal_text(self) -> str:
        """Return the current plain-text contents of the VTE terminal.

        Returns an empty string on any error or if the terminal is blank.

        get_text() must be called with attributes=None to satisfy the
        vte_terminal_get_text internal assertion 'attributes == nullptr'.
        Passing only the is_selected callback causes PyGObject to supply a
        non-null GArray, which triggers that assertion.
        """
        try:
            result = self.install_terminal.get_text(lambda *a: True, None)
            text = result[0] if isinstance(result, tuple) else result
            return text or ""
        except Exception:
            return ""
    def _on_reboot_bar_response(self, _bar: Gtk.InfoBar, response_id: int) -> None:
        """Handle the Restart Now button in the reboot info bar."""
        if response_id != Gtk.ResponseType.ACCEPT:
            return

        privilege_tool = find_privilege_tool()
        if privilege_tool is None:
            self._set_status(_("No privilege tool found. Please reboot manually."))
            return

        from bodhi_update.install_commands import get_helper_path  # noqa: PLC0415
        try:
            subprocess.Popen([privilege_tool, get_helper_path(), "reboot"])
        except OSError as exc:
            self._set_status(_("Failed to initiate reboot: %(exc)s") %
            {"exc":exc})

    # ------------------------------------------------------------------ #
    # Signal handlers                                                      #
    # ------------------------------------------------------------------ #

    def on_install_terminal_contents_changed(self, _terminal: Vte.Terminal) -> None:
        """VTE contents-changed signal handler (reserved for future use)."""
        pass

    def on_toggle_selected(self, _renderer: Gtk.CellRendererToggle, path: str) -> None:
        if self.refresh_in_progress or self.install_in_progress:
            return

        # Path is relative to the filter_model, translate to child store.
        filter_iter = self.filter_model.get_iter(path)
        child_iter = self.filter_model.convert_iter_to_child_iter(filter_iter)

        current = self.store[child_iter][self.COL_SELECTED]
        self.store[child_iter][self.COL_SELECTED] = not current
        self._refresh_selection_status()

    def on_clear_selection(self, _button: Gtk.Button) -> None:
        """Uncheck all rows in the store."""
        if self.refresh_in_progress or self.install_in_progress:
            return
        for row in self.store:
            row[self.COL_SELECTED] = False
        self._refresh_selection_status()

    def on_select_all(self, _button: Gtk.Button) -> None:
        """Check all rows currently visible through the active category filter."""
        if self.refresh_in_progress or self.install_in_progress:
            return

        # Collect paths first for safe iteration when modifying underlying store
        paths = [row.path for row in self.filter_model]
        for path in paths:
            f_iter = self.filter_model.get_iter(path)
            c_iter = self.filter_model.convert_iter_to_child_iter(f_iter)
            self.store[c_iter][self.COL_SELECTED] = True

        self._refresh_selection_status()

    def on_category_changed(self, _combo: Gtk.ComboBoxText) -> None:
        if self.refresh_in_progress or self.install_in_progress:
            return
        self.filter_model.refilter()

    def on_toggle_descriptions(self, checkmenuitem: Gtk.CheckMenuItem) -> None:
        """View-menu CheckMenuItem — delegate to the shared helper.

        Guarded by _syncing_desc to prevent re-entry when _set_show_descriptions
        programmatically updates the menu item state.
        """
        if self._syncing_desc:
            return
        self._set_show_descriptions(checkmenuitem.get_active())

    def on_check_updates(self, _button: Gtk.Button | None) -> None:
        if self.refresh_in_progress or self.install_in_progress:
            return

        # Check all enabled backends to see if any package manager is busy
        for backend in get_registry().get_all_backends():
            is_busy, message = backend.check_busy()
            if is_busy:
                self._set_status(message)
                return

        self._set_refresh_busy(True)
        self._set_status(_("Checking for updates..."))
        log.info(_("Starting background refresh for updates."))

        worker = threading.Thread(target=self._refresh_worker, daemon=True)
        worker.start()

    def _build_install_target_command(
        self, grouped_packages: Dict[str, List[str]] | None
    ) -> list[str]:
        """
        Produce an install command for the specified backend group.

        Raises RuntimeError for multi-backend simultaneous installs or an
        unrecognised backend ID.
        """
        if not grouped_packages:
            registry = get_registry()
            apt_backend = registry.get_backend("apt")
            if apt_backend:
                return apt_backend.build_install_command(None)
            raise RuntimeError(_("Primary backend (APT) is not configured."))

        if len(grouped_packages) > 1:
            raise RuntimeError(
                _("Installing from multiple package sources simultaneously is not yet supported. "
                "Please select packages from one source type only.")
            )

        backend_id = next(iter(grouped_packages.keys()))
        target_packages = grouped_packages[backend_id]

        registry = get_registry()
        backend = registry.get_backend(backend_id)
        if not backend:
            raise RuntimeError(_("Requested installation for unknown backend: %(backend_id)s")
            % {"backend_id":backend_id})

        return backend.build_install_command(target_packages)

    def on_install_selected(self, _button: Gtk.Button | Gtk.MenuItem | None) -> None:
        if self.refresh_in_progress or self.install_in_progress:
            return

        grouped_packages = self._selected_package_names()
        if not any(pkgs for pkgs in grouped_packages.values()):
            self._set_status(_("No packages selected."))
            return

        try:
            argv = self._build_install_target_command(grouped_packages)
        except RuntimeError as exc:
            self._set_status(str(exc))
            return

        self._launch_install(argv, _("Installing selected updates..."))

    def on_toggle_details(self, button: Gtk.ToggleButton) -> None:
        revealed = button.get_active()
        self.install_details_revealer.set_reveal_child(revealed)
        button.set_label(_("Hide Details") if revealed else _("Show Details"))

    def on_back_to_updates(self, _button: Gtk.Button) -> None:
        if self.install_in_progress:
            return

        # Clear all checkbox state immediately so no stale checked rows are
        # visible while the async refresh is pending.
        for row in self.store:
            row[self.COL_SELECTED] = False

        self.stack.set_visible_child_name("updates")
        self._update_action_sensitivity()
        # Use the non-privileged cached load path only — on_check_updates
        # would trigger backend.refresh() which prompts for pkexec
        # authentication on the APT backend, which is unacceptable UX
        # during simple back-navigation after install.
        GLib.idle_add(self._load_cached_updates_on_startup)

    def on_install_child_exited(self, _terminal: Vte.Terminal, status: int) -> None:
        if status == 0:
            self._finish_install_success()
        else:
            self._finish_install_failure(status)



class UpdateManagerApplication(Gtk.Application):
    """Single-instance GTK application that owns one window and one tray icon.

    Launching a second copy raises the existing window instead of creating
    a duplicate.  The tray indicator (when active) operates on the same
    shared window rather than spawning another process.
    """

    def __init__(self, *, deb_path: str | None = None) -> None:
        super().__init__(
            application_id="org.bodhilinux.UpdateManager",
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self._tray_mode: bool = False
        self._deb_path = deb_path
        self._window: UpdateManagerWindow | None = None
        self._tray = None
        self._held_for_tray: bool = False

    # ------------------------------------------------------------------
    # Gtk.Application overrides
    # ------------------------------------------------------------------

    def do_command_line(self, command_line) -> int:  # type: ignore[override]
        """Parse --tray from the GTK command line before activation."""
        args = command_line.get_arguments()[1:]
        self._tray_mode = "--tray" in args
        self.activate()
        return 0

    def do_activate(self) -> None:  # type: ignore[override]
        """First activation: create window (normal) or tray only (tray mode).

        In tray mode the window is NOT created here at all — GTK cannot
        implicitly show a window that doesn't exist yet.  The tray icon will
        call _get_or_create_window() lazily when the user requests it.

        On subsequent activations (e.g. user re-launches the binary while the
        app is already running) we create/show the window unconditionally so
        the user always gets a visible result.
        """
        if self._window is None:
            if self._tray_mode:
                from bodhi_update.tray import TrayIcon  # noqa: PLC0415
                self._tray = TrayIcon(self)
                self.hold()           # prevent GLib loop from exiting with no windows
                self._held_for_tray = True
                return

            self._window = self._get_or_create_window()
            self._window.show_all()
            return

        # Already running → raise the window.
        win = self._get_or_create_window()
        win.show_all()
        win.present()

    def _get_or_create_window(self) -> "UpdateManagerWindow":
        """Return the existing window, creating and wiring it up if needed."""
        if self._window is None:
            self._window = UpdateManagerWindow(deb_path=self._deb_path)
            self._window.set_application(self)
            # Intercept close: hide instead of destroy while the tray is active.
            self._window.connect("delete-event", self._on_window_delete)
        return self._window

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _on_window_delete(self, _win: Gtk.Window, _event: object) -> bool:
        """Hide instead of destroy when a tray is active; destroy otherwise."""
        if self._tray is not None:
            self._window.hide()
            return True  # Suppress default destroy
        return False  # Let default destroy proceed → app exits


def main() -> None:
    import sys  # noqa: PLC0415

    # Sniff positional args for a .deb path only — --tray is handled by
    # do_command_line() so that GTK sees it before do_activate() fires.
    deb_path: str | None = None
    for arg in sys.argv[1:]:
        if arg.lower().endswith(".deb"):
            deb_path = arg
            break

    app = UpdateManagerApplication(deb_path=deb_path)
    app.run(sys.argv)

