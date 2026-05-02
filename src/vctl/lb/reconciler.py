"""Reconciler — single owner of (haproxy, state-file) consistency.

Phase 1: module ships alongside existing _do_add / _do_remove / _do_drain /
_do_auto_add functions in lb_scaling.py. Migration of callers is Phase 2.

Invariant: haproxy ack must precede any state-file write.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vctl.lb.runtime import BackendStatus

if TYPE_CHECKING:
    from vctl.lb.manager import LbManager


class Action(enum.Enum):
    NONE = "none"
    ADDED = "added"
    REMOVED = "removed"
    DRAINED = "drained"
    READIED = "readied"
    ADOPTED = "adopted"
    ORPHANED_CLEANED = "orphaned_cleaned"


@dataclass(frozen=True)
class Outcome:
    """Result of a single Reconciler mutation."""

    ep: str
    pool: str
    action: Action
    note: str = ""


@dataclass(frozen=True)
class Drift:
    """Snapshot of divergence between haproxy state and the on-disk state file."""

    pool: str
    lb_reachable: bool
    only_in_state: list[str]
    only_in_haproxy: list[str]
    in_both: list[str]
    statuses: dict[str, BackendStatus]


class Reconciler:
    """Single authoritative path for keeping haproxy and state file in sync."""

    def __init__(self, mgr: LbManager) -> None:
        self.mgr = mgr
