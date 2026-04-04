"""Data models used by the Bodhi Update Manager."""

from dataclasses import dataclass

# Possible values for UpdateItem.constraint:
#   "held"            – package is pinned via `apt-mark hold`
#   "blocked_by_hold" – package is kept back because a held dep blocks the upgrade
#   "normal"          – no APT resolver constraint; eligible for upgrade
CONSTRAINT_HELD = "held"
CONSTRAINT_BLOCKED = "blocked_by_hold"
CONSTRAINT_NORMAL = "normal"


@dataclass(frozen=True)
class UpdateItem:
    """Represent a single available package update."""

    name: str
    installed_version: str
    candidate_version: str
    size: int
    origin: str
    backend: str
    category: str
    description: str = ""
    constraint: str = CONSTRAINT_NORMAL
