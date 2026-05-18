# TmuxSession Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use super-agent-skills:subagent-driven-development (recommended) or super-agent-skills:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the four tmux helpers (`tmux_run_detached`, `tmux_run_detached_argv`, `tmux_kill`, `tmux_session_exists`) and the duplicated env/kill plumbing in 3 callers with a single `TmuxSession` class providing full-snapshot env injection via `tmux new-session -e KEY=VAL`.

**Architecture:** New `src/vctl/tmux.py` with `TmuxSession` class. `_validate_tmux_name` and `tmux_session_exists` move from `platform.py` to `tmux.py`; `platform.py` re-exports them during migration then they are deleted. 3 callers (LbManager, VllmManager, lmmseval) migrate to `TmuxSession`. Old helpers deleted in the final task.

**Tech Stack:** Python 3.10+, stdlib + psutil, mypy --strict, ruff E,F,W,I,B,UP,SIM,N. tmux 3.2+ required (deployed env is tmux 3.4).

---

## File Map

| File | Status | Purpose |
|---|---|---|
| `src/vctl/tmux.py` | NEW | `TmuxSession`, `_validate_tmux_name`, `tmux_session_exists`, `_validate_env`, `_check_tmux_version` |
| `src/vctl/platform.py` | MODIFY (Task 1 re-export, Task 8 delete) | Remove deprecated tmux helpers after all callers migrated |
| `src/vctl/lb/manager.py` | MODIFY Task 6 | Import `TmuxSession`; replace `tmux_run_detached_argv`, `tmux_kill`, `tmux_session_exists` calls |
| `src/vctl/vllm_manager.py` | MODIFY Task 7 | Import `TmuxSession`; replace `env_cmd` prefix + `tmux_*` calls |
| `src/vctl/commands/lmmseval.py` | MODIFY Task 8 | Replace `_build_env_exports` + `tmux_run_detached` with `TmuxSession` |
| `tests/test_tmux.py` | NEW | Unit + integration tests for `TmuxSession` (all 10 ATs) |
| `tests/test_lb_manager_b.py` | MODIFY Task 6 | Update `monkeypatch` targets from `tmux_run_detached_argv` / `tmux_kill` to `TmuxSession` |
| `tests/test_vllm_manager.py` | MODIFY Task 7 | Update `monkeypatch` targets from `tmux_session_exists` / `tmux_run_detached_argv` / `tmux_kill` to `TmuxSession` |
| `tests/test_platform.py` | MODIFY Task 8 | Drop tests for the deleted helpers |
| `pyproject.toml` | MODIFY Task 8 | Version bump `0.7.4` → `0.8.0` |
| `src/vctl/__init__.py` | MODIFY Task 8 | `__version__` → `"0.8.0"` |
| `tests/test_smoke.py` | MODIFY Task 8 | Version assert |
| `docs/CHANGELOG.md` | MODIFY Task 8 | 0.8.0 entry |

---

## TASK ORDERING (8 tasks, single stream)

---

### Task 1: TmuxSession skeleton + `_validate_tmux_name` + `tmux_session_exists`

**What we build:** New module `src/vctl/tmux.py` with the name-validation helpers and the `TmuxSession` class skeleton (constructor + `exists()` delegation only). `platform.py` gains re-exports of the moved names so existing callers don't break yet. Four tests cover name validation and the `TmuxSession.__init__` guard.

**Files:**
- Create: `src/vctl/tmux.py`
- Modify: `src/vctl/platform.py` (add re-exports at bottom)
- Test: `tests/test_tmux.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tmux.py`:

```python
"""Unit tests for TmuxSession (Tasks 1-5) + integration tests."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_ok() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


# ---------------------------------------------------------------------------
# Task 1 — _validate_tmux_name + TmuxSession.__init__
# ---------------------------------------------------------------------------

def test_validate_tmux_name_valid() -> None:
    from vctl.tmux import _validate_tmux_name
    _validate_tmux_name("vctl-lb")   # no exception
    _validate_tmux_name("vctl.lb")   # dots allowed
    _validate_tmux_name("sess_1")    # underscores allowed


def test_validate_tmux_name_rejects_slash() -> None:
    from vctl.tmux import _validate_tmux_name
    with pytest.raises(ValueError, match="invalid tmux session name"):
        _validate_tmux_name("bad/name")


def test_validate_tmux_name_rejects_empty() -> None:
    from vctl.tmux import _validate_tmux_name
    with pytest.raises(ValueError, match="invalid tmux session name"):
        _validate_tmux_name("")


def test_validate_tmux_name_rejects_space() -> None:
    from vctl.tmux import _validate_tmux_name
    with pytest.raises(ValueError, match="invalid tmux session name"):
        _validate_tmux_name("bad name")


# AT-10: TmuxSession raises ValueError on invalid name at __init__ time
@pytest.mark.parametrize("bad_name", ["bad/name", "bad name", "", "has\ttab"])
def test_at10_invalid_name_raises_at_init(
    bad_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_calls: list[list[str]] = []
    monkeypatch.setattr(
        "vctl.tmux.subprocess.run",
        lambda a, **k: run_calls.append(a) or _fake_ok(),
    )
    from vctl.tmux import TmuxSession
    with pytest.raises(ValueError, match="invalid tmux session name"):
        TmuxSession(bad_name)
    assert not run_calls
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_tmux.py -x -q 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'vctl.tmux'`

- [ ] **Step 3: Implement `src/vctl/tmux.py` skeleton**

Create `src/vctl/tmux.py`:

```python
"""TmuxSession — unified tmux session lifecycle management.

Replaces the four helpers (tmux_run_detached, tmux_run_detached_argv,
tmux_kill, tmux_session_exists) and the duplicated env/kill plumbing in
LbManager, VllmManager, and commands/lmmseval.

Requires tmux 3.2+ (deployed env: tmux 3.4).  The -e KEY=VALUE flag for
tmux new-session was introduced in tmux 3.2 and injects env vars into the
new session regardless of the tmux server's own stale environment cache.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shlex
import signal
import subprocess
from pathlib import Path

import psutil

_LOG = logging.getLogger(__name__)
_TMUX_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")

# Module-level tmux version cache — checked once per process.
_TMUX_VERSION_OK: bool | None = None


def _validate_tmux_name(name: str) -> None:
    """Raise ValueError if name is not a safe tmux session name."""
    if not _TMUX_NAME_RE.fullmatch(name):
        raise ValueError(
            f"invalid tmux session name {name!r}; must match [A-Za-z0-9_.-]+"
        )


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
        if "=" in k or not k:
            raise ValueError(f"invalid env key {k!r}: empty or contains '='")
        if "\n" in v or "\x00" in v:
            raise ValueError(
                f"invalid env value for {k!r}: contains newline or NUL"
            )


def _check_tmux_version() -> None:
    """Raise RuntimeError if installed tmux is older than 3.2.

    Result is cached in _TMUX_VERSION_OK after first check so subsequent
    TmuxSession.start() calls in the same process pay no subprocess cost.
    """
    global _TMUX_VERSION_OK
    if _TMUX_VERSION_OK is True:
        return
    try:
        proc = subprocess.run(
            ["tmux", "-V"], capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        raise RuntimeError("tmux not installed") from None
    m = re.match(r"tmux (\d+)\.(\d+)", proc.stdout)
    if not m or (int(m.group(1)), int(m.group(2))) < (3, 2):
        raise RuntimeError(
            f"tmux 3.2+ required; found {proc.stdout.strip()!r}"
        )
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
        pipeline with `source`).

        Raises RuntimeError if the session already exists.
        Raises RuntimeError if tmux is not installed or version < 3.2.
        Raises ValueError on invalid session name (caught at __init__) or
        invalid env entries.
        """
        _check_tmux_version()

        if self.exists():
            raise RuntimeError(
                f"tmux session {self.name!r} already exists; "
                "call kill() first or use a different name"
            )

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

        if self.log_path is not None:
            subprocess.run(
                [
                    "tmux", "pipe-pane", "-t", self.name, "-o",
                    f"cat >> {self.log_path}",
                ],
                check=False,
            )

    def pane_pid(self) -> int | None:
        """Return the PID of the foreground process in the session's first pane.

        Uses `tmux list-panes -t NAME -F '#{pane_pid}'`.
        Returns None if the session does not exist or the PID cannot be parsed.
        """
        try:
            result = subprocess.run(
                ["tmux", "list-panes", "-t", self.name, "-F", "#{pane_pid}"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                return None
            raw = result.stdout.strip().splitlines()
            if not raw:
                return None
            return int(raw[0])
        except (FileNotFoundError, ValueError):
            return None

    def kill(self, *, tree: bool = True, grace_s: float = 5.0) -> None:
        """Terminate the session's process tree then kill the tmux session.

        Algorithm:
          1. If tree=True: collect pane_pid() + all psutil descendants.
             SIGTERM all; poll for exit up to grace_s; SIGKILL survivors.
          2. tmux kill-session -t NAME (idempotent — check=False).

        Idempotent: safe to call when session does not exist.
        kill(tree=False) skips psutil tree-kill — use for callers (e.g.
        LbManager.stop) where the process was already terminated via pidfile
        and only an empty pane remains.
        """
        if not self.exists():
            return

        if tree:
            pid = self.pane_pid()
            if pid is not None:
                try:
                    root = psutil.Process(pid)
                    procs = root.children(recursive=True) + [root]
                except psutil.NoSuchProcess:
                    procs = []

                for p in procs:
                    with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                        p.send_signal(signal.SIGTERM)

                _, alive = psutil.wait_procs(procs, timeout=grace_s)
                for p in alive:
                    with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                        p.send_signal(signal.SIGKILL)

        try:
            subprocess.run(
                ["tmux", "kill-session", "-t", self.name],
                check=False,
                capture_output=True,
            )
        except FileNotFoundError:
            pass  # tmux gone — nothing to clean up
```

