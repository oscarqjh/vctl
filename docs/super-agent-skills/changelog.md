# Changelog

Changes shipped via super-agent-skills workflows. For the project-wide
release CHANGELOG, see `/CHANGELOG.md` at the repo root.

## [Unreleased]

- **Reconciler Phase 1** — new `vctl.lb.reconciler.Reconciler` module that becomes the single authoritative path for keeping haproxy + state-file in sync. Additive only; no migration of existing `_do_add` / `_do_remove` / `_do_drain` / `_do_auto_add` callers (Phase 2). Hardening: `RuntimeClient.set_state` now parses haproxy responses + raises on error tokens. Branch `feat/reconciler-phase1`, 13 commits.
