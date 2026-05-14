# Backlogs

## In Progress

- **vctl → tctl rename + workload reorg (v0.9.0).** Rename project to `tctl` and reorganize into workload sub-command groups (`tctl vllm`, `tctl haproxy`, `tctl lmms`) for easy extension (e.g. future `tctl lmdeploy`). Breaking: cluster.yaml schema (`lb:` → `haproxy:`, top-level `profile:` → `vllm.default_profile`), env vars (`VCTL_*` → `TCTL_*`), state paths (`~/.vctl/*` → `~/.tctl/*`), tmux session names. No backwards compat. Spec: [specs/2026-05-09-tctl-rename-workload-reorg-design.md](specs/2026-05-09-tctl-rename-workload-reorg-design.md) — 12 ATs.

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

### 2026-05-09 — v0.8.0 TmuxSession refactor (unified env propagation)

- **New `src/vctl/tmux.py` `TmuxSession` class** unifies tmux session management across `LbManager` (haproxy), `VllmManager` (vllm), and `lmmseval._cmd_run_loop`.
- **Full `os.environ` snapshot at `start()` call time** via `tmux new-session -e KEY=VAL` (tmux 3.2+ required; deployed env is 3.4). Eliminates the chronic "tmux server cached env from when first started" footgun (v0.5.4 PATH, v0.7.3-0.7.4 HF_/TRANSFORMERS_OFFLINE).
- **BREAKING (internal):** `tmux_run_detached`, `tmux_run_detached_argv`, `tmux_kill`, `tmux_session_exists`, `_validate_tmux_name` deleted from `vctl.platform`. Public CLI surface unchanged.
- **`kill(tree=True)` tree-kill** via psutil (SIGTERM tree → wait `grace_s` → SIGKILL survivors → tmux kill-session). Catches accelerate worker processes that previously survived `tmux kill-session`.
- **`log_path` constructor arg** consolidates pipe-pane log capture (was scattered across VllmManager).
- 35 new unit tests in `tests/test_tmux.py` + 3 integration tests. All AT-1 through AT-10 covered.
- platform.py shrunk from ~110 lines to 46 (only `detect_self_ip` + `which`).
- Spec: [specs/2026-05-06-tmux-session-mgmt-design.md](specs/2026-05-06-tmux-session-mgmt-design.md) — 10 ATs.
- Plan: [plans/2026-05-06-tmux-session-mgmt.md](plans/2026-05-06-tmux-session-mgmt.md) — 8 tasks.

### 2026-05-05 — v0.7.0 rolling-restart orchestration (Phase 3 of 3)

- **`vctl rolling-restart --pool <name>`** — sequential, halt-on-failure ssh-loop. For each ep in the pool: `ssh <ep_host> 'bash -lc "vctl serve restart"'` then poll HAProxy stats until `UP` (default 60s window).
- **Idempotent re-run via per-pool session file** at `~/.vctl/lb/rolling-restart/<pool>.json`. Interrupted runs auto-resume on next invocation: failed eps probed first (operator prompt skip/retry/abort if still DOWN), then pending list resumes.
- **Aux flags:** `--fresh` (override session, force fresh start), `--status` (read session JSON to stdout), `--abort` (delete session, idempotent), `--dry-run`, `--quiet`, `--ssh-user`, `--vllm-timeout`, `--ready-timeout`, `--remote-vctl-path`.
- **No-TTY guard:** prompt path defaults to skip when stdin is not a TTY (nohup, systemd-run safe).
- **Exit codes:** 0 (success / dry-run), 1 (halt-on-failure), 2 (config / corrupt session), 3 (unknown pool), 4 (concurrent run).
- 32 unit tests; full suite 535+ passing, coverage 82%.
- Spec: [specs/2026-05-04-rolling-restart-phase3-design.md](specs/2026-05-04-rolling-restart-phase3-design.md) — 10 acceptance tests.
- Plan: [plans/2026-05-04-rolling-restart-phase3.md](plans/2026-05-04-rolling-restart-phase3.md) — 8 tasks.
- **Closes the 3-phase vllm lifecycle architecture** (Phase 1 v0.5.0 supervisor, Phase 2 v0.6.0 prune, Phase 3 v0.7.0 rolling-restart).

### 2026-05-04 — v0.6.0 lb prune (worker reaper, Phase 2 of 3)

- **`vctl lb prune`** — manual reaper. Removes backends DOWN > threshold (default 5m). Flags: `--threshold DURATION`, `--pool NAME`, `--dry-run`. Reuses `Reconciler.want_absent`. MAINT/DRAIN backends preserved. Exit 3 on unknown pool, 4 on LbUnreachable.
- **Auto-watcher bundled into `vctl lb start/stop/status`** — when `cluster.lb.prune.enabled: true` (default), `lb start` spawns `vctl-lb-watch` tmux session running `bash -c 'while true; do vctl lb prune; sleep N; done'`. `lb stop` kills it idempotently. `lb status` reports state. Sentinel pidfile at `~/.vctl/lb/watch.pid`.
- **`src/vctl/duration.py`** — new stdlib-only `_parse_duration` helper.
- **`LbPrune` pydantic class** — `enabled`/`threshold`/`watch_interval` config under `lb.prune` in cluster.yaml. Backwards-compatible defaults.
- **`docs/CLI-REFERENCE.md`** — new comprehensive command reference (279 lines, every command + flags + exit codes).
- **`vctl serve --help`** now lists sub-verbs (status/stop/restart/console/logs) in the epilog. Each sub-verb's argparse has a description.
- 501 unit tests passing (+12 net), coverage 81.6%.
- Spec: [specs/2026-05-04-lb-prune-phase2-design.md](specs/2026-05-04-lb-prune-phase2-design.md) — 11 acceptance tests.
- Plan: [plans/2026-05-04-lb-prune-phase2.md](plans/2026-05-04-lb-prune-phase2.md) — 7 tasks.

### 2026-05-04 — v0.5.0 VllmManager (tmux-backed vllm supervisor, Phase 1 of 3)

- New `src/vctl/vllm_manager.py` — `VllmManager` class mirrors `LbManager` shape. Tmux session `vctl-vllm-<profile>` owns vllm. State files `~/.vctl/vllm/<profile>.{pid,log,cmd.json,host}`.
- `vctl serve` now runs vllm DETACHED in tmux by default; returns 0 immediately. SSH disconnect / shell hangup no longer kill vllm. Identical process-tree shape to a manual `tmux new-session -d 'vllm serve …'`.
- New sub-verbs: `vctl serve status / stop / restart / attach / logs [-n N] [-f]`.
- Backwards-compat `--foreground` flag (or `VCTL_SERVE_FOREGROUND=1` env) preserves v0.4.x blocking behavior.
- Cross-host guard: `stop` and `restart` refuse to operate on state files belonging to a different host.
- 22 unit tests + 5 integration test skeletons (`@pytest.mark.vllm_supervisor_integration`).
- Spec: [specs/2026-05-03-vllm-supervisor-phase1-design.md](specs/2026-05-03-vllm-supervisor-phase1-design.md) — 10 acceptance tests.
- Plan: [plans/2026-05-03-vllm-supervisor-phase1.md](plans/2026-05-03-vllm-supervisor-phase1.md) — 12 tasks.

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
