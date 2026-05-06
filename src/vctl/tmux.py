"""TmuxSession — unified tmux session lifecycle management.

Replaces the four helpers (tmux_run_detached, tmux_run_detached_argv,
tmux_kill, tmux_session_exists) and the duplicated env/kill plumbing in
LbManager, VllmManager, and commands/lmmseval.

Requires tmux 3.2+ (deployed env: tmux 3.4).  The -e KEY=VALUE flag for
tmux new-session was introduced in tmux 3.2 and injects env vars into the
new session regardless of the tmux server's own stale environment cache.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
from pathlib import Path

_LOG = logging.getLogger(__name__)
_TMUX_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")

# Module-level tmux version cache — checked once per process.
_TMUX_VERSION_OK: bool | None = None


def _validate_tmux_name(name: str) -> None:
    """Raise ValueError if name is not a safe tmux session name."""
    if not _TMUX_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid tmux session name {name!r}; must match [A-Za-z0-9_.-]+")


def tmux_session_exists(name: str) -> bool:
    """Return True if a tmux session with this name currently exists.

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


def _validate_env(env: dict[str, str]) -> None:
    """Raise ValueError on env entries that would be silently mangled by tmux -e."""
    for k, v in env.items():
        if not k or "=" in k:
            raise ValueError(f"invalid env key {k!r}: empty or contains '='")
        if "\n" in v or "\x00" in v:
            raise ValueError(f"invalid env value for {k!r}: contains newline or NUL")


def _check_tmux_version() -> None:
    """Raise RuntimeError if installed tmux is older than 3.2.

    Result is cached in _TMUX_VERSION_OK after first check so subsequent
    TmuxSession.start() calls in the same process pay no subprocess cost.
    """
    global _TMUX_VERSION_OK
    if _TMUX_VERSION_OK is True:
        return
    try:
        proc = subprocess.run(["tmux", "-V"], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        raise RuntimeError("tmux not installed") from None
    m = re.match(r"tmux (\d+)\.(\d+)", proc.stdout)
    if not m or (int(m.group(1)), int(m.group(2))) < (3, 2):
        raise RuntimeError(f"tmux 3.2+ required; found {proc.stdout.strip()!r}")
    _TMUX_VERSION_OK = True


class TmuxSession:
    """Manage a single detached tmux session with full env injection.

    env=None (default) means os.environ is snapshotted at start() call time —
    NOT at __init__ time.  This is the safe default that eliminates the
    stale-tmux-server-cache footgun.  Callers that need explicit control pass
    env={**os.environ, "KEY": "val"}.

    log_path precondition: the caller must ensure log_path.parent exists before
    calling start().  TmuxSession does not mkdir — per-feature managers own
    their directory layout.
    """

    def __init__(
        self,
        name: str,
        env: dict[str, str] | None = None,
        log_path: Path | None = None,
    ) -> None:
        _validate_tmux_name(name)
        self.name = name
        self._env = env
        self.log_path = log_path

    def exists(self) -> bool:
        """Return True if the tmux session currently exists."""
        return tmux_session_exists(self.name)

    def start(self, argv: list[str] | str) -> None:
        """Spawn a new detached tmux session running argv.

        If argv is a list, elements are joined with shlex.join before being
        passed to tmux (exec-form safety).  If argv is a str, it is passed
        verbatim (shell-line form, for callers like lmmseval that build a
        pipeline with ``source``).

        Raises RuntimeError if the session already exists.
        Raises RuntimeError if tmux is not installed or version < 3.2.
        Raises ValueError on invalid env entries.
        """
        if self.exists():
            raise RuntimeError(
                f"tmux session {self.name!r} already exists; "
                "call kill() first or use a different name"
            )

        _check_tmux_version()

        env = self._env if self._env is not None else dict(os.environ)
        _validate_env(env)
        cmd = shlex.join(argv) if isinstance(argv, list) else argv

        tmux_argv: list[str] = ["tmux", "new-session", "-d", "-s", self.name]
        for k, v in env.items():
            tmux_argv += ["-e", f"{k}={v}"]
        tmux_argv.append(cmd)

        try:
            subprocess.run(tmux_argv, check=True)
        except FileNotFoundError:
            raise RuntimeError("tmux not installed") from None

    def pane_pid(self) -> int | None:
        """Return the PID of the foreground process in the session's first pane.

        Returns None if the session does not exist or the PID cannot be parsed.
        """
        raise NotImplementedError

    def kill(self, *, tree: bool = True, grace_s: float = 5.0) -> None:
        """Terminate the session's process tree then kill the tmux session.

        Idempotent: safe to call when session does not exist.
        """
        raise NotImplementedError
