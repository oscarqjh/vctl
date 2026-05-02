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

    def _acquire(self) -> RuntimeClient:
        """Open a fresh admin client or raise LbUnreachable.

        Each haproxy admin command must use its own connection — the admin
        socket closes after sending a response in the default (non-prompt)
        mode, so reusing a single RuntimeClient across multiple commands
        triggers BrokenPipeError on the second send.
        """
        client = lb_admin_client(self.mgr)
        if client is None:
            raise LbUnreachable(
                sock=str(self.mgr.sock_path),
                tcp=f"{self.mgr.lb.host}:{self.mgr.lb.admin.bind_port}",
            )
        return client

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
            May also be raised mid-operation (after pre-state read but before
            the second admin call) if the LB stops between commands. State
            file is not written in that case — the caller can retry.
          BackendOpFailed: if haproxy admin command raises RuntimeError.
        """
        self._validate_pool(pool)
        backend_section = f"pool_{pool}"

        # Pre-state read — fresh socket per command (see _acquire docstring).
        haproxy_map = self._haproxy_servers(backend_section, self._acquire())
        in_haproxy = ep in haproxy_map
        in_state = ep in BackendState(self.mgr.state_dir, self.mgr.lb.host, pool=pool).list()

        if not in_haproxy:
            try:
                self._acquire().add_server(backend_section, _name_for(ep), ep)
            except RuntimeError as exc:
                raise BackendOpFailed(op="add_server", ep=ep, backend=backend_section) from exc

        try:
            self._acquire().set_state(backend_section, _name_for(ep), "ready")
        except RuntimeError as exc:
            raise BackendOpFailed(op="set_state", ep=ep, backend=backend_section) from exc

        if not in_state:
            BackendState(self.mgr.state_dir, self.mgr.lb.host, pool=pool).add(ep)

        if not in_haproxy and not in_state:
            return Outcome(ep=ep, pool=pool, action=Action.ADDED)
        if in_haproxy and not in_state:
            return Outcome(ep=ep, pool=pool, action=Action.ADOPTED)
        return Outcome(ep=ep, pool=pool, action=Action.READIED)

    def want_absent(self, ep: str, pool: str) -> Outcome:
        """Ensure ep is removed from haproxy and from the state file.

        Invariant: state file is only written after haproxy ack.

        Action mapping based on pre-state:
          not in_haproxy and not in_state → NONE (nothing to do)
              in_haproxy and     in_state → REMOVED
              in_haproxy and not in_state → REMOVED (state was already absent)
          not in_haproxy and     in_state → ORPHANED_CLEANED (state file cleaned)

        Raises:
          PoolNotFound: if pool is not in mgr.lb.pools.
          LbUnreachable: if both unix socket and TCP admin are unreachable.
            May also be raised mid-operation (between set_state maint and
            remove_server) if the LB stops between commands. State file is
            not written in that case; haproxy may be left with the server
            in maint state — the caller can retry to complete removal.
          BackendOpFailed: if haproxy admin command raises RuntimeError.
        """
        self._validate_pool(pool)
        backend_section = f"pool_{pool}"

        # Pre-state read — fresh socket per command (see _acquire docstring).
        haproxy_map = self._haproxy_servers(backend_section, self._acquire())
        in_haproxy = ep in haproxy_map
        in_state = ep in BackendState(self.mgr.state_dir, self.mgr.lb.host, pool=pool).list()

        if not in_haproxy and not in_state:
            return Outcome(ep=ep, pool=pool, action=Action.NONE)

        if in_haproxy:
            try:
                self._acquire().set_state(backend_section, _name_for(ep), "maint")
            except RuntimeError as exc:
                raise BackendOpFailed(op="set_state", ep=ep, backend=backend_section) from exc
            try:
                self._acquire().remove_server(backend_section, _name_for(ep))
            except RuntimeError as exc:
                raise BackendOpFailed(op="remove_server", ep=ep, backend=backend_section) from exc

        if in_state:
            BackendState(self.mgr.state_dir, self.mgr.lb.host, pool=pool).remove(ep)

        if not in_haproxy and in_state:
            return Outcome(ep=ep, pool=pool, action=Action.ORPHANED_CLEANED)
        return Outcome(ep=ep, pool=pool, action=Action.REMOVED)

    def want_draining(self, ep: str, pool: str) -> Outcome:
        """Set ep to drain state in haproxy. State file is NOT modified.

        Drain is a transitional haproxy admin state indicating the server should
        complete in-flight requests and accept no new ones. The state file
        represents intended membership (present or absent), not transient drain
        state. Calling want_present after want_draining will re-ready the server.

        Raises:
          PoolNotFound: if pool is not in mgr.lb.pools.
          LbUnreachable: if both unix socket and TCP admin are unreachable.
          BackendOpFailed: if set_state raises RuntimeError (e.g. server not found).
        """
        self._validate_pool(pool)
        backend_section = f"pool_{pool}"

        try:
            self._acquire().set_state(backend_section, _name_for(ep), "drain")
        except RuntimeError as exc:
            raise BackendOpFailed(op="set_state", ep=ep, backend=backend_section) from exc

        return Outcome(ep=ep, pool=pool, action=Action.DRAINED)

    # ---- bulk reconciliation ----

    def reconcile_pool(self, pool: str, target: set[str]) -> list[Outcome]:
        """Converge haproxy and state to exactly the endpoints in target.

        For each ep in target: call want_present.
        For each ep currently in haproxy but not in target: call want_absent.
        Returns the concatenated list of Outcome objects from all operations.

        Fail-fast: acquires client once at the start; raises LbUnreachable
        immediately if the admin socket is unreachable before any mutations.

        Performance note: opens 3*(N+M)+2 HAProxy admin socket connections
        where N=len(target) and M=number of excess haproxy entries (each
        want_present and want_absent opens 3 fresh sockets per the HAProxy
        single-command-per-connection contract). Acceptable for startup
        reconcile and lb auto-add; reconsider for tight-loop callers.

        Raises:
          PoolNotFound: if pool is not in mgr.lb.pools.
          LbUnreachable: if both unix socket and TCP admin are unreachable.
        """
        self._validate_pool(pool)
        backend_section = f"pool_{pool}"

        # Fail-fast acquire so we surface LbUnreachable before any mutations.
        self._acquire()

        outcomes: list[Outcome] = []

        for ep in sorted(target):
            outcomes.append(self.want_present(ep, pool))

        haproxy_map = self._haproxy_servers(backend_section, self._acquire())
        for ep in sorted(haproxy_map.keys()):
            if ep not in target:
                outcomes.append(self.want_absent(ep, pool))

        return outcomes

    def reconcile_from_state(self, pool: str) -> list[Outcome]:
        """Read state file as the target set and delegate to reconcile_pool.

        Raises:
          PoolNotFound: if pool is not in mgr.lb.pools.
          LbUnreachable: if both unix socket and TCP admin are unreachable.
        """
        state_entries = BackendState(self.mgr.state_dir, self.mgr.lb.host, pool=pool).list()
        return self.reconcile_pool(pool, set(state_entries))
