# Reconciler Phase 2 — migrate scaling verbs — Design Spec

## Objective

Phase 2 migrates the six scaling verb functions in `src/vctl/commands/lb_scaling.py` — `_do_add`, `_do_remove`, `_do_drain`, `_do_auto_add`, `_do_remove_cli`, and `_do_detach` — to delegate all state mutations to the `Reconciler` module shipped in Phase 1. The motivation is to close two long-standing bugs: F11 (state file written before haproxy ack in `_do_add`, enabling split-brain if haproxy then refuses) and F12 (haproxy failures silently suppressed in `_do_auto_add` via `contextlib.suppress`, masking broken pool registrations). By routing every mutation through `Reconciler`, the single invariant enforced there — haproxy ack must precede any state-file write — applies uniformly across all CLI verbs. This migration also introduces consistent exit-code semantics (exit 4 for LB-unreachable across all mutating verbs) and replaces legacy ad-hoc `(new)` / `(already present)` stderr strings with `Outcome.action.name` values that carry the full four-case distinction.

**Success criteria:**

1. `vctl lb add <ep>` against a running LB produces exit 0, registers ep in haproxy AND state file, and prints a stderr line containing `Action.ADDED` (or `READIED` / `ADOPTED` for re-applications and orphan adoption).
2. `vctl lb add <ep>` against a stopped LB exits 4 with a stderr message identifying the unreachable LB; the state file remains unchanged.
3. `vctl lb remove <ep>` against a running LB produces exit 0, removes ep from haproxy first then state file, and surfaces `Action.REMOVED` (or `ORPHANED_CLEANED` if state-only).
4. `vctl lb remove <ep>` against a stopped LB exits 4; state file unchanged.
5. `vctl lb drain <ep>` against a running LB produces exit 0 and surfaces `Action.DRAINED`; the state file is unchanged.
6. `vctl lb drain <ep>` against a stopped LB exits 4.
7. `vctl lb auto-add` against a running LB with healthy pools produces exit 0; per-pool outcomes are logged. Against an LB with one failing pool, exits 1 with stderr identifying the failed pool (closes F12).
8. `vctl lb add <ep> --pool nonexistent` exits 3 with stderr listing available pool names.
9. F11 is closed: a code audit of `lb_scaling.py` confirms no `bs.add(ep)` or `bs.remove(ep)` call precedes any haproxy admin call across all six migrated verbs. The state-file write only happens via Reconciler delegation.
10. F12 is closed: a code audit confirms no `contextlib.suppress(Exception)` wraps any haproxy admin call in `_do_auto_add` or `_do_add` after migration.
11. `mypy --strict src/vctl` exits 0; all 406+ existing tests pass after fixture updates; new exit-4 tests added for verbs that lack one today — `_do_add`, `_do_remove`, `_do_auto_add`, `_do_remove_cli`, `_do_detach` (5 new tests). The existing A4 test for `_do_drain`'s exit-4 path is verified to still pass (no new test needed for drain).
12. Version is `0.3.0` in both `pyproject.toml` and `src/vctl/__init__.py`. `CHANGELOG.md` has a `[0.3.0]` section documenting the breaking exit-code changes and F11/F12 closure.

---

## Tech Stack

- **Language/Runtime:** Python 3.10+
- **Framework:** argparse-based CLI; project uses `uv` for tooling and `hatchling` for build
- **Key dependencies:** pydantic v2, httpx, psutil, rich (existing project deps — Phase 2 adds none new)
- **Linting/typing:** ruff (target-version py310, line-length 100, lint E,F,W,I,B,UP,SIM,N); mypy --strict over `src/vctl`
- **Testing:** pytest with `--cov-fail-under=50`

---

## Architecture

Phase 2 uses a **wrapper-only** approach: each `_do_X` function keeps its existing argparse integration, pool-resolution logic, and stderr formatting responsibilities unchanged. The function body shrinks to a `try/except` block that calls the appropriate `Reconciler` method and then prints a result line derived from `Outcome.action.name`. This preserves all six function signatures, keeps `lb_scaling.dispatch` routing untouched, and limits the blast radius of any regression to the individual verb being migrated — each verb can be reverted independently without touching the others.

A single shared helper `_exit_for(exc: ReconcilerError) -> int` is added near the top of `lb_scaling.py` alongside the helper imports. It maps the three `ReconcilerError` subclasses to exit codes: `LbUnreachable → 4`, `PoolNotFound → 3`, `BackendOpFailed → 1`. Every migrated verb body catches `ReconcilerError` and calls `_exit_for(exc)` after printing a descriptive stderr message, ensuring that the exit-code policy is defined in exactly one place and each verb body remains mechanical.

