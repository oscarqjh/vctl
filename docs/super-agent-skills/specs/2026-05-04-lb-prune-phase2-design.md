# vctl lb prune (worker reaper) — Phase 2 — Design Spec

## Objective

Phase 2 adds `vctl lb prune`, a manual command that scans all pools (or one named
pool) and removes backends whose HAProxy health status is DOWN and whose `lastchg`
(seconds continuously in that state) exceeds a configurable threshold. An
auto-watcher is bundled directly into `vctl lb start/stop/status`: when
`cluster.lb.prune.enabled: true` (the default), `vctl lb start` also spawns a
background prune loop in tmux session `vctl-lb-watch` alongside the HAProxy session
(`vctl-lb`). There is no separate `vctl lb watch` sub-command group.
The feature targets cluster operators who currently have to manually spot dead
vllm workers via `vctl lb status` and issue individual `vctl lb remove` calls.

### Success Criteria

1. `vctl lb prune` removes an endpoint matching DOWN + `lastchg` > 5 m default; LB
   endpoint cleared from HAProxy and from the pool state file.
2. `vctl lb prune --threshold 1m` uses the flag value and overrides cluster.yaml.
3. `vctl lb prune --pool qwen3-5-9b` scopes the scan to that pool only; other pools
   are untouched.
4. `vctl lb prune --dry-run` prints the would-remove list to stderr and exits 0
   without mutating HAProxy or the state file.
5. Backends in MAINT or DRAIN admin-state are NEVER pruned regardless of `lastchg`.
6. UP backends are never pruned.
7. `vctl lb start` with `prune.enabled: true` spawns BOTH the `vctl-lb` HAProxy
   session AND the `vctl-lb-watch` watcher session; writes a sentinel pidfile at
   `~/.vctl/lb/watch.pid`; exits 0.
7b. `vctl lb start` with `prune.enabled: false` spawns ONLY the `vctl-lb` HAProxy
    session; does NOT spawn `vctl-lb-watch`; does NOT write `watch.pid`; exits 0.
8. `vctl lb stop` kills BOTH `vctl-lb` and `vctl-lb-watch` tmux sessions; removes
   `~/.vctl/lb/watch.pid`; exits 0.
9. `vctl lb status` reports both the HAProxy state and the watcher state (running /
   not running / disabled).
10. `mypy --strict`, `ruff check`, `ruff format --check`, and
    `pytest --cov-fail-under=50` all pass after this change.

---

## Tech Stack

- Python 3.10+ (`from __future__ import annotations` everywhere).
- Pydantic v2 for new `LbPrune` schema class (extends `LbHaproxy` via composition).
- HAProxy admin socket: `show stat` CSV for `lastchg` + `status`; `show servers
  state` for `admin_state` (MAINT/DRAIN bitmask).
- `Reconciler.want_absent` as the single removal primitive.
- `tmux_run_detached_argv` / `tmux_kill` / `tmux_session_exists` (existing helpers
  in `lb/tmux.py`) for the watcher session.
- `fcntl.flock`-protected `BackendState._locked()` — no new locking primitives.
- Standard library only for duration parsing (`re`, `int`).
- `rich` for console output (already a runtime dependency).

---

## Architecture

`vctl lb prune` is a read-haproxy-then-conditionally-mutate operation. It calls
`_fetch_haproxy_stats` (already in `commands/lb.py`) to obtain `(status, lastchg)`
per backend per pool, then calls `RuntimeClient.show_servers_state` (via
`Reconciler._acquire`) to get the `admin_state` bitmask. A backend qualifies for
pruning only when all three conditions hold: `status` starts with `"DOWN"`,
`admin_state` is `ready` (neither MAINT nor DRAIN bitmask bits set), and `lastchg >=
threshold_seconds`. Once candidates are identified, `prune` delegates each removal to
`Reconciler.want_absent(ep, pool)`, which preserves the haproxy-first invariant
(set maint → del server → state file write). Dry-run mode short-circuits before the
`want_absent` call.

