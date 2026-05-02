# Reconciler Phase 2 — migrate scaling verbs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use super-agent-skills:subagent-driven-development (recommended) or super-agent-skills:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Migrate the six scaling verbs in src/vctl/commands/lb_scaling.py (_do_add, _do_remove, _do_drain, _do_auto_add, _do_remove_cli, _do_detach) to delegate state mutations to the Reconciler module shipped in Phase 1, closing F11 (state-first ordering bug) and F12 (silent suppress in auto-add) by routing all mutations through Reconciler's haproxy-first invariant.

**Architecture:** Wrapper-only approach (Approach A): each _do_X function preserves its argparse, pool-resolution, and stderr formatting; the function body shrinks to a try/except calling Reconciler. Function signatures and lb_scaling.dispatch routing are unchanged. A new _exit_for(exc: ReconcilerError) -> int helper centralizes the exception-to-exit-code mapping (LbUnreachable → 4, PoolNotFound → 3, BackendOpFailed → 1). Phase 1 modules (reconciler.py, errors.py) are sealed — Phase 2 only touches lb_scaling.py + tests.

**Tech Stack:** Python 3.10+ with strict mypy; pydantic v2 (existing dep); pytest + pytest-cov + pytest-asyncio (existing); ruff for lint/format; httpx + psutil + rich (all existing project deps; no new dependencies introduced).

---

### Task 1: Add `_exit_for` helper to lb_scaling.py

**Files:**
- Modify: `src/vctl/commands/lb_scaling.py` — add `_exit_for`, `BackendOpFailed`, `LbUnreachable`, `PoolNotFound`, `ReconcilerError` imports, and `Action`, `Reconciler` imports
- Modify: `tests/test_commands_lb_scaling.py` — add 4 unit tests for `_exit_for`

- [ ] **Step 1: Write the failing test**

Add the following tests to `tests/test_commands_lb_scaling.py` after the existing imports:

```python
# ---------------------------------------------------------------------------
# Task 1: _exit_for helper — exit-code mapping
# ---------------------------------------------------------------------------

from vctl.lb.errors import BackendOpFailed, LbUnreachable, PoolNotFound, ReconcilerError


def test_exit_for_lb_unreachable_returns_4(tmp_path: Path) -> None:
    """_exit_for must return 4 for LbUnreachable."""
    exc = LbUnreachable(sock="/run/haproxy.sock", tcp="10.0.0.1:9001")
    assert lb_scaling._exit_for(exc) == 4


def test_exit_for_pool_not_found_returns_3(tmp_path: Path) -> None:
    """_exit_for must return 3 for PoolNotFound."""
    exc = PoolNotFound(requested="nonexistent", available=["default"])
    assert lb_scaling._exit_for(exc) == 3


def test_exit_for_backend_op_failed_returns_1(tmp_path: Path) -> None:
    """_exit_for must return 1 for BackendOpFailed."""
    exc = BackendOpFailed(op="add_server", ep="10.0.0.5:8000", backend="pool_default")
    assert lb_scaling._exit_for(exc) == 1


def test_exit_for_arbitrary_reconciler_error_subclass_returns_1(tmp_path: Path) -> None:
    """_exit_for must return 1 for any unknown ReconcilerError subclass."""

    class _UnknownError(ReconcilerError):
        pass

    exc = _UnknownError("unknown haproxy failure")
    assert lb_scaling._exit_for(exc) == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /mnt/umm/users/qianjianheng/workspace/vctl && python -m pytest tests/test_commands_lb_scaling.py::test_exit_for_lb_unreachable_returns_4 tests/test_commands_lb_scaling.py::test_exit_for_pool_not_found_returns_3 tests/test_commands_lb_scaling.py::test_exit_for_backend_op_failed_returns_1 tests/test_commands_lb_scaling.py::test_exit_for_arbitrary_reconciler_error_subclass_returns_1 -v 2>&1 | tail -20
```

Expected: FAIL with `AttributeError: module 'vctl.commands.lb_scaling' has no attribute '_exit_for'`

- [ ] **Step 3: Implement**

In `src/vctl/commands/lb_scaling.py`, add the following imports after the existing import block (after line 17, before `_LOG = logging.getLogger(__name__)`):

```python
from vctl.lb.errors import BackendOpFailed, LbUnreachable, PoolNotFound, ReconcilerError
from vctl.lb.reconciler import Action, Reconciler
```

Then add the `_exit_for` function after the `__all__` declaration (after line 29):

```python
def _exit_for(exc: ReconcilerError) -> int:
    """Map a ReconcilerError subclass to a CLI exit code.

    LbUnreachable  → 4  (environment error: LB socket down)
    PoolNotFound   → 3  (user error: unknown pool name)
    BackendOpFailed and any future ReconcilerError subclass → 1  (generic failure)
    """
    if isinstance(exc, LbUnreachable):
        return 4
    if isinstance(exc, PoolNotFound):
        return 3
    return 1  # BackendOpFailed and any future ReconcilerError subclass
```

The full updated top of `src/vctl/commands/lb_scaling.py` through `_exit_for` should look like:

```python
"""Scaling verbs: add / remove / drain / attach / detach / auto-add / health."""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
import time

from vctl.lb.errors import BackendOpFailed, LbUnreachable, PoolNotFound, ReconcilerError
from vctl.lb.manager import LbManager
from vctl.lb.probe import probe_local_vllm, probe_vllm
from vctl.lb.reconciler import Action, Reconciler
from vctl.lb.routing import _name_for, pool_for_endpoint
from vctl.lb.runtime import RuntimeClient, _NoOpClient
from vctl.lb.runtime import lb_admin_client as _client
from vctl.lb.state import BackendState
from vctl.platform import detect_self_ip

_LOG = logging.getLogger(__name__)

# Explicit re-export declaration for mypy --strict (and a marker for readers).
# The canonical implementations live in vctl.lb.runtime / vctl.lb.routing;
# they are imported above and surfaced here so:
#   1. mypy treats `from vctl.commands.lb_scaling import _client` (used in
#      vctl.commands.lb) as an explicit re-export, not an attr-defined error.
#   2. `monkeypatch.setattr(lb_scaling, "_client", ...)` in existing tests
#      keeps working unchanged after the Phase 1 extraction.
__all__ = ["_NoOpClient", "_client", "RuntimeClient", "_name_for"]


def _exit_for(exc: ReconcilerError) -> int:
    """Map a ReconcilerError subclass to a CLI exit code.

    LbUnreachable  → 4  (environment error: LB socket down)
    PoolNotFound   → 3  (user error: unknown pool name)
    BackendOpFailed and any future ReconcilerError subclass → 1  (generic failure)
    """
    if isinstance(exc, LbUnreachable):
        return 4
    if isinstance(exc, PoolNotFound):
        return 3
    return 1  # BackendOpFailed and any future ReconcilerError subclass
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /mnt/umm/users/qianjianheng/workspace/vctl && python -m pytest tests/test_commands_lb_scaling.py::test_exit_for_lb_unreachable_returns_4 tests/test_commands_lb_scaling.py::test_exit_for_pool_not_found_returns_3 tests/test_commands_lb_scaling.py::test_exit_for_backend_op_failed_returns_1 tests/test_commands_lb_scaling.py::test_exit_for_arbitrary_reconciler_error_subclass_returns_1 -v 2>&1 | tail -10
```

Expected: PASS — 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/vctl/commands/lb_scaling.py tests/test_commands_lb_scaling.py
git commit -m "$(cat <<'EOF'
feat: add _exit_for helper + Reconciler/errors imports to lb_scaling

Centralises exit-code policy (LbUnreachable→4, PoolNotFound→3,
BackendOpFailed→1) in one place so subsequent verb migrations each
reduce to a mechanical try/except + return _exit_for(exc).

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Migrate `_do_drain` to Reconciler.want_draining

**Files:**
- Modify: `src/vctl/commands/lb_scaling.py` — replace `_do_drain` body
- Modify: `tests/test_commands_lb_scaling.py` — add happy-path + PoolNotFound tests; verify A4 still passes

Acceptance test mapped here:
- **AT-5:** `test: lb drain against running LB exits 0 and surfaces DRAINED action`
- **AT-6:** `test: lb drain against stopped LB exits 4` (existing A4 test verified)

- [ ] **Step 1: Write the failing test**

Add the following tests to `tests/test_commands_lb_scaling.py`:

