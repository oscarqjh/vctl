# Changelog

All notable changes to this project will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: Semver.

## [Unreleased]

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
- Positional profile shortcut: `vctl serve models/qwen3-9b.yaml`.

### Changed
- Schema is now grouped (`parallelism.*`, `resources.*`, `lb.*`) and gated by an `apiVersion` header.
- LB config uses tagged-union discriminator on `lb.kind` (only `haproxy` shipped).

### Removed
- Direct dependency on bash/socat at runtime.
