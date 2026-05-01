"""Render config + manage haproxy lifecycle in tmux."""

from __future__ import annotations

import contextlib
import logging
import socket
from pathlib import Path

from vctl.config.models import LbHaproxy
from vctl.lb.installer import ensure_haproxy
from vctl.lb.render import RuntimePaths, render_haproxy_cfg
from vctl.lb.state import BackendState
from vctl.platform import detect_self_ip, tmux_kill, tmux_run_detached, tmux_session_exists

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
        self_ip = detect_self_ip()
        if self_ip != self.lb.host and not force:
            raise RuntimeError(
                f"refusing to start LB: self_ip={self_ip} but lb.host={self.lb.host}; "
                "pass --force to override"
            )
        cfg = self.render_config()
        self.cfg_path.write_text(cfg)
        binary = ensure_haproxy()
        cmd = f"{binary} -f {self.cfg_path} -p {self.pid_path}"
        tmux_run_detached(_TMUX_NAME, cmd)
        _LOG.info("haproxy started in tmux session %s", _TMUX_NAME)

    def stop(self) -> None:
        tmux_kill(_TMUX_NAME)

    def reload(self) -> None:
        if not self.pid_path.exists():
            raise RuntimeError(f"no pidfile at {self.pid_path}; is haproxy running?")
        binary = ensure_haproxy()
        import subprocess

        subprocess.run(
            [
                binary,
                "-f",
                str(self.cfg_path),
                "-p",
                str(self.pid_path),
                "-sf",
                self.pid_path.read_text().strip(),
            ],
            check=True,
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
                    import os

                    with contextlib.suppress(OSError, ProcessLookupError):
                        os.kill(pid, 0)
                        pid_alive = True
            except (ValueError, OSError):
                pid = None

        # 2. admin port reachable
        admin_reachable = False
        with contextlib.suppress(OSError):
            with socket.create_connection(
                (self.lb.host, self.lb.admin.bind_port), timeout=2
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
        }
