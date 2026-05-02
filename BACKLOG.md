# Backlog

## Open

- **F5** *(deferred — probably wontfix)*: emit `daemon` directive in `render.py` + redirect stdout log to a file so we get both pidfile AND captured logs. Tradeoff: tmux pane closes immediately, lose interactive `tmux attach`. F1 already covers status/stop, so the value is marginal.

## Up Next (post-v0.4)

- **Prometheus metrics endpoint** (`vctl lb metrics`)
- **Multi-cluster support** (`~/.config/vctl/clusters/<name>.yaml` + `--cluster <name>`)

## Ideas (unprioritized)

- **Drop the state file entirely.** Use HAProxy's native `server-state-file` for restart persistence + worker-side self-registration daemon for cross-host coordination. Eliminates the dual-store consistency problem at the root. Big change; v0.5.x candidate.
- **Per-entry status in state file.** Replace flat `host:port` lines with JSON entries carrying `status` (LIVE / DRAINING / PENDING_ADD), `added_at`, `last_haproxy_sync_ts`. Lets `lb info` surface "PENDING for 5 min — admin socket stuck."
- vLLM Router (cache-aware) as alternative `lb.kind`
- Self-update check
- Daemon mode for LB supervision
- REST API for orchestration
- Audit log
- Hash-based sticky routing docs
- Profile inheritance (`extends:`)
- Dry-run mode for `serve`

---

## History

Full closure history of v0.2.x and v0.3.x–v0.4.x backlog items lives in `CHANGELOG.md`. Highlights:

- **v0.4.1** — `_name_for` ep validation; per-pool `_haproxy_servers` filtering via `BackendStatus.backend` field.
- **v0.4.0** — dropped bash-prototype migration support (`vctl config migrate`, `BackendState.migrate_if_needed`, `Model._drop_deprecated_served_as` shim, `[migrate]` extra, `MIGRATION.md`). Net −924 LOC.
- **v0.3.0** — Reconciler Phase 1+2: closes F11 (state-first ordering bug) and F12 (silent suppress in auto-add) by routing all state mutations through `Reconciler`'s haproxy-first invariant.
- **v0.2.x A1–A8 / B1–B12 / C1–C12 / D1–D13 / E1–E3** — code-review-driven hardening (correctness, manager hygiene, config UX, env coerce/resolver, security). All shipped.
- **v0.2.4** — unified `lb info` dashboard.
- **v0.2.2** — F1, F2, F3, F4, F6, F7, F8, F9, F10 hotfixes.
