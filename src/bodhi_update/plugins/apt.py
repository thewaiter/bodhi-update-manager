"""APT-backed update discovery and package-list refresh."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import List, Tuple

import apt

from bodhi_update.backends import UpdateBackend
from bodhi_update.install_commands import build_upgrade_argv, get_helper_path
from bodhi_update.models import (
    CONSTRAINT_BLOCKED,
    CONSTRAINT_HELD,
    CONSTRAINT_NORMAL,
    UpdateItem,
)
from bodhi_update.utils import find_privilege_tool

# APT/dpkg lock files whose open FileDescriptions indicate a busy package system.
_LOCK_PATHS = (
    Path("/var/lib/dpkg/lock"),
    Path("/var/lib/dpkg/lock-frontend"),
    Path("/var/lib/apt/lists/lock"),
    Path("/var/cache/apt/archives/lock"),
)

# Package-manager process names and cmdline fragments to detect.
# Matching against cmdline as well as comm catches helpers (e.g.
# apt.systemd.daily) that appear as "python3" in /proc/<pid>/comm.
_APT_PROCESS_KEYWORDS = frozenset((
    "apt",
    "apt-get",
    "dpkg",
    "aptitude",
    "unattended-upgrade",
    "apt.systemd.daily",
    "synaptic",
    "software-properties",
))

_LOCK_STDERR_HINTS = (
    "could not get lock",
    "unable to acquire the dpkg frontend lock",
    "unable to lock directory",
    "is another process using it?",
)

# Network-connectivity and partial-refresh failure fingerprints.
_NETWORK_ERROR_HINTS = (
    "could not resolve",
    "unable to connect",
    "network is unreachable",
    "temporary failure in name resolution",
    "failed to fetch",
    "connection timed out",
    "some index files failed to download",
    "they have been ignored, or old ones used instead",
)

# ------------------------------------------------------------------ #
# Internal /proc helpers                                               #
# ------------------------------------------------------------------ #


def _proc_comm(pid: str) -> str:
    """Return the comm (process name) for *pid*, stripped.  '' on error."""
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _proc_cmdline(pid: str) -> str:
    """Return the NUL-separated cmdline as a space-joined string.  '' on error."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read()
        return raw.replace(b"\x00", b" ").decode("utf-8",
                                                 errors="replace").strip()
    except OSError:
        return ""


def _matches_apt_keyword(comm: str, cmdline: str) -> bool:
    """Return True if comm or a cmdline token exactly matches a PM keyword."""
    if comm in _APT_PROCESS_KEYWORDS:
        return True

    for token in cmdline.split():
        if os.path.basename(token) in _APT_PROCESS_KEYWORDS:
            return True

    return False


# ------------------------------------------------------------------ #
# Package helpers                                                      #
# ------------------------------------------------------------------ #


def _get_origin_name(pkg: apt.package.Package) -> str:
    """Return a compact archive/origin label for a candidate package."""
    if pkg.candidate and pkg.candidate.origins:
        for origin in pkg.candidate.origins:
            for attr in ("archive", "origin", "label", "site", "component"):
                value = getattr(origin, attr, "")
                if value:
                    return value
    return "unknown"


def _is_security_update(origin: str) -> bool:
    """Return True if the origin label looks like a security channel."""
    return "security" in origin.lower()


def _is_kernel_update(pkg_name: str) -> bool:
    """Return True if the package is a common kernel or kernel-module package."""
    return pkg_name.startswith(
        ("linux-image", "linux-headers", "linux-modules"))


def _determine_category(pkg_name: str, origin: str) -> str:
    """Return 'security', 'kernel', or 'system' according to priority."""
    if _is_security_update(origin):
        return "security"
    if _is_kernel_update(pkg_name):
        return "kernel"
    return "system"


def _sort_key(item: UpdateItem) -> tuple[int, str]:
    """Sort security updates first, then alphabetically by name."""
    return (0 if _is_security_update(item.origin) else 1, item.name.lower())


# ------------------------------------------------------------------ #
# APT constraint helpers                                               #
# ------------------------------------------------------------------ #


