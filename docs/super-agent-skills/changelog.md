# Changelog

Changes shipped via super-agent-skills workflows. For the project-wide
release CHANGELOG, see `/CHANGELOG.md` at the repo root.

## [Unreleased]

- **Reconciler Phase 2 (v0.3.0)** — six scaling verbs in `lb_scaling.py` migrated to delegate state mutations to Reconciler. Closes F11 (state-first ordering bug) and F12 (silent suppress in auto-add). BREAKING: `lb add`/`remove`/`drain` against stopped LB now exits 4 (was: silent state-write or 0); stderr surfaces `Action.NAME` instead of legacy `(new)`/`(already present)` strings. `vctl serve` now checks `_do_add` rc and aborts vllm if attach fails. Branch `feat/reconciler-phase2`, 11 commits, 414 tests passing.
- **Reconciler Phase 1** — new `vctl.lb.reconciler.Reconciler` module that becomes the single authoritative path for keeping haproxy + state-file in sync. Additive only; no migration of existing `_do_add` / `_do_remove` / `_do_drain` / `_do_auto_add` callers (Phase 2). Hardening: `RuntimeClient.set_state` now parses haproxy responses + raises on error tokens. Branch `feat/reconciler-phase1`, 13 commits.