The watcher is a thin Python script run inside a tmux session by
`tmux_run_detached_argv`. It imports vctl directly (`python -m vctl lb prune ...`)
in a loop, sleeping `watch_interval` seconds between iterations. This keeps the
watcher stateless: it reads `cluster.yaml` fresh every iteration so rolling config
updates take effect without restarting the session. The watcher refuses to start on
non-LB hosts by calling `LbManager.is_host()` before creating the tmux session.

The threshold for "dead" is resolved in precedence order at prune time:
`--threshold` flag → `cluster.lb.prune.threshold` YAML field → default `"5m"`. The
duration string is converted to an integer second count by `_parse_duration`, a small
pure-Python helper added to `vctl/lb/prune.py`.  The same helper validates user input
on the command line and YAML schema via a Pydantic field validator in `LbPrune`.

---

## Components

### 1. `LbPrune` pydantic class — `config/models.py`

New optional subfield on `LbHaproxy`:

```python
class LbPrune(_Strict):
    enabled: bool = True        # set false to disable auto-watcher on lb start
    threshold: str = "5m"       # minimum DOWN duration before pruning
    watch_interval: str = "30s" # period between watcher iterations

    @field_validator("threshold", "watch_interval", mode="after")
    @classmethod
    def _valid_duration(cls, v: str) -> str:
        from vctl.duration import _parse_duration  # top-of-class import OK; vctl.duration is stdlib-only

        _parse_duration(v)  # raises ValueError on bad input
        return v
```

`LbHaproxy` gains one new field:

```python
prune: LbPrune = Field(default_factory=LbPrune)
```

`_parse_duration` lives in a NEW standalone module `src/vctl/duration.py` with pure stdlib
imports only — NO vctl-specific imports. This breaks the circular dependency that would
otherwise occur if `_parse_duration` lived in `vctl.lb.prune` (since `models.py` imports
from `vctl.duration` at module load time, and `vctl.lb.prune` would also need
`vctl.config.models` for type hints). Decision is final — do not put the parser in
`vctl/lb/prune.py`.

### 2. `_parse_duration` helper — `vctl/duration.py`

```python
def _parse_duration(s: str) -> int:
    """Parse '300s', '5m', '2h', '1d' → integer seconds.

    Raises ValueError on unrecognised format.
    """
```

Accepted suffixes: `s` (seconds), `m` (minutes, ×60), `h` (hours, ×3600),
`d` (days, ×86400). Input must match `^\d+[smhd]$`; anything else raises
`ValueError("invalid duration: ...")`.  Return value is always a positive integer.

### 3. `_collect_prune_candidates` — `vctl/lb/prune.py`

```python
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
    """
```

**Two-call data join** (the ambiguity in the original design is now explicit):

`_fetch_haproxy_stats` (already in `commands/lb.py:416`) returns a nested dict shape:
`dict[backend_section, dict[server_name, dict[field_name, int|str]]]`. The fields populated
include `"status"` (string, e.g. `"UP"`, `"DOWN"`, `"DOWN 1/3"`, `"MAINT"`, `"no check"`)
and `"lastchg"` (int seconds since last UP↔DOWN transition). Caller selects the slice for
backend section `f"pool_{pool_name}"`, then iterates the inner dict to map
`server_name → (status, lastchg)`.

`RuntimeClient.show_servers_state()` (in `runtime.py`) returns a flat list of
`BackendStatus` rows with `(name, endpoint, op_state, admin_state, backend)`. There is no
`lastchg` field on `BackendStatus`. Caller filters rows by `backend == f"pool_{pool_name}"`,
then derives `admin = "maint" | "drain" | "ready"` from the `admin_state` bitmask via the
existing `BackendStatus.admin` property.

The join key is `name` (haproxy server name) on both sides. The endpoint string `<ip>:<port>`
comes from `BackendStatus.endpoint`. Both calls require fresh `RuntimeClient` instances
(per-command socket contract from `CLAUDE.md`); the implementation uses `lb_admin_client(mgr)`
directly (NOT `Reconciler._acquire`, which is private and would fail mypy --strict access
checks from outside the class).

