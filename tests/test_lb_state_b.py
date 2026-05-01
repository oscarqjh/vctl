"""Commit-B tests for BackendState: B12 — concurrent migration safety."""

from __future__ import annotations

import multiprocessing as mp
import threading
from pathlib import Path

from vctl.lb.state import BackendState

# ---------------------------------------------------------------------------
# B12: migrate_if_needed is flock-protected and runs only once
# ---------------------------------------------------------------------------


def _run_migration(args: tuple[str, str]) -> str:
    """Worker that calls migrate_if_needed and reads back the result."""
    state_dir_s, lb_host = args
    BackendState.migrate_if_needed(Path(state_dir_s), lb_host)
    new_path = Path(state_dir_s) / lb_host / "default_backends.txt"
    return new_path.read_text() if new_path.exists() else ""


def test_migration_runs_once_under_threading_concurrency(tmp_path: Path) -> None:
    """B12: concurrent threads both calling migrate_if_needed must produce
    exactly one correct default_backends.txt; no corruption."""
    legacy = tmp_path / "host_backends.txt"
    legacy.write_text("10.0.0.1:8000\n10.0.0.2:8000\n")

    barrier = threading.Barrier(4)
    completed: list[bool] = []

    def worker() -> None:
        barrier.wait()  # all start together
        BackendState.migrate_if_needed(tmp_path, "host")
        completed.append(True)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(completed) == 4, "all threads must complete"
    new_path = tmp_path / "host" / "default_backends.txt"
    assert new_path.exists(), "default_backends.txt must exist after migration"
    content = set(new_path.read_text().splitlines())
    assert "10.0.0.1:8000" in content
    assert "10.0.0.2:8000" in content
    # Legacy file must be gone.
    assert not legacy.exists(), "legacy file must be removed after migration"


def test_migration_concurrent_processes(tmp_path: Path) -> None:
    """B12: concurrent processes calling migrate_if_needed produce no corruption."""
    legacy = tmp_path / "host_backends.txt"
    legacy.write_text("10.0.0.1:8000\n10.0.0.2:8000\n")

    args = [(str(tmp_path), "host")] * 4
    ctx = mp.get_context("spawn")
    with ctx.Pool(processes=4) as pool:
        pool.map(_run_migration, args)

    new_path = tmp_path / "host" / "default_backends.txt"
    assert new_path.exists()
    content = set(new_path.read_text().splitlines())
    # Check at least the two endpoints are present (no partial write corruption).
    assert "10.0.0.1:8000" in content
    assert "10.0.0.2:8000" in content
    # Legacy gone.
    assert not legacy.exists()


def test_migration_no_op_when_already_done(tmp_path: Path) -> None:
    """B12: second call to migrate_if_needed is a no-op (no legacy file)."""
    legacy = tmp_path / "host_backends.txt"
    legacy.write_text("10.0.0.1:8000\n")

    BackendState.migrate_if_needed(tmp_path, "host")
    new_path = tmp_path / "host" / "default_backends.txt"
    mtime_after_first = new_path.stat().st_mtime

    # Second call — no legacy file present, should be a fast-path no-op.
    BackendState.migrate_if_needed(tmp_path, "host")
    mtime_after_second = new_path.stat().st_mtime

    assert mtime_after_first == mtime_after_second, "second call must not modify the file"


def test_migration_does_not_overwrite_existing_new_path(tmp_path: Path) -> None:
    """B12: if new path already exists, migration skips (under lock check)."""
    legacy = tmp_path / "host_backends.txt"
    legacy.write_text("new_data:8000\n")
    new_path = tmp_path / "host" / "default_backends.txt"
    new_path.parent.mkdir(parents=True, exist_ok=True)
    new_path.write_text("original:9000\n")

    BackendState.migrate_if_needed(tmp_path, "host")

    # new_path should NOT be overwritten.
    assert new_path.read_text() == "original:9000\n"
    # Legacy should remain since we didn't migrate.
    assert legacy.exists()