```python
# ---------------------------------------------------------------------------
# Task 2: _do_drain — Reconciler migration
# ---------------------------------------------------------------------------

from vctl.lb import reconciler as reconciler_mod


def test_do_drain_happy_path_returns_0_and_surfaces_drained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AT-5: _do_drain must return 0 and print DRAINED to stderr on success."""
    mgr = _make_mgr(tmp_path)

    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = []
    mock_client.set_state.return_value = None
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    rc = lb_scaling._do_drain("10.0.0.5:8000", mgr, pool_name="default")
    assert rc == 0
    captured = capsys.readouterr()
    assert "DRAINED" in captured.err
    mock_client.set_state.assert_called_once_with("pool_default", "b_10_0_0_5_8000", "drain")


def test_do_drain_pool_not_found_returns_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """_do_drain returns 3 when pool name is unknown."""
    mgr = _make_mgr(tmp_path)

    mock_client = MagicMock()
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    rc = lb_scaling._do_drain("10.0.0.5:8000", mgr, pool_name="nonexistent")
    assert rc == 3
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /mnt/umm/users/qianjianheng/workspace/vctl && python -m pytest tests/test_commands_lb_scaling.py::test_do_drain_happy_path_returns_0_and_surfaces_drained tests/test_commands_lb_scaling.py::test_do_drain_pool_not_found_returns_3 -v 2>&1 | tail -20
```

Expected: FAIL — `test_do_drain_happy_path_returns_0_and_surfaces_drained` fails because the current `_do_drain` prints no `DRAINED` keyword; `test_do_drain_pool_not_found_returns_3` fails because current code calls `_resolve_pool_name` which calls `sys.exit(3)` rather than catching `PoolNotFound`.

- [ ] **Step 3: Implement**

Replace the `_do_drain` function body in `src/vctl/commands/lb_scaling.py`:

```python
def _do_drain(ep: str, mgr: LbManager, pool_name: str | None = None) -> int:
    pool_name = _resolve_pool_name(mgr, pool_name)
    try:
        outcome = Reconciler(mgr).want_draining(ep, pool_name)
        print(f"drain {ep} {outcome.action.name} (pool: {pool_name})", file=sys.stderr)
        return 0
    except ReconcilerError as exc:
        print(f"drain {ep} failed: {exc}", file=sys.stderr)
        return _exit_for(exc)
```

Note: `_resolve_pool_name` continues to run before the Reconciler call. Pool validation happens there via `sys.exit(3)` for the user-facing `--pool` flag path. The Reconciler's own `PoolNotFound` guard is also in place as a defence-in-depth path and surfaces as exit 3 via `_exit_for`. The existing A4 test (`test_do_drain_exits_4_when_lb_unreachable`) patches `lb_scaling._client` — after this migration `_do_drain` no longer calls `_client` directly. That test must be updated to patch at the Reconciler level.

Update the existing A4 tests to patch `reconciler_mod.lb_admin_client` instead of `lb_scaling._client`:

```python
def test_do_drain_exits_4_when_lb_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A4: _do_drain must return 4 (not 0) when LB admin socket is unreachable."""
    mgr = _make_mgr(tmp_path)
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: None)
    rc = lb_scaling._do_drain("10.0.0.5:8000", mgr, pool_name="default")
    assert rc == 4


def test_do_drain_succeeds_when_cli_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A4 inverse: _do_drain returns 0 when client is present."""
    mgr = _make_mgr(tmp_path)
    cli = MagicMock()
    cli.show_servers_state.return_value = []
    cli.set_state.return_value = None
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: cli)
    rc = lb_scaling._do_drain("10.0.0.5:8000", mgr, pool_name="default")
    assert rc == 0
    cli.set_state.assert_called_once_with("pool_default", "b_10_0_0_5_8000", "drain")
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /mnt/umm/users/qianjianheng/workspace/vctl && python -m pytest tests/test_commands_lb_scaling.py::test_do_drain_exits_4_when_lb_unreachable tests/test_commands_lb_scaling.py::test_do_drain_succeeds_when_cli_available tests/test_commands_lb_scaling.py::test_do_drain_happy_path_returns_0_and_surfaces_drained tests/test_commands_lb_scaling.py::test_do_drain_pool_not_found_returns_3 -v 2>&1 | tail -10
```

Expected: PASS — 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/vctl/commands/lb_scaling.py tests/test_commands_lb_scaling.py
git commit -m "$(cat <<'EOF'
feat: migrate _do_drain to Reconciler.want_draining (Phase 2 T2)

Replaces direct _client + set_state call with Reconciler delegation.
Surfaces DRAINED in stderr. A4 tests updated to patch reconciler_mod.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Checkpoint 1 — helper + drain working

Run the full test suite to confirm no regressions before proceeding:

```bash
cd /mnt/umm/users/qianjianheng/workspace/vctl && python -m pytest tests/test_commands_lb_scaling.py -v 2>&1 | tail -20
```

Expected: all existing tests pass (may need fixture updates for patching level — see Task 2 Step 3 notes). Zero new failures.

Also verify mypy is clean over lb_scaling so far:

```bash
cd /mnt/umm/users/qianjianheng/workspace/vctl && python -m mypy --strict src/vctl/commands/lb_scaling.py 2>&1 | tail -10
```

Expected: `Success: no issues found`

---

### Task 3: Migrate `_do_add` to Reconciler.want_present

**Files:**
- Modify: `src/vctl/commands/lb_scaling.py` — replace `_do_add` body; `bs` stays in signature but is unused inside the body
- Modify: `tests/test_commands_lb_scaling.py` — flip exit-0/exit-4 expectations; update stderr assertions from `(new)` / `(already present)` to `Action.ADDED` / `Action.READIED` / `Action.ADOPTED`; add action-mapping and haproxy-refuses-no-state-write tests

Acceptance tests mapped here:
- **AT-1:** `test: lb add against running LB exits 0 and prints ADDED action in stderr`
- **AT-2:** `test: lb add against stopped LB exits 4 and leaves state file unchanged`
- **AT-8:** `test: lb add with unknown pool exits 3 and lists available pools` (existing test verified passes unchanged)

- [ ] **Step 1: Write the failing test**

Add the following tests to `tests/test_commands_lb_scaling.py`:

```python
# ---------------------------------------------------------------------------
# Task 3: _do_add — Reconciler migration
# ---------------------------------------------------------------------------


def test_do_add_happy_path_returns_0_and_surfaces_added(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AT-1: _do_add returns 0, prints ADDED to stderr, state file contains ep."""
    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")

    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = []
    mock_client.add_server.return_value = None
    mock_client.set_state.return_value = None
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    rc = lb_scaling._do_add("10.0.0.5:8000", mgr, bs, pool_name="default")
    assert rc == 0
    captured = capsys.readouterr()
    assert "ADDED" in captured.err
    assert "10.0.0.5:8000" in BackendState(state_dir, "10.0.0.1", pool="default").list()
    mock_client.add_server.assert_called_once()
    mock_client.set_state.assert_called()


def test_do_add_exits_4_when_lb_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AT-2: _do_add must return 4 and NOT write state file when LB is unreachable."""
    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")

    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: None)

    rc = lb_scaling._do_add("10.0.0.5:8000", mgr, bs, pool_name="default")
    assert rc == 4
    # F11 closed: state file must NOT be written when haproxy refuses
    assert "10.0.0.5:8000" not in BackendState(state_dir, "10.0.0.1", pool="default").list()
    captured = capsys.readouterr()
    assert "failed" in captured.err.lower() or "unreachable" in captured.err.lower()


def test_do_add_action_readied_on_idempotent_reregister(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """_do_add returns 0 and surfaces READIED when ep already in state + haproxy."""
    from vctl.lb.runtime import BackendStatus

    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    bs.add("10.0.0.5:8000")  # pre-populate state file

    mock_row = MagicMock(spec=BackendStatus)
    mock_row.endpoint = "10.0.0.5:8000"
    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = [mock_row]
    mock_client.set_state.return_value = None
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    rc = lb_scaling._do_add("10.0.0.5:8000", mgr, bs, pool_name="default")
    assert rc == 0
    captured = capsys.readouterr()
    assert "READIED" in captured.err


def test_do_add_haproxy_backend_op_failed_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """_do_add returns 1 when haproxy add_server raises RuntimeError (BackendOpFailed)."""
    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")

    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = []
    mock_client.add_server.side_effect = RuntimeError("connection refused")
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    rc = lb_scaling._do_add("10.0.0.5:8000", mgr, bs, pool_name="default")
    assert rc == 1
    # State file must NOT be written when haproxy refuses
    assert "10.0.0.5:8000" not in BackendState(state_dir, "10.0.0.1", pool="default").list()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /mnt/umm/users/qianjianheng/workspace/vctl && python -m pytest tests/test_commands_lb_scaling.py::test_do_add_happy_path_returns_0_and_surfaces_added tests/test_commands_lb_scaling.py::test_do_add_exits_4_when_lb_unreachable tests/test_commands_lb_scaling.py::test_do_add_action_readied_on_idempotent_reregister tests/test_commands_lb_scaling.py::test_do_add_haproxy_backend_op_failed_returns_1 -v 2>&1 | tail -20
```