Pseudo-code:

```python
from vctl.commands.lb import _fetch_haproxy_stats
from vctl.lb.runtime import lb_admin_client
from vctl.lb.errors import LbUnreachable

def _collect_prune_candidates(mgr, pool_name, threshold_s):
    cli1 = lb_admin_client(mgr)
    if cli1 is None:
        raise LbUnreachable(f"haproxy admin socket unreachable on {mgr.lb.host}")
    stats_by_section = _fetch_haproxy_stats(cli1)
    pool_stats = stats_by_section.get(f"pool_{pool_name}", {})

    cli2 = lb_admin_client(mgr)  # fresh socket per call
    if cli2 is None:
        raise LbUnreachable(...)
    rows = [r for r in cli2.show_servers_state() if r.backend == f"pool_{pool_name}"]

    candidates: list[tuple[str, int]] = []
    for row in rows:
        if row.admin != "ready":
            continue  # skip MAINT / DRAIN
        s = pool_stats.get(row.name, {})
        status = str(s.get("status", ""))
        if not status.startswith("DOWN"):
            continue
        lastchg = int(s.get("lastchg", 0))
        if lastchg >= threshold_s:
            candidates.append((row.endpoint, lastchg))

    candidates.sort()
    return candidates
```

Returns a list sorted by `ep` for deterministic output. If the admin socket is
unreachable on EITHER call, raises `LbUnreachable` (caller maps to exit 4).

### 4. `vctl lb prune` verb — `commands/lb.py`

Add `"prune"` to `_LB_VERB_HELP` and to `_build_subparser()`:

```python
prune = sp.add_parser("prune", help="Remove health-check-failed (DOWN) backends past threshold")
prune.add_argument("--pool", default=None, help="scope to one pool (default: all)")
prune.add_argument(
    "--threshold",
    default=None,
    metavar="DURATION",
    help="override dead threshold, e.g. 5m, 300s, 2h (default: cluster.lb.prune.threshold)",
)
prune.add_argument("--dry-run", action="store_true", help="print candidates; do not remove")
```

Handler `_do_prune(mgr, parsed) -> int` in `commands/lb.py`:

1. Resolve threshold: flag → `mgr.lb.prune.threshold` → `"5m"`. Parse with
   `_parse_duration`; if the flag value is invalid, print error to stderr, return 1.
2. Determine pool list:
   - If `--pool` given: validate against `mgr.lb.pools` BEFORE any haproxy call.
     `pool_names = {p.name for p in mgr.lb.pools}`. If `parsed.pool not in pool_names`,
     print `unknown pool: <name>; available: <comma-sep list>` to stderr, return 3
     (matches existing pool-routing exit-code convention; see `lb_scaling._exit_for`).
   - Otherwise: iterate `[p.name for p in mgr.lb.pools]`.
   This pre-flight guard is intentional — without it, `PoolNotFound` would only surface
   inside the per-pool loop, after the first haproxy admin call, leading to confusing
   partial behavior.
3. For each pool: call `_collect_prune_candidates(mgr, pool, threshold_s)`.
4. If `--dry-run`: print `would prune <ep> from pool <pool> (DOWN for <duration>)`
   for each candidate; return 0.
5. Otherwise: call `Reconciler(mgr).want_absent(ep, pool)` for each candidate.
   Print `pruned <ep> from pool <pool> (DOWN for <duration>)` to stderr on success.
6. Catch `LbUnreachable` → print to stderr, return 4.
7. Catch `BackendOpFailed` → print to stderr, return 1.
8. Return 0 on full success.

### 5. Watcher helpers — `lb/prune.py`

Three new functions live in `src/vctl/lb/prune.py` so `commands/lb.py` does not get
bloated. They are imported locally (inside the verb handlers) to keep the cold-import path
light.

#### `_spawn_watcher(mgr, prune_cfg, cluster_yaml_path)` — `lb/prune.py`

