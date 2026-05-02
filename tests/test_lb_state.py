"""Atomic backend-state file tests."""

from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

from vctl.lb.state import BackendState


def _add_one(args: tuple[str, str, str]) -> None:
    state_dir, lb_host, ep = args
    BackendState(Path(state_dir), lb_host).add(ep)


def test_add_returns_new_then_already_present(tmp_path: Path) -> None:
    bs = BackendState(tmp_path, "10.0.0.1")
    assert bs.add("10.0.0.5:8000") == "new"
    assert bs.add("10.0.0.5:8000") == "already_present"
    assert bs.list() == ["10.0.0.5:8000"]


def test_remove(tmp_path: Path) -> None:
    bs = BackendState(tmp_path, "10.0.0.1")
    bs.add("a:1")
    bs.add("b:2")
    bs.remove("a:1")
    assert bs.list() == ["b:2"]


def test_concurrent_writers_lose_no_entries(tmp_path: Path) -> None:
    """AT-11: 8 workers each add a unique endpoint; final file has all 8."""
    state_dir = tmp_path
    eps = [f"10.0.0.{i}:8000" for i in range(8)]
    args = [(str(state_dir), "lb-host", ep) for ep in eps]
    with mp.get_context("spawn").Pool(processes=8) as pool:
        pool.map(_add_one, args)
    final = sorted(BackendState(state_dir, "lb-host").list())
    assert final == sorted(eps)


def test_per_pool_state_isolation(tmp_path: Path) -> None:
    a = BackendState(tmp_path, "host", pool="a")
    b = BackendState(tmp_path, "host", pool="b")
    a.add("10.0.0.1:8000")
    b.add("10.0.0.2:8000")
    assert a.list() == ["10.0.0.1:8000"]
    assert b.list() == ["10.0.0.2:8000"]
    assert (tmp_path / "host" / "a_backends.txt").exists()
    assert (tmp_path / "host" / "b_backends.txt").exists()


def test_list_pools_enumerates_state_files(tmp_path: Path) -> None:
    BackendState(tmp_path, "host", pool="a").add("ep:1")
    BackendState(tmp_path, "host", pool="b").add("ep:2")
    assert BackendState.list_pools(tmp_path, "host") == ["a", "b"]
