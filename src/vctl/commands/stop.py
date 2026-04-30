"""`vctl stop` — drain self + reap local vllm subprocess tree."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import psutil

from vctl.commands import lb_scaling
from vctl.commands.serve import _kill_tree, _wait_for_idle
from vctl.lb.manager import LbManager
from vctl.lb.state import BackendState
from vctl.platform import detect_self_ip
from vctl.resolver import resolve


def _build_subparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vctl stop")
    p.add_argument("--json", action="store_true")
    return p


def _find_local_vllm() -> list[int]:
    pids: list[int] = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmd = p.info.get("cmdline") or []
            if cmd and "vllm" in cmd[0] and "serve" in cmd:
                pids.append(p.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return pids


def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    parsed = _build_subparser().parse_args(argv_rest)
    rc = resolve(ns.config, profile=ns.profile)
    state_dir = Path(rc.cluster.state_dir)
    mgr = LbManager(rc.lb, state_dir=state_dir, run_dir=Path.home() / ".vctl" / "lb")
    bs = BackendState(state_dir, rc.lb.host)
    self_ip = detect_self_ip()
    matching = [ep for ep in bs.list() if ep.startswith(f"{self_ip}:")]

    actions: list[str] = []
    if matching:
        ep = matching[0]
        lb_scaling._do_drain(ep, mgr)
        port = int(ep.rsplit(":", 1)[1])
        _wait_for_idle(port, timeout=float(os.environ.get("LB_DETACH_WAIT", "30")))
        lb_scaling._do_remove(ep, mgr, bs)
        actions.append(f"removed {ep}")

    for pid in _find_local_vllm():
        _kill_tree(pid)
        actions.append(f"killed pid {pid}")

    if parsed.json:
        print(json.dumps({"actions": actions}, indent=2))
    else:
        for line in actions:
            print(line)
    return 0
