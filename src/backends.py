"""Backend registry and abstract base class for update discovery."""

import inspect
import logging
from abc import ABC, abstractmethod
from importlib.metadata import entry_points, import_module
from pathlib import Path
from typing import Dict, List, Tuple

from bodhi_update.models import UpdateItem

_log = logging.getLogger(__name__)


class UpdateBackend(ABC):
    """Interface for update discovery and installation backends."""
    
    # A unique string identifier for this backend, e.g., 'apt', 'python'.
    backend_id: str | None = None
    # A human-readable name for the backend, e.g., 'Debian/Ubuntu packages'.
    display_name: str | None = None

    def __init_subclass__(cls, **kwargs):
        """ require subclasses to define these class variables."""
        
        super().__init_subclass__(**kwargs)
        if cls is UpdateBackend:
            return
        if not isinstance(cls.backend_id, str) or not cls.backend_id:
            raise TypeError(f"{cls.__name__} must define a non-empty backend_id")
        if not isinstance(cls.display_name, str) or not cls.display_name:
            raise TypeError(f"{cls.__name__} must define a non-empty display_name")
    
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
    """Scan the built-in plugins package for concrete UpdateBackend classes."""
    plugins_dir = Path(__file__).parent / "plugins"
    if not plugins_dir.exists():
        _log.debug("Built-in plugins directory %r does not exist", plugins_dir)
        return []

    discovered: List[type[UpdateBackend]] = []
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


# ------------------------------------------------------------------ #
# Registry initialisation                                             #
# ------------------------------------------------------------------ #


def discover_entrypoint_plugins() -> List[type[UpdateBackend]]:
    """Return UpdateBackend classes exposed via package entry points."""
    discovered: List[type[UpdateBackend]] = []
    seen: set[type[UpdateBackend]] = set()

    for ep in entry_points(group="bodhi_update.backends"):
        try:
            obj = ep.load()
        except (ImportError, RuntimeError, SyntaxError, TypeError) as exc:
            _log.debug("Skipping entry point %r: %s", ep.name, exc)
            continue

        if not _is_valid_backend_class(obj, getattr(obj, "__module__", "")):
            _log.debug("Skipping entry point %r: not a valid backend class", ep.name)
            continue

        if obj in seen:
            continue

        seen.add(obj)
        discovered.append(obj)

    return discovered


def _iter_backend_classes() -> List[type[UpdateBackend]]:
    """Return all discovered backend classes from built-ins and entry points."""
    discovered: List[type[UpdateBackend]] = []
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
        if not isinstance(bid, str) or not bid:
            _log.warning(
                "Backend %r returned an invalid backend_id %r; skipping.",
                backend_cls.__name__,
                bid,
            )
            continue

        if reg.get_backend(bid) is not None:
            _log.warning(
                "Duplicate backend_id %r from %r; skipping.",
                bid,
                backend_cls.__name__,
            )
            continue

        reg.register(instance)
        _log.debug("Registered backend: %r", bid)

    if reg.get_backend("apt") is None:
        _log.warning(
            "APT backend wasn't registered. Package updates may be unavailable."
        )