```python
def _spawn_watcher(
    mgr: LbManager,
    prune_cfg: LbPrune,
    cluster_yaml_path: Path,
) -> None:
```

1. Build `inner_argv`: `[sys.executable, "-m", "vctl", "--config", str(cluster_yaml_path), "lb", "prune"]`.
2. Resolve `interval_s = _parse_duration(prune_cfg.watch_interval)`.
3. Build `loop_cmd = f"while true; do {shlex.join(inner_argv)}; sleep {interval_s}; done"`.
4. Call `tmux_run_detached_argv("vctl-lb-watch", ["bash", "-c", loop_cmd])`.
5. Write pidfile `~/.vctl/lb/watch.pid` (i.e. `mgr.run_dir / "watch.pid"`) atomically
   (`.tmp` + `os.replace`) with content `tmux:vctl-lb-watch\n`.

The string `tmux:<session_name>` is a sentinel — `_watcher_status` reads the pidfile,
verifies the session name matches `vctl-lb-watch`, then calls `tmux_session_exists` to
confirm liveness. This sentinel format is deliberately not a numeric PID because tmux's
pane PID is unstable across shell exec'ing the inner `vctl lb prune` subprocess each
iteration.

#### `_stop_watcher(mgr)` — `lb/prune.py`

```python
def _stop_watcher(mgr: LbManager) -> None:
```

1. `tmux_kill("vctl-lb-watch")` — idempotent (no error if session absent).
2. Remove `mgr.run_dir / "watch.pid"` if it exists.

#### `_watcher_status(mgr) -> dict[str, object]` — `lb/prune.py`

```python
def _watcher_status(mgr: LbManager) -> dict[str, object]:
```

Returns a dict with keys:
- `"enabled"` (`bool`) — `mgr.lb.prune.enabled`
- `"session_alive"` (`bool`) — `tmux_session_exists("vctl-lb-watch")`
- `"pidfile_ok"` (`bool`) — pidfile present and contains `tmux:vctl-lb-watch`
- `"state"` (`str`) — `"running"` / `"not running"` / `"disabled"`

When `enabled` is `False`, `state` is `"disabled"` regardless of session liveness.

### 6. `lb start / stop / status` integration — `commands/lb.py`

No new top-level verbs. Three existing handlers are extended:

#### `_do_start` hook — call `_spawn_watcher_if_enabled(mgr, cluster_yaml_path)` AFTER `mgr.start()` succeeds

```python
def _spawn_watcher_if_enabled(mgr: LbManager, cluster_yaml_path: Path) -> None:
    """Spawn vctl-lb-watch watcher session if prune.enabled is True."""
    from vctl.lb.prune import _spawn_watcher
    if mgr.lb.prune.enabled:
        _spawn_watcher(mgr, mgr.lb.prune, cluster_yaml_path)
```

Called at the end of `_do_start` after a successful `mgr.start()`. If `prune.enabled` is
`False`, the watcher is silently skipped and only the HAProxy session is started.

#### `_do_stop` hook — call `_stop_watcher_if_running(mgr)` BEFORE `mgr.stop()`

```python
def _stop_watcher_if_running(mgr: LbManager) -> None:
    """Stop vctl-lb-watch watcher session if it is running."""
    from vctl.lb.prune import _stop_watcher
    _stop_watcher(mgr)  # idempotent — safe even if watcher is not running
```

Called at the start of `_do_stop` before `mgr.stop()`. Idempotent: if the watcher was
never started (e.g. `prune.enabled: false`), `tmux_kill` is a no-op and missing pidfile is
silently ignored.

#### `_do_status` (formerly `_do_info`) hook — add "watcher:" row

`_watcher_status(mgr)` is called inside `_do_status` and its result is rendered alongside
the existing HAProxy status rows. The display format:

```
watcher:  running (session=vctl-lb-watch)
```
or:
```
watcher:  not running
```
or:
```
watcher:  disabled (prune.enabled: false)
```

---

## Data Flow

### `prune` sequence

