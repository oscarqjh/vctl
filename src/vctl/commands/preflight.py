"""`vctl preflight` — sanity checks: GPUs, /dev/shm, venv, lb route."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
from pathlib import Path

from vctl.resolver import resolve


def _build_subparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vctl preflight")
    p.add_argument("--json", action="store_true")
    return p


def _check_gpus(num_gpus: int) -> tuple[bool, str]:
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return (num_gpus == 0, "nvidia-smi not found (ok only if num_gpus=0)")
    return (True, "nvidia-smi present")


def _check_shm() -> tuple[bool, str]:
    try:
        st = os.statvfs("/dev/shm")
        size_gb = st.f_blocks * st.f_frsize / 1e9
        return (size_gb >= 8, f"/dev/shm = {size_gb:.1f} GB")
    except OSError as e:
        return (False, str(e))


def _check_venv(path: str) -> tuple[bool, str]:
    p = Path(path)
    return (p.exists(), f"venv {path}")


def _check_lb_route(host: str, port: int) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=2):
            return (True, f"tcp {host}:{port} reachable")
    except OSError as e:
        return (False, f"tcp {host}:{port}: {e}")


def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    parsed = _build_subparser().parse_args(argv_rest)
    rc = resolve(ns.config, profile=ns.profile)
    checks = [
        ("gpus", *_check_gpus(rc.resources.num_gpus)),
        ("shm", *_check_shm()),
        ("venv", *_check_venv(rc.cluster.venv)),
        ("lb_route", *_check_lb_route(rc.lb.host, rc.lb.client.bind_port)),
    ]
    payload = {"checks": [{"name": n, "ok": ok, "msg": m} for (n, ok, m) in checks]}
    if parsed.json:
        print(json.dumps(payload, indent=2))
    else:
        for name, ok, msg in checks:
            mark = "OK" if ok else "FAIL"
            print(f"[{mark}] {name}: {msg}")
    return 0 if all(c["ok"] for c in payload["checks"]) else 3
