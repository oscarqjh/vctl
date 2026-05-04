"""Tests for rolling-restart session file helpers (Task 1)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers (import lazily so collection doesn't blow up before module exists)
# ---------------------------------------------------------------------------

def _session_path(pool: str, base: Path) -> Path:
    from vctl.commands.rolling_restart import _session_path as _sp
    # Override SESSION_DIR in-test: re-use the helper with monkeypatched constant
    import vctl.commands.rolling_restart as rr
    orig = rr._SESSION_DIR
    rr._SESSION_DIR = base / "rolling-restart"
    try:
        return _sp(pool)
    finally:
        rr._SESSION_DIR = orig


# Test 1
def test_session_path_uses_pool_name(tmp_path: Path) -> None:
    from vctl.commands.rolling_restart import _session_path
    import vctl.commands.rolling_restart as rr
    orig = rr._SESSION_DIR
    rr._SESSION_DIR = tmp_path / "rolling-restart"
    try:
        p = _session_path("mypool")
        assert p == tmp_path / "rolling-restart" / "mypool.json"
    finally:
        rr._SESSION_DIR = orig


# Test 2
def test_load_session_returns_none_when_missing(tmp_path: Path) -> None:
    import vctl.commands.rolling_restart as rr
    sf = rr._SessionFile.__new__(rr._SessionFile)
    sf._path = tmp_path / "nofile.json"
    sf._lock_path = tmp_path / "nofile.lock"
    assert sf.exists() is False
    assert sf.read() is None  # type: ignore[func-returns-value]


# Test 3
def test_load_session_reads_existing_file(tmp_path: Path) -> None:
    import vctl.commands.rolling_restart as rr
    data = {"pool": "p", "completed": [], "failed": [], "pending": ["ep1"], "in_progress": True}
    path = tmp_path / "p.json"
    path.write_text(json.dumps(data))
    sf = rr._SessionFile.__new__(rr._SessionFile)
    sf._path = path
    sf._lock_path = tmp_path / "p.lock"
    assert sf.read() == data


# Test 4
def test_write_session_atomic(tmp_path: Path) -> None:
    import vctl.commands.rolling_restart as rr
    sf = rr._SessionFile.__new__(rr._SessionFile)
    sf._path = tmp_path / "p.json"
    sf._lock_path = tmp_path / "p.lock"
    data = {"pool": "p", "in_progress": True}
    sf.write(data)
    assert json.loads(sf._path.read_text()) == data
    assert not (tmp_path / "p.json.tmp").exists()


# Test 5
def test_write_session_overwrites_existing(tmp_path: Path) -> None:
    import vctl.commands.rolling_restart as rr
    sf = rr._SessionFile.__new__(rr._SessionFile)
    sf._path = tmp_path / "p.json"
    sf._lock_path = tmp_path / "p.lock"
    sf.write({"pool": "p", "in_progress": True})
    sf.write({"pool": "p", "in_progress": False, "extra": "new"})
    loaded = json.loads(sf._path.read_text())
    assert loaded["in_progress"] is False
    assert loaded.get("extra") == "new"


# Test 6
def test_delete_session_idempotent(tmp_path: Path) -> None:
    import vctl.commands.rolling_restart as rr
    sf = rr._SessionFile.__new__(rr._SessionFile)
    sf._path = tmp_path / "nonexistent.json"
    sf._lock_path = tmp_path / "nonexistent.lock"
    # Must not raise even when file is absent
    sf.delete()
    sf.delete()  # second call also silent