- [ ] **Step 4: Add `TmuxSession` re-export to `src/vctl/platform.py`**

`platform.py` already defines `_validate_tmux_name` and `tmux_session_exists` —
those don't need re-exporting (they'll be deleted in Task 8). Only `TmuxSession`
is new; add a single re-export at the bottom of `src/vctl/platform.py` so any
code that imports from `platform` during migration can reach it:

```python
# TmuxSession re-export — callers should import from vctl.tmux directly;
# this re-export exists only so that code that imports platform during migration
# can reach TmuxSession without a separate import change.
from vctl.tmux import TmuxSession as TmuxSession  # noqa: F401
```

Note: `_validate_tmux_name` and `tmux_session_exists` remain as their original
definitions in `platform.py` until Task 8 removes them entirely.  No duplicate
re-import is needed.

- [ ] **Step 5: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_tmux.py::test_validate_tmux_name_valid \
  tests/test_tmux.py::test_validate_tmux_name_rejects_slash \
  tests/test_tmux.py::test_validate_tmux_name_rejects_empty \
  tests/test_tmux.py::test_validate_tmux_name_rejects_space \
  "tests/test_tmux.py::test_at10_invalid_name_raises_at_init[bad/name]" \
  "tests/test_tmux.py::test_at10_invalid_name_raises_at_init[bad name]" \
  "tests/test_tmux.py::test_at10_invalid_name_raises_at_init[]" \
  "tests/test_tmux.py::test_at10_invalid_name_raises_at_init[has\ttab]" \
  -v
```

Expected: all 8 PASS

- [ ] **Step 6: Run ruff + mypy on new module**

```bash
.venv/bin/ruff check src/vctl/tmux.py src/vctl/platform.py
.venv/bin/ruff format --check src/vctl/tmux.py src/vctl/platform.py
.venv/bin/mypy --strict src/vctl/tmux.py src/vctl/platform.py
```

Expected: no errors

- [ ] **Step 7: Run full test suite to confirm no regressions**

```bash
.venv/bin/pytest -q --tb=short 2>&1 | tail -20
```

Expected: same pass/fail count as before (the re-exports in platform.py keep existing callers intact)

- [ ] **Step 8: Commit**

```bash
git add src/vctl/tmux.py src/vctl/platform.py tests/test_tmux.py
git commit -m "feat(tmux): add TmuxSession skeleton + _validate_tmux_name + tmux_session_exists"
```

---

### Task 2: `TmuxSession.start()` — env flags + `_validate_env` + version check + double-start guard

**What we build:** The core behavior of `start()` — already implemented in the skeleton above. This task adds the targeted tests that prove the behavior: list-form shlex join, str-form verbatim passthrough, `env=None` snapshots at `start()` time (the headline regression test), `_validate_env` rejections, double-start guard, and the tmux-not-installed RuntimeError path. Also tests `_check_tmux_version` caching behavior.

**Files:**
- Modify: `tests/test_tmux.py` (append new test functions)

Note: `start()` is already implemented in Task 1's skeleton. This task is purely test-coverage.

- [ ] **Step 1: Append `start()` tests to `tests/test_tmux.py`**

Add the following functions after the Task 1 tests:

```python
# ---------------------------------------------------------------------------
# Task 2 — TmuxSession.start(): env flags, validation, double-start guard
# ---------------------------------------------------------------------------

def test_start_list_form_passes_env_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """List-form argv is shlex-joined; env dict becomes -e KEY=VAL flags."""
    calls: list[list[str]] = []
    monkeypatch.setattr("vctl.tmux._TMUX_VERSION_OK", True)
    monkeypatch.setattr(
        "vctl.tmux.subprocess.run",
        lambda argv, **kw: calls.append(list(argv)) or _fake_ok(),
    )
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    from vctl.tmux import TmuxSession
    sess = TmuxSession("test-sess", env={"FOO": "bar", "BAZ": "qux"})
    sess.start(["echo", "hello"])
    new_sess_call = calls[0]
    assert "-e" in new_sess_call
    assert "FOO=bar" in new_sess_call
    assert "BAZ=qux" in new_sess_call
    # shlex-joined list form: "echo hello"
    assert new_sess_call[-1] == "echo hello"


def test_start_str_form_passed_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """Str-form argv is forwarded verbatim as the final tmux argument."""
    calls: list[list[str]] = []
    monkeypatch.setattr("vctl.tmux._TMUX_VERSION_OK", True)
    monkeypatch.setattr(
        "vctl.tmux.subprocess.run",
        lambda argv, **kw: calls.append(list(argv)) or _fake_ok(),
    )
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    from vctl.tmux import TmuxSession
    sess = TmuxSession("test-sess", env={})
    sess.start("source /venv/bin/activate && bash run.sh")
    assert calls[0][-1] == "source /venv/bin/activate && bash run.sh"


def test_env_none_snapshots_at_start_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Headline behavior: env=None snapshots os.environ at start() call time,
    NOT at __init__ time.  This is what fixes the stale-tmux-server-cache footgun."""
    calls: list[list[str]] = []
    monkeypatch.setattr("vctl.tmux._TMUX_VERSION_OK", True)
    monkeypatch.setattr(
        "vctl.tmux.subprocess.run",
        lambda argv, **kw: calls.append(list(argv)) or _fake_ok(),
    )
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    monkeypatch.setenv("VCTL_TEST_KEY", "before_init")
    from vctl.tmux import TmuxSession
    sess = TmuxSession("test-sess")  # env=None — must NOT snapshot os.environ now
    monkeypatch.setenv("VCTL_TEST_KEY", "after_init_before_start")
    sess.start(["echo"])
    new_sess_call = calls[0]
    # Value at start() time must win, not value at __init__ time.
    assert "VCTL_TEST_KEY=after_init_before_start" in new_sess_call
    assert "VCTL_TEST_KEY=before_init" not in new_sess_call


def test_validate_env_rejects_key_with_equals(monkeypatch: pytest.MonkeyPatch) -> None:
    """_validate_env rejects env keys that contain '='."""
    monkeypatch.setattr("vctl.tmux._TMUX_VERSION_OK", True)
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    from vctl.tmux import TmuxSession
    sess = TmuxSession("test-sess", env={"K=BAD": "v"})
    with pytest.raises(ValueError, match="invalid env key"):
        sess.start(["echo"])


def test_validate_env_rejects_value_with_newline(monkeypatch: pytest.MonkeyPatch) -> None:
    """_validate_env rejects env values containing newline."""
    monkeypatch.setattr("vctl.tmux._TMUX_VERSION_OK", True)
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    from vctl.tmux import TmuxSession
    sess = TmuxSession("test-sess", env={"K": "value\nwith\nnewline"})
    with pytest.raises(ValueError, match="newline or NUL"):
        sess.start(["echo"])


# AT-7: start() raises RuntimeError when session already exists
def test_at7_start_raises_on_existing_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """AT-7: RuntimeError raised if session already exists; no new-session issued."""
    monkeypatch.setattr("vctl.tmux._TMUX_VERSION_OK", True)
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: True)
    run_calls: list[list[str]] = []
    monkeypatch.setattr(
        "vctl.tmux.subprocess.run",
        lambda a, **k: run_calls.append(list(a)) or _fake_ok(),
    )
    from vctl.tmux import TmuxSession
    with pytest.raises(RuntimeError, match="already exists"):
        TmuxSession("vctl-lb", env={}).start(["haproxy"])
    assert not run_calls


def test_start_raises_runtime_if_tmux_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """start() raises RuntimeError when tmux binary is not on PATH."""
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)

    def fake_run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError

    monkeypatch.setattr("vctl.tmux.subprocess.run", fake_run)
    # Reset version cache so the FileNotFoundError bubbles from _check_tmux_version
    monkeypatch.setattr("vctl.tmux._TMUX_VERSION_OK", None)
    from vctl.tmux import TmuxSession
    sess = TmuxSession("vctl-lb", env={})
    with pytest.raises(RuntimeError, match="tmux not installed"):
        sess.start(["haproxy"])


def test_check_tmux_version_rejects_old_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_check_tmux_version raises RuntimeError for tmux < 3.2."""
    import subprocess as _sp
    monkeypatch.setattr("vctl.tmux._TMUX_VERSION_OK", None)
    monkeypatch.setattr(
        "vctl.tmux.subprocess.run",
        lambda argv, **kw: _sp.CompletedProcess(
            args=argv, returncode=0, stdout="tmux 3.1\n", stderr=""
        ),
    )
    from vctl.tmux import _check_tmux_version
    with pytest.raises(RuntimeError, match="tmux 3.2\\+ required"):
        _check_tmux_version()


