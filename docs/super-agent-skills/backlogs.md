# Backlogs

## In Progress

_(none — v0.4.1 shipped 2026-05-03)_

## Up Next

- **Prometheus metrics endpoint** (`vctl lb metrics`).
- **Multi-cluster support** (`~/.config/vctl/clusters/<name>.yaml` + `--cluster <name>`).

## Ideas (Unprioritized)

- **Drop the state file entirely.** Use HAProxy's native `server-state-file` for restart persistence + worker-side self-registration daemon for cross-host coordination. Eliminates the dual-store consistency problem at the root. Big change; v0.5.x candidate.
- **Per-entry status in state file.** Replace flat `host:port` lines with JSON entries carrying `status` (LIVE / DRAINING / PENDING_ADD), `added_at`, `last_haproxy_sync_ts`. Lets `lb info` surface "PENDING for 5 min — admin socket stuck."
- vLLM Router (cache-aware) as alternative `lb.kind`.
- Daemon mode for LB supervision.
- REST API for orchestration.
- Audit log.
- Profile inheritance (`extends:`).
- Dry-run mode for `serve`.

## Declined / Deferred

- **`vctl lb info --fix` flag.** Declined post-Phase 2 — `vctl lb auto-add` already calls `Reconciler.reconcile_from_state` per pool which is bidirectional reconcile. `--fix` would just be a UX synonym. Drift in normal usage is rare after Reconciler enforces haproxy-ack-before-state-write.
- **F5** *(probably wontfix)*: emit `daemon` directive in `render.py` + redirect stdout log to a file. F1's pidfile/pgrep fallback already covers status/stop; tradeoff (lose interactive `tmux attach`) outweighs the marginal pidfile cleanliness gain.

## Completed

### 2026-05-03 — v0.4.1 hardening

- `_name_for` validates ep format (`re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d{1,5}")`); raises `ValueError` on injection-shaped inputs. Closes the haproxy admin-socket command-injection vector flagged in Phase 1 review.
- `BackendStatus` gains `backend: str` field populated from `show servers state` parts[1]. `Reconciler._haproxy_servers` now filters rows by section instead of returning all rows. Empty `backend` (legacy haproxy / `_NoOpClient` stub) passes through for back-compat.
- 411 tests passing (+12 new).

### 2026-05-03 — v0.4.0 drop bash-prototype migration

- Removed `vctl config migrate` command, `src/vctl/config/migrate.py` module, `[migrate]` optional dep, `MIGRATION.md`, `BackendState.migrate_if_needed` classmethod, `Model._drop_deprecated_served_as` validator shim, README "Migration from bash prototype" section, bash-prototype mentions in CI / comments.
- BREAKING: profiles still carrying `served_as:` now fail schema validation. v0.1.0 state files must be migrated by running v0.2.x–v0.3.x first.
- Net −924 LOC. 399 tests passing (was 414 in v0.3.0; −15 from migration test removals).

### 2026-05-03 — v0.3.0 Reconciler Phase 2

- Migrated six scaling verbs in `lb_scaling.py` to delegate state mutations to Reconciler: `_do_add`, `_do_remove`, `_do_drain`, `_do_auto_add`, `_do_remove_cli`, `_do_detach`.
- Closes **F11** (state-first ordering bug) and **F12** (silent suppress in auto-add).
- New `_exit_for(exc)` helper centralizes ReconcilerError → exit code mapping.
- `vctl serve` checks `_do_add` rc; aborts vllm if attach fails.
- BREAKING: `lb add`/`remove`/`drain` against stopped LB exits 4 (was: silent state cache or 0); stderr surfaces `Action.NAME` instead of `(new)`/`(already present)`.
- Spec: [specs/2026-05-02-reconciler-phase2-design.md](specs/2026-05-02-reconciler-phase2-design.md) — 12 acceptance tests.
- Plan: [plans/2026-05-02-reconciler-phase2.md](plans/2026-05-02-reconciler-phase2.md) — 9 tasks.

### 2026-05-02 — Reconciler Phase 1 (additive module)

- `src/vctl/lb/reconciler.py` (Reconciler class + Action / Outcome / Drift dataclasses).
- `src/vctl/lb/errors.py` (ReconcilerError hierarchy).
- Refactor: `_name_for` → `lb/routing.py`; `_NoOpClient` + `lb_admin_client` → `lb/runtime.py`.
- Hardening: `RuntimeClient.set_state` parses haproxy response + raises on errors.
- Spec: [specs/2026-05-01-reconciler-design.md](specs/2026-05-01-reconciler-design.md) — 10 acceptance tests.
- Plan: [plans/2026-05-01-reconciler-phase1.md](plans/2026-05-01-reconciler-phase1.md) — 13 tasks.
