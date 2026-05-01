"""Scaling verbs: add / remove / drain / attach / detach / auto-add / health."""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
import time

from vctl.lb.manager import LbManager
from vctl.lb.probe import probe_local_vllm
from vctl.lb.routing import pool_for_endpoint
from vctl.lb.runtime import RuntimeClient
from vctl.lb.state import BackendState
from vctl.platform import detect_self_ip

_LOG = logging.getLogger(__name__)


def _client(mgr: LbManager) -> RuntimeClient | None:
    if os.environ.get("VCTL_TEST_NO_SOCKET") == "1":
        return None
    sock = mgr.sock_path
    try:
        if sock.exists():
            return RuntimeClient.for_unix(str(sock))
        return RuntimeClient.for_tcp(mgr.lb.host, mgr.lb.admin.bind_port)
    except OSError:
        return None


def _name_for(ep: str) -> str:
    return "b_" + ep.replace(".", "_").replace(":", "_")


def dispatch(
    verb: str, parsed: argparse.Namespace, ns: argparse.Namespace, mgr: LbManager, bs: BackendState
) -> int:
    if verb == "add":
        return _do_add(parsed.endpoint, mgr, bs)
    if verb == "remove":
        return _do_remove(parsed.endpoint, mgr, bs)
    if verb == "drain":
        return _do_drain(parsed.endpoint, mgr)
    if verb == "attach":
        port = parsed.port or 8000
        return _do_attach(port, mgr, bs)
    if verb == "detach":
        return _do_detach(mgr, bs)
    if verb == "auto-add":
        return _do_auto_add(mgr, bs)
    if verb == "health":
        return _do_health(mgr, bs)
    print(f"unknown lb verb: {verb}", file=sys.stderr)
    return 2


def _resolve_pool_name(mgr: LbManager, requested: str | None) -> str:
    """Validate/default the pool name against the LB config.

    - If *requested* is given and exists → return it.
    - If *requested* is given but unknown → stderr + exit 3.
    - If *requested* is None and there is exactly one pool → use it.
    - If *requested* is None and there are multiple pools → raise ValueError
      (the caller must specify a pool; serve.py always passes pool_name).
    """
    if requested:
        if requested not in {p.name for p in mgr.lb.pools}:
            print(
                f"unknown pool {requested!r}; available: {[p.name for p in mgr.lb.pools]}",
                file=sys.stderr,
            )
            sys.exit(3)
        return requested
    if len(mgr.lb.pools) == 1:
        return mgr.lb.pools[0].name
    raise ValueError("multiple pools configured; pool_name required")


def _do_add(ep: str, mgr: LbManager, bs: BackendState, pool_name: str | None = None) -> int:
    pool_name = _resolve_pool_name(mgr, pool_name)
    backend_section = f"pool_{pool_name}"
    state_result = bs.add(ep)
    cli = _client(mgr)
    if cli is not None:
        try:
            cli.add_server(backend_section, _name_for(ep), ep)
        except Exception as e:
            _LOG.error("admin socket add_server failed: %s", e)
        # Force ready: clears any lingering drain/maint from a previous
        # session that crashed mid-detach. Idempotent.
        with contextlib.suppress(Exception):
            cli.set_state(backend_section, _name_for(ep), "ready")
    label = "(new)" if state_result == "new" else "(already present)"
    print(f"add {ep} {label} (pool: {pool_name})", file=sys.stderr)
    return 0


def _do_remove(ep: str, mgr: LbManager, bs: BackendState, pool_name: str | None = None) -> int:
    pool_name = _resolve_pool_name(mgr, pool_name)
    backend_section = f"pool_{pool_name}"
    bs.remove(ep)
    cli = _client(mgr)
    if cli is not None:
        # HAProxy refuses `del server` unless the server is in maint state.
        # Set maint first (idempotent), then del. Both errors swallowed —
        # state file is always updated.
        with contextlib.suppress(Exception):
            cli.set_state(backend_section, _name_for(ep), "maint")
        with contextlib.suppress(Exception):
            cli.remove_server(backend_section, _name_for(ep))
    return 0


def _do_drain(ep: str, mgr: LbManager, pool_name: str | None = None) -> int:
    pool_name = _resolve_pool_name(mgr, pool_name)
    backend_section = f"pool_{pool_name}"
    cli = _client(mgr)
    if cli is not None:
        cli.set_state(backend_section, _name_for(ep), "drain")
    return 0


def _do_attach(port: int, mgr: LbManager, bs: BackendState) -> int:
    self_ip = detect_self_ip()
    ep = f"{self_ip}:{port}"
    pool = pool_for_endpoint(mgr.lb, ep)
    probe = probe_local_vllm(port)
    if not probe.get("models_loaded"):
        print(f"refusing to attach: localhost:{port} model not loaded", file=sys.stderr)
        return 1
    bs_pool = BackendState(bs.state_dir, bs.lb_host, pool=pool.name)
    return _do_add(ep, mgr, bs_pool, pool_name=pool.name)


def _do_detach(mgr: LbManager, bs: BackendState) -> int:
    self_ip = detect_self_ip()
    # Scan all known pools for an endpoint matching this host.
    pool_names = BackendState.list_pools(bs.state_dir, bs.lb_host)
    if not pool_names:
        # Fall back to pools in LB config.
        pool_names = [p.name for p in mgr.lb.pools]
    for pname in pool_names:
        pbs = BackendState(bs.state_dir, bs.lb_host, pool=pname)
        matching = [ep for ep in pbs.list() if ep.startswith(f"{self_ip}:")]
        if not matching:
            continue
        ep = matching[0]
        cli = _client(mgr)
        backend_section = f"pool_{pname}"
        if cli is not None:
            cli.set_state(backend_section, _name_for(ep), "drain")
        timeout = float(os.environ.get("LB_DETACH_WAIT", "30"))
        deadline = time.monotonic() + timeout
        port = int(ep.rsplit(":", 1)[1])
        while time.monotonic() < deadline:
            probe = probe_local_vllm(port)
            if probe.get("num_requests_running", 0.0) <= 0.0:
                break
            time.sleep(1)
        return _do_remove(ep, mgr, pbs, pool_name=pname)
    return 0


def _do_auto_add(mgr: LbManager, bs: BackendState) -> int:
    cli = _client(mgr)
    # Scan all known pools.
    pool_names = BackendState.list_pools(bs.state_dir, bs.lb_host)
    if not pool_names:
        pool_names = [p.name for p in mgr.lb.pools]
    for pname in pool_names:
        pbs = BackendState(bs.state_dir, bs.lb_host, pool=pname)
        backend_section = f"pool_{pname}"
        for ep in pbs.list():
            if cli is not None:
                with contextlib.suppress(Exception):
                    cli.add_server(backend_section, _name_for(ep), ep)
    return 0


def _do_health(mgr: LbManager, bs: BackendState) -> int:
    unhealthy = 0
    for ep in bs.list():
        port = int(ep.rsplit(":", 1)[1])
        probe = probe_local_vllm(port)
        ok = probe.get("healthy", False)
        marker = "OK" if ok else "FAIL"
        print(
            f"{ep:30s} {marker}  health={probe.get('health_code')} "
            f"models_loaded={probe.get('models_loaded')}"
        )
        if not ok:
            unhealthy += 1
    return 0 if unhealthy == 0 else unhealthy