# AT-1: vllm PATH in env flags
def test_at1_vllm_path_in_env_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """AT-1: PATH set in env dict appears as -e PATH=... in tmux new-session call."""
    calls: list[list[str]] = []
    monkeypatch.setattr("vctl.tmux._TMUX_VERSION_OK", True)
    monkeypatch.setattr(
        "vctl.tmux.subprocess.run",
        lambda a, **k: calls.append(list(a)) or _fake_ok(),
    )
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    from vctl.tmux import TmuxSession
    sess = TmuxSession("vctl-vllm-qwen", env={"PATH": "/venv/bin:/usr/bin"})
    sess.start(["vllm", "serve", "model"])
    new_sess_call = calls[0]
    assert any(arg == "PATH=/venv/bin:/usr/bin" for arg in new_sess_call)


# AT-2: lb manager env propagated
def test_at2_lb_manager_env_propagated(monkeypatch: pytest.MonkeyPatch) -> None:
    """AT-2: CUSTOM_VAR in env dict appears in tmux new-session argv."""
    calls: list[list[str]] = []
    monkeypatch.setattr("vctl.tmux._TMUX_VERSION_OK", True)
    monkeypatch.setattr(
        "vctl.tmux.subprocess.run",
        lambda a, **k: calls.append(list(a)) or _fake_ok(),
    )
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    from vctl.tmux import TmuxSession
    sess = TmuxSession("vctl-lb", env={"CUSTOM_VAR": "sentinel"})
    sess.start(["haproxy", "-f", "/tmp/ha.cfg"])
    assert any("CUSTOM_VAR=sentinel" == arg for arg in calls[0])
```

- [ ] **Step 2: Run new tests to verify they pass**

```bash
.venv/bin/pytest tests/test_tmux.py -k "test_start or test_env or test_validate_env or test_check_tmux or test_at1 or test_at2 or test_at7" -v
```

Expected: all PASS (implementation was written in Task 1)

- [ ] **Step 3: Run ruff + mypy**

```bash
.venv/bin/ruff check tests/test_tmux.py
.venv/bin/ruff format --check tests/test_tmux.py
.venv/bin/mypy --strict src/vctl/tmux.py
```

Expected: no errors

- [ ] **Step 4: Commit**

```bash
git add tests/test_tmux.py
git commit -m "test(tmux): TmuxSession.start() env flags + validation + double-start guard (AT-1, AT-2, AT-7)"
```

---

### Task 3: `TmuxSession.start()` — `log_path` + pipe-pane

**What we build:** Tests that prove `log_path` triggers the pipe-pane subprocess call, and that omitting `log_path` does not. Also the integration test for log capture.

**Files:**
- Modify: `tests/test_tmux.py` (append log_path tests)

- [ ] **Step 1: Append log_path tests to `tests/test_tmux.py`**

```python
# ---------------------------------------------------------------------------
# Task 3 — log_path + pipe-pane
# ---------------------------------------------------------------------------

# AT-9: log_path triggers pipe-pane call
def test_at9_log_path_emits_pipe_pane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AT-9: When log_path is given, a second subprocess.run with pipe-pane is issued."""
    calls: list[list[str]] = []
    monkeypatch.setattr("vctl.tmux._TMUX_VERSION_OK", True)
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    monkeypatch.setattr(
        "vctl.tmux.subprocess.run",
        lambda a, **k: calls.append(list(a)) or _fake_ok(),
    )
    log = tmp_path / "out.log"
    from vctl.tmux import TmuxSession
    TmuxSession("vctl-vllm-qwen", env={}, log_path=log).start(["vllm"])
    pipe_calls = [c for c in calls if "pipe-pane" in c]
    assert len(pipe_calls) == 1
    assert str(log) in " ".join(pipe_calls[0])


def test_no_log_path_skips_pipe_pane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without log_path, no pipe-pane subprocess is issued."""
    calls: list[list[str]] = []
    monkeypatch.setattr("vctl.tmux._TMUX_VERSION_OK", True)
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    monkeypatch.setattr(
        "vctl.tmux.subprocess.run",
        lambda a, **k: calls.append(list(a)) or _fake_ok(),
    )
    from vctl.tmux import TmuxSession
    TmuxSession("vctl-lb", env={}).start(["haproxy", "-f", "/tmp/h.cfg"])
    pipe_calls = [c for c in calls if "pipe-pane" in c]
    assert len(pipe_calls) == 0


# Integration test (requires real tmux 3.2+ on PATH)
@pytest.mark.integration
def test_log_path_captures_output(tmp_path: Path) -> None:
    """Integration: log_path receives stdout via pipe-pane."""
    import time
    from vctl.tmux import TmuxSession
    name = "vctl-test-log"
    log = tmp_path / "session.log"
    sess = TmuxSession(name, env={}, log_path=log)
    sess.start(["echo", "captured-output"])
    try:
        time.sleep(1)
        text = log.read_text() if log.exists() else ""
        assert "captured-output" in text
    finally:
        sess.kill(tree=False)
```

- [ ] **Step 2: Run log_path tests**

```bash
.venv/bin/pytest tests/test_tmux.py -k "log_path or pipe_pane" -v
```

Expected: `test_at9_log_path_emits_pipe_pane` and `test_no_log_path_skips_pipe_pane` PASS; integration test skipped (no `--integration` flag)

- [ ] **Step 3: Commit**

```bash
git add tests/test_tmux.py
git commit -m "test(tmux): log_path pipe-pane coverage (AT-9)"
```

---

### Checkpoint: After Tasks 1-3

- [ ] All tmux unit tests pass: `.venv/bin/pytest tests/test_tmux.py -v --ignore-glob="*integration*" -m "not integration"`
- [ ] Full suite still passes: `.venv/bin/pytest -q --tb=short 2>&1 | tail -10`
- [ ] `mypy --strict src/vctl/tmux.py` passes with no errors
- [ ] `ruff check src/vctl/` passes with no errors
- [ ] Review with human before proceeding

---

### Task 4: `TmuxSession.exists()` + `pane_pid()` + `kill(tree=False)`

**What we build:** Tests for `exists()` (delegation to `tmux_session_exists`), `pane_pid()` (output parsing), `kill(tree=False)` (only does kill-session, never calls psutil), and idempotent kill when session is gone.

**Files:**
- Modify: `tests/test_tmux.py` (append exists/pane_pid/kill tests)

- [ ] **Step 1: Append tests to `tests/test_tmux.py`**

```python
# ---------------------------------------------------------------------------
# Task 4 — exists() + pane_pid() + kill(tree=False)
# ---------------------------------------------------------------------------

def test_exists_delegates_to_tmux_session_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exists() is a thin wrapper around tmux_session_exists."""
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: True)
    from vctl.tmux import TmuxSession
    assert TmuxSession("vctl-lb").exists() is True
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    assert TmuxSession("vctl-lb").exists() is False


def test_pane_pid_parses_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """pane_pid() parses the integer from tmux list-panes -F '#{pane_pid}'."""
    import subprocess as _sp
    result = _sp.CompletedProcess(args=[], returncode=0, stdout="12345\n", stderr="")
    monkeypatch.setattr("vctl.tmux.subprocess.run", lambda *a, **kw: result)
    from vctl.tmux import TmuxSession
    assert TmuxSession("vctl-vllm-qwen", env={}).pane_pid() == 12345


def test_pane_pid_returns_none_if_session_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """pane_pid() returns None when tmux list-panes returns non-zero."""
    import subprocess as _sp
    result = _sp.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
    monkeypatch.setattr("vctl.tmux.subprocess.run", lambda *a, **kw: result)
    from vctl.tmux import TmuxSession
    assert TmuxSession("gone", env={}).pane_pid() is None


def test_pane_pid_returns_none_on_empty_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pane_pid() returns None when list-panes returns 0 but empty stdout."""
    import subprocess as _sp
    result = _sp.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    monkeypatch.setattr("vctl.tmux.subprocess.run", lambda *a, **kw: result)
    from vctl.tmux import TmuxSession
    assert TmuxSession("vctl-lb", env={}).pane_pid() is None