Expected: FAIL — new tests fail because the current `_do_add` does not produce `ADDED`/`READIED` in stderr, returns 0 on LB-unreachable (F11), and uses legacy `(new)` / `(already present)` strings.

- [ ] **Step 3: Implement**

Replace the `_do_add` function body in `src/vctl/commands/lb_scaling.py`. The `bs` parameter stays in the signature for backward compatibility with `_do_add_cli` and `_do_attach` callers, but the body no longer uses it — Reconciler reads and writes its own `BackendState` instances internally.

```python
def _do_add(ep: str, mgr: LbManager, bs: BackendState, pool_name: str | None = None) -> int:
    pool_name = _resolve_pool_name(mgr, pool_name)
    del bs  # unused: Reconciler owns all state mutations; kept for caller compat
    try:
        outcome = Reconciler(mgr).want_present(ep, pool_name)
        print(f"add {ep} {outcome.action.name} (pool: {pool_name})", file=sys.stderr)
        return 0
    except ReconcilerError as exc:
        print(f"add {ep} failed: {exc}", file=sys.stderr)
        return _exit_for(exc)
```

Also update the two existing integration-style tests in `tests/test_commands_lb_scaling.py` that assert on the legacy `(new)` and `(already present)` strings. These tests use `VCTL_TEST_NO_SOCKET=1` which causes `lb_admin_client` to return `_NoOpClient` (per Phase 1's sealed contract — NOT `None`, exit stays 0). `_NoOpClient.show_servers_state()` returns `[]` always, so Reconciler sees `in_haproxy=False` on every call. The first call yields `Action.ADDED`, the second call yields `Action.READIED` (in_state=True, in_haproxy=False → re-register path). Exit code stays 0; only the stderr label changes. Rewrite as direct-call unit tests with a mocked Reconciler client to avoid spawning subprocesses (faster + more precise assertions on the action values):

```python
def test_lb_add_idempotent_first_then_dup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """First call says ADDED, second call says READIED (idempotent re-add)."""
    from vctl.lb.runtime import BackendStatus

    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")

    # First add: ep not in haproxy, not in state → ADDED
    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = []
    mock_client.add_server.return_value = None
    mock_client.set_state.return_value = None
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    rc1 = lb_scaling._do_add("10.0.0.5:8000", mgr, bs, pool_name="default")
    assert rc1 == 0
    cap1 = capsys.readouterr()
    assert "ADDED" in cap1.err

    # Second add: ep in haproxy AND in state → READIED
    mock_row = MagicMock(spec=BackendStatus)
    mock_row.endpoint = "10.0.0.5:8000"
    mock_client2 = MagicMock()
    mock_client2.show_servers_state.return_value = [mock_row]
    mock_client2.set_state.return_value = None
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client2)

    rc2 = lb_scaling._do_add("10.0.0.5:8000", mgr, bs, pool_name="default")
    assert rc2 == 0
    cap2 = capsys.readouterr()
    assert "READIED" in cap2.err


def test_lb_add_with_explicit_pool_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """`lb add 10.x.x.x:8000 --pool a` registers in pool a's state."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    lb = LbHaproxy(
        host="10.0.0.1",
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        health=LbHealth(),
        defaults=LbDefaults(),
        pools=[
            Pool(name="a", served_model="M/A", bind_port=8080),
            Pool(name="b", served_model="M/B", bind_port=8081),
        ],
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    mgr = LbManager(lb, state_dir=state_dir, run_dir=run_dir)
    bs = BackendState(state_dir, "10.0.0.1", pool="a")

    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = []
    mock_client.add_server.return_value = None
    mock_client.set_state.return_value = None
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    rc = lb_scaling._do_add_cli("10.0.0.5:8000", mgr, bs, requested_pool="a")
    assert rc == 0, capsys.readouterr().err
    cap = capsys.readouterr()
    assert "ADDED" in cap.err
    assert "10.0.0.5:8000" in BackendState(state_dir, "10.0.0.1", pool="a").list()
    assert "10.0.0.5:8000" not in BackendState(state_dir, "10.0.0.1", pool="b").list()
```

Also update the A5 tests that patch `lb_scaling._client` and test error handling. After migration, `_do_add` delegates to Reconciler, so errors must be triggered via `reconciler_mod.lb_admin_client`. The A5 tests `test_do_add_propagates_haproxy_error`, `test_do_add_idempotent_already_exists_is_ok`, and `test_do_add_no_such_backend_exits_3` need to be rewritten to patch at the Reconciler level. The old distinction between "already exists" (exit 0) and "no such backend" (exit 3) mapped to string-inspection in `_do_add` — Reconciler normalises these through `BackendOpFailed` (exit 1). Replace the three tests with:

```python
def test_do_add_propagates_haproxy_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A5: _do_add returns non-zero when haproxy add_server raises RuntimeError."""
    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")

    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = []
    mock_client.add_server.side_effect = RuntimeError("connection refused")
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    rc = lb_scaling._do_add("10.0.0.5:8000", mgr, bs, pool_name="default")
    assert rc != 0
    assert "10.0.0.5:8000" not in BackendState(state_dir, "10.0.0.1", pool="default").list()


def test_do_add_idempotent_already_exists_is_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A5: haproxy already knowing the server returns exit 0 (ADOPTED or READIED)."""
    from vctl.lb.runtime import BackendStatus

    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")

    # Ep is already in haproxy but not in state → ADOPTED (idempotent re-add path)
    mock_row = MagicMock(spec=BackendStatus)
    mock_row.endpoint = "10.0.0.5:8000"
    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = [mock_row]
    mock_client.set_state.return_value = None
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    rc = lb_scaling._do_add("10.0.0.5:8000", mgr, bs, pool_name="default")
    assert rc == 0
    cap = capsys.readouterr()
    assert "ADOPTED" in cap.err


def test_do_add_no_such_backend_exits_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A5: unknown pool name → exit 3 (PoolNotFound from _resolve_pool_name)."""
    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")

    # _resolve_pool_name calls sys.exit(3) for unknown pool names — no mock needed
    with pytest.raises(SystemExit) as exc_info:
        lb_scaling._do_add("10.0.0.5:8000", mgr, bs, pool_name="nonexistent")
    assert exc_info.value.code == 3
    assert "10.0.0.5:8000" not in BackendState(state_dir, "10.0.0.1", pool="default").list()
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /mnt/umm/users/qianjianheng/workspace/vctl && python -m pytest tests/test_commands_lb_scaling.py -k "add" -v 2>&1 | tail -20
```

Expected: PASS — all add-related tests pass with new assertions

- [ ] **Step 5: Commit**

```bash
git add src/vctl/commands/lb_scaling.py tests/test_commands_lb_scaling.py
git commit -m "$(cat <<'EOF'
feat: migrate _do_add to Reconciler.want_present — closes F11 (Phase 2 T3)

State file is no longer written before haproxy ack. Replaces (new)/
(already present) stderr strings with Outcome.action.name values.
Tests updated: A5 patches at Reconciler level; subprocess tests
converted to unit tests with mock client.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Migrate `_do_remove` to Reconciler.want_absent

**Files:**
- Modify: `src/vctl/commands/lb_scaling.py` — replace `_do_remove` body; `bs` stays in signature but unused
- Modify: `tests/test_commands_lb_scaling.py` — flip exit-4 expectation patching level; update stderr assertions; add haproxy-first ordering test

Acceptance tests mapped here:
- **AT-3:** `test: lb remove against running LB exits 0 and surfaces REMOVED action`
- **AT-4:** `test: lb remove against stopped LB exits 4 and leaves state file unchanged`

- [ ] **Step 1: Write the failing test**

Add the following tests to `tests/test_commands_lb_scaling.py`:

```python
# ---------------------------------------------------------------------------
# Task 4: _do_remove — Reconciler migration
# ---------------------------------------------------------------------------


def test_do_remove_happy_path_returns_0_and_surfaces_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AT-3: _do_remove returns 0, prints REMOVED to stderr, removes from state file."""
    from vctl.lb.runtime import BackendStatus

    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    bs.add("10.0.0.5:8000")

    mock_row = MagicMock(spec=BackendStatus)
    mock_row.endpoint = "10.0.0.5:8000"
    set_state_calls: list[tuple[object, ...]] = []
    remove_server_calls: list[tuple[object, ...]] = []

    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = [mock_row]
    mock_client.set_state.side_effect = lambda *a: set_state_calls.append(a)
    mock_client.remove_server.side_effect = lambda *a: remove_server_calls.append(a)
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    rc = lb_scaling._do_remove("10.0.0.5:8000", mgr, bs, pool_name="default")
    assert rc == 0
    captured = capsys.readouterr()
    assert "REMOVED" in captured.err
    assert "10.0.0.5:8000" not in BackendState(state_dir, "10.0.0.1", pool="default").list()
    # haproxy-first ordering: set_state(maint) must precede remove_server
    assert any("maint" in str(c) for c in set_state_calls), "set_state maint not called"
    assert remove_server_calls, "remove_server not called"


def test_do_remove_exits_4_when_lb_unreachable_reconciler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AT-4: _do_remove returns 4 and leaves state file unchanged when LB is unreachable."""
    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    bs.add("10.0.0.5:8000")

    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: None)

    rc = lb_scaling._do_remove("10.0.0.5:8000", mgr, bs, pool_name="default")
    assert rc == 4
    assert "10.0.0.5:8000" in BackendState(state_dir, "10.0.0.1", pool="default").list()
    captured = capsys.readouterr()
    assert "failed" in captured.err.lower() or "unreachable" in captured.err.lower()


