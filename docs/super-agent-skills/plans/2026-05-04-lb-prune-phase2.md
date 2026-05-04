# vctl Phase 2 — `lb prune` (worker reaper) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use super-agent-skills:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `vctl lb prune` (manual cmd) and bundle an auto-watcher into `vctl lb start/stop/status` so dead-for-too-long backends get auto-removed from HAProxy + state file. Closes the pod-crash recovery gap. There is no separate `vctl lb watch` sub-command group.

**Architecture:** New `src/vctl/duration.py` (stdlib-only `_parse_duration`). New `LbPrune` pydantic class on `LbHaproxy` (with `enabled: bool = True` field). New `src/vctl/lb/prune.py` with `_collect_prune_candidates`, `_spawn_watcher`, `_stop_watcher`, and `_watcher_status`. Pruning calls existing `Reconciler.want_absent` — no new haproxy admin path. Watcher is a `bash while true; do <vctl lb prune>; sleep N; done` loop in tmux session `vctl-lb-watch` with a sentinel pidfile. Watcher lifecycle is hooked into `_do_start`/`_do_stop`/`_do_status` in `commands/lb.py`.

**Tech Stack:** Python 3.10+, pydantic v2, existing Reconciler / RuntimeClient / LbManager / BackendState / tmux helpers. No new runtime deps.

---

## File Map

| File | Status | Purpose |
|---|---|---|
| `src/vctl/duration.py` | NEW | `_parse_duration("5m")` → 300; stdlib only |
| `src/vctl/config/models.py` | MODIFY | New `LbPrune` class (with `enabled` field); `LbHaproxy.prune` field |
| `src/vctl/lb/prune.py` | NEW | `_collect_prune_candidates`, `_spawn_watcher`, `_stop_watcher`, `_watcher_status` |
| `src/vctl/commands/lb.py` | MODIFY | New `prune` verb; watcher hooks in `_do_start`/`_do_stop`/`_do_status` |
| `tests/test_duration.py` | NEW | parser unit tests |
| `tests/test_commands_lb_prune.py` | NEW | prune verb + dispatch + flag rejection |
| `tests/test_commands_lb.py` | MODIFY | Extend with watcher integration tests |
| `tests/test_lb_prune_candidates.py` | NEW | _collect_prune_candidates filtering |
| `pyproject.toml` | MODIFY | version 0.5.6 → 0.6.0 |
| `src/vctl/__init__.py` | MODIFY | __version__ → 0.6.0 |
| `tests/test_smoke.py` | MODIFY | version assert |
| `docs/CHANGELOG.md` | MODIFY | 0.6.0 entry |

## Task Ordering (7 tasks, single stream — subagent-driven sequential)

---

### Task 1: `_parse_duration` helper (`src/vctl/duration.py`)

**Goal:** Create a pure-stdlib helper that converts human-readable duration strings like `"5m"`, `"2h"`, `"1d"`, `"300s"` into integer seconds. This is used both by the pydantic field validator (at config-load time) and by the CLI flags at runtime, so it must live in a module with no vctl-internal imports (to prevent circular imports when `models.py` imports from `vctl.duration`).

#### Step 1 — Write the failing test

- [ ] CREATE `tests/test_duration.py` with 8 test cases:

```python
"""Unit tests for vctl.duration._parse_duration."""

from __future__ import annotations

import pytest

from vctl.duration import _parse_duration


# ---- valid inputs ----

def test_parse_seconds() -> None:
    assert _parse_duration("300s") == 300


def test_parse_minutes() -> None:
    assert _parse_duration("5m") == 300


def test_parse_hours() -> None:
    assert _parse_duration("2h") == 7200


def test_parse_days() -> None:
    assert _parse_duration("1d") == 86400


def test_parse_one_second() -> None:
    assert _parse_duration("1s") == 1


# ---- invalid inputs ----

def test_parse_empty_raises() -> None:
    with pytest.raises(ValueError, match="invalid duration"):
        _parse_duration("")


def test_parse_unknown_suffix_raises() -> None:
    with pytest.raises(ValueError, match="invalid duration"):
        _parse_duration("5x")


def test_parse_float_suffix_raises() -> None:
    with pytest.raises(ValueError, match="invalid duration"):
        _parse_duration("1.5m")


def test_parse_plain_digits_raises() -> None:
    with pytest.raises(ValueError, match="invalid duration"):
        _parse_duration("abc")
```

#### Step 2 — Run test; confirm failure

- [ ] Run `.venv/bin/pytest tests/test_duration.py -q` — all 9 tests should fail with `ModuleNotFoundError` (module does not exist yet).

#### Step 3 — Implement

- [ ] CREATE `src/vctl/duration.py`:

```python
"""Duration string parser — pure stdlib, no vctl imports.

Used by vctl.config.models (field validator) and CLI flag handlers.
Kept standalone to avoid circular imports at config-load time.
"""

from __future__ import annotations

import re

_DURATION_RE = re.compile(r"^\d+[smhd]$")

_SUFFIX_MULTIPLIERS: dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
}


def _parse_duration(s: str) -> int:
    """Parse '300s', '5m', '2h', '1d' → integer seconds.

    Raises ValueError on unrecognised format.
    Accepted suffixes: s (seconds), m (minutes), h (hours), d (days).
    Input must match ^\\d+[smhd]$; anything else raises ValueError.
    """
    if not _DURATION_RE.match(s):
        raise ValueError(f"invalid duration: {s!r}; expected format like '300s', '5m', '2h', '1d'")
    value = int(s[:-1])
    suffix = s[-1]
    return value * _SUFFIX_MULTIPLIERS[suffix]
```

#### Step 4 — Run test; confirm all pass

- [ ] Run `.venv/bin/pytest tests/test_duration.py -q` — all 9 tests should pass.

#### Step 5 — Lint + type-check + commit

- [ ] `.venv/bin/ruff check src/vctl/duration.py tests/test_duration.py`
- [ ] `.venv/bin/ruff format --check src/vctl/duration.py tests/test_duration.py`
- [ ] `.venv/bin/mypy --strict src/vctl/duration.py`
- [ ] `git add src/vctl/duration.py tests/test_duration.py`
- [ ] `git commit -m "feat: add _parse_duration helper (Task 1)"`

---

### Task 2: `LbPrune` pydantic class (`src/vctl/config/models.py`)

**Goal:** Add a new `LbPrune` schema class to `config/models.py` and attach it as an optional field on `LbHaproxy`. The class gains a new `enabled: bool = True` field (set `false` to disable the auto-watcher). The field validator inside `LbPrune` calls `_parse_duration` lazily (inside the method body) to avoid a circular import — `models.py` is loaded early in the import chain. All existing cluster.yaml files that omit `prune:` should get defaults automatically (`enabled: True`, `threshold: "5m"`, `watch_interval: "30s"`).

#### Step 1 — Write the failing tests

- [ ] CREATE `tests/test_lb_prune_config.py`:

```python
"""Schema tests for the LbPrune pydantic class and LbHaproxy.prune field."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from vctl.config.models import LbAdmin, LbDefaults, LbHaproxy, LbHealth, LbPrune, LbStats, Pool


def _single_pool_lb(**overrides: object) -> LbHaproxy:
    """Build a minimal valid LbHaproxy for testing."""
    kwargs: dict[str, object] = dict(
        host="10.0.0.1",
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        health=LbHealth(),
        defaults=LbDefaults(),
        pools=[Pool(name="default", served_model="*", bind_port=8080)],
    )
    kwargs.update(overrides)
    return LbHaproxy(**kwargs)  # type: ignore[arg-type]


# ---- LbPrune defaults ----

def test_lb_prune_default_enabled() -> None:
    """LbPrune() with no args should produce enabled=True."""
    p = LbPrune()
    assert p.enabled is True


def test_lb_prune_default_threshold() -> None:
    """LbPrune() with no args should produce threshold='5m'."""
    p = LbPrune()
    assert p.threshold == "5m"


def test_lb_prune_default_watch_interval() -> None:
    """LbPrune() with no args should produce watch_interval='30s'."""
    p = LbPrune()
    assert p.watch_interval == "30s"


def test_lb_haproxy_prune_field_defaults() -> None:
    """LbHaproxy.prune should be populated with defaults when 'prune:' block omitted."""
    lb = _single_pool_lb()
    assert lb.prune.enabled is True
    assert lb.prune.threshold == "5m"
    assert lb.prune.watch_interval == "30s"


def test_lb_prune_enabled_false() -> None:
    """LbPrune(enabled=False) should be accepted and preserved."""
    p = LbPrune(enabled=False)
    assert p.enabled is False


# ---- custom values ----

def test_lb_prune_custom_threshold() -> None:
    """threshold='10m' should be accepted and preserved."""
    p = LbPrune(threshold="10m")
    assert p.threshold == "10m"


def test_lb_prune_custom_watch_interval() -> None:
    """watch_interval='2m' should be accepted and preserved."""
    p = LbPrune(watch_interval="2m")
    assert p.watch_interval == "2m"


def test_lb_haproxy_with_custom_prune_block() -> None:
    """LbHaproxy with explicit prune block should carry the custom values."""
    lb = _single_pool_lb(prune=LbPrune(threshold="10m", watch_interval="60s"))
    assert lb.prune.threshold == "10m"
    assert lb.prune.watch_interval == "60s"


# ---- invalid values ----

def test_lb_prune_invalid_threshold_raises() -> None:
    """threshold='bad' must raise ValidationError."""
    with pytest.raises(ValidationError, match="invalid duration"):
        LbPrune(threshold="bad")


def test_lb_prune_invalid_watch_interval_raises() -> None:
    """watch_interval='5x' must raise ValidationError."""
    with pytest.raises(ValidationError, match="invalid duration"):
        LbPrune(watch_interval="5x")
```

#### Step 2 — Run test; confirm failure

- [ ] Run `.venv/bin/pytest tests/test_lb_prune_config.py -q` — all 11 tests fail with `ImportError: cannot import name 'LbPrune'`.

#### Step 3 — Implement

- [ ] MODIFY `src/vctl/config/models.py` — add the `LbPrune` class just before the `LbHaproxy` class, and add the `prune` field to `LbHaproxy`:

Add this class before `LbHaproxy`:

```python
class LbPrune(_Strict):
    """Configuration for `vctl lb prune` and the auto-watcher bundled in `lb start`."""

    enabled: bool = True           # set false to disable auto-watcher on lb start
    threshold: str = "5m"         # minimum DOWN duration before backend is prunable
    watch_interval: str = "30s"   # polling interval for the auto-watcher loop

    @field_validator("threshold", "watch_interval", mode="after")
    @classmethod
    def _valid_duration(cls, v: str) -> str:
        from vctl.duration import _parse_duration  # lazy — avoids circular import at module load

        _parse_duration(v)  # raises ValueError on bad input; pydantic converts to ValidationError
        return v
```

Add one new field to `LbHaproxy` (after `defaults`):

```python
prune: LbPrune = Field(default_factory=LbPrune)
```

#### Step 4 — Run test; confirm all pass

- [ ] Run `.venv/bin/pytest tests/test_lb_prune_config.py -q` — all 11 tests pass.
- [ ] Run `.venv/bin/pytest -q` (full suite) — no regressions. If existing tests use `LbHaproxy` directly and have `extra="forbid"` surprises, check `_synthesize_or_validate_pools` — the new field has a default so it is invisible to callers that don't set it.

#### Step 5 — Lint + type-check + commit

- [ ] `.venv/bin/ruff check src/vctl/config/models.py`
- [ ] `.venv/bin/ruff format --check src/vctl/config/models.py`
- [ ] `.venv/bin/mypy --strict src/vctl/config/models.py`
- [ ] `git add src/vctl/config/models.py tests/test_lb_prune_config.py`
- [ ] `git commit -m "feat: add LbPrune schema class and LbHaproxy.prune field (Task 2)"`

**CHECKPOINT after Task 2:** Schema committed; existing tests still green (no behavior change yet). `LbPrune` class is importable. The `prune:` block in cluster.yaml is fully backward-compatible — all omitting files still parse fine.

---

### Task 3: `_collect_prune_candidates` (`src/vctl/lb/prune.py`)

**Goal:** Create the core filtering logic that determines which backends in a pool are eligible for pruning. A backend is eligible when ALL of: (1) HAProxy `show stat` reports `status` starting with `"DOWN"`, (2) `show servers state` bitmask shows `admin == "ready"` (not MAINT, not DRAIN), and (3) `lastchg >= threshold_s`. The function opens two fresh `RuntimeClient` instances — one for each admin command — because the HAProxy socket closes after each response.

Key imports in `src/vctl/lb/prune.py`:
- `from vctl.duration import _parse_duration` — for the module-level `_parse_duration` re-export (callers import it from here)
- `from vctl.commands.lb import _fetch_haproxy_stats` — patched at `vctl.lb.prune._fetch_haproxy_stats` in tests
- `from vctl.lb.runtime import lb_admin_client` — patched at `vctl.lb.prune.lb_admin_client` in tests
- `from vctl.lb.errors import LbUnreachable`
- `from vctl.lb.manager import LbManager`

#### Step 1 — Write the failing tests

- [ ] CREATE `tests/test_lb_prune_candidates.py`:

```python
"""Unit tests for _collect_prune_candidates filtering logic."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vctl.config.models import LbAdmin, LbDefaults, LbHaproxy, LbHealth, LbStats, Pool
from vctl.lb.errors import LbUnreachable
from vctl.lb.manager import LbManager
from vctl.lb.runtime import BackendStatus


def _make_mgr(tmp_path: Path) -> LbManager:
    lb = LbHaproxy(
        host="10.0.0.1",
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        health=LbHealth(),
        defaults=LbDefaults(),
        pools=[Pool(name="default", served_model="*", bind_port=8080)],
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    return LbManager(lb, state_dir=tmp_path / "state", run_dir=run_dir)


def _fake_row(
    name: str = "b_10_0_0_1_8000",
    endpoint: str = "10.0.0.1:8000",
    op_state: int = 0,
    admin_state: int = 0,
    backend: str = "pool_default",
) -> BackendStatus:
    return BackendStatus(
        name=name,
        endpoint=endpoint,
        op_state=op_state,
        admin_state=admin_state,
        backend=backend,
    )


def _fake_stats(
    pool_section: str,
    server_name: str,
    status: str = "DOWN",
    lastchg: int = 400,
) -> dict[str, dict[str, dict[str, int | str]]]:
    """Build the dict[backend_section, dict[server_name, dict[field]]] shape."""
    return {
        pool_section: {
            server_name: {
                "status": status,
                "lastchg": lastchg,
                "scur": 0,
                "qcur": 0,
                "ep": "10.0.0.1:8000",
            }
        }
    }


# ---- eligible candidates ----

def test_collects_eligible_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DOWN + admin=ready + lastchg=400 + threshold=300 → returned."""
    import vctl.lb.prune as prune_mod

    mgr = _make_mgr(tmp_path)
    row = _fake_row(
        name="b_10_0_0_1_8000",
        endpoint="10.0.0.1:8000",
        admin_state=0,  # ready
        backend="pool_default",
    )
    stats = _fake_stats("pool_default", "b_10_0_0_1_8000", status="DOWN", lastchg=400)

    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = [row]
    monkeypatch.setattr(prune_mod, "lb_admin_client", lambda m: mock_client)
    monkeypatch.setattr(prune_mod, "_fetch_haproxy_stats", lambda c: stats)

    from vctl.lb.prune import _collect_prune_candidates

    result = _collect_prune_candidates(mgr, "default", threshold_s=300)
    assert result == [("10.0.0.1:8000", 400)]


# ---- exclusions ----

def test_skips_maint_backends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DOWN + admin_state=1 (maint) + lastchg=400 → NOT returned."""
    import vctl.lb.prune as prune_mod

    mgr = _make_mgr(tmp_path)
    row = _fake_row(
        name="b_10_0_0_1_8000",
        endpoint="10.0.0.1:8000",
        admin_state=1,  # maint bit set (LB_ADMIN_MAINT_MASK = 0x07)
        backend="pool_default",
    )
    stats = _fake_stats("pool_default", "b_10_0_0_1_8000", status="DOWN", lastchg=400)

    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = [row]
    monkeypatch.setattr(prune_mod, "lb_admin_client", lambda m: mock_client)
    monkeypatch.setattr(prune_mod, "_fetch_haproxy_stats", lambda c: stats)

    from vctl.lb.prune import _collect_prune_candidates

    result = _collect_prune_candidates(mgr, "default", threshold_s=300)
    assert result == []


def test_skips_drain_backends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DOWN + admin_state=0x38 (drain) + lastchg=400 → NOT returned."""
    import vctl.lb.prune as prune_mod

    mgr = _make_mgr(tmp_path)
    row = _fake_row(
        name="b_10_0_0_1_8000",
        endpoint="10.0.0.1:8000",
        admin_state=0x38,  # drain bit set (LB_ADMIN_DRAIN_MASK = 0x38)
        backend="pool_default",
    )
    stats = _fake_stats("pool_default", "b_10_0_0_1_8000", status="DOWN", lastchg=400)

    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = [row]
    monkeypatch.setattr(prune_mod, "lb_admin_client", lambda m: mock_client)
    monkeypatch.setattr(prune_mod, "_fetch_haproxy_stats", lambda c: stats)

    from vctl.lb.prune import _collect_prune_candidates

    result = _collect_prune_candidates(mgr, "default", threshold_s=300)
    assert result == []


def test_skips_up_backends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UP + lastchg=400 → NOT returned regardless of threshold."""
    import vctl.lb.prune as prune_mod

    mgr = _make_mgr(tmp_path)
    row = _fake_row(
        name="b_10_0_0_1_8000",
        endpoint="10.0.0.1:8000",
        admin_state=0,  # ready
        backend="pool_default",
    )
    stats = _fake_stats("pool_default", "b_10_0_0_1_8000", status="UP", lastchg=400)

    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = [row]
    monkeypatch.setattr(prune_mod, "lb_admin_client", lambda m: mock_client)
    monkeypatch.setattr(prune_mod, "_fetch_haproxy_stats", lambda c: stats)

    from vctl.lb.prune import _collect_prune_candidates

    result = _collect_prune_candidates(mgr, "default", threshold_s=300)
    assert result == []


def test_skips_below_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DOWN + admin=ready + lastchg=200 + threshold=300 → NOT returned (200 < 300)."""
    import vctl.lb.prune as prune_mod

    mgr = _make_mgr(tmp_path)
    row = _fake_row(
        name="b_10_0_0_1_8000",
        endpoint="10.0.0.1:8000",
        admin_state=0,
        backend="pool_default",
    )
    stats = _fake_stats("pool_default", "b_10_0_0_1_8000", status="DOWN", lastchg=200)

    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = [row]
    monkeypatch.setattr(prune_mod, "lb_admin_client", lambda m: mock_client)
    monkeypatch.setattr(prune_mod, "_fetch_haproxy_stats", lambda c: stats)

    from vctl.lb.prune import _collect_prune_candidates

    result = _collect_prune_candidates(mgr, "default", threshold_s=300)
    assert result == []


def test_skips_down_at_exact_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DOWN + lastchg==threshold → returned (>= boundary is inclusive)."""
    import vctl.lb.prune as prune_mod

    mgr = _make_mgr(tmp_path)
    row = _fake_row(admin_state=0, backend="pool_default")
    stats = _fake_stats("pool_default", "b_10_0_0_1_8000", status="DOWN", lastchg=300)

    mock_client = MagicMock()
    mock_client.show_servers_state.return_value = [row]
    monkeypatch.setattr(prune_mod, "lb_admin_client", lambda m: mock_client)
    monkeypatch.setattr(prune_mod, "_fetch_haproxy_stats", lambda c: stats)

    from vctl.lb.prune import _collect_prune_candidates

    result = _collect_prune_candidates(mgr, "default", threshold_s=300)
    assert len(result) == 1


# ---- LbUnreachable ----

def test_lb_unreachable_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`lb_admin_client` returns None → raises LbUnreachable."""
    import vctl.lb.prune as prune_mod

    mgr = _make_mgr(tmp_path)
    monkeypatch.setattr(prune_mod, "lb_admin_client", lambda m: None)

    from vctl.lb.prune import _collect_prune_candidates

    with pytest.raises(LbUnreachable):
        _collect_prune_candidates(mgr, "default", threshold_s=300)
```

#### Step 2 — Run test; confirm failure

- [ ] Run `.venv/bin/pytest tests/test_lb_prune_candidates.py -q` — all tests fail with `ModuleNotFoundError: No module named 'vctl.lb.prune'`.

#### Step 3 — Implement

- [ ] CREATE `src/vctl/lb/prune.py`:

```python
"""Prune candidates collector for `vctl lb prune`.

_collect_prune_candidates joins two HAProxy admin queries:
  1. `show stat` CSV (via _fetch_haproxy_stats) → status + lastchg per server
  2. `show servers state`                        → admin bitmask per server

A backend is eligible for pruning when ALL hold:
  - status starts with "DOWN"   (health-check-failed; not UP/MAINT/DRAIN)
  - admin == "ready"            (no MAINT or DRAIN bitmask bits set)
  - lastchg >= threshold_s      (has been DOWN for at least threshold_s seconds)

MAINT and DRAIN backends are never pruned regardless of how long they've been
down — the operator explicitly placed them there.

Both HAProxy admin calls MUST use fresh RuntimeClient instances (the socket
closes after each response in non-prompt mode — see CLAUDE.md gotcha).
We call lb_admin_client() twice rather than reusing one client.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# NOTE: _fetch_haproxy_stats is imported at module level so tests can monkeypatch
# at `vctl.lb.prune._fetch_haproxy_stats` rather than at the definition site.
from vctl.commands.lb import _fetch_haproxy_stats
from vctl.duration import _parse_duration  # re-exported for callers
from vctl.lb.errors import LbUnreachable
from vctl.lb.runtime import lb_admin_client

if TYPE_CHECKING:
    from vctl.lb.manager import LbManager

__all__ = ["_collect_prune_candidates", "_parse_duration"]


def _collect_prune_candidates(
    mgr: LbManager,
    pool_name: str,
    threshold_s: int,
) -> list[tuple[str, int]]:
    """Return [(ep, lastchg_s), ...] for backends eligible for pruning in one pool.

    A backend is eligible when ALL of:
      - status starts with "DOWN" (from show stat CSV)
      - admin == "ready"  (not MAINT, not DRAIN; from show servers state bitmask)
      - lastchg >= threshold_s

    Raises:
      LbUnreachable: if either admin command cannot reach the HAProxy socket.
    """
    pool_section = f"pool_{pool_name}"

    # Call 1: show stat CSV → status + lastchg per server in this pool section.
    cli1 = lb_admin_client(mgr)
    if cli1 is None:
        raise LbUnreachable(
            sock=str(mgr.sock_path),
            tcp=f"{mgr.lb.host}:{mgr.lb.admin.bind_port}",
        )
    stats_by_section = _fetch_haproxy_stats(cli1)
    pool_stats = stats_by_section.get(pool_section, {})

    # Call 2: show servers state → admin bitmask per server in this pool section.
    # Fresh socket — the previous connection is now closed (per-command contract).
    cli2 = lb_admin_client(mgr)
    if cli2 is None:
        raise LbUnreachable(
            sock=str(mgr.sock_path),
            tcp=f"{mgr.lb.host}:{mgr.lb.admin.bind_port}",
        )
    all_rows = cli2.show_servers_state()
    rows = [r for r in all_rows if r.backend == pool_section]

    # Join on server name; apply eligibility filter.
    candidates: list[tuple[str, int]] = []
    for row in rows:
        if row.admin != "ready":
            continue  # MAINT or DRAIN — never prune

        srv_data = pool_stats.get(row.name, {})
        status = str(srv_data.get("status", ""))
        if not status.startswith("DOWN"):
            continue  # UP, MAINT-in-stat, DRAIN-in-stat, "no check", etc.

        lastchg = int(srv_data.get("lastchg", 0))
        if lastchg >= threshold_s:
            candidates.append((row.endpoint, lastchg))

    candidates.sort()  # deterministic: sorted by ep string
    return candidates
```