# AT-8: kill is idempotent when session already gone
def test_at8_kill_idempotent_when_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """AT-8: kill() is a no-op and raises no error when session does not exist."""
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    run_calls: list[list[str]] = []
    monkeypatch.setattr(
        "vctl.tmux.subprocess.run",
        lambda a, **k: run_calls.append(list(a)) or _fake_ok(),
    )
    from vctl.tmux import TmuxSession
    TmuxSession("vctl-lb").kill()  # must not raise
    assert not run_calls


# AT-6: kill(tree=False) skips psutil, still calls kill-session
def test_at6_lb_stop_tree_false_no_psutil(monkeypatch: pytest.MonkeyPatch) -> None:
    """AT-6: kill(tree=False) skips psutil.Process; tmux kill-session IS called."""
    import psutil as _psutil
    run_calls: list[list[str]] = []
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: True)
    monkeypatch.setattr(
        "vctl.tmux.subprocess.run",
        lambda a, **k: run_calls.append(list(a)) or _fake_ok(),
    )
    monkeypatch.setattr(
        "vctl.tmux.TmuxSession.pane_pid", lambda self: 999
    )
    psutil_calls: list[int] = []
    monkeypatch.setattr(
        "vctl.tmux.psutil.Process",
        lambda pid: psutil_calls.append(pid),
    )
    from vctl.tmux import TmuxSession
    TmuxSession("vctl-lb").kill(tree=False)
    assert not psutil_calls
    assert any("kill-session" in " ".join(c) for c in run_calls)


# Integration: full roundtrip (requires real tmux)
@pytest.mark.integration
def test_session_start_exists_kill_roundtrip(tmp_path: Path) -> None:
    """Integration: start → exists → pane_pid → kill roundtrip."""
    import time
    from vctl.tmux import TmuxSession
    name = "vctl-test-integration"
    sess = TmuxSession(name, env={"VCTL_TMUX_TEST": "1"})
    sess.start(["sleep", "60"])
    try:
        assert sess.exists()
        pid = sess.pane_pid()
        assert pid is not None and pid > 0
        sess.kill(tree=True)
        time.sleep(0.5)
        assert not sess.exists()
    finally:
        sess.kill(tree=False)  # idempotent cleanup
```

- [ ] **Step 2: Run Task 4 tests**

```bash
.venv/bin/pytest tests/test_tmux.py -k "exists or pane_pid or test_at6 or test_at8" -v -m "not integration"
```

Expected: all 6 new tests PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_tmux.py
git commit -m "test(tmux): exists() + pane_pid() + kill(tree=False) coverage (AT-6, AT-8)"
```

---

### Task 5: `TmuxSession.kill(tree=True)` — psutil tree-kill with grace period

**What we build:** Tests that prove `kill(tree=True)` collects the process tree (root + children), sends SIGTERM to all, calls `psutil.wait_procs`, and SIGKILLs survivors. Also integration test for env propagation into session.

**Files:**
- Modify: `tests/test_tmux.py` (append tree-kill tests)

- [ ] **Step 1: Append tree-kill tests to `tests/test_tmux.py`**

```python
# ---------------------------------------------------------------------------
# Task 5 — kill(tree=True): psutil SIGTERM → wait → SIGKILL
# ---------------------------------------------------------------------------

import signal as _signal


# AT-4: vllm stop tree-kills workers
def test_at4_serve_stop_tree_kills_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """AT-4: kill(tree=True) sends SIGTERM to pane_pid + all children."""
    import signal
    sigtermed: list[int] = []
    killed: list[int] = []

    class FakeProc:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def send_signal(self, sig: int) -> None:
            (sigtermed if sig == signal.SIGTERM else killed).append(self.pid)

        def children(self, recursive: bool = False) -> list["FakeProc"]:
            return [FakeProc(1001), FakeProc(1002)]

    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: True)
    monkeypatch.setattr("vctl.tmux.TmuxSession.pane_pid", lambda s: 1000)
    monkeypatch.setattr("vctl.tmux.psutil.Process", lambda pid: FakeProc(pid))
    monkeypatch.setattr(
        "vctl.tmux.psutil.wait_procs", lambda procs, timeout: ([], [])
    )
    monkeypatch.setattr("vctl.tmux.subprocess.run", lambda *a, **k: _fake_ok())
    from vctl.tmux import TmuxSession
    TmuxSession("vctl-vllm-qwen").kill(tree=True)
    assert set(sigtermed) == {1000, 1001, 1002}


# AT-5: lmmseval stop tree-kills run_loop + 8 accelerate workers
def test_at5_lmmseval_stop_tree_kills_8_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AT-5: kill(tree=True) sends SIGTERM to root (2000) + 8 children (2001-2008)."""
    import signal
    sigtermed: list[int] = []

    class FakeProc:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def send_signal(self, sig: int) -> None:
            if sig == signal.SIGTERM:
                sigtermed.append(self.pid)

        def children(self, recursive: bool = False) -> list["FakeProc"]:
            return [FakeProc(p) for p in range(2001, 2009)]

    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: True)
    monkeypatch.setattr("vctl.tmux.TmuxSession.pane_pid", lambda s: 2000)
    monkeypatch.setattr("vctl.tmux.psutil.Process", lambda pid: FakeProc(pid))
    monkeypatch.setattr(
        "vctl.tmux.psutil.wait_procs", lambda procs, timeout: ([], [])
    )
    monkeypatch.setattr("vctl.tmux.subprocess.run", lambda *a, **k: _fake_ok())
    from vctl.tmux import TmuxSession
    TmuxSession("vctl-lmmseval").kill(tree=True)
    assert set(sigtermed) == set(range(2000, 2009))


def test_kill_tree_sigkills_survivors_after_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kill(tree=True) sends SIGKILL to processes still alive after grace_s."""
    import signal
    sigtermed: list[int] = []
    sigkilled: list[int] = []

    class FakeProc:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def send_signal(self, sig: int) -> None:
            (sigtermed if sig == signal.SIGTERM else sigkilled).append(self.pid)

        def children(self, recursive: bool = False) -> list["FakeProc"]:
            return [FakeProc(5001)]

    survivor = FakeProc(5000)

    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: True)
    monkeypatch.setattr("vctl.tmux.TmuxSession.pane_pid", lambda s: 5000)
    monkeypatch.setattr("vctl.tmux.psutil.Process", lambda pid: FakeProc(pid))
    # Simulate wait_procs returning one survivor (root process)
    monkeypatch.setattr(
        "vctl.tmux.psutil.wait_procs", lambda procs, timeout: ([], [survivor])
    )
    monkeypatch.setattr("vctl.tmux.subprocess.run", lambda *a, **k: _fake_ok())
    from vctl.tmux import TmuxSession
    TmuxSession("vctl-vllm-qwen").kill(tree=True, grace_s=0.01)
    assert 5000 in sigkilled  # survivor received SIGKILL


# Integration: env propagation into real session
@pytest.mark.integration
def test_env_propagation_into_session(tmp_path: Path) -> None:
    """Integration: env vars passed via -e actually reach the pane process."""
    import time
    from vctl.tmux import TmuxSession
    name = "vctl-test-env"
    marker_file = tmp_path / "env_marker.txt"
    sess = TmuxSession(
        name,
        env={"VCTL_MARKER": "hello-from-test"},
    )
    sess.start(f"sh -c 'echo $VCTL_MARKER > {marker_file}; sleep 5'")
    try:
        time.sleep(1)
        assert marker_file.exists()
        assert "hello-from-test" in marker_file.read_text()
    finally:
        sess.kill(tree=False)
```

- [ ] **Step 2: Run Task 5 tests**

```bash
.venv/bin/pytest tests/test_tmux.py -k "test_at4 or test_at5 or test_kill_tree" -v -m "not integration"
```

Expected: all 3 new tests PASS

- [ ] **Step 3: Run full tmux unit test suite**

```bash
.venv/bin/pytest tests/test_tmux.py -v -m "not integration"
```

Expected: all unit tests PASS

- [ ] **Step 4: Run ruff + mypy**

```bash
.venv/bin/ruff check src/vctl/tmux.py tests/test_tmux.py
.venv/bin/ruff format --check src/vctl/tmux.py tests/test_tmux.py
.venv/bin/mypy --strict src/vctl/tmux.py
```

Expected: no errors

- [ ] **Step 5: Commit**

```bash
git add tests/test_tmux.py
git commit -m "test(tmux): kill(tree=True) psutil tree-kill coverage (AT-4, AT-5)"
```

---

### Checkpoint: After Tasks 1-5

- [ ] All tmux unit tests pass: `.venv/bin/pytest tests/test_tmux.py -v -m "not integration"`
- [ ] `mypy --strict src/vctl/tmux.py` passes with 0 errors
- [ ] `ruff check src/vctl/` and `ruff format --check src/vctl/` pass
- [ ] Full suite passes: `.venv/bin/pytest -q --tb=short 2>&1 | tail -15`
- [ ] `TmuxSession` class is complete; all 10 ATs have unit test coverage
- [ ] Review with human before proceeding to caller migration

