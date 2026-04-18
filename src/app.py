"""GTK3 GUI for the Update Manager with embedded VTE install view."""

# pylint: disable=too-many-lines  # UpdateManagerWindow is one cohesive GTK class
from __future__ import annotations

import gettext
from enum import IntEnum
import logging
import os
import subprocess
import threading
from typing import Dict, List

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("bodhi-update-manager")
logger = logging.getLogger(__name__)

APP_NAME = "bodhi-update-manager"

gettext.bindtextdomain(APP_NAME, "/usr/share/locale")
gettext.textdomain(APP_NAME)
_ = gettext.gettext
ngettext = gettext.ngettext

# gi.require_version() must be called before any gi.repository imports.
import gi  # noqa: E402

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango, Vte  # noqa: E402

from bodhi_update._version import __version__  # noqa: E402
from bodhi_update.backend_ui_service import BackendUIService  # noqa: E402
from bodhi_update.hold_controller import HoldController  # noqa: E402
from bodhi_update.install_controller import InstallController  # noqa: E402
from bodhi_update.models import (  # noqa: E402
    CONSTRAINT_BLOCKED, CONSTRAINT_HELD, CONSTRAINT_NORMAL, UpdateItem,
)
from bodhi_update.prefs import PreferencesStore  # noqa: E402
from bodhi_update.refresh_controller import RefreshController  # noqa: E402
from bodhi_update.status_messages import (  # noqa: E402
    format_selected_count_status,
    format_update_count_status,
    hidden_held_count,
    ready_status_text,
    with_restart_suffix,
)
from bodhi_update.utils import (  # noqa: E402
    find_privilege_tool, format_size, reboot_required,
)

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

COLUMN_SCHEMA = (
    ("SELECTED", bool),
    ("PACKAGE", str),
    ("INSTALLED", str),
    ("NEW", str),
    ("SIZE", str),
    ("REPO", str),
    ("RAW_NAME", str),
    ("CATEGORY", str),
    ("FILTER_GROUP", str),
    ("BACKEND", str),
    ("ICON", str),
    ("RAW_SIZE", int),
    ("DESC", str),
    ("HELD", str),
)

Col = IntEnum(
    "Col",
    {name: index for index, (name, _col_type) in enumerate(COLUMN_SCHEMA)},
)

COLUMN_TYPES = tuple(col_type for _name, col_type in COLUMN_SCHEMA)

assert len(COLUMN_TYPES) == len(Col)


def clamp(value: int, lo: int, hi: int) -> int:
    """Return *value* clamped to [lo, hi]."""
    return max(lo, min(value, hi))