The approach was chosen over an inline replacement strategy (which would have deleted the `_do_X` wrappers and routed `dispatch` directly to thin Reconciler shims) because the wrapper-only approach requires only minor test updates — format strings and exit-code expectations — rather than a full rewrite of every verb test. The Reconciler already shrinks each verb body to roughly ten lines; the additional token savings from removing the wrappers do not justify the test churn or the larger diff surface.

---

## Components

### `_exit_for(exc: ReconcilerError) -> int` — shared exit-code helper

Added to `src/vctl/commands/lb_scaling.py` near the existing helper imports, before any `_do_X` definition.

```python
from vctl.lb.errors import BackendOpFailed, LbUnreachable, PoolNotFound, ReconcilerError

def _exit_for(exc: ReconcilerError) -> int:
    if isinstance(exc, LbUnreachable):
        return 4
    if isinstance(exc, PoolNotFound):
        return 3
    return 1  # BackendOpFailed and any future ReconcilerError subclass
```

---

### `_do_add(ep, mgr, bs, pool_name=None)` — migration

**Before:** resolves pool name, calls `bs.add(ep)` first (state-first — F11 root cause), then calls `cli.add_server` / `cli.set_state`, rolls back `bs.remove(ep)` on failure.

**After:** resolves pool name via `_resolve_pool_name` (unchanged), then:

```python
try:
    outcome = Reconciler(mgr).want_present(ep, pool_name)
    print(f"add {ep} {outcome.action.name} (pool: {pool_name})", file=sys.stderr)
    return 0
except ReconcilerError as exc:
    print(f"add {ep} failed: {exc}", file=sys.stderr)
    return _exit_for(exc)
```

The manual state-rollback logic and the `(new)` / `(already present)` strings are dropped. `Reconciler.want_present` enforces haproxy-first ordering and closes F11 by construction.

---

### `_do_remove(ep, mgr, bs, pool_name=None)` — migration

**Before:** acquires `_client`, guards on `None` (returns 4), then calls `cli.set_state("maint")` → `cli.remove_server` → `bs.remove(ep)` with manual per-step exception handling.

**After:** resolves pool name via `_resolve_pool_name` (unchanged), then:

```python
try:
    outcome = Reconciler(mgr).want_absent(ep, pool_name)
    print(f"remove {ep} {outcome.action.name} (pool: {pool_name})", file=sys.stderr)
    return 0
except ReconcilerError as exc:
    print(f"remove {ep} failed: {exc}", file=sys.stderr)
    return _exit_for(exc)
```

The manual maint-then-del-then-state-remove ordering is dropped; `Reconciler.want_absent` enforces it. The haproxy-only-no-state path (previously a special branch in `_do_remove_cli`) is handled automatically by `Reconciler.want_absent` returning `Action.ORPHANED_CLEANED`.

---

### `_do_drain(ep, mgr, pool_name=None)` — migration

**Before:** acquires `_client`, guards on `None` (returns 4), calls `cli.set_state("drain")`.

**After:** resolves pool name via `_resolve_pool_name` (unchanged), then:

```python
try:
    outcome = Reconciler(mgr).want_draining(ep, pool_name)
    print(f"drain {ep} {outcome.action.name} (pool: {pool_name})", file=sys.stderr)
    return 0
except ReconcilerError as exc:
    print(f"drain {ep} failed: {exc}", file=sys.stderr)
    return _exit_for(exc)
```

**State-file contract:** Phase 1 sealed `Reconciler.want_draining` as state-file-read-only — drain is a transitional haproxy admin state, the state file represents intended membership, drained backends remain members. Success Criterion 5 ("the state file is unchanged") is satisfied by Phase 1's contract; Phase 2 inherits it. See `src/vctl/lb/reconciler.py:want_draining` docstring.

---

### `_do_auto_add(mgr, bs)` — migration

**Before:** acquires `_client`, iterates pools, iterates endpoints per pool, calls `cli.add_server` and `cli.set_state("ready")` both wrapped in `contextlib.suppress(Exception)` — F12 root cause.

**After:** iterates pools (state-file pool list, fall back to configured pools), calls `Reconciler(mgr).reconcile_from_state(pool)` per pool. Collects failures; exits 1 if any pool reconcile failed with stderr identifying the pool.