---

### Task 6: Migrate `LbManager` to `TmuxSession`

**What we build:** `LbManager` switches from the four old helpers to `TmuxSession`. Three call sites change: `start()` replaces `tmux_run_detached_argv` with `TmuxSession(name, env={**os.environ}).start(...)`, `stop()` replaces `tmux_kill` with `TmuxSession(name).kill(tree=False)`, and `status()` replaces `tmux_session_exists` with `TmuxSession(name).exists()`. The `_validate_tmux_name` import in `__init__` switches to `vctl.tmux`. Existing `test_lb_manager_b.py` mock targets are updated from the module-level function names to `TmuxSession` method patches.

**Files:**
- Modify: `src/vctl/lb/manager.py`
- Modify: `tests/test_lb_manager_b.py`

- [ ] **Step 1: Update `src/vctl/lb/manager.py`**

Replace the import block at the top of `src/vctl/lb/manager.py` (lines 18-24):

```python
from vctl.config.models import LbHaproxy
from vctl.lb.installer import ensure_haproxy
from vctl.lb.render import RuntimePaths, render_haproxy_cfg
from vctl.lb.state import BackendState
from vctl.platform import detect_self_ip
from vctl.tmux import TmuxSession, _validate_tmux_name
```

Replace the `start()` method's tmux call (the `tmux_run_detached_argv(...)` call and the log line after it):

```python
        cfg = self.render_config()
        self.cfg_path.write_text(cfg)
        binary = ensure_haproxy()
        # Use TmuxSession so haproxy's pane inherits the caller's full env.
        # env={**os.environ} snapshots the calling process's live environment —
        # this replaces the old no-env tmux_run_detached_argv call (AT-2).
        TmuxSession(
            self.tmux_name,
            env={**os.environ},
        ).start([binary, "-f", str(self.cfg_path), "-p", str(self.pid_path)])
        _LOG.info("haproxy started in tmux session %s", self.tmux_name)
```

Replace the `tmux_kill(self.tmux_name)` call at the end of `stop()`:

```python
        # Tear down the tmux session if it exists (idempotent). tree=False because
        # haproxy was already killed via pidfile SIGTERM above; only an empty pane remains.
        TmuxSession(self.tmux_name).kill(tree=False)
```

Replace the `tmux_session_exists(self.tmux_name)` call in `status()`:

```python
        # 3. tmux-managed (informational only)
        tmux_managed = TmuxSession(self.tmux_name).exists()
```

- [ ] **Step 2: Update `tests/test_lb_manager_b.py`**

Find every `monkeypatch.setattr(...)` and `@patch(...)` call that references the old module-level helpers and update them:

Change `@patch("vctl.lb.manager.tmux_run_detached_argv")` to `@patch("vctl.lb.manager.TmuxSession")` in `test_start_force_calls_stop_then_starts`:

```python
@patch("vctl.lb.manager.detect_self_ip", return_value="10.0.0.1")
@patch("vctl.lb.manager.tmux_session_exists", return_value=False)
@patch("vctl.lb.manager.socket.create_connection", side_effect=OSError)
@patch("vctl.lb.manager.TmuxSession")
@patch("vctl.lb.manager.ensure_haproxy", return_value="/usr/bin/haproxy")
@patch("vctl.lb.manager._verify_pid_is_haproxy", return_value=True)
def test_start_force_calls_stop_then_starts(
    mock_verify: MagicMock,
    mock_haproxy: MagicMock,
    mock_tmux_cls: MagicMock,
    mock_conn: MagicMock,
    mock_tmux_exists: MagicMock,
    mock_ip: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B1: start(force=True) when running must call stop() then proceed."""
    mgr = _make_mgr(tmp_path)
    stop_called = []

    def fake_stop() -> None:
        stop_called.append(True)
        mgr.pid_path.unlink(missing_ok=True)

    monkeypatch.setattr(mgr, "stop", fake_stop)

    pid = os.getpid()
    mgr.pid_path.write_text(str(pid))

    mgr.start(force=True)
    assert stop_called, "stop() must be called when force=True and already running"
    assert mock_tmux_cls.called, "TmuxSession must be instantiated after stop()"
```

Also add an import for `TmuxSession` at the top of the test file so the test for `status()` can verify `tmux_managed`. In any test that previously patched `tmux_session_exists` to control `status()["tmux_managed"]`, update to patch `vctl.lb.manager.TmuxSession`:

In all remaining `@patch("vctl.lb.manager.tmux_session_exists", ...)` decorators, change to `@patch("vctl.lb.manager.TmuxSession")` and adjust the mock instance's `exists.return_value` accordingly. Example for a test that expects `tmux_managed=False`:

```python
@patch("vctl.lb.manager.TmuxSession")
def test_status_tmux_not_managed(mock_tmux_cls: MagicMock, tmp_path: Path) -> None:
    mock_tmux_cls.return_value.exists.return_value = False
    ...
```

- [ ] **Step 3: Run LbManager tests**

```bash
.venv/bin/pytest tests/test_lb_manager_b.py tests/test_f7_f8_f10.py tests/test_f2_f3_f4.py -v --tb=short
```

Expected: all tests PASS

- [ ] **Step 4: Run full suite**

```bash
.venv/bin/pytest -q --tb=short 2>&1 | tail -15
```

Expected: same pass count as before this task

- [ ] **Step 5: Run ruff + mypy**

```bash
.venv/bin/ruff check src/vctl/lb/manager.py
.venv/bin/ruff format --check src/vctl/lb/manager.py
.venv/bin/mypy --strict src/vctl/lb/manager.py
```

Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add src/vctl/lb/manager.py tests/test_lb_manager_b.py
git commit -m "refactor(lb): migrate LbManager to TmuxSession (AT-2, AT-6)"
```

---

### Task 7: Migrate `VllmManager` to `TmuxSession`

**What we build:** `VllmManager` switches from the `env_cmd = ["env", "K=V", ...]` prefix pattern + `tmux_run_detached_argv` to `TmuxSession(name, env=env_overrides, log_path=self.log_path).start(argv)`. The separate `subprocess.run(["tmux", "pipe-pane", ...])` call is removed. `_cleanup_on_failure()` switches from `tmux_kill` to `TmuxSession(name).kill(tree=True)`. `stop()` keeps the `send-keys C-c` step, but replaces the pidfile-poll loop + `tmux_kill` with `TmuxSession(name).kill(tree=True, grace_s=grace)`. All `tmux_session_exists` and `_validate_tmux_name` references switch to `TmuxSession`-based equivalents. Test mock targets updated accordingly.

**Files:**
- Modify: `src/vctl/vllm_manager.py`
- Modify: `tests/test_vllm_manager.py`

- [ ] **Step 1: Update imports in `src/vctl/vllm_manager.py`**

Replace the import block (lines 22-28):

```python
from vctl.platform import detect_self_ip
from vctl.tmux import TmuxSession, _validate_tmux_name
```

- [ ] **Step 2: Update `VllmManager.__init__`**

The `_validate_tmux_name(self.session_name)` call is already using the function — it now imports from `vctl.tmux` (same function, different import origin). No change needed to the call itself, only to the import above.

- [ ] **Step 3: Update `VllmManager.start()`**

Replace the entire `env_cmd` construction + `tmux_run_detached_argv` + separate `subprocess.run(pipe-pane)` block with:

```python
        # Build env for the tmux session: os.environ base + per-profile overrides.
        # TmuxSession passes every key via `-e KEY=VAL` so the tmux server's stale
        # cache is bypassed entirely (fixes the v0.5.4 ninja PATH bug).
        session_env: dict[str, str] = {**os.environ, **env_overrides}

        # Spawn the tmux session with log capture via log_path.
        TmuxSession(
            self.session_name,
            env=session_env,
            log_path=self.log_path,
        ).start(argv)
        _LOG.info("vllm started in tmux session %s", self.session_name)
```

Remove the lines that build `env_cmd` (the `env_cmd: list[str] = ["env"]` block and `env_cmd.extend(argv)`) as well as the separate `subprocess.run(["tmux", "pipe-pane", ...], check=False)` call.

- [ ] **Step 4: Update `VllmManager.start()` double-start guard**

Replace `if tmux_session_exists(self.session_name):` with:

```python
        # Double-start guard.
        if TmuxSession(self.session_name).exists():
```

- [ ] **Step 5: Update `VllmManager._cleanup_on_failure()`**

Replace `tmux_kill(self.session_name)` with:

```python
    def _cleanup_on_failure(self) -> None:
        """Kill tmux session and unlink state files after a start() failure."""
        TmuxSession(self.session_name).kill(tree=True)
        for p in (self.pid_path, self.cmd_path, self.host_path):
            with contextlib.suppress(OSError):
                p.unlink()