def test_do_remove_orphaned_cleaned_when_only_in_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """_do_remove returns 0 and surfaces ORPHANED_CLEANED when ep in state but not haproxy."""
    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    bs.add("10.0.0.5:8000")

    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = []  # ep not in haproxy
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    rc = lb_scaling._do_remove("10.0.0.5:8000", mgr, bs, pool_name="default")
    assert rc == 0
    captured = capsys.readouterr()
    assert "ORPHANED_CLEANED" in captured.err
    assert "10.0.0.5:8000" not in BackendState(state_dir, "10.0.0.1", pool="default").list()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /mnt/umm/users/qianjianheng/workspace/vctl && python -m pytest tests/test_commands_lb_scaling.py::test_do_remove_happy_path_returns_0_and_surfaces_removed tests/test_commands_lb_scaling.py::test_do_remove_exits_4_when_lb_unreachable_reconciler tests/test_commands_lb_scaling.py::test_do_remove_orphaned_cleaned_when_only_in_state -v 2>&1 | tail -20
```

Expected: FAIL — current `_do_remove` uses direct `_client` calls and prints legacy strings, not `REMOVED` / `ORPHANED_CLEANED`.

- [ ] **Step 3: Implement**

Replace the `_do_remove` function body in `src/vctl/commands/lb_scaling.py`. The `bs` parameter is kept in the signature (backward compatibility with callers) but the body delegates entirely to Reconciler:

```python
def _do_remove(ep: str, mgr: LbManager, bs: BackendState, pool_name: str | None = None) -> int:
    """Haproxy-first removal via Reconciler.want_absent.

    Reconciler enforces: set maint → del server → remove from state file.
    State file is NOT mutated if haproxy is unreachable or if any haproxy
    step fails (LbUnreachable or BackendOpFailed raised).
    """
    pool_name = _resolve_pool_name(mgr, pool_name)
    del bs  # unused: Reconciler owns all state mutations; kept for caller compat
    try:
        outcome = Reconciler(mgr).want_absent(ep, pool_name)
        print(f"remove {ep} {outcome.action.name} (pool: {pool_name})", file=sys.stderr)
        return 0
    except ReconcilerError as exc:
        print(f"remove {ep} failed: {exc}", file=sys.stderr)
        return _exit_for(exc)
```

Also update the existing A6 tests that patch `lb_scaling._client` to patch at the Reconciler level. The A6 tests `test_do_remove_exits_4_when_lb_unreachable`, `test_do_remove_state_unchanged_when_set_maint_fails`, `test_do_remove_state_unchanged_when_del_server_fails`, and `test_do_remove_happy_path_ordering` need updating:

```python
def test_do_remove_exits_4_when_lb_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A6: _do_remove must return 4 and NOT mutate state when LB is unreachable."""
    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    bs.add("10.0.0.5:8000")

    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: None)

    rc = lb_scaling._do_remove("10.0.0.5:8000", mgr, bs, pool_name="default")
    assert rc == 4
    assert "10.0.0.5:8000" in BackendState(state_dir, "10.0.0.1", pool="default").list()


def test_do_remove_state_unchanged_when_set_maint_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A6: state file must NOT be mutated if set_state maint raises RuntimeError."""
    from vctl.lb.runtime import BackendStatus

    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    bs.add("10.0.0.5:8000")

    mock_row = MagicMock(spec=BackendStatus)
    mock_row.endpoint = "10.0.0.5:8000"
    cli = MagicMock()
    cli.show_servers_state.return_value = [mock_row]
    cli.set_state.side_effect = RuntimeError("haproxy reloading")
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: cli)

    rc = lb_scaling._do_remove("10.0.0.5:8000", mgr, bs, pool_name="default")
    assert rc != 0
    assert "10.0.0.5:8000" in BackendState(state_dir, "10.0.0.1", pool="default").list()


def test_do_remove_state_unchanged_when_del_server_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A6: state file must NOT be mutated if remove_server raises RuntimeError."""
    from vctl.lb.runtime import BackendStatus

    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    bs.add("10.0.0.5:8000")

    mock_row = MagicMock(spec=BackendStatus)
    mock_row.endpoint = "10.0.0.5:8000"
    cli = MagicMock()
    cli.show_servers_state.return_value = [mock_row]
    cli.set_state.return_value = None  # maint succeeds
    cli.remove_server.side_effect = RuntimeError("Operation not permitted")
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: cli)

    rc = lb_scaling._do_remove("10.0.0.5:8000", mgr, bs, pool_name="default")
    assert rc != 0
    assert "10.0.0.5:8000" in BackendState(state_dir, "10.0.0.1", pool="default").list()


def test_do_remove_happy_path_ordering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A6: on success, set_state maint is called before remove_server; state cleaned up."""
    from vctl.lb.runtime import BackendStatus

    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    bs.add("10.0.0.5:8000")

    mock_row = MagicMock(spec=BackendStatus)
    mock_row.endpoint = "10.0.0.5:8000"
    call_order: list[str] = []
    cli = MagicMock()
    cli.show_servers_state.return_value = [mock_row]
    cli.set_state.side_effect = lambda *a, **kw: call_order.append("set_state")
    cli.remove_server.side_effect = lambda *a, **kw: call_order.append("remove_server")
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: cli)

    rc = lb_scaling._do_remove("10.0.0.5:8000", mgr, bs, pool_name="default")
    assert rc == 0
    assert call_order == ["set_state", "remove_server"]
    assert "10.0.0.5:8000" not in BackendState(state_dir, "10.0.0.1", pool="default").list()
```

Also update `test_lb_remove_after_add` and `test_lb_remove_finds_pool_automatically` (subprocess-based tests that use `VCTL_TEST_NO_SOCKET=1`). Per Phase 1's sealed contract, `VCTL_TEST_NO_SOCKET=1` returns `_NoOpClient` (NOT `None`), so haproxy admin calls become no-ops, state file is still written/cleaned, and exit stays 0. The tests merely need to surface `Action.REMOVED` / `Action.ORPHANED_CLEANED` instead of legacy strings. Rewrite as direct-call unit tests with a mocked Reconciler client (faster + more precise):

```python
def test_lb_remove_after_add(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    bs.add("10.0.0.5:8000")

    from vctl.lb.runtime import BackendStatus
    mock_row = MagicMock(spec=BackendStatus)
    mock_row.endpoint = "10.0.0.5:8000"
    cli = MagicMock()
    cli.show_servers_state.return_value = [mock_row]
    cli.set_state.return_value = None
    cli.remove_server.return_value = None
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: cli)

    rc = lb_scaling._do_remove("10.0.0.5:8000", mgr, bs, pool_name="default")
    assert rc == 0


