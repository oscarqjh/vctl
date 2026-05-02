# Reconciler — single owner of (haproxy, state-file) consistency for vctl — Design Spec

## Objective

We are building a `Reconciler` class that becomes the single authoritative path for keeping HAProxy's in-memory backend list and vctl's on-disk state file in sync. Today's `_do_add` / `_do_remove` / `_do_drain` / `_do_auto_add` functions in `lb_scaling.py` each manage this coordination ad-hoc, with subtly different ordering (state-first vs haproxy-first) and silent fallback behavior when the socket is unreachable. The Reconciler enforces one invariant across every mutation path: **haproxy ack must precede any state-file write**. It also provides a read-only `diff` API so higher-level commands can detect and surface drift between the two sources of truth without needing to understand the internal bookkeeping. Phase 1 is additive — the new module ships alongside the existing functions and is not yet wired into the CLI; Phase 2 (a separate PR) will migrate the callers.

**Success criteria:**

1. `Reconciler.want_present(ep, pool)` against a running LB registers `ep` in haproxy AND in the state file. The state file is never written ahead of the haproxy ack.
2. `Reconciler.want_present(ep, pool)` against a stopped LB raises `LbUnreachable`. The state file is left unchanged.
3. `Reconciler.want_absent(ep, pool)` against a running LB removes `ep` from haproxy AND from the state file. HAProxy is mutated first; the state file is mutated only after the haproxy ack.
4. Calling any mutating method twice with the same arguments produces the same final state. The second call returns `Action.NONE` or a harmless re-heal action (`Action.READIED`).
5. `Reconciler.diff(pool)` against a stopped LB returns `Drift(lb_reachable=False)` with the state-file membership populated. The call does not raise.
6. `Reconciler.reconcile_pool(pool, target)` converges haproxy and state to exactly `target`, regardless of the starting state.
7. The module is mypy-strict clean: `mypy --strict src/vctl/lb/reconciler.py` exits 0.
8. The unit test file `tests/test_lb_reconciler.py` runs in under 5 seconds and requires no real haproxy or network; all `RuntimeClient` interactions are mocked.
9. Concurrency test: 4 multiprocess workers each call `want_present` with the same endpoint. The final state file has exactly one entry; no exceptions are raised; all returned `Outcome` objects are valid (a mix of `ADDED` and `NONE`/`READIED` is acceptable).
10. Existing `_do_add` / `_do_remove` / `_do_drain` / `_do_auto_add` functions in `src/vctl/commands/lb_scaling.py` are unchanged. All existing tests pass unchanged.

---

## Tech Stack

- **Language/Runtime:** Python 3.10+
- **Framework:** argparse-based CLI; project uses `uv` for tooling and `hatchling` for build
- **Key dependencies:** pydantic v2, httpx, psutil, rich (existing project deps — Reconciler adds none new)
- **Linting/typing:** ruff (target-version py310, line-length 100, lint E,F,W,I,B,UP,SIM,N); mypy --strict over `src/vctl`
- **Testing:** pytest with `--cov-fail-under=50`

---

## Architecture

The Reconciler follows **Approach A**: a class that holds an `LbManager` reference and constructs a fresh `RuntimeClient` on every call. This matches the existing `_client(mgr)` pattern in `lb_scaling.py`, which performs a unix-socket connect with TCP fallback and returns `None` if both fail. **Phase 1 extracts that helper to `src/vctl/lb/runtime.py` as a public module-level function `lb_admin_client(mgr)` so `lb_scaling.py` and `reconciler.py` import the same canonical implementation rather than maintain duplicate copies.** Constructing a new client per call costs one socket connect — approximately one millisecond over a Unix domain socket — which is negligible at the expected mutation volume (single-digit ops per minute at most). More importantly, this approach is robust to an LB restart between calls: there is no stale cached connection to invalidate, no liveness-detection loop to maintain, and no reconnect state machine.

The class holds two references: the `LbManager` (which owns `mgr.lb`, `mgr.state_dir`, `mgr.sock_path`, and the admin TCP coordinates) and nothing else. Each method is responsible for its own client acquisition, state-file access via `BackendState`, and result production via the `Outcome` dataclass. The Reconciler does not render haproxy config, does not manage tmux, and does not resolve pool names from model IDs — those responsibilities remain with `LbManager` and `routing.py` respectively.

