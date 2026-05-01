"""Render config + manage haproxy lifecycle in tmux."""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

from vctl.config.models import LbHaproxy
from vctl.lb.installer import ensure_haproxy
from vctl.lb.render import RuntimePaths, render_haproxy_cfg
from vctl.lb.state import BackendState
from vctl.platform import (
    detect_self_ip,
    tmux_kill,
    tmux_run_detached_argv,
    tmux_session_exists,
)

_LOG = logging.getLogger(__name__)
_TMUX_NAME = "vctl-lb"


class LbManager:
    def __init__(self, lb: LbHaproxy, state_dir: Path, run_dir: Path) -> None:
        self.lb = lb
        self.state_dir = Path(state_dir)
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.cfg_path = self.run_dir / "haproxy.cfg"
        self.pid_path = self.run_dir / "haproxy.pid"
        self.sock_path = self.run_dir / "haproxy.sock"
        # B12: run legacy migration once at manager init (flock-protected inside BackendState)
        BackendState.migrate_if_needed(self.state_dir, self.lb.host)

    @property
    def runtime_paths(self) -> RuntimePaths:
        return RuntimePaths(unix_socket=str(self.sock_path), pid_file=str(self.pid_path))

    def is_host(self) -> bool:
        return detect_self_ip() == self.lb.host

    def render_config(self) -> str:
        backends_by_pool: dict[str, list[str]] = {}
        for pool in self.lb.pools:
            bs = BackendState(self.state_dir, self.lb.host, pool=pool.name)
            backends_by_pool[pool.name] = bs.list()
        return render_haproxy_cfg(self.lb, self.runtime_paths, backends_by_pool)

    def start(self, force: bool = False) -> None:
        # B1: double-start guard — check if already running.
        status = self.status()
        if status["running"]:
            if force:
                _LOG.info(
                    "haproxy already running (pid=%s); stopping before restart",
                    status["pid"],
                )
                self.stop()
            else:
                raise RuntimeError(
                    f"haproxy already running (pid={status['pid']}); "
                    "call stop() first or pass force=True"
                )

        self_ip = detect_self_ip()
        if self_ip != self.lb.host and not force:
            raise RuntimeError(
                f"refusing to start LB: self_ip={self_ip} but lb.host={self.lb.host}; "
                "pass --force to override"
            )

        # E1: warn when admin socket is bound on all interfaces with level admin
        if self.lb.admin.bind_addr == "0.0.0.0":
            _LOG.warning(
                "HAProxy admin socket bound on 0.0.0.0:%d with level admin — "
                "ensure this port is firewalled or set lb.admin.bind_addr to 127.0.0.1",
                self.lb.admin.bind_port,
            )

        cfg = self.render_config()
        self.cfg_path.write_text(cfg)
        binary = ensure_haproxy()
        # E3: use argv form so paths with spaces are quoted correctly
        tmux_run_detached_argv(
            _TMUX_NAME,
            [binary, "-f", str(self.cfg_path), "-p", str(self.pid_path)],
        )
        _LOG.info("haproxy started in tmux session %s", _TMUX_NAME)

    def stop(self) -> None:
        """Stop haproxy reliably.

        HAProxy daemonizes by default — `tmux_kill` alone doesn't reach the
        long-running process (it's session-detached from tmux). Use the
        pidfile to send SIGTERM directly, poll for exit with SIGKILL fallback,
        then clean up the tmux session, pidfile, and admin socket.
        """
        # 1. SIGTERM the haproxy pid if pidfile is good.
        pid: int | None = None
        if self.pid_path.exists():
            try:
                raw = self.pid_path.read_text().strip()
                if raw:
                    pid = int(raw.split()[0])
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.kill(pid, signal.SIGTERM)
                        _LOG.info("sent SIGTERM to haproxy pid=%d", pid)
            except (ValueError, OSError) as e:
                _LOG.warning("could not read pidfile %s: %s", self.pid_path, e)

        # B3: Poll for exit up to 10s; SIGKILL fallback if still alive.
        if pid is not None:
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                except PermissionError:
                    break
                time.sleep(0.5)
            else:
                # Still alive after 10s — escalate to SIGKILL.
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
                    _LOG.warning("haproxy pid=%d did not exit after SIGTERM; sent SIGKILL", pid)
                time.sleep(0.2)

        # B3/B4: unlink pidfile and admin socket AFTER process is gone.
        with contextlib.suppress(OSError):
            self.pid_path.unlink()
        # B4: admin socket cleanup (haproxy normally does this on clean exit,
        # but matters on SIGKILL/crash).
        with contextlib.suppress(OSError):
            self.sock_path.unlink()

        # 2. Tear down the tmux session if it exists (idempotent).
        tmux_kill(_TMUX_NAME)

    def reload(self) -> None:
        if not self.pid_path.exists():
            raise RuntimeError(f"no pidfile at {self.pid_path}; is haproxy running?")
        binary = ensure_haproxy()

        # B11: precheck config syntax before reload.
        pre = subprocess.run(
            [binary, "-c", "-f", str(self.cfg_path)],
            capture_output=True,
            text=True,
        )
        if pre.returncode != 0:
            raise RuntimeError("haproxy config syntax error:\n" + pre.stderr)

        # Reload with stdout/stderr captured; surface failures.
        result = subprocess.run(
            [
                binary,
                "-f",
                str(self.cfg_path),
                "-p",
                str(self.pid_path),
                "-sf",
                self.pid_path.read_text().strip(),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode,
                result.args,
                output=result.stdout,
                stderr=result.stderr,
            )

    def status(self) -> dict[str, object]:
        """Comprehensive status: detect haproxy regardless of who started it.

        Checks (in order):
          1. pidfile present + the pid is alive  → process truth
          2. admin port reachable (TCP)          → service truth
          3. tmux-managed via `vctl-lb` session   → ownership flag

        `running` is True if either pidfile alive OR admin port reachable.
        """
        # 1. pidfile + alive
        pid: int | None = None
        pid_alive = False
        if self.pid_path.exists():
            try:
                raw = self.pid_path.read_text().strip()
                if raw:
                    pid = int(raw.split()[0])
                    # Signal 0 = does the process exist + are we allowed to signal it
                    try:
                        os.kill(pid, 0)
                        # B5: verify /proc/<pid>/comm to avoid PID-recycle false positives.
                        pid_alive = _verify_pid_is_haproxy(pid)
                    except (ProcessLookupError, PermissionError):
                        pid_alive = False
            except (ValueError, OSError):
                pid = None

        # 2. admin port reachable
        admin_reachable = False
        with (
            contextlib.suppress(OSError),
            socket.create_connection((self.lb.host, self.lb.admin.bind_port), timeout=2),
        ):
            admin_reachable = True

        # 3. tmux-managed (informational only)
        tmux_managed = tmux_session_exists(_TMUX_NAME)

        return {
            "running": pid_alive or admin_reachable,
            "pid": pid,
            "pid_alive": pid_alive,
            "admin_reachable": admin_reachable,
            "tmux_managed": tmux_managed,
            "cfg_path": str(self.cfg_path),
            "admin_bind": f"{self.lb.admin.bind_addr}:{self.lb.admin.bind_port}",
        }


def _verify_pid_is_haproxy(pid: int) -> bool:
    """B5: Check /proc/<pid>/comm to guard against PID reuse.

    Returns True if the process appears to be haproxy (or if /proc is
    unavailable, e.g. on macOS — conservatively trusts the signal-0 result).
    """
    try:
        comm = Path(f"/proc/{pid}/comm").read_text().strip()
        return "haproxy" in comm
    except OSError:
        # /proc not available (macOS) or process vanished — trust signal 0
        return True
