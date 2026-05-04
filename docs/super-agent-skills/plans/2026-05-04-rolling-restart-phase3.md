# vctl Phase 3 — `rolling-restart` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use super-agent-skills:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `vctl rolling-restart --pool <name>` — sequential, halt-on-failure restart of every endpoint in a pool via ssh-loop. Idempotent: per-pool session file at `~/.vctl/lb/rolling-restart/<pool>.json` enables auto-resume after operator fixes a failed ep.

**Architecture:** New top-level command `vctl rolling-restart` in `src/vctl/commands/rolling_restart.py`. For each ep, ssh + run `bash -lc 'vctl serve restart'` on the remote host, then poll local HAProxy `_fetch_haproxy_stats` until ep status starts with `UP`. State persisted to atomic JSON session file. Reuses Phase 1 `VllmManager` (invoked remotely via `vctl serve restart`) and Phase 2 `_fetch_haproxy_stats` (for UP verification).

**Tech Stack:** Python 3.10+, pydantic v2 (existing config), ssh subprocess (no paramiko dep). Reuses Phase 1 + Phase 2 infrastructure. No new runtime deps.

---

## File Map

| File | Status | Purpose |
|---|---|---|
| `src/vctl/commands/rolling_restart.py` | NEW | command module: argparse, dispatch, session file mgmt, ssh-loop, verification |
| `src/vctl/cli.py` | MODIFY | register `"rolling-restart"` in `_COMMANDS` dict |
| `tests/test_commands_rolling_restart.py` | NEW | unit tests with mocked ssh + `_fetch_haproxy_stats` |
| `pyproject.toml` | MODIFY | version 0.6.0 → 0.7.0 |
| `src/vctl/__init__.py` | MODIFY | `__version__` → `"0.7.0"` |
| `tests/test_smoke.py` | MODIFY | version assert |
| `docs/CHANGELOG.md` | MODIFY | 0.7.0 entry |
| `docs/CLI-REFERENCE.md` | MODIFY | new `vctl rolling-restart` section |

---

## TASK ORDERING (8 tasks, single stream)

---

### Task 1 — Session file schema + atomic read/write helpers (`_SessionFile` class)

**What we build:** The atomic JSON session file helper class and its six unit tests. No command dispatch yet — just the storage layer.

#### Step 1 — Write the failing tests

Create `tests/test_commands_rolling_restart.py` with 6 tests that import from the not-yet-existing module:

```python
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
```

- [ ] Create `tests/test_commands_rolling_restart.py` with the 6 tests above.

#### Step 2 — Run tests (expect import failure / AttributeError)

```bash
.venv/bin/pytest tests/test_commands_rolling_restart.py -x -q 2>&1 | head -30
```

Expected: `ModuleNotFoundError: No module named 'vctl.commands.rolling_restart'` — confirms the tests are live and failing for the right reason.

- [ ] Run `pytest tests/test_commands_rolling_restart.py -x -q` and confirm failure is `ModuleNotFoundError`.

#### Step 3 — Implement `_SessionFile` and helpers

Create `src/vctl/commands/rolling_restart.py` with the session layer only. No `run()` yet.

```python
"""``vctl rolling-restart`` — sequential per-pool endpoint restart."""
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

# Module-level imports for monkeypatching (same pattern as vctl.lb.prune).
import subprocess

from vctl.commands.lb import _fetch_haproxy_stats
from vctl.lb.runtime import lb_admin_client

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
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass
```

- [ ] Create `src/vctl/commands/rolling_restart.py` with the above content.

#### Step 4 — Run tests (expect 6 passing)

```bash
.venv/bin/pytest tests/test_commands_rolling_restart.py -x -q
```

Expected: `6 passed`.

- [ ] Run `pytest tests/test_commands_rolling_restart.py -x -q` and confirm 6 passed.

#### Step 5 — Ruff + mypy gate

```bash
.venv/bin/ruff check src/vctl/commands/rolling_restart.py
.venv/bin/ruff format --check src/vctl/commands/rolling_restart.py
.venv/bin/mypy --strict src/vctl/commands/rolling_restart.py
```

Fix any issues before proceeding to Task 2.

- [ ] All three gates pass on `rolling_restart.py`.

---

### Task 2 — `_verify_ep_up` health check helper

**What we build:** The HAProxy polling loop that waits for a single endpoint to appear `UP` in stats, plus 4 targeted unit tests.

#### Step 1 — Write the failing tests

Append to `tests/test_commands_rolling_restart.py`:

```python
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


def test_verify_ep_up_polls_until_up(
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
```

- [ ] Append the 4 `_verify_ep_up` tests to `tests/test_commands_rolling_restart.py`.

#### Step 2 — Run tests (expect import error on `_verify_ep_up`)

```bash
.venv/bin/pytest tests/test_commands_rolling_restart.py -k "verify_ep_up" -x -q 2>&1 | head -20
```

Expected: `AttributeError: module 'vctl.commands.rolling_restart' has no attribute '_verify_ep_up'`.

- [ ] Run the 4 new tests and confirm they fail with `AttributeError`.

#### Step 3 — Implement `_verify_ep_up`

Add to `src/vctl/commands/rolling_restart.py` after the `_SessionFile` class:

```python
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vctl.lb.manager import LbManager


def _verify_ep_up(
    ep: str,
    pool_name: str,
    mgr: "LbManager",
    timeout_s: int,
) -> bool:
    """Poll HAProxy stats until *ep* in pool_<pool_name> reports status starting 'UP'.

    Opens a fresh lb_admin_client per iteration (HAProxy admin socket closes after
    each response — see CLAUDE.md gotcha). Returns True on first UP hit; False if
    deadline expires without seeing UP. A None client (LB unreachable) is treated as
    non-fatal: we sleep and retry until the deadline.
    """
    deadline = time.monotonic() + timeout_s
    pool_section = f"pool_{pool_name}"
    while time.monotonic() < deadline:
        cli = lb_admin_client(mgr)
        if cli is None:
            time.sleep(2)
            continue
        stats = _fetch_haproxy_stats(cli)
        for srv_data in stats.get(pool_section, {}).values():
            if srv_data.get("ep") == ep:
                status = str(srv_data.get("status", ""))
                if status.startswith("UP"):
                    return True
                break
        time.sleep(2)
    return False
```

Note: `time` is already in the stdlib; add `import time` at the top of the file if not present.

- [ ] Add `import time` and `_verify_ep_up` to `src/vctl/commands/rolling_restart.py`.

#### Step 4 — Run tests (expect 10 passing)

```bash
.venv/bin/pytest tests/test_commands_rolling_restart.py -x -q
```

Expected: `10 passed` (6 from Task 1 + 4 new).