```python
pool_names = BackendState.list_pools(bs.state_dir, bs.lb_host)
if not pool_names:
    pool_names = [p.name for p in mgr.lb.pools]
failed: list[str] = []
for pname in pool_names:
    try:
        outcomes = Reconciler(mgr).reconcile_from_state(pname)
        for outcome in outcomes:
            print(f"auto-add {outcome.ep} {outcome.action.name} (pool: {pname})", file=sys.stderr)
    except ReconcilerError as exc:
        print(f"auto-add pool {pname!r} failed: {exc}", file=sys.stderr)
        failed.append(pname)
return 1 if failed else 0
```

`contextlib.suppress` is removed. Any `ReconcilerError` for a pool is surfaced on stderr and accumulated; execution continues with remaining pools so a single broken pool does not prevent recovery of healthy ones. Closes F12.

---

### `_do_remove_cli(ep, mgr, bs)` — migration

**Before:** scans state-file pools for ep; if found, delegates to `_do_remove`; if not found, falls back to a legacy haproxy-only branch using `_client(mgr)` directly.

**After:** scans state-file pools to find the first pool whose state file contains the ep (cheap, no socket). On a hit, calls `Reconciler(mgr).want_absent(ep, pool)` for that pool and returns. The same ep appearing in multiple pool state files is unexpected (vctl's design: one ep maps to one pool); if it does happen, the first match wins and the duplicates would be cleaned up by a subsequent invocation or by `vctl lb auto-add` reconciling drift. If absent from all state files, iterates configured pools and calls `Reconciler(mgr).want_absent(ep, pool)` on each — idempotent: returns `Action.NONE` if not present anywhere, `Action.ORPHANED_CLEANED` if state-only, `Action.REMOVED` if haproxy had it. The fallback loop returns `_exit_for(exc)` on the first ReconcilerError encountered so a propagating `LbUnreachable` surfaces as exit 4 immediately rather than masking under the not-found path; this is intentional and differs from `_do_auto_add`'s accumulate-and-continue model because remove-cli is a single-ep operation, not a bulk reconcile.

```python
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

The legacy haproxy-only fallback branch that used `_client(mgr)` directly is dropped. `Reconciler.want_absent` already handles the haproxy-only-no-state case as `Action.ORPHANED_CLEANED`. Note: the `_NoOpClient` and `_client` imports are retained in `lb_scaling.py` because the `__all__` export and `monkeypatch.setattr(lb_scaling, "_client", ...)` patterns in existing tests depend on them.

---

### `_do_detach(mgr, bs)` — migration

**Before:** acquires `_client`, guards on `None` (returns 4), calls `cli.set_state("drain")` in a `contextlib.suppress` block, then polls `probe_local_vllm` for idle, then delegates to `_do_remove`.

**After:** scans state-file pools for endpoint matching `self_ip` (unchanged). For the matching ep:
1. Calls `Reconciler(mgr).want_draining(ep, pool)` — replaces the direct `cli.set_state` call.
2. Polls `probe_local_vllm` for idle (drain-wait stays in caller, not absorbed by Reconciler). Timeout via `LB_DETACH_WAIT` env var is preserved.
3. Calls `Reconciler(mgr).want_absent(ep, pool)` — replaces the `_do_remove` delegation.

```python
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

The `LB_DETACH_WAIT` timeout and the `probe_local_vllm` poll loop are application-level concerns and remain in the caller.

---

## Data Flow

### Successful `vctl lb add 10.0.0.5:8000` (happy path)

1. `dispatch` routes to `_do_add_cli("10.0.0.5:8000", mgr, bs, None)`.
2. Single-pool config: `pool_name = mgr.lb.pools[0].name` = `"default"`.
3. `_do_add("10.0.0.5:8000", mgr, pbs, pool_name="default")` is called.
4. `_resolve_pool_name(mgr, "default")` validates the pool name — passes.
5. `Reconciler(mgr).want_present("10.0.0.5:8000", "default")` is called.
6. Reconciler acquires `RuntimeClient` via `lb_admin_client(mgr)` — socket connects successfully.
7. `client.show_servers_state()` returns empty list — ep is not in haproxy, not in state file.
8. `client.add_server("pool_default", "b_10_0_0_5_8000", "10.0.0.5:8000")` — haproxy acks.
9. `client.set_state("pool_default", "b_10_0_0_5_8000", "ready")` — haproxy acks.
10. `BackendState.add("10.0.0.5:8000")` — state file written atomically.
11. Returns `Outcome(ep="10.0.0.5:8000", pool="default", action=Action.ADDED)`.
12. `_do_add` prints `add 10.0.0.5:8000 ADDED (pool: default)` to stderr; returns 0.

### `vctl lb add 10.0.0.5:8000` against stopped LB (exit 4)

1. Steps 1–4 as above.
2. `Reconciler(mgr).want_present("10.0.0.5:8000", "default")` is called.
3. `lb_admin_client(mgr)` tries unix socket — `OSError`. Falls through to TCP — `OSError`. Returns `None`.
4. Reconciler raises `LbUnreachable(sock=..., tcp=...)`.
5. `_do_add` catches `ReconcilerError`. Prints `add 10.0.0.5:8000 failed: LB admin socket unreachable: sock=..., tcp=...` to stderr. Returns `_exit_for(exc)` = 4.
6. State file is not touched at any point — no `bs.add(ep)` was called before the haproxy attempt.

---

## Error Handling

### Exit-code mapping (authoritative for v0.3.0)

| Exit code | Meaning | Produced by |
|---|---|---|
| 0 | Success | all verbs on happy path |
| 1 | Generic failure / haproxy admin error | `BackendOpFailed`, `_do_auto_add` pool failure, `_do_remove_cli` ep not found |
| 2 | Config error | argparse / config load (not produced by these verbs) |
| 3 | User error (unknown pool, ambiguous routing) | `PoolNotFound` via `_exit_for` |
| 4 | Environment error — LB unreachable | `LbUnreachable` via `_exit_for` — NEW for `lb add` / `lb drain`; was silent state-file write or exit 0 |

### `_exit_for(exc: ReconcilerError) -> int` mapping

- `LbUnreachable` → 4
- `PoolNotFound` → 3
- `BackendOpFailed` (and any future `ReconcilerError` subclass) → 1

### Breaking behavior changes (version bump to v0.3.0)

- **`lb add` / `lb remove` / `lb drain` against stopped LB now exits 4.** Previously, `_do_add` silently wrote to the state file (staging the ep) and returned 0; `_do_remove` and `_do_drain` already exited 4, but `_do_add` did not. After Phase 2, all three verbs consistently exit 4 with a clear stderr message and leave the state file unchanged. Operators that relied on `lb add` for offline pre-population (LB stopped) must run `lb start` first, then `lb add`.
- **`lb auto-add` no longer silently suppresses haproxy failures.** Previously, `contextlib.suppress(Exception)` wrapped every `cli.add_server` and `cli.set_state` call, masking broken pool registrations (F12). After Phase 2, any `ReconcilerError` for a pool is surfaced on stderr and causes exit 1. Processing continues for remaining pools.
- **`lb add` no longer writes the state file when haproxy refuses (F11 closed).** Previously, `bs.add(ep)` was called before the haproxy admin command; rollback on failure was attempted but not guaranteed atomic. After Phase 2, `Reconciler.want_present` enforces haproxy-ack-first ordering.
- **stderr output format change.** `(new)` / `(already present)` strings are replaced by `Outcome.action.name` values (`ADDED`, `READIED`, `ADOPTED`, `REMOVED`, `ORPHANED_CLEANED`, `DRAINED`). Existing tests asserting on legacy strings must be updated to match the new format.

---

## Testing Strategy

- **Framework:** pytest
- **Test location:** `tests/test_commands_lb_scaling.py` (existing file — update format/exit-code expectations; add new tests)
- **Existing test updates required:**
  - `test_lb_add_idempotent_first_then_dup`: change `"(new)"` assertion to `"ADDED"` and `"(already present)"` to `"READIED"`.
  - `test_lb_add_with_explicit_pool_flag`: change `"(new)"` assertion to `"ADDED"`.
  - Tests in `A5` group (e.g. `test_do_add_propagates_haproxy_error`) that use `monkeypatch.setattr(lb_scaling, "_client", ...)` to simulate haproxy errors now need to patch at the Reconciler level via `monkeypatch.setattr(reconciler_mod, "lb_admin_client", ...)` or inject a `MagicMock` client that raises `RuntimeError` (which Reconciler wraps to `BackendOpFailed`).
  - `test_do_auto_add_calls_force_ready` and `test_do_auto_add_force_ready_called_even_when_add_raises`: the assertion that `set_state("ready")` is called is satisfied by `Reconciler.want_present`'s unconditional `set_state("ready")` call; test may need to patch at the Reconciler level.
  - `test_do_remove_cli_returns_0_when_haproxy_cleanup_succeeds`: the legacy haproxy-only branch is replaced by `Reconciler.want_absent` returning `Action.ORPHANED_CLEANED`; test assertions remain valid but the mock setup changes to inject via `lb_admin_client`.
- **New tests required (at least 6 — one per migrated verb):**
  - `test_do_add_exits_4_when_lb_unreachable` — `_do_add` with `lb_admin_client` returning `None`; assert return code 4 and state file unchanged.
  - `test_do_remove_exits_4_when_lb_unreachable` — `_do_remove` with `lb_admin_client` returning `None`; assert return code 4.
  - `test_do_drain_exits_4_when_lb_unreachable` — already exists (`A4`); verify it passes after migration.
  - `test_do_auto_add_exits_1_when_pool_reconcile_fails` — `_do_auto_add` with `lb_admin_client` returning `None`; assert return code 1 and stderr identifies the failed pool. (Closes F12 regression test.)
  - `test_do_remove_cli_exits_4_when_ep_found_in_state_but_lb_unreachable` — ep in state file, `lb_admin_client` returns `None`; assert return code 4.
  - `test_do_detach_exits_4_when_lb_unreachable` — `_do_detach` with `lb_admin_client` returning `None` and state file containing a matching ep; assert return code 4.
- **Coverage expectations:** every migrated verb has at least one happy-path test and one `LbUnreachable` test. `PoolNotFound` (exit 3) is covered by the existing `test_lb_add_unknown_pool_exits_3` which continues to pass unchanged (pool-resolution happens before the Reconciler call, via `_resolve_pool_name` / `_do_add_cli`).
- **Typing:** `mypy --strict src/vctl` must exit 0 over the migrated `lb_scaling.py` as part of the definition of done.

---

## Boundaries

**Always do:**
- Catch `ReconcilerError` subclasses in every migrated verb body; never let them escape to argparse.
- Map exit codes via `_exit_for(exc)` — do not inline exit-code logic in individual verb bodies.
- Print a descriptive stderr message before returning the exit code.
- Surface `Outcome.action.name` (not legacy `(new)` / `(already present)` strings) in success messages.
- Keep `_resolve_pool_name` calls in `_do_add`, `_do_remove`, `_do_drain` — pool validation still happens at the caller layer, before the Reconciler call.

**Ask first:**
- Any change to function signatures of `_do_add`, `_do_remove`, `_do_drain`, `_do_auto_add`, `_do_remove_cli`, `_do_detach` — Phase 2 is a body-replacement only.
- Any change to `lb_scaling.dispatch` routing logic.
- Any change to the Reconciler module itself — Phase 1 is sealed.
- Any change to `BackendState` schema or file layout.

**Never do:**
- Write to the state file directly in any migrated verb (`bs.add(ep)` / `bs.remove(ep)`). Reconciler owns all state mutations.
- Use `contextlib.suppress` around any haproxy admin call — this was the F12 root cause.
- Re-introduce state-first ordering (calling `bs.add` before the haproxy command) — this was the F11 root cause.
- Remove the `_NoOpClient` and `_client` (aliased `lb_admin_client`) imports from `lb_scaling.py` — they are needed for the `__all__` export and existing test monkeypatching.
- Absorb the drain-wait poll loop (`probe_local_vllm` / `LB_DETACH_WAIT`) into the Reconciler — it is an application-level concern and stays in `_do_detach`.
- Add new third-party dependencies.

---

## Acceptance Tests

- [x] `test: lb add against running LB exits 0 and prints ADDED action in stderr`
      Given: a running LB (mocked `lb_admin_client` returning a `MagicMock` `RuntimeClient` that reports empty `show_servers_state`), an empty state file, and a single configured pool named `"default"`
      When: `vctl lb add 10.0.0.5:8000` is invoked (or `_do_add("10.0.0.5:8000", mgr, bs, pool_name="default")` is called directly)
      Then: return code is 0; stderr contains `ADDED`; the state file contains `"10.0.0.5:8000"`; `mock_client.add_server` and `mock_client.set_state` were each called once

- [x] `test: lb add against stopped LB exits 4 and leaves state file unchanged`
      Given: a stopped LB (mocked `lb_admin_client` returning `None`), an empty state file
      When: `_do_add("10.0.0.5:8000", mgr, bs, pool_name="default")` is called
      Then: return code is 4; the state file does not contain `"10.0.0.5:8000"` (unchanged); stderr contains a message referencing the unreachable LB

- [x] `test: lb remove against running LB exits 0 and surfaces REMOVED action`
      Given: a running LB (mocked `lb_admin_client` returning a `MagicMock` `RuntimeClient` that reports `10.0.0.5:8000` in `show_servers_state`), a state file containing `"10.0.0.5:8000"`
      When: `_do_remove("10.0.0.5:8000", mgr, bs, pool_name="default")` is called
      Then: return code is 0; stderr contains `REMOVED`; the state file no longer contains `"10.0.0.5:8000"`; `mock_client.set_state` was called with `maint` before `mock_client.remove_server`

- [x] `test: lb remove against stopped LB exits 4 and leaves state file unchanged`
      Given: a stopped LB (mocked `lb_admin_client` returning `None`), a state file containing `"10.0.0.5:8000"`
      When: `_do_remove("10.0.0.5:8000", mgr, bs, pool_name="default")` is called
      Then: return code is 4; the state file still contains `"10.0.0.5:8000"` (unchanged); stderr contains a message referencing the unreachable LB

- [x] `test: lb drain against running LB exits 0 and surfaces DRAINED action`
      Given: a running LB (mocked `lb_admin_client` returning a `MagicMock` `RuntimeClient`), a single configured pool
      When: `_do_drain("10.0.0.5:8000", mgr, pool_name="default")` is called
      Then: return code is 0; stderr contains `DRAINED`; `mock_client.set_state` was called with `"drain"`; the state file is unchanged

- [x] `test: lb drain against stopped LB exits 4`
      Given: a stopped LB (mocked `lb_admin_client` returning `None`)
      When: `_do_drain("10.0.0.5:8000", mgr, pool_name="default")` is called
      Then: return code is 4; stderr contains a message referencing the unreachable LB

- [x] `test: lb auto-add exits 1 and identifies failed pool when LB unreachable`
      Given: a stopped LB (mocked `lb_admin_client` returning `None`), a state file for pool `"default"` containing `"10.0.0.5:8000"`
      When: `_do_auto_add(mgr, bs)` is called
      Then: return code is 1; stderr contains the pool name `"default"` in the failure message (closes F12 regression)

- [x] `test: lb add with unknown pool exits 3 and lists available pools`
      Given: a two-pool LB config with pools `"a"` and `"b"` (VCTL_TEST_NO_SOCKET=1); a nonexistent pool `"nonexistent"`
      When: `vctl lb add 10.0.0.5:8000 --pool nonexistent` is invoked
      Then: return code is 3; stderr contains `"nonexistent"` and at least one of `"a"`, `"b"`

- [x] `test: lb_scaling.py contains no direct state-file write before any haproxy admin call (F11 audit)`
      Given: the migrated `src/vctl/commands/lb_scaling.py` source
      When: an AST or grep audit checks for `bs.add(` or `bs.remove(` outside of Reconciler delegation in the six migrated verb bodies
      Then: no such call is found — every state mutation goes through `Reconciler`; the `bs.add` / `bs.remove` calls exist only in `reconciler.py`

- [x] `test: lb_scaling.py contains no contextlib.suppress around haproxy admin calls (F12 audit)`
      Given: the migrated `src/vctl/commands/lb_scaling.py` source
      When: a grep or AST audit checks for `contextlib.suppress` in the bodies of `_do_auto_add` and `_do_add`
      Then: no `contextlib.suppress` is found wrapping any haproxy admin call in either function

- [x] `test: mypy --strict passes and all 406+ existing tests pass after format updates`
      Given: the Phase 2 migration applied to `src/vctl/commands/lb_scaling.py`; test fixtures in `tests/test_commands_lb_scaling.py` updated for new stderr format (`ADDED` / `READIED` replacing `(new)` / `(already present)`) and new exit-4 expectations where applicable
      When: `mypy --strict src/vctl` and `pytest` are run
      Then: both commands exit 0; at least 406 tests collected and passing; at least 6 new exit-4 tests present

- [x] `test: version is 0.3.0 in pyproject.toml and __init__.py, CHANGELOG.md has [0.3.0] section`
      Given: the Phase 2 PR landed
      When: `pyproject.toml`, `src/vctl/__init__.py`, and `CHANGELOG.md` are inspected
      Then: `pyproject.toml` version field is `"0.3.0"`; `src/vctl/__init__.py` version string is `"0.3.0"`; `CHANGELOG.md` contains a `[0.3.0]` section documenting breaking exit-code changes (exit 4 for LB-unreachable) and F11/F12 closure
