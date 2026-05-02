"""Reconciler — single owner of (haproxy, state-file) consistency.

Phase 1: module ships alongside existing _do_add / _do_remove / _do_drain /
_do_auto_add functions in lb_scaling.py. Migration of callers is Phase 2.

Invariant: haproxy ack must precede any state-file write.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vctl.lb.errors import BackendOpFailed, LbUnreachable, PoolNotFound
from vctl.lb.routing import _name_for
from vctl.lb.runtime import BackendStatus, RuntimeClient, lb_admin_client
from vctl.lb.state import BackendState

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

    # ---- private helpers ----

    def _validate_pool(self, pool: str) -> None:
        """Raise PoolNotFound if pool name is not in the LB config."""
        available = [p.name for p in self.mgr.lb.pools]
        if pool not in available:
            raise PoolNotFound(requested=pool, available=available)

    def _haproxy_servers(self, section: str, client: RuntimeClient) -> dict[str, BackendStatus]:
        """Return {endpoint: BackendStatus} for live haproxy server rows.

        HAProxy's ``show servers state`` does not include the backend section
        name per row in all versions, so filtering by ``section`` is currently
        a no-op — all rows are returned keyed by endpoint. Callers that want
        per-pool semantics rely on the ``b_<ip>_<port>`` naming convention
        plus their own state-file scope to associate rows with pools.

        The ``section`` argument is retained for documentation and to make
        the eventual filtering upgrade a no-op for callers.
        """
        del section  # reserved for future filtering
        rows = client.show_servers_state()
        return {row.endpoint: row for row in rows}

    # ---- read-only API ----

    def diff(self, pool: str) -> Drift:
        """Return a Drift snapshot comparing the state file vs live haproxy state.

        Never raises on LB unreachable — returns Drift(lb_reachable=False) instead.
        Always raises PoolNotFound if the pool name is unknown.
        """
        self._validate_pool(pool)
        backend_section = f"pool_{pool}"
        state_eps = set(BackendState(self.mgr.state_dir, self.mgr.lb.host, pool=pool).list())

        client = lb_admin_client(self.mgr)
        if client is None:
            return Drift(
                pool=pool,
                lb_reachable=False,
                only_in_state=sorted(state_eps),
                only_in_haproxy=[],
                in_both=[],
                statuses={},
            )

        haproxy_map = self._haproxy_servers(backend_section, client)
        haproxy_eps = set(haproxy_map.keys())

        return Drift(
            pool=pool,
            lb_reachable=True,
            only_in_state=sorted(state_eps - haproxy_eps),
            only_in_haproxy=sorted(haproxy_eps - state_eps),
            in_both=sorted(state_eps & haproxy_eps),
            statuses={ep: haproxy_map[ep] for ep in haproxy_eps},
        )

    def diff_all(self) -> list[Drift]:
        """Return one Drift per configured pool."""
        return [self.diff(pool.name) for pool in self.mgr.lb.pools]

    # ---- mutating API ----

    def want_present(self, ep: str, pool: str) -> Outcome:
        """Ensure ep is registered in haproxy and in the state file.

        Invariant: state file is never written before haproxy acknowledges.

        Action mapping based on pre-state:
          not in_haproxy and not in_state → ADDED
          not in_haproxy and     in_state → READIED (re-registered)
              in_haproxy and not in_state → ADOPTED (state file catches up)
              in_haproxy and     in_state → READIED (idempotent re-heal)

        Raises:
          PoolNotFound: if pool is not in mgr.lb.pools.
          LbUnreachable: if both unix socket and TCP admin are unreachable.
          BackendOpFailed: if haproxy admin command raises RuntimeError.
        """
        self._validate_pool(pool)
        backend_section = f"pool_{pool}"

        client = lb_admin_client(self.mgr)
        if client is None:
            raise LbUnreachable(
                sock=str(self.mgr.sock_path),
                tcp=f"{self.mgr.lb.host}:{self.mgr.lb.admin.bind_port}",
            )

        haproxy_map = self._haproxy_servers(backend_section, client)
        in_haproxy = ep in haproxy_map
        in_state = ep in BackendState(self.mgr.state_dir, self.mgr.lb.host, pool=pool).list()

        if not in_haproxy:
            try:
                client.add_server(backend_section, _name_for(ep), ep)
            except RuntimeError as exc:
                raise BackendOpFailed(op="add_server", ep=ep, backend=backend_section) from exc

        try:
            client.set_state(backend_section, _name_for(ep), "ready")
        except RuntimeError as exc:
            raise BackendOpFailed(op="set_state", ep=ep, backend=backend_section) from exc

        if not in_state:
            BackendState(self.mgr.state_dir, self.mgr.lb.host, pool=pool).add(ep)

        if not in_haproxy and not in_state:
            return Outcome(ep=ep, pool=pool, action=Action.ADDED)
        if in_haproxy and not in_state:
            return Outcome(ep=ep, pool=pool, action=Action.ADOPTED)
        return Outcome(ep=ep, pool=pool, action=Action.READIED)