The architectural fix for the historical F11/F12 bugs is enforced as a single rule applied consistently: every mutating method acquires a `RuntimeClient` first; if the client is `None` (both socket paths failed) it raises `LbUnreachable` immediately without touching the state file. If the haproxy admin command raises, the state file is also left untouched and `BackendOpFailed` propagates to the caller. Only after haproxy acknowledges the operation is `BackendState.add()` or `BackendState.remove()` called. `BackendState`'s existing `fcntl.flock` protection handles concurrent writers safely; the Reconciler itself adds no cross-call lock.

---

## Components

### `src/vctl/lb/errors.py` — Exception hierarchy

- **Responsibility:** Provide the canonical exception types for all Reconciler hard failures.
- **Interface:** Imported by `reconciler.py` and by any caller that wants to `except` a specific error kind. All three error classes inherit from `ReconcilerError` so callers can catch the base class when they want a single handler.
- **Dependencies:** None (stdlib only).

Classes:
- `ReconcilerError(Exception)` — base class.
- `LbUnreachable(ReconcilerError)` — raised when both the unix socket and TCP admin port are unreachable. Carries a human-readable message including `sock_path` and `host:port`.
- `PoolNotFound(ReconcilerError)` — raised when the caller passes a pool name that does not appear in `{p.name for p in mgr.lb.pools}`.
- `BackendOpFailed(ReconcilerError)` — raised when haproxy admin returned an unexpected error response during a mutation (i.e. `RuntimeClient` raised `RuntimeError`). Carries the original exception as `__cause__`.

---

### `src/vctl/lb/reconciler.py` — Reconciler class

- **Responsibility:** The single owner of haproxy + state-file consistency. Exposes mutating methods (`want_present`, `want_absent`, `want_draining`) and bulk methods (`reconcile_pool`, `reconcile_from_state`) plus read-only methods (`diff`, `diff_all`). Also defines the `Action` enum, `Outcome` dataclass, and `Drift` dataclass.
- **Interface:** Constructed as `Reconciler(mgr)` where `mgr: LbManager`. All public methods take `pool: str` as an explicit required argument. Pool resolution is the caller's responsibility; the Reconciler only validates that the supplied name is known.
- **Dependencies:** `LbManager` (for config + socket coordinates), `BackendState` (for state-file reads and writes), `RuntimeClient` (via the `_client(mgr)` helper), `errors.py` (for exception types).

#### `Action` enum

```
class Action(enum.Enum):
    NONE = "none"
    ADDED = "added"
    REMOVED = "removed"
    DRAINED = "drained"
    READIED = "readied"
    ADOPTED = "adopted"
    ORPHANED_CLEANED = "orphaned_cleaned"
```

#### `Outcome` dataclass

Frozen dataclass with fields:
- `ep: str` — the endpoint that was acted on.
- `pool: str` — the pool name.
- `action: Action` — what happened.
- `note: str` — optional human-readable detail (default `""`).

#### `Drift` dataclass

Frozen dataclass with fields:
- `pool: str` — the pool name.
- `lb_reachable: bool` — False when the LB admin socket was unreachable at diff time.
- `only_in_state: list[str]` — endpoints present in the state file but not in haproxy.
- `only_in_haproxy: list[str]` — endpoints present in haproxy but not in the state file.
- `in_both: list[str]` — endpoints present in both sources.
- `statuses: dict[str, BackendStatus]` — keyed by endpoint; populated for all endpoints that were reachable in haproxy (i.e. `only_in_haproxy` + `in_both`).

When `lb_reachable` is `False`, `only_in_haproxy`, `in_both`, and `statuses` are all empty; `only_in_state` is populated from the state file.

#### Public API

| Method | Mutating | Raises on LB unreachable |
|---|---|---|
| `want_present(ep, pool)` | yes | yes — `LbUnreachable` |
| `want_absent(ep, pool)` | yes | yes — `LbUnreachable` |
| `want_draining(ep, pool)` | yes (haproxy only) | yes — `LbUnreachable` |
| `reconcile_pool(pool, target)` | yes | yes — `LbUnreachable` |
| `reconcile_from_state(pool)` | yes | yes — `LbUnreachable` |
| `diff(pool)` | no | no — returns `Drift(lb_reachable=False)` |
| `diff_all()` | no | no — returns list of `Drift` with `lb_reachable=False` |

