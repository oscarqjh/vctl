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

import shlex
import sys
from pathlib import Path
from typing import TYPE_CHECKING

# NOTE: _fetch_haproxy_stats is imported at module level so tests can monkeypatch
# at `vctl.lb.prune._fetch_haproxy_stats` rather than at the definition site.
from vctl.commands.lb import _fetch_haproxy_stats
from vctl.duration import _parse_duration  # re-exported for callers
from vctl.lb.errors import LbUnreachable
from vctl.lb.runtime import lb_admin_client
from vctl.tmux import TmuxSession as _TmuxSession
from vctl.tmux import tmux_session_exists

if TYPE_CHECKING:
    from vctl.config.models import LbPrune
    from vctl.lb.manager import LbManager

__all__ = [
    "_collect_prune_candidates",
    "_parse_duration",
    "_spawn_watcher",
    "_stop_watcher",
    "_watcher_status",
]


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


def _spawn_watcher(
    mgr: LbManager,
    prune_cfg: LbPrune,
    cluster_yaml_path: Path,
) -> None:
    """Spawn the vctl-lb-watch tmux session and write the sentinel pidfile.

    Builds: while true; do python -m vctl --config <path> lb prune; sleep N; done
    Writes: mgr.run_dir / "watch.pid"  with content "tmux:vctl-lb-watch\\n"
    """
    interval_s = _parse_duration(prune_cfg.watch_interval)
    inner_argv: list[str] = [
        sys.executable,
        "-m",
        "vctl",
        "--config",
        str(cluster_yaml_path),
        "lb",
        "prune",
    ]
    loop_cmd = f"while true; do {shlex.join(inner_argv)}; sleep {interval_s}; done"
    _TmuxSession("vctl-lb-watch").start(["bash", "-c", loop_cmd])

    pid_path = mgr.run_dir / "watch.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = pid_path.with_suffix(".tmp")
    tmp.write_text("tmux:vctl-lb-watch\n")
    tmp.replace(pid_path)


def _stop_watcher(mgr: LbManager) -> bool:
    """Kill the vctl-lb-watch tmux session and remove the sentinel pidfile.

    Returns True if a watcher session was killed; False if nothing to do.
    Idempotent: safe to call even if the watcher was never started.
    """
    was_running = tmux_session_exists("vctl-lb-watch")
    _TmuxSession("vctl-lb-watch").kill(tree=False)
    pid_path = mgr.run_dir / "watch.pid"
    pid_path.unlink(missing_ok=True)
    return was_running


def _watcher_status(mgr: LbManager) -> dict[str, object]:
    """Return watcher liveness info for `vctl lb status` display.

    Returns dict with keys:
      - "enabled"       (bool) — mgr.lb.prune.enabled
      - "session_alive" (bool) — tmux_session_exists("vctl-lb-watch")
      - "pidfile_ok"    (bool) — pidfile present with correct sentinel
      - "state"         (str)  — "running" | "not running" | "disabled"
    """
    enabled: bool = mgr.lb.prune.enabled
    if not enabled:
        return {"state": "disabled", "enabled": False, "session_alive": False, "pidfile_ok": False}
    session_alive = tmux_session_exists("vctl-lb-watch")
    pid_path = mgr.run_dir / "watch.pid"
    pidfile_ok = False
    if pid_path.exists():
        try:
            content = pid_path.read_text().strip()
            pidfile_ok = content == "tmux:vctl-lb-watch"
        except OSError:
            pass

    state = "running" if session_alive else "not running"

    return {
        "enabled": True,
        "session_alive": session_alive,
        "pidfile_ok": pidfile_ok,
        "state": state,
    }