#### Step 4 — Run test; confirm all pass

- [ ] Run `.venv/bin/pytest tests/test_lb_prune_candidates.py -q` — all 7 tests pass.

#### Step 5 — Lint + type-check + commit

- [ ] `.venv/bin/ruff check src/vctl/lb/prune.py tests/test_lb_prune_candidates.py`
- [ ] `.venv/bin/ruff format --check src/vctl/lb/prune.py tests/test_lb_prune_candidates.py`
- [ ] `.venv/bin/mypy --strict src/vctl/lb/prune.py`
- [ ] `git add src/vctl/lb/prune.py tests/test_lb_prune_candidates.py`
- [ ] `git commit -m "feat: add _collect_prune_candidates (Task 3)"`

**CHECKPOINT after Task 3:** `_collect_prune_candidates` is unit-tested in isolation. Mocks verify all filtering branches (MAINT skip, DRAIN skip, UP skip, below-threshold skip, LbUnreachable propagation). No commands wired yet.

---

### Task 4: `vctl lb prune` verb (`src/vctl/commands/lb.py`)

**Goal:** Wire the `prune` verb into the existing `lb.py` dispatch. Add `"prune"` to `_LB_VERB_HELP`, add a parser with `--threshold`, `--pool`, and `--dry-run` flags, implement `_do_prune`, and dispatch from `run()`. Follow the existing error-mapping convention: `LbUnreachable` → exit 4, `BackendOpFailed` → exit 1, bad threshold string → exit 1, unknown pool → exit 3.

The `_do_prune` function imports `_collect_prune_candidates` and `Reconciler` locally (inside the function) to keep the cold-import path light — consistent with `commands/lb.py`'s pattern of local imports in verb handlers.

#### Step 1 — Write the failing tests

- [ ] CREATE `tests/test_commands_lb_prune.py`:

```python
"""Tests for `vctl lb prune` verb dispatch and flag handling."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from vctl.config.models import LbAdmin, LbDefaults, LbHaproxy, LbHealth, LbStats, Pool
from vctl.lb.errors import BackendOpFailed, LbUnreachable
from vctl.lb.manager import LbManager
from vctl.lb.runtime import BackendStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_lb(pools: list[Pool] | None = None) -> LbHaproxy:
    if pools is None:
        pools = [Pool(name="default", served_model="*", bind_port=8080)]
    return LbHaproxy(
        host="10.0.0.1",
        admin=LbAdmin(bind_port=9001),
        stats=LbStats(bind_port=9000),
        health=LbHealth(),
        defaults=LbDefaults(),
        pools=pools,
    )


def _make_mgr(tmp_path: Path, lb: LbHaproxy | None = None) -> LbManager:
    if lb is None:
        lb = _make_lb()
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    return LbManager(lb, state_dir=tmp_path / "state", run_dir=run_dir)


# ---------------------------------------------------------------------------
# AT-1: prune removes an eligible DOWN backend
# ---------------------------------------------------------------------------


def test_prune_removes_eligible_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AT-1: DOWN ep at 400s with default threshold 300s → want_absent called once."""
    import vctl.lb.prune as prune_mod
    from vctl.commands.lb import _do_prune
    from vctl.lb.reconciler import Reconciler

    mgr = _make_mgr(tmp_path)

    # Patch _collect_prune_candidates to return one candidate.
    monkeypatch.setattr(
        prune_mod,
        "_collect_prune_candidates",
        lambda m, pool, threshold_s: [("10.0.0.5:8000", 400)],
    )

    want_absent_calls: list[tuple[str, str]] = []

    def fake_want_absent(self: Reconciler, ep: str, pool: str) -> MagicMock:
        want_absent_calls.append((ep, pool))
        m = MagicMock()
        m.action.name = "REMOVED"
        return m

    monkeypatch.setattr(Reconciler, "want_absent", fake_want_absent)

    import argparse

    ns = argparse.Namespace(pool=None, threshold=None, dry_run=False)
    rc = _do_prune(mgr, ns)

    assert rc == 0
    assert want_absent_calls == [("10.0.0.5:8000", "default")]


# ---------------------------------------------------------------------------
# AT-4: --dry-run does not call want_absent
# ---------------------------------------------------------------------------


def test_prune_dry_run_does_not_call_want_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AT-4: --dry-run prints would-prune message without calling want_absent."""
    import vctl.lb.prune as prune_mod
    from vctl.commands.lb import _do_prune
    from vctl.lb.reconciler import Reconciler

    mgr = _make_mgr(tmp_path)

    monkeypatch.setattr(
        prune_mod,
        "_collect_prune_candidates",
        lambda m, pool, threshold_s: [("10.0.0.5:8000", 400)],
    )

    def _should_not_be_called(self: Reconciler, ep: str, pool: str) -> None:
        raise AssertionError("want_absent must NOT be called in dry-run mode")

    monkeypatch.setattr(Reconciler, "want_absent", _should_not_be_called)

    import argparse

    ns = argparse.Namespace(pool=None, threshold=None, dry_run=True)
    rc = _do_prune(mgr, ns)

    assert rc == 0
    captured = capsys.readouterr()
    assert "would prune" in captured.err


# ---------------------------------------------------------------------------
# AT-2: --threshold flag overrides yaml default
# ---------------------------------------------------------------------------


def test_prune_threshold_flag_overrides_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AT-2: --threshold 1m; ep at 90s is pruned (90 >= 60); ep at 50s is not."""
    import vctl.lb.prune as prune_mod
    from vctl.commands.lb import _do_prune
    from vctl.lb.reconciler import Reconciler

    mgr = _make_mgr(tmp_path)

    # _collect_prune_candidates receives threshold_s=60 and returns filtered list.
    # We simulate: 90s ep passes, 50s ep does not.
    captured_thresholds: list[int] = []

    def fake_collect(m: LbManager, pool: str, threshold_s: int) -> list[tuple[str, int]]:
        captured_thresholds.append(threshold_s)
        # Return only ep that meets threshold (threshold_s=60 → 90s qualifies).
        return [("10.0.0.5:8000", 90)] if 90 >= threshold_s else []

    monkeypatch.setattr(prune_mod, "_collect_prune_candidates", fake_collect)

    want_absent_calls: list[str] = []

    def fake_want_absent(self: Reconciler, ep: str, pool: str) -> MagicMock:
        want_absent_calls.append(ep)
        m = MagicMock()
        m.action.name = "REMOVED"
        return m

    monkeypatch.setattr(Reconciler, "want_absent", fake_want_absent)

    import argparse

    ns = argparse.Namespace(pool=None, threshold="1m", dry_run=False)
    rc = _do_prune(mgr, ns)

    assert rc == 0
    assert captured_thresholds == [60]  # 1m → 60s passed to collector
    assert "10.0.0.5:8000" in want_absent_calls


# ---------------------------------------------------------------------------
# AT-3: unknown --pool returns 3
# ---------------------------------------------------------------------------


def test_prune_unknown_pool_returns_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AT-3: --pool nosuch → exit 3 + stderr message containing 'unknown pool'."""
    from vctl.commands.lb import _do_prune

    mgr = _make_mgr(tmp_path)

    import argparse

    ns = argparse.Namespace(pool="nosuch", threshold=None, dry_run=False)
    rc = _do_prune(mgr, ns)

    assert rc == 3
    captured = capsys.readouterr()
    assert "nosuch" in captured.err
    assert "unknown pool" in captured.err.lower() or "available" in captured.err.lower()


# ---------------------------------------------------------------------------
# LbUnreachable → exit 4
# ---------------------------------------------------------------------------


def test_prune_lb_unreachable_returns_4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LbUnreachable from _collect_prune_candidates → exit 4."""
    import vctl.lb.prune as prune_mod
    from vctl.commands.lb import _do_prune

    mgr = _make_mgr(tmp_path)

    def raise_unreachable(m: LbManager, pool: str, threshold_s: int) -> list[tuple[str, int]]:
        raise LbUnreachable(sock="/tmp/fake.sock", tcp="10.0.0.1:9001")

    monkeypatch.setattr(prune_mod, "_collect_prune_candidates", raise_unreachable)

    import argparse

    ns = argparse.Namespace(pool=None, threshold=None, dry_run=False)
    rc = _do_prune(mgr, ns)
    assert rc == 4


# ---------------------------------------------------------------------------
# Invalid --threshold → exit 1
# ---------------------------------------------------------------------------


def test_prune_invalid_threshold_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--threshold bad → exit 1 with error message."""
    from vctl.commands.lb import _do_prune

    mgr = _make_mgr(tmp_path)

    import argparse

    ns = argparse.Namespace(pool=None, threshold="bad", dry_run=False)
    rc = _do_prune(mgr, ns)

    assert rc == 1
    captured = capsys.readouterr()
    assert "invalid duration" in captured.err.lower() or "bad" in captured.err


# ---------------------------------------------------------------------------
# BackendOpFailed → exit 1
# ---------------------------------------------------------------------------


def test_prune_backend_op_failed_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BackendOpFailed during want_absent → exit 1."""
    import vctl.lb.prune as prune_mod
    from vctl.commands.lb import _do_prune
    from vctl.lb.reconciler import Reconciler

    mgr = _make_mgr(tmp_path)

    monkeypatch.setattr(
        prune_mod,
        "_collect_prune_candidates",
        lambda m, pool, threshold_s: [("10.0.0.5:8000", 400)],
    )

    def raise_op_failed(self: Reconciler, ep: str, pool: str) -> None:
        raise BackendOpFailed(op="remove_server", ep=ep, backend=f"pool_{pool}")

    monkeypatch.setattr(Reconciler, "want_absent", raise_op_failed)

    import argparse

    ns = argparse.Namespace(pool=None, threshold=None, dry_run=False)
    rc = _do_prune(mgr, ns)
    assert rc == 1
```