- [ ] Run pytest and confirm 10 passed.

#### Step 5 — Ruff + mypy gate

```bash
.venv/bin/ruff check src/vctl/commands/rolling_restart.py
.venv/bin/ruff format --check src/vctl/commands/rolling_restart.py
.venv/bin/mypy --strict src/vctl/commands/rolling_restart.py
```

- [ ] All three gates pass.

---

### Task 3 — `_restart_one_ep` per-ep restart helper

**What we build:** The function that builds the ssh command, runs it, then calls `_verify_ep_up`. Returns `"ok"` or `"failed"`. 5 unit tests.

**CHECKPOINT AFTER TASK 3:** per-ep restart primitive unit-tested in isolation. State machine + ssh + verify all mocked.

#### Step 1 — Write the failing tests

Append to `tests/test_commands_rolling_restart.py`:

```python
# ---------------------------------------------------------------------------
# Task 3: _restart_one_ep
# ---------------------------------------------------------------------------
import subprocess as _subprocess


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
        ep="10.0.0.2:8000", idx=1, total=3, pool_name="mypool", mgr=mgr,
        ssh_user="", vllm_timeout=600, ready_timeout=60,
        dry_run=True, quiet=False, remote_vctl_path=None,
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
        ep="10.0.0.2:8000", idx=1, total=3, pool_name="mypool", mgr=mgr,
        ssh_user="", vllm_timeout=600, ready_timeout=60,
        dry_run=False, quiet=True, remote_vctl_path=None,
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
        ep="10.0.0.2:8000", idx=1, total=3, pool_name="mypool", mgr=mgr,
        ssh_user="", vllm_timeout=600, ready_timeout=60,
        dry_run=False, quiet=True, remote_vctl_path=None,
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
        ep="10.0.0.2:8000", idx=1, total=3, pool_name="mypool", mgr=mgr,
        ssh_user="", vllm_timeout=600, ready_timeout=60,
        dry_run=False, quiet=True, remote_vctl_path=None,
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
        ep="10.0.0.2:8000", idx=1, total=3, pool_name="mypool", mgr=mgr,
        ssh_user="admin", vllm_timeout=600, ready_timeout=60,
        dry_run=False, quiet=False, remote_vctl_path=None,
    )
    assert result == "ok"
    assert len(ssh_calls) == 1
    argv = ssh_calls[0]
    assert "ssh" in argv[0]
    assert "admin@10.0.0.2" in argv
    assert any("bash -lc" in a or "vctl serve restart" in a for a in argv)
```

- [ ] Append the 5 `_restart_one_ep` tests to `tests/test_commands_rolling_restart.py`.

#### Step 2 — Run tests (expect AttributeError on `_restart_one_ep`)

```bash
.venv/bin/pytest tests/test_commands_rolling_restart.py -k "restart_one_ep" -x -q 2>&1 | head -20
```

- [ ] Confirm tests fail with `AttributeError: module '...' has no attribute '_restart_one_ep'`.

#### Step 3 — Implement `_restart_one_ep`

Add to `src/vctl/commands/rolling_restart.py`:

```python
import sys
from typing import Literal


def _restart_one_ep(
    ep: str,
    idx: int,
    total: int,
    pool_name: str,
    mgr: "LbManager",
    ssh_user: str,
    vllm_timeout: int,
    ready_timeout: int,
    dry_run: bool,
    quiet: bool,
    remote_vctl_path: str | None,
) -> Literal["ok", "failed"]:
    """Restart a single endpoint via ssh and verify it returns UP in HAProxy.

    Returns "ok" on success, "failed" on ssh error, ssh timeout, or health-check
    timeout. All progress + error output goes to stderr (stdout stays clean).
    """
    ep_host = ep.split(":")[0]
    prefix = f"[{idx}/{total}] {ep}"

    if not quiet:
        print(f"{prefix}  draining → restarting...", file=sys.stderr)

    if dry_run:
        print(f"{prefix}  would restart", file=sys.stderr)
        return "ok"

    # Build ssh target and remote command.
    ssh_target = f"{ssh_user}@{ep_host}" if ssh_user else ep_host
    if remote_vctl_path:
        remote_cmd = f"{remote_vctl_path} serve restart"
    else:
        remote_cmd = "bash -lc 'vctl serve restart'"

    argv = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        "-o", "StrictHostKeyChecking=accept-new",
        ssh_target,
        remote_cmd,
    ]

    try:
        result = subprocess.run(
            argv,
            timeout=vllm_timeout,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        print(
            f"{prefix}  timed out after {vllm_timeout}s. HALTING.",
            file=sys.stderr,
        )
        return "failed"

    if result.returncode != 0:
        stderr_snippet = result.stderr.strip()[:200]
        print(
            f"{prefix}  ssh failed (rc={result.returncode}): {stderr_snippet}. HALTING.",
            file=sys.stderr,
        )
        return "failed"

    if not quiet:
        print(f"{prefix}  waiting for UP...", file=sys.stderr)

    t0 = time.monotonic()
    if not _verify_ep_up(ep, pool_name, mgr, timeout_s=ready_timeout):
        print(
            f"{prefix}  did not become UP within {ready_timeout}s. HALTING.",
            file=sys.stderr,
        )
        return "failed"

    elapsed = int(time.monotonic() - t0)
    if not quiet:
        print(f"{prefix}  ready ({elapsed}s)", file=sys.stderr)
    return "ok"
```

- [ ] Add `import sys`, `from typing import Literal` (if not present), and `_restart_one_ep` to `src/vctl/commands/rolling_restart.py`.

#### Step 4 — Run tests (expect 15 passing)

```bash
.venv/bin/pytest tests/test_commands_rolling_restart.py -x -q
```

Expected: `15 passed` (10 previous + 5 new).

- [ ] Confirm 15 passed.

#### Step 5 — Ruff + mypy gate

```bash
.venv/bin/ruff check src/vctl/commands/rolling_restart.py
.venv/bin/ruff format --check src/vctl/commands/rolling_restart.py
.venv/bin/mypy --strict src/vctl/commands/rolling_restart.py
```

- [ ] All three gates pass on the current module file.

---

### Task 4 — Fresh-run path: argparse, pool resolution, sequential loop, session lifecycle

**What we build:** The `run()` entry point, `_build_subparser()`, `_run_fresh()`, and 5 unit tests covering the fresh-run happy path plus edge cases (unknown pool, empty pool, concurrency guard, halt-on-failure).

#### Step 1 — Write the failing tests

Append to `tests/test_commands_rolling_restart.py`:

```python
# ---------------------------------------------------------------------------
# Task 4: _run_fresh + run() argparse
# ---------------------------------------------------------------------------
import argparse as _argparse
import json as _json


def _make_ns(config: str = "/dev/null", profile: str | None = None) -> _argparse.Namespace:
    return _argparse.Namespace(config=config, profile=profile)


def _make_full_mgr(tmp_path: Path, pool_names: list[str] | None = None) -> object:
    from vctl.config.models import LbAdmin, LbDefaults, LbHaproxy, LbHealth, LbStats, Pool
    from vctl.lb.manager import LbManager
    if pool_names is None:
        pool_names = ["mypool"]
    pools = [Pool(name=n, served_model="*", bind_port=8080 + i)
             for i, n in enumerate(pool_names)]
    lb = LbHaproxy(
        host="10.0.0.1",
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        health=LbHealth(),
        defaults=LbDefaults(),
        pools=pools,
    )
    return LbManager(lb, state_dir=tmp_path / "state", run_dir=tmp_path / "run")


def test_fresh_run_unknown_pool_returns_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        _json.dumps({"pool": "mypool", "in_progress": True, "completed": [], "failed": [], "pending": []})
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
    monkeypatch.setattr(
        rr, "_restart_one_ep",
        lambda ep, idx, total, pool_name, mgr, ssh_user, vllm_timeout,
               ready_timeout, dry_run, quiet, remote_vctl_path: "ok",
    )

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
```

- [ ] Append the 5 fresh-run tests to `tests/test_commands_rolling_restart.py`.

#### Step 2 — Run tests (expect failures on `_build_subparser` / `_run_fresh`)

```bash
.venv/bin/pytest tests/test_commands_rolling_restart.py -k "fresh_run or unknown_pool" -x -q 2>&1 | head -20
```

- [ ] Confirm tests fail with `AttributeError` on `_build_subparser` or `_run_fresh`.

#### Step 3 — Implement `_build_subparser`, `_run_fresh`, and `run`

Add to `src/vctl/commands/rolling_restart.py`:

```python
import argparse
import datetime

from vctl.lb.state import BackendState
from vctl.resolver import resolve


def _build_subparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vctl rolling-restart",
        description=(
            "Sequential, halt-on-failure rolling restart of every endpoint in a pool.\n"
            "ssh-es to each worker, runs `vctl serve restart`, waits until HAProxy "
            "reports UP before moving to the next.\n"
            "\n"
            "State is persisted to ~/.vctl/lb/rolling-restart/<pool>.json so an "
            "interrupted run can be auto-resumed by re-running the same command."
        ),
    )
    p.add_argument("--pool", required=True, metavar="NAME", help="Target pool name (required).")
    mx = p.add_mutually_exclusive_group()
    mx.add_argument(
        "--fresh",
        action="store_true",
        help="Delete existing session file before starting; force fresh run from all eps.",
    )
    mx.add_argument(
        "--status",
        action="store_true",
        help="Print session file contents (or 'no session in progress'); exit 0.",
    )
    mx.add_argument(
        "--abort",
        action="store_true",
        help="Delete session file if present; exit 0.",
    )
    mx.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Print what would happen without ssh-ing; session file not written or deleted.",
    )
    p.add_argument(
        "--ready-timeout",
        type=int,
        default=60,
        dest="ready_timeout",
        metavar="SECONDS",
        help="Seconds to wait for HAProxy UP after ssh returns 0 (default: 60).",
    )
    p.add_argument(
        "--vllm-timeout",
        type=int,
        default=600,
        dest="vllm_timeout",
        metavar="SECONDS",
        help="Seconds vctl serve restart is allowed to take on the remote (default: 600).",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-ep progress lines; print only final summary.",
    )
    p.add_argument(
        "--ssh-user",
        default="",
        dest="ssh_user",
        metavar="USER",
        help="Override ssh username (default: use ssh config / key).",
    )
    p.add_argument(
        "--remote-vctl-path",
        default=None,
        dest="remote_vctl_path",
        metavar="PATH",
        help=(
            "Override remote vctl path (default: bash -lc 'vctl serve restart'). "
            "Use for non-standard installs e.g. /opt/vctl/bin/vctl."
        ),
    )
    return p


def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    """Entry point dispatched by cli._dispatch."""
    parsed = _build_subparser().parse_args(argv_rest)

    # Resolve LbManager from cluster config.
    from pathlib import Path
    rc = resolve(ns.config, profile=ns.profile)
    state_dir = Path(rc.cluster.state_dir)
    run_dir = Path.home() / ".vctl" / "lb"
    from vctl.lb.manager import LbManager
    mgr = LbManager(rc.lb, state_dir=state_dir, run_dir=run_dir)

    pool_name = parsed.pool
    sf = _SessionFile(pool_name)

    # --status: print and exit.
    if parsed.status:
        if sf.exists():
            try:
                data = sf.read()
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(json.dumps(data, indent=2))
        else:
            print(f"no session in progress for pool {pool_name!r}")
        return 0

    # --abort: delete and exit.
    if parsed.abort:
        if sf.exists():
            sf.delete()
            print(f"session file for pool {pool_name!r} deleted.", file=sys.stderr)
        else:
            print(f"no session file for pool {pool_name!r}", file=sys.stderr)
        return 0

    # --fresh: delete existing session before starting.
    if parsed.fresh:
        sf.delete()

    # Dispatch: resume if session present, fresh otherwise.
    try:
        existing = sf.read()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if existing is not None and not parsed.dry_run:
        return _run_resume(parsed, mgr, sf, existing)
    return _run_fresh(parsed, mgr)


def _run_fresh(parsed: argparse.Namespace, mgr: "LbManager") -> int:
    """Execute a fresh rolling restart from all eps in the pool."""
    pool_name: str = parsed.pool
    dry_run: bool = getattr(parsed, "dry_run", False)
    quiet: bool = getattr(parsed, "quiet", False)
    ssh_user: str = getattr(parsed, "ssh_user", "")
    vllm_timeout: int = getattr(parsed, "vllm_timeout", 600)
    ready_timeout: int = getattr(parsed, "ready_timeout", 60)
    remote_vctl_path: str | None = getattr(parsed, "remote_vctl_path", None)

    # Validate pool exists in config.
    configured = {p.name for p in mgr.lb.pools}
    if pool_name not in configured:
        available = ", ".join(sorted(configured))
        print(
            f"unknown pool: {pool_name!r}; available: {available}",
            file=sys.stderr,
        )
        return 3

    # Enumerate endpoints from state file.
    from pathlib import Path
    pbs = BackendState(mgr.state_dir if isinstance(mgr.state_dir, Path) else Path(mgr.state_dir),
                       mgr.lb.host, pool=pool_name)
    eps = pbs.list()
    if not eps:
        print(
            f"pool {pool_name!r} has no registered backends; nothing to restart",
            file=sys.stderr,
        )
        return 0

    sf = _SessionFile(pool_name)

    # Concurrency guard: refuse if another invocation is already in_progress.
    if sf.exists() and not dry_run:
        try:
            data = sf.read()
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if data is not None and data.get("in_progress"):
            print(
                f"rolling-restart already in progress for pool {pool_name!r} "
                "— kill the other invocation or use --abort",
                file=sys.stderr,
            )
            return 4

    # Build initial session.
    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    session: dict[str, object] = {
        "pool": pool_name,
        "started_at": now_utc,
        "completed": [],
        "failed": [],
        "pending": list(eps),
        "in_progress": True,
    }
    if not dry_run:
        sf.write(session)

    completed: list[str] = []
    failed: list[str] = []
    pending = list(eps)

    for idx, ep in enumerate(eps, start=1):
        outcome = _restart_one_ep(
            ep=ep,
            idx=idx,
            total=len(eps),
            pool_name=pool_name,
            mgr=mgr,
            ssh_user=ssh_user,
            vllm_timeout=vllm_timeout,
            ready_timeout=ready_timeout,
            dry_run=dry_run,
            quiet=quiet,
            remote_vctl_path=remote_vctl_path,
        )
        pending = [e for e in pending if e != ep]
        if outcome == "ok":
            completed.append(ep)
            if not dry_run:
                sf.write({
                    "pool": pool_name,
                    "started_at": now_utc,
                    "completed": completed,
                    "failed": [],
                    "pending": pending,
                    "in_progress": True,
                })
        else:
            failed.append(ep)
            if not dry_run:
                sf.write({
                    "pool": pool_name,
                    "started_at": now_utc,
                    "completed": completed,
                    "failed": failed,
                    "pending": pending,
                    "in_progress": False,
                })
            print(
                f"HALTING after failure on {ep}. "
                f"Fix the ep then re-run `vctl rolling-restart --pool {pool_name}` to resume.",
                file=sys.stderr,
            )
            return 1

    # Full success.
    if not dry_run:
        sf.delete()
    print(
        f"rolling-restart complete: {len(completed)} ep(s) restarted in pool {pool_name!r}",
        file=sys.stderr,
    )
    return 0
```

