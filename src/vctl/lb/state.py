"""Atomic, fcntl.flock-protected backend list."""

from __future__ import annotations

import builtins
import contextlib
import fcntl
import os
import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import Literal


class BackendState:
    def __init__(self, state_dir: Path, lb_host: str) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / f"{lb_host}_backends.txt"
        self._lock_path = self.state_dir / f"{lb_host}_backends.lock"

    @contextlib.contextmanager
    def _locked(self) -> Generator[None, None, None]:
        """Hold an exclusive flock on a stable lock file for the duration."""
        with open(self._lock_path, "a", encoding="utf-8") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    def _read_path(self) -> list[str]:
        if not self.path.exists():
            return []
        return [
            line.strip()
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _atomic_write(self, entries: list[str]) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=self.state_dir, delete=False, encoding="utf-8"
        ) as tmp:
            tmp_name = tmp.name
            try:
                tmp.write("\n".join(entries) + ("\n" if entries else ""))
                tmp.flush()
                os.fsync(tmp.fileno())
            except Exception:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
                raise
        os.replace(tmp_name, self.path)

    def add(self, ep: str) -> Literal["new", "already_present"]:
        with self._locked():
            entries = self._read_path()
            if ep in entries:
                return "already_present"
            entries.append(ep)
            self._atomic_write(sorted(set(entries)))
            return "new"

    def remove(self, ep: str) -> bool:
        with self._locked():
            entries = self._read_path()
            if ep not in entries:
                return False
            entries.remove(ep)
            self._atomic_write(entries)
            return True

    def list(self) -> builtins.list[str]:
        with self._locked():
            return self._read_path()

    def read(self) -> builtins.list[str]:
        return self.list()
