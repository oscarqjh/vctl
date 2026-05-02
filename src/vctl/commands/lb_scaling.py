"""Scaling verbs: add / remove / drain / attach / detach / auto-add / health."""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
import time

from vctl.lb.errors import BackendOpFailed, LbUnreachable, PoolNotFound, ReconcilerError
from vctl.lb.manager import LbManager
from vctl.lb.probe import probe_local_vllm, probe_vllm
from vctl.lb.reconciler import Action, Reconciler
from vctl.lb.routing import _name_for, pool_for_endpoint
from vctl.lb.runtime import RuntimeClient, _NoOpClient
from vctl.lb.runtime import lb_admin_client as _client
from vctl.lb.state import BackendState
from vctl.platform import detect_self_ip

_LOG = logging.getLogger(__name__)

# Explicit re-export declaration for mypy --strict (and a marker for readers).
# The canonical implementations live in vctl.lb.runtime / vctl.lb.routing;
# they are imported above and surfaced here so:
#   1. mypy treats `from vctl.commands.lb_scaling import _client` (used in
#      vctl.commands.lb) as an explicit re-export, not an attr-defined error.
#   2. `monkeypatch.setattr(lb_scaling, "_client", ...)` in existing tests
#      keeps working unchanged after the Phase 1 extraction.
__all__ = ["_NoOpClient", "_client", "RuntimeClient", "_name_for"]


def _exit_for(exc: ReconcilerError) -> int:
    """Map a ReconcilerError subclass to a CLI exit code.

    LbUnreachable  → 4  (environment error: LB socket down)
    PoolNotFound   → 3  (user error: unknown pool name)
    BackendOpFailed and any future ReconcilerError subclass → 1  (generic failure)
    """
    if isinstance(exc, LbUnreachable):
        return 4
    if isinstance(exc, PoolNotFound):
        return 3
    return 1


def dispatch(
    verb: str, parsed: argparse.Namespace, ns: argparse.Namespace, mgr: LbManager, bs: BackendState
) -> int:
    if verb == "add":
        return _do_add_cli(parsed.endpoint, mgr, bs, getattr(parsed, "pool", None))
    if verb == "remove":
        return _do_remove_cli(parsed.endpoint, mgr, bs)
    if verb == "drain":
        return _do_drain(parsed.endpoint, mgr, pool_name=getattr(parsed, "pool", None))
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


def _do_add_cli(ep: str, mgr: LbManager, bs: BackendState, requested_pool: str | None) -> int:
    """User-invoked ``lb add``.  If ``--pool`` given, use it.
    Otherwise: single pool → use it; multi-pool → probe ep for model.
    """
    if requested_pool:
        pool_name = requested_pool  # _resolve_pool_name in _do_add validates
    elif len(mgr.lb.pools) == 1:
        pool_name = mgr.lb.pools[0].name
    else:
        pool = pool_for_endpoint(mgr.lb, ep)
        pool_name = pool.name
    pbs = BackendState(bs.state_dir, bs.lb_host, pool=pool_name)
    return _do_add(ep, mgr, pbs, pool_name=pool_name)


def _do_remove_cli(ep: str, mgr: LbManager, bs: BackendState) -> int:
    """User-invoked ``lb remove``. Scan all pools for the ep.

    - If found in a pool state file → delegate to _do_remove (haproxy-first).
    - If not found in any state file → attempt haproxy-side cleanup against all
      configured pools (server may still linger there without a state-file entry).
      Returns 0 if any haproxy removal succeeded, 1 if nothing was removed anywhere.
    """
    pool_names = BackendState.list_pools(bs.state_dir, bs.lb_host)
    if not pool_names:
        pool_names = [p.name for p in mgr.lb.pools]
    for pname in pool_names:
        pbs = BackendState(bs.state_dir, bs.lb_host, pool=pname)
        if ep in pbs.list():
            return _do_remove(ep, mgr, pbs, pool_name=pname)

    # Not found in any pool state file — attempt haproxy-side cleanup.
    cli = _client(mgr)
    if cli is None:
        print(
            f"endpoint {ep} not found in any pool state file and LB admin socket unreachable",
            file=sys.stderr,
        )
        return 1

    any_removed = False
    for pname in pool_names:
        backend_section = f"pool_{pname}"
        name = _name_for(ep)
        try:
            cli.set_state(backend_section, name, "maint")
            cli.remove_server(backend_section, name)
            print(f"remove {ep} (removed from haproxy only, pool: {pname})", file=sys.stderr)
            any_removed = True
        except Exception:
            pass

    if any_removed:
        return 0
    print(f"endpoint {ep} not found in any pool state file", file=sys.stderr)
    return 1


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
    label = "(new)" if state_result == "new" else "(already present)"
    cli = _client(mgr)
    if cli is not None:
        try:
            cli.add_server(backend_section, _name_for(ep), ep)
        except Exception as e:
            msg = str(e)
            if "already exists" in msg.lower() or "already present" in msg.lower():
                # Idempotent re-add: haproxy already knows this server.
                pass
            elif "no such backend" in msg.lower():
                print(
                    f"haproxy refused add_server for {ep}: {e} (pool section {backend_section!r}"
                    f" missing from haproxy config?)",
                    file=sys.stderr,
                )
                # Roll back state add if it was a fresh insert.
                if state_result == "new":
                    bs.remove(ep)
                return 3
            else:
                print(
                    f"haproxy admin add_server failed for {ep}: {e}",
                    file=sys.stderr,
                )
                # Roll back state add if it was a fresh insert.
                if state_result == "new":
                    bs.remove(ep)
                return 1
        # Force ready: clears any lingering drain/maint from a previous
        # session that crashed mid-detach. Idempotent.
        with contextlib.suppress(Exception):
            cli.set_state(backend_section, _name_for(ep), "ready")
    print(f"add {ep} {label} (pool: {pool_name})", file=sys.stderr)
    return 0