```
vctl lb prune [--pool P] [--threshold T] [--dry-run]
  │
  ├─ resolve threshold (flag → yaml → "5m")
  ├─ for each target pool:
  │    ├─ _collect_prune_candidates(mgr, pool, threshold_s)
  │    │    ├─ fresh RuntimeClient → show stat CSV  (status, lastchg per ep)
  │    │    └─ fresh RuntimeClient → show servers state  (admin_state per ep)
  │    │         → filter: status.startswith("DOWN")
  │    │                   AND admin == "ready"
  │    │                   AND lastchg >= threshold_s
  │    │
  │    └─ for each (ep, lastchg_s) in candidates:
  │         if dry-run: print "would prune ..." → continue
  │         else: Reconciler(mgr).want_absent(ep, pool)
  │               → set_state maint   (fresh socket)
  │               → del server        (fresh socket)
  │               → BackendState.remove(ep)
  │               print "pruned ..."
  │
  └─ return 0
```

### `lb start` with watcher sequence

```
vctl lb start
  │
  ├─ mgr.start()  →  HAProxy launched in tmux session "vctl-lb"
  │
  └─ _spawn_watcher_if_enabled(mgr, cluster_yaml_path)
       │
       ├─ if prune.enabled == False → skip (no watcher session spawned)
       │
       └─ if prune.enabled == True:
            ├─ resolve interval_s from prune.watch_interval
            ├─ build loop_cmd:
            │    "while true; do python -m vctl lb prune; sleep N; done"
            ├─ tmux_run_detached_argv("vctl-lb-watch", ["bash", "-c", loop_cmd])
            └─ write ~/.vctl/lb/watch.pid  (sentinel: "tmux:vctl-lb-watch")
```

### `lb stop` with watcher sequence

```
vctl lb stop
  │
  ├─ _stop_watcher_if_running(mgr)
  │    ├─ tmux_kill("vctl-lb-watch")  (idempotent)
  │    └─ remove ~/.vctl/lb/watch.pid  (if exists)
  │
  └─ mgr.stop()  →  HAProxy tmux session "vctl-lb" killed
```

---

## Error Handling

| Condition | Exit code | Message |
|---|---|---|
| HAProxy admin socket unreachable | 4 | `LbUnreachable: <detail>` |
| Unknown pool name (`--pool`) | 1 | `PoolNotFound: <detail>` |
| Invalid threshold/interval string | 1 | `invalid duration: '<value>'` |
| `want_absent` fails (BackendOpFailed) | 1 | `backend op failed: <detail>` |
| watcher already running (session exists at `lb start` time) | logged warning, non-fatal | `watcher already running — skipping spawn` |
| Generic unexpected exception | 1 | exception repr to stderr |

All error messages go to `sys.stderr`. Exit 0 is reserved for full success (including
zero candidates pruned or a clean dry-run). The `LbUnreachable` → exit 4 mapping
mirrors the existing `lb start` error convention (exit 4 = environment/host guard).

---

## Testing Strategy

### New test files

**`tests/test_lb_prune_helpers.py`** — unit tests for `_parse_duration`:
- Valid inputs: `"300s"` → 300, `"5m"` → 300, `"2h"` → 7200, `"1d"` → 86400.
- Invalid inputs: `""`, `"5x"`, `"1.5m"`, `"abc"` → `ValueError`.

**`tests/test_commands_lb_prune.py`** — subprocess + mock tests for `vctl lb prune`:

Follows the same `_vctl()` subprocess-invocation pattern from
`test_commands_lb_scaling.py`. For unit-level tests that need to assert on
`Reconciler.want_absent` calls, use `monkeypatch.setattr(Reconciler, "want_absent", mock_fn)`.

**Correct monkeypatch targets** (the new module imports these directly, so patches at the
DEFINITION site do NOT intercept calls inside `vctl.lb.prune`):

