"""Atomic, fcntl.flock-protected per-pool backend list."""

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
    def __init__(self, state_dir: Path, lb_host: str, pool: str = "default") -> None:
        self.state_dir = Path(state_dir)
        self.lb_host = lb_host
        self.pool = pool
        # New layout: <state_dir>/<lb_host>/<pool>_backends.txt
        self.host_dir = self.state_dir / lb_host
        self.host_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.host_dir / f"{pool}_backends.txt"
        self.lock_path = self.host_dir / f"{pool}_backends.lock"

    @classmethod
    def migrate_if_needed(cls, state_dir: Path, lb_host: str) -> None:
        """B12: Flock-protected, atomic legacy migration.

        v0.1.0 layout: <state_dir>/<lb_host>_backends.txt (flat).
        Move it under <lb_host>/default_backends.txt on first access.
        Only one process performs the migration; concurrent callers wait on
        the lock then no-op.
        """
        state_dir = Path(state_dir)
        host_dir = state_dir / lb_host
        host_dir.mkdir(parents=True, exist_ok=True)

        legacy = state_dir / f"{lb_host}_backends.txt"
        new_path = host_dir / "default_backends.txt"
        migration_lock = host_dir / "_migration.lock"

        if not legacy.exists():
            return  # fast-path: nothing to migrate

        migration_lock.touch(exist_ok=True)
        with open(migration_lock, "r+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                # Re-check under lock — another process may have finished already.
                if not legacy.exists() or new_path.exists():
                    return
                # Atomic write: copy to temp file then os.replace.
                with tempfile.NamedTemporaryFile(
                    mode="w", dir=host_dir, delete=False, encoding="utf-8"
                ) as tmp:
                    tmp_name = tmp.name
                    try:
                        tmp.write(legacy.read_text(encoding="utf-8"))
                        tmp.flush()
                        os.fsync(tmp.fileno())
                    except Exception:
                        with contextlib.suppress(OSError):
                            os.unlink(tmp_name)
                        raise
                os.replace(tmp_name, new_path)
                # Remove legacy file + its lock if present.
                with contextlib.suppress(OSError):
                    legacy.unlink()
                legacy_lock = state_dir / f"{lb_host}_backends.lock"
                with contextlib.suppress(OSError):
                    legacy_lock.unlink()
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    @contextlib.contextmanager
    def _locked(self) -> Generator[None, None, None]:
        """Hold an exclusive flock on the pool's lock file for the duration."""
        self.lock_path.touch(exist_ok=True)
        with open(self.lock_path, "r+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _read_data(self) -> builtins.list[str]:
        if not self.path.exists():
            return []
        return [
            ln.strip() for ln in self.path.read_text(encoding="utf-8").splitlines() if ln.strip()
        ]

    def _atomic_write(self, entries: builtins.list[str]) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=self.host_dir, delete=False, encoding="utf-8"
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
            entries = self._read_data()
            if ep in entries:
                return "already_present"
            entries.append(ep)
            self._atomic_write(sorted(set(entries)))
            return "new"

    def remove(self, ep: str) -> bool:
        with self._locked():
            entries = self._read_data()
            if ep not in entries:
                return False
            entries.remove(ep)
            self._atomic_write(entries)
            return True

    def list(self) -> builtins.list[str]:
        with self._locked():
            return self._read_data()

    def read(self) -> builtins.list[str]:
        return self.list()

    @classmethod
    def list_pools(cls, state_dir: Path, lb_host: str) -> builtins.list[str]:
        """Return all pool names that currently have a state file."""
        host_dir = Path(state_dir) / lb_host
        if not host_dir.is_dir():
            return []
        out: builtins.list[str] = []
        for f in host_dir.glob("*_backends.txt"):
            out.append(f.stem.removesuffix("_backends"))
        return sorted(out)
