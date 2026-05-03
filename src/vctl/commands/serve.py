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
from vctl.lb.routing import pool_for_model
from vctl.lb.state import BackendState
from vctl.platform import detect_self_ip
from vctl.resolver import resolve

_LOG = logging.getLogger(__name__)


def _build_subparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vctl serve",
        description=(
            "Spawn a vllm inference server, wait for readiness, attach to the LB pool,\n"
            "then wait for the subprocess to exit.  On SIGINT/SIGTERM/SIGHUP the backend\n"
            "is drained and removed from the pool before the process tree is killed.\n"
            "\n"
            "Signal handling:\n"
            "  SIGINT / SIGTERM / SIGHUP — graceful drain → detach → kill subprocess tree\n"
            "\n"
            "Relevant env vars:\n"
            "  VCTL_READY_TIMEOUT      — readiness poll timeout in seconds (default: 1800)\n"
            "  VLLM_ENGINE_READY_TIMEOUT_S — per-profile override (wins over VCTL_READY_TIMEOUT)\n"
            "  LB_DETACH_WAIT          — seconds to wait for in-flight requests to drain\n"
            "  VCTL_KILL_GRACE         — SIGTERM→SIGKILL grace period in seconds\n"
        ),
    )
    p.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip the preflight checks (GPU, /dev/shm, venv, LB route) before serving",
    )
    return p


def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    parsed = _build_subparser().parse_args(argv_rest)
    return _run_foreground(ns, parsed)


def _run_foreground(ns: argparse.Namespace, parsed: argparse.Namespace) -> int:
    # C6: wire --skip-preflight.  When not skipped, run preflight checks first.
    if not parsed.skip_preflight:
        from vctl.commands import preflight as _preflight

        pf_rc = _preflight.run(ns, [])
        if pf_rc != 0:
            _LOG.error("preflight checks failed (exit %d); aborting serve", pf_rc)
            return pf_rc

    rc = resolve(ns.config, profile=ns.profile)

    # FAIL-FAST POOL ROUTING — before subprocess.
    # If the cluster has no pool serving this profile's model, exit 3
    # immediately (don't waste a multi-minute model load).
    state_dir = Path(rc.cluster.state_dir)
    run_dir = Path.home() / ".vctl" / "lb"
    mgr = LbManager(rc.lb, state_dir=state_dir, run_dir=run_dir)
    pool = pool_for_model(rc.lb, rc.model.name)
    bs = BackendState(state_dir, rc.lb.host, pool=pool.name)

    env = os.environ.copy()
    venv_bin = str(Path(rc.cluster.venv) / "bin")
    env["PATH"] = f"{venv_bin}:{env['PATH']}"
    # Export CUDA_VISIBLE_DEVICES from profile.resources so vllm sees the
    # intended GPU set. Without this, vllm picks up the inherited env value
    # (often unset or overly broad), which can break shm setup for
    # --mm-processor-cache-type=shm and tensor-parallel placement.
    if rc.resources.cuda_visible_devices:
        env["CUDA_VISIBLE_DEVICES"] = rc.resources.cuda_visible_devices
    for k, v in rc.env.items():
        # D12: booleans must serialize as "true"/"false" (lowercase) for env export
        if isinstance(v, bool):
            env[k] = "true" if v else "false"
        else:
            env[k] = str(v)

    # Don't pass --served-model-name; vllm defaults to model.name (the canonical
    # HF id), which matches our pool routing key. Setting it explicitly is redundant.
    cmd = [
        "vllm",
        "serve",
        rc.model.name,
        f"--data-parallel-size={rc.parallelism.data_parallel}",
        f"--tensor-parallel-size={rc.parallelism.tensor_parallel}",
        f"--port={rc.server.http_port}",
    ]
    # api_server_count is optional. When omitted vllm defaults to data_parallel,
    # which is what we want. Forcing api_server_count=1 with data_parallel>1 +
    # mm-processor-cache-type=shm hits a vllm 0.19.x bug: renderer (in API
    # server) computes cache_type="processor_only" while workers compute "shm",
    # so workers FileNotFoundError on shm_open against a shm the writer never
    # made. See vllm multimodal/registry.py:_get_cache_type.
    if rc.parallelism.api_server_count is not None:
        cmd.append(f"--api-server-count={rc.parallelism.api_server_count}")
        if (
            rc.parallelism.api_server_count == 1
            and rc.parallelism.data_parallel > 1
            and rc.vllm_args.get("mm-processor-cache-type") == "shm"
        ):
            _LOG.warning(
                "config will hit vllm shm bug: api_server_count=1 + data_parallel=%d + "
                "mm-processor-cache-type=shm. Remove api_server_count from the profile "
                "(vllm will default to data_parallel), or change mm-processor-cache-type "
                "to 'lru'. Continuing anyway — vllm WILL crash with FileNotFoundError "
                "on shm_open.",
                rc.parallelism.data_parallel,
            )
    for k, v in rc.vllm_args.items():
        # vLLM uses argparse BooleanOptionalAction for boolean flags
        # (e.g. --enable-prefix-caching / --no-enable-prefix-caching).
        # `--flag=true` is rejected with "ignored explicit argument 'true'".
        if v is True:
            cmd.append(f"--{k}")
        elif v is False:
            cmd.append(f"--no-{k}")
        else:
            cmd.append(f"--{k}={v}")

    _LOG.info("spawning %s", " ".join(cmd))
    # D11: place vllm in its own PGID so SIGINT to vctl doesn't double-deliver
    # via the terminal foreground-PG. v0.4.12 changed from start_new_session=True
    # (setsid) to preexec_fn=os.setpgrp on a wrong hypothesis about a vllm shm
    # bug — the actual cause was api_server_count=1 + DP>1 + shm-cache (see
    # v0.4.13 CHANGELOG). The setpgrp change is harmless and gives the same
    # SIGINT isolation as setsid without detaching the controlling tty, so
    # we keep it.
    proc = subprocess.Popen(cmd, env=env, preexec_fn=os.setpgrp)

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
                lb_scaling._do_drain(ep, mgr, pool_name=pool.name)
                _wait_for_idle(
                    rc.server.http_port,
                    timeout=float(os.environ.get("LB_DETACH_WAIT", "600")),
                )
                lb_scaling._do_remove(ep, mgr, bs, pool_name=pool.name)
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
        _wait_for_ready(rc.server.http_port, timeout=_resolve_ready_timeout(rc))
    except TimeoutError as e:
        _LOG.error("readiness timed out: %s", e)
        _kill_tree(proc.pid)
        return 4  # C8: environment error (timeout) → exit 4

    # Phase 2 v0.3.0: _do_add now returns 4 if LB unreachable (was: silent
    # state-file write). Check the exit code — if attach fails, kill vllm and
    # exit rather than serving traffic that never reaches haproxy.
    attach_rc = lb_scaling._do_add(ep, mgr, bs, pool_name=pool.name)
    if attach_rc != 0:
        _LOG.error("lb attach failed (rc=%d) for %s; shutting down vllm", attach_rc, ep)
        _kill_tree(proc.pid)
        return attach_rc
    state["attached"] = True

    # F8: PPID watchdog — detect orphan parent (PPID==1 means the launching shell
    # died and init reaped the parent).  Check every ~10 iterations of the 0.5s
    # poll loop (≈5s resolution) and trigger the same drain+kill shutdown path
    # as SIGTERM.  Opt-out: VCTL_NO_PPID_WATCHDOG=1 (for containers where
    # PPID=1 is legitimate, e.g. processes run directly under tini/init).
    _watchdog_enabled = os.environ.get("VCTL_NO_PPID_WATCHDOG", "0") not in ("1", "true", "yes")
    _watchdog_tick = 0

    # Poll with a short timeout so Python signal handlers can fire between iterations.
    while True:
        try:
            rc_code = proc.wait(timeout=0.5)
            return rc_code
        except subprocess.TimeoutExpired:
            pass  # loop back; pending signal handlers run during the sleep in wait()

        # F8: check PPID every ~10 poll ticks (≈5 s).
        if _watchdog_enabled:
            _watchdog_tick += 1
            if _watchdog_tick >= 10:
                _watchdog_tick = 0
                if os.getppid() == 1:
                    _LOG.warning(
                        "PPID==1: launching shell appears to have died; "
                        "triggering graceful drain+shutdown"
                    )
                    _shutdown(signal.SIGTERM, None)