def test_lb_remove_finds_pool_automatically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vctl.lb.runtime import BackendStatus

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    lb = LbHaproxy(
        host="10.0.0.1",
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        health=LbHealth(),
        defaults=LbDefaults(),
        pools=[
            Pool(name="a", served_model="M/A", bind_port=8080),
            Pool(name="b", served_model="M/B", bind_port=8081),
        ],
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    mgr = LbManager(lb, state_dir=state_dir, run_dir=run_dir)
    bs_a = BackendState(state_dir, "10.0.0.1", pool="a")
    bs_a.add("10.0.0.5:8000")
    bs_top = BackendState(state_dir, "10.0.0.1")

    mock_row = MagicMock(spec=BackendStatus)
    mock_row.endpoint = "10.0.0.5:8000"
    cli = MagicMock()
    cli.show_servers_state.return_value = [mock_row]
    cli.set_state.return_value = None
    cli.remove_server.return_value = None
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: cli)

    rc = lb_scaling._do_remove_cli("10.0.0.5:8000", mgr, bs_top)
    assert rc == 0
    assert "10.0.0.5:8000" not in BackendState(state_dir, "10.0.0.1", pool="a").list()
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /mnt/umm/users/qianjianheng/workspace/vctl && python -m pytest tests/test_commands_lb_scaling.py -k "remove" -v 2>&1 | tail -20
```

Expected: PASS — all remove-related tests pass

- [ ] **Step 5: Commit**

```bash
git add src/vctl/commands/lb_scaling.py tests/test_commands_lb_scaling.py
git commit -m "$(cat <<'EOF'
feat: migrate _do_remove to Reconciler.want_absent (Phase 2 T4)

Drops manual maint→del→state ordering in favour of Reconciler delegation.
Surfaces REMOVED / ORPHANED_CLEANED in stderr. A6 tests updated to
patch at reconciler_mod level; subprocess tests converted to unit tests.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Checkpoint 2 — add + remove migrated, F11 closed

Run the full test suite and type-check:

```bash
cd /mnt/umm/users/qianjianheng/workspace/vctl && python -m pytest tests/test_commands_lb_scaling.py -v 2>&1 | tail -30
```

Expected: all tests pass — no regressions from Task 1–4.

```bash
cd /mnt/umm/users/qianjianheng/workspace/vctl && python -m mypy --strict src/vctl/commands/lb_scaling.py 2>&1 | tail -10
```

Expected: `Success: no issues found`

F11 audit (manual step):

```bash
grep -n "bs\.add\|bs\.remove" /mnt/umm/users/qianjianheng/workspace/vctl/src/vctl/commands/lb_scaling.py
```

Expected: zero matches — every state mutation now goes through Reconciler.

---

### Task 5: Migrate `_do_auto_add` to Reconciler.reconcile_from_state

**Files:**
- Modify: `src/vctl/commands/lb_scaling.py` — replace `_do_auto_add` body; drop `contextlib.suppress`; add failure accumulation
- Modify: `tests/test_commands_lb_scaling.py` — flip always-0 expectation; add partial-failure and LB-down tests

Acceptance test mapped here:
- **AT-7:** `test: lb auto-add exits 1 and identifies failed pool when LB unreachable` (closes F12 regression test)

- [ ] **Step 1: Write the failing test**

Add the following tests to `tests/test_commands_lb_scaling.py`:

```python
# ---------------------------------------------------------------------------
# Task 5: _do_auto_add — Reconciler migration (closes F12)
# ---------------------------------------------------------------------------


def test_do_auto_add_exits_1_when_pool_reconcile_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AT-7 / F12: _do_auto_add must return 1 and identify the failed pool on LB-down."""
    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    bs.add("10.0.0.5:8000")

    # LB unreachable → Reconciler raises LbUnreachable → failure accumulated
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: None)

    rc = lb_scaling._do_auto_add(mgr, bs)
    assert rc == 1
    captured = capsys.readouterr()
    assert "default" in captured.err  # pool name must appear in failure message


def test_do_auto_add_happy_path_returns_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """_do_auto_add returns 0 when all pools reconcile successfully."""
    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    bs.add("10.0.0.5:8000")

    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = []
    mock_client.add_server.return_value = None
    mock_client.set_state.return_value = None
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    rc = lb_scaling._do_auto_add(mgr, bs)
    assert rc == 0
    captured = capsys.readouterr()
    assert "ADDED" in captured.err or "READIED" in captured.err


def test_do_auto_add_partial_failure_continues_and_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """_do_auto_add: one pool fails, other succeeds; accumulate failures, exit 1."""
    state_dir = tmp_path / "state"
    lb = LbHaproxy(
        host="10.0.0.1",
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        health=LbHealth(),
        defaults=LbDefaults(),
        pools=[
            Pool(name="a", served_model="M/A", bind_port=8080),
            Pool(name="b", served_model="M/B", bind_port=8081),
        ],
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    mgr = LbManager(lb, state_dir=state_dir, run_dir=run_dir)
    bs_a = BackendState(state_dir, "10.0.0.1", pool="a")
    bs_a.add("10.0.0.5:8000")
    bs_b = BackendState(state_dir, "10.0.0.1", pool="b")
    bs_b.add("10.0.0.6:8000")
    bs_top = BackendState(state_dir, "10.0.0.1")

    call_count = 0

    def side_effect_client(m: object) -> MagicMock | None:
        nonlocal call_count
        call_count += 1
        if call_count <= 3:
            # First few calls for pool "a" succeed
            mc = MagicMock()
            mc.show_servers_state.return_value = []
            mc.add_server.return_value = None
            mc.set_state.return_value = None
            return mc
        # Subsequent calls for pool "b" fail
        return None

    monkeypatch.setattr(reconciler_mod, "lb_admin_client", side_effect_client)

    rc = lb_scaling._do_auto_add(mgr, bs_top)
    assert rc == 1
    captured = capsys.readouterr()
    # Both pools must appear in output: a succeeded, b failed
    assert "b" in captured.err
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /mnt/umm/users/qianjianheng/workspace/vctl && python -m pytest tests/test_commands_lb_scaling.py::test_do_auto_add_exits_1_when_pool_reconcile_fails tests/test_commands_lb_scaling.py::test_do_auto_add_happy_path_returns_0 tests/test_commands_lb_scaling.py::test_do_auto_add_partial_failure_continues_and_exits_1 -v 2>&1 | tail -20
```

Expected: FAIL — current `_do_auto_add` always returns 0 (F12 root cause) and uses `contextlib.suppress` which swallows the `LbUnreachable` error.

- [ ] **Step 3: Implement**

Replace the `_do_auto_add` function body in `src/vctl/commands/lb_scaling.py`:

```python
def _do_auto_add(mgr: LbManager, bs: BackendState) -> int:
    """Reconcile all pools from state files. Accumulates failures — does not
    short-circuit on a single broken pool so healthy pools continue to recover.

    Closes F12: contextlib.suppress is removed; any ReconcilerError is surfaced
    on stderr and causes exit 1.
    """
    pool_names = BackendState.list_pools(bs.state_dir, bs.lb_host)
    if not pool_names:
        pool_names = [p.name for p in mgr.lb.pools]
    failed: list[str] = []
    for pname in pool_names:
        try:
            outcomes = Reconciler(mgr).reconcile_from_state(pname)
            for outcome in outcomes:
                print(
                    f"auto-add {outcome.ep} {outcome.action.name} (pool: {pname})",
                    file=sys.stderr,
                )
        except ReconcilerError as exc:
            print(f"auto-add pool {pname!r} failed: {exc}", file=sys.stderr)
            failed.append(pname)
    return 1 if failed else 0
```

Also update the existing A7 tests that patch `lb_scaling._client` and test auto-add behaviour. After migration, `_do_auto_add` no longer calls `_client` directly — it delegates to Reconciler. Update `test_do_auto_add_calls_force_ready` and `test_do_auto_add_force_ready_called_even_when_add_raises`:

```python
def test_do_auto_add_calls_force_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A7: reconcile_from_state calls want_present which unconditionally calls set_state ready."""
    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    bs.add("10.0.0.5:8000")

    set_state_calls: list[tuple[object, ...]] = []
    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = []
    mock_client.add_server.return_value = None
    mock_client.set_state.side_effect = lambda *a: set_state_calls.append(a)
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    rc = lb_scaling._do_auto_add(mgr, bs)
    assert rc == 0
    ready_calls = [c for c in set_state_calls if "ready" in c]
    assert ready_calls, f"set_state 'ready' not called; calls: {set_state_calls}"


def test_do_auto_add_force_ready_called_even_when_add_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A7: Reconciler.want_present calls set_state ready unconditionally even when
    add_server returns (ep already present in haproxy → skip add_server, still set ready)."""
    from vctl.lb.runtime import BackendStatus

    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    bs.add("10.0.0.5:8000")

    # Ep already in haproxy → want_present skips add_server and calls set_state ready
    mock_row = MagicMock(spec=BackendStatus)
    mock_row.endpoint = "10.0.0.5:8000"
    set_state_calls: list[tuple[object, ...]] = []
    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = [mock_row]
    mock_client.set_state.side_effect = lambda *a: set_state_calls.append(a)
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    rc = lb_scaling._do_auto_add(mgr, bs)
    assert rc == 0
    ready_calls = [c for c in set_state_calls if "ready" in c]
    assert ready_calls, "force-ready must be attempted (set_state ready called by Reconciler)"
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /mnt/umm/users/qianjianheng/workspace/vctl && python -m pytest tests/test_commands_lb_scaling.py -k "auto_add" -v 2>&1 | tail -15
```

