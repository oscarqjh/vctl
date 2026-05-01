"""`vctl lb <verb>` dispatch."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from vctl.lb.manager import LbManager
from vctl.lb.runtime import BackendStatus, RuntimeClient
from vctl.lb.state import BackendState
from vctl.resolver import resolve

_LOG = logging.getLogger(__name__)


def _build_subparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vctl lb")
    sp = p.add_subparsers(dest="verb", required=True)
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
    ):
        sp.add_parser(verb)
    sp.add_parser("start").add_argument("--force", action="store_true")
    wr = sp.add_parser("wait-ready")
    wr.add_argument("count", type=int, nargs="?", default=1)
    sp.add_parser("auto-add")
    add = sp.add_parser("add")
    add.add_argument("endpoint")
    add.add_argument(
        "--pool", default=None, help="explicit pool name (default: auto by /v1/models probe)"
    )
    rm = sp.add_parser("remove")
    rm.add_argument("endpoint")
    dr = sp.add_parser("drain")
    dr.add_argument("endpoint")
    dr.add_argument("--pool", default=None, help="explicit pool name")
    at = sp.add_parser("attach")
    at.add_argument("port", type=int, nargs="?")
    sp.add_parser("detach")
    sp.add_parser("health")
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
        for ep in bs.list():
            print(ep)
        return 0
    if verb == "wait-ready":
        return _wait_ready(mgr, parsed.count)
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


def _wait_ready(mgr: LbManager, n: int) -> int:
    """Block until >=n backends are READY (op=UP, admin not maint/drain) AND
    LB front responds 200 to /v1/models. Default timeout infinite;
    LB_WAIT_TIMEOUT (seconds, 0 = forever) env override.

    Drained backends still report op_state=UP — naive count is wrong.
    """
    import httpx

    timeout = os.environ.get("LB_WAIT_TIMEOUT")
    deadline = None
    if timeout is not None:
        try:
            t = float(timeout)
            if t > 0:
                deadline = time.monotonic() + t
        except ValueError:
            pass

    def _try_runtime(connect_timeout: float = 1.0) -> list[BackendStatus]:
        import socket as _socket

        try:
            sock_path = mgr.sock_path
            if sock_path.exists():
                s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
                s.settimeout(connect_timeout)
                s.connect(str(sock_path))
                rc = RuntimeClient.for_unix_fd(s)
            else:
                s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
                s.settimeout(connect_timeout)
                s.connect((mgr.lb.host, mgr.lb.admin.bind_port))
                rc = RuntimeClient.for_unix_fd(s)
            return rc.show_servers_state()
        except OSError:
            return []

    front_url = f"http://{mgr.lb.host}:{mgr.lb.pools[0].bind_port}/v1/models"
    last_log = 0.0
    while True:
        # Short socket connect timeout — capped by remaining deadline.
        sock_timeout = 1.0
        if deadline is not None:
            sock_timeout = min(sock_timeout, max(0.05, deadline - time.monotonic()))
        statuses = _try_runtime(connect_timeout=sock_timeout)
        ready = sum(1 for s in statuses if s.op == "UP" and s.admin == "ready")

        # Compute remaining time for this iteration's http probe.
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print(
                    f"timed out: {ready}/{n} ready, LB front=ERR after {timeout}s",
                    file=sys.stderr,
                )
                return 1
            http_timeout = min(3.0, remaining)
        else:
            http_timeout = 3.0

        front_code = 0
        try:
            r = httpx.get(front_url, timeout=http_timeout)
            front_code = r.status_code
        except (httpx.HTTPError, httpx.TransportError, Exception):
            front_code = 0

        if ready >= n and front_code == 200:
            print(f"{ready} backend(s) ready (needed {n}); LB front responds 200")
            return 0

        if deadline is not None and time.monotonic() > deadline:
            print(
                f"timed out: {ready}/{n} ready, LB front={front_code or 'ERR'} after {timeout}s",
                file=sys.stderr,
            )
            return 1

        now = time.monotonic()
        if now - last_log >= 30:
            print(f"waiting... {ready}/{n} ready, LB front={front_code or 'ERR'}", file=sys.stderr)
            last_log = now
        # Sleep at most until deadline (or 2s if no deadline).
        sleep_s = 2.0
        if deadline is not None:
            sleep_s = min(sleep_s, max(0.0, deadline - time.monotonic()))
        time.sleep(sleep_s)