_READY_TIMEOUT_DEFAULT = 1800


def _resolve_ready_timeout(rc: object) -> float:
    """Return the readiness-poll timeout in seconds.

    Precedence (first wins):
    1. ``rc.env["VLLM_ENGINE_READY_TIMEOUT_S"]`` — per-profile override in the
       cluster YAML (e.g. for large models that need more than 30 min).
    2. ``VCTL_READY_TIMEOUT`` OS environment variable — operator-level override.
    3. Hard default: 1800 s (30 min).

    On parse failure the value is ignored with a warning and the default is used.
    """
    default = float(_READY_TIMEOUT_DEFAULT)

    # 1. Profile env dict (populated from cluster YAML rc.env).
    env_dict: dict[str, object] = {}
    if hasattr(rc, "env") and isinstance(getattr(rc, "env", None), dict):
        env_dict = rc.env
    raw: object = env_dict.get("VLLM_ENGINE_READY_TIMEOUT_S")
    if raw is None:
        # 2. OS environment variable.
        raw = os.environ.get("VCTL_READY_TIMEOUT")
    if raw is not None:
        try:
            return float(int(str(raw)))
        except (ValueError, TypeError):
            _LOG.warning(
                "invalid ready-timeout value %r; falling back to %ss", raw, _READY_TIMEOUT_DEFAULT
            )
    return default


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
    # D10: root may have exited between Process() and children(); tolerate that.
    try:
        children = root.children(recursive=True)
    except psutil.NoSuchProcess:
        children = []
    for p in [root, *children]:
        with contextlib.suppress(psutil.NoSuchProcess):
            p.terminate()
    _, alive = psutil.wait_procs([root, *children], timeout=grace)
    for p in alive:
        with contextlib.suppress(psutil.NoSuchProcess):
            p.kill()