Expected: PASS — all auto-add tests pass; `test_do_auto_add_exits_1_when_pool_reconcile_fails` now returns 1 instead of 0

- [ ] **Step 5: Commit**

```bash
git add src/vctl/commands/lb_scaling.py tests/test_commands_lb_scaling.py
git commit -m "$(cat <<'EOF'
feat: migrate _do_auto_add to Reconciler.reconcile_from_state — closes F12 (Phase 2 T5)

Removes contextlib.suppress from auto-add loop. Any ReconcilerError is
now surfaced on stderr and causes exit 1. Processing continues for
remaining pools on partial failure. A7 tests updated to Reconciler level.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Migrate `_do_remove_cli` to Reconciler.want_absent across pool scan

**Files:**
- Modify: `src/vctl/commands/lb_scaling.py` — replace `_do_remove_cli` body; drop legacy `_client` fallback branch
- Modify: `tests/test_commands_lb_scaling.py` — update A8 tests to new mock level; add fallback-loop and first-error-exit-4 tests

- [ ] **Step 1: Write the failing test**

Add the following tests to `tests/test_commands_lb_scaling.py`:

```python
# ---------------------------------------------------------------------------
# Task 6: _do_remove_cli — Reconciler migration
# ---------------------------------------------------------------------------


def test_do_remove_cli_exits_4_when_ep_found_in_state_but_lb_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_do_remove_cli returns 4 when ep found in state file but LB is unreachable."""
    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    bs.add("10.0.0.5:8000")
    bs_top = BackendState(state_dir, "10.0.0.1")

    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: None)

    rc = lb_scaling._do_remove_cli("10.0.0.5:8000", mgr, bs_top)
    assert rc == 4
    # State file must remain unchanged
    assert "10.0.0.5:8000" in BackendState(state_dir, "10.0.0.1", pool="default").list()


def test_do_remove_cli_fallback_loop_exits_4_on_first_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_do_remove_cli fallback loop returns _exit_for on first ReconcilerError (no accumulation)."""
    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs_top = BackendState(state_dir, "10.0.0.1")
    # No ep in any state file → triggers fallback loop

    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: None)

    rc = lb_scaling._do_remove_cli("10.0.0.5:8000", mgr, bs_top)
    # LbUnreachable in fallback loop → 4
    assert rc == 4


def test_do_remove_cli_fallback_iterates_configured_pools_when_state_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """_do_remove_cli fallback: ep not in any state file but found in haproxy → ORPHANED_CLEANED."""
    from vctl.lb.runtime import BackendStatus

    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs_top = BackendState(state_dir, "10.0.0.1")
    # No state files at all → pool_names falls back to configured pools

    mock_row = MagicMock(spec=BackendStatus)
    mock_row.endpoint = "10.0.0.5:8000"
    # show_servers_state: first call (pre-state read) returns ep → in_haproxy=True; not in state
    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = [mock_row]
    mock_client.set_state.return_value = None
    mock_client.remove_server.return_value = None
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    rc = lb_scaling._do_remove_cli("10.0.0.5:8000", mgr, bs_top)
    assert rc == 0
    captured = capsys.readouterr()
    assert "ORPHANED_CLEANED" in captured.err or "REMOVED" in captured.err
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /mnt/umm/users/qianjianheng/workspace/vctl && python -m pytest tests/test_commands_lb_scaling.py::test_do_remove_cli_exits_4_when_ep_found_in_state_but_lb_unreachable tests/test_commands_lb_scaling.py::test_do_remove_cli_fallback_loop_exits_4_on_first_error tests/test_commands_lb_scaling.py::test_do_remove_cli_fallback_iterates_configured_pools_when_state_empty -v 2>&1 | tail -20
```

Expected: FAIL — current `_do_remove_cli` returns 1 (not 4) on LB-unreachable and uses legacy `_client` branch.

- [ ] **Step 3: Implement**

Replace the `_do_remove_cli` function body in `src/vctl/commands/lb_scaling.py`:

```python
def _do_remove_cli(ep: str, mgr: LbManager, bs: BackendState) -> int:
    """User-invoked ``lb remove``. Scan all pools for the ep.

    State-file-found path: calls Reconciler.want_absent for the matching pool.
    Fallback path (ep absent from all state files): iterates configured pools
    and calls Reconciler.want_absent on each — idempotent. Returns on the first
    ReconcilerError (differs from _do_auto_add's accumulate model because this
    is a single-ep operation, not a bulk reconcile).
    """
    pool_names = BackendState.list_pools(bs.state_dir, bs.lb_host)
    if not pool_names:
        pool_names = [p.name for p in mgr.lb.pools]

    for pname in pool_names:
        pbs = BackendState(bs.state_dir, bs.lb_host, pool=pname)
        if ep in pbs.list():
            try:
                outcome = Reconciler(mgr).want_absent(ep, pname)
                print(f"remove {ep} {outcome.action.name} (pool: {pname})", file=sys.stderr)
                return 0
            except ReconcilerError as exc:
                print(f"remove {ep} failed: {exc}", file=sys.stderr)
                return _exit_for(exc)

    # Not found in any state file — attempt haproxy-side cleanup across all pools.
    any_removed = False
    for pname in pool_names:
        try:
            outcome = Reconciler(mgr).want_absent(ep, pname)
            if outcome.action not in (Action.NONE,):
                print(f"remove {ep} {outcome.action.name} (pool: {pname})", file=sys.stderr)
                any_removed = True
        except ReconcilerError as exc:
            print(f"remove {ep} pool {pname!r} failed: {exc}", file=sys.stderr)
            return _exit_for(exc)

    if any_removed:
        return 0
    print(f"endpoint {ep} not found in any pool state file or haproxy", file=sys.stderr)
    return 1
```

Also update the existing A8 tests that patch `lb_scaling._client`:

```python
def test_do_remove_cli_returns_1_when_not_found_and_haproxy_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A8: remove_cli returns 1 when endpoint not in any state file and
    Reconciler.want_absent returns Action.NONE for all pools."""
    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    # Ensure state file exists but is empty so list_pools finds "default"
    bs.add("10.0.0.99:9999")
    bs.remove("10.0.0.99:9999")
    bs_top = BackendState(state_dir, "10.0.0.1")

    # want_absent returns NONE when ep is not in haproxy and not in state
    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = []  # ep not found in haproxy
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    rc = lb_scaling._do_remove_cli("10.0.0.5:8000", mgr, bs_top)
    assert rc == 1


def test_do_remove_cli_returns_0_when_haproxy_cleanup_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A8: remove_cli returns 0 when ep not in state file but haproxy removal
    succeeds (ORPHANED_CLEANED — stale entry cleanup)."""
    from vctl.lb.runtime import BackendStatus

    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    bs.add("10.0.0.99:9999")
    bs.remove("10.0.0.99:9999")
    bs_top = BackendState(state_dir, "10.0.0.1")

    mock_row = MagicMock(spec=BackendStatus)
    mock_row.endpoint = "10.0.0.5:8000"
    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = [mock_row]
    mock_client.set_state.return_value = None
    mock_client.remove_server.return_value = None
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)

    rc = lb_scaling._do_remove_cli("10.0.0.5:8000", mgr, bs_top)
    assert rc == 0


