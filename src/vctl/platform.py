"""Host primitives — IP detection, `which`, tmux helpers."""

from __future__ import annotations

import logging
import re
import shlex
import shutil
import socket
import subprocess

_LOG = logging.getLogger(__name__)

# E3: valid tmux session name pattern — only alphanumerics, hyphens, underscores, dots
_TMUX_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")


def _validate_tmux_name(name: str) -> None:
    """E3: Raise ValueError if name is not a safe tmux session name."""
    if not _TMUX_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid tmux session name {name!r}; must match [A-Za-z0-9_.-]+")


def detect_self_ip(probe_target: str = "8.8.8.8", probe_port: int = 80) -> str:
    """Return the IP this host would use to reach probe_target.

    Fallback chain (D5):
    1. UDP-connect probe to probe_target — works on any routed interface.
    2. ``socket.gethostbyname(socket.gethostname())`` — works on air-gapped hosts.
    3. ``"127.0.0.1"`` — last resort; logs a WARNING.
    """
    # 1. UDP connect probe
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((probe_target, probe_port))
            return str(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass

    # 2. gethostbyname fallback
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        pass

    # 3. Last resort
    _LOG.warning("detect_self_ip: all probes failed; falling back to 127.0.0.1")
    return "127.0.0.1"


def which(binary: str) -> str:
    found = shutil.which(binary)
    if found is None:
        raise FileNotFoundError(f"{binary!r} not on PATH")
    return found


def tmux_session_exists(name: str) -> bool:
    """E3: Check if a tmux session exists by name.

    Raises ValueError on invalid session name.
    Raises RuntimeError if tmux is not installed.
    """
    _validate_tmux_name(name)
    try:
        proc = subprocess.run(
            ["tmux", "has-session", "-t", name],
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0
    except FileNotFoundError:
        raise RuntimeError("tmux not installed") from None


def tmux_run_detached(name: str, cmd: str) -> None:
    """E3: Launch cmd in a new detached tmux session named `name`.

    `cmd` is treated as a shell line and passed as-is to tmux.
    For callers building cmd from path/arg components, use
    :func:`tmux_run_detached_argv` to get safe shlex quoting.

    Raises ValueError on invalid session name.
    Raises RuntimeError if tmux is not installed.
    """
    _validate_tmux_name(name)
    try:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", name, cmd],
            check=True,
        )
    except FileNotFoundError:
        raise RuntimeError("tmux not installed") from None


def tmux_run_detached_argv(name: str, argv: list[str]) -> None:
    """E3: Like :func:`tmux_run_detached` but accepts an argv list.

    Each component is quoted with :func:`shlex.join` before being passed
    to tmux as a shell command, so paths with spaces and special characters
    are handled safely.

    Raises ValueError on invalid session name.
    Raises RuntimeError if tmux is not installed.
    """
    cmd = shlex.join(argv)
    tmux_run_detached(name, cmd)


def tmux_kill(name: str) -> None:
    """E3: Kill a tmux session by name.

    Raises ValueError on invalid session name.
    Raises RuntimeError if tmux is not installed.
    """
    _validate_tmux_name(name)
    try:
        subprocess.run(["tmux", "kill-session", "-t", name], check=False)
    except FileNotFoundError:
        raise RuntimeError("tmux not installed") from None