```python
# Patch lb_admin_client at the IMPORT SITE inside vctl.lb.prune:
monkeypatch.setattr("vctl.lb.prune.lb_admin_client", fake_client_fn)

# Patch _fetch_haproxy_stats at the IMPORT SITE inside vctl.lb.prune (it's imported from
# vctl.commands.lb at the top of vctl.lb.prune):
monkeypatch.setattr("vctl.lb.prune._fetch_haproxy_stats", fake_stats_fn)
```

The `BackendStatus.admin_state` field is an integer bitmask (not the string `"maint"`/
`"drain"`/`"ready"`). When mocking rows, set the bitmask integer directly — the existing
admin-state mask constants in `vctl.lb.runtime` are: `0` (ready), `0x01` (maint),
`0x02` (drain). The `BackendStatus.admin` property (lines ~36-41 of runtime.py) computes
the lowercase string from the bitmask. Use the property, not a mock string field.

Example mock row:
```python
from vctl.lb.runtime import BackendStatus
fake_row = BackendStatus(
    name="b_10_0_0_1_8000",
    endpoint="10.0.0.1:8000",
    op_state=0,           # 0=stopped, 2=running
    admin_state=0,         # 0=ready, 1=maint, 2=drain
    backend="pool_default",
)
```

Key test cases:
- `test_prune_removes_down_backend_past_threshold` — one DOWN ep at 400 s with
  default threshold 300 s → `want_absent` called once.
- `test_prune_respects_flag_threshold` — `--threshold 1m`, ep at 90 s → pruned;
  ep at 50 s → not pruned.
- `test_prune_pool_flag_scopes_to_one_pool` — two-pool cluster; DOWN ep in pool B;
  `--pool a` → want_absent NOT called.
- `test_prune_dry_run_no_mutation` — `--dry-run`, DOWN ep past threshold → stderr
  contains "would prune"; `want_absent` never called.
- `test_prune_skips_maint` — ep status DOWN, admin MAINT → not pruned.
- `test_prune_skips_drain` — ep status DOWN, admin DRAIN → not pruned.
- `test_prune_skips_up` — ep status UP → not pruned regardless of lastchg.
- `test_prune_lb_unreachable_exit4` — `lb_admin_client` returns None → exit 4.
- `test_prune_invalid_threshold_exit1` — `--threshold bad` → exit 1.
- `test_prune_unknown_pool_exit1` — `--pool nonexistent` → exit 1.

**`tests/test_commands_lb.py`** (existing file) — extend with watcher integration tests:

Use `monkeypatch` on `tmux_session_exists`, `tmux_run_detached_argv`, `tmux_kill`.
All tests run without a real tmux binary.

Key test cases added in Tasks 5 and 6:
- `test_lb_start_spawns_watcher_when_enabled` — `prune.enabled=True` →
  `tmux_run_detached_argv` called with session `vctl-lb-watch`; pidfile written.
- `test_lb_start_skips_watcher_when_disabled` — `prune.enabled=False` →
  `tmux_run_detached_argv` NOT called with `vctl-lb-watch`; no pidfile.
- `test_lb_stop_kills_both_sessions` — `tmux_kill` called for both `vctl-lb` and
  `vctl-lb-watch`; pidfile removed.
- `test_lb_status_reports_watcher_running` — session alive + pidfile present →
  output contains `watcher: running`.
- `test_lb_status_reports_watcher_not_running` — watcher session absent →
  output contains `watcher: not running`.
- `test_lb_status_reports_watcher_disabled` — `prune.enabled=False` →
  output contains `watcher: disabled`.

### Mocking pattern (mirrors existing tests)

```python
import os
from unittest.mock import MagicMock, patch
from vctl.lb.reconciler import Reconciler

# Inject fake show_servers_state + lb_admin_client:
mock_client = MagicMock()
mock_client.show_servers_state.return_value = [
    BackendStatus(name="b_10_0_0_5_8000", endpoint="10.0.0.5:8000",
                  op_state=0, admin_state=0, backend="pool_default"),
]
with patch("vctl.commands.lb_scaling._client", return_value=mock_client):
    with patch("vctl.lb.reconciler.lb_admin_client", return_value=mock_client):
        ...
```

---

## Schema Changes