```

- [ ] **Step 6: Update `VllmManager.stop()`**

Remove the pidfile-poll loop (the `pid: int | None = None` block through `time.sleep(0.5)`) and the `tmux_kill(self.session_name)` call. Replace with:

```python
        # Send C-c to vllm (clean SIGINT shutdown — flush KV cache, release GPU memory).
        subprocess.run(
            ["tmux", "send-keys", "-t", self.session_name, "C-c", ""],
            check=False,
        )

        # Tree-kill the session: SIGTERM tree → wait grace_s → SIGKILL survivors.
        # Catches vllm + any accelerate worker processes that survive the pane kill.
        grace = float(os.environ.get("VCTL_KILL_GRACE", "30"))
        TmuxSession(self.session_name).kill(tree=True, grace_s=grace)
```

- [ ] **Step 7: Update `VllmManager.status()` and `VllmManager.console()`**

Replace `tmux_alive = tmux_session_exists(self.session_name)` in `status()`:

```python
        # 1. tmux session liveness (informational).
        tmux_alive = TmuxSession(self.session_name).exists()
```

Replace `if not tmux_session_exists(self.session_name):` in `console()`:

```python
        if not TmuxSession(self.session_name).exists():
```

- [ ] **Step 8: Update `tests/test_vllm_manager.py`**

Find every `monkeypatch.setattr(vm_mod, "tmux_session_exists", ...)` and change to `monkeypatch.setattr("vctl.vllm_manager.TmuxSession", ...)` with appropriate `.return_value.exists.return_value`:

```python
# Before:
monkeypatch.setattr(vm_mod, "tmux_session_exists", lambda name: False)
# After:
mock_ts = MagicMock()
mock_ts.return_value.exists.return_value = False
monkeypatch.setattr(vm_mod, "TmuxSession", mock_ts)
```

Find every `monkeypatch.setattr(vm_mod, "tmux_run_detached_argv", lambda name, argv: None)` and remove it (TmuxSession is mocked at class level now).

Find every `monkeypatch.setattr(vm_mod, "tmux_kill", lambda name: ...)`:
- In `test_start_pid_discovery_timeout_kills_session`: change to capture `TmuxSession.kill` calls via the mock:

```python
    killed: list[str] = []
    mock_ts = MagicMock()
    mock_ts.return_value.exists.return_value = False
    mock_ts.return_value.kill = lambda **kw: killed.append("vctl-vllm-qwen3-9b")
    monkeypatch.setattr(vm_mod, "TmuxSession", mock_ts)
```

- In `test_start_wait_for_ready_failure_cleans_up`: same pattern.

Also update `test_start_writes_all_four_state_files` to remove the old `tmux_run_detached_argv` and `tmux_kill` patches and add the `TmuxSession` mock.

Full updated `test_start_refuses_when_session_exists`:

```python
def test_start_refuses_when_session_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """start() raises RuntimeError (exit 4) when tmux session already exists."""
    import vctl.vllm_manager as vm_mod
    from vctl.vllm_manager import VllmManager

    mock_ts = MagicMock()
    mock_ts.return_value.exists.return_value = True
    monkeypatch.setattr(vm_mod, "TmuxSession", mock_ts)
    rc = _make_rc()
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
    with pytest.raises(RuntimeError, match="already running"):
        vm.start()
```

Full updated `test_start_writes_all_four_state_files`:

```python
def test_start_writes_all_four_state_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """start() writes pid, log (via pipe-pane), cmd.json, and host state files."""
    import vctl.vllm_manager as vm_mod
    from vctl.vllm_manager import VllmManager

    mock_ts = MagicMock()
    mock_ts.return_value.exists.return_value = False
    monkeypatch.setattr(vm_mod, "TmuxSession", mock_ts)

    fake_pid = 99999

    def fake_process_iter(attrs: list[str]) -> list[MagicMock]:
        proc = MagicMock()
        proc.info = {
            "cmdline": ["python", "vllm", "serve", "--port=8000"],
            "create_time": 1000.0,
            "pid": fake_pid,
        }
        proc.pid = fake_pid
        return [proc]

    monkeypatch.setattr(vm_mod.psutil, "process_iter", fake_process_iter)
    monkeypatch.setattr(vm_mod.subprocess, "run", lambda *a, **kw: MagicMock(returncode=0))
    monkeypatch.setattr(vm_mod, "_wait_for_ready", lambda port, timeout: None)
    monkeypatch.setattr(vm_mod, "_do_add", lambda ep, mgr, bs, pool_name=None: 0)

    rc = _make_rc()
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
    vm.start()

    assert vm.pid_path.exists(), "pid file must be written"
    assert int(vm.pid_path.read_text().strip()) == fake_pid
    assert vm.cmd_path.exists(), "cmd.json must be written"
    assert vm.host_path.exists(), "host file must be written"
    assert vm.host_path.read_text().strip() == socket.gethostname()
```

Full updated `test_start_pid_discovery_timeout_kills_session`:

```python
def test_start_pid_discovery_timeout_kills_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """start() kills tmux session and raises RuntimeError when PID discovery times out."""
    import vctl.vllm_manager as vm_mod
    from vctl.vllm_manager import VllmManager

    killed: list[str] = []
    mock_ts = MagicMock()
    mock_ts.return_value.exists.return_value = False
    mock_ts.return_value.kill = MagicMock(side_effect=lambda **kw: killed.append("vctl-vllm-qwen3-9b"))
    monkeypatch.setattr(vm_mod, "TmuxSession", mock_ts)
    monkeypatch.setattr(vm_mod.subprocess, "run", lambda *a, **kw: MagicMock(returncode=0))
    monkeypatch.setattr(vm_mod.psutil, "process_iter", lambda attrs: [])

    monkeypatch.setattr(vm_mod, "_VLLM_PID_POLL_TIMEOUT", 0.1)
    monkeypatch.setattr(vm_mod, "_VLLM_PID_POLL_INTERVAL", 0.05)

    rc = _make_rc()
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
    with pytest.raises(RuntimeError, match="timed out"):
        vm.start()

    assert mock_ts.return_value.kill.called, "kill() must be called on timeout"
```

Full updated `test_start_wait_for_ready_failure_cleans_up`:

```python
def test_start_wait_for_ready_failure_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """start() kills tmux session and unlinks state files when _wait_for_ready raises."""
    import vctl.vllm_manager as vm_mod
    from vctl.vllm_manager import VllmManager

    mock_ts = MagicMock()
    mock_ts.return_value.exists.return_value = False
    monkeypatch.setattr(vm_mod, "TmuxSession", mock_ts)

    fake_pid = 99999

    def fake_process_iter(attrs: list[str]) -> list[MagicMock]:
        proc = MagicMock()
        proc.info = {
            "cmdline": ["python", "vllm", "serve", "--port=8000"],
            "create_time": 1000.0,
            "pid": fake_pid,
        }
        proc.pid = fake_pid
        return [proc]

    monkeypatch.setattr(vm_mod.psutil, "process_iter", fake_process_iter)
    monkeypatch.setattr(vm_mod.subprocess, "run", lambda *a, **kw: MagicMock(returncode=0))

    def _fail_ready(port: int, timeout: float) -> None:
        raise TimeoutError("stubbed timeout")

    monkeypatch.setattr(vm_mod, "_wait_for_ready", _fail_ready)

    rc = _make_rc()
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
    with pytest.raises(RuntimeError, match="did not become ready"):
        vm.start()

    assert not vm.pid_path.exists(), "pid file must be cleaned up"
    assert not vm.cmd_path.exists(), "cmd.json must be cleaned up"
    assert not vm.host_path.exists(), "host file must be cleaned up"
    assert mock_ts.return_value.kill.called
```

Update `test_status_all_alive` and `test_status_tmux_dead_pid_alive` to use `TmuxSession` mock:

```python
def test_status_all_alive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """status() returns all True fields when session, pid, http, and LB are alive."""
    import vctl.vllm_manager as vm_mod
    from vctl.vllm_manager import VllmManager

    mock_ts = MagicMock()
    mock_ts.return_value.exists.return_value = True
    monkeypatch.setattr(vm_mod, "TmuxSession", mock_ts)

    fake_pid = os.getpid()
    rc = _make_rc()
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")

    vm.pid_path.parent.mkdir(parents=True, exist_ok=True)
    vm.pid_path.write_text(str(fake_pid))

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": [{"id": "m"}]}
    monkeypatch.setattr(vm_mod.httpx, "get", lambda url, timeout=None: mock_resp)

    self_ip = vm_mod.detect_self_ip()
    ep = f"{self_ip}:{rc.server.http_port}"
    monkeypatch.setattr(vm_mod.BackendState, "list", lambda self: [ep])

    result = vm.status()
    assert result["tmux_alive"] is True
    assert result["pid_alive"] is True
    assert result["vllm_ready"] is True
    assert result["lb_attached"] is True


