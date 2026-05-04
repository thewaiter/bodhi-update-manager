"""Small utility helpers for display, system state checks, and severity classification."""

from __future__ import annotations

import logging
import os
import shutil

APP_NAME = "bodhi-update-manager"
log = logging.getLogger(APP_NAME)

_SYSTEM_PREFIX = f"/usr/lib/"
REBOOT_REQUIRED_PATH = "/var/run/reboot-required"

# Privilege tools tried in preference order.
_PRIVILEGE_TOOLS = ("pkexec", "sudo", "doas")


def format_size(num_bytes: int) -> str:
    """Convert a size in bytes into a human-readable string (e.g. 23.5 MB)."""
    size = float(num_bytes)

    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024

    return f"{size:.1f} TB"  # unreachable, but satisfies the type checker


def reboot_required() -> bool:
    """Return True if the system has flagged that a restart is needed."""
    return os.path.exists(REBOOT_REQUIRED_PATH)


def find_privilege_tool() -> str | None:
    """Return the first available privilege-escalation binary."""

    sys_installed = os.path.abspath(__file__).startswith(_SYSTEM_PREFIX)
    for tool in _PRIVILEGE_TOOLS:
        # If running locally in a terminal do not use pkexec
        if tool == "pkexec" and not sys_installed:
            continue
        if shutil.which(tool):
            log.debug("Privilege Tool: %s, %d", tool, sys_installed)
            return tool

    log.error("No Privilege Tool found.")
    return None


# Keep this list small: core platform plumbing only.
_MEDIUM_PREFIXES = (
    "linux-",
    "systemd",
    "libc",
    "glibc",
    "dbus",
    "openssl",
    "gnupg",
    "apt",
    "dpkg",
    "bash",
    "coreutils",
    "util-linux",
    "sudo",
    "moksha",
    "bodhi-",
)


def get_pkg_severity(name: str, category: str, backend: str) -> str:
    """Return high, medium, or low severity for an update item."""
    if category in ("security", "kernel"):
        return "high"
    if backend == "apt" and name.startswith(_MEDIUM_PREFIXES):
        return "medium"
    return "low"
