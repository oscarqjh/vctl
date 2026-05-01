"""`vctl lb <verb>` dispatch."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from vctl.lb.manager import LbManager
from vctl.lb.state import BackendState
from vctl.resolver import resolve

_LOG = logging.getLogger(__name__)


_LB_VERB_HELP: dict[str, str] = {
    "install": "Install haproxy (conda → source-build fallback)",
    "start": "Start haproxy in tmux session vctl-lb",
    "stop": "SIGTERM haproxy (via pidfile) + tear down tmux session",
    "status": "Process + admin-socket + tmux state",
    "is-host": "Exit 0 if this pod's IP == lb.host, else exit 1",
    "where": "Print lb.host:bind_port",
    "list": "List backends per pool from the state file",
    "wait-ready": "Block until ≥N ready in every non-empty pool (and LB front 200)",
    "stats": "Print stats dashboard URL",
    "logs": "Print contents of haproxy.log",
    "config": "Print rendered haproxy.cfg",
    "reload": "Re-render config + haproxy -sf <pid> (graceful, zero-downtime)",
    "auto-add": "Re-register every backend from the state file (post-restart recovery)",
    "add": "Register an endpoint in a pool (idempotent; auto-routes on multi-pool)",
    "remove": "Drop an endpoint from its pool (set maint then del)",
    "drain": "Mark backend as drain (no new traffic, finish in-flight)",
    "attach": "Probe localhost:<port>/v1/models then add self to its pool",
    "detach": "Drain self, wait for in-flight to drain, remove",
    "health": "Probe each registered backend; exit non-zero on any unhealthy",
}


def _build_subparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vctl lb", description="LB lifecycle + scaling control.")
    sp = p.add_subparsers(dest="verb", required=True, metavar="VERB")
    # Verbs with no extra args.
    for verb in (
        "install",
        "stop",
        "status",
        "is-host",
        "where",
        "list",
        "stats",
        "logs",
        "config",
        "reload",
        "auto-add",
        "detach",
        "health",
    ):
        sp.add_parser(verb, help=_LB_VERB_HELP[verb])
    # Verbs with args / flags.
    start = sp.add_parser("start", help=_LB_VERB_HELP["start"])
    start.add_argument("--force", action="store_true",
                       help="start even if this pod's IP != lb.host")
    wr = sp.add_parser("wait-ready", help=_LB_VERB_HELP["wait-ready"])
    wr.add_argument("count", type=int, nargs="?", default=1,
                    help="minimum ready backends per non-empty pool (default: 1)")
    wr.add_argument("--pool", default=None, help="scope to a single pool")
    add = sp.add_parser("add", help=_LB_VERB_HELP["add"])
    add.add_argument("endpoint", help="ip:port to add")
    add.add_argument("--pool", default=None,
                     help="explicit pool name (default: auto by /v1/models probe)")
    rm = sp.add_parser("remove", help=_LB_VERB_HELP["remove"])
    rm.add_argument("endpoint", help="ip:port to remove")
    dr = sp.add_parser("drain", help=_LB_VERB_HELP["drain"])
    dr.add_argument("endpoint", help="ip:port to drain")
    dr.add_argument("--pool", default=None, help="explicit pool name")
    at = sp.add_parser("attach", help=_LB_VERB_HELP["attach"])
    at.add_argument("port", type=int, nargs="?",
                    help="local vllm port to probe (default: 8000)")
    return p


def _manager(ns: argparse.Namespace) -> tuple[LbManager, BackendState, Path]:
    rc = resolve(ns.config, profile=ns.profile)
    state_dir = Path(rc.cluster.state_dir)
    run_dir = Path.home() / ".vctl" / "lb"
    mgr = LbManager(rc.lb, state_dir=state_dir, run_dir=run_dir)
    bs = BackendState(state_dir, rc.lb.host)
    return mgr, bs, state_dir


def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    parsed = _build_subparser().parse_args(argv_rest)
    verb = parsed.verb
    mgr, bs, _ = _manager(ns)
    if verb == "where":
        print(f"{mgr.lb.host}:{mgr.lb.pools[0].bind_port}")
        return 0
    if verb == "is-host":
        return 0 if mgr.is_host() else 1
    if verb == "config":
        print(mgr.render_config())
        return 0
    if verb == "start":
        try:
            mgr.start(force=parsed.force)
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 4
        return 0
    if verb == "stop":
        mgr.stop()
        return 0
    if verb == "status":
        for k, v in mgr.status().items():
            print(f"{k}: {v}")
        return 0
    if verb == "reload":
        mgr.reload()
        return 0
    if verb == "list":
        pools = mgr.lb.pools
        # Detect LB liveness so we can annotate state-file content correctly.
        # When LB is up: state file == live haproxy pool (per add/remove sync).
        # When LB is down: state file is the persistent registry for the
        # next `lb start` + `auto-add`. Backends shown are NOT actually
        # serving traffic. Annotate to avoid confusion.
        st = mgr.status()
        lb_running = bool(st.get("running"))
        if not lb_running:
            print(
                "WARNING: LB is not running. The entries below are persisted "
                "registrations from the state file — they are NOT serving "
                "traffic right now. Run `vctl lb start` to bring them back."
            )
            print()

        found_any = False
        for pool in pools:
            pbs = BackendState(bs.state_dir, bs.lb_host, pool=pool.name)
            eps = pbs.list()
            url = f"http://{mgr.lb.host}:{pool.bind_port}"
            suffix = "" if lb_running else "  [LB STOPPED]"
            print(f"pool: {pool.name} ({pool.served_model}) — {url}{suffix}")
            if eps:
                for ep in eps:
                    print(f"  {ep}")
                found_any = True
            else:
                print("  (no backends)")
        if not found_any:
            print()
            print("(no backends registered in any pool)")
        return 0
    if verb == "wait-ready":
        return _wait_ready(mgr, parsed.count, pool_filter=parsed.pool)
    if verb == "install":
        from vctl.lb.installer import ensure_haproxy

        print(ensure_haproxy())
        return 0
    if verb == "stats":
        print(f"http://{mgr.lb.host}:{mgr.lb.stats.bind_port}")
        return 0
    if verb == "logs":
        log_path = mgr.run_dir / "haproxy.log"
        if log_path.exists():
            print(log_path.read_text())
        return 0
    # Scaling verbs (Task 17).
    from vctl.commands import lb_scaling

    return lb_scaling.dispatch(verb, parsed, ns, mgr, bs)


def _wait_ready(mgr: LbManager, n: int, pool_filter: str | None = None) -> int:
    """Block until each non-empty target pool has >=n ready backends AND its
    front-port returns 200 on /v1/models. Empty pools are skipped (not blocking).

    LB_WAIT_TIMEOUT (seconds, 0 = forever) env override.

    Note: the `n` threshold is checked per-pool based on the number of registered
    backends in each pool's state file combined with a live HTTP probe of the
    pool's front-port. HAProxy admin-socket per-server state is not inspected.
    """
    import httpx

    timeout_str = os.environ.get("LB_WAIT_TIMEOUT")
    deadline = None
    if timeout_str is not None:
        try:
            t = float(timeout_str)
            if t > 0:
                deadline = time.monotonic() + t
        except ValueError:
            pass

    target_pools = [p for p in mgr.lb.pools if pool_filter is None or p.name == pool_filter]
    if pool_filter and not target_pools:
        print(
            f"unknown pool {pool_filter!r}; available: {[p.name for p in mgr.lb.pools]}",
            file=sys.stderr,
        )
        return 3

    state_dir = mgr.state_dir if isinstance(mgr.state_dir, Path) else Path(mgr.state_dir)
    last_log = 0.0
    while True:
        all_pools_ok = True
        any_pool_has_backends = False
        details: list[str] = []
        for p in target_pools:
            pbs = BackendState(state_dir, mgr.lb.host, pool=p.name)
            registered = pbs.list()
            if not registered:
                details.append(f"{p.name}=empty")
                continue
            any_pool_has_backends = True
            url = f"http://{mgr.lb.host}:{p.bind_port}/v1/models"
            try:
                r = httpx.get(url, timeout=3.0)
                code = r.status_code
            except Exception:
                code = 0
            backends_label = f"{len(registered)}backend{'s' if len(registered) != 1 else ''}"
            details.append(f"{p.name}={backends_label}/{code or 'ERR'}")
            if code != 200 or len(registered) < n:
                all_pools_ok = False

        if all_pools_ok and any_pool_has_backends:
            print(f"all pools ready: {', '.join(details)}")
            return 0

        if deadline is not None and time.monotonic() > deadline:
            print(
                f"timed out: {', '.join(details)} (after {timeout_str}s)",
                file=sys.stderr,
            )
            return 1

        now = time.monotonic()
        if now - last_log >= 30:
            print(f"waiting... {', '.join(details)}", file=sys.stderr)
            last_log = now
        sleep_s = 2.0
        if deadline is not None:
            sleep_s = min(sleep_s, max(0.0, deadline - time.monotonic()))
        time.sleep(sleep_s)