def test_status_tmux_dead_pid_alive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """status() reports tmux_alive=False but pid_alive=True for an orphan process."""
    import vctl.vllm_manager as vm_mod
    from vctl.vllm_manager import VllmManager

    mock_ts = MagicMock()
    mock_ts.return_value.exists.return_value = False
    monkeypatch.setattr(vm_mod, "TmuxSession", mock_ts)

    fake_pid = os.getpid()
    rc = _make_rc()
    vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
    vm.pid_path.write_text(str(fake_pid))

    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": []}
    monkeypatch.setattr(vm_mod.httpx, "get", lambda url, timeout=None: mock_resp)
    monkeypatch.setattr(vm_mod.BackendState, "list", lambda self: [])

    result = vm.status()
    assert result["tmux_alive"] is False
    assert result["pid_alive"] is True
```

Also update `test_status_pidfile_missing` and `test_status_cross_host_pidfile_skips_liveness_check` similarly (replace `monkeypatch.setattr(vm_mod, "tmux_session_exists", ...)` with the `TmuxSession` mock pattern).

- [ ] **Step 9: Run VllmManager tests**

```bash
.venv/bin/pytest tests/test_vllm_manager.py -v --tb=short
```

Expected: all tests PASS

- [ ] **Step 10: Run full suite**

```bash
.venv/bin/pytest -q --tb=short 2>&1 | tail -15
```

Expected: same pass count as before this task

- [ ] **Step 11: Run ruff + mypy**

```bash
.venv/bin/ruff check src/vctl/vllm_manager.py
.venv/bin/ruff format --check src/vctl/vllm_manager.py
.venv/bin/mypy --strict src/vctl/vllm_manager.py
```

Expected: no errors

- [ ] **Step 12: Commit**

```bash
git add src/vctl/vllm_manager.py tests/test_vllm_manager.py
git commit -m "refactor(vllm): migrate VllmManager to TmuxSession (AT-1, AT-4)"
```

---

### Checkpoint: After Tasks 6-7

- [ ] LbManager tests pass: `.venv/bin/pytest tests/test_lb_manager_b.py tests/test_f7_f8_f10.py -v`
- [ ] VllmManager tests pass: `.venv/bin/pytest tests/test_vllm_manager.py -v`
- [ ] Full suite passes: `.venv/bin/pytest -q --tb=short --cov=vctl --cov-fail-under=50`
- [ ] `mypy --strict src/vctl/` passes on both migrated files
- [ ] Review with human before final cleanup task

---

### Task 8: Migrate `lmmseval` + delete old helpers + version bump + CHANGELOG

**What we build:** `commands/lmmseval.py` replaces `_build_env_exports()` (the prefix-whitelist `export K=V;` pattern) and `tmux_run_detached` with `TmuxSession(name, env={**os.environ, **_FORCED_ENV}).start(shell_body)`. `_cmd_stop` replaces `tmux_kill` with `TmuxSession(name).kill(tree=True)`. `_cmd_status` replaces `tmux_session_exists` with `TmuxSession(name).exists()`. The `_build_env_exports` function and `_ENV_PROPAGATE_PREFIXES` constant are deleted. Then the four deprecated helpers (`tmux_run_detached`, `tmux_run_detached_argv`, `tmux_kill`, `tmux_session_exists`) and `_validate_tmux_name` are removed from `platform.py`, along with the `TmuxSession` re-export added in Task 1. Tests for the deleted helpers are removed from `test_platform.py`. Finally, version is bumped to `0.8.0` and CHANGELOG updated.

**Files:**
- Modify: `src/vctl/commands/lmmseval.py`
- Modify: `src/vctl/platform.py` (delete all tmux helpers + TmuxSession re-export)
- Modify: `tests/test_platform.py` (delete tests for removed helpers)
- Modify: `pyproject.toml` (version bump)
- Modify: `src/vctl/__init__.py` (version bump)
- Modify: `tests/test_smoke.py` (version assert)
- Modify: `docs/CHANGELOG.md`

- [ ] **Step 1: Update `src/vctl/commands/lmmseval.py`**

Replace the import line at the top:

```python
from vctl.tmux import TmuxSession
```

Remove `_ENV_PROPAGATE_PREFIXES` (the entire constant block, lines 22-35), the `_build_env_exports` function (lines 47-57), and the `shlex` import (no longer needed).

Replace `_build_run_loop_cmd`:

```python
def _build_run_loop_cmd() -> str:
    """Return the shell command line to run in the tmux session.

    All env vars are propagated via TmuxSession's -e flags; this function
    only builds the shell pipeline body (source activate + bash run_loop.sh).
    """
    return (
        f"source {_VENV}/bin/activate && "
        f"bash {_RUN_LOOP_SH} {_TASK_SH} {_START_IDX} {_END_IDX}"
    )
```

Replace `_cmd_run_loop`:

```python
def _cmd_run_loop(_ns: argparse.Namespace) -> int:
    if TmuxSession(_TMUX_NAME).exists():
        print(
            f"tmux session {_TMUX_NAME!r} already exists. "
            f"attach: tmux attach -t {_TMUX_NAME}  |  kill: vctl lmmseval stop",
            file=sys.stderr,
        )
        return 4
    # Full os.environ snapshot + forced offline vars ensures HF_HOME,
    # TRANSFORMERS_OFFLINE, CUDA_*, etc. are all available in the pane
    # regardless of how old the tmux server's environment cache is (AT-3).
    session_env = {**os.environ, **_FORCED_ENV}
    cmd = _build_run_loop_cmd()
    TmuxSession(_TMUX_NAME, env=session_env).start(cmd)
    print(f"started in tmux session {_TMUX_NAME!r}", file=sys.stderr)
    print(f"  attach: tmux attach -t {_TMUX_NAME}", file=sys.stderr)
    print(f"  cmd:    {cmd}", file=sys.stderr)
    return 0
```

Replace `_cmd_stop`:

```python
def _cmd_stop(_ns: argparse.Namespace) -> int:
    if not TmuxSession(_TMUX_NAME).exists():
        print(f"tmux session {_TMUX_NAME!r} not running", file=sys.stderr)
        return 0
    # tree=True: kill run_loop.sh + accelerate + 8 worker processes.
    TmuxSession(_TMUX_NAME).kill(tree=True)
    print(f"killed tmux session {_TMUX_NAME!r}", file=sys.stderr)
    return 0
```

Replace `_cmd_status`:

```python
def _cmd_status(_ns: argparse.Namespace) -> int:
    if TmuxSession(_TMUX_NAME).exists():
        print(f"tmux session {_TMUX_NAME!r}: running", file=sys.stderr)
        print(f"  attach: tmux attach -t {_TMUX_NAME}", file=sys.stderr)
    else:
        print(f"tmux session {_TMUX_NAME!r}: not running", file=sys.stderr)
    return 0
```

Final `src/vctl/commands/lmmseval.py` should look like:

```python
"""Hidden helper commands for the lmms-eval workspace."""

from __future__ import annotations

import argparse
import os
import sys

from vctl.tmux import TmuxSession

_TMUX_NAME = "vctl-lmmseval"

_LMMS_ROOT = "/mnt/umm/users/qianjianheng/workspace/lmms-eval"
_VENV = f"{_LMMS_ROOT}/.venv_novllm"
_RUN_LOOP_SH = f"{_LMMS_ROOT}/scripts/run_loop.sh"
_TASK_SH = f"{_LMMS_ROOT}/scripts/osibench_32frame/qwen3vl8binstruct.sh"
_START_IDX = 0
_END_IDX = 5

# Pods have no internet egress to huggingface.co. Force offline mode so
# transformers / huggingface_hub never HEAD the network.
# Note: full os.environ snapshot via TmuxSession means HF_HOME, CUDA_* etc.
# from the operator's shell are propagated automatically — no more prefix
# whitelist needed.
_FORCED_ENV: dict[str, str] = {
    "TRANSFORMERS_OFFLINE": "1",
    "HF_HUB_OFFLINE": "1",
    "HF_DATASETS_OFFLINE": "1",
}


def _build_run_loop_cmd() -> str:
    """Return the shell command line to run in the tmux session."""
    return (
        f"source {_VENV}/bin/activate && "
        f"bash {_RUN_LOOP_SH} {_TASK_SH} {_START_IDX} {_END_IDX}"
    )


def _cmd_run_loop(_ns: argparse.Namespace) -> int:
    if TmuxSession(_TMUX_NAME).exists():
        print(
            f"tmux session {_TMUX_NAME!r} already exists. "
            f"attach: tmux attach -t {_TMUX_NAME}  |  kill: vctl lmmseval stop",
            file=sys.stderr,
        )
        return 4
    session_env = {**os.environ, **_FORCED_ENV}
    cmd = _build_run_loop_cmd()
    TmuxSession(_TMUX_NAME, env=session_env).start(cmd)
    print(f"started in tmux session {_TMUX_NAME!r}", file=sys.stderr)
    print(f"  attach: tmux attach -t {_TMUX_NAME}", file=sys.stderr)
    print(f"  cmd:    {cmd}", file=sys.stderr)
    return 0