### cluster.yaml (additive, fully backward-compatible)

```yaml
lb:
  kind: haproxy
  # ... existing fields ...
  prune:                  # NEW optional block; omit = use all defaults
    enabled: true         # set false to disable auto-watcher on lb start
    threshold: 5m         # minimum DOWN duration before a backend is eligible
    watch_interval: 30s   # polling interval for the auto-watcher loop
```

The `prune` block is optional. Omitting it (all existing cluster.yaml files) uses
`LbPrune` defaults. No migration needed.

### `config/models.py` additions

```python
class LbPrune(_Strict):
    enabled: bool = True
    threshold: str = "5m"
    watch_interval: str = "30s"

    @field_validator("threshold", "watch_interval", mode="after")
    @classmethod
    def _valid_duration(cls, v: str) -> str:
        from vctl.duration import _parse_duration  # lazy import avoids circular
        _parse_duration(v)
        return v

# Inside LbHaproxy:
prune: LbPrune = Field(default_factory=LbPrune)
```

---

## File Map

| File | Change |
|---|---|
| `src/vctl/config/models.py` | Add `LbPrune` class (with `enabled` field); add `prune` field to `LbHaproxy` |
| `src/vctl/lb/prune.py` | New module: `_parse_duration`, `_collect_prune_candidates`, `_spawn_watcher`, `_stop_watcher`, `_watcher_status` |
| `src/vctl/commands/lb.py` | Add `prune` verb; extend `_do_start`/`_do_stop`/`_do_status` with watcher hooks |
| `tests/test_duration.py` | New: `_parse_duration` unit tests |
| `tests/test_commands_lb_prune.py` | New: prune cmd tests |
| `tests/test_commands_lb.py` | Extend: watcher integration tests for start/stop/status |
| `examples/cluster_template.yaml` | Add `prune:` block (commented) |
| `CHANGELOG.md` | v0.6.0 entry |
| `pyproject.toml` | Bump version `0.5.3` → `0.6.0` |

No new runtime dependencies. No changes to `cli.py` (`lb` is already registered as
a profile-aware command).

---

## Boundaries

### Always do

- Use `Reconciler.want_absent` for every removal — never bypass the
  haproxy-first ordering.
- Open a fresh `RuntimeClient` per HAProxy admin command.
- Use `BackendState._locked()` (via `.remove()`) for all state file mutations —
  no new locking primitives.
- Write to `sys.stderr` for per-removal output and error messages.
- Validate `--threshold` and `--interval` at parse time, not lazily.
- Skip watcher spawn silently (log only) when the watcher session is already running at `lb start` time.

### Ask first

- Any change to `BackendStatus` fields (e.g. adding `lastchg`) — touches runtime
  parsing and all tests that construct `BackendStatus` directly.
- Adding a real OS-level PID to `watch.pid` instead of a session-name sentinel —
  affects `watch status` logic and requires careful tmux pane PID extraction.
- Supporting `--dry-run` output to stdout instead of stderr.

### Never do

- Prune backends in MAINT or DRAIN admin-state.
- Skip the `set_state maint` step before `del server` (haproxy contract).
- Reuse a single `RuntimeClient` across multiple admin commands.
- Write to the state file before HAProxy acknowledges the removal.
- Create a new tmux session named `vctl-lb` (reserved for haproxy); watcher always uses `vctl-lb-watch`.
- Add a `vctl lb watch` sub-command group — watcher lifecycle is bundled into `lb start/stop/status`.
- Cross-host prune (SSH to another node to run the admin command) — Phase 3 only.
- Auto-revive pruned backends — Phase 3 only.

---

## Acceptance Tests

**AT-1** (SC-1 — prune removes dead backend)
Given a cluster with pool `default` and backend `10.0.0.5:8000` registered in the
state file and in HAProxy with `status=DOWN`, `admin=ready`, `lastchg=400` (> 300 s
threshold);
When `vctl lb prune` is invoked with default threshold;
Then `Reconciler.want_absent("10.0.0.5:8000", "default")` is called exactly once,
stderr contains `pruned 10.0.0.5:8000 from pool default`, and the backend is absent
from the state file.