**`want_present(ep: str, pool: str) -> Outcome`**
1. Validate pool via `_validate_pool`. Raises `PoolNotFound` if unknown.
2. Acquire client via `lb_admin_client(mgr)`. Raises `LbUnreachable` if `None`.
3. Read pre-state once: `in_haproxy = ep in self._haproxy_servers(backend_section)` (calls `client.show_servers_state()` and filters to this pool's section). `in_state = ep in BackendState(mgr.state_dir, mgr.lb.host, pool=pool).list()`.
4. If `not in_haproxy`: call `client.add_server(backend_section, _name_for(ep), ep)`. The method returns `Literal["new", "already_present"]` and raises `RuntimeError` only on real haproxy errors (per `RuntimeClient.add_server` contract, the `"already_present"` case is a return value, not an exception). The return value is ignored here — the pre-state read is the source of truth for the action mapping. On `RuntimeError`: raise `BackendOpFailed`.
5. Call `client.set_state(backend_section, _name_for(ep), "ready")` unconditionally (heals lingering drain/maint). On `RuntimeError`: raise `BackendOpFailed`.
6. If `not in_state`: call `BackendState(mgr.state_dir, mgr.lb.host, pool=pool).add(ep)`.
7. Return action mapping based on pre-state:
   - `not in_haproxy and not in_state` → `Action.ADDED`
   - `not in_haproxy and in_state` → `Action.READIED` (haproxy was missing the entry; we re-registered and re-readied)
   - `in_haproxy and not in_state` → `Action.ADOPTED` (haproxy already had it; state file caught up)
   - `in_haproxy and in_state` → `Action.READIED` (idempotent re-heal — set_state ready re-applied to clear any lingering drain/maint)

**`want_absent(ep: str, pool: str) -> Outcome`**
1. Validate pool. Raises `PoolNotFound` if unknown.
2. Acquire client via `lb_admin_client(mgr)`. Raises `LbUnreachable` if `None`.
3. Read pre-state once: `in_haproxy = ep in self._haproxy_servers(backend_section)`. `in_state = ep in BackendState(mgr.state_dir, mgr.lb.host, pool=pool).list()`.
4. If `not in_haproxy and not in_state`: return `Outcome(ep, pool, Action.NONE)` (nothing to do).
5. If `in_haproxy`: call `client.set_state(backend_section, _name_for(ep), "maint")`, then `client.remove_server(backend_section, _name_for(ep))`. `RuntimeClient.remove_server` already tolerates `"no such server"` silently. On other `RuntimeError`: raise `BackendOpFailed`.
6. If `in_state`: call `BackendState(mgr.state_dir, mgr.lb.host, pool=pool).remove(ep)`.
7. Return action mapping based on pre-state:
   - `in_haproxy and in_state` → `Action.REMOVED`
   - `in_haproxy and not in_state` → `Action.REMOVED` (state file was already absent; haproxy cleaned)
   - `not in_haproxy and in_state` → `Action.ORPHANED_CLEANED` (haproxy was already absent; state file cleaned)

**`want_draining(ep: str, pool: str) -> Outcome`**
1. Validate pool. Raises `PoolNotFound` if unknown.
2. Acquire client. Raises `LbUnreachable` if `None`.
3. Call `client.set_state(backend_section, _name_for(ep), "drain")`. On `RuntimeError` where message contains "no such server": raise `BackendOpFailed` (cannot drain a non-existent server). On other `RuntimeError`: raise `BackendOpFailed`.
4. State file is NOT modified (drain is a transitional haproxy admin state; state file represents intended membership).
5. Return `Outcome(ep, pool, Action.DRAINED)`.

**`reconcile_pool(pool: str, target: set[str]) -> list[Outcome]`**
1. Validate pool. Raises `PoolNotFound` if unknown.
2. Acquire client once (fail fast: raises `LbUnreachable` if `None`).
3. For each `ep` in `target`: call `want_present(ep, pool)`.
4. Query current haproxy state via `client.show_servers_state()` to find endpoints currently present but not in `target`.
5. For each ep currently present in haproxy but not in `target`: call `want_absent(ep, pool)`.
6. Return the concatenated list of all `Outcome` objects.

**`reconcile_from_state(pool: str) -> list[Outcome]`**
1. Read `BackendState(mgr.state_dir, mgr.lb.host, pool=pool).list()` to obtain the target set.
2. Delegate to `reconcile_pool(pool, set(state_entries))`.

**`diff(pool: str) -> Drift`**
1. Validate pool. On failure, raise `PoolNotFound`.
2. Read state file: `state_eps = set(BackendState(...).list())`.
3. Acquire client. If `None`: return `Drift(pool=pool, lb_reachable=False, only_in_state=sorted(state_eps), only_in_haproxy=[], in_both=[], statuses={})`.
4. Query `client.show_servers_state()`. Build `haproxy_eps = {s.endpoint for s in statuses_for_pool}` and `statuses_map = {s.endpoint: s for s in statuses_for_pool}`.
5. Compute set differences and intersection; return fully populated `Drift`.

**`diff_all() -> list[Drift]`**
1. Acquire client once (may be `None` — handled per diff call).
2. For each pool in `mgr.lb.pools`: call `diff(pool.name)`.
3. Return list of `Drift` objects.

#### Private helpers

- `_validate_pool(pool: str) -> None` — checks `pool ∈ {p.name for p in mgr.lb.pools}`; raises `PoolNotFound` otherwise.
- `_haproxy_servers(section: str) -> dict[str, BackendStatus]` — calls `client.show_servers_state()` once, filters to rows whose backend section matches `section`, returns `{endpoint: BackendStatus}`. Used by every method that needs pre-state.
- `lb_admin_client(mgr: LbManager) -> RuntimeClient | None` — extracted from `lb_scaling.py._client` and moved to `src/vctl/lb/runtime.py` as a public module-level function. Identical semantics: try unix socket (with EOPNOTSUPP/ECONNREFUSED fallthrough to TCP per the existing NFS-mirage workaround), fall back to TCP at `mgr.lb.host:mgr.lb.admin.bind_port`, return `None` if both fail. The `VCTL_TEST_NO_SOCKET=1` shortcut also applies here, returning the existing `_NoOpClient` stub. Both `lb_scaling.py` and `reconciler.py` import this single canonical version; the previous `_client` definition in `lb_scaling.py` is deleted and replaced with an import.
- `_name_for(ep: str) -> str` — canonical server name derivation: `"b_" + ep.replace(".", "_").replace(":", "_")`. This function is moved from `lb_scaling.py` to `src/vctl/lb/routing.py` (the canonical shared home) and imported by both `lb_scaling.py` and `reconciler.py`. The definition in `lb_scaling.py` is deleted and replaced with an import.

---

### `src/vctl/lb/routing.py` — `_name_for` moved here

- **Responsibility (change):** Gains `_name_for(ep: str) -> str` as a module-level function. This is a minor refactor inside Phase 1 to eliminate the duplication.
- **Interface:** `from vctl.lb.routing import _name_for` — used by `lb_scaling.py` (import replaces inline definition) and `reconciler.py` (new import).
- **Dependencies:** Unchanged.

---

### `src/vctl/lb/runtime.py` — `lb_admin_client` and `_NoOpClient` added here

- **Responsibility (change):** Gains `lb_admin_client(mgr: LbManager) -> RuntimeClient | None` as a public module-level function. Replaces the private `_client` helper in `lb_scaling.py`. Also gains the `_NoOpClient` class (moved from `lb_scaling.py` so the helper can return it when `VCTL_TEST_NO_SOCKET=1`, avoiding a circular import between `runtime.py` and `lb_scaling.py`).
- **Interface:** `from vctl.lb.runtime import lb_admin_client` — used by `lb_scaling.py` (import replaces inline `_client` definition; the `_NoOpClient` class is also re-exported from `runtime.py` so existing tests that monkeypatch `lb_scaling._client` continue to work via the import-binding).
- **Dependencies:** `LbManager` for socket path / admin host / admin port; stdlib `os` for `VCTL_TEST_NO_SOCKET` env check.

---

## Data Flow

### Successful `want_present(ep="10.0.0.5:8000", pool="default")`

1. Caller constructs `Reconciler(mgr)` and calls `want_present("10.0.0.5:8000", "default")`.
2. `_validate_pool("default")` checks `mgr.lb.pools` — pool exists, no exception.
3. `_client(mgr)` tries `mgr.sock_path` (unix socket connect), succeeds; returns `RuntimeClient`.
4. `client.add_server("pool_default", "b_10_0_0_5_8000", "10.0.0.5:8000")` sends `add server pool_default/b_10_0_0_5_8000 10.0.0.5:8000 check` to haproxy. HAProxy responds `New server registered.`. `add_server` returns `"new"`.
5. `client.set_state("pool_default", "b_10_0_0_5_8000", "ready")` sends `set server pool_default/b_10_0_0_5_8000 state ready`. HAProxy acks.
6. `BackendState(mgr.state_dir, mgr.lb.host, pool="default").add("10.0.0.5:8000")` acquires flock, appends entry, atomically writes file, releases flock. Returns `"new"`.
7. Returns `Outcome(ep="10.0.0.5:8000", pool="default", action=Action.ADDED, note="")`.

### Failing `want_present` — haproxy admin error

1. Steps 1–3 succeed.
2. `client.add_server(...)` raises `RuntimeError("haproxy add_server failed: no such backend")`.
3. Reconciler catches the error, constructs `BackendOpFailed` with the `RuntimeError` as `__cause__`, and raises it.
4. `BackendState.add()` is **never called**. State file is untouched.
5. Caller receives `BackendOpFailed`.

### `want_present` against stopped LB — `LbUnreachable`

1. `_validate_pool` succeeds.
2. `_client(mgr)` tries unix socket — `OSError` (socket file does not exist or connection refused). Falls through to TCP. `RuntimeClient.for_tcp(host, port)` raises `OSError`. Returns `None`.
3. Reconciler raises `LbUnreachable("admin socket and TCP both unreachable: sock=..., tcp=host:port")`.
4. State file is untouched.

### `diff(pool)` against stopped LB

1. `_validate_pool` succeeds.
2. `BackendState(...).list()` reads the state file. Returns `["10.0.0.5:8000", "10.0.0.6:8000"]`.
3. `_client(mgr)` returns `None`.
4. Returns `Drift(pool="default", lb_reachable=False, only_in_state=["10.0.0.5:8000", "10.0.0.6:8000"], only_in_haproxy=[], in_both=[], statuses={})`.
5. No exception raised. Caller (e.g. `vctl lb info`) can render `[LB STOPPED]` next to the endpoints.

---

## Error Handling

### Input validation — `PoolNotFound`

Every public method begins with `_validate_pool(pool)`. If the supplied pool name is not in `{p.name for p in mgr.lb.pools}`, `PoolNotFound` is raised immediately, before any socket or state-file access. This surfaces misconfigured callers early with a clear message listing available pools.

### LB unreachable — `LbUnreachable` (mutating ops) / `Drift(lb_reachable=False)` (read-only ops)

Mutating operations (`want_present`, `want_absent`, `want_draining`, `reconcile_pool`, `reconcile_from_state`) treat a `None` return from `_client(mgr)` as a hard error and raise `LbUnreachable` before touching the state file. This is a deliberate departure from the existing `_do_add` behavior, which silently writes to the state file when the socket is down; the Reconciler considers silent split-brain worse than a visible error.

Read-only operations (`diff`, `diff_all`) catch the `None` client and return `Drift(lb_reachable=False)` with the state-file membership populated. This allows `vctl lb info` to surface stale state without entering an exception path.

### HAProxy admin errors — `BackendOpFailed`

If `RuntimeClient.add_server`, `remove_server`, or `set_state` raises a `RuntimeError` for any reason other than an idempotency token ("already exists", "no such server" on removal), the Reconciler wraps it in `BackendOpFailed` and re-raises. The state file is never written. The haproxy server is left in whatever partial state the failed command left it — the caller must retry or investigate.

### Idempotent re-application

`RuntimeClient.add_server` already returns `"already_present"` when the server exists; `RuntimeClient.remove_server` already tolerates "no such server". `want_present` always calls `set_state("ready")` after `add_server`, so a second call on an already-present server re-heals any lingering drain/maint and returns `Action.READIED`. `want_absent` returns `Action.NONE` when the ep is not in the state file. `reconcile_pool` is idempotent by construction: all its mutations are delegated to idempotent leaf methods.

### Concurrency

The Reconciler holds no cross-call lock. Concurrent `want_present` calls for the same endpoint are safe because:
- `BackendState.add()` is protected by `fcntl.flock`; concurrent writers produce a single deduplicated entry.
- `RuntimeClient.add_server` returns `"already_present"` if the server already exists; this is not treated as an error.

Concurrent `want_absent` calls are similarly safe: the second `remove_server` call receives "no such server" (tolerated); the second `BackendState.remove()` is a no-op (returns `False`).

---

## Testing Strategy

- **Framework:** pytest
- **Unit tests:** `tests/test_lb_reconciler.py`
  - All `RuntimeClient` interactions are mocked via `monkeypatch.setattr(reconciler, "lb_admin_client", lambda m: mock_or_None)`, following the same pattern established in `tests/test_commands_lb_scaling.py` (which currently patches `lb_scaling._client` and continues to do so since `_client` is now an alias for the imported `lb_admin_client`).
  - Every public method has at minimum: one happy-path test and one failure-mode test (LB unreachable for mutating ops; haproxy error for BackendOpFailed path).
  - Concurrency test uses `mp.get_context("spawn").Pool`, matching the pattern in `tests/test_lb_state.py`. Four workers each call `want_present` with the same endpoint. Final state file must have exactly one entry; all outcomes must be valid `Outcome` objects.
  - Total runtime target: under 5 seconds (no network, no real haproxy).
- **Integration tests:** `tests/test_lb_reconciler_integration.py`
  - Marked with `@pytest.mark.integration`.
  - Requires a real haproxy process launched via `LbManager.start()` so the session-scoped sweeper in `conftest.py` can clean up on failure.
  - Tests `want_present` / `want_absent` / `reconcile_pool` against a live haproxy in a pytest `tmp_path`.
- **Coverage:** new module is subject to the project-wide `--cov-fail-under=50` gate; the unit test suite is expected to exceed this threshold comfortably.
- **Typing:** `mypy --strict src/vctl/lb/reconciler.py` must exit 0 as part of the definition of done.

---

## Boundaries

**Always do:**
- Acquire `RuntimeClient` before any state-file write in every mutating method.
- Raise `LbUnreachable` (not return a sentinel or log-and-continue) when the client is `None` in a mutating context.
- Return `Drift(lb_reachable=False)` (not raise) when the client is `None` in a read-only context.
- Call `set_state("ready")` after every successful `add_server` in `want_present` to heal lingering drain/maint.
- Leave the state file untouched when any haproxy admin command raises a non-idempotency error.
- Validate the pool name at the start of every public method.
- Import `_name_for` from `src/vctl/lb/routing.py` — never redefine it.
- Import `lb_admin_client` from `src/vctl/lb/runtime.py` — never redefine the unix→TCP fallback logic in another module.
- Keep all existing `_do_add` / `_do_remove` / `_do_drain` / `_do_auto_add` / `_do_attach` / `_do_detach` functions in `lb_scaling.py` unchanged.

**Ask first:**
- Any change to the state-file schema (currently flat `host:port` lines).
- Any change that gives the Reconciler a persistent long-lived socket or introduces a module-level lock.
- Adding a new public CLI command or flag backed by the Reconciler (Phase 2 work).
- Wiring the Reconciler into existing callers (`_do_add` etc.) — this is Phase 2 and requires a separate PR.

**Never do:**
- Write to the state file before receiving a haproxy ack.
- Silently swallow `LbUnreachable` in a mutating method (no silent state-file staging).
- Duplicate `LbManager` responsibilities: do not render haproxy config, do not manage tmux sessions, do not probe `/v1/models` for pool resolution.
- Add new third-party dependencies.
- Export the `Reconciler` through `vctl.__all__` in Phase 1 (the module is internal).
- Modify or bypass `BackendState`'s flock — rely on it, never replace it.

---

## Acceptance Tests

- [ ] `test: want_present registers ep in both haproxy and state file`
      Given: a running LB (mocked `_client` returning a `MagicMock` RuntimeClient) and an empty state file
      When: `Reconciler(mgr).want_present("10.0.0.5:8000", "default")` is called
      Then: `mock_client.add_server` was called with `("pool_default", "b_10_0_0_5_8000", "10.0.0.5:8000")`; `mock_client.set_state` was called with `("pool_default", "b_10_0_0_5_8000", "ready")`; the state file contains `"10.0.0.5:8000"`; the returned `Outcome.action` is `Action.ADDED`

- [ ] `test: want_present raises LbUnreachable and leaves state file untouched`
      Given: a stopped LB (mocked `_client` returning `None`) and an empty state file
      When: `Reconciler(mgr).want_present("10.0.0.5:8000", "default")` is called
      Then: `LbUnreachable` is raised; the state file does not exist or remains empty

- [ ] `test: want_absent removes ep from haproxy first then from state file`
      Given: a running LB and a state file containing `"10.0.0.5:8000"` for pool `"default"`
      When: `Reconciler(mgr).want_absent("10.0.0.5:8000", "default")` is called
      Then: `mock_client.set_state` was called with `("pool_default", "b_10_0_0_5_8000", "maint")` before `BackendState.remove`; `mock_client.remove_server` was called with `("pool_default", "b_10_0_0_5_8000")`; the state file no longer contains `"10.0.0.5:8000"`; returned `Outcome.action` is `Action.REMOVED`

- [ ] `test: mutating methods are idempotent — second call returns NONE or READIED`
      Given: a running LB and a state file already containing `"10.0.0.5:8000"` for pool `"default"` (first `want_present` already applied)
      When: `Reconciler(mgr).want_present("10.0.0.5:8000", "default")` is called a second time
      Then: no exception is raised; the state file still contains exactly one entry for `"10.0.0.5:8000"`; the returned `Outcome.action` is `Action.NONE` or `Action.READIED`

- [ ] `test: diff returns Drift with lb_reachable=False and state membership populated`
      Given: a stopped LB (mocked `_client` returning `None`) and a state file containing `["10.0.0.5:8000", "10.0.0.6:8000"]` for pool `"default"`
      When: `Reconciler(mgr).diff("default")` is called
      Then: no exception is raised; the returned `Drift.lb_reachable` is `False`; `Drift.only_in_state` equals `["10.0.0.5:8000", "10.0.0.6:8000"]` (sorted); `Drift.only_in_haproxy` is `[]`; `Drift.statuses` is `{}`

- [ ] `test: reconcile_pool converges haproxy and state to target set`
      Given: a running LB with haproxy currently reporting `["10.0.0.5:8000", "10.0.0.7:8000"]` in pool `"default"`, and a state file containing `["10.0.0.5:8000", "10.0.0.6:8000"]`
      When: `Reconciler(mgr).reconcile_pool("default", {"10.0.0.5:8000", "10.0.0.6:8000"})` is called
      Then: `want_present` was invoked for `"10.0.0.5:8000"` and `"10.0.0.6:8000"`; `want_absent` was invoked for `"10.0.0.7:8000"`; the state file contains exactly `["10.0.0.5:8000", "10.0.0.6:8000"]`; `"10.0.0.7:8000"` is removed from haproxy

- [ ] `test: module passes mypy --strict`
      Given: the file `src/vctl/lb/reconciler.py` as written
      When: `mypy --strict src/vctl/lb/reconciler.py` is executed in the project environment
      Then: the command exits with code 0 and produces no error output

- [ ] `test: unit test suite runs without real haproxy and completes in under 5 seconds`
      Given: `tests/test_lb_reconciler.py` with all `RuntimeClient` calls mocked via `monkeypatch.setattr(reconciler, "lb_admin_client", ...)`
      When: `pytest tests/test_lb_reconciler.py` is run with no network and no haproxy process
      Then: all tests pass; total wall-clock time is under 5 seconds

- [ ] `test: concurrent want_present with same ep produces exactly one state-file entry`
      Given: 4 worker processes spawned via `mp.get_context("spawn").Pool`, a running LB (VCTL_TEST_NO_SOCKET=1 so haproxy calls are no-ops), and an empty state file
      When: all 4 workers call `Reconciler(mgr).want_present("10.0.0.5:8000", "default")` concurrently
      Then: no worker raises an exception; the final state file contains exactly one entry `"10.0.0.5:8000"`; all returned `Outcome` objects have `ep == "10.0.0.5:8000"` and `action` in `{Action.ADDED, Action.NONE, Action.READIED}`

- [ ] `test: existing lb_scaling functions are unchanged and all existing tests pass`
      Given: the Phase 1 Reconciler module added to the codebase; `_do_add`, `_do_remove`, `_do_drain`, `_do_auto_add` in `lb_scaling.py` unchanged except for (a) replacing the `_name_for` definition with `from vctl.lb.routing import _name_for`, and (b) replacing the `_client` definition with `from vctl.lb.runtime import lb_admin_client as _client` (preserves the existing `lb_scaling._client` symbol so `monkeypatch.setattr(lb_scaling, "_client", ...)` patterns in current tests keep working without modification)
      When: `pytest tests/test_commands_lb_scaling.py tests/test_lb_state.py` is run
      Then: all tests pass with exit code 0; no test is modified or skipped
