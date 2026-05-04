"""Tests for rolling-restart session file helpers (Task 1)."""

from __future__ import annotations

import argparse as _argparse
import json as _json
import subprocess as _subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers (import lazily so collection doesn't blow up before module exists)
# ---------------------------------------------------------------------------


def _session_path(pool: str, base: Path) -> Path:
    # Override SESSION_DIR in-test: re-use the helper with monkeypatched constant
    import vctl.commands.rolling_restart as rr
    from vctl.commands.rolling_restart import _session_path as _sp

    orig = rr._SESSION_DIR
    rr._SESSION_DIR = base / "rolling-restart"
    try:
        return _sp(pool)
    finally:
        rr._SESSION_DIR = orig


# Test 1
def test_session_path_uses_pool_name(tmp_path: Path) -> None:
    import vctl.commands.rolling_restart as rr
    from vctl.commands.rolling_restart import _session_path

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
    path.write_text(_json.dumps(data))
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
    assert _json.loads(sf._path.read_text()) == data
    assert not (tmp_path / "p.json.tmp").exists()


# Test 5
def test_write_session_overwrites_existing(tmp_path: Path) -> None:
    import vctl.commands.rolling_restart as rr

    sf = rr._SessionFile.__new__(rr._SessionFile)
    sf._path = tmp_path / "p.json"
    sf._lock_path = tmp_path / "p.lock"
    sf.write({"pool": "p", "in_progress": True})
    sf.write({"pool": "p", "in_progress": False, "extra": "new"})
    loaded = _json.loads(sf._path.read_text())
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


# ---------------------------------------------------------------------------
# Task 2: _verify_ep_up
# ---------------------------------------------------------------------------


def _make_stats_returning(ep: str, status: str) -> object:
    """Return a fake _fetch_haproxy_stats function that always returns *status* for *ep*."""

    def _fake_stats(_cli: object) -> dict[str, dict[str, dict[str, int | str]]]:
        return {
            "pool_mypool": {
                "b_10_0_0_1_8000": {"ep": ep, "status": status, "scur": 0, "qcur": 0, "lastchg": 1},
            }
        }

    return _fake_stats


def test_verify_ep_up_returns_true_immediately_when_already_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vctl.commands.rolling_restart as rr
    from vctl.config.models import LbAdmin, LbDefaults, LbHaproxy, LbHealth, LbStats, Pool
    from vctl.lb.manager import LbManager

    lb = LbHaproxy(
        host="10.0.0.1",
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        health=LbHealth(),
        defaults=LbDefaults(),
        pools=[Pool(name="mypool", served_model="*", bind_port=8080)],
    )
    mgr = LbManager(lb, state_dir=tmp_path / "state", run_dir=tmp_path / "run")

    monkeypatch.setattr(rr, "_fetch_haproxy_stats", _make_stats_returning("10.0.0.1:8000", "UP"))
    monkeypatch.setattr(rr, "lb_admin_client", lambda m: object())

    result = rr._verify_ep_up("10.0.0.1:8000", "mypool", mgr, timeout_s=5)
    assert result is True


def test_verify_ep_up_polls_until_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import vctl.commands.rolling_restart as rr
    from vctl.config.models import LbAdmin, LbDefaults, LbHaproxy, LbHealth, LbStats, Pool
    from vctl.lb.manager import LbManager

    lb = LbHaproxy(
        host="10.0.0.1",
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        health=LbHealth(),
        defaults=LbDefaults(),
        pools=[Pool(name="mypool", served_model="*", bind_port=8080)],
    )
    mgr = LbManager(lb, state_dir=tmp_path / "state", run_dir=tmp_path / "run")

    call_count = {"n": 0}

    def _fake_stats(_cli: object) -> dict[str, dict[str, dict[str, int | str]]]:
        call_count["n"] += 1
        status = "UP" if call_count["n"] >= 3 else "DOWN"
        return {
            "pool_mypool": {
                "srv": {"ep": "10.0.0.1:8000", "status": status, "scur": 0, "qcur": 0, "lastchg": 0}
            }
        }

    monkeypatch.setattr(rr, "_fetch_haproxy_stats", _fake_stats)
    monkeypatch.setattr(rr, "lb_admin_client", lambda m: object())
    monkeypatch.setattr(rr.time, "sleep", lambda _: None)  # type: ignore[attr-defined]

    result = rr._verify_ep_up("10.0.0.1:8000", "mypool", mgr, timeout_s=30)
    assert result is True
    assert call_count["n"] >= 3


def test_verify_ep_up_returns_false_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vctl.commands.rolling_restart as rr
    from vctl.config.models import LbAdmin, LbDefaults, LbHaproxy, LbHealth, LbStats, Pool
    from vctl.lb.manager import LbManager

    lb = LbHaproxy(
        host="10.0.0.1",
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        health=LbHealth(),
        defaults=LbDefaults(),
        pools=[Pool(name="mypool", served_model="*", bind_port=8080)],
    )
    mgr = LbManager(lb, state_dir=tmp_path / "state", run_dir=tmp_path / "run")

    monkeypatch.setattr(rr, "_fetch_haproxy_stats", _make_stats_returning("10.0.0.1:8000", "DOWN"))
    monkeypatch.setattr(rr, "lb_admin_client", lambda m: object())
    # Advance time so the deadline is always exceeded after first iteration
    _calls = {"n": 0}

    def _fake_monotonic() -> float:
        _calls["n"] += 1
        return float(_calls["n"] * 100)

    monkeypatch.setattr(rr.time, "monotonic", _fake_monotonic)  # type: ignore[attr-defined]
    monkeypatch.setattr(rr.time, "sleep", lambda _: None)  # type: ignore[attr-defined]

    result = rr._verify_ep_up("10.0.0.1:8000", "mypool", mgr, timeout_s=1)
    assert result is False


def test_verify_ep_up_returns_false_on_lb_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vctl.commands.rolling_restart as rr
    from vctl.config.models import LbAdmin, LbDefaults, LbHaproxy, LbHealth, LbStats, Pool
    from vctl.lb.manager import LbManager

    lb = LbHaproxy(
        host="10.0.0.1",
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        health=LbHealth(),
        defaults=LbDefaults(),
        pools=[Pool(name="mypool", served_model="*", bind_port=8080)],
    )
    mgr = LbManager(lb, state_dir=tmp_path / "state", run_dir=tmp_path / "run")

    # lb_admin_client always returns None → LB unreachable
    monkeypatch.setattr(rr, "lb_admin_client", lambda m: None)
    _calls = {"n": 0}

    def _fake_monotonic() -> float:
        _calls["n"] += 1
        return float(_calls["n"] * 100)

    monkeypatch.setattr(rr.time, "monotonic", _fake_monotonic)  # type: ignore[attr-defined]
    monkeypatch.setattr(rr.time, "sleep", lambda _: None)  # type: ignore[attr-defined]

    result = rr._verify_ep_up("10.0.0.1:8000", "mypool", mgr, timeout_s=1)
    assert result is False


# ---------------------------------------------------------------------------
# Task 3: _restart_one_ep
# ---------------------------------------------------------------------------


def _make_mgr_for_restart(tmp_path: Path) -> object:
    from vctl.config.models import LbAdmin, LbDefaults, LbHaproxy, LbHealth, LbStats, Pool
    from vctl.lb.manager import LbManager

    lb = LbHaproxy(
        host="10.0.0.1",
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        health=LbHealth(),
        defaults=LbDefaults(),
        pools=[Pool(name="mypool", served_model="*", bind_port=8080)],
    )
    return LbManager(lb, state_dir=tmp_path / "state", run_dir=tmp_path / "run")


def test_restart_one_ep_dry_run_skips_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import vctl.commands.rolling_restart as rr

    ssh_calls: list[list[str]] = []

    def _fake_run(argv: list[str], **kwargs: object) -> object:
        ssh_calls.append(argv)
        return _subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(rr.subprocess, "run", _fake_run)

    mgr = _make_mgr_for_restart(tmp_path)
    result = rr._restart_one_ep(
        ep="10.0.0.2:8000",
        idx=1,
        total=3,
        pool_name="mypool",
        mgr=mgr,
        ssh_user="",
        vllm_timeout=600,
        ready_timeout=60,
        dry_run=True,
        quiet=False,
        remote_vctl_path=None,
    )
    assert result == "ok"
    assert ssh_calls == [], "ssh must NOT be called in dry-run mode"
    captured = capsys.readouterr()
    assert "would restart" in captured.err


def test_restart_one_ep_ssh_failure_returns_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vctl.commands.rolling_restart as rr

    def _fake_run(argv: list[str], **kwargs: object) -> object:
        return _subprocess.CompletedProcess(argv, 255, stdout="", stderr="Permission denied")

    monkeypatch.setattr(rr.subprocess, "run", _fake_run)

    mgr = _make_mgr_for_restart(tmp_path)
    result = rr._restart_one_ep(
        ep="10.0.0.2:8000",
        idx=1,
        total=3,
        pool_name="mypool",
        mgr=mgr,
        ssh_user="",
        vllm_timeout=600,
        ready_timeout=60,
        dry_run=False,
        quiet=True,
        remote_vctl_path=None,
    )
    assert result == "failed"


def test_restart_one_ep_ssh_timeout_returns_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vctl.commands.rolling_restart as rr

    def _fake_run(argv: list[str], **kwargs: object) -> _subprocess.CompletedProcess[str]:
        raise _subprocess.TimeoutExpired(cmd=argv, timeout=600)

    monkeypatch.setattr(rr.subprocess, "run", _fake_run)

    mgr = _make_mgr_for_restart(tmp_path)
    result = rr._restart_one_ep(
        ep="10.0.0.2:8000",
        idx=1,
        total=3,
        pool_name="mypool",
        mgr=mgr,
        ssh_user="",
        vllm_timeout=600,
        ready_timeout=60,
        dry_run=False,
        quiet=True,
        remote_vctl_path=None,
    )
    assert result == "failed"


def test_restart_one_ep_health_check_fails_returns_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vctl.commands.rolling_restart as rr

    def _fake_run(argv: list[str], **kwargs: object) -> object:
        return _subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(rr.subprocess, "run", _fake_run)
    # _verify_ep_up always returns False → health check failure
    monkeypatch.setattr(rr, "_verify_ep_up", lambda ep, pool, mgr, timeout_s: False)

    mgr = _make_mgr_for_restart(tmp_path)
    result = rr._restart_one_ep(
        ep="10.0.0.2:8000",
        idx=1,
        total=3,
        pool_name="mypool",
        mgr=mgr,
        ssh_user="",
        vllm_timeout=600,
        ready_timeout=60,
        dry_run=False,
        quiet=True,
        remote_vctl_path=None,
    )
    assert result == "failed"


def test_restart_one_ep_full_success_returns_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vctl.commands.rolling_restart as rr

    ssh_calls: list[list[str]] = []

    def _fake_run(argv: list[str], **kwargs: object) -> object:
        ssh_calls.append(argv)
        return _subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(rr.subprocess, "run", _fake_run)
    monkeypatch.setattr(rr, "_verify_ep_up", lambda ep, pool, mgr, timeout_s: True)

    mgr = _make_mgr_for_restart(tmp_path)
    result = rr._restart_one_ep(
        ep="10.0.0.2:8000",
        idx=1,
        total=3,
        pool_name="mypool",
        mgr=mgr,
        ssh_user="admin",
        vllm_timeout=600,
        ready_timeout=60,
        dry_run=False,
        quiet=False,
        remote_vctl_path=None,
    )
    assert result == "ok"
    assert len(ssh_calls) == 1
    argv = ssh_calls[0]
    assert "ssh" in argv[0]
    assert "admin@10.0.0.2" in argv
    assert any("bash -lc" in a or "vctl serve restart" in a for a in argv)


# ---------------------------------------------------------------------------
# Task 4: _run_fresh + run() argparse
# ---------------------------------------------------------------------------


def _make_ns(config: str = "/dev/null", profile: str | None = None) -> _argparse.Namespace:
    return _argparse.Namespace(config=config, profile=profile)


def _make_full_mgr(tmp_path: Path, pool_names: list[str] | None = None) -> object:
    from vctl.config.models import LbAdmin, LbDefaults, LbHaproxy, LbHealth, LbStats, Pool
    from vctl.lb.manager import LbManager

    if pool_names is None:
        pool_names = ["mypool"]
    pools = [Pool(name=n, served_model="*", bind_port=8080 + i) for i, n in enumerate(pool_names)]
    lb = LbHaproxy(
        host="10.0.0.1",
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        health=LbHealth(),
        defaults=LbDefaults(),
        pools=pools,
    )
    return LbManager(lb, state_dir=tmp_path / "state", run_dir=tmp_path / "run")


def test_fresh_run_unknown_pool_returns_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import vctl.commands.rolling_restart as rr

    monkeypatch.setattr(rr, "_SESSION_DIR", tmp_path / "sessions")
    mgr = _make_full_mgr(tmp_path, pool_names=["mypool"])

    # Parse args manually (no config resolution needed)
    parsed = rr._build_subparser().parse_args(["--pool", "nonexistent"])
    rc = rr._run_fresh(parsed, mgr)
    assert rc == 3


def test_fresh_run_empty_pool_returns_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import vctl.commands.rolling_restart as rr
    from vctl.lb.state import BackendState

    monkeypatch.setattr(rr, "_SESSION_DIR", tmp_path / "sessions")
    mgr = _make_full_mgr(tmp_path, pool_names=["mypool"])
    # Patch BackendState.list to return empty
    monkeypatch.setattr(BackendState, "list", lambda self: [])

    parsed = rr._build_subparser().parse_args(["--pool", "mypool"])
    rc = rr._run_fresh(parsed, mgr)
    assert rc == 0
    out = capsys.readouterr()
    assert "nothing to restart" in out.err or "no registered backends" in out.err.lower()


def test_fresh_run_concurrency_guard_returns_4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vctl.commands.rolling_restart as rr

    sess_dir = tmp_path / "sessions"
    monkeypatch.setattr(rr, "_SESSION_DIR", sess_dir)
    sess_dir.mkdir(parents=True, exist_ok=True)
    # Write a session file with in_progress=True to simulate a concurrent run
    session_file = sess_dir / "mypool.json"
    session_file.write_text(
        _json.dumps(
            {"pool": "mypool", "in_progress": True, "completed": [], "failed": [], "pending": []}
        )
    )
    mgr = _make_full_mgr(tmp_path, pool_names=["mypool"])

    parsed = rr._build_subparser().parse_args(["--pool", "mypool"])
    rc = rr._run_fresh(parsed, mgr)
    assert rc == 4


def test_fresh_run_full_success_deletes_session_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vctl.commands.rolling_restart as rr
    from vctl.lb.state import BackendState

    sess_dir = tmp_path / "sessions"
    monkeypatch.setattr(rr, "_SESSION_DIR", sess_dir)
    mgr = _make_full_mgr(tmp_path, pool_names=["mypool"])

    eps = ["10.0.0.1:8000", "10.0.0.2:8000"]
    monkeypatch.setattr(BackendState, "list", lambda self: list(eps))

    # _restart_one_ep always succeeds
    def _always_ok(ep: str, **kwargs: object) -> str:
        return "ok"

    monkeypatch.setattr(rr, "_restart_one_ep", _always_ok)

    parsed = rr._build_subparser().parse_args(["--pool", "mypool"])
    rc = rr._run_fresh(parsed, mgr)
    assert rc == 0
    # Session file must be deleted after full success
    assert not (sess_dir / "mypool.json").exists()


def test_fresh_run_halt_on_failure_persists_session_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vctl.commands.rolling_restart as rr
    from vctl.lb.state import BackendState

    sess_dir = tmp_path / "sessions"
    monkeypatch.setattr(rr, "_SESSION_DIR", sess_dir)
    mgr = _make_full_mgr(tmp_path, pool_names=["mypool"])

    eps = ["ep1:8000", "ep2:8000", "ep3:8000"]
    monkeypatch.setattr(BackendState, "list", lambda self: list(eps))

    call_count = {"n": 0}

    def _fake_restart(ep: str, **kwargs: object) -> str:
        call_count["n"] += 1
        return "failed" if ep == "ep2:8000" else "ok"

    monkeypatch.setattr(rr, "_restart_one_ep", _fake_restart)

    parsed = rr._build_subparser().parse_args(["--pool", "mypool"])
    rc = rr._run_fresh(parsed, mgr)
    assert rc == 1

    # Session file must persist with correct state
    session_path = sess_dir / "mypool.json"
    assert session_path.exists()
    data = _json.loads(session_path.read_text())
    assert "ep1:8000" in data["completed"]
    assert "ep2:8000" in data["failed"]
    assert "ep3:8000" in data["pending"]
    assert data["in_progress"] is False


# ---------------------------------------------------------------------------
# Task 5: _run_resume
# ---------------------------------------------------------------------------


def _write_session(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps(data))


def test_resume_verifies_failed_ep_now_up_marks_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AT-3: failed ep is now UP in HAProxy → moved to completed without ssh."""
    import vctl.commands.rolling_restart as rr
    from vctl.lb.state import BackendState

    sess_dir = tmp_path / "sessions"
    monkeypatch.setattr(rr, "_SESSION_DIR", sess_dir)
    sess_dir.mkdir(parents=True)

    session_data: dict[str, object] = {
        "pool": "mypool",
        "started_at": "2026-05-04T00:00:00+00:00",
        "completed": ["ep1:8000"],
        "failed": ["ep2:8000"],
        "pending": ["ep3:8000"],
        "in_progress": False,
    }
    _write_session(sess_dir / "mypool.json", session_data)

    mgr = _make_full_mgr(tmp_path, pool_names=["mypool"])

    # ep2 returns UP on the verification probe
    def _fake_verify(ep: str, pool: str, m: object, timeout_s: int) -> bool:
        return ep == "ep2:8000"

    monkeypatch.setattr(rr, "_verify_ep_up", _fake_verify)

    # ep3 restart succeeds
    monkeypatch.setattr(
        rr,
        "_restart_one_ep",
        lambda ep, **kwargs: "ok",
    )
    monkeypatch.setattr(BackendState, "list", lambda self: ["ep1:8000", "ep2:8000", "ep3:8000"])

    parsed = rr._build_subparser().parse_args(["--pool", "mypool"])
    # Create sf directly (bypass _SessionFile.__init__ which creates _SESSION_DIR)
    sf2 = rr._SessionFile.__new__(rr._SessionFile)
    sf2._path = sess_dir / "mypool.json"
    sf2._lock_path = sess_dir / "mypool.lock"
    rc = rr._run_resume(parsed, mgr, sf2, dict(session_data))  # type: ignore[arg-type]

    assert rc == 0
    out = capsys.readouterr()
    assert "verified" in out.err and "ep2:8000" in out.err
    # Session file deleted on full success
    assert not (sess_dir / "mypool.json").exists()


def test_resume_failed_ep_still_down_skip_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator picks 'a' (skip): ep2 moves to completed, run continues."""
    import vctl.commands.rolling_restart as rr
    from vctl.lb.state import BackendState

    sess_dir = tmp_path / "sessions"
    monkeypatch.setattr(rr, "_SESSION_DIR", sess_dir)
    sess_dir.mkdir(parents=True)

    session_data: dict[str, object] = {
        "pool": "mypool",
        "started_at": "2026-05-04T00:00:00+00:00",
        "completed": [],
        "failed": ["ep1:8000"],
        "pending": [],
        "in_progress": False,
    }
    _write_session(sess_dir / "mypool.json", session_data)

    mgr = _make_full_mgr(tmp_path, pool_names=["mypool"])

    # ep1 is still DOWN
    monkeypatch.setattr(rr, "_verify_ep_up", lambda ep, pool, m, timeout_s: False)
    # Operator chooses "a" (skip)
    monkeypatch.setattr(rr.sys, "stdin", type("FakeStdin", (), {"read": lambda s, n: "a"})())
    monkeypatch.setattr(BackendState, "list", lambda self: ["ep1:8000"])

    sf = rr._SessionFile.__new__(rr._SessionFile)
    sf._path = sess_dir / "mypool.json"
    sf._lock_path = sess_dir / "mypool.lock"

    parsed = rr._build_subparser().parse_args(["--pool", "mypool"])
    rc = rr._run_resume(parsed, mgr, sf, dict(session_data))  # type: ignore[arg-type]
    assert rc == 0
    assert not (sess_dir / "mypool.json").exists()


def test_resume_failed_ep_still_down_retry_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator picks 'b' (retry): ep1 ssh called again, succeeds."""
    import vctl.commands.rolling_restart as rr
    from vctl.lb.state import BackendState

    sess_dir = tmp_path / "sessions"
    monkeypatch.setattr(rr, "_SESSION_DIR", sess_dir)
    sess_dir.mkdir(parents=True)

    session_data: dict[str, object] = {
        "pool": "mypool",
        "started_at": "2026-05-04T00:00:00+00:00",
        "completed": [],
        "failed": ["ep1:8000"],
        "pending": [],
        "in_progress": False,
    }
    _write_session(sess_dir / "mypool.json", session_data)

    mgr = _make_full_mgr(tmp_path, pool_names=["mypool"])

    restart_calls: list[str] = []

    def _fake_restart(ep: str, **kwargs: object) -> str:
        restart_calls.append(ep)
        return "ok"

    # ep1 is DOWN in initial probe → prompt → operator retries → _restart_one_ep called
    monkeypatch.setattr(rr, "_verify_ep_up", lambda ep, pool, m, timeout_s: False)
    monkeypatch.setattr(rr, "_restart_one_ep", _fake_restart)
    monkeypatch.setattr(rr.sys, "stdin", type("FakeStdin", (), {"read": lambda s, n: "b"})())
    monkeypatch.setattr(BackendState, "list", lambda self: ["ep1:8000"])

    sf = rr._SessionFile.__new__(rr._SessionFile)
    sf._path = sess_dir / "mypool.json"
    sf._lock_path = sess_dir / "mypool.lock"

    parsed = rr._build_subparser().parse_args(["--pool", "mypool"])
    rc = rr._run_resume(parsed, mgr, sf, dict(session_data))  # type: ignore[arg-type]
    assert rc == 0
    assert "ep1:8000" in restart_calls


def test_resume_failed_ep_still_down_abort_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator picks 'c' (abort): exit 1, session file preserved unchanged."""
    import vctl.commands.rolling_restart as rr
    from vctl.lb.state import BackendState

    sess_dir = tmp_path / "sessions"
    monkeypatch.setattr(rr, "_SESSION_DIR", sess_dir)
    sess_dir.mkdir(parents=True)

    session_data: dict[str, object] = {
        "pool": "mypool",
        "started_at": "2026-05-04T00:00:00+00:00",
        "completed": [],
        "failed": ["ep1:8000"],
        "pending": ["ep2:8000"],
        "in_progress": False,
    }
    _write_session(sess_dir / "mypool.json", session_data)

    mgr = _make_full_mgr(tmp_path, pool_names=["mypool"])

    monkeypatch.setattr(rr, "_verify_ep_up", lambda ep, pool, m, timeout_s: False)
    monkeypatch.setattr(rr, "_restart_one_ep", lambda ep, **kwargs: "ok")
    monkeypatch.setattr(rr.sys, "stdin", type("FakeStdin", (), {"read": lambda s, n: "c"})())
    monkeypatch.setattr(BackendState, "list", lambda self: ["ep1:8000", "ep2:8000"])

    sf = rr._SessionFile.__new__(rr._SessionFile)
    sf._path = sess_dir / "mypool.json"
    sf._lock_path = sess_dir / "mypool.lock"

    parsed = rr._build_subparser().parse_args(["--pool", "mypool"])
    rc = rr._run_resume(parsed, mgr, sf, dict(session_data))  # type: ignore[arg-type]
    assert rc == 1
    # Session file must still exist (preserved for operator inspection)
    assert (sess_dir / "mypool.json").exists()


def test_resume_continues_pending_after_failed_handling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After resolving the failed ep (skip), pending ep3 is also restarted."""
    import vctl.commands.rolling_restart as rr
    from vctl.lb.state import BackendState

    sess_dir = tmp_path / "sessions"
    monkeypatch.setattr(rr, "_SESSION_DIR", sess_dir)
    sess_dir.mkdir(parents=True)

    session_data: dict[str, object] = {
        "pool": "mypool",
        "started_at": "2026-05-04T00:00:00+00:00",
        "completed": ["ep1:8000"],
        "failed": ["ep2:8000"],
        "pending": ["ep3:8000"],
        "in_progress": False,
    }
    _write_session(sess_dir / "mypool.json", session_data)

    mgr = _make_full_mgr(tmp_path, pool_names=["mypool"])

    restarted: list[str] = []

    def _fake_restart(ep: str, **kwargs: object) -> str:
        restarted.append(ep)
        return "ok"

    # ep2 still DOWN → skip
    monkeypatch.setattr(rr, "_verify_ep_up", lambda ep, pool, m, timeout_s: False)
    monkeypatch.setattr(rr, "_restart_one_ep", _fake_restart)
    monkeypatch.setattr(rr.sys, "stdin", type("FakeStdin", (), {"read": lambda s, n: "a"})())
    monkeypatch.setattr(BackendState, "list", lambda self: ["ep1:8000", "ep2:8000", "ep3:8000"])

    sf = rr._SessionFile.__new__(rr._SessionFile)
    sf._path = sess_dir / "mypool.json"
    sf._lock_path = sess_dir / "mypool.lock"

    parsed = rr._build_subparser().parse_args(["--pool", "mypool"])
    rc = rr._run_resume(parsed, mgr, sf, dict(session_data))  # type: ignore[arg-type]
    assert rc == 0
    # ep3 must have been restarted as the pending ep
    assert "ep3:8000" in restarted