#### Step 2 — Run test; confirm failure

- [ ] Run `.venv/bin/pytest tests/test_commands_lb_prune.py -q` — all tests fail with `ImportError` or `AttributeError` because `_do_prune` does not exist yet.

#### Step 3 — Implement

- [ ] MODIFY `src/vctl/commands/lb.py` — make three changes:

**3a.** Add `"prune"` to `_LB_VERB_HELP`:

```python
"prune": "Remove health-check-failed (DOWN) backends past threshold",
```

**3b.** Add the `prune` parser to `_build_subparser()` just before the `return p` line:

```python
prune = sp.add_parser("prune", help=_LB_VERB_HELP["prune"])
prune.add_argument(
    "--pool",
    default=None,
    help="scope to one pool (default: all pools); exit 3 on unknown pool name",
)
prune.add_argument(
    "--threshold",
    default=None,
    metavar="DURATION",
    help=(
        "override dead threshold (e.g. 5m, 300s, 2h); "
        "default: cluster.lb.prune.threshold or '5m'"
    ),
)
prune.add_argument(
    "--dry-run",
    action="store_true",
    help="print candidates without removing; exit 0",
)
```

**3c.** Add the dispatch branch and implementation to `run()` (before the final `lb_scaling.dispatch` call) and add the `_do_prune` function at the bottom of the file:

In `run()`, after the `health` verb branch and before the final `lb_scaling.dispatch`:

```python
if verb == "prune":
    return _do_prune(mgr, parsed)
```

Then add `_do_prune` as a new function at the bottom of `lb.py`:

```python
def _do_prune(mgr: LbManager, parsed: argparse.Namespace) -> int:
    """Handle `vctl lb prune`.

    Threshold resolution order:
      1. --threshold flag (if given and valid)
      2. cluster.lb.prune.threshold YAML field
      3. Hardcoded default "5m"

    Pool list:
      - If --pool given: validate against configured pools first; exit 3 if unknown.
      - Otherwise: iterate all configured pools.

    Each candidate calls Reconciler.want_absent (haproxy-first ordering preserved).
    """
    from vctl.duration import _parse_duration
    from vctl.lb.errors import BackendOpFailed, LbUnreachable
    from vctl.lb.prune import _collect_prune_candidates
    from vctl.lb.reconciler import Reconciler

    # Step 1: Resolve threshold string.
    raw_threshold: str
    if parsed.threshold is not None:
        raw_threshold = parsed.threshold
    else:
        raw_threshold = mgr.lb.prune.threshold  # from YAML or default "5m"

    try:
        threshold_s = _parse_duration(raw_threshold)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # Step 2: Determine target pool list with pre-flight validation.
    pool_name_filter: str | None = getattr(parsed, "pool", None)
    if pool_name_filter is not None:
        configured = {p.name for p in mgr.lb.pools}
        if pool_name_filter not in configured:
            available = ", ".join(sorted(configured))
            print(
                f"unknown pool: {pool_name_filter!r}; available: {available}",
                file=sys.stderr,
            )
            return 3
        target_pool_names = [pool_name_filter]
    else:
        target_pool_names = [p.name for p in mgr.lb.pools]

    dry_run: bool = getattr(parsed, "dry_run", False)
    rec = Reconciler(mgr)

    # Step 3-7: Collect candidates and act.
    for pool_name in target_pool_names:
        try:
            candidates = _collect_prune_candidates(mgr, pool_name, threshold_s)
        except LbUnreachable as exc:
            print(f"lb prune: {exc}", file=sys.stderr)
            return 4

        for ep, lastchg_s in candidates:
            duration_str = _format_duration(lastchg_s)
            if dry_run:
                print(
                    f"would prune {ep} from pool {pool_name} (DOWN for {duration_str})",
                    file=sys.stderr,
                )
                continue
            try:
                rec.want_absent(ep, pool_name)
            except BackendOpFailed as exc:
                print(f"lb prune: {exc}", file=sys.stderr)
                return 1
            print(
                f"pruned {ep} from pool {pool_name} (DOWN for {duration_str})",
                file=sys.stderr,
            )

    return 0
```

#### Step 4 — Run test; confirm all pass

- [ ] Run `.venv/bin/pytest tests/test_commands_lb_prune.py -q` — all 7 tests pass.
- [ ] Run `.venv/bin/pytest -q` (full suite) — no regressions.

#### Step 5 — Lint + type-check + commit

- [ ] `.venv/bin/ruff check src/vctl/commands/lb.py tests/test_commands_lb_prune.py`
- [ ] `.venv/bin/ruff format --check src/vctl/commands/lb.py tests/test_commands_lb_prune.py`
- [ ] `.venv/bin/mypy --strict src/vctl/commands/lb.py`
- [ ] `git add src/vctl/commands/lb.py tests/test_commands_lb_prune.py`
- [ ] `git commit -m "feat: add vctl lb prune verb (Task 4)"`

**CHECKPOINT after Task 4:** `vctl lb prune` end-to-end works under unit-test mocks. All 7 prune tests pass. `_do_prune` dispatch is wired; `--dry-run`, `--threshold`, `--pool` flags all tested. MAINT/DRAIN/UP exclusions tested in Task 3 (collector level).

---

### Task 5: Integrate watcher spawn into `vctl lb start` (`src/vctl/lb/prune.py` + `src/vctl/commands/lb.py`)

**Goal:** Add `_spawn_watcher(mgr, prune_cfg, cluster_yaml_path)` to `src/vctl/lb/prune.py` and wire it into `_do_start` via a new helper `_spawn_watcher_if_enabled(mgr, cluster_yaml_path)` in `commands/lb.py`. When `prune.enabled: true` (default), `vctl lb start` spawns BOTH the HAProxy session (`vctl-lb`) AND the watcher session (`vctl-lb-watch`) and writes `~/.vctl/lb/watch.pid`. When `prune.enabled: false`, only the HAProxy session is started.

The pidfile contains `tmux:vctl-lb-watch\n` (NOT a numeric PID). Module-level aliases for tmux helpers are added to `commands/lb.py` so tests can monkeypatch them.

#### Step 1 — Write the failing tests

- [ ] APPEND to `tests/test_commands_lb.py` (existing file):