def _do_remove(ep: str, mgr: LbManager, bs: BackendState, pool_name: str | None = None) -> int:
    """Haproxy-first removal: set maint → del server → remove from state file.

    If the admin socket is unreachable the state file is NOT mutated (to avoid
    split-brain).  If any haproxy step fails the state file is also NOT mutated
    (server left in maint state on partial failure).
    """
    pool_name = _resolve_pool_name(mgr, pool_name)
    backend_section = f"pool_{pool_name}"
    cli = _client(mgr)

    if cli is None:
        # A4-family: unreachable socket → clear error, exit 4, no state mutation.
        print(
            f"LB admin socket unreachable at {mgr.sock_path} (and TCP"
            f" {mgr.lb.host}:{mgr.lb.admin.bind_port}); cannot remove {ep}",
            file=sys.stderr,
        )
        return 4

    name = _name_for(ep)

    # Step 1: set server maint (HAProxy requires maint before del server).
    try:
        cli.set_state(backend_section, name, "maint")
    except Exception as e:
        msg = str(e)
        no_such = "no such server" in msg.lower()
        already_maint = "already in maint" in msg.lower()
        if no_such:
            # Server is already absent from haproxy — remove from state file
            # to reconcile, then return 0 (idempotent).
            bs.remove(ep)
            print(
                f"remove {ep} (absent from haproxy, cleaned state) (pool: {pool_name})",
                file=sys.stderr,
            )
            return 0
        if not already_maint:
            print(
                f"haproxy set_state maint failed for {ep}: {e}",
                file=sys.stderr,
            )
            return 1
        # already_maint — continue to del server.

    # Step 2: del server.
    try:
        cli.remove_server(backend_section, name)
    except Exception as e:
        msg = str(e)
        if "no such server" in msg.lower():
            # Already removed from haproxy — reconcile state file.
            bs.remove(ep)
            print(
                f"remove {ep} (absent from haproxy, cleaned state) (pool: {pool_name})",
                file=sys.stderr,
            )
            return 0
        print(
            f"haproxy remove_server failed for {ep} (left in maint): {e}",
            file=sys.stderr,
        )
        return 1

    # Step 3: only update the state file after both haproxy ops succeed.
    bs.remove(ep)
    return 0


def _do_drain(ep: str, mgr: LbManager, pool_name: str | None = None) -> int:
    pool_name = _resolve_pool_name(mgr, pool_name)
    backend_section = f"pool_{pool_name}"
    cli = _client(mgr)
    if cli is None:
        # A4: unreachable socket → clear error, exit 4.
        print(
            f"LB admin socket unreachable at {mgr.sock_path} (and TCP"
            f" {mgr.lb.host}:{mgr.lb.admin.bind_port}); cannot drain {ep}",
            file=sys.stderr,
        )
        return 4
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

    cli = _client(mgr)
    if cli is None:
        # A4-family: unreachable socket → clear error, exit 4.
        print(
            f"LB admin socket unreachable at {mgr.sock_path} (and TCP"
            f" {mgr.lb.host}:{mgr.lb.admin.bind_port}); cannot detach",
            file=sys.stderr,
        )
        return 4

    for pname in pool_names:
        pbs = BackendState(bs.state_dir, bs.lb_host, pool=pname)
        matching = [ep for ep in pbs.list() if ep.startswith(f"{self_ip}:")]
        if not matching:
            continue
        ep = matching[0]
        backend_section = f"pool_{pname}"
        with contextlib.suppress(Exception):
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
                # A7: force-set state ready after add so backends don't linger
                # in maint after a recovery restart.
                with contextlib.suppress(Exception):
                    cli.set_state(backend_section, _name_for(ep), "ready")
    return 0


def _do_health(mgr: LbManager, bs: BackendState) -> int:
    unhealthy = 0
    total = 0
    use_color = sys.stdout.isatty()
    green = "\x1b[32m" if use_color else ""
    red = "\x1b[31m" if use_color else ""
    dim = "\x1b[2m" if use_color else ""
    reset = "\x1b[0m" if use_color else ""

    pools = list(mgr.lb.pools)
    for i, pool in enumerate(pools):
        if i > 0:
            print()  # blank line between pools
        pbs = BackendState(bs.state_dir, bs.lb_host, pool=pool.name)
        eps = pbs.list()
        url = f"http://{mgr.lb.host}:{pool.bind_port}"
        header = f"pool: {pool.name} ({pool.served_model})  {dim}→ {url}{reset}"
        if not eps:
            print(f"{header}  {dim}— (no backends){reset}")
            continue
        print(header)
        # Aligned columns: endpoint | status | /health | model | running
        col_w = max(len(ep) for ep in eps)
        for ep in eps:
            # B9: probe the actual backend host, not localhost.
            host = ep.split(":")[0]
            port = int(ep.rsplit(":", 1)[1])
            probe = probe_vllm(host, port)
            ok = probe.get("healthy", False)
            color = green if ok else red
            marker = "OK  " if ok else "FAIL"
            health = probe.get("health_code", "?")
            loaded = "yes" if probe.get("models_loaded") else "no"
            running = probe.get("num_requests_running", 0.0)
            print(
                f"  {color}{marker}{reset}  {ep:<{col_w}}  "
                f"/health={health}  loaded={loaded}  running={running:.0f}"
            )
            total += 1
            if not ok:
                unhealthy += 1
    # B10: print summary + return 1 if any unhealthy (not the count).
    print(f"unhealthy: {unhealthy} of {total}")
    return 1 if unhealthy else 0