class UpdateManagerWindow(Gtk.Window):  # pylint: disable=too-many-instance-attributes
    """Main application window: update list, install screen, preferences, and tray hooks."""

    def __init__(self, deb_path: str | None = None) -> None:
        super().__init__(title=_("Update Manager"))
        self._apply_adaptive_window_size()
        self.set_icon_name("bodhi-update-manager")
        self.set_position(Gtk.WindowPosition.CENTER)

        self.refresh_in_progress = False
        self.install_in_progress = False

        self.pref_store = PreferencesStore(APP_NAME)
        self.prefs = self.pref_store.load()
        self.backend_service = BackendUIService(self.prefs)

        # Guard flag used by _set_show_descriptions() to suppress menu re-entry.
        self._syncing_desc = False

        # Show a bare window immediately; defer the heavy build to the event loop.
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(self.main_box)
        self.show_all()

        GLib.idle_add(self._build_full_ui, deb_path)

    def _build_full_ui(self, deb_path: str | None) -> bool:
        """Build all widgets and wire signals. Deferred via GLib.idle_add; returns False."""
        # Returns False so GLib won't reschedule this one-shot idle callback.
        self.backend_service.initialize()

        self.store = Gtk.ListStore(*COLUMN_TYPES)
        self.filter_model = self.store.filter_new()
        self.filter_model.set_visible_func(self._category_filter_func)

        self.outer_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                 spacing=8)
        self.outer_box.set_border_width(8)

        self._build_menubar()
        self.main_box.pack_start(self.outer_box, True, True, 0)

        self._build_toolbar()
        self._build_reboot_bar()
        self._build_stack()
        self.install_controller = InstallController(self)
        self.refresh_controller = RefreshController(self)
        self.hold_controller = HoldController(self)
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
            self._set_updates_loading(True)
            threading.Thread(target=self._load_cached_updates_on_startup,
                             daemon=True).start()

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
        self.refresh_menu_item.connect("activate",
                                       lambda _: self.on_check_updates(None))
        file_menu.append(self.refresh_menu_item)

        self.install_sel_menu_item = Gtk.MenuItem(label=_("Install Selected"))
        self.install_sel_menu_item.connect(
            "activate", lambda _: self.on_install_selected(None))
        file_menu.append(self.install_sel_menu_item)

        file_menu.append(Gtk.SeparatorMenuItem())

        self.select_all_menu_item = Gtk.MenuItem(label=_("Select All"))
        self.select_all_menu_item.connect("activate",
                                          lambda _: self.on_select_all(None))
        file_menu.append(self.select_all_menu_item)

        self.clear_menu_item = Gtk.MenuItem(label=_("Clear"))
        self.clear_menu_item.connect("activate",
                                     lambda _: self.on_clear_selection(None))
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
        self.show_desc_menu_item.set_active(
            self.prefs.get("show_descriptions", True))
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
        self.install_selected_button.connect("clicked",
                                             self.on_install_selected)
        toolbar.pack_start(self.install_selected_button, False, False, 0)

        spacer = Gtk.Box()
        toolbar.pack_start(spacer, True, True, 0)

        self.category_combo = Gtk.ComboBoxText()
        self._rebuild_category_combo()
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

    def _build_updates_page(self) -> None:  # pylint: disable=too-many-statements
        """Build the updates list page (treeview + loading stack)."""
        self.updates_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                    spacing=0)
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
        # Fixed height mode is faster; requires all columns to use FIXED sizing.
        self.tree.set_fixed_height_mode(True)
        self.tree.set_hover_selection(False)

        # Type icon column (leftmost) — symbolic GTK icons
        icon_renderer = Gtk.CellRendererPixbuf()
        icon_renderer.set_property("xalign", 0.5)

        icon_column = Gtk.TreeViewColumn(_("Type"), icon_renderer)
        icon_column.add_attribute(icon_renderer, "icon-name", Col.ICON)

        icon_column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        icon_column.set_fixed_width(70)
        icon_column.set_resizable(False)

        self.tree.append_column(icon_column)

        # Checkbox "Upgrade" column.
        toggle_renderer = Gtk.CellRendererToggle()
        toggle_renderer.set_property("activatable", True)
        toggle_renderer.connect("toggled", self.on_toggle_selected)
        toggle_column = Gtk.TreeViewColumn(_("Upgrade"), toggle_renderer, active=Col.SELECTED)
        toggle_column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        toggle_column.set_fixed_width(90)
        # Hide the checkbox for held/blocked rows — they are non-actionable.
        toggle_column.set_cell_data_func(toggle_renderer,
                                         self._toggle_cell_data_func)
        self.tree.append_column(toggle_column)

        # COL_PACKAGE always uses Pango markup; regenerated on Show Descriptions toggle.
        self.pkg_renderer = Gtk.CellRendererText()
        self.pkg_renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
        self.pkg_renderer.set_property("ellipsize-set", True)
        self.pkg_column = Gtk.TreeViewColumn(
            _("Package"), self.pkg_renderer, markup=Col.PACKAGE)
        self.pkg_column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        self.pkg_column.set_resizable(True)
        self.pkg_column.set_expand(True)
        self.pkg_column.set_min_width(220)
        self.pkg_column.set_alignment(0.0)
        self.tree.append_column(self.pkg_column)

        self.tree.append_column(
            self._make_text_column(
                _("Installed"),Col.INSTALLED, expand=False, min_width=150
            )
        )
        self.tree.append_column(
            self._make_text_column(
                _("New"), Col.NEW, expand=False, min_width=150
            )
        )
        self.tree.append_column(
            self._make_text_column(
                _("Size"), Col.SIZE, expand=False, min_width=100
            )
        )
        self.tree.append_column(
            self._make_text_column(
                _("Repository"), Col.REPO, expand=True, min_width=180
            )
        )

        scroller.add(self.tree)

        # Loading page: spinner only, centred. Status bar carries the text.
        loading_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        loading_box.set_halign(Gtk.Align.CENTER)
        loading_box.set_valign(Gtk.Align.CENTER)
        self._loading_spinner = Gtk.Spinner()
        self._loading_spinner.set_size_request(32, 32)
        loading_box.pack_start(self._loading_spinner, False, False, 0)

        # Nested stack: "loading" vs "list".
        self.updates_stack = Gtk.Stack()
        self.updates_stack.set_transition_type(
            Gtk.StackTransitionType.CROSSFADE)
        self.updates_stack.set_transition_duration(150)
        self.updates_stack.set_hexpand(True)
        self.updates_stack.set_vexpand(True)
        self.updates_stack.add_named(loading_box, "loading")
        self.updates_stack.add_named(scroller, "list")
        self.updates_stack.set_visible_child_name("list")

        self.updates_page.pack_start(self.updates_stack, True, True, 0)
        self.tree.connect("button-press-event", self._on_tree_button_press)

    def _build_install_page(self) -> None:
        self.install_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                    spacing=10)
        self.install_page.set_hexpand(True)
        self.install_page.set_vexpand(True)

        self.install_title_label = Gtk.Label()
        self.install_title_label.set_xalign(0.0)
        self.install_title_label.set_markup(
            f"<b>{_('Installing updates...')}</b>")
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
        self.install_details_revealer.set_transition_type(
            Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.install_details_revealer.set_transition_duration(180)
        self.install_details_revealer.set_hexpand(True)
        self.install_details_revealer.set_vexpand(True)

        terminal_scroller = Gtk.ScrolledWindow()
        terminal_scroller.set_policy(Gtk.PolicyType.AUTOMATIC,
                                     Gtk.PolicyType.AUTOMATIC)
        terminal_scroller.set_hexpand(True)
        terminal_scroller.set_vexpand(True)

        self.install_terminal = Vte.Terminal()
        self.install_terminal.set_hexpand(True)
        self.install_terminal.set_vexpand(True)
        self.install_terminal.set_scrollback_lines(10000)
        self.install_terminal.set_cursor_blink_mode(Vte.CursorBlinkMode.ON)
        self.install_terminal.set_font(Pango.FontDescription("monospace 10"))
        self.install_terminal.connect("child-exited",
                                      self.on_install_child_exited)
        self.install_terminal.connect("contents-changed",
                                      self.on_install_terminal_contents_changed)

        terminal_scroller.add(self.install_terminal)
        self.install_details_revealer.add(terminal_scroller)
        self.install_page.pack_start(self.install_details_revealer, True, True,
                                     0)

    def _build_status(self) -> None:
        self.status_label = Gtk.Label()
        self.status_label.set_xalign(0.0)
        self.outer_box.pack_start(self.status_label, False, False, 0)
        self._set_status(ready_status_text())

    # ------------------------------------------------------------------ #
    # Dialogs                                                              #
    # ------------------------------------------------------------------ #

    def _rebuild_category_combo(self) -> None:
        """Rebuild the category combo from current backend state + prefs."""
        current_id = self.category_combo.get_active_id() or "all"

        self.category_combo.remove_all()

        # Built-in categories
        self.category_combo.append("all", _("All"))
        self.category_combo.append("security", _("Security"))
        self.category_combo.append("kernel", _("Kernel"))
        self.category_combo.append("system", _("System"))

        # Dynamic backend groups
        for group_key, (group_label, _sort_order) in sorted(
            self.backend_service.get_visible_filter_groups().items(),
            key=lambda item: (item[1][1], item[1][0].lower()),
        ):
            self.category_combo.append(group_key, group_label)

        # Restore selection if possible
        self.category_combo.set_active_id(current_id)
        if self.category_combo.get_active_id() is None:
            self.category_combo.set_active_id("all")

    def _show_preferences_dialog(self) -> None:  # pylint: disable=too-many-statements
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

        notif_check = Gtk.CheckButton(label=_("Show notifications"))
        notif_check.set_active(self.prefs.get("show_notifications", True))
        box.pack_start(notif_check, False, False, 0)

        held_check = Gtk.CheckButton(label=_("Show held/blocked packages"))
        held_check.set_active(self.prefs.get("show_held_packages", False))
        box.pack_start(held_check, False, False, 0)

        backend_checks: dict[str, Gtk.CheckButton] = {}

        for backend in self.backend_service.get_preference_backends():
            label = _("Show %(name)s updates") % {"name": backend.display_name}
            check = Gtk.CheckButton(label=label)
            check.set_active(
                self.backend_service.is_backend_enabled(backend.backend_id)
            )
            box.pack_start(check, False, False, 0)
            backend_checks[backend.backend_id] = check

        dialog.show_all()
        response = dialog.run()

        if response == Gtk.ResponseType.APPLY:
            changed = False

            new_notif = notif_check.get_active()
            if self.prefs.get("show_notifications", True) != new_notif:
                self.prefs["show_notifications"] = new_notif
                changed = True
                if not new_notif:
                    _app = self.get_application()
                    if _app is not None and hasattr(_app, "set_tray_count"):
                        _app.set_tray_count(0)

            new_held = held_check.get_active()
            if self.prefs.get("show_held_packages", False) != new_held:
                self.prefs["show_held_packages"] = new_held
                changed = True

            visibility = self.prefs.setdefault("backend_visibility", {})

            for backend_id, check in backend_checks.items():
                new_val = check.get_active()
                if visibility.get(backend_id, True) != new_val:
                    visibility[backend_id] = new_val
                    changed = True
            if changed:
                self.pref_store.save(self.prefs)
                self._rebuild_category_combo()
                self.filter_model.refilter()
                # Flash "Preferences saved." briefly, then restore the real count status.
                self._restore_current_update_status()
                self._set_status(_("Preferences saved."))
                GLib.timeout_add_seconds(3, self._restore_current_update_status)

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
        """Update and persist the show-descriptions pref, sync the menu item, rebuild markup."""
        # _syncing_desc blocks on_toggle_descriptions re-entry during the menu sync.
        self.prefs["show_descriptions"] = enabled
        self.pref_store.save(self.prefs)
        self._syncing_desc = True
        try:
            self.show_desc_menu_item.set_active(enabled)
        finally:
            self._syncing_desc = False
        self._apply_show_descriptions()

    def _apply_show_descriptions(self) -> None:
        """Rebuild COL_PACKAGE markup for all rows. Only the markup string changes."""
        # freeze_notify/thaw_notify batches TreeView change signals.
        show_desc = self.prefs.get("show_descriptions", True)
        self.store.freeze_notify()
        try:
            for row in self.store:
                name = row[Col.RAW_NAME]
                desc = row[Col.DESC]
                constraint = row[Col.HELD]
                row[Col.PACKAGE] = self._build_pkg_markup(
                    name, desc, show_desc, constraint)
        finally:
            self.store.thaw_notify()

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

    def _apply_adaptive_window_size(self) -> None:
        """Set initial window size, clamped to the monitor workarea (respects panels/docks)."""
        # Falls back to preferred size if GDK can't report workarea dimensions.
        preferred_w = 1100
        preferred_h = 700
        min_w = 760
        min_h = 520
        margin = 12
        margin = 12

        width = preferred_w
        height = preferred_h

        # Catch GDK errors; fall back to preferred size.
        try:
            screen = Gdk.Screen.get_default()
            if screen is not None:
                monitor_num = max(screen.get_primary_monitor(), 0)
                workarea = screen.get_monitor_workarea(monitor_num)

                if workarea and workarea.width > 0 and workarea.height > 0:
                    max_w = max(min_w, workarea.width - margin)
                    max_h = max(min_h, workarea.height - margin)

                    # shrink if the monitor/workarea is too small
                    width = clamp(preferred_w, min_w, max_w)
                    height = clamp(preferred_h, min_h, max_h)
        except (AttributeError, TypeError, ValueError):
            # Fall back to preferred size if GDK API fails or returns junk
            width, height = preferred_w, preferred_h

        self.set_default_size(width, height)

    # ------------------------------------------------------------------ #
    # State management                                                     #
    # ------------------------------------------------------------------ #

    def _update_action_sensitivity(self) -> None:
        is_updates = self.stack.get_visible_child_name() == "updates"
        updates_loading = getattr(self, "_updates_loading", False)
        sensitive = (not self.refresh_in_progress and
                     not self.install_in_progress and not updates_loading and
                     is_updates)

        self.check_button.set_sensitive(sensitive)
        self.install_selected_button.set_sensitive(sensitive)
        self.clear_button.set_sensitive(sensitive)
        self.select_all_button.set_sensitive(sensitive)
        if hasattr(self, "category_combo"):
            self.category_combo.set_sensitive(sensitive)

        if hasattr(self, "refresh_menu_item"):
            self.refresh_menu_item.set_sensitive(sensitive)
            self.install_sel_menu_item.set_sensitive(sensitive)
            self.select_all_menu_item.set_sensitive(sensitive)
            self.clear_menu_item.set_sensitive(sensitive)
            self.show_desc_menu_item.set_sensitive(sensitive)

    def _set_updates_loading(self, loading: bool) -> None:
        """Switch the updates view between the loading and list pages."""
        self._updates_loading = loading
        if loading:
            self.updates_stack.set_visible_child_name("loading")
            self._loading_spinner.start()
            self._set_status(_("Loading updates..."))
        else:
            self._loading_spinner.stop()
            self.updates_stack.set_visible_child_name("list")
        self._update_action_sensitivity()

    def _set_refresh_busy(self, busy: bool) -> None:
        self.refresh_in_progress = busy
        self._update_action_sensitivity()

    def _notify_tray(self, count: int, severity: str = "medium") -> None:
        """Forward update count and severity to the tray icon badge (no-op if no tray)."""
        app = self.get_application()
        if app is not None and hasattr(app, "set_tray_count"):
            app.set_tray_count(count, severity)

    def _set_install_busy(self, busy: bool) -> None:
        self.install_in_progress = busy
        self._update_action_sensitivity()
        self.back_to_updates_button.set_sensitive(not busy)
        self.show_details_button.set_sensitive(True)

    def _set_status(self, message: str) -> None:
        self.status_label.set_text(with_restart_suffix(message))

    def _update_count_status(
        self,
        count: int,
        total_bytes: int,
        *,
        cached: bool = False,
    ) -> None:
        if count == 0:
            self._set_status(format_update_count_status(0, total_bytes, cached=cached))
            self._notify_tray(0, "low")
            return

        has_unknown_size = any(
            row[Col.RAW_SIZE] == 0 and row[Col.BACKEND] != "apt"
            for row in self.store
        )

        extras = []
        for backend, label in (("snap", "Snap"), ("flatpak", "Flatpak")):
            if any(row[Col.BACKEND] == backend for row in self.store):
                extras.append(label)

        hidden = 0
        if not self.prefs.get("show_held_packages", False):
            hidden = hidden_held_count(self.store, Col.HELD)

        message = format_update_count_status(
            count,
            total_bytes,
            cached=cached,
            has_unknown_size=has_unknown_size,
            extras=extras,
            hidden_held_count=hidden,
        )
        self._set_status(message)

        from bodhi_update.tray import _pkg_severity  # noqa: PLC0415

        severity = "low"
        actionable_count = 0
        for row in self.store:
            if row[Col.HELD] in (CONSTRAINT_HELD, CONSTRAINT_BLOCKED):
                continue
            actionable_count += 1
            value = _pkg_severity(
                row[Col.RAW_NAME],
                row[Col.CATEGORY],
                row[Col.BACKEND],
            )
            if value == "high":
                severity = "high"
                break
            if value == "medium":
                severity = "medium"

        self._notify_tray(actionable_count, severity)

    def _refresh_selection_status(self) -> None:
        """Update the status bar with selected count + download size.

        No-op if nothing is selected.  Download summary:
          unknown only  →  —
          known only    →  42.2 KB
          mixed         →  42.2 KB+  (non-APT backends don't report sizes)
        """
        total_selected = 0
        known_bytes = 0
        has_known = False
        has_unknown = False

        for row in self.store:
            if not row[Col.SELECTED]:
                continue

            total_selected += 1
            raw = row[Col.RAW_SIZE]
            if raw > 0:
                has_known = True
                known_bytes += raw
            elif row[Col.BACKEND] != "apt":
                has_unknown = True

        message = format_selected_count_status(
            total_selected,
            known_bytes,
            has_known=has_known,
            has_unknown=has_unknown,
        )
        if message:
            self._set_status(message)
    # ------------------------------------------------------------------ #
    # Context menu (right-click hold/unhold)                               #
    # ------------------------------------------------------------------ #

    def _on_tree_button_press(self, widget: Gtk.TreeView,
                              event: object) -> bool:
        """Show APT hold/unhold context menu on right-click."""
        if event.type != Gdk.EventType.BUTTON_PRESS or event.button != 3:
            return False
        result = widget.get_path_at_pos(int(event.x), int(event.y))
        if result is None:
            return False
        path, *_ = result
        f_iter = self.filter_model.get_iter(path)
        row = self.filter_model[f_iter]
        if row[Col.BACKEND] != "apt":
            return False
        self._show_hold_menu(
            event,
            row[Col.RAW_NAME],
            row[Col.HELD] == CONSTRAINT_HELD,
        )
        return True

    def _show_hold_menu(self, event: object, pkg_name: str,
                        is_held: bool) -> None:
        label = _("Unhold package") if is_held else _("Hold package")
        menu = Gtk.Menu()
        item = Gtk.ImageMenuItem(label=label)
        img = Gtk.Image.new_from_icon_name("changes-prevent-symbolic",
                                           Gtk.IconSize.MENU)
        item.set_image(img)
        item.set_always_show_image(True)
        item.connect(
            "activate",
            lambda _: self.hold_controller.do_hold_toggle(pkg_name, not is_held),
        )
        menu.append(item)
        menu.show_all()
        menu.popup_at_pointer(event)

    # ------------------------------------------------------------------ #
    # Store / data helpers                                                 #
    # ------------------------------------------------------------------ #

    def _restore_current_update_status(self) -> bool:
        """Recompute the normal status line from the current store state."""
        if any(row[Col.SELECTED] for row in self.store):
            return False  # user made a selection; leave their status line alone
        total_bytes = sum(row[Col.RAW_SIZE]
                          for row in self.store
                          if row[Col.HELD] == CONSTRAINT_NORMAL)
        actionable = sum(
            1 for row in self.store if row[Col.HELD] == CONSTRAINT_NORMAL)
        self._update_count_status(actionable, total_bytes, cached=True)
        return False  # one-shot: remove the timeout source

    def _category_filter_func(
        self,
        model: Gtk.TreeModel,
        iter_: Gtk.TreeIter,
        _data: object,
    ) -> bool:
        row_backend = model[iter_][Col.BACKEND]

        if not self.backend_service.is_backend_enabled(row_backend):
            return False

        if (
            model[iter_][Col.HELD] in (CONSTRAINT_HELD, CONSTRAINT_BLOCKED)
            and not self.prefs.get("show_held_packages", False)
        ):
            return False

        selected_id = self.category_combo.get_active_id()
        if not selected_id or selected_id == "all":
            return True

        if selected_id in {"security", "kernel", "system"}:
            return model[iter_][Col.CATEGORY] == selected_id

        return model[iter_][Col.FILTER_GROUP] == selected_id

    @staticmethod
    def _toggle_cell_data_func(
        _column: Gtk.TreeViewColumn,
        cell: Gtk.CellRenderer,
        model: Gtk.TreeModel,
        iter_: Gtk.TreeIter,
        _data: object,
    ) -> None:
        """Hide the checkbox for held/blocked rows — they are non-actionable."""
        constraint = model[iter_][Col.HELD]
        cell.set_property(
            "visible",
            constraint not in (CONSTRAINT_HELD, CONSTRAINT_BLOCKED),
        )

    def _clear_store(self) -> None:
        self.store.clear()

    @staticmethod
    def _build_pkg_markup(name: str,
                          description: str,
                          show_desc: bool,
                          constraint: str = CONSTRAINT_NORMAL) -> str:
        """Return Pango markup for the Package column."""
        name_esc = GLib.markup_escape_text(name)
        markup = f"<b>{name_esc}</b>"
        if constraint == CONSTRAINT_HELD:
            hint = _("Held package")
            if show_desc:
                desc_esc = GLib.markup_escape_text(hint)
                markup += f"\n<small>{desc_esc}</small>"
            else:
                held_esc = GLib.markup_escape_text(hint)
                markup += f"\n<small>{held_esc}</small>"
        elif constraint == CONSTRAINT_BLOCKED:
            hint_esc = GLib.markup_escape_text(
                _("Blocked by held package or dependency constraints")
            )
            markup += f"\n<small>{hint_esc}</small>"
        else:
            if show_desc:
                desc_esc = GLib.markup_escape_text(
                    description or _("System package"))
                markup += f"\n<small>{desc_esc}</small>"
        return markup

    def _populate_store(self, updates: List[UpdateItem]) -> None:
        self.store.freeze_notify()
        try:
            self.store.clear()
            show_desc = self.prefs.get("show_descriptions", True)
            for update in updates:
                constraint = getattr(update, "constraint", CONSTRAINT_NORMAL)
                icon = self.backend_service.get_row_icon(
                    update.category,
                    update.backend,
                    constraint,
                )
                pkg_markup = self._build_pkg_markup(update.name,
                                                    update.description,
                                                    show_desc, constraint)
                size_str = (
                    _("N/A")
                    if update.size == 0 and update.backend != "apt"
                    else format_size(update.size)
                )
                filter_group = self.backend_service.get_row_filter_group(update.backend)
                self.store.append([
                    False,                       # Col.SELECTED
                    pkg_markup,                  # Col.PACKAGE
                    update.installed_version,    # Col.INSTALLED
                    update.candidate_version,    # Col.NEW
                    size_str,                    # Col.SIZE
                    update.origin,               # Col.REPO
                    update.name,                 # Col.RAW_NAME
                    update.category,             # Col.CATEGORY
                    filter_group,                # Col.FILTER_GROUP
                    update.backend,              # Col.BACKEND
                    icon,                        # Col.ICON
                    update.size,                 # Col.RAW_SIZE
                    update.description or _("System package"),  # Col.DESC
                    constraint,                  # Col.HELD
                ])
        finally:
            self.store.thaw_notify()

    def _selected_package_names(self) -> Dict[str, List[str]]:
        """Return a mapping of backend_id -> [list of selected raw package names]."""
        grouped: Dict[str, List[str]] = {}
        for row in self.filter_model:
            if row[Col.SELECTED]:
                b_id = row[Col.BACKEND]
                grouped.setdefault(b_id, []).append(row[Col.RAW_NAME])
        return grouped

    def _load_cached_updates_on_startup(self) -> None:
        """Background worker: read cached package data, then hand off to GTK."""
        result = self.backend_service.load_cached_updates()
        GLib.idle_add(
            self._finish_startup_load,
            result.updates,
            result.total_bytes,
            result.error_messages,
        )

    def _finish_startup_load(
        self,
        updates: List[UpdateItem],
        total_bytes: int,
        error_msgs: list[str],
    ) -> bool:
        """GTK-thread callback: populate the store after startup load completes."""
        self._set_updates_loading(False)

        if error_msgs and not updates:
            self._clear_store()
            self._set_status(_("Failed to read cached package information."))
            return False

        self._populate_store(updates)
        actionable = self.backend_service.count_actionable_updates(updates)
        self._update_count_status(actionable, total_bytes, cached=True)
        return False

    # ------------------------------------------------------------------ #
    # Install flow                                                         #
    # ------------------------------------------------------------------ #

    def _launch_install(self, argv: list[str], title: str) -> None:
        self.install_controller.launch_install(argv, title)

    def _launch_deb_install(self, deb_path: str) -> None:
        deb_name = os.path.basename(deb_path)

        try:
            self.install_controller.launch_deb_install(
                deb_path,
                _("Installing %(deb_name)s...") % {"deb_name": deb_name},
            )
        except (RuntimeError, ValueError, FileNotFoundError) as exc:
            self._set_install_busy(False)
            self.install_progress.set_fraction(0.0)
            self.install_progress.set_text(_("Failed"))
            self.install_phase_label.set_text(str(exc))
            self._set_status(_("Validation failed: %(exc)s") % {"exc": exc})

    def _finish_install_success(self) -> None:
        self.install_controller.finish_install_success()
        if reboot_required():
            self.reboot_info_bar.show()

    def _finish_install_failure(self, exit_code: int) -> None:
        self.install_controller.finish_install_failure(exit_code)

        def _terminal_text(self) -> str:
            """Return plain text from the VTE terminal, or '' on error.

            Must pass attributes=None to avoid a vte_terminal_get_text assertion
            failure caused by PyGObject supplying a non-null GArray pointer.
            """
            try:
                result = self.install_terminal.get_text(lambda *a: True, None)
                text = result[0] if isinstance(result, tuple) else result
                return text or ""
            except (AttributeError, TypeError, ValueError):
                # VTE API is loosely typed and sensitive to GArray pointers;
                # return empty string if the bridge or assertion fails.
                return ""

    def _on_reboot_bar_response(self, _bar: Gtk.InfoBar,
                                response_id: int) -> None:
        """Handle the Restart Now button in the reboot info bar."""
        if response_id != Gtk.ResponseType.ACCEPT:
            return

        privilege_tool = find_privilege_tool()
        if privilege_tool is None:
            self._set_status(_("No privilege tool found. Please reboot manually."))
            return

        from bodhi_update.install_commands import get_helper_path  # noqa: PLC0415
        try:
            subprocess.Popen(  # pylint: disable=consider-using-with
                [privilege_tool, get_helper_path(), "reboot"])
        except OSError as exc:
            self._set_status(_("Failed to initiate reboot: %(exc)s") % {"exc": exc})

    # ------------------------------------------------------------------ #
    # Signal handlers                                                      #
    # ------------------------------------------------------------------ #

    def on_install_terminal_contents_changed(self,
                                             _terminal: Vte.Terminal) -> None:
        """VTE contents-changed signal handler (reserved for future use)."""

    def on_toggle_selected(self, _renderer: Gtk.CellRendererToggle,
                           path: str) -> None:
        """Toggle the checkbox for a package row; skip held/blocked packages."""
        if self.refresh_in_progress or self.install_in_progress:
            return

        filter_iter = self.filter_model.get_iter(path)
        child_iter = self.filter_model.convert_iter_to_child_iter(filter_iter)

        if self.store[child_iter][Col.HELD] in (CONSTRAINT_HELD,
                                                     CONSTRAINT_BLOCKED):
            return

        current = self.store[child_iter][Col.SELECTED]
        self.store[child_iter][Col.SELECTED] = not current
        self._refresh_selection_status()

    def on_clear_selection(self, _button: Gtk.Button) -> None:
        """Uncheck all rows in the store."""
        if self.refresh_in_progress or self.install_in_progress:
            return
        for row in self.store:
            row[Col.SELECTED] = False
        self._refresh_selection_status()

    def on_select_all(self, _button: Gtk.Button) -> None:
        """Check all rows currently visible through the active category filter."""
        if self.refresh_in_progress or self.install_in_progress:
            return

        # Snapshot paths before modifying the store; skip held/blocked rows.
        paths = [row.path for row in self.filter_model]
        for path in paths:
            f_iter = self.filter_model.get_iter(path)
            c_iter = self.filter_model.convert_iter_to_child_iter(f_iter)
            if self.store[c_iter][Col.HELD] == CONSTRAINT_NORMAL:
                self.store[c_iter][Col.SELECTED] = True

        self._refresh_selection_status()

    def on_category_changed(self, _combo: Gtk.ComboBoxText) -> None:
        """Refilter the update list when the category combo selection changes."""
        if self.refresh_in_progress or self.install_in_progress:
            return
        self.filter_model.refilter()

    def on_toggle_descriptions(self, checkmenuitem: Gtk.CheckMenuItem) -> None:
        """View-menu handler for Show Descriptions. Re-entry guarded by _syncing_desc."""
        # _syncing_desc is set by _set_show_descriptions when it syncs the menu item.
        if self._syncing_desc:
            return
        self._set_show_descriptions(checkmenuitem.get_active())

    def on_check_updates(self, _button: Gtk.Button | None) -> None:
        """Trigger a privileged refresh and reload the update list."""
        if self.refresh_in_progress or self.install_in_progress:
            return

        message = self.backend_service.check_any_backend_busy()
        if message:
            self._set_status(message)
            return

        self.refresh_controller.start_refresh()

    def on_install_selected(self,
                            _button: Gtk.Button | Gtk.MenuItem | None) -> None:
        """Install all checked packages using the appropriate backend."""
        if self.refresh_in_progress or self.install_in_progress:
            return

        grouped_packages = self._selected_package_names()
        if not any(pkgs for pkgs in grouped_packages.values()):
            self._set_status(_("No packages selected."))
            GLib.timeout_add_seconds(3, self._restore_current_update_status)
            return

        try:
            argv = self.backend_service.build_install_target_command(grouped_packages)
        except RuntimeError as exc:
            self._set_status(str(exc))
            return

        self._launch_install(argv, _("Installing selected updates..."))

    def on_toggle_details(self, button: Gtk.ToggleButton) -> None:
        """Reveal or hide the VTE terminal details pane."""
        revealed = button.get_active()
        self.install_details_revealer.set_reveal_child(revealed)
        button.set_label(_("Hide Details") if revealed else _("Show Details"))

    def on_back_to_updates(self, _button: Gtk.Button) -> None:
        """Switch back to the update list view and reload from cache."""
        if self.install_in_progress:
            return

        # Clear selection so no stale checkboxes are visible during reload.
        for row in self.store:
            row[Col.SELECTED] = False

        self.stack.set_visible_child_name("updates")
        self._update_action_sensitivity()
        self._set_updates_loading(True)
        # Use the cached (non-privileged) path — on_check_updates would prompt
        # for pkexec auth, which is wrong after a simple back-navigation.
        threading.Thread(target=self._load_cached_updates_on_startup,
                         daemon=True).start()

    def on_install_child_exited(self, _terminal: Vte.Terminal, status: int) -> None:
        """VTE child-exited signal: route to success or failure finish handler."""

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
        """Initialise the Gtk.Application with HANDLES_COMMAND_LINE flag."""
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
        """Create window (normal mode) or tray only (--tray mode).

        In tray mode the window is deferred to get_or_create_window() so GTK
        doesn't implicitly show it at startup.  On re-activation (second launch
        while already running) the window is always raised.
        """
        if self._window is None:
            if self._tray_mode:
                from bodhi_update.tray import TrayIcon  # noqa: PLC0415
                self._tray = TrayIcon(self)
                self.hold()  # prevent GLib loop from exiting with no windows
                self._held_for_tray = True
                return

            self._window = self.get_or_create_window()
            self._window.show_all()
            return

        # Already running — raise the existing window.
        win = self.get_or_create_window()
        win.show_all()
        win.present()

    def get_or_create_window(self) -> "UpdateManagerWindow":
        """Return the existing window, creating and wiring it up if needed."""
        if self._window is None:
            self._window = UpdateManagerWindow(deb_path=self._deb_path)
            self._window.set_application(self)
            # Intercept delete-event: hide instead of destroy while tray is active.
            self._window.connect("delete-event", self._on_window_delete)
        return self._window

    # ------------------------------------------------------------------
    # Public tray helpers (called by TrayIcon to avoid protected-access)
    # ------------------------------------------------------------------

    def set_tray_count(self, count: int, severity: str = "medium") -> None:
        """Forward an update count to the tray badge (no-op if no tray)."""
        if self._tray is not None:
            self._tray.set_update_count(count, severity)

    def quit_from_tray(self) -> None:
        """Quit the application, releasing hold() if in tray-only mode."""
        if self._held_for_tray:
            self._held_for_tray = False
            self.release()
        self.quit()

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
    """Entry point: parse argv, create the application, and run."""
    import sys  # noqa: PLC0415

    # --tray is consumed by do_command_line(); only look for a .deb path here.
    deb_path: str | None = None
    for arg in sys.argv[1:]:
        if arg.lower().endswith(".deb"):
            deb_path = arg
            break

    app = UpdateManagerApplication(deb_path=deb_path)
    app.run(sys.argv)