Note: `_run_resume` is a stub for now — add a placeholder that returns 0. Full implementation comes in Task 5.

```python
def _run_resume(
    parsed: argparse.Namespace,
    mgr: "LbManager",
    sf: _SessionFile,
    session: dict[str, object],
) -> int:
    """Resume an interrupted rolling restart. Implemented in Task 5."""
    # Placeholder — Task 5 will fill this in.
    return _run_fresh(parsed, mgr)
```

- [ ] Add `import argparse`, `import datetime`, `import json` (at top), and implement `_build_subparser`, `_run_fresh`, `run`, and the `_run_resume` stub in `src/vctl/commands/rolling_restart.py`.

#### Step 4 — Run tests (expect 20 passing)

```bash
.venv/bin/pytest tests/test_commands_rolling_restart.py -x -q
```

Expected: `20 passed` (15 previous + 5 new).

- [ ] Confirm 20 passed.

#### Step 5 — Ruff + mypy gate

```bash
.venv/bin/ruff check src/vctl/commands/rolling_restart.py
.venv/bin/ruff format --check src/vctl/commands/rolling_restart.py
.venv/bin/mypy --strict src/vctl/commands/rolling_restart.py
```

- [ ] All three gates pass.

---

### Task 5 — Resume mode: verify failed eps, prompt logic, continue pending

**What we build:** Replace the `_run_resume` stub with the real implementation plus 5 unit tests covering the resume scenarios from the spec.

**CHECKPOINT AFTER TASK 5:** Full fresh + resume flows unit-tested. State file lifecycle verified.

#### Step 1 — Write the failing tests

Append to `tests/test_commands_rolling_restart.py`:

```python
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
        "pool": "mypool", "started_at": "2026-05-04T00:00:00+00:00",
        "completed": ["ep1:8000"], "failed": ["ep2:8000"],
        "pending": ["ep3:8000"], "in_progress": False,
    }
    _write_session(sess_dir / "mypool.json", session_data)

    mgr = _make_full_mgr(tmp_path, pool_names=["mypool"])

    # ep2 returns UP on the verification probe
    def _fake_verify(ep: str, pool: str, m: object, timeout_s: int) -> bool:
        return ep == "ep2:8000"
    monkeypatch.setattr(rr, "_verify_ep_up", _fake_verify)

    # ep3 restart succeeds
    monkeypatch.setattr(
        rr, "_restart_one_ep",
        lambda ep, **kwargs: "ok",
    )
    monkeypatch.setattr(BackendState, "list", lambda self: ["ep1:8000", "ep2:8000", "ep3:8000"])

    parsed = rr._build_subparser().parse_args(["--pool", "mypool"])
    rc = rr.run(parsed_for_resume := parsed, [])

    # run() with no extra argv_rest uses the top-level run; invoke _run_resume directly
    sf = rr._SessionFile("mypool")
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
        "pool": "mypool", "started_at": "2026-05-04T00:00:00+00:00",
        "completed": [], "failed": ["ep1:8000"], "pending": [], "in_progress": False,
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
        "pool": "mypool", "started_at": "2026-05-04T00:00:00+00:00",
        "completed": [], "failed": ["ep1:8000"], "pending": [], "in_progress": False,
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
        "pool": "mypool", "started_at": "2026-05-04T00:00:00+00:00",
        "completed": [], "failed": ["ep1:8000"], "pending": ["ep2:8000"], "in_progress": False,
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
        "pool": "mypool", "started_at": "2026-05-04T00:00:00+00:00",
        "completed": ["ep1:8000"], "failed": ["ep2:8000"],
        "pending": ["ep3:8000"], "in_progress": False,
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
```

- [ ] Append the 5 resume tests to `tests/test_commands_rolling_restart.py`.

#### Step 2 — Run tests (expect 5 failures on `_run_resume` stub)

```bash
.venv/bin/pytest tests/test_commands_rolling_restart.py -k "resume" -x -q 2>&1 | head -25
```

Expected: failures because `_run_resume` is a stub that calls `_run_fresh` instead of doing real resume logic.

