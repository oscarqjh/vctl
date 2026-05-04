"""``vctl rolling-restart`` — sequential per-pool endpoint restart."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import subprocess  # noqa: F401  (module-level for monkeypatching in later tasks)
import time  # noqa: F401  (module-level for monkeypatching in later tasks)
from pathlib import Path
from typing import Any

from vctl.commands.lb import _fetch_haproxy_stats  # noqa: F401  (re-export for later tasks)
from vctl.lb.runtime import lb_admin_client  # noqa: F401  (re-export for later tasks)

_SESSION_DIR: Path = Path.home() / ".vctl" / "lb" / "rolling-restart"


def _session_path(pool: str) -> Path:
    """Return the JSON session file path for *pool*."""
    return _SESSION_DIR / f"{pool}.json"


class _SessionFile:
    """Atomic, fcntl.flock-protected session file for a single pool.

    Mirrors BackendState._locked() from vctl.lb.state — holds an exclusive
    flock on a sibling <pool>.lock file for every read and write so two
    concurrent invocations for the same pool never race.
    """

    def __init__(self, pool: str) -> None:
        _SESSION_DIR.mkdir(parents=True, exist_ok=True)
        self._path: Path = _SESSION_DIR / f"{pool}.json"
        self._lock_path: Path = _SESSION_DIR / f"{pool}.lock"

    def exists(self) -> bool:
        return self._path.exists()

    def read(self) -> dict[str, Any] | None:
        """Return parsed JSON dict, or None if file absent.

        Raises ValueError on JSON decode error (corrupted file).
        """
        if not self._path.exists():
            return None
        self._lock_path.touch(exist_ok=True)
        with open(self._lock_path, "r+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                raw = self._path.read_text(encoding="utf-8")
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        try:
            return json.loads(raw)  # type: ignore[no-any-return]
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"corrupted session file at {self._path}; use --abort to clear it"
            ) from exc

    def write(self, data: dict[str, Any]) -> None:
        """Atomically write *data* as JSON (via .tmp + os.replace)."""
        self._lock_path.touch(exist_ok=True)
        with open(self._lock_path, "r+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                tmp_path = Path(str(self._path) + ".tmp")
                tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                os.replace(tmp_path, self._path)
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def delete(self) -> None:
        """Remove the session file if present; no-op if absent."""
        with contextlib.suppress(FileNotFoundError):
            self._path.unlink()
