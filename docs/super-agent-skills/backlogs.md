# Backlogs

## In Progress

- **Reconciler Phase 2 — migrate scaling verbs to Reconciler**
  - Spec: [specs/2026-05-02-reconciler-phase2-design.md](specs/2026-05-02-reconciler-phase2-design.md)
  - Branch: `feat/reconciler-phase2` (cut from `feat/reconciler-phase1`)
  - Migrates `_do_add` / `_do_remove` / `_do_drain` / `_do_auto_add` / `_do_remove_cli` / `_do_detach` to delegate state mutations to Reconciler. Closes F11 + F12. v0.3.0 bump.

## Up Next

- **Phase 2 — migrate scaling verbs to Reconciler.** Replace ad-hoc two-phase ordering in `_do_add` / `_do_remove` / `_do_drain` / `_do_auto_add` / `_do_remove_cli` / `_do_detach` with `Reconciler` calls. Closes F11 (state-first ordering) and F12 (silent suppress in auto-add). Likely v0.3.0 due to behavior change: `lb add` against stopped LB exits 4 instead of silent state cache.
- **`vctl lb info --fix` flag.** Once Phase 2 wires Reconciler in, expose interactive drift repair via `lb info --fix` (calls `reconcile_from_state` per pool with detected drift). Optional UX win; v0.3.x.
- **`_name_for` input sanitization.** Reject endpoints containing chars outside `[0-9a-zA-Z:.]` before any haproxy admin call. Low-risk hardening (state file is admin-only) but worth a one-line `re.fullmatch` check. Surfaced by code review on Phase 1.
- **Extend `BackendStatus` with `backend: str` field.** Parse `parts[1]` from `show servers state` so `_haproxy_servers` can do real per-pool filtering instead of returning all rows. Currently no-op due to documented HAProxy version quirk. Worth doing when Phase 2 lands `lb info --fix`.

## Ideas (Unprioritized)

- **Drop the state file entirely.** Use HAProxy's native `server-state-file` for restart persistence + worker-side self-registration daemon for cross-host coordination. Eliminates the dual-store consistency problem at the root. Big change; v0.4.x candidate after Phase 2 stabilizes.
- **Per-entry status in state file.** Replace flat `host:port` lines with JSON entries carrying `status` (LIVE / DRAINING / PENDING_ADD), `added_at`, `last_haproxy_sync_ts`. Lets `lb info` surface "PENDING for 5 min — admin socket stuck."

## Completed

### 2026-05-02 — Reconciler Phase 1

- `src/vctl/lb/reconciler.py` (Reconciler class + Action / Outcome / Drift)
- `src/vctl/lb/errors.py` (ReconcilerError hierarchy)
- Refactor: `_name_for` → `lb/routing.py`; `_NoOpClient` + `lb_admin_client` → `lb/runtime.py`
- Hardening: `RuntimeClient.set_state` parses haproxy response + raises on errors
- Spec: [specs/2026-05-01-reconciler-design.md](specs/2026-05-01-reconciler-design.md) — 10 acceptance tests, all checked off
- Plan: [plans/2026-05-01-reconciler-phase1.md](plans/2026-05-01-reconciler-phase1.md) — 13 tasks
- Tests: 33 unit + 7 errors + 1 multiprocess concurrency + 1 `@pytest.mark.integration` real-haproxy
- Branch: `feat/reconciler-phase1` (PR pending merge)
