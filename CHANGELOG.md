# Changelog

All notable changes to this project will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: Semver.

## [Unreleased]

## [0.2.0] - 2026-05-01

### Added
- Multi-pool LB routing: `lb.pools: [...]` in cluster.yaml; one HAProxy frontend per served model.
- `vctl serve` fail-fast pool routing — exits 3 (within seconds) if no pool serves the profile's model, before spawning vllm.
- `vctl lb add <ep> [--pool <name>]` — auto-routes by `/v1/models` probe; `--pool` overrides.
- `vctl lb drain <ep> [--pool <name>]` — explicit pool selection.
- `vctl lb wait-ready [N] [--pool <name>]` — waits for ≥N ready in every non-empty pool (or one).
- `vctl lb list` / `vctl lb health` — output grouped by pool with per-pool URLs / pass/fail rollup.
- `vctl info` lists every pool's URL annotated with served_model.
- `vctl init-config` template now generates a multi-pool `cluster.yaml`.
- `pool_for_model` and `pool_for_endpoint` in new `vctl.lb.routing` module.
- Per-pool state files: `<state_dir>/<lb_host>/<pool>_backends.txt` with stable `.lock` sidecar.

### Changed
- HAProxy renderer emits one `frontend pool_<name>` + `backend pool_<name>` block per pool.
- `BackendState` accepts a `pool=<name>` argument; default `"default"` for legacy callers.
- `cluster.yaml` schema gains `lb.pools: list[Pool]`. Legacy `lb.client.bind_port` remains accepted; the loader synthesizes a single default pool with `served_model: "*"`.
- `config migrate` writes `lb.pools` instead of `lb.client.bind_port`.

### Deprecated
- `lb.client.bind_port` (still works via auto-synthesis; new configs should use `lb.pools`).

### Migration
- Existing v0.1.0 deployments require no changes — yaml auto-loads with synthesized default pool.
- One-shot canonicalize: `vctl config migrate cluster.yaml`.
- State files migrate in place on first read of the default pool.

## [0.1.0] - 2026-05-01

### Added
- Typed Python CLI replacing the bash + python prototype at `multi_node_dp/`.
- Pydantic v2 schema (`apiVersion: vctl/v1`) with `cluster.yaml` + `models/<name>.yaml`.
- Subcommands: `info`, `profiles`, `args`, `preflight`, `serve`, `stop`,
  `lb {install,start,stop,status,is-host,where,list,wait-ready,stats,logs,
  config,reload,add,remove,drain,attach,detach,auto-add,health}`,
  `config {validate,show,schema,migrate}`.
- Atomic `fcntl.flock`-protected backend state file with stable lock sidecar (AT-11).
- Idempotent `lb add` (returns `(new)` vs `(already present)`; AT-9).
- `lb attach` health probe via `/v1/models` data array (AT-10).
- Pretty + JSON structured logging on stderr (`--log-format json`; AT-13).
- Migration command: old prototype shape → `vctl/v1` (AT-4).
- Self-IP guard on `lb start` (exit code 4; AT-8).
- Process-tree reaping on `serve` shutdown via psutil.
- Distribution via `uv tool install`; `--help` <200 ms (AT-1).
- GitHub Actions CI matrix (3.10/3.11/3.12), ruff + mypy --strict + pytest, coverage gate ≥50% (AT-15).
- HAProxy admin-state bitmask handling (MAINT 0x07, DRAIN 0x38) — drained backends NOT counted as ready in `wait-ready`.
- `wait-ready` dual-check: ready_count + LB front HTTP 200 (AT-12).
- Positional profile shortcut: `vctl serve models/qwen3_5-9b.yaml`.
- `vctl init-config` — scaffolds a fully-documented `cluster.yaml` + `models/<name>.yaml` profiles in one step.

### Changed
- Schema is now grouped (`parallelism.*`, `resources.*`, `lb.*`) and gated by an `apiVersion` header.
- LB config uses tagged-union discriminator on `lb.kind` (only `haproxy` shipped).

### Removed
- Direct dependency on bash/socat at runtime.