- [ ] Confirm the 5 resume tests fail (stub doesn't implement resume correctly).

#### Step 3 — Implement `_run_resume`

Replace the `_run_resume` stub in `src/vctl/commands/rolling_restart.py` with:

```python
def _run_resume(
    parsed: argparse.Namespace,
    mgr: "LbManager",
    sf: _SessionFile,
    session: dict[str, object],
) -> int:
    """Resume an interrupted rolling restart from a persisted session file.

    For each ep in `failed`:
      - Quick HAProxy probe (5s window): if UP → mark completed, log, continue.
      - Still DOWN → prompt operator: (a) skip, (b) retry, (c) abort.
    Then continue sequentially with `pending` eps via the same restart loop.
    """
    pool_name: str = str(session.get("pool", parsed.pool))
    started_at: str = str(session.get("started_at", ""))
    dry_run: bool = getattr(parsed, "dry_run", False)
    quiet: bool = getattr(parsed, "quiet", False)
    ssh_user: str = getattr(parsed, "ssh_user", "")
    vllm_timeout: int = getattr(parsed, "vllm_timeout", 600)
    ready_timeout: int = getattr(parsed, "ready_timeout", 60)
    remote_vctl_path: str | None = getattr(parsed, "remote_vctl_path", None)

    completed: list[str] = list(session.get("completed", []))  # type: ignore[arg-type]
    failed: list[str] = list(session.get("failed", []))  # type: ignore[arg-type]
    pending: list[str] = list(session.get("pending", []))  # type: ignore[arg-type]

    # Mark in_progress=True now that we're resuming.
    if not dry_run:
        sf.write({
            "pool": pool_name,
            "started_at": started_at,
            "completed": completed,
            "failed": failed,
            "pending": pending,
            "in_progress": True,
        })

    # Step 1: resolve the failed list.
    to_retry: list[str] = []
    for ep in list(failed):
        is_up = _verify_ep_up(ep, pool_name, mgr, timeout_s=5)
        if is_up:
            print(
                f"verified: {ep} was fixed externally — moving to completed",
                file=sys.stderr,
            )
            failed.remove(ep)
            completed.append(ep)
            if not dry_run:
                sf.write({
                    "pool": pool_name,
                    "started_at": started_at,
                    "completed": completed,
                    "failed": failed,
                    "pending": pending,
                    "in_progress": True,
                })
        else:
            # DOWN/MAINT/other — prompt unless --dry-run / --quiet.
            if dry_run or quiet:
                # Non-interactive: default to skip.
                print(
                    f"{ep} is still DOWN — skipping (--dry-run/--quiet mode)",
                    file=sys.stderr,
                )
                failed.remove(ep)
                completed.append(ep)
            else:
                print(
                    f"\nep {ep} is still DOWN. Choose:\n"
                    f"  (a) skip — mark as completed and continue\n"
                    f"  (b) retry — re-attempt restart\n"
                    f"  (c) abort — exit now (session file preserved)\n",
                    file=sys.stderr,
                )
                choice = sys.stdin.read(1).strip().lower()
                if choice == "a":
                    failed.remove(ep)
                    completed.append(ep)
                    if not dry_run:
                        sf.write({
                            "pool": pool_name,
                            "started_at": started_at,
                            "completed": completed,
                            "failed": failed,
                            "pending": pending,
                            "in_progress": True,
                        })
                elif choice == "b":
                    failed.remove(ep)
                    to_retry.append(ep)
                else:
                    # abort (c or anything else)
                    print(
                        f"Aborted. Session file preserved at {sf._path}.",
                        file=sys.stderr,
                    )
                    return 1

    # Build work queue: retried eps first, then remaining pending.
    work_queue = to_retry + pending
    total = len(completed) + len(work_queue)
    start_idx = len(completed) + 1

    for i, ep in enumerate(work_queue):
        idx = start_idx + i
        outcome = _restart_one_ep(
            ep=ep,
            idx=idx,
            total=total,
            pool_name=pool_name,
            mgr=mgr,
            ssh_user=ssh_user,
            vllm_timeout=vllm_timeout,
            ready_timeout=ready_timeout,
            dry_run=dry_run,
            quiet=quiet,
            remote_vctl_path=remote_vctl_path,
        )
        if ep in pending:
            pending = [e for e in pending if e != ep]
        if outcome == "ok":
            completed.append(ep)
            if not dry_run:
                sf.write({
                    "pool": pool_name,
                    "started_at": started_at,
                    "completed": completed,
                    "failed": failed,
                    "pending": pending,
                    "in_progress": True,
                })
        else:
            failed.append(ep)
            if not dry_run:
                sf.write({
                    "pool": pool_name,
                    "started_at": started_at,
                    "completed": completed,
                    "failed": failed,
                    "pending": pending,
                    "in_progress": False,
                })
            print(
                f"HALTING after failure on {ep}. "
                f"Fix the ep then re-run `vctl rolling-restart --pool {pool_name}` to resume.",
                file=sys.stderr,
            )
            return 1

    # Full success.
    if not dry_run:
        sf.delete()
    print(
        f"rolling-restart complete: {len(completed)} ep(s) confirmed in pool {pool_name!r}",
        file=sys.stderr,
    )
    return 0
```

- [ ] Replace the `_run_resume` stub with the full implementation above.

#### Step 4 — Run tests (expect 25 passing)

```bash
.venv/bin/pytest tests/test_commands_rolling_restart.py -x -q
```

Expected: `25 passed`.

- [ ] Confirm 25 passed.

#### Step 5 — Ruff + mypy gate

```bash
.venv/bin/ruff check src/vctl/commands/rolling_restart.py
.venv/bin/ruff format --check src/vctl/commands/rolling_restart.py
.venv/bin/mypy --strict src/vctl/commands/rolling_restart.py
```

- [ ] All three gates pass.

---

### Task 6 — Aux flags: `--status`, `--abort`, `--dry-run`, `--quiet`, `--fresh`

**What we build:** 5 unit tests that exercise the aux-flag dispatch paths in `run()` — these are already implemented in `run()` from Task 4 but not yet covered by tests.

#### Step 1 — Write the failing tests

Append to `tests/test_commands_rolling_restart.py`:

```python
# ---------------------------------------------------------------------------
# Task 6: aux flags — --status, --abort, --dry-run, --quiet, --fresh
# ---------------------------------------------------------------------------

def _invoke_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    eps: list[str] | None = None,
    pool_names: list[str] | None = None,
) -> tuple[int, str, str]:
    """Helper: invoke vctl.commands.rolling_restart.run() with full mocking."""
    import vctl.commands.rolling_restart as rr
    from vctl.lb.state import BackendState

    sess_dir = tmp_path / "sessions"
    monkeypatch.setattr(rr, "_SESSION_DIR", sess_dir)

    if eps is not None:
        monkeypatch.setattr(BackendState, "list", lambda self: list(eps))

    real_pools = pool_names or ["mypool"]

    def _fake_resolve(config: str, profile: str | None = None) -> object:
        from vctl.config.models import (
            ClusterSettings, LbAdmin, LbDefaults, LbHaproxy, LbHealth, LbStats, Pool,
        )
        pools = [Pool(name=n, served_model="*", bind_port=8080 + i)
                 for i, n in enumerate(real_pools)]
        lb = LbHaproxy(
            host="10.0.0.1",
            admin=LbAdmin(bind_port=9001),
            stats=LbStats(bind_port=9000),
            health=LbHealth(),
            defaults=LbDefaults(),
            pools=pools,
        )
        return type("RC", (), {
            "lb": lb,
            "cluster": type("C", (), {"state_dir": str(tmp_path / "state")})(),
        })()

    monkeypatch.setattr(rr, "resolve", _fake_resolve)

    import io
    out = io.StringIO()
    err = io.StringIO()
    import contextlib
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        ns = _argparse.Namespace(config="/dev/null", profile=None)
        rc = rr.run(ns, argv)
    return rc, out.getvalue(), err.getvalue()


def test_status_with_no_session_prints_no_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rc, out, err = _invoke_run(tmp_path, monkeypatch, ["--pool", "mypool", "--status"])
    assert rc == 0
    assert "no session in progress" in out


def test_status_with_active_session_prints_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vctl.commands.rolling_restart as rr
    sess_dir = tmp_path / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(rr, "_SESSION_DIR", sess_dir)
    data = {"pool": "mypool", "in_progress": False, "completed": ["ep1:8000"],
            "failed": [], "pending": []}
    (sess_dir / "mypool.json").write_text(_json.dumps(data))

    rc, out, _ = _invoke_run(tmp_path, monkeypatch, ["--pool", "mypool", "--status"])
    assert rc == 0
    assert "mypool" in out
    assert "ep1:8000" in out


def test_abort_deletes_session_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vctl.commands.rolling_restart as rr
    sess_dir = tmp_path / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(rr, "_SESSION_DIR", sess_dir)
    (sess_dir / "mypool.json").write_text(_json.dumps({"pool": "mypool", "in_progress": False}))

    rc, _, _ = _invoke_run(tmp_path, monkeypatch, ["--pool", "mypool", "--abort"])
    assert rc == 0
    assert not (sess_dir / "mypool.json").exists()


def test_abort_with_no_session_returns_0_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rc, _, err = _invoke_run(tmp_path, monkeypatch, ["--pool", "mypool", "--abort"])
    assert rc == 0
    assert "no session file" in err


def test_fresh_deletes_existing_session_then_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import vctl.commands.rolling_restart as rr
    from vctl.lb.state import BackendState

    sess_dir = tmp_path / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(rr, "_SESSION_DIR", sess_dir)

    # Existing partial session (completed only ep1, ep2 pending)
    old_data = {"pool": "mypool", "in_progress": False,
                "completed": ["ep1:8000"], "failed": [], "pending": ["ep2:8000"]}
    (sess_dir / "mypool.json").write_text(_json.dumps(old_data))

    all_eps = ["ep1:8000", "ep2:8000", "ep3:8000"]
    monkeypatch.setattr(BackendState, "list", lambda self: list(all_eps))
    restarted: list[str] = []

    def _fake_restart(ep: str, **kwargs: object) -> str:
        restarted.append(ep)
        return "ok"
    monkeypatch.setattr(rr, "_restart_one_ep", _fake_restart)

    def _fake_resolve(config: str, profile: str | None = None) -> object:
        from vctl.config.models import (
            LbAdmin, LbDefaults, LbHaproxy, LbHealth, LbStats, Pool,
        )
        lb = LbHaproxy(
            host="10.0.0.1",
            admin=LbAdmin(bind_port=9001),
            stats=LbStats(bind_port=9000),
            health=LbHealth(),
            defaults=LbDefaults(),
            pools=[Pool(name="mypool", served_model="*", bind_port=8080)],
        )
        return type("RC", (), {
            "lb": lb,
            "cluster": type("C", (), {"state_dir": str(tmp_path / "state")})(),
        })()
    monkeypatch.setattr(rr, "resolve", _fake_resolve)

    ns = _argparse.Namespace(config="/dev/null", profile=None)
    rc = rr.run(ns, ["--pool", "mypool", "--fresh"])
    assert rc == 0
    # All 3 eps must be restarted (fresh start from scratch)
    assert restarted == all_eps
    # Session file deleted on success
    assert not (sess_dir / "mypool.json").exists()
```

- [ ] Append the 5 aux-flag tests to `tests/test_commands_rolling_restart.py`.

#### Step 2 — Run tests (expect 5 new failures if `run()` stub not yet wired correctly)

```bash
.venv/bin/pytest tests/test_commands_rolling_restart.py -k "status or abort or fresh_deletes" -x -q 2>&1 | head -25
```

- [ ] Confirm which tests fail and why (likely `resolve` import path inside `run()`).

#### Step 3 — Fix `run()` to import `resolve` at module top

The `run()` function currently imports `resolve` inline. For the `_fake_resolve` monkeypatch in the tests to work, `resolve` must be imported at module level so `monkeypatch.setattr(rr, "resolve", ...)` patches it at the correct binding. Add:

```python
# At module top (after other imports):
from vctl.resolver import resolve
```

And in `run()`, remove the local `from vctl.resolver import resolve` inline import, relying on the module-level name instead.

- [ ] Move `from vctl.resolver import resolve` to module top of `rolling_restart.py` and remove the inline import inside `run()`.

#### Step 4 — Run tests (expect 30 passing)

```bash
.venv/bin/pytest tests/test_commands_rolling_restart.py -x -q
```

Expected: `30 passed`.

- [ ] Confirm 30 passed.

#### Step 5 — Ruff + mypy gate

```bash
.venv/bin/ruff check src/vctl/commands/rolling_restart.py
.venv/bin/ruff format --check src/vctl/commands/rolling_restart.py
.venv/bin/mypy --strict src/vctl/commands/rolling_restart.py
```

- [ ] All three gates pass.

---

### Task 7 — `cli.py` registration + dispatch test

**What we build:** Register `"rolling-restart"` in `_COMMANDS`, add 1 dispatch test to the existing CLI test file.

**CHECKPOINT AFTER TASK 7:** `vctl rolling-restart` dispatches cleanly from top-level CLI.

#### Step 1 — Write the failing test

Open `tests/test_cli.py` and append (or find the appropriate test class):

```python
def test_rolling_restart_command_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    """'vctl rolling-restart' must be found in _COMMANDS and dispatch without ImportError."""
    from vctl.cli import _COMMANDS, _dispatch
    import argparse

    assert "rolling-restart" in _COMMANDS, (
        "'rolling-restart' missing from _COMMANDS in cli.py"
    )
    # Verify the module is importable (no syntax / import errors).
    import importlib
    mod = importlib.import_module(_COMMANDS["rolling-restart"])
    assert callable(getattr(mod, "run", None)), "rolling_restart.run must be callable"
```

- [ ] Append `test_rolling_restart_command_dispatches` to `tests/test_cli.py`.

#### Step 2 — Run the test (expect `AssertionError: 'rolling-restart' missing from _COMMANDS`)

```bash
.venv/bin/pytest tests/test_cli.py::test_rolling_restart_command_dispatches -x -q 2>&1 | head -15
```

- [ ] Confirm the test fails with `AssertionError` about missing key.

#### Step 3 — Modify `src/vctl/cli.py`

Add `"rolling-restart"` to the `_COMMANDS` dict (NOT to `_PROFILE_AWARE` — rolling-restart operates per-pool, not per-profile):

```python
_COMMANDS: dict[str, str] = {
    "info": "vctl.commands.info",
    "profiles": "vctl.commands.profiles",
    "args": "vctl.commands.args_cmd",
    "preflight": "vctl.commands.preflight",
    "serve": "vctl.commands.serve",
    "stop": "vctl.commands.stop",
    "lb": "vctl.commands.lb",
    "config": "vctl.commands.config_cmd",
    "init-config": "vctl.commands.init_config",
    "rolling-restart": "vctl.commands.rolling_restart",   # Phase 3
}
```

- [ ] Edit `src/vctl/cli.py` to add `"rolling-restart": "vctl.commands.rolling_restart"` to `_COMMANDS`.

#### Step 4 — Run all tests (expect all passing)

```bash
.venv/bin/pytest tests/test_cli.py tests/test_commands_rolling_restart.py -x -q
```

Expected: all tests pass. The new dispatch test should now pass; all rolling-restart tests continue passing.

- [ ] Confirm all tests in both files pass.

#### Step 5 — Ruff + mypy gate on modified files

```bash
.venv/bin/ruff check src/vctl/cli.py src/vctl/commands/rolling_restart.py
.venv/bin/ruff format --check src/vctl/cli.py src/vctl/commands/rolling_restart.py
.venv/bin/mypy --strict src/vctl
```

- [ ] All three gates pass for both modified files.

---

### Task 8 — Final gates + version bump + docs

**What we build:** Version bump across 3 files, CHANGELOG entry, CLI-REFERENCE section, then run the full 4-gate CI pipeline.

**CHECKPOINT AFTER TASK 8:** 4-gate pipeline green; ready to merge.

#### Step 1 — Bump version in 3 files

**`src/vctl/__init__.py`** — change `__version__`:

```python
# Before:
__version__ = "0.6.0"
# After:
__version__ = "0.7.0"
```

**`pyproject.toml`** — update `version` field:
```toml
# Before:
version = "0.6.0"
# After:
version = "0.7.0"
```

**`tests/test_smoke.py`** — find the version assertion and update it:
```python
# Before (find the line asserting "0.6.0"):
assert __version__ == "0.6.0"
# After:
assert __version__ == "0.7.0"
```

- [ ] Edit `src/vctl/__init__.py`: `__version__ = "0.7.0"`.
- [ ] Edit `pyproject.toml`: `version = "0.7.0"`.
- [ ] Edit `tests/test_smoke.py`: update the version assertion to `"0.7.0"`.

#### Step 2 — Prepend CHANGELOG entry

Open `docs/CHANGELOG.md` and prepend after the `# Changelog` header line:

```markdown
## [0.7.0] - 2026-05-04

### Added

- **`vctl rolling-restart --pool <name>`** — sequential, halt-on-failure rolling restart of every endpoint in a pool. ssh-loops to each ep's host, runs `vctl serve restart` remotely, polls HAProxy until ep returns to `UP` before moving to next. Closes the last gap in the vllm lifecycle architecture (Phase 3 of 3).
- **Idempotent + resumable.** Per-pool session file at `~/.vctl/lb/rolling-restart/<pool>.json`. Re-running `vctl rolling-restart --pool X` after a failure auto-resumes: verifies the failed ep is now `UP` (via HAProxy stats), then continues with `pending` eps. Use `--fresh` to force a clean start, `--status` to inspect session, `--abort` to clear it.
- **Strict ready definition** — moves to next ep only after LB health check shows `UP` (default 60s timeout via `--ready-timeout`).
- **Halt-on-failure** — first restart failure stops the run. Operator investigates manually then re-runs `vctl rolling-restart --pool X` (auto-resume).
- **`--dry-run`, `--quiet`, `--ssh-user`, `--remote-vctl-path`** flags for operational flexibility.
```

- [ ] Prepend the `## [0.7.0]` entry to `docs/CHANGELOG.md` (after the header, before `## [0.6.0]`).

#### Step 3 — Add CLI-REFERENCE section

Open `docs/CLI-REFERENCE.md` and add a new `### vctl rolling-restart` section. Find the appropriate alphabetical location (after `vctl profiles`, before `vctl serve`) or append at the end if structure is flat. Content:

```markdown
### vctl rolling-restart

```
vctl rolling-restart --pool <name> [FLAGS]
```

Sequential, halt-on-failure rolling restart of every endpoint in a named pool. For each endpoint: ssh to the worker host, run `vctl serve restart`, poll HAProxy until the endpoint returns `UP`, then move to the next. State is persisted to `~/.vctl/lb/rolling-restart/<pool>.json` so a failed or interrupted run can be resumed by re-running the same command.

**Required flag:**

| Flag | Description |
|---|---|
| `--pool NAME` | Target pool name. Must match a pool configured in `cluster.yaml`. |

**Mode flags (mutually exclusive):**

| Flag | Description |
|---|---|
| `--fresh` | Delete any existing session file before starting; force a fresh run over all endpoints. |
| `--status` | Print the current session file (or "no session in progress") and exit 0. No changes made. |
| `--abort` | Delete the session file if present and exit 0. |
| `--dry-run` | Print what would happen per endpoint without ssh-ing or writing a session file. |

**Tuning flags:**

| Flag | Default | Description |
|---|---|---|
| `--ready-timeout SECONDS` | 60 | Seconds to wait for HAProxy `UP` status after ssh returns 0. |
| `--vllm-timeout SECONDS` | 600 | Seconds the remote `vctl serve restart` is allowed to take (ssh subprocess timeout). |
| `--ssh-user USER` | (ssh config) | Override ssh username for all worker connections. |
| `--remote-vctl-path PATH` | (login shell) | Absolute path to `vctl` on the remote host. If omitted, uses `bash -lc 'vctl serve restart'` to ensure PATH is loaded. |
| `--quiet` | false | Suppress per-endpoint progress lines; print only the final summary. |

**Exit codes:**

| Code | Meaning |
|---|---|
| 0 | All endpoints restarted and verified UP. Session file deleted. |
| 1 | Restart or health-check failure on an endpoint, or operator aborted resume. Session file preserved. |
| 3 | Unknown pool name. |
| 4 | Rolling restart already in progress for this pool (`in_progress: true` in session file). Use `--abort` to clear it. |

**Session file location:** `~/.vctl/lb/rolling-restart/<pool>.json`

**Resume behaviour:** After a halt-on-failure (exit 1), re-running `vctl rolling-restart --pool <name>` automatically resumes: first verifies the failed endpoint's HAProxy status (5s probe); if `UP` it is moved to `completed`; if still DOWN the operator is prompted for (a) skip, (b) retry, or (c) abort.
```

- [ ] Add the `### vctl rolling-restart` section to `docs/CLI-REFERENCE.md`.

#### Step 4 — Run the full 4-gate CI pipeline

```bash
# Gate 1: ruff lint
.venv/bin/ruff check .

# Gate 2: ruff format
.venv/bin/ruff format --check .

# Gate 3: mypy strict
.venv/bin/mypy --strict src/vctl

# Gate 4: pytest coverage gate
.venv/bin/pytest -q --cov=vctl --cov-report=term-missing --cov-fail-under=50
```

Fix any failures before proceeding to Step 5.

Common issues to watch:
- `mypy --strict` may flag `dict[str, object]` indexing issues in `_run_resume` — use `cast()` or explicit type: ignore comments where needed.
- `ruff` may flag unused imports or line-length violations in the new module.
- Coverage gate: the new module adds many lines; verify the 50% threshold is still met.

- [ ] Gate 1 (`ruff check .`) passes with 0 errors.
- [ ] Gate 2 (`ruff format --check .`) passes with no diffs.
- [ ] Gate 3 (`mypy --strict src/vctl`) passes with 0 errors.
- [ ] Gate 4 (`pytest --cov-fail-under=50`) passes (≥50% coverage, all tests pass).

#### Step 5 — Commit

```bash
git add src/vctl/commands/rolling_restart.py \
        src/vctl/cli.py \
        src/vctl/__init__.py \
        pyproject.toml \
        tests/test_commands_rolling_restart.py \
        tests/test_cli.py \
        tests/test_smoke.py \
        docs/CHANGELOG.md \
        docs/CLI-REFERENCE.md

git commit -m "$(cat <<'EOF'
feat: vctl rolling-restart (Phase 3) — sequential per-pool ep restart via ssh

Adds `vctl rolling-restart --pool <name>`: ssh-loops through every registered
endpoint, calls `vctl serve restart` remotely, polls HAProxy until the ep
reports UP, then advances to the next. Halt-on-failure with resumable session
file at ~/.vctl/lb/rolling-restart/<pool>.json.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

- [ ] Commit all 9 changed files with the message above.

---

## Acceptance Test Map

| AT | Covered by | Task |
|---|---|---|
| AT-1 (fresh full success) | `test_fresh_run_full_success_deletes_session_file` | Task 4 |
| AT-2 (halt on failure persists state) | `test_fresh_run_halt_on_failure_persists_session_file` | Task 4 |
| AT-3 (resume verifies UP ep) | `test_resume_verifies_failed_ep_now_up_marks_completed` | Task 5 |
| AT-4 (resume prompts on still-DOWN) | `test_resume_failed_ep_still_down_skip_choice`, `test_resume_failed_ep_still_down_retry_choice`, `test_resume_failed_ep_still_down_abort_choice` | Task 5 |
| AT-5 (`--fresh` deletes + restarts) | `test_fresh_deletes_existing_session_then_runs` | Task 6 |
| AT-6 (`--status` prints content) | `test_status_with_no_session_prints_no_session`, `test_status_with_active_session_prints_content` | Task 6 |
| AT-7 (`--abort` deletes file) | `test_abort_deletes_session_file`, `test_abort_with_no_session_returns_0_silently` | Task 6 |
| AT-8 (`--dry-run` no action) | `test_restart_one_ep_dry_run_skips_ssh` | Task 3 |
| AT-9 (concurrency guard) | `test_fresh_run_concurrency_guard_returns_4` | Task 4 |
| AT-10 (gates) | Task 8 final pipeline | Task 8 |

---

## Checkpoints

- **After Task 3:** per-ep restart primitive unit-tested in isolation. State machine + ssh + verify all mocked. 15 tests passing.
- **After Task 5:** full fresh + resume flows unit-tested. State file lifecycle verified. 25 tests passing.
- **After Task 7:** `vctl rolling-restart` dispatches cleanly from top-level CLI. 31+ tests passing.
- **After Task 8:** 4-gate pipeline green (ruff check, ruff format --check, mypy --strict, pytest --cov-fail-under=50); ready to merge.

---

## Key Design Decisions (reference for implementer)

1. **`_SessionFile` owns locking** — `fcntl.flock` on a sibling `.lock` file (never on the data file). Mirrors `BackendState._locked()` exactly. This means the lock survives `os.replace` swaps and two pools can run concurrently (separate lock files).

2. **Module-level imports for monkeypatching** — `_fetch_haproxy_stats`, `lb_admin_client`, `subprocess`, `sys`, `time`, `resolve` are ALL imported at module top. Tests patch them at `vctl.commands.rolling_restart.<name>`. Do NOT inline-import inside functions (breaks patching).

3. **Fresh `lb_admin_client` per `_verify_ep_up` call** — the HAProxy admin socket closes after each response. Never reuse a client across two commands. `_verify_ep_up` opens a new client per poll iteration.

4. **`--pool` is required, NOT in `_PROFILE_AWARE`** — rolling-restart is pool-scoped, not profile-scoped. The top-level `--profile` flag on `ns` is available but unused; pool resolution goes via `LbManager.lb.pools` directly.

5. **Halt-on-first-failure, no auto-retry** — the run loop stops on the first `"failed"` outcome. The operator investigates and re-runs. `_run_resume`'s "retry" path in the prompt is opt-in, not automatic.

6. **`--dry-run` skips session file entirely** — no session file written or deleted in dry-run mode. HAProxy stats not queried. Only `subprocess.run` + `_verify_ep_up` are skipped; argparse and pool validation still run.

7. **Argparse mutual exclusivity via `add_mutually_exclusive_group()`** — `--fresh`, `--status`, `--abort`, `--dry-run` share a group. argparse handles the error message (exit 2) automatically; no manual flag validation needed in `run()`.