def test_do_remove_cli_returns_1_no_socket_and_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A8: remove_cli returns 4 when endpoint not found in state AND socket is down
    (fallback loop hits LbUnreachable → exit 4, not 1)."""
    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1")

    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: None)

    rc = lb_scaling._do_remove_cli("10.0.0.5:8000", mgr, bs)
    # After migration: fallback loop hits LbUnreachable → _exit_for → 4
    assert rc == 4
```

Note: `test_do_remove_cli_returns_1_no_socket_and_not_found` changes its expected return code from 1 to 4 — the new fallback loop returns `_exit_for(LbUnreachable(...))` = 4 on the first haproxy error, matching the spec's "single-ep operation exits on first error" contract.

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /mnt/umm/users/qianjianheng/workspace/vctl && python -m pytest tests/test_commands_lb_scaling.py -k "remove_cli" -v 2>&1 | tail -15
```

Expected: PASS — all remove_cli tests pass

- [ ] **Step 5: Commit**

```bash
git add src/vctl/commands/lb_scaling.py tests/test_commands_lb_scaling.py
git commit -m "$(cat <<'EOF'
feat: migrate _do_remove_cli to Reconciler.want_absent scan (Phase 2 T6)

Drops legacy _client fallback branch. Fallback loop uses Reconciler and
returns on first ReconcilerError (single-ep operation). A8 tests updated
to reconciler_mod level; test_returns_1_no_socket now expects exit 4.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Checkpoint 3 — auto-add + remove_cli migrated, F12 closed

Run the full test suite:

```bash
cd /mnt/umm/users/qianjianheng/workspace/vctl && python -m pytest tests/test_commands_lb_scaling.py -v 2>&1 | tail -30
```

Expected: all tests pass.

F12 audit:

```bash
grep -n "contextlib\.suppress" /mnt/umm/users/qianjianheng/workspace/vctl/src/vctl/commands/lb_scaling.py
```

Expected: zero matches in `_do_auto_add` and `_do_add` bodies (may still appear in `_do_detach` until Task 7).

---

### Task 7: Migrate `_do_detach` to Reconciler chain (drain + wait + remove)

**Files:**
- Modify: `src/vctl/commands/lb_scaling.py` — replace `_do_detach` body; chain `want_draining` → poll loop → `want_absent`
- Modify: `tests/test_commands_lb_scaling.py` — add LB-down-during-drain and LB-down-during-remove tests; update existing detach tests for new stderr format

- [ ] **Step 1: Write the failing test**

Add the following tests to `tests/test_commands_lb_scaling.py`:

```python
# ---------------------------------------------------------------------------
# Task 7: _do_detach — Reconciler migration
# ---------------------------------------------------------------------------


def test_do_detach_exits_4_when_lb_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_do_detach returns 4 when LB is unreachable during the drain phase."""
    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    bs.add("10.0.0.5:8000")

    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: None)
    monkeypatch.setattr(lb_scaling, "detect_self_ip", lambda: "10.0.0.5")

    rc = lb_scaling._do_detach(mgr, bs)
    assert rc == 4


def test_do_detach_happy_path_returns_0_and_surfaces_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """_do_detach returns 0 and surfaces REMOVED after drain + remove cycle."""
    from vctl.lb.runtime import BackendStatus

    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    bs.add("10.0.0.5:8000")

    mock_row = MagicMock(spec=BackendStatus)
    mock_row.endpoint = "10.0.0.5:8000"
    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = [mock_row]
    mock_client.set_state.return_value = None
    mock_client.remove_server.return_value = None
    monkeypatch.setattr(reconciler_mod, "lb_admin_client", lambda m: mock_client)
    monkeypatch.setattr(lb_scaling, "detect_self_ip", lambda: "10.0.0.5")
    # Skip the drain-wait loop by making probe report no running requests immediately
    monkeypatch.setattr(
        lb_scaling, "probe_local_vllm", lambda port: {"num_requests_running": 0.0}
    )
    monkeypatch.setenv("LB_DETACH_WAIT", "1")

    rc = lb_scaling._do_detach(mgr, bs)
    assert rc == 0
    captured = capsys.readouterr()
    assert "REMOVED" in captured.err


