"""`vctl serve` — launch vllm, attach to LB, drain on signal, reap subtree."""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psutil

from vctl.commands import lb_scaling
from vctl.lb.manager import LbManager
from vctl.lb.state import BackendState
from vctl.platform import detect_self_ip
from vctl.resolver import resolve

_LOG = logging.getLogger(__name__)


def _build_subparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vctl serve")
    p.add_argument("--skip-preflight", action="store_true")
    return p


def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    _build_subparser().parse_args(argv_rest)
    rc = resolve(ns.config, profile=ns.profile)
    state_dir = Path(rc.cluster.state_dir)
    run_dir = Path.home() / ".vctl" / "lb"
    mgr = LbManager(rc.lb, state_dir=state_dir, run_dir=run_dir)
    bs = BackendState(state_dir, rc.lb.host)

    env = os.environ.copy()
    venv_bin = str(Path(rc.cluster.venv) / "bin")
    env["PATH"] = f"{venv_bin}:{env['PATH']}"
    for k, v in rc.env.items():
        env[k] = str(v)

    cmd = [
        "vllm",
        "serve",
        rc.model.name,
        f"--served-model-name={rc.model.served_as}",
        f"--data-parallel-size={rc.parallelism.data_parallel}",
        f"--tensor-parallel-size={rc.parallelism.tensor_parallel}",
        f"--api-server-count={rc.parallelism.api_server_count}",
        f"--port={rc.server.http_port}",
    ]
    for k, v in rc.vllm_args.items():
        if v is True:
            cmd.append(f"--{k}=true")
        elif v is False:
            cmd.append(f"--{k}=false")
        else:
            cmd.append(f"--{k}={v}")

    _LOG.info("spawning %s", " ".join(cmd))
    proc = subprocess.Popen(cmd, env=env)

    # I-1: compute ep and install signal handlers BEFORE waiting for readiness so
    # SIGINT during model loading does not orphan the vllm subprocess.
    self_ip = detect_self_ip()
    ep = f"{self_ip}:{rc.server.http_port}"

    # Mutable cell: False until _do_add has succeeded.
    state = {"attached": False}

    def _shutdown(signum: int, frame: object) -> None:
        if state["attached"]:
            _LOG.info("signal %d received; draining + detaching", signum)
            try:
                lb_scaling._do_drain(ep, mgr)
                _wait_for_idle(
                    rc.server.http_port,
                    timeout=float(os.environ.get("LB_DETACH_WAIT", "30")),
                )
                lb_scaling._do_remove(ep, mgr, bs)
            finally:
                grace = float(os.environ.get("VCTL_KILL_GRACE", "30"))
                _kill_tree(proc.pid, grace=grace)
                sys.exit(130)
        else:
            _LOG.info("signal %d received during model load; reaping subprocess", signum)
            grace = float(os.environ.get("VCTL_KILL_GRACE", "30"))
            _kill_tree(proc.pid, grace=grace)
            sys.exit(130)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGHUP, _shutdown)

    try:
        _wait_for_ready(rc.server.http_port, timeout=120)
    except TimeoutError as e:
        _LOG.error("readiness timed out: %s", e)
        _kill_tree(proc.pid)
        return 1

    lb_scaling._do_add(ep, mgr, bs)
    state["attached"] = True
    # Poll with a short timeout so Python signal handlers can fire between iterations.
    while True:
        try:
            rc_code = proc.wait(timeout=0.5)
            return rc_code
        except subprocess.TimeoutExpired:
            pass  # loop back; pending signal handlers run during the sleep in wait()


def _wait_for_ready(port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"http://localhost:{port}/v1/models", timeout=2.0)
            if r.json().get("data"):
                return
        except Exception as e:
            last_err = e
        time.sleep(0.5)
    raise TimeoutError(f"vllm did not become ready on :{port}: {last_err}")


def _wait_for_idle(port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            text = httpx.get(f"http://localhost:{port}/metrics", timeout=2.0).text
            running = 0.0
            for line in text.splitlines():
                if line.startswith("vllm:num_requests_running "):
                    running = float(line.split()[1])
            if running <= 0.0:
                return
        except Exception:
            return
        time.sleep(1)


def _kill_tree(pid: int, grace: float = 30.0) -> None:
    """TERM the process tree; KILL anything still alive after `grace`."""
    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    children = root.children(recursive=True)
    for p in [root, *children]:
        with contextlib.suppress(psutil.NoSuchProcess):
            p.terminate()
    _, alive = psutil.wait_procs([root, *children], timeout=grace)
    for p in alive:
        with contextlib.suppress(psutil.NoSuchProcess):
            p.kill()
