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
from vctl.platform import detect_self_ip
from vctl.tmux import TmuxSession, _validate_tmux_name

_LOG = logging.getLogger(__name__)
_TMUX_NAME = "vctl-lb"


class LbManager:
    def __init__(
        self,
        lb: LbHaproxy,
        state_dir: Path,
        run_dir: Path,
        tmux_name: str = "vctl-lb",
    ) -> None:
        _validate_tmux_name(tmux_name)
        self.lb = lb
        self.state_dir = Path(state_dir)
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.cfg_path = self.run_dir / "haproxy.cfg"
        self.pid_path = self.run_dir / "haproxy.pid"
        self.sock_path = self.run_dir / "haproxy.sock"
        self.tmux_name = tmux_name

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
        # E3: use argv form so paths with spaces are quoted correctly.
        # env={**os.environ} ensures the haproxy session inherits the caller's
        # full environment, eliminating the stale-tmux-server-cache footgun
        # (spec §4.1 — LbManager env propagation).
        TmuxSession(self.tmux_name).start(
            [binary, "-f", str(self.cfg_path), "-p", str(self.pid_path)]
        )
        _LOG.info("haproxy started in tmux session %s", self.tmux_name)

    def stop(self) -> None:
        """Stop haproxy reliably.

        HAProxy daemonizes by default — `tmux_kill` alone doesn't reach the
        long-running process (it's session-detached from tmux). Use the
        pidfile to send SIGTERM directly, poll for exit with SIGKILL fallback,
        then clean up the tmux session, pidfile, and admin socket.

        F10: when multiple haproxy processes share the cfg path (reload race),
        SIGTERM all of them oldest-first, then poll each for exit with a shared
        10 s deadline, and SIGKILL any survivors.
        """
        # 1. Collect all haproxy PIDs: pidfile entry + pgrep fallback.
        pids: list[int] = []
        if self.pid_path.exists():
            try:
                raw = self.pid_path.read_text().strip()
                if raw:
                    pids.append(int(raw.split()[0]))
            except (ValueError, OSError) as e:
                _LOG.warning("could not read pidfile %s: %s", self.pid_path, e)
        # F10: find ALL matching haproxy processes (sorted oldest-first).
        all_found = _find_all_haproxy_pids_by_cfg(self.cfg_path)
        for p in all_found:
            if p not in pids:
                pids.append(p)
                _LOG.info(
                    "pidfile missing or incomplete; located haproxy via cfg-path scan: pid=%d",
                    p,
                )

        # SIGTERM all (oldest-first order maintained from _find_all_haproxy_pids_by_cfg).
        for pid in pids:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGTERM)
                _LOG.info("sent SIGTERM to haproxy pid=%d", pid)

        # B3: Poll for exit up to 10s (shared deadline across all pids); SIGKILL survivors.
        if pids:
            deadline = time.monotonic() + 10.0
            remaining = list(pids)
            while remaining and time.monotonic() < deadline:
                still_alive = []
                for pid in remaining:
                    try:
                        os.kill(pid, 0)
                        still_alive.append(pid)
                    except (ProcessLookupError, PermissionError):
                        pass  # gone
                remaining = still_alive
                if remaining:
                    time.sleep(0.5)
            for pid in remaining:
                # Still alive after 10s — escalate to SIGKILL.
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
                    _LOG.warning("haproxy pid=%d did not exit after SIGTERM; sent SIGKILL", pid)
            if remaining:
                time.sleep(0.2)

        # B3/B4: unlink pidfile and admin socket AFTER process is gone.
        with contextlib.suppress(OSError):
            self.pid_path.unlink()
        # B4: admin socket cleanup (haproxy normally does this on clean exit,
        # but matters on SIGKILL/crash).
        with contextlib.suppress(OSError):
            self.sock_path.unlink()

        # 2. Tear down the tmux session if it exists (idempotent).
        # tree=False: haproxy was already terminated via pidfile SIGTERM above;
        # only the empty tmux pane remains.
        TmuxSession(self.tmux_name).kill(tree=False)

    def reload(self) -> None:
        # F10: collect ALL haproxy pids sharing this cfg so we can -sf them all.
        # Pidfile entry is included first; pgrep scan adds any extras.
        current_pids: list[int] = []
        if self.pid_path.exists():
            try:
                raw = self.pid_path.read_text().strip()
                if raw:
                    current_pids.append(int(raw.split()[0]))
            except (ValueError, OSError):
                pass
        for p in _find_all_haproxy_pids_by_cfg(self.cfg_path):
            if p not in current_pids:
                current_pids.append(p)
        if not current_pids:
            raise RuntimeError(
                f"no running haproxy found (pidfile={self.pid_path}, "
                f"cfg={self.cfg_path}); is it running?"
            )
        binary = ensure_haproxy()

        # F9: re-render cluster.yaml + state file → cfg before reload, otherwise
        # `lb reload` after a config edit silently re-execs haproxy on the
        # stale on-disk cfg (the previous bug: editing cluster.yaml then
        # `vctl lb reload` left HAProxy on the old config).
        cfg = self.render_config()
        self.cfg_path.write_text(cfg)

        # B11: precheck config syntax before reload.
        pre = subprocess.run(
            [binary, "-c", "-f", str(self.cfg_path)],
            capture_output=True,
            text=True,
        )
        if pre.returncode != 0:
            raise RuntimeError("haproxy config syntax error:\n" + pre.stderr)

        # F10: pass ALL current pids to -sf (space-separated); haproxy will
        # gracefully drain each old process and take over from all of them.
        # This prevents a reload-race from stacking haproxy processes.
        sf_args = [str(p) for p in current_pids]
        result = subprocess.run(
            [
                binary,
                "-f",
                str(self.cfg_path),
                "-p",
                str(self.pid_path),
                "-sf",
                *sf_args,
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

        # 1b. Fallback: foreground haproxy doesn't write a pidfile (we don't emit
        # `daemon` so the tmux pane keeps haproxy + its stdout). Scan processes
        # for one whose cmdline includes `-f <our cfg_path>`.
        if pid is None or not pid_alive:
            scanned = _find_haproxy_pid_by_cfg(self.cfg_path)
            if scanned is not None:
                pid = scanned
                pid_alive = True

        # 2. admin port reachable
        admin_reachable = False
        with (
            contextlib.suppress(OSError),
            socket.create_connection((self.lb.host, self.lb.admin.bind_port), timeout=2),
        ):
            admin_reachable = True

        # 3. tmux-managed (informational only)
        tmux_managed = TmuxSession(self.tmux_name).exists()

        # F4: is_local_host — True when our IP matches lb.host
        is_local_host = detect_self_ip() == self.lb.host

        return {
            "running": pid_alive or admin_reachable,
            "pid": pid,
            "pid_alive": pid_alive,
            "admin_reachable": admin_reachable,
            "tmux_managed": tmux_managed,
            "cfg_path": str(self.cfg_path),
            "admin_bind": f"{self.lb.admin.bind_addr}:{self.lb.admin.bind_port}",
            "is_local_host": is_local_host,
        }


def _find_all_haproxy_pids_by_cfg(cfg_path: Path) -> list[int]:
    """Return ALL haproxy PIDs whose cmdline includes ``-f <cfg_path>``.

    Results are sorted by ``psutil.Process.create_time()`` oldest-first.
    This covers the case where a back-to-back reload race has left multiple
    haproxy processes all pointing at the same config file.
    """
    import psutil

    target = str(cfg_path)
    matches: list[tuple[float, int]] = []  # (create_time, pid)
    for proc in psutil.process_iter(["name", "cmdline", "create_time"]):
        try:
            cmd = proc.info.get("cmdline") or []
            if not cmd:
                continue
            if "haproxy" not in (proc.info.get("name") or "") and not any(
                "haproxy" in part for part in cmd[:1]
            ):
                continue
            for i, tok in enumerate(cmd):
                if tok == "-f" and i + 1 < len(cmd) and cmd[i + 1] == target:
                    ct = proc.info.get("create_time") or 0.0
                    matches.append((ct, int(proc.pid)))
                    break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    matches.sort(key=lambda x: x[0])  # oldest first
    return [pid for _, pid in matches]


def _find_haproxy_pid_by_cfg(cfg_path: Path) -> int | None:
    """Locate a running haproxy process whose cmdline includes ``-f <cfg_path>``.

    Returns the YOUNGEST (most recently created) match — that is the process
    currently bound to the cfg and accepting traffic; older siblings are
    mid-shutdown orphans from reload races.

    Used as a fallback when the pidfile is missing/empty — haproxy without the
    ``daemon`` directive runs in the foreground and never writes the ``-p``
    pidfile, so we have to discover the PID by scanning processes.
    """
    pids = _find_all_haproxy_pids_by_cfg(cfg_path)
    if not pids:
        return None
    return pids[-1]  # youngest last (sorted oldest-first)


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
