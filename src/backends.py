"""Backend registry and abstract base class for update discovery."""

from __future__ import annotations

import inspect
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path
from typing import Dict, List

from bodhi_update.models import UpdateItem

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackendMeta:
    """Static metadata describing an update backend."""

    backend_id: str
    display_name: str
    filter_group: str | None = None
    filter_label: str | None = None
    filter_sort_order: int = 100
    show_in_preferences: bool = False
    icon_name: str | None = None


class UpdateBackend(ABC):
    """Interface for update discovery and installation backends."""

    meta: BackendMeta | None = None

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)

        if cls is UpdateBackend:
            return

        if inspect.isabstract(cls):
            return

        meta = getattr(cls, "meta", None)
        if not isinstance(meta, BackendMeta):
            raise TypeError(
                f"{cls.__name__} must define meta as a BackendMeta instance"
            )

        if not isinstance(meta.backend_id, str) or not meta.backend_id:
            raise TypeError(
                f"{cls.__name__} must define a non-empty meta.backend_id"
            )

        if not isinstance(meta.display_name, str) or not meta.display_name:
            raise TypeError(
                f"{cls.__name__} must define a non-empty meta.display_name"
            )

        if meta.filter_group is None and meta.filter_label is not None:
            raise TypeError(
                f"{cls.__name__} defines filter_label without filter_group"
            )

        if meta.filter_group is not None:
            if not isinstance(meta.filter_group, str) or not meta.filter_group:
                raise TypeError(
                    f"{cls.__name__} must define a non-empty meta.filter_group"
                )
            if not isinstance(meta.filter_label, str) or not meta.filter_label:
                raise TypeError(
                    f"{cls.__name__} must define meta.filter_label when "
                    "meta.filter_group is set"
                )

    @property
    def backend_id(self) -> str:
        """Return the stable backend identifier."""
        assert self.meta is not None
        return self.meta.backend_id

    @property
    def display_name(self) -> str:
        """Return the human-readable backend name."""
        assert self.meta is not None
        return self.meta.display_name

    @property
    def filter_group(self) -> str | None:
        """Return the optional UI filter-group key."""
        assert self.meta is not None
        return self.meta.filter_group

    @property
    def filter_label(self) -> str | None:
        """Return the optional UI filter-group label."""
        assert self.meta is not None
        return self.meta.filter_label

    @property
    def filter_sort_order(self) -> int:
        """Return the sort order for an optional UI filter group."""
        assert self.meta is not None
        return self.meta.filter_sort_order

    def is_available(self) -> bool:
        """Return True if this backend is supported on the current system."""
        return False

    def check_busy(self) -> tuple[bool, str]:
        """Check if the package manager is currently locked or running.

        Return (True, reason) if busy, otherwise (False, "").
        """
        return False, ""

    # pylint: disable=unused-argument
    def refresh(self, sentinel_path: str | None = None) -> tuple[bool, str]:
        """Refresh the local list of available updates.

        Return (True, "") on success, or (False, error_message).
        The optional *sentinel_path* is forwarded to backends that support
        the pkexec auth-success handshake (currently APT only).
        """
        return True, ""

    def get_updates(self) -> tuple[list[UpdateItem], int]:
        """Read the local cache and return available updates.

        Return (updates_list, total_download_bytes).
        """
        return [], 0

    @abstractmethod
    def build_install_command(
        self,
        packages: list[str] | None = None,
    ) -> list[str]:
        """Return an argv list required to install the given packages.

        If packages is None or empty, return the argv to upgrade all
        available packages. The list is passed directly to VTE
        spawn_async — no shell layer is used.
        """


class BackendRegistry:
    """Singleton registry holding all instantiated update backends."""

    def __init__(self) -> None:
        self._backends: Dict[str, UpdateBackend] = {}

    def register(self, backend: UpdateBackend) -> None:
        """Register a backend instance."""
        bid = backend.backend_id

        if bid in self._backends:
            _log.warning("Duplicate backend_id %r, skipping", bid)
            return

        self._backends[bid] = backend
        _log.debug("Registered backend: %s", bid)

    def get_backend(self, backend_id: str) -> UpdateBackend | None:
        """Return a registered backend by ID, or None if not found."""
        return self._backends.get(backend_id)

    def get_all_backends(self) -> List[UpdateBackend]:
        """Return all registered backends."""
        return list(self._backends.values())

    def get_available_backends(self) -> List[UpdateBackend]:
        """Return only backends supported on this system."""
        return [b for b in self._backends.values() if b.is_available()]

    def get_filter_groups(self) -> dict[str, tuple[str, int]]:
        """Return backend-declared UI filter groups.

        Mapping:
            filter_group_key -> (filter_label, filter_sort_order)
        """
        groups: dict[str, tuple[str, int]] = {}

        for backend in self._backends.values():
            group = backend.filter_group
            label = backend.filter_label

            if group is None or label is None:
                continue

            groups.setdefault(group, (label, backend.filter_sort_order))

        return groups

    def is_initialized(self) -> bool:
        """Return True if the registry has been initialized."""
        return bool(self._backends)


