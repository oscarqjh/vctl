"""Prune candidates collector for `vctl lb prune`.

_collect_prune_candidates joins two HAProxy admin queries:
  1. `show stat` CSV (via _fetch_haproxy_stats) → status + lastchg per server
  2. `show servers state`                        → admin bitmask per server

A backend is eligible for pruning when ALL hold:
  - status starts with "DOWN"   (health-check-failed; not UP/MAINT/DRAIN)
  - admin == "ready"            (no MAINT or DRAIN bitmask bits set)
  - lastchg >= threshold_s      (has been DOWN for at least threshold_s seconds)

MAINT and DRAIN backends are never pruned regardless of how long they've been
down — the operator explicitly placed them there.

Both HAProxy admin calls MUST use fresh RuntimeClient instances (the socket
closes after each response in non-prompt mode — see CLAUDE.md gotcha).
We call lb_admin_client() twice rather than reusing one client.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# NOTE: _fetch_haproxy_stats is imported at module level so tests can monkeypatch
# at `vctl.lb.prune._fetch_haproxy_stats` rather than at the definition site.
from vctl.commands.lb import _fetch_haproxy_stats
from vctl.duration import _parse_duration  # re-exported for callers
from vctl.lb.errors import LbUnreachable
from vctl.lb.runtime import lb_admin_client

if TYPE_CHECKING:
    from vctl.lb.manager import LbManager

__all__ = ["_collect_prune_candidates", "_parse_duration"]


def _collect_prune_candidates(
    mgr: LbManager,
    pool_name: str,
    threshold_s: int,
) -> list[tuple[str, int]]:
    """Return [(ep, lastchg_s), ...] for backends eligible for pruning in one pool.

    A backend is eligible when ALL of:
      - status starts with "DOWN" (from show stat CSV)
      - admin == "ready"  (not MAINT, not DRAIN; from show servers state bitmask)
      - lastchg >= threshold_s

    Raises:
      LbUnreachable: if either admin command cannot reach the HAProxy socket.
    """
    pool_section = f"pool_{pool_name}"

    # Call 1: show stat CSV → status + lastchg per server in this pool section.
    cli1 = lb_admin_client(mgr)
    if cli1 is None:
        raise LbUnreachable(
            sock=str(mgr.sock_path),
            tcp=f"{mgr.lb.host}:{mgr.lb.admin.bind_port}",
        )
    stats_by_section = _fetch_haproxy_stats(cli1)
    pool_stats = stats_by_section.get(pool_section, {})

    # Call 2: show servers state → admin bitmask per server in this pool section.
    # Fresh socket — the previous connection is now closed (per-command contract).
    cli2 = lb_admin_client(mgr)
    if cli2 is None:
        raise LbUnreachable(
            sock=str(mgr.sock_path),
            tcp=f"{mgr.lb.host}:{mgr.lb.admin.bind_port}",
        )
    all_rows = cli2.show_servers_state()
    rows = [r for r in all_rows if r.backend == pool_section]

    # Join on server name; apply eligibility filter.
    candidates: list[tuple[str, int]] = []
    for row in rows:
        if row.admin != "ready":
            continue  # MAINT or DRAIN — never prune

        srv_data = pool_stats.get(row.name, {})
        status = str(srv_data.get("status", ""))
        if not status.startswith("DOWN"):
            continue  # UP, MAINT-in-stat, DRAIN-in-stat, "no check", etc.

        lastchg = int(srv_data.get("lastchg", 0))
        if lastchg >= threshold_s:
            candidates.append((row.endpoint, lastchg))

    candidates.sort()  # deterministic: sorted by ep string
    return candidates
