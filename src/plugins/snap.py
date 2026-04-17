"""Snap-backed update discovery for the Bodhi Update Manager."""

# pylint: disable=duplicate-code  # build_install_command mirrors flatpak; required by ABC contract
import shutil
import subprocess
from typing import Dict, List, Tuple

from bodhi_update.backends import UpdateBackend
from bodhi_update.models import UpdateItem


class SnapBackend(UpdateBackend):
    """Update backend that queries installed Snap packages."""

    backend_id = "snap"
    display_name = "Snap Packages"

    def is_available(self) -> bool:
        """Return True only if snap exists and snapd is responsive.

        Uses `snap list` as a lightweight probe: it requires no network access
        and succeeds as long as snapd is running.  A non-zero exit, timeout,
        or OSError is treated as unavailable.
        """
        if shutil.which("snap") is None:
            return False
        try:
            result = subprocess.run(
                ["snap", "list"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=8,
                check=False,
            )
            if result.returncode != 0:
                return False
            # Paranoia: stderr containing daemon-unavailable text is a hard fail.
            stderr_text = (result.stderr or
                           b"").decode(errors="replace").lower()
            return "cannot connect" not in stderr_text
        except (OSError, subprocess.TimeoutExpired):
            return False

    def check_busy(self) -> Tuple[bool, str]:
        return False, ""

    def refresh(self) -> Tuple[bool, str]:
        # Discovery is done live in get_updates(); no separate cache step.
        return True, ""

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_snap_table(stdout: str) -> List[List[str]]:
        """Return non-header, non-blank rows from a snap tabular output."""
        rows: List[List[str]] = []
        for line in stdout.strip().splitlines():
            stripped = line.strip()
            if not stripped or stripped.lower().startswith("name"):
                continue
            parts = stripped.split()
            if parts:
                rows.append(parts)
        return rows

    def _installed_versions(self) -> Dict[str, str]:
        """Return {snap_name: installed_version} from `snap list`."""
        try:
            result = subprocess.run(
                ["snap", "list"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {}
        if result.returncode != 0 or not result.stdout:
            return {}
        installed: Dict[str, str] = {}
        # snap list columns: Name  Version  Rev  Tracking  Publisher  Notes
        for row in self._parse_snap_table(result.stdout):
            if len(row) >= 2:
                installed[row[0]] = row[1]
        return installed

    # ------------------------------------------------------------------ #
    # Backend interface                                                    #
    # ------------------------------------------------------------------ #

    def get_updates(self) -> Tuple[List[UpdateItem], int]:
        """Return snaps that have an available refresh.

        `snap refresh --list` reports only snaps with a pending update; it does
        NOT list all installed snaps, so no filtering is needed.
        Installed versions are looked up separately from `snap list`.
        """
        try:
            result = subprocess.run(
                ["snap", "refresh", "--list"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return [], 0

        # returncode 0 → updates present; anything else (e.g. 1 for "no updates")
        # may still produce empty but valid output.  We only need the rows.
        if not result.stdout or not result.stdout.strip():
            return [], 0

        # Fetch installed versions for honest population of installed_version.
        installed = self._installed_versions()

        updates: List[UpdateItem] = []
        # snap refresh --list columns: Name  Version  Rev  Size  Publisher  Notes
        for row in self._parse_snap_table(result.stdout):
            if len(row) < 2:
                continue
            name = row[0]
            candidate_version = row[1]
            installed_version = installed.get(name, "-")

            updates.append(
                UpdateItem(
                    name=name,
                    installed_version=installed_version,
                    candidate_version=candidate_version,
                    size=0,
                    origin="snap",
                    backend="snap",
                    category="snap",
                    description="Snap package",
                ))

        return updates, 0

    def build_install_command(self,
                              packages: List[str] | None = None) -> list[str]:
        if not packages:
            discovered, _ = self.get_updates()
            packages = [item.name for item in discovered]
        if not packages:
            return ["true"]  # nothing to refresh; exit cleanly
        return ["snap", "refresh"] + packages