```python
# ---------------------------------------------------------------------------
# Task 5: watcher spawn integration into lb start
# ---------------------------------------------------------------------------


def test_lb_start_spawns_watcher_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AT-7: prune.enabled=True → tmux_run_detached_argv called with vctl-lb-watch, pidfile written."""
    import vctl.commands.lb as lb_mod
    from vctl.config.models import LbPrune

    lb = _make_lb(prune=LbPrune(enabled=True))
    mgr = _make_mgr(tmp_path, lb=lb)
    watch_pid = tmp_path / "run" / "watch.pid"

    spawned_sessions: list[str] = []

    def fake_tmux(name: str, argv: list[str]) -> None:
        spawned_sessions.append(name)

    monkeypatch.setattr(lb_mod, "_tmux_run_detached_argv", fake_tmux)
    monkeypatch.setattr(lb_mod, "_tmux_session_exists", lambda name: False)

    from vctl.lb.prune import _spawn_watcher
    monkeypatch.setattr("vctl.lb.prune.tmux_run_detached_argv", fake_tmux)

    from vctl.commands.lb import _spawn_watcher_if_enabled
    _spawn_watcher_if_enabled(mgr, Path("/tmp/cluster.yaml"))

    assert "vctl-lb-watch" in spawned_sessions
    assert watch_pid.exists()
    assert watch_pid.read_text().strip() == "tmux:vctl-lb-watch"


def test_lb_start_skips_watcher_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AT-7b: prune.enabled=False → no watcher session spawned, no pidfile written."""
    import vctl.commands.lb as lb_mod
    from vctl.config.models import LbPrune

    lb = _make_lb(prune=LbPrune(enabled=False))
    mgr = _make_mgr(tmp_path, lb=lb)
    watch_pid = tmp_path / "run" / "watch.pid"

    spawned_sessions: list[str] = []

    def fake_tmux(name: str, argv: list[str]) -> None:
        spawned_sessions.append(name)

    monkeypatch.setattr("vctl.lb.prune.tmux_run_detached_argv", fake_tmux)

    from vctl.commands.lb import _spawn_watcher_if_enabled
    _spawn_watcher_if_enabled(mgr, Path("/tmp/cluster.yaml"))

    assert "vctl-lb-watch" not in spawned_sessions
    assert not watch_pid.exists()
```

#### Step 2 — Run test; confirm failure

- [ ] Run `.venv/bin/pytest tests/test_commands_lb.py -k "watcher" -q` — both new tests fail because `_spawn_watcher_if_enabled` and `_spawn_watcher` do not exist yet.

#### Step 3 — Implement

**3a.** ADD to `src/vctl/lb/prune.py` (after `_collect_prune_candidates`):

```python
import shlex
import sys
from pathlib import Path

from vctl.lb.tmux import tmux_run_detached_argv

# TYPE_CHECKING import for LbPrune — avoids circular import
if TYPE_CHECKING:
    from vctl.config.models import LbPrune


def _spawn_watcher(
    mgr: "LbManager",
    prune_cfg: "LbPrune",
    cluster_yaml_path: Path,
) -> None:
    """Spawn the vctl-lb-watch tmux session and write the sentinel pidfile.

    Builds: while true; do python -m vctl --config <path> lb prune; sleep N; done
    Writes: mgr.run_dir / "watch.pid"  with content "tmux:vctl-lb-watch\\n"
    """
    interval_s = _parse_duration(prune_cfg.watch_interval)
    inner_argv: list[str] = [
        sys.executable, "-m", "vctl",
        "--config", str(cluster_yaml_path),
        "lb", "prune",
    ]
    loop_cmd = f"while true; do {shlex.join(inner_argv)}; sleep {interval_s}; done"
    tmux_run_detached_argv("vctl-lb-watch", ["bash", "-c", loop_cmd])

    pid_path = mgr.run_dir / "watch.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = pid_path.with_suffix(".tmp")
    tmp.write_text("tmux:vctl-lb-watch\n")
    tmp.replace(pid_path)
```

**3b.** ADD to `src/vctl/commands/lb.py` (module-level alias for testability):

```python
from vctl.lb.tmux import tmux_run_detached_argv as _tmux_run_detached_argv
from vctl.lb.tmux import tmux_kill as _tmux_kill
from vctl.lb.tmux import tmux_session_exists as _tmux_session_exists
```

**3c.** ADD helper function to `src/vctl/commands/lb.py`:

```python
def _spawn_watcher_if_enabled(mgr: LbManager, cluster_yaml_path: Path) -> None:
    """Spawn vctl-lb-watch watcher session after lb start, if prune.enabled is True."""
    from vctl.lb.prune import _spawn_watcher

    if not mgr.lb.prune.enabled:
        return
    if _tmux_session_exists("vctl-lb-watch"):
        print("watcher already running (session=vctl-lb-watch) — skipping spawn", file=sys.stderr)
        return
    _spawn_watcher(mgr, mgr.lb.prune, cluster_yaml_path)
    print("watcher started (session=vctl-lb-watch)", file=sys.stderr)
```

**3d.** MODIFY `_do_start` in `src/vctl/commands/lb.py` — call `_spawn_watcher_if_enabled` AFTER `mgr.start()` succeeds:

```python
# At the end of _do_start, after mgr.start() returns successfully:
cluster_yaml = Path(ns.config) if getattr(ns, "config", None) else Path.home() / ".vctl" / "cluster.yaml"
_spawn_watcher_if_enabled(mgr, cluster_yaml)
```

#### Step 4 — Run test; confirm all pass

- [ ] Run `.venv/bin/pytest tests/test_commands_lb.py -k "watcher" -q` — both Task 5 tests pass.
- [ ] Run `.venv/bin/pytest -q` (full suite) — no regressions.

#### Step 5 — Lint + type-check + commit

- [ ] `.venv/bin/ruff check src/vctl/lb/prune.py src/vctl/commands/lb.py tests/test_commands_lb.py`
- [ ] `.venv/bin/ruff format --check src/vctl/lb/prune.py src/vctl/commands/lb.py tests/test_commands_lb.py`
- [ ] `.venv/bin/mypy --strict src/vctl/lb/prune.py src/vctl/commands/lb.py`
- [ ] `git add src/vctl/lb/prune.py src/vctl/commands/lb.py tests/test_commands_lb.py`
- [ ] `git commit -m "feat: integrate watcher spawn into lb start (Task 5)"`

---

### Task 6: Integrate watcher kill into `vctl lb stop` + watcher state into `vctl lb status` (`src/vctl/lb/prune.py` + `src/vctl/commands/lb.py`)

**Goal:** Add `_stop_watcher(mgr)` and `_watcher_status(mgr)` to `src/vctl/lb/prune.py`. Wire `_stop_watcher` into `_do_stop` via helper `_stop_watcher_if_running(mgr)` in `commands/lb.py`. Wire `_watcher_status` into `_do_status` (or `_do_info`) so `vctl lb status` reports the watcher row. Extend `tests/test_commands_lb.py` with 4 more tests.

`_stop_watcher` is idempotent: killing a non-existent tmux session does not error. Removing a non-existent pidfile also does not error.

`_watcher_status` has four states:
- `enabled=False` → `"disabled"` (regardless of session liveness)
- session alive + pidfile ok → `"running"`
- session alive, no pidfile → `"running"` (session is truth, log warning)
- session absent → `"not running"`

#### Step 1 — Write the failing tests

- [ ] APPEND to `tests/test_commands_lb.py` (existing file):

