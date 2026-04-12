"""Installation command building for the Bodhi Update Manager.

All public 'build_*_argv' functions return a plain 'list[str]' suitable
for direct use with 'subprocess.run' or 'Vte.Terminal.spawn_async'.
No shell is involved between the GUI and pkexec: the privilege path is
strictly:  GUI → pkexec → /usr/libexec/bodhi-update-manager-root → apt-get
"""

from __future__ import annotations

import os

from bodhi_update.utils import find_privilege_tool

# ---------------------------------------------------------------------------
# Installed-helper path resolution
# ---------------------------------------------------------------------------

# Production path registered in the polkit policy file.
_INSTALLED_HELPER = "/usr/libexec/bodhi-update-manager-root"

# Development fallback: the source-tree helper at data/libexec/bodhi-update-manager-root,
# reached by walking two levels up from src/bodhi_update/ to the repo root.
_DEV_HELPER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),  # src/bodhi_update/
    "..",
    "..",  # → repo root
    "data",
    "libexec",
    "bodhi-update-manager-root",
)


def get_helper_path() -> str:
    """Return the absolute path to the root helper that pkexec will invoke.

    Resolution order:
    1. 'BODHI_HELPER_PATH' env var — lets packagers or CI override the path.
    2. The installed binary at '/usr/libexec/bodhi-update-manager-root'.
    3. 'data/libexec/bodhi-update-manager-root' in the source tree
       (development / uninstalled mode).
    """
    override = os.environ.get("BODHI_HELPER_PATH")
    if override:
        return override
    if os.path.isfile(_INSTALLED_HELPER):
        return _INSTALLED_HELPER
    return _DEV_HELPER


def _privilege_tool() -> str:
    """Return the privilege-escalation tool, raising RuntimeError if absent."""
    tool = find_privilege_tool()
    if tool is None:
        raise RuntimeError("No privilege tool found (pkexec / sudo / doas).")
    return tool


# ---------------------------------------------------------------------------
# APT argv builders  (no shell — direct exec)
# ---------------------------------------------------------------------------


def build_upgrade_argv(packages: list[str] | None = None) -> list[str]:
    """Return the argv for an APT upgrade (or targeted install) via the helper.

    The returned list is ready for 'Vte.Terminal.spawn_async' or
    'subprocess.run' — no shell quoting or wrapping is needed or used.

    Args:
        packages: Optional explicit package list.  When *None* or empty the
                  helper performs a full 'apt-get full-upgrade'.
    """
    tool = _privilege_tool()
    helper = get_helper_path()

    if packages:
        return [tool, helper, "install", *packages]
    return [tool, helper, "upgrade"]


def build_deb_install_argv(deb_path: str) -> list[str]:
    """Return the argv for installing a local .deb file via the helper.

    Validates the path client-side before building the argv so callers
    receive a meaningful error before any privilege escalation occurs.

    Raises:
        ValueError: *deb_path* does not end with '.deb'.
        FileNotFoundError: *deb_path* does not exist or is not a regular file.
        RuntimeError: no privilege tool is available.
    """
    norm_path = os.path.abspath(os.path.expanduser(deb_path))

    if not norm_path.lower().endswith(".deb"):
        raise ValueError(f"Not a .deb file: {deb_path}")

    if not os.path.isfile(norm_path):
        raise FileNotFoundError(
            f"File not found or not a regular file: {deb_path}")

    tool = _privilege_tool()
    helper = get_helper_path()
    return [tool, helper, "install-deb", norm_path]


def build_hold_argv(package: str,
                    *,
                    hold: bool,
                    sentinel_path: str | None = None) -> list[str]:
    """Return the argv for hold or unhold of a single APT package via the helper.

    Args:
        package: Debian package name to act on.
        hold: True to place the package on hold; False to remove the hold.
        sentinel_path: Optional path for the auth-success sentinel file.
            When provided, '--sentinel <path>' is inserted after the helper
            path so the root helper can signal auth success to the GUI.
    """
    tool = _privilege_tool()
    helper = get_helper_path()
    action = "hold" if hold else "unhold"
    if sentinel_path:
        return [tool, helper, "--sentinel", sentinel_path, action, package]
    return [tool, helper, action, package]
