# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`tctl` is a typed Python CLI (Python 3.10+) for managing tmux-supervised long-running processes; ships workloads for vllm, haproxy, and lmms. Distributed via `uv tool install`. Build backend is `hatchling`; project metadata, dependencies, and scripts live in `pyproject.toml`. The CLI entry point is `tctl = "tctl.cli:main"`.

## Common commands

Use `uv` everywhere — it is the project's canonical tool runner.

```bash
# editable install with dev + migrate extras
uv pip install -e ".[dev,migrate]"

# lint, format-check, type-check (mirrors CI)
ruff check .
ruff format --check .
mypy --strict src/tctl

# full test run with coverage gate (CI fails under 50%)
pytest -q --cov=tctl --cov-report=term-missing --cov-fail-under=50

# single test file / single test
pytest tests/workloads/haproxy/test_commands.py
pytest tests/workloads/haproxy/test_commands.py::test_info_shows_all_pools -x

# integration tests (require a real `haproxy` binary on PATH)
pytest -m integration

# run the CLI from source
python -m tctl --help
```

CI matrix (`.github/workflows/ci.yml`) runs all four steps (`ruff check`, `ruff format --check`, `mypy --strict`, `pytest --cov-fail-under=50`) on Python 3.10 / 3.11 / 3.12. Match this locally before pushing.

Ruff config (`.ruff.toml`): `target-version = py310`, `line-length = 100`, lint rules `E,F,W,I,B,UP,SIM,N`, double-quote format. Mypy is `strict = True` over `src/tctl` (see `mypy.ini`).

## Gotchas

- **`pytest` / `mypy` / `ruff` are not on global PATH.** Use `.venv/bin/pytest`, `.venv/bin/mypy`, `.venv/bin/ruff` (or prefix with `uv run`). The shell on this box has a different binary called `gh` (a "Github browser opener", not the official GitHub CLI) — `gh pr create` etc. is not available; push the branch and open the GitHub URL manually.
- **`tests/test_cli.py::test_help_under_200ms` is timing-flaky under suite load.** Passes standalone, can fail when full suite runs in parallel. Pre-existing; not a Reconciler regression.
- **HAProxy admin socket closes after every response** in default (non-prompt) mode. Code that issues multiple admin commands MUST open a fresh `RuntimeClient` per command (see `Reconciler._acquire`). Reusing one client triggers `BrokenPipeError` on the second send. Existing `haproxy/scaling.py`'s `_do_add` masks this with `contextlib.suppress` on the trailing `set_state` call — per-call client pattern must be preserved.
- **`haproxy/scaling.py` re-exports** `_client` (alias for `tctl.workloads.haproxy.runtime.lb_admin_client`) and `_NoOpClient` and `_name_for` via `__all__`. Required for mypy `--strict` to treat `from tctl.workloads.haproxy.scaling import _client` as an explicit re-export, AND for backward compatibility with `monkeypatch.setattr(scaling, "_client", ...)` in existing tests. Do not remove `__all__` — mypy will fail.
- **Workloads — add new ones via cookbook at `docs/COOKBOOK-workloads.md`.** No base class, no plugin system — convention-driven: create `src/tctl/workloads/<name>/`, implement `run(ns, argv_rest) -> int`, register one line in `_WORKLOADS` in `cli.py`.

## Architecture

### Workload structure

```
src/tctl/
    cli.py                  # dispatches to workloads + platform cmds via _WORKLOADS dict
    tmux.py                 # TmuxSession — shared primitive for all workloads
    config/                 # Pydantic v2 schemas, env-var overrides, YAML loader
    commands/               # platform-level commands only: config_cmd, init_config
    workloads/
        vllm/               # tctl vllm serve / stop / rolling-restart / info / ...
        haproxy/            # tctl haproxy start / stop / status / add / drain / ...
        lmms/               # tctl lmms run-loop / stop / status (hidden workload)
```

### CLI dispatch — lazy imports for sub-200ms startup

`src/tctl/cli.py` registers each workload as a string in `_WORKLOADS` and only `importlib.import_module()`s the chosen module inside `_dispatch`. Adding a new workload means: add an entry to `_WORKLOADS`, create `src/tctl/workloads/<name>/` with an `__init__.py` that exposes `run(ns, argv_rest) -> int`. Do not add eager imports at the top level — startup time is a feature.

`_hoist_positional_profile` rewrites `tctl vllm serve models/qwen3_5-9b.yaml` into `tctl vllm serve --profile qwen3_5-9b` before argparse runs.

### Config resolution chain

