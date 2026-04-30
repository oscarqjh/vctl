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


def _do_add(ep: str, mgr: LbManager, bs: BackendState) -> int:
    state_result = bs.add(ep)
    cli = _client(mgr)
    if cli is not None:
        try:
            cli.add_server("pool", _name_for(ep), ep)
        except Exception as e:
            _LOG.error("admin socket add_server failed: %s", e)
    label = "(new)" if state_result == "new" else "(already present)"
    print(f"add {ep} {label}", file=sys.stderr)
    return 0


def _do_remove(ep: str, mgr: LbManager, bs: BackendState) -> int:
    bs.remove(ep)
    cli = _client(mgr)
    if cli is not None:
        with contextlib.suppress(Exception):
            cli.remove_server("pool", _name_for(ep))
    return 0


def _do_drain(ep: str, mgr: LbManager) -> int:
    cli = _client(mgr)
    if cli is not None:
        cli.set_state("pool", _name_for(ep), "drain")
    return 0


def _do_attach(port: int, mgr: LbManager, bs: BackendState) -> int:
    probe = probe_local_vllm(port)
    if not probe.get("models_loaded"):
        print(f"refusing to attach: localhost:{port} model not loaded", file=sys.stderr)
        return 1
    self_ip = detect_self_ip()
    return _do_add(f"{self_ip}:{port}", mgr, bs)


def _do_detach(mgr: LbManager, bs: BackendState) -> int:
    self_ip = detect_self_ip()
    matching = [ep for ep in bs.list() if ep.startswith(f"{self_ip}:")]
    if not matching:
        return 0
    ep = matching[0]
    cli = _client(mgr)
    if cli is not None:
        cli.set_state("pool", _name_for(ep), "drain")
    timeout = float(os.environ.get("LB_DETACH_WAIT", "30"))
    deadline = time.monotonic() + timeout
    port = int(ep.rsplit(":", 1)[1])
    while time.monotonic() < deadline:
        probe = probe_local_vllm(port)
        if probe.get("num_requests_running", 0.0) <= 0.0:
            break
        time.sleep(1)
    return _do_remove(ep, mgr, bs)


def _do_auto_add(mgr: LbManager, bs: BackendState) -> int:
    cli = _client(mgr)
    for ep in bs.list():
        if cli is not None:
            with contextlib.suppress(Exception):
                cli.add_server("pool", _name_for(ep), ep)
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