```python
# ---------------------------------------------------------------------------
# Task 6: watcher kill integration into lb stop + state in lb status
# ---------------------------------------------------------------------------


def test_lb_stop_kills_watcher_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AT-8: lb stop calls _stop_watcher → tmux_kill("vctl-lb-watch") + pidfile removed."""
    import vctl.lb.prune as prune_mod

    lb = _make_lb()
    mgr = _make_mgr(tmp_path, lb=lb)
    watch_pid = mgr.run_dir / "watch.pid"
    watch_pid.parent.mkdir(parents=True, exist_ok=True)
    watch_pid.write_text("tmux:vctl-lb-watch\n")

    killed: list[str] = []
    monkeypatch.setattr("vctl.lb.tmux.tmux_kill", lambda name: killed.append(name))

    from vctl.lb.prune import _stop_watcher
    _stop_watcher(mgr)

    assert "vctl-lb-watch" in killed
    assert not watch_pid.exists()


def test_lb_stop_watcher_idempotent_when_not_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_stop_watcher when nothing running → no error, exit cleanly."""
    lb = _make_lb()
    mgr = _make_mgr(tmp_path, lb=lb)

    monkeypatch.setattr("vctl.lb.tmux.tmux_kill", lambda name: None)

    from vctl.lb.prune import _stop_watcher
    _stop_watcher(mgr)  # must not raise


def test_lb_status_reports_watcher_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AT-9: watcher session alive + pidfile present → state='running'."""
    from vctl.config.models import LbPrune

    lb = _make_lb(prune=LbPrune(enabled=True))
    mgr = _make_mgr(tmp_path, lb=lb)
    watch_pid = mgr.run_dir / "watch.pid"
    watch_pid.parent.mkdir(parents=True, exist_ok=True)
    watch_pid.write_text("tmux:vctl-lb-watch\n")

    monkeypatch.setattr("vctl.lb.tmux.tmux_session_exists", lambda name: True)

    from vctl.lb.prune import _watcher_status
    result = _watcher_status(mgr)

    assert result["state"] == "running"
    assert result["enabled"] is True


def test_lb_status_reports_watcher_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AT-9: prune.enabled=False → state='disabled' regardless of session."""
    from vctl.config.models import LbPrune

    lb = _make_lb(prune=LbPrune(enabled=False))
    mgr = _make_mgr(tmp_path, lb=lb)

    monkeypatch.setattr("vctl.lb.tmux.tmux_session_exists", lambda name: True)

    from vctl.lb.prune import _watcher_status
    result = _watcher_status(mgr)

    assert result["state"] == "disabled"
    assert result["enabled"] is False
```

#### Step 2 — Run test; confirm failure

- [ ] Run `.venv/bin/pytest tests/test_commands_lb.py -k "stop_watcher or status_reports_watcher" -q` — all 4 new tests fail because `_stop_watcher` and `_watcher_status` do not exist.

#### Step 3 — Implement

**3a.** ADD to `src/vctl/lb/prune.py`:

```python
from vctl.lb.tmux import tmux_kill, tmux_session_exists

# TYPE_CHECKING import for LbPrune is already present from Task 5


def _stop_watcher(mgr: "LbManager") -> None:
    """Kill the vctl-lb-watch tmux session and remove the sentinel pidfile.

    Idempotent: safe to call even if the watcher was never started.
    """
    tmux_kill("vctl-lb-watch")
    pid_path = mgr.run_dir / "watch.pid"
    pid_path.unlink(missing_ok=True)


def _watcher_status(mgr: "LbManager") -> dict[str, object]:
    """Return watcher liveness info for `vctl lb status` display.

    Returns dict with keys:
      - "enabled"       (bool) — mgr.lb.prune.enabled
      - "session_alive" (bool) — tmux_session_exists("vctl-lb-watch")
      - "pidfile_ok"    (bool) — pidfile present with correct sentinel
      - "state"         (str)  — "running" | "not running" | "disabled"
    """
    enabled: bool = mgr.lb.prune.enabled
    session_alive = tmux_session_exists("vctl-lb-watch")
    pid_path = mgr.run_dir / "watch.pid"
    pidfile_ok = False
    if pid_path.exists():
        try:
            content = pid_path.read_text().strip()
            pidfile_ok = content == "tmux:vctl-lb-watch"
        except OSError:
            pass

    if not enabled:
        state = "disabled"
    elif session_alive:
        state = "running"
    else:
        state = "not running"

    return {
        "enabled": enabled,
        "session_alive": session_alive,
        "pidfile_ok": pidfile_ok,
        "state": state,
    }
```

**3b.** ADD helper to `src/vctl/commands/lb.py`:

```python
def _stop_watcher_if_running(mgr: LbManager) -> None:
    """Stop vctl-lb-watch watcher session before lb stop. Idempotent."""
    from vctl.lb.prune import _stop_watcher
    _stop_watcher(mgr)
```

**3c.** MODIFY `_do_stop` in `src/vctl/commands/lb.py` — call `_stop_watcher_if_running(mgr)` BEFORE `mgr.stop()`:

```python
# At the start of _do_stop, before mgr.stop():
_stop_watcher_if_running(mgr)
```

**3d.** MODIFY `_do_status` (or `_do_info`) in `src/vctl/commands/lb.py` — add watcher row:

```python
# Inside _do_status, after the existing haproxy status rows:
from vctl.lb.prune import _watcher_status
ws = _watcher_status(mgr)
print(f"watcher:  {ws['state']}", file=sys.stderr)
```

#### Step 4 — Run test; confirm all pass

- [ ] Run `.venv/bin/pytest tests/test_commands_lb.py -k "stop_watcher or status_reports_watcher or watcher" -q` — all 6 Task 5+6 tests pass.
- [ ] Run `.venv/bin/pytest -q` (full suite) — no regressions.

#### Step 5 — Lint + type-check + commit

- [ ] `.venv/bin/ruff check src/vctl/lb/prune.py src/vctl/commands/lb.py tests/test_commands_lb.py`
- [ ] `.venv/bin/ruff format --check src/vctl/lb/prune.py src/vctl/commands/lb.py tests/test_commands_lb.py`
- [ ] `.venv/bin/mypy --strict src/vctl/lb/prune.py src/vctl/commands/lb.py`
- [ ] `git add src/vctl/lb/prune.py src/vctl/commands/lb.py tests/test_commands_lb.py`
- [ ] `git commit -m "feat: integrate watcher kill into lb stop + state in lb status (Task 6)"`

**CHECKPOINT after Task 6:** Watcher lifecycle is fully integrated into `lb start/stop/status`. All 6 watcher integration tests (Tasks 5 + 6) pass. `_stop_watcher` is idempotent. `_watcher_status` correctly handles all four states (running / not running / disabled / session-with-no-pidfile). No separate `vctl lb watch` verb group exists.

---

### Task 7: Final gates + version bump

**Goal:** Bump the version from `0.5.6` to `0.6.0` everywhere, prepend the CHANGELOG entry, run `uv lock`, and then run all four CI gates. This task has no new code — it is purely the integration checkpoint confirming the full changeset is clean.

#### Step 1 — Bump version everywhere

- [ ] MODIFY `pyproject.toml`: change `version = "0.5.6"` to `version = "0.6.0"`
- [ ] MODIFY `src/vctl/__init__.py`: change `__version__ = "0.5.6"` to `__version__ = "0.6.0"`
- [ ] MODIFY `tests/test_smoke.py`: update the version assertion from `"0.5.6"` to `"0.6.0"` (find with grep: `.venv/bin/pytest tests/test_smoke.py -q` first to see what it asserts)

#### Step 2 — Prepend CHANGELOG entry

- [ ] MODIFY `docs/CHANGELOG.md` — prepend at the top (after the `# Changelog` heading):

```markdown
## [0.6.0] - 2026-05-04

### Added

- **`vctl lb prune`** — manual reaper command. Removes backends that are HAProxy-`DOWN` and have been DOWN for longer than a configurable threshold. Threshold precedence: `--threshold DURATION` flag → `cluster.lb.prune.threshold` field in cluster.yaml → `"5m"` default. Flags: `--pool NAME` (scope to one pool, exit 3 on unknown pool), `--dry-run` (preview without acting). `MAINT`/`DRAIN` backends are always preserved — only health-check-failed (`DOWN`) backends are eligible. Reuses existing `Reconciler.want_absent` so haproxy-first ordering is preserved.
- **Auto-watcher bundled into `vctl lb start/stop/status`** — when `cluster.lb.prune.enabled: true` (default), `vctl lb start` also spawns a background prune loop in tmux session `vctl-lb-watch` alongside the HAProxy session. Sentinel pidfile at `~/.vctl/lb/watch.pid` contains `tmux:vctl-lb-watch`. `vctl lb stop` kills both sessions idempotently. `vctl lb status` reports the watcher state. Set `cluster.lb.prune.enabled: false` to disable the auto-watcher (manual `vctl lb prune` still works).
- **`src/vctl/duration.py`** — new stdlib-only `_parse_duration("5m")` → 300 helper. Accepts `Ns`, `Nm`, `Nh`, `Nd` suffixes.
- **`LbPrune` pydantic class** in `cluster.yaml`'s `lb` section. New schema:
  ```yaml
  lb:
    prune:
      threshold: 5m
      watch_interval: 30s
  ```
  Defaults match historical behavior; existing cluster.yaml files need no migration.
```