1. `cluster.yaml` path: `--config` flag → `$CLUSTER_CONFIG` → `~/.tctl/cluster.yaml`. Resolution lives in `cli._resolve_config_path`.
2. Profile selection: CLI `--profile` → `$TCTL_PROFILE` → `cluster.vllm.default_profile` (see `config/settings.py:resolve_profile_name`).
3. `resolver.resolve()` loads cluster + profile, deep-merges `cluster.env` and `profile.env` (None in profile *deletes* a key), and returns a frozen `ResolvedConfig`. Profile names are validated against a regex that rejects path traversal.

### Pydantic v2 schemas (`config/models.py`)

All schema classes inherit from `_Strict` (`extra="forbid"`). Top-level `ClusterFile` / `ProfileFile` reject unknown keys — typos produce a clear error rather than silent data loss. `apiVersion` is pinned to the literal `"tctl/v1"`.

`LbHaproxy._synthesize_or_validate_pools` is the single source of truth for pool invariants: synthesizes a `default` pool from legacy `client.bind_port` if none given, rejects duplicate names/ports/served_models, and refuses `bind_port` collisions with `admin`/`stats`. New pool-shape rules belong here, not scattered across commands.

### TCTL_* env overrides (`config/settings.py`)

Pattern: `TCTL_<TOPLEVEL>__<NESTED>__...=value`. Double underscore is the nest delimiter; first segment must match a top-level field of the document being loaded (`_CLUSTER_TOPLEVEL` / `_PROFILE_TOPLEVEL`) or it is **silently ignored**. This whitelist exists so test sentinels like `TCTL_TEST_NO_SOCKET` can't poison the document and trip `extra="forbid"`. Scalars are coerced strictly: `true|false`, `^-?\d+$`, `^-?\d+\.\d+$` — no nan/inf/scientific/hex.

### Multi-pool routing (`workloads/haproxy/routing.py`)

`pool_for_model(lb, served_model)` is the canonical resolver: exact match wins; multiple exact matches → `sys.exit(3)`; one wildcard (`served_model: "*"`) is the fallback; otherwise exit 3 with available pools listed. `pool_for_endpoint` probes `/v1/models` and delegates. `tctl vllm serve` calls this *before* spawning vLLM so misconfiguration fails fast.

### Backend state file (`workloads/haproxy/state.py`)

Per-pool list at `<state_dir>/<lb_host>/<pool>_backends.txt` with a sibling `<pool>_backends.lock`. All reads/writes go through `BackendState._locked()`, which holds an `fcntl.flock` exclusive lock on the lock file (separate from the data file so locks survive `os.replace` swaps — AT-11). `BackendState.migrate_if_needed` performs a one-shot, flock-protected migration from the v0.1.0 flat layout `<state_dir>/<lb_host>_backends.txt` to the new per-pool layout; `HaproxyManager.__init__` triggers it.

### Haproxy lifecycle (`workloads/haproxy/manager.py`)

`HaproxyManager` renders `haproxy.cfg` from `LbHaproxy` + per-pool `BackendState`, then runs HAProxy inside a tmux session named `tctl-haproxy` (validated by `_validate_tmux_name`). `start()` guards against double-start and against running on the LB host's own IP (exit 4). PID discovery falls back to `psutil.process_iter` matching the cfg path because HAProxy runs foreground inside tmux and may not write a pidfile.

### Tmux session names

| Workload | Session name |
|---|---|
| HAProxy daemon | `tctl-haproxy` |
| HAProxy watcher | `tctl-haproxy-watch` |
| vLLM (per-profile) | `tctl-vllm-<profile>` |
| lmms-eval | `tctl-lmms` |

### Tests

`tests/conftest.py` has two session-scoped autouse fixtures that SIGKILL leaked `haproxy` and `tctl vllm serve` / fake-vllm processes whose cmdline contains `/tmp/pytest-of-*`. They are intentionally narrow — production paths under `~/.tctl/` or `/mnt/...` are never matched. When writing new tests that spawn real subprocesses, route any artifacts through `tmp_path` so the sweepers can find them.

Mark integration tests that need a real `haproxy` binary with `@pytest.mark.integration` (declared in `pyproject.toml`).

### Exit codes (callers depend on these)

- `2` — config error (missing/invalid `cluster.yaml`, profile YAML, etc.). Routed in `cli.main` via `_missing_path_is_config`.
- `3` — pool routing failure (no pool serves model, ambiguous, probe failed).
- `4` — LB self-IP guard tripped (`haproxy start` refuses to run on a worker).
- `130` — `KeyboardInterrupt`.

## Conventions

- New code goes under `src/tctl/`. The `tctl` import path is the only public surface.
- Always run code through `ruff format` + `ruff check` + `mypy --strict` before committing — CI rejects on any of the four checks.
- Workload `__init__.py` files expose a single `run(ns: argparse.Namespace, argv_rest: list[str]) -> int`. Build the per-workload argparse parser inside that function so it stays out of the cold import path.
- Treat `cluster.yaml` / profile YAML as the contract: schema changes must update `config/models.py`, `examples/`, and `README.md` together.
