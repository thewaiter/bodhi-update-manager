"""Flatpak-backed update discovery for the Bodhi Update Manager."""

import shutil
import subprocess
from typing import Dict, List, Tuple

from bodhi_update.backends import BackendMeta, UpdateBackend
from bodhi_update.models import UpdateItem

# `flatpak remote-ls --updates --columns=application,branch,origin` outputs
# tab-separated rows.  We query each scope explicitly so that both system-wide
# and per-user Flatpak installations are covered (matching `flatpak update`).
_LS_COLS = "application,branch,origin"


class FlatpakBackend(UpdateBackend):
    """Update backend that queries installed Flatpak applications."""

    meta = BackendMeta(
        backend_id = "flatpak",
        display_name = "Flatpak Packages",
        filter_group = "flatpak",
        filter_label = "FlatPak",
        filter_sort_order = 200,
    )

    def is_available(self) -> bool:
        """Return True when the flatpak binary exists and can list apps.

        Uses `flatpak list` as a lightweight, network-free probe.
        Only the return code is checked — stderr output is ignored so that
        routine warning/info messages do not suppress the backend.
        """
        if shutil.which("flatpak") is None:
            return False
        try:
            result = subprocess.run(
                ["flatpak", "list", "--columns=application"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=8,
                check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def check_busy(self) -> Tuple[bool, str]:
        return False, ""

    def refresh(self, sentinel_path: str | None = None) -> tuple[bool, str]:
        # Remote checks are done live in get_updates(); no warm-up needed.
        return True, ""

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _run(argv: List[str], timeout: int = 30) -> str:
        """Run *argv*, return stdout text on rc=0, empty string on failure."""
        try:
            result = subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if result.returncode != 0:
            return ""
        return result.stdout or ""

    def _query_scope(self, scope_flag: str) -> List[Tuple[str, str, str]]:
        """Run remote-ls --updates for *scope_flag* ('--system' or '--user').

        Returns list of (app_id, branch, origin) tuples.
        """
        out = self._run(
            [
                "flatpak", scope_flag, "remote-ls", "--updates",
                f"--columns={_LS_COLS}"
            ],
            timeout=30,
        )
        return self._parse_ls_output(out)

    @staticmethod
    def _parse_ls_output(stdout: str) -> List[Tuple[str, str, str]]:
        """Parse tab-separated `remote-ls --updates` output.

        Each line: application \\t branch \\t origin
        Skips blank lines and any header line (starts with non-app text).
        """
        rows: List[Tuple[str, str, str]] = []
        for line in stdout.strip().splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            parts = stripped.split("\t")
            if len(parts) < 3:
                continue
            app_id = parts[0].strip()
            branch = parts[1].strip()
            origin = parts[2].strip()
            # Skip the header row emitted by some flatpak versions.
            if not app_id or app_id.lower() == "application id":
                continue
            rows.append((app_id, branch, origin))
        return rows

    def _installed_versions(self) -> Dict[str, str]:
        """Return {app_id: installed_version} for all installed Flatpaks."""
        # Query both scopes and merge.
        installed: Dict[str, str] = {}
        for scope in ("--system", "--user"):
            out = self._run(
                ["flatpak", scope, "list", "--columns=application,version"],
                timeout=10,
            )
            for line in out.strip().splitlines():
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    app_id, version = parts[0].strip(), parts[1].strip()
                    if app_id:
                        installed[app_id] = version
        return installed

    # ------------------------------------------------------------------ #
    # Backend interface                                                    #
    # ------------------------------------------------------------------ #

    def get_updates(self) -> Tuple[List[UpdateItem], int]:
        """Return Flatpak apps/runtimes that have an available update.

        Queries both --system and --user scopes explicitly so that the result
        matches what `flatpak update` (which also checks both) would show.
        """
        seen: set = set()
        pending: List[Tuple[str, str, str]] = []

        for scope in ("--system", "--user"):
            for row in self._query_scope(scope):
                app_id = row[0]
                if app_id not in seen:
                    seen.add(app_id)
                    pending.append(row)

        if not pending:
            return [], 0

        installed = self._installed_versions()
        updates: List[UpdateItem] = []

        for app_id, branch, origin in pending:
            installed_version = installed.get(app_id, "-")
            # remote-ls does not expose the exact new version string;
            # use the tracking branch (e.g. "stable") as the descriptor.
            candidate_version = branch or "update available"
            updates.append(
                UpdateItem(
                    name=app_id,
                    installed_version=installed_version,
                    candidate_version=candidate_version,
                    size=0,
                    origin=origin or "flatpak",
                    backend="flatpak",
                    category="flatpak",
                    description="Flatpak package",
                ))

        return updates, 0

    def build_install_command(self,
                              packages: List[str] | None = None) -> list[str]:
        if not packages:
            discovered, _ = self.get_updates()
            packages = [item.name for item in discovered]
        if not packages:
            return ["true"]  # nothing to update; exit cleanly
        # `flatpak update -y <app_id>...` updates named refs non-interactively.
        return ["flatpak", "update", "-y"] + packages