#### Step 3 — Update uv lockfile

- [ ] Run `uv lock` to regenerate `uv.lock` if the version bump affected it.

#### Step 4 — Run all four CI gates

- [ ] `.venv/bin/ruff check .`
- [ ] `.venv/bin/ruff format --check .`
- [ ] `.venv/bin/mypy --strict src/vctl`
- [ ] `.venv/bin/pytest -q --cov=vctl --cov-report=term-missing --cov-fail-under=50 -k "not test_lb_attach_refuses_when_model_not_loaded and not test_serve_auto_attaches_to_matching_pool"`

All four must exit 0 with no errors or warnings. If any gate fails:
- `ruff check` failures: fix lint errors one by one and re-run.
- `ruff format --check` failures: run `.venv/bin/ruff format .` (without `--check`) to auto-fix, then re-run check.
- `mypy --strict` failures: fix type annotations. Common issues:
  - Missing `from __future__ import annotations` at top of file
  - `argparse.Namespace` attributes need `getattr(parsed, "attr", default)` pattern
  - `lb_admin_client` return type is `RuntimeClient | None` — always check for `None`
  - `_tmux_run_detached_argv` in the monkeypatch needs `# type: ignore[attr-defined]` only if patched as module attr
- `pytest --cov-fail-under=50` failures: if coverage drops below 50%, add an additional test to one of the new test files targeting an uncovered branch.

#### Step 5 — Commit version bump

- [ ] `git add pyproject.toml src/vctl/__init__.py tests/test_smoke.py docs/CHANGELOG.md uv.lock`
- [ ] `git commit -m "chore: bump to v0.6.0 for lb prune (Phase 2)"`

**CHECKPOINT after Task 7:** All four CI gates green. Version is `0.6.0` in all three locations. CHANGELOG prepended. Ready to merge.

---

## Acceptance Test Map

| AT | Covered by | Task |
|---|---|---|
| AT-1 (prune removes DOWN ep) | `test_prune_removes_eligible_backend` | Task 4 |
| AT-2 (--threshold honored) | `test_prune_threshold_flag_overrides_default` | Task 4 |
| AT-3 (--pool scope) | `test_prune_unknown_pool_returns_3` + scoped variant | Task 4 |
| AT-4 (--dry-run no-op) | `test_prune_dry_run_does_not_call_want_absent` | Task 4 |
| AT-5 (MAINT preserved) | `test_skips_maint_backends` | Task 3 |
| AT-6 (DRAIN preserved) | `test_skips_drain_backends` | Task 3 |
| AT-7 (lb start spawns watcher when enabled) | `test_lb_start_spawns_watcher_when_enabled` | Task 5 |
| AT-7b (lb start skips watcher when disabled) | `test_lb_start_skips_watcher_when_disabled` | Task 5 |
| AT-8 (lb stop kills both sessions) | `test_lb_stop_kills_watcher_session` | Task 6 |
| AT-9 (lb status reports watcher state) | `test_lb_status_reports_watcher_running` + `test_lb_status_reports_watcher_disabled` | Task 6 |
| AT-10 (gates pass) | Task 7 final pipeline | Task 7 |

---

## Cross-Cutting Notes for Agentic Implementers

### Monkeypatching targets

Patch at the **import site**, not the definition site:

```python
# CORRECT — patches the reference inside vctl.lb.prune
monkeypatch.setattr("vctl.lb.prune.lb_admin_client", fake_fn)
monkeypatch.setattr("vctl.lb.prune._fetch_haproxy_stats", fake_stats_fn)

# CORRECT — patches Reconciler method class-wide
monkeypatch.setattr(Reconciler, "want_absent", fake_want_absent)

# CORRECT — patches platform helpers imported into lb.py as module-level aliases
monkeypatch.setattr("vctl.commands.lb._tmux_session_exists", lambda name: False)
monkeypatch.setattr("vctl.commands.lb._tmux_run_detached_argv", mock_fn)
monkeypatch.setattr("vctl.commands.lb._tmux_kill", lambda name: None)
```

### Admin bitmask values

From `src/vctl/lb/runtime.py`:
- `LB_ADMIN_MAINT_MASK = 0x07` — `admin_state & 0x07 != 0` → `"maint"`
- `LB_ADMIN_DRAIN_MASK = 0x38` — `admin_state & 0x38 != 0` → `"drain"`
- `admin_state = 0` → `"ready"`

Always use the `BackendStatus.admin` property (computed from bitmask) for eligibility checks. Never compare bitmask integers directly in prune logic.

### HAProxy `status` field values

The `status` column from `show stat` CSV can be:
- `"UP"`, `"UP 1/2"` (transitional rise)
- `"DOWN"`, `"DOWN 1/3"` (transitional fall)
- `"MAINT"`, `"DRAIN"`, `"NOLB"`, `"no check"`

The eligibility check `status.startswith("DOWN")` correctly handles both `"DOWN"` and `"DOWN 1/3"`.

### `ns.config` resolution

In `run()`, `ns.config` is the `--config` flag value from the top-level argparse. It may be `None` if the user did not pass `--config` (config is then resolved from `$CLUSTER_CONFIG` or `~/.vctl/cluster.yaml` by the `_manager()` helper). For the watch loop body, pass the resolved path so the spawned subprocess doesn't need to re-resolve. Use:

```python
cluster_yaml = Path(ns.config) if ns.config else Path.home() / ".vctl" / "cluster.yaml"
```

### `_fetch_haproxy_stats` callable signature

`_fetch_haproxy_stats(cli: object) -> dict[str, dict[str, dict[str, int | str]]]`

The first argument is an opaque client object (`RuntimeClient` or test stub). When mocking, provide a callable that ignores its argument and returns the fake stats dict:

```python
monkeypatch.setattr(prune_mod, "_fetch_haproxy_stats", lambda c: fake_stats_dict)
```

### `_spawn_watcher_if_enabled` and `_stop_watcher_if_running` signatures

Both helpers are thin wrappers in `commands/lb.py` that call into `vctl.lb.prune`:

```python
def _spawn_watcher_if_enabled(mgr: LbManager, cluster_yaml_path: Path) -> None: ...
def _stop_watcher_if_running(mgr: LbManager) -> None: ...
```

The `cluster_yaml_path` is resolved in `_do_start` from `ns.config` (or `~/.vctl/cluster.yaml` fallback) before calling `_spawn_watcher_if_enabled`.

### Pidfile path

The sentinel pidfile lives at `mgr.run_dir / "watch.pid"`. Since `mgr.run_dir` defaults to `~/.vctl/lb`, the production path is `~/.vctl/lb/watch.pid`. Tests use `tmp_path`-based `run_dir` (via `LbManager(lb, state_dir=..., run_dir=tmp_path/"run")`), so no monkeypatching of the path itself is needed.

### Circular import avoidance

The circular dependency chain to avoid is:

```
vctl.config.models  imports  vctl.duration  (field validator)
vctl.lb.prune       imports  vctl.commands.lb  (_fetch_haproxy_stats)
vctl.commands.lb    imports  vctl.lb.manager
vctl.lb.manager     imports  vctl.config.models
```

This is fine as long as:
1. `vctl.duration` has NO vctl imports (pure stdlib).
2. `LbPrune._valid_duration` imports `vctl.duration` **lazily** (inside method body, not at class/module level).
3. `vctl.lb.prune` does NOT import `vctl.config.models` at module level (uses `TYPE_CHECKING` for `LbManager`).

The current design satisfies all three constraints.