def _cmd_stop(_ns: argparse.Namespace) -> int:
    if not TmuxSession(_TMUX_NAME).exists():
        print(f"tmux session {_TMUX_NAME!r} not running", file=sys.stderr)
        return 0
    TmuxSession(_TMUX_NAME).kill(tree=True)
    print(f"killed tmux session {_TMUX_NAME!r}", file=sys.stderr)
    return 0


def _cmd_status(_ns: argparse.Namespace) -> int:
    if TmuxSession(_TMUX_NAME).exists():
        print(f"tmux session {_TMUX_NAME!r}: running", file=sys.stderr)
        print(f"  attach: tmux attach -t {_TMUX_NAME}", file=sys.stderr)
    else:
        print(f"tmux session {_TMUX_NAME!r}: not running", file=sys.stderr)
    return 0


def run(_ns: argparse.Namespace, argv_rest: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="vctl lmmseval",
        description="Hidden helper commands for the lmms-eval workspace.",
    )
    sub = p.add_subparsers(dest="verb", required=True, metavar="VERB")

    sp_run = sub.add_parser(
        "run-loop",
        help=f"start {_RUN_LOOP_SH} in detached tmux session (with venv activated)",
    )
    sp_run.set_defaults(_handler=_cmd_run_loop)

    sp_stop = sub.add_parser("stop", help=f"kill the {_TMUX_NAME!r} tmux session")
    sp_stop.set_defaults(_handler=_cmd_stop)

    sp_status = sub.add_parser("status", help=f"show whether {_TMUX_NAME!r} is running")
    sp_status.set_defaults(_handler=_cmd_status)

    parsed = p.parse_args(argv_rest)
    handler = parsed._handler
    return int(handler(parsed))
```

- [ ] **Step 2: Run lmmseval smoke test and CLI help**

There is no `tests/test_commands_lmmseval.py` — the module has no unit tests.  Verify
the module at least imports and the `run-loop` subcommand is wired correctly:

```bash
.venv/bin/python -c "from vctl.commands.lmmseval import run; print('import ok')"
.venv/bin/python -m vctl lmmseval --help
```

Expected: import succeeds, help text shows `run-loop`, `stop`, `status` subcommands

- [ ] **Step 3: Delete tmux helpers from `src/vctl/platform.py`**

Remove from `src/vctl/platform.py`:
1. The `import re` and `import shlex` lines (no longer needed)
2. The `_TMUX_NAME_RE = re.compile(...)` line
3. The `_validate_tmux_name` function (lines 18-21)
4. The `tmux_session_exists` function (lines 61-76)
5. The `tmux_run_detached` function (lines 79-96)
6. The `tmux_run_detached_argv` function (lines 99-110)
7. The `tmux_kill` function (lines 113-123)
8. The `TmuxSession` re-export line added at the end in Task 1

The final `src/vctl/platform.py` should contain only `detect_self_ip` and `which`:

```python
"""Host primitives — IP detection, `which`."""

from __future__ import annotations

import logging
import shutil
import socket

_LOG = logging.getLogger(__name__)


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
```

- [ ] **Step 4: Update `tests/test_platform.py`**

Remove the tests for the deleted helpers. The final file keeps only `detect_self_ip` and `which` tests:

```python
"""Platform helpers — IP detection, which."""

from __future__ import annotations

from unittest.mock import patch

from vctl.platform import detect_self_ip, which


def test_detect_self_ip_returns_string() -> None:
    ip = detect_self_ip()
    assert isinstance(ip, str)
    assert ip.count(".") == 3 or ":" in ip


@patch("shutil.which", return_value="/usr/bin/haproxy")
def test_which_returns_path(mock_which) -> None:
    assert which("haproxy") == "/usr/bin/haproxy"


@patch("shutil.which", return_value=None)
def test_which_raises_when_missing(mock_which) -> None:
    import pytest

    with pytest.raises(FileNotFoundError):
        which("definitely-not-on-path-zzzz")
```

- [ ] **Step 5: Run full suite to confirm no regressions**

```bash
.venv/bin/pytest -q --tb=short 2>&1 | tail -20
```

Expected: all tests that were passing before still pass; no import errors from deleted helpers

- [ ] **Step 6: Run coverage gate**

```bash
.venv/bin/pytest -q --cov=vctl --cov-report=term-missing --cov-fail-under=50
```

Expected: coverage >= 50%, gate passes

- [ ] **Step 7: Bump version to 0.8.0 in `pyproject.toml`**

Find and replace the current version line (`version = "0.7.4"`) with:

```toml
version = "0.8.0"
```

- [ ] **Step 8: Bump `__version__` in `src/vctl/__init__.py`**

```python
__version__ = "0.8.0"
```

- [ ] **Step 9: Update `tests/test_smoke.py` version assert**

Find `assert pkg_version == __version__ == "0.7.4"` in `test_pyproject_version_matches_module_version` and change to:

```python
    assert pkg_version == __version__ == "0.8.0"
```

- [ ] **Step 10: Add CHANGELOG entry**

Prepend to `docs/CHANGELOG.md` (after the `# Changelog` header, before the previous release):

```markdown
## v0.8.0 (2026-05-06)

### Breaking Changes

- **Internal API removed:** `tmux_run_detached`, `tmux_run_detached_argv`, `tmux_kill`,
  `tmux_session_exists`, and `_validate_tmux_name` have been deleted from
  `vctl.platform`. Any code that imported these directly must switch to
  `from vctl.tmux import TmuxSession` (or `tmux_session_exists` /
  `_validate_tmux_name` from `vctl.tmux`). The public CLI surface is unchanged.

### Improvements

- **Full env propagation in all tmux sessions:** `LbManager`, `VllmManager`, and
  `lmmseval` now pass the calling process's complete environment via
  `tmux new-session -e KEY=VAL` (tmux 3.2+). This eliminates the class of bugs
  where variables set in the operator's shell (PATH, HF_HOME, CUDA_VISIBLE_DEVICES,
  etc.) were silently missing inside sessions because the tmux server cached its
  environment at login time. Fixes the v0.5.4 ninja PATH bug, the v0.7.3
  HF_HOME/TRANSFORMERS_OFFLINE regression, and the class of issues in AT-1–AT-3.
- **`lmmseval` env propagation simplified:** Removed the `_ENV_PROPAGATE_PREFIXES`
  whitelist approach. All variables in the operator's shell are now available in
  the lmmseval session. `_FORCED_ENV` overrides (TRANSFORMERS_OFFLINE, HF_HUB_OFFLINE,
  HF_DATASETS_OFFLINE) still take precedence.
- **`lmmseval stop` now tree-kills workers:** `TmuxSession.kill(tree=True)` sends
  SIGTERM to the run_loop.sh shell + all accelerate + worker processes. Previously
  `tmux kill-session` only killed the pane shell, leaving workers as orphans.
- **`VllmManager stop` tree-kills escaped workers:** Replaces the pidfile-poll loop
  with `TmuxSession.kill(tree=True, grace_s=grace)`. Catches accelerate worker
  processes that survive the pane kill. C-c (clean vllm SIGINT shutdown) is still
  sent first.
- **`TmuxSession` requires tmux 3.2+:** Version is checked on first `start()` call
  per process with a clear error message. Deployed environment is tmux 3.4.
- **haproxy env note:** `LbManager.start()` now forwards the caller's full
  `os.environ` to haproxy's tmux session. HAProxy ignores Python-related env vars;
  practical impact is near-zero, but documented in case of exotic deployments.
```

- [ ] **Step 11: Run final gate**

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy --strict src/vctl
.venv/bin/pytest -q --cov=vctl --cov-report=term-missing --cov-fail-under=50
```

Expected: all four checks pass

- [ ] **Step 12: Commit**

```bash
git add src/vctl/commands/lmmseval.py \
        src/vctl/platform.py \
        tests/test_platform.py \
        pyproject.toml \
        src/vctl/__init__.py \
        tests/test_smoke.py \
        docs/CHANGELOG.md
git commit -m "refactor(tmux): migrate lmmseval + delete old helpers + bump v0.8.0 (AT-3, AT-5)"
```

---

### Checkpoint: After Task 8 (Final)

- [ ] All 10 ATs have unit test coverage in `tests/test_tmux.py`
- [ ] `platform.py` contains only `detect_self_ip` and `which` — no tmux helpers
- [ ] `vctl.tmux` is the single source of truth for all tmux session management
- [ ] Full suite passes: `.venv/bin/pytest -q --cov=vctl --cov-fail-under=50`
- [ ] All four CI gates pass: `ruff check`, `ruff format --check`, `mypy --strict`, `pytest --cov-fail-under=50`
- [ ] Version is `0.8.0` in `pyproject.toml`, `__init__.py`, and `test_smoke.py`
- [ ] CHANGELOG entry for `v0.8.0` is present
- [ ] Integration tests (optional, require real tmux): `.venv/bin/pytest -m integration -v`
