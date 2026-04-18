"""Preferences storage for Bodhi Update Manager."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_PREFS: dict[str, Any] = {
    "show_descriptions": True,
    "show_notifications": True,
    "show_held_packages": False,
    "show_snap": True,
    "show_flatpak": True,
    "backend_visibility": {},
}


class PreferencesStore:
    """Load and save simple JSON preferences."""

    def __init__(
        self,
        app_name: str = "bodhi-update-manager",
        defaults: dict[str, Any] | None = None,
    ) -> None:
        self.app_name = app_name
        self.defaults = dict(defaults or DEFAULT_PREFS)

    def get_path(self) -> str:
        """Return the JSON preferences file path."""
        config_home = os.environ.get(
            "XDG_CONFIG_HOME",
            os.path.expanduser("~/.config"),
        )
        return os.path.join(config_home, self.app_name, "prefs.json")

    def load(self) -> dict[str, Any]:
        """Load preferences from disk, falling back to defaults."""
        prefs = dict(self.defaults)
        path = self.get_path()

        if not os.path.exists(path):
            return prefs

        try:
            with open(path, "r", encoding="utf-8") as handle:
                data: Any = json.load(handle)
        except OSError as exc:
            logger.warning("Could not read prefs file at %s: %s", path, exc)
            return prefs
        except json.JSONDecodeError as exc:
            logger.error("Prefs file is corrupted JSON at %s: %s", path, exc)
            return prefs

        if not isinstance(data, dict):
            logger.error(
                "Prefs file expected a dict but got %s",
                type(data).__name__,
            )
            return prefs

        for key, value in data.items():
            if key == "backend_visibility" and isinstance(value, dict):
                prefs[key] = value
            elif key in prefs and isinstance(value, bool):
                prefs[key] = value

        return prefs

    def save(self, prefs: dict[str, Any]) -> bool:
        """Persist preferences to disk. Return True on success."""
        path = self.get_path()

        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(prefs, handle)
        except (OSError, TypeError) as exc:
            logger.error("Could not save prefs file at %s: %s", path, exc)
            return False

        return True
