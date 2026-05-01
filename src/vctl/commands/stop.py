"""`vctl stop` — drain self + reap local vllm subprocess tree."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import psutil

from vctl.commands import lb_scaling
from vctl.commands.serve import _kill_tree, _wait_for_idle
from vctl.lb.manager import LbManager
from vctl.lb.state import BackendState
from vctl.platform import detect_self_ip
from vctl.resolver import resolve


def _build_subparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vctl stop",
        description=(
            "Drain and remove this host's vllm endpoint(s) from all LB pools,\n"
            "then kill the local vllm subprocess tree.\n"
            "\n"
            "Env vars:\n"
            "  LB_DETACH_WAIT   — seconds to wait for in-flight requests to drain\n"
            "  VCTL_KILL_GRACE  — SIGTERM→SIGKILL grace period in seconds\n"
        ),
    )
    p.add_argument("--json", action="store_true", help="Emit results as JSON")
    return p


def _find_local_vllm(port: int) -> list[int]:
    pids: list[int] = []
    port_flag = f"--port={port}"
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmd = p.info.get("cmdline") or []
            if not cmd or "vllm" not in cmd[0]:
                continue
            if "serve" not in cmd:
                continue
            if not any(arg == port_flag for arg in cmd):
                continue
            pids.append(p.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return pids


def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    parsed = _build_subparser().parse_args(argv_rest)
    rc = resolve(ns.config, profile=ns.profile)
    state_dir = Path(rc.cluster.state_dir)
    mgr = LbManager(rc.lb, state_dir=state_dir, run_dir=Path.home() / ".vctl" / "lb")
    self_ip = detect_self_ip()

    actions: list[str] = []
    errors: list[str] = []

    # Iterate every registered pool rather than hard-coding "default".
    pool_names = BackendState.list_pools(state_dir, rc.lb.host)
    for pname in pool_names:
        bs = BackendState(state_dir, rc.lb.host, pool=pname)
        matching = [ep for ep in bs.list() if ep.startswith(f"{self_ip}:")]
        for ep in matching:
            drain_rc = lb_scaling._do_drain(ep, mgr, pool_name=pname)
            if drain_rc != 0:
                msg = f"drain failed (exit {drain_rc}) for {ep} in pool {pname}"
                print(msg, file=sys.stderr)
                errors.append(msg)
            else:
                port = int(ep.rsplit(":", 1)[1])
                _wait_for_idle(port, timeout=float(os.environ.get("LB_DETACH_WAIT", "30")))
            remove_rc = lb_scaling._do_remove(ep, mgr, bs, pool_name=pname)
            if remove_rc != 0:
                msg = f"remove failed (exit {remove_rc}) for {ep} in pool {pname}"
                print(msg, file=sys.stderr)
                errors.append(msg)
            else:
                actions.append(f"removed {ep} (pool: {pname})")

    # Always kill local vllm processes even if drain/remove had errors.
    for pid in _find_local_vllm(rc.server.http_port):
        _kill_tree(pid)
        actions.append(f"killed pid {pid}")

    if parsed.json:
        print(json.dumps({"actions": actions, "errors": errors}, indent=2))
    else:
        for line in actions:
            print(line)
    return 1 if errors else 0
