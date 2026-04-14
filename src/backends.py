"""Backend registry and abstract base class for update discovery."""

import importlib
import inspect
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Tuple

from bodhi_update.models import UpdateItem

_log = logging.getLogger(__name__)


class UpdateBackend(ABC):
    """Interface for update discovery and installation backends."""

    @property
    @abstractmethod
    def backend_id(self) -> str:
        """A unique string identifier for this backend, e.g., 'apt', 'python'."""

    @property
    @abstractmethod
    def display_name(self) -> str:
        """A human-readable name for the backend, e.g., 'Debian/Ubuntu packages'."""

    def is_available(self) -> bool:
        """Return True if this backend is supported on the current system."""
        return False

    def check_busy(self) -> Tuple[bool, str]:
        """Check if the package manager is currently locked or running.

        Return (True, reason) if busy, otherwise (False, "").
        """
        return False, ""

    def refresh(self, sentinel_path: str | None = None) -> Tuple[bool, str]:
        """Refresh the local list of available updates.

        Return (True, "") on success, or (False, error_message).
        The optional *sentinel_path* is forwarded to backends that support
        the pkexec auth-success handshake (currently APT only).
        """
        return True, ""

    def get_updates(self) -> Tuple[List[UpdateItem], int]:
        """Read the local cache and return available updates.

        Return (updates_list, total_download_bytes).
        """
        return [], 0

    @abstractmethod
    def build_install_command(self,
                              packages: List[str] | None = None) -> list[str]:
        """Return an argv list required to install the given packages.

        If packages is None or empty, return the argv to upgrade all available
        packages.  The list is passed directly to VTE spawn_async — no shell
        layer is used.
        """


class BackendRegistry:
    """Singleton registry holding all instantiated update backends."""

    def __init__(self) -> None:
        self._backends: Dict[str, UpdateBackend] = {}

    def register(self, backend: UpdateBackend) -> None:
        """Register a backend instance."""
        bid = backend.backend_id

        if not isinstance(bid, str) or not bid:
            _log.warning("Skipping backend with invalid backend_id: %r", bid)
            return

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

    def is_initialized(self) -> bool:
        """Return True if the registry has been initialized."""
        return bool(self._backends)


_REGISTRY = BackendRegistry()


def get_registry() -> BackendRegistry:
    """Return the module-level backend registry singleton."""
    return _REGISTRY


# ------------------------------------------------------------------ #
# Plugin discovery                                                     #
# ------------------------------------------------------------------ #


def _is_valid_backend_class(obj: object, module_name: str) -> bool:
    """Return True if *obj* is a concrete UpdateBackend subclass defined in *module_name*.

    Criteria:
    - must be a class (not an instance or module)
    - must be defined in the plugin module, not just imported into it
    - must subclass UpdateBackend without being UpdateBackend itself
    - must not be abstract

    Note: backend_id validity is checked by initialize_registry() after
    instantiation, not here.
    """
    if not inspect.isclass(obj):
        return False
    # Ignore classes that are only imported into the module (e.g. UpdateBackend itself).
    if getattr(obj, "__module__", None) != module_name:
        return False
    if not issubclass(obj, UpdateBackend):
        return False
    if obj is UpdateBackend:
        return False
    # Skip any class that is still abstract.
    if inspect.isabstract(obj):
        return False
    return True


def discover_plugins() -> List[type[UpdateBackend]]:
    """Scan the plugins package and return concrete UpdateBackend subclasses."""
    # Sorted for deterministic loading order across environments.
    plugins_dir = Path(__file__).parent / "plugins"
    if not plugins_dir.exists():
        return []
    discovered: List[type[UpdateBackend]] = []
    seen: set[type] = set()

    # Sort for deterministic loading order.
    plugin_files = sorted(plugins_dir.glob("*.py"))

    for path in plugin_files:
        stem = path.stem
        if stem.startswith("_"):
            # Skip __init__.py and any private helpers.
            continue

        module_name = f"bodhi_update.plugins.{stem}"
        try:
            module = importlib.import_module(module_name)
        except (ImportError, RuntimeError, SyntaxError, TypeError) as exc:
            # Missing deps, syntax errors, or module-level init failure.
            _log.debug("Skipping plugin %r: %s", module_name, exc)
            continue

        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if not _is_valid_backend_class(obj, module_name):
                continue
            if obj in seen:
                continue
            seen.add(obj)
            discovered.append(obj)

    return discovered


# ------------------------------------------------------------------ #
# Registry initialisation                                             #
# ------------------------------------------------------------------ #


def initialize_registry() -> None:
    """Discover and register all available backend plugins. Idempotent."""
    # Each backend is instantiated inside try/except so one broken plugin
    # can't block the rest.
    reg = get_registry()
    if reg.is_initialized():
        return

    for backend_cls in discover_plugins():
        try:
            instance = backend_cls()
            bid = instance.backend_id
            if not isinstance(bid, str) or not bid:
                _log.warning(
                    "Backend %r returned an invalid backend_id %r; skipping.",
                    backend_cls.__name__,
                    bid,
                )
                continue
            reg.register(instance)
            _log.debug("Registered backend: %r", bid)
        except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
            # Catches missing backend_id, bad constructor args, or init failures.
            _log.warning(
                "Failed to instantiate backend %r: %s",
                backend_cls.__name__,
                exc,
            )

    if reg.get_backend("apt") is None:
        _log.warning("APT backend was not registered. "
                     "Package updates may be unavailable.")
