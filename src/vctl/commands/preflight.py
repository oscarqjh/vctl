"""`vctl preflight` — sanity checks: GPUs, /dev/shm, venv, lb route, vllm port."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
from pathlib import Path

from vctl.resolver import resolve


def _build_subparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vctl preflight",
        description=(
            "Run sanity checks before launching a vllm inference server:\n"
            "  gpus       — nvidia-smi present (or num_gpus=0)\n"
            "  shm        — /dev/shm ≥ 8 GB\n"
            "  venv       — cluster.venv path exists\n"
            "  lb_route   — TCP connection to lb.host:pool.bind_port succeeds\n"
            "  vllm_port  — server.http_port is free on localhost (no stale vllm)\n"
            "\n"
            "Exit 0 when all checks pass, exit 4 when any check fails.\n"
        ),
    )
    p.add_argument("--json", action="store_true", help="Emit results as JSON")
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


def _check_vllm_port_free(port: int) -> tuple[bool, str]:
    """Verify server.http_port is NOT already in use on localhost.

    Without this check, a stale vllm from a previous run silently steals
    the port: the new `vllm serve` subprocess fails to bind, but
    `_wait_for_ready` happily polls the stale process and returns success,
    so vctl serve registers the *stale* vllm as the new endpoint. v0.4.10
    catches this at preflight time so the operator gets a clear error and
    can run `vctl stop` (or kill the stale process) before retrying.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # SO_REUSEADDR mimics what vllm/uvicorn does when binding. We test bind
        # on 127.0.0.1 specifically (vllm typically binds on all interfaces;
        # 127.0.0.1 is sufficient to detect any conflict).
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as e:
            return (
                False,
                f"localhost:{port} already in use ({e}); "
                f"run `vctl stop` or kill the stale process first",
            )
        return (True, f"localhost:{port} free")
    finally:
        sock.close()


def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    parsed = _build_subparser().parse_args(argv_rest)
    rc = resolve(ns.config, profile=ns.profile)
    checks = [
        ("gpus", *_check_gpus(rc.resources.num_gpus)),
        ("shm", *_check_shm()),
        ("venv", *_check_venv(rc.cluster.venv)),
        ("lb_route", *_check_lb_route(rc.lb.host, rc.lb.pools[0].bind_port)),
        ("vllm_port", *_check_vllm_port_free(rc.server.http_port)),
    ]
    payload = {"checks": [{"name": n, "ok": ok, "msg": m} for (n, ok, m) in checks]}
    if parsed.json:
        print(json.dumps(payload, indent=2))
    else:
        for name, ok, msg in checks:
            mark = "OK" if ok else "FAIL"
            print(f"[{mark}] {name}: {msg}")
    return 0 if all(c["ok"] for c in payload["checks"]) else 4  # C8: environment error → exit 4
