"""tmux-backed vllm process supervisor — mirrors LbManager shape."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import subprocess
import time
from pathlib import Path

import httpx
import psutil

from vctl.commands.lb_scaling import _do_add
from vctl.commands.serve import _wait_for_ready
from vctl.lb.manager import LbManager
from vctl.lb.routing import pool_for_model
from vctl.lb.state import BackendState
from vctl.platform import (
    _validate_tmux_name,
    detect_self_ip,
    tmux_kill,
    tmux_run_detached_argv,
    tmux_session_exists,
)
from vctl.resolver import ResolvedConfig

_LOG = logging.getLogger(__name__)

# PID-discovery polling constants — monkeypatchable in tests.
_VLLM_PID_POLL_TIMEOUT: float = 30.0
_VLLM_PID_POLL_INTERVAL: float = 0.5


class VllmManager:
    def __init__(
        self,
        rc: ResolvedConfig,
        state_dir: Path,
        run_dir: Path,
    ) -> None:
        self.session_name = f"vctl-vllm-{rc.profile_name}"
        _validate_tmux_name(self.session_name)
        self.rc = rc
        self.state_dir = Path(state_dir)
        self.run_dir = Path(run_dir)
        self._vllm_dir = self.run_dir / "vllm"
        self._vllm_dir.mkdir(parents=True, exist_ok=True)
        self.pid_path = self._vllm_dir / f"{rc.profile_name}.pid"
        self.log_path = self._vllm_dir / f"{rc.profile_name}.log"
        self.cmd_path = self._vllm_dir / f"{rc.profile_name}.cmd.json"
        self.host_path = self._vllm_dir / f"{rc.profile_name}.host"

    def start(self) -> None:
        """Preflight → spawn tmux → _wait_for_ready → _do_add → write state files."""
        rc = self.rc
        port = rc.server.http_port

        # Stale pidfile cleanup: if pid file exists but process is dead or wrong cmdline.
        if self.pid_path.exists():
            try:
                old_pid = int(self.pid_path.read_text().strip())
                try:
                    os.kill(old_pid, 0)
                    # Process alive — check it's actually vllm serve on our port.
                    try:
                        cmdline = " ".join(psutil.Process(old_pid).cmdline())
                        if "vllm" not in cmdline or f"--port={port}" not in cmdline:
                            raise ProcessLookupError
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        raise ProcessLookupError from None
                except ProcessLookupError:
                    # Stale — remove all state files silently and continue.
                    _LOG.info("removing stale state files for profile %s", rc.profile_name)
                    for p in (self.pid_path, self.cmd_path, self.host_path):
                        with contextlib.suppress(OSError):
                            p.unlink()
            except (ValueError, OSError):
                pass

        # Double-start guard.
        if tmux_session_exists(self.session_name):
            raise RuntimeError(
                f"vllm already running for profile {rc.profile_name!r} "
                f"(tmux session {self.session_name!r}); "
                "use `vctl serve restart` to restart or `vctl serve stop` to stop."
            )

        # Build vllm argv (mirrors _run_foreground).
        env = os.environ.copy()
        venv_bin = str(Path(rc.cluster.venv) / "bin")
        env["PATH"] = f"{venv_bin}:{env['PATH']}"
        if rc.resources.cuda_visible_devices:
            env["CUDA_VISIBLE_DEVICES"] = rc.resources.cuda_visible_devices
        for k, v in rc.env.items():
            if isinstance(v, bool):
                env[k] = "true" if v else "false"
            else:
                env[k] = str(v)

        argv: list[str] = [
            "vllm",
            "serve",
            rc.model.name,
            f"--data-parallel-size={rc.parallelism.data_parallel}",
            f"--tensor-parallel-size={rc.parallelism.tensor_parallel}",
            f"--port={port}",
        ]
        if rc.parallelism.api_server_count is not None:
            argv.append(f"--api-server-count={rc.parallelism.api_server_count}")
        for k, v in rc.vllm_args.items():
            if v is True:
                argv.append(f"--{k}")
            elif v is False:
                argv.append(f"--no-{k}")
            else:
                argv.append(f"--{k}={v}")

        # Spawn the tmux session.
        tmux_run_detached_argv(self.session_name, argv)
        _LOG.info("vllm started in tmux session %s", self.session_name)

        # Set up pipe-pane log capture BEFORE any failure point so we capture early output.
        subprocess.run(
            ["tmux", "pipe-pane", "-t", self.session_name, "-o", f"cat >> {self.log_path}"],
            check=False,
        )

        # Poll psutil for the vllm PID.
        pid: int | None = None
        deadline = time.monotonic() + _VLLM_PID_POLL_TIMEOUT
        while time.monotonic() < deadline:
            for proc in psutil.process_iter(["cmdline", "create_time", "pid"]):
                try:
                    cmd = proc.info.get("cmdline") or []
                    cmd_str = " ".join(cmd)
                    if "vllm" in cmd_str and "serve" in cmd_str and f"--port={port}" in cmd_str:
                        pid = int(proc.pid)
                        break
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            if pid is not None:
                break
            time.sleep(_VLLM_PID_POLL_INTERVAL)

        if pid is None:
            tmux_kill(self.session_name)
            raise RuntimeError(
                f"vllm PID discovery timed out after {_VLLM_PID_POLL_TIMEOUT}s "
                f"for profile {rc.profile_name!r} on port {port}; "
                "check `vctl serve logs` for startup errors."
            )

        # Write state files atomically (write .tmp → os.replace).
        self._write_atomic(self.pid_path, str(pid))
        self._write_atomic(self.cmd_path, json.dumps(argv))
        self._write_atomic(self.host_path, socket.gethostname())

        # Wait for vllm HTTP readiness.
        from vctl.commands.serve import _resolve_ready_timeout

        timeout = _resolve_ready_timeout(rc)
        try:
            _wait_for_ready(port, timeout)
        except TimeoutError as e:
            _LOG.error("vllm readiness timed out: %s", e)
            self._cleanup_on_failure()
            raise RuntimeError(
                f"vllm did not become ready on port {port} within {timeout}s "
                f"for profile {rc.profile_name!r}"
            ) from e

        # Attach to the LB pool.
        state_dir = self.state_dir
        mgr = LbManager(rc.lb, state_dir=state_dir, run_dir=self.run_dir / "lb")
        pool = pool_for_model(rc.lb, rc.model.name)
        bs = BackendState(state_dir, rc.lb.host, pool=pool.name)
        self_ip = detect_self_ip()
        ep = f"{self_ip}:{port}"
        attach_rc = _do_add(ep, mgr, bs, pool_name=pool.name)
        if attach_rc != 0:
            _LOG.error("lb attach failed (rc=%d) for %s; cleaning up", attach_rc, ep)
            self._cleanup_on_failure()
            raise RuntimeError(
                f"lb attach failed (rc={attach_rc}) for endpoint {ep}; vllm session killed."
            )

        _LOG.info(
            "vllm serving profile %r on port %d; attached to pool %r",
            rc.profile_name,
            port,
            pool.name,
        )

    def _write_atomic(self, path: Path, content: str) -> None:
        """Write content to path atomically via a .tmp sibling + os.replace."""
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content)
        os.replace(tmp, path)

    def _cleanup_on_failure(self) -> None:
        """Kill tmux session and unlink state files after a start() failure."""
        tmux_kill(self.session_name)
        for p in (self.pid_path, self.cmd_path, self.host_path):
            with contextlib.suppress(OSError):
                p.unlink()

    def stop(self) -> None:
        """_do_drain → _wait_for_idle → _do_remove → tmux send-keys C-c → poll → kill-session."""
        raise NotImplementedError

    def restart(self) -> None:
        """stop() → reload config → start(). Logs warning if cmd snapshot differs."""
        raise NotImplementedError

    def status(self) -> dict[str, object]:
        """Return tmux_alive, pid_alive, vllm_ready, lb_attached, started_at, log_size."""
        rc = self.rc
        port = rc.server.http_port

        # 1. tmux session liveness (informational).
        tmux_alive = tmux_session_exists(self.session_name)

        # 2. Pidfile + process liveness (read-only; never clean stale state here).
        pid: int | None = None
        pid_alive: bool | None = False
        cross_host = False

        if self.host_path.exists():
            stored_host = self.host_path.read_text().strip()
            if stored_host != socket.gethostname():
                cross_host = True

        if self.pid_path.exists():
            try:
                pid = int(self.pid_path.read_text().strip())
            except (ValueError, OSError):
                pid = None

            if pid is not None:
                if cross_host:
                    pid_alive = None  # Cannot check liveness on a different host.
                else:
                    try:
                        os.kill(pid, 0)
                        pid_alive = True
                    except (ProcessLookupError, PermissionError):
                        pid_alive = False

        # 3. vllm HTTP readiness (1s timeout; read-only probe).
        vllm_ready = False
        try:
            resp = httpx.get(f"http://127.0.0.1:{port}/v1/models", timeout=1.0)
            if resp.json().get("data"):
                vllm_ready = True
        except Exception:
            vllm_ready = False

        # 4. LB attachment — check BackendState for our endpoint.
        self_ip = detect_self_ip()
        ep = f"{self_ip}:{port}"
        bs = BackendState(
            self.state_dir, rc.lb.host, pool=pool_for_model(rc.lb, rc.model.name).name
        )
        lb_attached = ep in bs.list()

        # 5. Log file size (bytes).
        log_size = self.log_path.stat().st_size if self.log_path.exists() else 0

        return {
            "tmux_alive": tmux_alive,
            "pid_alive": pid_alive,
            "vllm_ready": vllm_ready,
            "lb_attached": lb_attached,
            "log_size": log_size,
            "pid": pid,
            "cross_host": cross_host,
            "session_name": self.session_name,
            "log_path": str(self.log_path),
        }

    def attach(self) -> None:
        """os.execvp into tmux attach-session -t <name>. Does not return."""
        raise NotImplementedError

    def logs(self, n: int = 50, follow: bool = False) -> int:
        """Tail log file. follow=True: subprocess.Popen(["tail", "-f", path])."""
        raise NotImplementedError
