# Backlogs

## In Progress

- **Reconciler — single owner of (haproxy, state-file) consistency** (Phase 1)
  - Spec: [docs/super-agent-skills/specs/2026-05-01-reconciler-design.md](specs/2026-05-01-reconciler-design.md)
  - Phase 1 = additive module + tests; existing `lb_scaling.py` callers untouched.
  - Phase 2 (separate item, see Up Next): migrate `_do_add` / `_do_remove` / `_do_drain` / `_do_auto_add` to use `Reconciler`. Closes F11 + F12 by construction.

## Up Next

- **Phase 2 — migrate scaling verbs to Reconciler.** After Phase 1 ships and bakes, replace ad-hoc two-phase ordering in `_do_add` / `_do_remove` / `_do_drain` / `_do_auto_add` / `_do_remove_cli` / `_do_detach` with `Reconciler` calls. Closes F11 (state-first ordering) and F12 (silent suppress in auto-add). Likely v0.3.0 due to behavior change: `lb add` against stopped LB exits 4 instead of silent state cache.
- **`vctl lb info --fix` flag.** Once `Reconciler.diff` lands, expose interactive drift repair via `lb info --fix` (calls `reconcile_from_state` per pool with detected drift). Optional UX win; v0.3.x.

## Ideas (Unprioritized)

- **Drop the state file entirely.** Use HAProxy's native `server-state-file` for restart persistence + worker-side self-registration daemon for cross-host coordination. Eliminates the dual-store consistency problem at the root. Big change; v0.4.x candidate after Phase 2 stabilizes.
- **Per-entry status in state file.** Replace flat `host:port` lines with JSON entries carrying `status` (LIVE / DRAINING / PENDING_ADD), `added_at`, `last_haproxy_sync_ts`. Lets `lb info` surface "PENDING for 5 min — admin socket stuck."
