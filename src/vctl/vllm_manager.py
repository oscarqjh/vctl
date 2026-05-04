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

from vctl.commands.lb_scaling import _do_add, _do_drain, _do_remove
from vctl.commands.serve import _wait_for_idle, _wait_for_ready
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
        # v0.5.3: state files are host-scoped because run_dir is on shared FS
        # in multi-pod setups (e.g. ~/.vctl/ on /mnt). Without the hostname
        # segment, N pods running the same profile would all write to the
        # same pid/log/cmd files and corrupt each other's state.
        self.hostname = socket.gethostname()
        self._vllm_dir = self.run_dir / "vllm" / self.hostname
        self._vllm_dir.mkdir(parents=True, exist_ok=True)
        self.pid_path = self._vllm_dir / f"{rc.profile_name}.pid"
        self.log_path = self._vllm_dir / f"{rc.profile_name}.log"
        self.cmd_path = self._vllm_dir / f"{rc.profile_name}.cmd.json"
        # Host marker kept for backwards-compat status() reporting + sanity
        # check (path-encoded hostname should equal recorded hostname).
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

        # Build vllm argv. Use absolute path to vllm because tmux session
        # does NOT inherit the operator's PATH — the tmux server has its
        # own minimal env, so plain "vllm" would fail with `command not
        # found` (the v0.5.0/0.5.3 supervisor regression).
        vllm_bin = str(Path(rc.cluster.venv) / "bin" / "vllm")

        # Env overrides that vllm needs.
        # PATH MUST include the venv bin because vllm spawns build tools at
        # runtime (ninja for FlashInfer JIT, nvcc, etc.) via subprocess. Without
        # this, the worker errors with "[Errno 2] No such file or directory:
        # 'ninja'" the moment the model warmup hits a JIT-compile path. The
        # tmux session otherwise inherits only the tmux server's minimal PATH.
        env_overrides: dict[str, str] = {}
        venv_bin = str(Path(rc.cluster.venv) / "bin")
        env_overrides["PATH"] = f"{venv_bin}:" + os.environ.get(
            "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        )
        if rc.resources.cuda_visible_devices:
            env_overrides["CUDA_VISIBLE_DEVICES"] = rc.resources.cuda_visible_devices
        for k, v in rc.env.items():
            if isinstance(v, bool):
                env_overrides[k] = "true" if v else "false"
            else:
                env_overrides[k] = str(v)

        argv: list[str] = [
            vllm_bin,
            "serve",
            rc.model.name,
            f"--data-parallel-size={rc.parallelism.data_parallel}",
            f"--tensor-parallel-size={rc.parallelism.tensor_parallel}",
            f"--port={port}",
        ]
        if rc.parallelism.api_server_count is not None:
            argv.append(f"--api-server-count={rc.parallelism.api_server_count}")
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
            if v is True:
                argv.append(f"--{k}")
            elif v is False:
                argv.append(f"--no-{k}")
            else:
                argv.append(f"--{k}={v}")

        # Wrap argv in `env K=V ... <argv>` so env_overrides take effect inside
        # the tmux session. `env(1)` is on every PATH and accepts inline
        # KEY=VALUE pairs followed by the command + args.
        env_cmd: list[str] = ["env"]
        for k, v in env_overrides.items():
            env_cmd.append(f"{k}={v}")
        env_cmd.extend(argv)

        # Spawn the tmux session.
        tmux_run_detached_argv(self.session_name, env_cmd)
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
            self._cleanup_on_failure()
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
        rc = self.rc
        port = rc.server.http_port

        # Cross-host guard.
        if self.host_path.exists():
            stored_host = self.host_path.read_text().strip()
            if stored_host != socket.gethostname():
                raise RuntimeError(
                    f"refusing operation: state files belong to host {stored_host!r}, "
                    f"current host is {socket.gethostname()!r}. "
                    "Run this command on the correct host."
                )

        # Resolve endpoint from pidfile (best-effort; continue even if missing).
        self_ip = detect_self_ip()
        ep = f"{self_ip}:{port}"

        state_dir = self.state_dir
        run_dir_lb = self.run_dir / "lb"
        mgr = LbManager(rc.lb, state_dir=state_dir, run_dir=run_dir_lb)
        pool = pool_for_model(rc.lb, rc.model.name)
        bs = BackendState(state_dir, rc.lb.host, pool=pool.name)

        # Drain → wait for idle → remove.
        drain_rc = _do_drain(ep, mgr, pool_name=pool.name)
        if drain_rc != 0:
            _LOG.warning("drain returned %d for %s; continuing with stop", drain_rc, ep)
        lb_detach_wait = float(os.environ.get("LB_DETACH_WAIT", "600"))
        _wait_for_idle(port, timeout=lb_detach_wait)
        remove_rc = _do_remove(ep, mgr, bs, pool_name=pool.name)
        if remove_rc != 0:
            _LOG.warning("remove returned %d for %s; continuing with kill", remove_rc, ep)

        # Send C-c to tmux session (graceful SIGINT to vllm).
        subprocess.run(
            ["tmux", "send-keys", "-t", self.session_name, "C-c", ""],
            check=False,
        )

        # Poll for pid exit up to VCTL_KILL_GRACE.
        pid: int | None = None
        if self.pid_path.exists():
            try:
                pid = int(self.pid_path.read_text().strip())
            except (ValueError, OSError):
                pid = None

        grace = float(os.environ.get("VCTL_KILL_GRACE", "30"))
        if pid is not None:
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except (ProcessLookupError, PermissionError):
                    break
                time.sleep(0.5)

        # Force-kill session if still exists.
        tmux_kill(self.session_name)

        # Unlink state files (leave log for post-mortem).
        for p in (self.pid_path, self.cmd_path, self.host_path):
            with contextlib.suppress(OSError):
                p.unlink()

        _LOG.info("stopped vllm for profile %r", rc.profile_name)

    def restart(self) -> None:
        """stop() → reload config → start(). Logs warning if cmd snapshot differs."""
        rc = self.rc
        port = rc.server.http_port

        # Read stored argv snapshot if it exists.
        if self.cmd_path.exists():
            try:
                old_argv: list[str] = json.loads(self.cmd_path.read_text())
            except (json.JSONDecodeError, OSError):
                old_argv = []

            # Compute what the current rc would produce. Must match start()'s argv
            # exactly — including the absolute vllm path — for drift detection.
            new_argv: list[str] = [
                str(Path(rc.cluster.venv) / "bin" / "vllm"),
                "serve",
                rc.model.name,
                f"--data-parallel-size={rc.parallelism.data_parallel}",
                f"--tensor-parallel-size={rc.parallelism.tensor_parallel}",
                f"--port={port}",
            ]
            if rc.parallelism.api_server_count is not None:
                new_argv.append(f"--api-server-count={rc.parallelism.api_server_count}")
            for k, v in rc.vllm_args.items():
                if v is True:
                    new_argv.append(f"--{k}")
                elif v is False:
                    new_argv.append(f"--no-{k}")
                else:
                    new_argv.append(f"--{k}={v}")

            if old_argv != new_argv:
                _LOG.warning(
                    "config drift detected for profile %r: "
                    "running argv differs from current config. "
                    "old=%r  new=%r",
                    rc.profile_name,
                    old_argv,
                    new_argv,
                )

        self.stop()
        self.start()

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

    def console(self) -> None:
        """os.execvp into tmux attach-session -t <name>. Does not return.

        Renamed from attach() in v0.5.1 to avoid CLI confusion with `vctl lb attach`
        (which registers a backend with the LB pool — different operation).
        """
        if not tmux_session_exists(self.session_name):
            raise RuntimeError(
                f"no running session for profile {self.rc.profile_name!r} "
                f"(session {self.session_name!r} not found). "
                "Start vllm first with `vctl serve`."
            )
        os.execvp("tmux", ["tmux", "attach-session", "-t", self.session_name])

    def logs(
        self,
        n: int = 50,
        follow: bool = False,
        prune: bool = False,
        keep: int = 10000,
        prune_all: bool = False,
    ) -> int:
        """Tail log file, or prune it in-place.

        Modes (precedence: prune > follow > default tail):
        - prune=True, prune_all=True  → truncate file to 0 bytes (wipe all logs).
        - prune=True, prune_all=False → keep last `keep` lines, rewriting in-place.
        - follow=True                 → stream new lines via tail -f.
        - default                     → print last `n` lines.

        Prune uses open("r+") + seek + write + truncate to preserve the inode so
        tmux pipe-pane's open file descriptor keeps writing to the same file after
        the prune — the operator continues to see new lines without a restart.

        NOTE: prune and follow are mutually exclusive; --all requires --prune.
        """
        import sys as _sys

        if not self.log_path.exists():
            print(
                f"no log file found at {self.log_path}; "
                f"vllm may not have started yet for profile {self.rc.profile_name!r}",
                file=_sys.stderr,
            )
            return 1

        if prune:
            if prune_all:
                # Truncate to 0 bytes in-place: preserves inode so tmux pipe-pane fd
                # keeps writing to the same file after the prune.
                with self.log_path.open("w"):
                    pass
                print(f"pruned {self.log_path}: removed everything")
                return 0

            # Keep last `keep` lines, rewriting in-place.
            text = self.log_path.read_text(errors="replace")
            lines = text.splitlines(keepends=True)
            if len(lines) <= keep:
                print(
                    f"pruned {self.log_path}: nothing to do "
                    f"(file has {len(lines)} lines, keep={keep})"
                )
                return 0
            trimmed = lines[-keep:]
            # Open for read+write, seek to start, write trimmed content, truncate
            # the remainder. This preserves the inode → tmux pipe-pane's fd keeps
            # writing to the same file → operator continues to see new lines after
            # the prune.  Using os.replace instead would detach the fd from the
            # path, sending new tmux output to the old (now-unlinked) inode.
            with self.log_path.open("r+") as f:
                f.seek(0)
                f.write("".join(trimmed))
                f.truncate()
            print(f"pruned {self.log_path}: kept last {len(trimmed)} lines (was {len(lines)})")
            return 0

        if follow:
            proc = subprocess.Popen(["tail", "-f", str(self.log_path)])
            try:
                proc.wait()
            except KeyboardInterrupt:
                with contextlib.suppress(ProcessLookupError, OSError):
                    proc.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=5)
            return 0

        # Non-follow: read last n lines.
        text = self.log_path.read_text(errors="replace")
        all_lines = text.splitlines()
        tail_lines = all_lines[-n:] if len(all_lines) > n else all_lines
        for line in tail_lines:
            print(line)
        return 0
