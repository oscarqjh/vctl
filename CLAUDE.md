# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`vctl` is a typed Python CLI (Python 3.10+) that orchestrates a multi-pod vLLM fleet behind an HAProxy load balancer. Distributed via `uv tool install`. Build backend is `hatchling`; project metadata, dependencies, and scripts live in `pyproject.toml`. The CLI entry point is `vctl = "vctl.cli:main"`.

## Common commands

Use `uv` everywhere — it is the project's canonical tool runner.

```bash
# editable install with dev + migrate extras
uv pip install -e ".[dev,migrate]"

# lint, format-check, type-check (mirrors CI)
ruff check .
ruff format --check .
mypy --strict src/vctl

# full test run with coverage gate (CI fails under 50%)
pytest -q --cov=vctl --cov-report=term-missing --cov-fail-under=50

# single test file / single test
pytest tests/test_commands_lb_info.py
pytest tests/test_commands_lb_info.py::test_info_shows_all_pools -x

# integration tests (require a real `haproxy` binary on PATH)
pytest -m integration

# run the CLI from source
python -m vctl --help
```

CI matrix (`.github/workflows/ci.yml`) runs all four steps (`ruff check`, `ruff format --check`, `mypy --strict`, `pytest --cov-fail-under=50`) on Python 3.10 / 3.11 / 3.12. Match this locally before pushing.

Ruff config (`.ruff.toml`): `target-version = py310`, `line-length = 100`, lint rules `E,F,W,I,B,UP,SIM,N`, double-quote format. Mypy is `strict = True` over `src/vctl` (see `mypy.ini`).

## Gotchas

- **`pytest` / `mypy` / `ruff` are not on global PATH.** Use `.venv/bin/pytest`, `.venv/bin/mypy`, `.venv/bin/ruff` (or prefix with `uv run`). The shell on this box has a different binary called `gh` (a "Github browser opener", not the official GitHub CLI) — `gh pr create` etc. is not available; push the branch and open the GitHub URL manually.
- **`tests/test_cli.py::test_help_under_200ms` is timing-flaky under suite load.** Passes standalone, can fail when full suite runs in parallel. Pre-existing; not a Reconciler regression.
- **HAProxy admin socket closes after every response** in default (non-prompt) mode. Code that issues multiple admin commands MUST open a fresh `RuntimeClient` per command (see `Reconciler._acquire`). Reusing one client triggers `BrokenPipeError` on the second send. Existing `lb_scaling._do_add` masks this with `contextlib.suppress` on the trailing `set_state` call — Phase 2 will inherit the per-call client pattern.
- **`lb_scaling.py` re-exports** `_client` (alias for `vctl.lb.runtime.lb_admin_client`) and `_NoOpClient` and `_name_for` via `__all__`. Required for mypy `--strict` to treat `from vctl.commands.lb_scaling import _client` (used in `commands/lb.py`) as an explicit re-export, AND for backward compatibility with `monkeypatch.setattr(lb_scaling, "_client", ...)` in existing tests. Do not remove `__all__` — mypy will fail.

## Architecture

### CLI dispatch — lazy imports for sub-200ms startup

`src/vctl/cli.py` registers each subcommand as a string in `_COMMANDS` and only `importlib.import_module()`s the chosen module inside `_dispatch`. Adding a new top-level command means: add an entry to `_COMMANDS`, create `vctl/commands/<name>.py` with a `run(ns, argv_rest) -> int` callable, and (if it consumes a profile) add the name to `_PROFILE_AWARE`. Do not add eager imports of command modules at the top level — startup time is a feature.

`_hoist_positional_profile` rewrites `vctl serve models/qwen3_5-9b.yaml` into `vctl serve --profile qwen3_5-9b` before argparse runs. Any new profile-aware subcommand must be added to `_PROFILE_AWARE` to participate.

### Config resolution chain

1. `cluster.yaml` path: `--config` flag → `$CLUSTER_CONFIG` → `~/.vctl/cluster.yaml`. Resolution lives in `cli._resolve_config_path`.
2. Profile selection: CLI `--profile` → `$VCTL_PROFILE` → `$MODEL_PROFILE` → `cluster.profile` (see `config/settings.py:resolve_profile_name`).
3. `resolver.resolve()` loads cluster + profile, deep-merges `cluster.env` and `profile.env` (None in profile *deletes* a key), and returns a frozen `ResolvedConfig`. Profile names are validated against a regex that rejects path traversal.

