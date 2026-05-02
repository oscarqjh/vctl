"""Exception hierarchy for the Reconciler module."""

from __future__ import annotations


class ReconcilerError(Exception):
    """Base class for all Reconciler hard failures."""


class LbUnreachable(ReconcilerError):  # noqa: N818  # spec contract: naked descriptive name
    """Raised when both the unix socket and TCP admin port are unreachable.

    Carries the socket path and TCP address so callers can surface a clear
    diagnostic without needing to re-inspect the LbManager config.
    """

    def __init__(self, *, sock: str, tcp: str) -> None:
        self.sock = sock
        self.tcp = tcp
        super().__init__(f"LB admin socket unreachable: sock={sock}, tcp={tcp}")


class PoolNotFound(ReconcilerError):  # noqa: N818  # spec contract: naked descriptive name
    """Raised when the caller supplies a pool name not present in the LB config."""

    def __init__(self, *, requested: str, available: list[str]) -> None:
        self.requested = requested
        self.available = available
        super().__init__(f"pool {requested!r} not found; available pools: {available}")


class BackendOpFailed(ReconcilerError):  # noqa: N818  # spec contract: naked descriptive name
    """Raised when a haproxy admin command raises RuntimeError.

    The original RuntimeError is attached as ``__cause__`` by the Reconciler.
    The state file is left untouched whenever this exception propagates.
    """

    def __init__(self, *, op: str, ep: str, backend: str) -> None:
        self.op = op
        self.ep = ep
        self.backend = backend
        super().__init__(f"haproxy {op} failed for ep={ep!r} in backend={backend!r}")
