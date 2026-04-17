"""Backend-facing UI service for Bodhi Update Manager."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from bodhi_update.backends import get_registry, initialize_registry
from bodhi_update.models import CONSTRAINT_NORMAL, UpdateItem


@dataclass(frozen=True)
class BackendLoadResult:
    """Aggregated update-load result from all backends."""

    updates: list[UpdateItem]
    total_bytes: int
    error_messages: list[str]


class BackendUIService:
    """Facade between the GTK UI and the backend registry."""

    def __init__(self, prefs: dict) -> None:
        self._prefs = prefs

    def initialize(self) -> None:
        """Initialize backend discovery/registry."""
        initialize_registry()

    def is_backend_enabled(self, backend_id: str) -> bool:
        """Return True if a backend is enabled in preferences."""
        visibility = self._prefs.get("backend_visibility", {})
        if not isinstance(visibility, dict):
            return True
        return visibility.get(backend_id, True)

    def get_all_backends(self):
        """Return all registered backends."""
        return get_registry().get_all_backends()

    def get_available_backends(self):
        """Return only backends supported on this system."""
        return get_registry().get_available_backends()

    def get_backend(self, backend_id: str):
        """Return a backend by ID."""
        return get_registry().get_backend(backend_id)

    def get_preference_backends(self) -> list:
        """Return available backends that should appear in Preferences."""
        result = []

        for backend in self.get_available_backends():
            meta = getattr(backend, "meta", None)
            if meta is None:
                continue
            if not getattr(meta, "show_in_preferences", False):
                continue
            result.append(backend)

        return sorted(result, key=lambda b: b.display_name.lower())

    def get_visible_filter_groups(self) -> dict[str, tuple[str, int]]:
        """Return filter groups for available + enabled backends.

        Mapping:
            filter_group_key -> (filter_label, filter_sort_order)
        """
        groups: dict[str, tuple[str, int]] = {}

        for backend in self.get_available_backends():
            if not self.is_backend_enabled(backend.backend_id):
                continue

            group = backend.filter_group
            label = backend.filter_label

            if not group or not label:
                continue

            groups.setdefault(group, (label, backend.filter_sort_order))

        return groups

    def get_row_filter_group(self, backend_id: str) -> str:
        """Return the filter-group key for a backend, or empty string."""
        backend = self.get_backend(backend_id)
        if backend is None or backend.filter_group is None:
            return ""
        return backend.filter_group

    def load_cached_updates(self) -> BackendLoadResult:
        """Read cached update data from all backends."""
        updates: list[UpdateItem] = []
        total_bytes = 0
        error_messages: list[str] = []

        for backend in self.get_all_backends():
            try:
                backend_updates, backend_bytes = backend.get_updates()
                updates.extend(backend_updates)
                total_bytes += backend_bytes
            except (OSError, RuntimeError, ValueError) as exc:
                error_messages.append(f"{backend.display_name}: {exc}")

        return BackendLoadResult(
            updates=updates,
            total_bytes=total_bytes,
            error_messages=error_messages,
        )

    def count_actionable_updates(self, updates: list[UpdateItem]) -> int:
        """Return the number of actionable updates."""
        return sum(
            1
            for update in updates
            if getattr(update, "constraint", CONSTRAINT_NORMAL)
            == CONSTRAINT_NORMAL
        )

    def check_any_backend_busy(self) -> str | None:
        """Return a busy message if any backend is busy, else None."""
        for backend in self.get_all_backends():
            is_busy, message = backend.check_busy()
            if is_busy:
                return message
        return None

    def build_install_target_command(
        self,
        grouped_packages: Dict[str, List[str]] | None,
    ) -> list[str]:
        """Return install argv for the selected packages.

        Raises RuntimeError for multi-backend selections or unknown backend IDs.
        """
        if not grouped_packages:
            apt_backend = self.get_backend("apt")
            if apt_backend:
                return apt_backend.build_install_command(None)
            raise RuntimeError("Primary backend (APT) is not configured.")

        if len(grouped_packages) > 1:
            raise RuntimeError(
                "Installing from multiple package sources simultaneously is "
                "not yet supported. Please select packages from one source "
                "type only."
            )

        backend_id = next(iter(grouped_packages.keys()))
        target_packages = grouped_packages[backend_id]

        backend = self.get_backend(backend_id)
        if not backend:
            raise RuntimeError(
                f"Requested installation for unknown backend: {backend_id}"
            )

        return backend.build_install_command(target_packages)


    def get_row_icon(
        self,
        category: str,
        backend_id: str,
        constraint: str,
    ) -> str:
        """Return GTK icon-name for a row.

        Priority:
          1. Constraint state (held / blocked)
          2. Core categories (security / kernel)
          3. Backend-provided icon (metadata)
          4. Generic fallback
        """

        # --- Constraint state (highest priority) ---
        if constraint == "held":
            return "changes-prevent-symbolic"

        if constraint == "blocked_by_hold":
            return "dialog-warning-symbolic"

        # --- Core categories ---
        if category == "security":
            return "security-high-symbolic"

        if category == "kernel":
            return "applications-system-symbolic"

        # --- Backend-provided icon ---
        backend = self.get_backend(backend_id)
        if backend is not None:
            icon_name = getattr(getattr(backend, "meta", None), "icon_name", None)
            if icon_name:
                return icon_name

        # --- Fallback ---
        return "system-software-update-symbolic"
