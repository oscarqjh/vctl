"""Host primitives — IP detection, `which`, tmux helpers."""

from __future__ import annotations

import shutil
import socket
import subprocess


def detect_self_ip(probe_target: str = "8.8.8.8", probe_port: int = 80) -> str:
    """Return the IP this host would use to reach probe_target."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((probe_target, probe_port))
        return str(s.getsockname()[0])
    finally:
        s.close()


def which(binary: str) -> str:
    found = shutil.which(binary)
    if found is None:
        raise FileNotFoundError(f"{binary!r} not on PATH")
    return found


def tmux_session_exists(name: str) -> bool:
    proc = subprocess.run(
        ["tmux", "has-session", "-t", name],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def tmux_run_detached(name: str, cmd: str) -> None:
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, cmd],
        check=True,
    )


def tmux_kill(name: str) -> None:
    subprocess.run(["tmux", "kill-session", "-t", name], check=False)