### Pydantic v2 schemas (`config/models.py`)

All schema classes inherit from `_Strict` (`extra="forbid"`). Top-level `ClusterFile` / `ProfileFile` reject unknown keys — typos like `Profile:` produce a clear error rather than silent data loss. `apiVersion` is pinned to the literal `"vctl/v1"`.

`LbHaproxy._synthesize_or_validate_pools` is the single source of truth for pool invariants: synthesizes a `default` pool from legacy `client.bind_port` if none given, rejects duplicate names/ports/served_models, and refuses `bind_port` collisions with `admin`/`stats`. New pool-shape rules belong here, not scattered across commands.

### VCTL_* env overrides (`config/settings.py`)

Pattern: `VCTL_<TOPLEVEL>__<NESTED>__...=value`. Double underscore is the nest delimiter; first segment must match a top-level field of the document being loaded (`_CLUSTER_TOPLEVEL` / `_PROFILE_TOPLEVEL`) or it is **silently ignored**. This whitelist exists so test sentinels like `VCTL_TEST_NO_SOCKET` can't poison the document and trip `extra="forbid"`. Scalars are coerced strictly: `true|false`, `^-?\d+$`, `^-?\d+\.\d+$` — no nan/inf/scientific/hex.

### Multi-pool routing (`lb/routing.py`)

`pool_for_model(lb, served_model)` is the canonical resolver: exact match wins; multiple exact matches → `sys.exit(3)`; one wildcard (`served_model: "*"`) is the fallback; otherwise exit 3 with available pools listed. `pool_for_endpoint` probes `/v1/models` and delegates. `vctl serve` calls this *before* spawning vLLM so misconfiguration fails fast.

### Backend state file (`lb/state.py`)

Per-pool list at `<state_dir>/<lb_host>/<pool>_backends.txt` with a sibling `<pool>_backends.lock`. All reads/writes go through `BackendState._locked()`, which holds an `fcntl.flock` exclusive lock on the lock file (separate from the data file so locks survive `os.replace` swaps — AT-11). `BackendState.migrate_if_needed` performs a one-shot, flock-protected migration from the v0.1.0 flat layout `<state_dir>/<lb_host>_backends.txt` to the new per-pool layout; `LbManager.__init__` triggers it.

### LB lifecycle (`lb/manager.py`)

`LbManager` renders `haproxy.cfg` from `LbHaproxy` + per-pool `BackendState`, then runs HAProxy inside a tmux session named `vctl-lb` (validated by `_validate_tmux_name`). `start()` guards against double-start and against running on the LB host's own IP (exit 4). PID discovery falls back to `psutil.process_iter` matching the cfg path because HAProxy runs foreground inside tmux and may not write a pidfile.

### Tests

`tests/conftest.py` has two session-scoped autouse fixtures that SIGKILL leaked `haproxy` and `vctl serve` / fake-vllm processes whose cmdline contains `/tmp/pytest-of-*`. They are intentionally narrow — production paths under `~/.vctl/` or `/mnt/...` are never matched. When writing new tests that spawn real subprocesses, route any artifacts through `tmp_path` so the sweepers can find them.

Mark integration tests that need a real `haproxy` binary with `@pytest.mark.integration` (declared in `pyproject.toml`).

### Exit codes (callers depend on these)

- `2` — config error (missing/invalid `cluster.yaml`, profile YAML, etc.). Routed in `cli.main` via `_missing_path_is_config`.
- `3` — pool routing failure (no pool serves model, ambiguous, probe failed).
- `4` — LB self-IP guard tripped (`lb start` refuses to run on a worker).
- `130` — `KeyboardInterrupt`.

## Conventions

- New code goes under `src/vctl/`. The `vctl` import path is the only public surface.
- Always run code through `ruff format` + `ruff check` + `mypy --strict` before committing — CI rejects on any of the four checks.
- Subcommand modules expose a single `run(ns: argparse.Namespace, argv_rest: list[str]) -> int`. Build the per-command argparse subparser inside that function so it stays out of the cold import path.
- Treat `cluster.yaml` / profile YAML as the contract: schema changes must update `config/models.py`, `examples/`, the migration helper (`config/migrate.py`), and `README.md` together.