_REGISTRY = BackendRegistry()


def get_registry() -> BackendRegistry:
    """Return the module-level backend registry singleton."""
    return _REGISTRY


# ------------------------------------------------------------------ #
# Plugin discovery                                                    #
# ------------------------------------------------------------------ #


def _is_valid_backend_class(
    obj: object,
    module_name: str,
) -> bool:
    """Return True if *obj* is a concrete UpdateBackend subclass.

    Criteria:
    - must be a class (not an instance or module)
    - must be defined in the plugin module, not just imported into it
    - must subclass UpdateBackend without being UpdateBackend itself
    - must not be abstract
    """
    if not inspect.isclass(obj):
        return False
    if getattr(obj, "__module__", None) != module_name:
        return False
    if not issubclass(obj, UpdateBackend):
        return False
    if obj is UpdateBackend:
        return False
    if inspect.isabstract(obj):
        return False
    return True


def _is_valid_backend_class_any_module(obj: object) -> bool:
    """Return True if *obj* is a concrete UpdateBackend subclass."""
    if not inspect.isclass(obj):
        return False
    if not issubclass(obj, UpdateBackend):
        return False
    if obj is UpdateBackend:
        return False
    if inspect.isabstract(obj):
        return False
    return True


def discover_plugins() -> list[type[UpdateBackend]]:
    """Scan the built-in plugins package for concrete UpdateBackend classes."""
    plugins_dir = Path(__file__).parent / "plugins"
    if not plugins_dir.exists():
        _log.debug("Built-in plugins directory %r does not exist", plugins_dir)
        return []

    discovered: list[type[UpdateBackend]] = []
    seen: set[type[UpdateBackend]] = set()

    for path in sorted(plugins_dir.glob("*.py")):
        stem = path.stem
        if stem.startswith("_"):
            continue

        module_name = f"bodhi_update.plugins.{stem}"

        try:
            module = import_module(module_name)
        except (ImportError, RuntimeError, SyntaxError, TypeError) as exc:
            _log.debug("Skipping plugin module %r: %s", module_name, exc)
            continue

        for _, obj in inspect.getmembers(module, inspect.isclass):
            if not _is_valid_backend_class(obj, module_name):
                continue
            if obj in seen:
                continue
            seen.add(obj)
            discovered.append(obj)

    return discovered


def discover_entrypoint_plugins() -> list[type[UpdateBackend]]:
    """Return UpdateBackend classes exposed via package entry points."""
    discovered: list[type[UpdateBackend]] = []
    seen: set[type[UpdateBackend]] = set()

    for ep in entry_points(group="bodhi_update.backends"):
        try:
            obj = ep.load()
        except (ImportError, RuntimeError, SyntaxError, TypeError) as exc:
            _log.debug("Skipping entry point %r: %s", ep.name, exc)
            continue

        if not _is_valid_backend_class_any_module(obj):
            _log.debug(
                "Skipping entry point %r: not a valid backend class",
                ep.name,
            )
            continue

        if obj in seen:
            continue

        seen.add(obj)
        discovered.append(obj)

    return discovered


def _iter_backend_classes() -> list[type[UpdateBackend]]:
    """Return all discovered backend classes from built-ins and entry points."""
    discovered: list[type[UpdateBackend]] = []
    seen: set[type[UpdateBackend]] = set()

    for backend_cls in discover_plugins():
        if backend_cls in seen:
            continue
        seen.add(backend_cls)
        discovered.append(backend_cls)

    for backend_cls in discover_entrypoint_plugins():
        if backend_cls in seen:
            continue
        seen.add(backend_cls)
        discovered.append(backend_cls)

    return discovered


# ------------------------------------------------------------------ #
# Registry initialisation                                             #
# ------------------------------------------------------------------ #


def initialize_registry() -> None:
    """Discover and register all available backend plugins. Idempotent."""
    reg = get_registry()
    if reg.is_initialized():
        return

    for backend_cls in _iter_backend_classes():
        try:
            instance = backend_cls()
        except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
            _log.warning(
                "Failed to instantiate backend %r: %s",
                backend_cls.__name__,
                exc,
            )
            continue

        bid = instance.backend_id
        if reg.get_backend(bid) is not None:
            _log.warning(
                "Duplicate backend_id %r from %r; skipping.",
                bid,
                backend_cls.__name__,
            )
            continue

        reg.register(instance)

    if reg.get_backend("apt") is None:
        _log.warning(
            "APT backend wasn't registered. Package updates may be unavailable."
        )