**AT-2** (SC-2 — flag threshold overrides yaml)
Given the same DOWN backend with `lastchg=90` and `cluster.lb.prune.threshold=5m`;
When `vctl lb prune --threshold 1m` is invoked;
Then the backend is pruned (90 s > 60 s); without the flag it would not be (90 s < 300 s).

**AT-3** (SC-3 — pool scoping)
Given a two-pool cluster (pools `a` and `b`), DOWN backend in pool `b`;
When `vctl lb prune --pool a` is invoked;
Then `want_absent` is NOT called; pool `b` is untouched; exit 0.

**AT-4** (SC-4 — dry-run makes no mutations)
Given a DOWN backend past threshold;
When `vctl lb prune --dry-run` is invoked;
Then stderr contains `would prune <ep> from pool <pool>`;
`want_absent` is never called; state file is unchanged; exit 0.

**AT-5** (SC-5 — MAINT not pruned)
Given a backend with `status=DOWN`, `admin=maint`, `lastchg=3600`;
When `vctl lb prune` is invoked;
Then `want_absent` is NOT called; exit 0 with zero pruned.

**AT-6** (SC-6 — DRAIN not pruned)
Given a backend with `status=DOWN`, `admin=drain`, `lastchg=3600`;
When `vctl lb prune` is invoked;
Then `want_absent` is NOT called; exit 0 with zero pruned.

**AT-7** (SC-7 — `lb start` with watcher enabled spawns both sessions)
Given `prune.enabled: true` (default) in cluster.yaml;
When `vctl lb start` is invoked and `mgr.start()` succeeds;
Then `tmux_run_detached_argv` is called with session name `vctl-lb-watch` (in addition to
the HAProxy session `vctl-lb`); `~/.vctl/lb/watch.pid` is created with content
`tmux:vctl-lb-watch`; exit 0.

**AT-7b** (SC-7b — `lb start` with watcher disabled spawns only HAProxy)
Given `prune.enabled: false` in cluster.yaml;
When `vctl lb start` is invoked and `mgr.start()` succeeds;
Then `tmux_run_detached_argv` is NOT called with session name `vctl-lb-watch`;
no `watch.pid` is written; exit 0.

**AT-8** (SC-8 — `lb stop` kills both sessions)
Given the HAProxy session `vctl-lb` and watcher session `vctl-lb-watch` are running,
and `watch.pid` exists;
When `vctl lb stop` is invoked;
Then `tmux_kill("vctl-lb-watch")` is called; `watch.pid` is removed;
`tmux_kill("vctl-lb")` is also called (or `mgr.stop()` equivalent); exit 0.

**AT-9** (SC-9 — `lb status` reports watcher state)
Given the watcher session exists and `watch.pid` is present;
When `vctl lb status` is invoked;
Then output contains `watcher: running` (or equivalent); exit 0.
Given `prune.enabled: false`;
When `vctl lb status` is invoked;
Then output contains `watcher: disabled`; exit 0.

**AT-10** (SC-10 — CI gates pass)
Given the full changeset from this spec is implemented;
When `ruff check .`, `ruff format --check .`, `mypy --strict src/vctl`, and
`pytest --cov=vctl --cov-fail-under=50` are run;
Then all four commands exit 0 with no errors or warnings.

---

## Out of Scope (Phase 3)

- Cross-host prune: operator runs `vctl lb prune` on a different host than the LB
  (requires SSH loop or remote agent).
- Auto-revive: re-spawn a pruned vllm worker on a healthy node.
- Watcher alerting (PagerDuty / Slack hook on N pruned in window).
- `vctl lb prune --force` to prune MAINT backends (deliberate operator override).
- A standalone `vctl lb watch start/stop/status` sub-command group — watcher lifecycle
  is intentionally bundled into `lb start/stop/status` for simplicity.

---

*Version bump: `0.5.3` → `0.6.0` (new user-visible feature).*