def test_do_detach_exits_4_when_lb_down_during_remove_after_successful_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_do_detach returns 4 if LB goes down between drain and remove phases."""
    from vctl.lb.runtime import BackendStatus

    state_dir = tmp_path / "state"
    mgr = _make_mgr(tmp_path)
    bs = BackendState(state_dir, "10.0.0.1", pool="default")
    bs.add("10.0.0.5:8000")

    call_count = 0

    def flaky_client(m: object) -> MagicMock | None:
        nonlocal call_count
        call_count += 1
        if call_count <= 1:
            # First call: drain phase — LB is up
            mc = MagicMock()
            mc.show_servers_state.return_value = [MagicMock(spec=BackendStatus, endpoint="10.0.0.5:8000")]
            mc.set_state.return_value = None
            return mc
        # Subsequent calls: remove phase — LB has gone down
        return None

    monkeypatch.setattr(reconciler_mod, "lb_admin_client", flaky_client)
    monkeypatch.setattr(lb_scaling, "detect_self_ip", lambda: "10.0.0.5")
    monkeypatch.setattr(
        lb_scaling, "probe_local_vllm", lambda port: {"num_requests_running": 0.0}
    )
    monkeypatch.setenv("LB_DETACH_WAIT", "1")

    rc = lb_scaling._do_detach(mgr, bs)
    assert rc == 4
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /mnt/umm/users/qianjianheng/workspace/vctl && python -m pytest tests/test_commands_lb_scaling.py::test_do_detach_exits_4_when_lb_unreachable tests/test_commands_lb_scaling.py::test_do_detach_happy_path_returns_0_and_surfaces_removed tests/test_commands_lb_scaling.py::test_do_detach_exits_4_when_lb_down_during_remove_after_successful_drain -v 2>&1 | tail -20
```

Expected: FAIL — current `_do_detach` uses `_client` directly and `contextlib.suppress` on drain; does not surface `REMOVED` in stderr.

- [ ] **Step 3: Implement**

Replace the `_do_detach` function body in `src/vctl/commands/lb_scaling.py`:

```python
def _do_detach(mgr: LbManager, bs: BackendState) -> int:
    """Graceful self-removal: drain → wait for idle → remove.

    Scan all known pools for an endpoint matching this host's IP. For the
    matching ep: drain via Reconciler, wait for vllm to drain in-flight
    requests (timeout via LB_DETACH_WAIT env var), then remove via Reconciler.
    The drain-wait poll loop is an application-level concern and stays here,
    not absorbed by Reconciler.
    """
    self_ip = detect_self_ip()
    pool_names = BackendState.list_pools(bs.state_dir, bs.lb_host)
    if not pool_names:
        pool_names = [p.name for p in mgr.lb.pools]

    for pname in pool_names:
        pbs = BackendState(bs.state_dir, bs.lb_host, pool=pname)
        matching = [ep for ep in pbs.list() if ep.startswith(f"{self_ip}:")]
        if not matching:
            continue
        ep = matching[0]
        try:
            Reconciler(mgr).want_draining(ep, pname)
        except ReconcilerError as exc:
            print(f"detach drain {ep} failed: {exc}", file=sys.stderr)
            return _exit_for(exc)
        timeout = float(os.environ.get("LB_DETACH_WAIT", "30"))
        deadline = time.monotonic() + timeout
        port = int(ep.rsplit(":", 1)[1])
        while time.monotonic() < deadline:
            probe = probe_local_vllm(port)
            if probe.get("num_requests_running", 0.0) <= 0.0:
                break
            time.sleep(1)
        try:
            outcome = Reconciler(mgr).want_absent(ep, pname)
            print(f"detach {ep} {outcome.action.name} (pool: {pname})", file=sys.stderr)
            return 0
        except ReconcilerError as exc:
            print(f"detach remove {ep} failed: {exc}", file=sys.stderr)
            return _exit_for(exc)
    return 0
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /mnt/umm/users/qianjianheng/workspace/vctl && python -m pytest tests/test_commands_lb_scaling.py -k "detach" -v 2>&1 | tail -15
```

Expected: PASS — all detach tests pass

- [ ] **Step 5: Commit**

```bash
git add src/vctl/commands/lb_scaling.py tests/test_commands_lb_scaling.py
git commit -m "$(cat <<'EOF'
feat: migrate _do_detach to Reconciler chain — drain+wait+remove (Phase 2 T7)

Replaces direct _client + contextlib.suppress(drain) + _do_remove delegation
with explicit Reconciler.want_draining → probe loop → Reconciler.want_absent
chain. LB-down at either phase returns exit 4. Surfaces REMOVED in stderr.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Checkpoint 4 — detach migrated, all six verbs done

Run the full test suite and confirm all verbs pass before version bump:

```bash
cd /mnt/umm/users/qianjianheng/workspace/vctl && python -m pytest tests/test_commands_lb_scaling.py -v 2>&1 | tail -30
```

Expected: all tests pass — no regressions.

```bash
cd /mnt/umm/users/qianjianheng/workspace/vctl && python -m mypy --strict src/vctl/commands/lb_scaling.py 2>&1 | tail -10
```

Expected: `Success: no issues found`

Full F11 + F12 audit:

```bash
grep -n "bs\.add\|bs\.remove\|contextlib\.suppress" /mnt/umm/users/qianjianheng/workspace/vctl/src/vctl/commands/lb_scaling.py
```

Expected: zero matches — all state mutations routed through Reconciler; no suppress calls remaining in migrated verb bodies.

---

### Task 8: Version bump v0.3.0 + CHANGELOG + BACKLOG F11/F12 close

**Files:**
- Modify: `pyproject.toml` — version `"0.2.13"` → `"0.3.0"`
- Modify: `src/vctl/__init__.py` — `__version__ = "0.3.0"`
- Modify: `CHANGELOG.md` — add `## [0.3.0] - 2026-05-03` section
- Modify: `BACKLOG.md` — mark F11 and F12 as done (`[ ]` → `[x]`)

- [ ] **Step 1: Write the failing test**

The version test is a read-only assertion. Add it to `tests/test_commands_lb_scaling.py`:

```python
# ---------------------------------------------------------------------------
# Task 8: version bump verification
# ---------------------------------------------------------------------------


def test_version_is_0_3_0() -> None:
    """AT-12: pyproject.toml and __init__.py must both report 0.3.0."""
    import vctl
    assert vctl.__version__ == "0.3.0"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /mnt/umm/users/qianjianheng/workspace/vctl && python -m pytest tests/test_commands_lb_scaling.py::test_version_is_0_3_0 -v 2>&1 | tail -10
```

Expected: FAIL with `AssertionError: assert '0.2.13' == '0.3.0'`

- [ ] **Step 3: Implement**

Update `pyproject.toml` — change the version field:

```toml
version = "0.3.0"
```

Update `src/vctl/__init__.py` — change the version string:

```python
__version__ = "0.3.0"
```

Prepend the following section to `CHANGELOG.md` (after the first two header lines, before the `## [0.2.13]` entry):

```markdown
## [0.3.0] - 2026-05-03

### Changed (BREAKING)

- **`vctl lb add` against a stopped LB now exits 4** (was: exit 0 with silent
  state-file write). Operators who used `lb add` for offline pre-population
  (LB stopped) must run `lb start` first, then `lb add`.
- **`vctl lb auto-add` no longer silently suppresses haproxy failures** (was:
  `contextlib.suppress` wrapped every `add_server` / `set_state` call, always
  returning exit 0 even when backends were not registered). After v0.3.0, any
  `ReconcilerError` for a pool is surfaced on stderr and causes exit 1.
- **stderr output format change.** `(new)` / `(already present)` strings
  replaced by `Outcome.action.name` values: `ADDED`, `READIED`, `ADOPTED`,
  `REMOVED`, `ORPHANED_CLEANED`, `DRAINED`.

### Fixed

- **F11 closed:** `lb add` no longer writes the state file before haproxy acks.
  `Reconciler.want_present` enforces haproxy-ack-first ordering atomically —
  if haproxy refuses, the state file is never touched.
- **F12 closed:** `lb auto-add` no longer uses `contextlib.suppress` around
  haproxy admin calls. Per-pool failures are surfaced on stderr and accumulated;
  exit 1 if any pool failed (exit 0 only when all pools reconcile successfully).

### Internal

- Phase 2 migration: all six scaling verbs (`_do_add`, `_do_remove`, `_do_drain`,
  `_do_auto_add`, `_do_remove_cli`, `_do_detach`) in `lb_scaling.py` now
  delegate state mutations to `Reconciler` (Phase 1). Shared `_exit_for` helper
  centralises exit-code policy.
```

Update `BACKLOG.md` — mark F11 and F12 as closed:

Change:
```
- [ ] F11: `_do_add` mutates state file BEFORE admin-socket add_server (line ~162).
```
to:
```
- [x] F11: `_do_add` mutates state file BEFORE admin-socket add_server (line ~162).
```

Change:
```
- [ ] F12: `_do_auto_add` wraps both `cli.add_server` and `cli.set_state` in
```
to:
```
- [x] F12: `_do_auto_add` wraps both `cli.add_server` and `cli.set_state` in
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /mnt/umm/users/qianjianheng/workspace/vctl && python -m pytest tests/test_commands_lb_scaling.py::test_version_is_0_3_0 -v 2>&1 | tail -10
```

Expected: PASS — 1 passed

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/vctl/__init__.py CHANGELOG.md BACKLOG.md tests/test_commands_lb_scaling.py
git commit -m "$(cat <<'EOF'
chore: bump to v0.3.0 — CHANGELOG, BACKLOG F11/F12 closed (Phase 2 T8)

Documents breaking exit-code changes (lb add/auto-add), F11/F12 closure,
and Phase 2 migration in CHANGELOG. Marks F11/F12 done in BACKLOG.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Final verification gate

**Files:**
- No code changes — verification only.
- Update: `docs/super-agent-skills/specs/2026-05-02-reconciler-phase2-design.md` — mark all 12 acceptance test checkboxes `[ ]` → `[x]`

- [ ] **Step 1: Run ruff lint + format check**

```bash
cd /mnt/umm/users/qianjianheng/workspace/vctl && python -m ruff check src/ tests/ 2>&1 | tail -10
```

Expected: `All checks passed.` (or zero errors)

```bash
cd /mnt/umm/users/qianjianheng/workspace/vctl && python -m ruff format --check src/ tests/ 2>&1 | tail -10
```

Expected: `All checks passed.` (or `N files would be reformatted` — if any: run `ruff format src/ tests/` then re-check)

- [ ] **Step 2: Run mypy --strict** (AT-11 — part 1)

```bash
cd /mnt/umm/users/qianjianheng/workspace/vctl && python -m mypy --strict src/vctl 2>&1 | tail -15
```

Expected: `Success: no issues found`

- [ ] **Step 3: Run full pytest suite with coverage** (AT-11 — part 2)

```bash
cd /mnt/umm/users/qianjianheng/workspace/vctl && python -m pytest -q --cov=vctl --cov-fail-under=50 2>&1 | tail -20
```

Expected: 406+ tests collected, all passing; coverage ≥ 50%; at least 6 new exit-4 tests present (one per migrated verb: `_do_add`, `_do_remove`, `_do_drain` (existing A4), `_do_auto_add`, `_do_remove_cli`, `_do_detach`).

- [ ] **Step 4: Run integration tests (skip-tolerant)**

```bash
cd /mnt/umm/users/qianjianheng/workspace/vctl && python -m pytest -m integration -v 2>&1 | tail -20 || true
```

Expected: skipped or passed — no unexpected failures. Integration tests require haproxy on PATH; if haproxy is absent all integration tests are skip-marked and this step exits 0.

- [ ] **Step 5: Code audit — F11 closed**

```bash
grep -n "bs\.add\|bs\.remove" /mnt/umm/users/qianjianheng/workspace/vctl/src/vctl/commands/lb_scaling.py
```

Expected: zero matches — acceptance test AT-9 satisfied. Every state mutation goes through Reconciler; `bs.add` / `bs.remove` calls exist only in `reconciler.py`.

- [ ] **Step 6: Code audit — F12 closed**

```bash
grep -n "contextlib\.suppress" /mnt/umm/users/qianjianheng/workspace/vctl/src/vctl/commands/lb_scaling.py
```

Expected: zero matches in the bodies of `_do_auto_add` and `_do_add` — acceptance test AT-10 satisfied.

- [ ] **Step 7: Verify version in all three locations**

```bash
grep "^version" /mnt/umm/users/qianjianheng/workspace/vctl/pyproject.toml
grep "__version__" /mnt/umm/users/qianjianheng/workspace/vctl/src/vctl/__init__.py
grep "\[0.3.0\]" /mnt/umm/users/qianjianheng/workspace/vctl/CHANGELOG.md
```

Expected:
```
version = "0.3.0"
__version__ = "0.3.0"
## [0.3.0] - 2026-05-03
```

Acceptance test AT-12 satisfied.

- [ ] **Step 8: Mark acceptance test checkboxes in spec**

Update `docs/super-agent-skills/specs/2026-05-02-reconciler-phase2-design.md` — change all 12 `- [ ]` acceptance test entries to `- [x]`. The 12 items start at the `## Acceptance Tests` section heading and each begins with `- [ ] \`test:`.

- [ ] **Step 9: Commit**

```bash
git add docs/super-agent-skills/specs/2026-05-02-reconciler-phase2-design.md
git commit -m "$(cat <<'EOF'
chore: mark all 12 Phase 2 acceptance tests as passed in spec (Phase 2 T9)

All gates green: ruff, mypy --strict, pytest 406+, coverage ≥50%,
F11/F12 code audits, version 0.3.0 verified in all three locations.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Checkpoint 5 — final gate complete

Confirm all tests still pass after the spec update commit:

```bash
cd /mnt/umm/users/qianjianheng/workspace/vctl && python -m pytest -q 2>&1 | tail -10
```

Expected: all tests pass — Phase 2 migration complete and verified.
