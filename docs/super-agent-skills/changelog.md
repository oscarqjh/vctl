# Changelog

Changes shipped via super-agent-skills workflows. For the project-wide
release CHANGELOG, see `/CHANGELOG.md` at the repo root.

## [Unreleased]

- **Rolling restart orchestration (v0.7.0)** — `vctl rolling-restart --pool <name>`. Sequential, halt-on-failure ssh-loop with idempotent per-pool session file at `~/.vctl/lb/rolling-restart/<pool>.json`. Aux flags: `--fresh`, `--status`, `--abort`, `--dry-run`, `--quiet`, `--ssh-user`, `--vllm-timeout`, `--ready-timeout`, `--remote-vctl-path`. No-TTY guard defaults prompts to skip. Exit codes 0/1/2/3/4. Closes the 3-phase vllm lifecycle architecture (Phase 1 v0.5.0 supervisor, Phase 2 v0.6.0 prune, Phase 3 v0.7.0 rolling-restart). Branch `feat/phase3-rolling-restart`, 12 commits, 32 unit tests + suite 535+ passing.
- **lb prune / worker reaper (v0.6.0)** — `vctl lb prune` (manual) + auto-watcher bundled into `lb start/stop/status`. Removes backends DOWN > threshold (default 5m). MAINT/DRAIN preserved. New `LbPrune` pydantic config + stdlib `_parse_duration` helper.
- **VllmManager / vctl serve sub-verbs (v0.5.0)** — tmux-backed vllm supervisor. `serve status / stop / restart / console / logs`. Host-scoped state files. PATH propagation via env_overrides.
- **Reconciler Phase 2 (v0.3.0)** — six scaling verbs in `lb_scaling.py` migrated to delegate state mutations to Reconciler. Closes F11 (state-first ordering bug) and F12 (silent suppress in auto-add). BREAKING: `lb add`/`remove`/`drain` against stopped LB now exits 4 (was: silent state-write or 0); stderr surfaces `Action.NAME` instead of legacy `(new)`/`(already present)` strings. `vctl serve` now checks `_do_add` rc and aborts vllm if attach fails. Branch `feat/reconciler-phase2`, 11 commits, 414 tests passing.
- **Reconciler Phase 1** — new `vctl.lb.reconciler.Reconciler` module that becomes the single authoritative path for keeping haproxy + state-file in sync. Additive only; no migration of existing `_do_add` / `_do_remove` / `_do_drain` / `_do_auto_add` callers (Phase 2). Hardening: `RuntimeClient.set_state` now parses haproxy responses + raises on error tokens. Branch `feat/reconciler-phase1`, 13 commits.