def _get_held_packages() -> set[str]:
    """Return the set of package names pinned via ``apt-mark hold``.

    Runs ``apt-mark showhold`` without a shell.  Returns an empty set
    on any error so callers can proceed gracefully.
    """
    try:
        result = subprocess.run(
            ["apt-mark", "showhold"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
        return set(result.stdout.split())
    except Exception:  # pylint: disable=broad-except
        return set()


def _get_kept_back_packages() -> set[str]:
    """Return packages that APT would keep back during a full upgrade.

    Runs ``apt-get --simulate full-upgrade`` (no root required for simulation)
    and parses the "The following packages have been kept back:" stanza.
    Handles multi-line package lists and multiarch names (e.g. ``libc6:i386``).
    Returns an empty set on any error.

    ``full-upgrade`` is used intentionally — it matches the command the root
    helper runs at install time.  Using plain ``upgrade`` would misclassify
    packages that need a full-upgrade step (e.g. Wine after unholding
    winehq-staging) as permanently blocked even when no hold is active.
    """
    try:
        result = subprocess.run(
            ["apt-get", "--simulate", "full-upgrade"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception:  # pylint: disable=broad-except
        return set()

    kept_back: set[str] = set()
    in_kept_section = False

    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped == "The following packages have been kept back:":
            in_kept_section = True
            continue
        if in_kept_section:
            # A blank line or a new capitalised section header ends the stanza.
            if not stripped or stripped[0].isupper():
                break
            kept_back.update(stripped.split())

    return kept_back


def _apt_cache_depends(held_pkg: str) -> set[str]:
    """Return the set of package names that *held_pkg* depends on.

    Uses ``apt-cache depends`` without a shell.  Returns an empty set on any
    error.  Results are intentionally *not* cached here — the caller is
    responsible for caching if repeated calls are expected.
    """
    try:
        result = subprocess.run(
            ["apt-cache", "depends", held_pkg],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:  # pylint: disable=broad-except
        return set()

    deps: set[str] = set()
    for line in result.stdout.splitlines():
        # Lines look like:  "  Depends: libfoo" or "  |Depends: libbar"
        # Strip leading whitespace, optional '|', and the relation keyword.
        stripped = line.strip().lstrip("|").strip()
        if ":" in stripped:
            _relation, _, dep_name = stripped.partition(":")
            dep_name = dep_name.strip()
            # Skip virtual / alternative markers (<foo>, |foo)
            if dep_name and not dep_name.startswith("<"):
                deps.add(dep_name)

    return deps


def _guess_blocking_held_package(
    pkg_name: str,
    held_names: set[str],
    depends_cache: dict[str, set[str]] | None = None,
) -> str | None:
    """Return the single held package most likely blocking *pkg_name*, or None.

    Heuristic (intentionally simple):
      For each held package, run ``apt-cache depends`` and check whether
      *pkg_name* appears in its dependency list.  If exactly one held package
      matches, return its name.  If zero or more than one match, return None
      so the caller can fall back to the generic message.

    *depends_cache* may be supplied by the caller as a shared mutable dict so
    that ``apt-cache depends`` is invoked at most once per held package across
    many blocked-package lookups in a single pass (e.g. ``get_updates()``).
    """
    if not held_names:
        return None

    if depends_cache is None:
        depends_cache = {}

    matched: list[str] = []
    for held in held_names:
        if held not in depends_cache:
            depends_cache[held] = _apt_cache_depends(held)
        if pkg_name in depends_cache[held]:
            matched.append(held)

    return matched[0] if len(matched) == 1 else None


def _stderr_mentions_lock(text: str) -> bool:
    """Return True if stderr text suggests an APT/dpkg lock conflict."""
    lowered = text.lower()
    return any(hint in lowered for hint in _LOCK_STDERR_HINTS)


def _output_mentions_network_error(text: str) -> bool:
    """Return True if output text suggests a network or partial update problem."""
    lowered = text.lower()
    return any(hint in lowered for hint in _NETWORK_ERROR_HINTS)


# ------------------------------------------------------------------ #
# AptBackend Class Definition                                          #
# ------------------------------------------------------------------ #


class AptBackend(UpdateBackend):
    """Update backend for Debian/Ubuntu APT package management."""

    @property
    def backend_id(self) -> str:
        return "apt"

    @property
    def display_name(self) -> str:
        return "Debian/Ubuntu Packages"

    def is_available(self) -> bool:
        # If python-apt is imported successfully (it is at the top), APT is available.
        return True

    def build_install_command(self,
                              packages: List[str] | None = None) -> list[str]:
        """Return a direct argv for privilege-escalated APT install/upgrade.

        The returned list is passed straight to VTE spawn_async — no shell
        layer is involved.  Privilege path: GUI → pkexec → helper → apt-get.
        """
        return build_upgrade_argv(packages)

    def check_busy(self) -> Tuple[bool, str]:
        """Return ``(is_busy, message)`` using layered /proc-based detection.

        **Layer 1 — process scan**: walks ``/proc/<pid>/comm`` and
        ``/proc/<pid>/cmdline`` for each running PID, matching against a broad
        set of package-manager keywords.  This catches helpers that appear as
        ``python3`` in *comm* but expose their identity in *cmdline* (e.g.
        ``apt.systemd.daily``).

        **Layer 2 — FD scan**: walks ``/proc/<pid>/fd`` symlinks looking for any
        process that currently has an APT/dpkg lock file open.  This catches
        processes that hold a lock but are momentarily sleeping and therefore not
        matched by the name scan.

        Returns ``(False, "")`` when no conflict is found or ``/proc`` is
        unreadable.
        """
        ignore_pids = {os.getpid(), os.getppid()}
        ignore_strs = {str(p) for p in ignore_pids} if ignore_pids else set()

        try:
            pids = [name for name in os.listdir("/proc") if name.isdigit()]
        except OSError:
            return False, ""

        # Layer 1: process name / cmdline keyword scan
        for pid in pids:
            if pid in ignore_strs:
                continue
            comm = _proc_comm(pid)
            cmdline = _proc_cmdline(pid)
            if _matches_apt_keyword(comm, cmdline):
                label = comm or "unknown"
                return True, f"Another package manager is running: {label} (PID {pid})"

        # Layer 2: open file-descriptor scan for APT/dpkg lock files
        lock_strs = {str(p) for p in _LOCK_PATHS}
        for pid in pids:
            if pid in ignore_strs:
                continue
            fd_dir = f"/proc/{pid}/fd"
            try:
                fds = os.listdir(fd_dir)
            except OSError:
                continue
            for fd in fds:
                try:
                    target = os.readlink(os.path.join(fd_dir, fd))
                except OSError:
                    continue
                if target in lock_strs:
                    comm = _proc_comm(pid) or "unknown"
                    return True, f"Another package manager is running: {comm} (PID {pid})"

        return False, ""

    @staticmethod
    def _parse_refresh_output(
            result: subprocess.CompletedProcess) -> Tuple[bool, str]:
        """Interpret a completed apt-get update subprocess result.

        Returns (success, message) — factors out the multi-return logic from
        refresh() so that method stays within the 6-return limit.
        """
        stdout_text = result.stdout or ""
        stderr_text = result.stderr or ""
        combined_output = stdout_text + "\n" + stderr_text

        if _output_mentions_network_error(combined_output):
            return False, "Unable to refresh package lists. Please check your internet connection."

        if result.returncode == 0:
            return True, "Package lists refreshed."

        if _stderr_mentions_lock(stderr_text):
            return False, "Another package manager is currently running."

        first_err = next(
            (line.strip() for line in stderr_text.splitlines() if line.strip()),
            "unknown error",
        )
        return False, f"Failed to refresh package lists. ({first_err})"

    def refresh(self, sentinel_path: str | None = None) -> Tuple[bool, str]:
        """Run a privileged ``apt-get update`` via the root helper and return ``(success,
        message)``."""
        privilege_tool = find_privilege_tool()
        if privilege_tool is None:
            return False, "No privilege tool found (pkexec / sudo / doas)."

        command = [privilege_tool, get_helper_path(), "refresh"]
        if sentinel_path:
            # Insert --sentinel before the subcommand so the root helper can
            # signal auth success to the GUI.
            command = [
                privilege_tool,
                get_helper_path(), "--sentinel", sentinel_path, "refresh"
            ]

        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False, "Package list refresh timed out."
        except OSError as exc:
            return False, f"Failed to launch privilege tool: {exc}"

        return self._parse_refresh_output(result)

    @staticmethod
    def _classify_constraint(
        pkg_name: str,
        summary: str,
        held_names: set,
        kept_back_names: set,
    ) -> Tuple[str, str]:
        """Return (constraint, description) for a single package."""
        if pkg_name in held_names:
            return CONSTRAINT_HELD, summary
        if pkg_name in kept_back_names:
            return CONSTRAINT_BLOCKED, "Blocked by held package or dependency constraints"
        return CONSTRAINT_NORMAL, summary

    @staticmethod
    def _build_update_item(
        pkg: apt.package.Package,
        held_names: set,
        kept_back_names: set,
    ) -> Tuple["UpdateItem", str]:
        """Build an UpdateItem and return (item, constraint) for one upgradable package."""
        installed_version = pkg.installed.version if pkg.installed else "unknown"
        candidate_version = pkg.candidate.version if pkg.candidate else "unknown"
        size = pkg.candidate.size if pkg.candidate else 0
        origin = _get_origin_name(pkg)
        summary = pkg.candidate.summary if pkg.candidate else ""
        constraint, description = AptBackend._classify_constraint(
            pkg.name, summary, held_names, kept_back_names)
        category = _determine_category(pkg.name, origin)
        return UpdateItem(
            name=pkg.name,
            installed_version=installed_version,
            candidate_version=candidate_version,
            size=size,
            origin=origin,
            backend="apt",
            category=category,
            description=description,
            constraint=constraint,
        ), constraint

    def get_updates(self) -> Tuple[List[UpdateItem], int]:
        """Read the local APT cache and return ``(updates, total_download_bytes)``."""
        held_names = _get_held_packages()
        kept_back_names = _get_kept_back_packages()
        cache = apt.Cache()
        cache.open()
        updates: List[UpdateItem] = []
        total_bytes = 0
        for pkg in cache:
            if not (pkg.is_installed and pkg.is_upgradable):
                continue
            item, constraint = self._build_update_item(pkg, held_names,
                                                       kept_back_names)
            updates.append(item)
            if constraint != CONSTRAINT_HELD:
                total_bytes += item.size
        updates.sort(key=_sort_key)
        return updates, total_bytes
