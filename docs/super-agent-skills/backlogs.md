# Backlogs

## In Progress

_(none — Phase 2 shipped 2026-05-03)_

## Up Next

- **`vctl lb info --fix` flag.** Now that Reconciler.diff and reconcile_from_state are wired in via Phase 2, expose interactive drift repair via `lb info --fix`. Optional UX win; v0.3.x.
- **`_name_for` input sanitization.** Reject endpoints containing chars outside `[0-9a-zA-Z:.]` before any haproxy admin call. Low-risk hardening (state file is admin-only) but worth a one-line `re.fullmatch` check. Surfaced by code review on Phase 1.
- **Extend `BackendStatus` with `backend: str` field.** Parse `parts[1]` from `show servers state` so `_haproxy_servers` can do real per-pool filtering instead of returning all rows. Currently no-op due to documented HAProxy version quirk.

## Ideas (Unprioritized)

- **Drop the state file entirely.** Use HAProxy's native `server-state-file` for restart persistence + worker-side self-registration daemon for cross-host coordination. Eliminates the dual-store consistency problem at the root. Big change; v0.4.x candidate.
- **Per-entry status in state file.** Replace flat `host:port` lines with JSON entries carrying `status` (LIVE / DRAINING / PENDING_ADD), `added_at`, `last_haproxy_sync_ts`. Lets `lb info` surface "PENDING for 5 min — admin socket stuck."

## Completed

### 2026-05-03 — Reconciler Phase 2 (v0.3.0)

- Migrated six scaling verbs in `lb_scaling.py` to delegate to Reconciler: `_do_add`, `_do_remove`, `_do_drain`, `_do_auto_add`, `_do_remove_cli`, `_do_detach`
- Closes **F11** (state-first ordering bug) and **F12** (silent suppress in auto-add)
- New `_exit_for(exc)` helper centralizes ReconcilerError → exit code mapping
- `vctl serve` now checks `_do_add` rc; aborts vllm if LB attach fails
- BREAKING: `lb add`/`remove`/`drain` against stopped LB exits 4 (was: silent state cache or 0); stderr surfaces `Action.NAME` instead of `(new)`/`(already present)`
- Spec: [specs/2026-05-02-reconciler-phase2-design.md](specs/2026-05-02-reconciler-phase2-design.md) — 12 acceptance tests
- Plan: [plans/2026-05-02-reconciler-phase2.md](plans/2026-05-02-reconciler-phase2.md) — 9 tasks
- Tests: 414 passing (was 411 in Phase 1; net +3 acceptance tests)
- Branch: `feat/reconciler-phase2` (PR pending merge after Phase 1)

### 2026-05-02 — Reconciler Phase 1

- `src/vctl/lb/reconciler.py` (Reconciler class + Action / Outcome / Drift)
- `src/vctl/lb/errors.py` (ReconcilerError hierarchy)
- Refactor: `_name_for` → `lb/routing.py`; `_NoOpClient` + `lb_admin_client` → `lb/runtime.py`
- Hardening: `RuntimeClient.set_state` parses haproxy response + raises on errors
- Spec: [specs/2026-05-01-reconciler-design.md](specs/2026-05-01-reconciler-design.md) — 10 acceptance tests, all checked off
- Plan: [plans/2026-05-01-reconciler-phase1.md](plans/2026-05-01-reconciler-phase1.md) — 13 tasks
- Tests: 33 unit + 7 errors + 1 multiprocess concurrency + 1 `@pytest.mark.integration` real-haproxy
- Branch: `feat/reconciler-phase1` (PR pending merge)
