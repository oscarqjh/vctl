# tctl v0.9.0 — Rename + Workload Reorganisation — Design Spec

## 1. Objective

Rename the project from `vctl` to `tctl` and reorganise its module tree from a
flat-command layout into a *workload-oriented* layout. The rename is a one-shot,
no-backwards-compat cut-over to v0.9.0.

The goals of this refactor are:

1. **Identity shift.** `vctl` reads as "vLLM controller". After v0.8.0, the code
   manages multiple long-running workloads through `TmuxSession` (haproxy, vllm,
   lmms-eval), with lmdeploy and training on the short-term roadmap. The name `tctl`
   (tmux controller) accurately describes what the tool is.

2. **Workload-oriented CLI shape.** Replace the top-level command soup
   (`vctl info`, `vctl lb start`, `vctl lmmseval run-loop`) with a
   `tctl <workload> <verb>` hierarchy that is self-documenting and naturally extensible
   for new workloads without touching shared CLI plumbing.

3. **Extensibility without framework overhead.** Future workloads (lmdeploy,
   fine-tuning jobs, data-prep pipelines) can be added by following a cookbook
   convention — dropping a new `src/tctl/workloads/<name>/` package and registering
   it in one dict in `cli.py`. No base class, no plugin system.

4. **Clean config surface.** Rename `lb:` → `haproxy:` in `cluster.yaml` and move
   the global profile pointer under `vllm.default_profile`. Env-var prefix changes
   from `VCTL_*` to `TCTL_*`, and the loose `MODEL_PROFILE` override is dropped.

What this refactor does NOT do: add generic `tctl run/ls/logs` verbs; introduce a
plugin system; define a `WorkloadManager` base class; or provide any migration tool
for v0.8.0 deployments. Operators drain their clusters, re-init config, and restart.

**Version:** v0.8.0 → **v0.9.0** (breaking, pre-1.0 — intentionally not v1.0.0).

---

## 2. Background

### 2.1 Why now

Three conditions converged at v0.8.0 to make this the right moment:

**TmuxSession is the unifying primitive.** The v0.8.0 refactor extracted a single
`TmuxSession` class that all three workload managers now use uniformly. Before that,
each manager had its own env-propagation and kill logic. Now the shared tmux surface is
small and stable — there is no longer anything tying the module layout to "lb and vllm
specifically". Any workload that needs a detached, supervised process plugs into the
same `TmuxSession` interface.

**Multi-workload identity has outgrown the name.** `vctl` was coined when the only
workload was vLLM. The lmms-eval workload shipped in v0.7.0 as `vctl lmmseval` — a
clear sign that the branding was already strained. lmdeploy support was deferred from
the v0.7.x roadmap to avoid building on a layout that would need restructuring. The
rename unblocks it.

**The command surface has grown incoherent.** At v0.8.0, the top-level help page lists
14 commands across three workloads plus platform utilities. First-time operators cannot
tell which commands belong together. The `tctl <workload>` grouping makes the surface
scannable and makes it obvious where to add documentation for each workload.

### 2.2 What is not changing

`TmuxSession` (`src/tctl/tmux.py`) is **unchanged**. The class, its API, its tests, and
its session-name validation rules all carry over verbatim, except the module path
changes from `vctl.tmux` to `tctl.tmux`.

The HAProxy runtime, state, reconciler, routing, prune, and scaling subsystems are
**moved** from `src/vctl/lb/` to `src/tctl/workloads/haproxy/` but their logic is
unchanged. This is a physical reorganisation, not a rewrite.

Exit codes 2, 3, 4, and 130 are **unchanged**.

---

## 3. Architecture

### 3.1 New module layout

```
src/tctl/
    __init__.py
    __main__.py
    cli.py                  # dispatches to workloads + platform cmds
    tmux.py                 # TmuxSession — UNCHANGED from v0.8.0
    platform.py             # detect_self_ip, which
    duration.py
    resolver.py
    logging.py
    config/
        __init__.py
        models.py           # Pydantic v2 schemas — updated for new YAML shape
        settings.py         # env-var prefix: TCTL_*; updated toplevel whitelist
        yaml_source.py
    commands/               # platform-level commands only
        __init__.py
        config_cmd.py       # tctl config validate/show/schema
        init_config.py      # tctl init-config
    workloads/
        __init__.py
        vllm/
            __init__.py     # run(ns, argv_rest) -> int — workload-level dispatch
            manager.py      # VllmManager (was src/vctl/vllm_manager.py);
                            #   adds _rolling_restart_session_path(pool, state_dir) -> Path
                            #   (replaces module-level _SESSION_DIR constant in rolling_restart.py)
            commands.py     # info / profiles / args / preflight / serve / stop / rolling-restart
            templates.py    # cluster + profile YAML templates (vllm-scoped)
        haproxy/
            __init__.py     # run(ns, argv_rest) -> int — workload-level dispatch
            manager.py      # LbManager (was src/vctl/lb/manager.py)
            render.py
            runtime.py
            state.py
            routing.py
            reconciler.py
            errors.py
            prune.py
            scaling.py
            installer.py    # haproxy binary install — conda → source-build fallback
            probe.py        # admin-socket health probe
            commands.py     # start/stop/status/reload/logs/config/health/add/remove/drain/scaling/prune
        lmms/
            __init__.py     # run(ns, argv_rest) -> int — workload-level dispatch; hidden in --help
            commands.py     # run-loop / stop / status
```

### 3.2 CLI top-level dispatch

The new CLI shape is `tctl <workload> <verb> [args]`. Platform-level commands
(`config`, `init-config`) remain at the top level.

```
tctl --help
    vllm          Manage vLLM inference workers
    haproxy       Manage the HAProxy load balancer
    config        Validate, show, or dump schema for cluster/profile config
    init-config   Scaffold a new cluster.yaml + profile YAML

tctl vllm --help
    info              Show cluster info
    profiles          List or set the active profile
    args              Show resolved vllm serve args for a profile
    preflight         Check GPU / CUDA environment
    serve             Start, inspect, or restart a vllm worker
    stop              Drain from LB + kill local vllm worker
    rolling-restart   Rolling restart across all pools

tctl haproxy --help
    start     Start HAProxy load balancer
    stop      Stop HAProxy load balancer
    status    Show pool status
    reload    Reload config without dropping connections
    logs      Tail HAProxy logs
    config    Show rendered haproxy.cfg
    health    Show health check results
    add       Add a backend to a pool
    remove    Remove a backend from a pool
    drain     Drain a backend from a pool
    scaling   Manage pool scaling
    prune     Remove stale backends

# lmms is hidden from tctl --help and tctl --help output; still reachable:
tctl lmms run-loop / stop / status
```

`lmms` does NOT appear in the top-level help output. `tctl --help` lists only
`vllm`, `haproxy`, `config`, and `init-config`. `tctl lmms --help` works when the
workload name is given directly.

### 3.3 CLI dispatch implementation

`cli.py` carries two dicts:

```python
# Platform commands: name -> module path
_COMMANDS: dict[str, str] = {
    "config": "tctl.commands.config_cmd",
    "init-config": "tctl.commands.init_config",
}

# Workload sub-trees: name -> module path, hidden flag
_WORKLOADS: dict[str, tuple[str, bool]] = {
    "vllm":    ("tctl.workloads.vllm", False),
    "haproxy": ("tctl.workloads.haproxy", False),
    "lmms":    ("tctl.workloads.lmms", True),   # True = hidden from --help
}
```

`_dispatch` checks `sys.argv[1]` against `_WORKLOADS` first; if matched, it
`importlib.import_module`s the workload package and calls `run(ns, argv_rest)`.
If not matched, it checks `_COMMANDS` and dispatches the platform command.
The lazy-import contract from v0.8.0 is preserved — nothing is imported at module
load time.

Each workload `__init__.py` builds its own argparse subparser, dispatches to
`commands.py` functions, and returns an `int` exit code. The dispatch depth is two
levels: `cli.py` → `workloads/<name>/__init__.py` → `workloads/<name>/commands.py`.

### 3.3.1 Positional profile hoister + `_PROFILE_AWARE` after reorg

Today's `_hoist_positional_profile` in `cli.py` rewrites:
  `vctl serve models/qwen3_5-9b.yaml` → `vctl serve --profile qwen3_5-9b`

After reorg, the form becomes two tokens deep:
  `tctl vllm serve models/qwen3_5-9b.yaml` → `tctl vllm serve --profile qwen3_5-9b`

The hoister must:
1. Consume `<workload>` (first non-flag token) — must be in `_WORKLOADS`.
2. Consume `<verb>` (second non-flag token).
3. Check `_PROFILE_AWARE.get(workload, set())` contains the verb. The whitelist becomes nested:

```python
_PROFILE_AWARE: dict[str, set[str]] = {
    "vllm": {"info", "args", "preflight", "serve", "stop"},
    # haproxy / lmms have no profile-aware verbs
}
```

4. If the next positional matches `models/<x>.yaml`, rewrite to `--profile <x>`.

`rolling-restart` is intentionally NOT in `_PROFILE_AWARE` (operates on a pool, not a profile).

### 3.4 Workload `__init__.py` shape (cookbook)

All three workloads follow this pattern:

```python
# src/tctl/workloads/vllm/__init__.py
from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass  # avoid circular imports in type checkers

_VERBS = {
    "info":            "_cmd_info",
    "profiles":        "_cmd_profiles",
    "args":            "_cmd_args",
    "preflight":       "_cmd_preflight",
    "serve":           "_cmd_serve",
    "stop":            "_cmd_stop",
    "rolling-restart": "_cmd_rolling_restart",
}


def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    """Entry point called by cli._dispatch for `tctl vllm <verb>`."""
    from tctl.workloads.vllm import commands as _cmds  # lazy import

    p = argparse.ArgumentParser(prog="tctl vllm")
    sub = p.add_subparsers(dest="verb", metavar="VERB")
    sub.required = True
    # register sub-verbs — each commands._register_<verb>(sub) call adds a subparser
    _cmds.register_all(sub)
    parsed = p.parse_args(argv_rest, namespace=ns)
    fn_name = _VERBS[parsed.verb]
    return getattr(_cmds, fn_name)(parsed, [])  # type: ignore[no-any-return]
```

The `run()` function is the only symbol the CLI layer calls. Everything below it is
private to the workload package.

### 3.5 cluster.yaml v0.9.0 shape

**Before (v0.8.0):**

```yaml
apiVersion: vctl/v1
cluster:
  venv: /path/to/venv
  state_dir: /var/state
  env: {}
lb:
  kind: haproxy
  host: 10.0.0.1
  admin: {bind_port: 9001}
  stats: {bind_port: 9000}
  health: {interval_s: 5, timeout_s: 2, rise: 2, fall: 3}
  defaults: {max_conn: 100, timeout_connect: 5s, timeout_client: 30s, timeout_server: 30s}
  prune: {enabled: false, dry_run: true, min_age_s: 3600}
  pools:
    - name: default
      bind_port: 8000
      served_models: ["*"]
profile: qwen3_5-9b
```

**After (v0.9.0):**

```yaml
apiVersion: tctl/v1
cluster:
  venv: /path/to/venv
  state_dir: /var/state
  env: {}
haproxy:
  kind: haproxy
  host: 10.0.0.1
  admin: {bind_port: 9001}
  stats: {bind_port: 9000}
  health: {interval_s: 5, timeout_s: 2, rise: 2, fall: 3}
  defaults: {max_conn: 100, timeout_connect: 5s, timeout_client: 30s, timeout_server: 30s}
  prune: {enabled: false, dry_run: true, min_age_s: 3600}
  pools:
    - name: default
      bind_port: 8000
      served_models: ["*"]
vllm:
  default_profile: qwen3_5-9b
# Future: lmdeploy: {default_profile: ...}
```

Key shape changes:
- `apiVersion: vctl/v1` → `apiVersion: tctl/v1`
- Top-level `lb:` → `haproxy:` (field rename; inner shape unchanged)
- Top-level `profile: <str>` → `vllm.default_profile: <str>` (moved + scoped)
- New top-level `vllm:` section with `default_profile` subfield

### 3.6 Pydantic schema diff (`config/models.py`)

```python
# BEFORE (v0.8.0 config/models.py)
class ClusterFile(_Strict):
    apiVersion: Literal["vctl/v1"]
    cluster: ClusterSection
    lb: LbHaproxy
    profile: str | None = None

# AFTER (v0.9.0 config/models.py)
class VllmCluster(_Strict):
    """vllm-scoped cluster settings. Currently holds only default_profile."""
    default_profile: str | None = None


class ClusterFile(_Strict):
    apiVersion: Literal["tctl/v1"]
    cluster: ClusterSection
    haproxy: LbHaproxy          # was: lb
    vllm: VllmCluster = Field(default_factory=VllmCluster)
```

`LbHaproxy` class body, `ClusterSection`, pool models, and profile-file models are
**unchanged**. The only modifications are:
1. `apiVersion` literal string: `"vctl/v1"` → `"tctl/v1"`
2. `ClusterFile.lb` renamed to `ClusterFile.haproxy`
3. `ClusterFile.profile` removed; replaced by `ClusterFile.vllm.default_profile`
4. New `VllmCluster` model added

Because all schema classes inherit from `_Strict` (`extra="forbid"`), any old
cluster.yaml that still uses `lb:` or top-level `profile:` will produce a
`ValidationError` with a clear field-unknown message. No silent data loss.

### 3.7 Profile resolution chain update

Old chain (`config/settings.py:resolve_profile_name`):
```
--profile CLI flag
→ $VCTL_PROFILE
→ $MODEL_PROFILE
→ cluster.profile
```

New chain:
```
--profile CLI flag
→ $TCTL_PROFILE
→ cluster.vllm.default_profile
```

`$MODEL_PROFILE` is **dropped**. It was a loose, undocumented escape hatch with no
namespace; operators should use `$TCTL_PROFILE` going forward. `$CLUSTER_CONFIG` is
**unchanged** (already workload-neutral).

### 3.8 Env-var override system (`config/settings.py`)

The `TCTL_<TOPLEVEL>__<NESTED>__...` pattern is preserved unchanged. Only the prefix
changes from `VCTL_` to `TCTL_`. The `_CLUSTER_TOPLEVEL` whitelist is updated:

```python
# BEFORE
_CLUSTER_TOPLEVEL = frozenset({"cluster", "lb", "profile"})

# AFTER
_CLUSTER_TOPLEVEL = frozenset({"cluster", "haproxy", "vllm"})
```

All process-level env sentinels rename too:
- `VCTL_TEST_NO_SOCKET` → `TCTL_TEST_NO_SOCKET`
- `VCTL_KILL_GRACE` → `TCTL_KILL_GRACE`
- `VCTL_READY_TIMEOUT` → `TCTL_READY_TIMEOUT`
- any other `VCTL_*` runtime constants follow the same pattern

---

## 4. Migration Plan / Breaking Changes

This is a full-cut migration. There is no compatibility shim, no `vctl-to-tctl` tool,
and no deprecation period. Operators running v0.8.0 clusters must:

1. Drain all vllm workers from their pools (`tctl haproxy drain ...` is not available
   yet — use the old `vctl lb drain` before upgrading).
2. Stop haproxy (`vctl lb stop`).
3. Uninstall `vctl` (`uv tool uninstall vctl`).
4. Install `tctl` (`uv tool install tctl`).
5. Re-init or hand-edit `cluster.yaml` to the new shape.
6. Re-init the model directory (`~/.tctl/models/`).
7. Restart haproxy (`tctl haproxy start`).
8. Re-serve workers (`tctl vllm serve --profile ...`).

### 4.1 Project artefacts

| Artefact | Before | After |
|---|---|---|
| PyPI package name | `vctl` | `tctl` |
| CLI entry point | `vctl` | `tctl` |
| Python package | `vctl` | `tctl` |
| Source tree root | `src/vctl/` | `src/tctl/` |
| All import paths | `from vctl.X import ...` | `from tctl.X import ...` |
| `pyproject.toml` `[project] name` | `vctl` | `tctl` |
| `pyproject.toml` `[project.scripts]` | `vctl = "vctl.cli:main"` | `tctl = "tctl.cli:main"` |
| `mypy.ini` source path | `src/vctl` | `src/tctl` |
| `__main__.py` | `python -m vctl` | `python -m tctl` |

### 4.2 cluster.yaml schema (BREAKING)

| Field | Before | After | Notes |
|---|---|---|---|
| `apiVersion` | `vctl/v1` | `tctl/v1` | Literal change — old value rejected |
| `lb:` top-level key | present | **removed** | Renamed to `haproxy:` |
| `haproxy:` top-level key | absent | **required** | Same inner shape as old `lb:` |
| `profile:` top-level key | `profile: <str>` | **removed** | Moved to `vllm.default_profile` |
| `vllm:` top-level key | absent | **optional** | `vllm: {default_profile: <str>}` |

Old cluster.yaml with `lb:` produces: `ValidationError: Extra inputs are not permitted
(field: lb)` plus `Field required (field: haproxy)`. Old `profile:` at top level
produces: `ValidationError: Extra inputs are not permitted (field: profile)`.
Both errors name the offending field; operators can fix manually.

### 4.3 State paths (BREAKING)

All state moves from `~/.vctl/` to `~/.tctl/`, with internal subdirectory renames to
match the new workload names.

| Old path | New path |
|---|---|
| `~/.vctl/cluster.yaml` | `~/.tctl/cluster.yaml` |
| `~/.vctl/models/*.yaml` | `~/.tctl/models/*.yaml` |
| `~/.vctl/vllm/<host>/<profile>.pid` | `~/.tctl/vllm/<host>/<profile>.pid` |
| `~/.vctl/vllm/<host>/<profile>.log` | `~/.tctl/vllm/<host>/<profile>.log` |
| `~/.vctl/vllm/<host>/<profile>.cmd.json` | `~/.tctl/vllm/<host>/<profile>.cmd.json` |
| `~/.vctl/vllm/<host>/<profile>.host` | `~/.tctl/vllm/<host>/<profile>.host` |
| `~/.vctl/lb/<lb_host>/<pool>_backends.txt` | `~/.tctl/haproxy/<lb_host>/<pool>_backends.txt` |
| `~/.vctl/lb/<lb_host>/<pool>_backends.lock` | `~/.tctl/haproxy/<lb_host>/<pool>_backends.lock` |
| `~/.vctl/lb/haproxy.cfg` | `~/.tctl/haproxy/haproxy.cfg` |
| `~/.vctl/lb/haproxy.sock` | `~/.tctl/haproxy/haproxy.sock` |
| `~/.vctl/lb/haproxy.pid` | `~/.tctl/haproxy/haproxy.pid` |
| `~/.vctl/lb/watch.pid` | `~/.tctl/haproxy/watch.pid` |
| `~/.vctl/lb/rolling-restart/<pool>.json` | `~/.tctl/vllm/rolling-restart/<pool>.json` |

The rolling-restart session file moves from under `lb/` to under `vllm/` — it is
vllm-owned orchestration state, not haproxy state. Any in-flight v0.8.0 rolling
restart at upgrade time is effectively orphaned. The operator must abort and re-run
`tctl vllm rolling-restart`.

**`_SESSION_DIR` refactor.** Today `src/vctl/commands/rolling_restart.py` has a module-level constant:
  `_SESSION_DIR: Path = Path.home() / ".vctl" / "lb" / "rolling-restart"`

In v0.9.0 this constant is removed. Path derivation moves into a method on `VllmManager`:

```python
class VllmManager:
    def _rolling_restart_session_path(self, pool: str, state_dir: Path | None = None) -> Path:
        """Return <state_dir>/vllm/rolling-restart/<pool>.json.

        Default state_dir: Path.home() / ".tctl"
        If callers want non-default, pass state_dir derived from cluster.state_dir.
        """
        base = (state_dir if state_dir is not None else Path.home() / ".tctl") / "vllm" / "rolling-restart"
        base.mkdir(parents=True, exist_ok=True)
        return base / f"{pool}.json"
```

The `commands.py` (rolling-restart sub-verb) calls this method instead of reading the module constant.

### 4.4 Tmux session names (BREAKING)

| Old name | New name |
|---|---|
| `vctl-lb` | `tctl-haproxy` |
| `vctl-lb-watch` | `tctl-haproxy-watch` |
| `vctl-vllm-<profile>` | `tctl-vllm-<profile>` |
| `vctl-lmmseval` | `tctl-lmms` |

Any tmux sessions from a v0.8.0 install are not detectable by tctl; operators must
kill them manually after draining.

Profile names are constrained by the existing regex in `config/models.py` (pydantic validator on `Profile.name`) to characters matching `[A-Za-z0-9_.-]+`, which is a subset of `_validate_tmux_name`'s accepted character class. So `tctl-vllm-<profile>` is always a valid tmux session name; no extra validation needed.

### 4.5 Env vars (BREAKING)

| Old | New |
|---|---|
| `VCTL_PROFILE` | `TCTL_PROFILE` |
| `VCTL_TEST_NO_SOCKET` | `TCTL_TEST_NO_SOCKET` |
| `VCTL_KILL_GRACE` | `TCTL_KILL_GRACE` |
| `VCTL_READY_TIMEOUT` | `TCTL_READY_TIMEOUT` |
| Any `VCTL_<TOPLEVEL>__...` override | Same with `TCTL_` prefix |
| `MODEL_PROFILE` | **Dropped** — use `TCTL_PROFILE` |
| `CLUSTER_CONFIG` | **Unchanged** |

**Source-code env-var literal renames.** These literals appear in source code (string args to `os.environ.get(...)`):
- `src/tctl/workloads/haproxy/runtime.py` — `VCTL_TEST_NO_SOCKET` → `TCTL_TEST_NO_SOCKET`
- `src/tctl/workloads/vllm/manager.py` — `VCTL_KILL_GRACE` → `TCTL_KILL_GRACE`
- `src/tctl/workloads/vllm/commands.py` — `VCTL_READY_TIMEOUT`, `VLLM_ENGINE_READY_TIMEOUT_S` (vllm-native, KEEP), `VCTL_KILL_GRACE` → `TCTL_*`
- `src/tctl/cli.py` — `CLUSTER_CONFIG` (kept) + any other `VCTL_*` literals

### 4.6 Command structure (BREAKING)

All commands move under a workload namespace. There is no backwards-compat alias.

| Old command | New command |
|---|---|
| `vctl info` | `tctl vllm info` |
| `vctl profiles [list/set]` | `tctl vllm profiles [list/set]` |
| `vctl args` | `tctl vllm args` |
| `vctl preflight` | `tctl vllm preflight` |
| `vctl serve` | `tctl vllm serve` |
| `vctl serve status` | `tctl vllm serve status` |
| `vctl serve restart` | `tctl vllm serve restart` |
| `vctl serve console` | `tctl vllm serve console` |
| `vctl serve logs` | `tctl vllm serve logs` |
| `vctl serve stop` | **Removed** — use `tctl vllm stop` |
| `vctl stop` | `tctl vllm stop` (merged; see §4.7) |
| `vctl rolling-restart --pool X` | `tctl vllm rolling-restart --pool X` |
| `vctl lb start` | `tctl haproxy start` |
| `vctl lb stop` | `tctl haproxy stop` |
| `vctl lb status` | `tctl haproxy status` |
| `vctl lb reload` | `tctl haproxy reload` |
| `vctl lb logs` | `tctl haproxy logs` |
| `vctl lb config` | `tctl haproxy config` |
| `vctl lb health` | `tctl haproxy health` |
| `vctl lb add` | `tctl haproxy add` |
| `vctl lb remove` | `tctl haproxy remove` |
| `vctl lb drain` | `tctl haproxy drain` |
| `vctl lb scaling` | `tctl haproxy scaling` |
| `vctl lb prune` | `tctl haproxy prune` |
| `vctl lmmseval run-loop` | `tctl lmms run-loop` |
| `vctl lmmseval stop` | `tctl lmms stop` |
| `vctl lmmseval status` | `tctl lmms status` |
| `vctl config validate/show/schema` | `tctl config validate/show/schema` |
| `vctl init-config` | `tctl init-config` |

### 4.7 `vctl stop` + `vctl serve stop` merge (Q4=B)

In v0.8.0, `vctl stop` drains the current host from all LB pools and kills any
unsupervised local vllm process tree. `vctl serve stop` kills the tmux-supervised
vllm session.

In v0.9.0, both are replaced by a single `tctl vllm stop` that does all three things
in sequence:

1. Drain this host's vllm endpoint(s) from all LB pools (current `vctl stop` drain
   step).
2. Send `C-c` to the `tctl-vllm-<profile>` tmux session if it exists, then
   `TmuxSession(...).kill(tree=True, grace_s=grace)` (current `vctl serve stop`
   behavior, updated to new session name).
3. Kill any unsupervised local vllm process tree as fallback (current `vctl stop`
   final sweep).

`tctl vllm serve` retains `status / restart / console / logs` sub-verbs. The `stop`
sub-verb is removed from `serve`. There are no flags on `tctl vllm stop` — the
semantics are always "fully remove this worker from the cluster and clean it up".

---

## 5. Tests Strategy

### 5.1 Mechanical updates (every existing test file)

All existing test files require the following mechanical changes:

1. **Import path replacement:** every `from vctl.X import ...` and
   `import vctl.X` becomes `from tctl.X import ...` and `import tctl.X`.

2. **`monkeypatch.setattr` target strings:** every string like
   `"vctl.lb.manager.TmuxSession"` or `"vctl.commands.stop._client"` gets the
   module prefix updated to `tctl` and the path updated for the new layout:
   - `"vctl.lb.manager.*"` → `"tctl.workloads.haproxy.manager.*"`
   - `"vctl.vllm_manager.*"` → `"tctl.workloads.vllm.manager.*"`
   - `"vctl.commands.lb_scaling.*"` → `"tctl.workloads.haproxy.scaling.*"`
   - `"vctl.commands.stop.*"` → `"tctl.workloads.vllm.commands.*"`
   - etc.

3. **cluster.yaml fixture strings:** every inline YAML fixture using `apiVersion:
   vctl/v1` and `lb:` must be updated to `apiVersion: tctl/v1` and `haproxy:`.

4. **Session name strings:** test strings like `"vctl-lb"`, `"vctl-vllm-qwen"`,
   `"vctl-lmmseval"` must be updated to the new names.

5. **Env var strings:** `"VCTL_PROFILE"`, `"VCTL_TEST_NO_SOCKET"`, etc. → `TCTL_*`.

**`tests/conftest.py` exact updates:**

1. Env-var unset list (top of file, autouse fixture): replace
   `("VCTL_PROFILE", "MODEL_PROFILE", "VCTL_LB__HOST", "VCTL_TEST_NO_SOCKET")`
   with
   `("TCTL_PROFILE", "TCTL_HAPROXY__HOST", "TCTL_TEST_NO_SOCKET")` (drop `MODEL_PROFILE`).

2. SIGKILL sweeper (line ~149) `_kill_leaked_serve_processes`: replace
   `"vctl" in cmd_str and "serve" in cmd_str`
   with
   `"tctl" in cmd_str and "vllm" in cmd_str and "serve" in cmd_str`.

3. Any `monkeypatch.setattr("vctl.X.Y", ...)` patches → `monkeypatch.setattr("tctl.X.Y", ...)`.

### 5.2 Test file reorganisation

Tests move from a flat `tests/` layout to a parallel workload-scoped structure:

```
tests/
    conftest.py
    test_cli.py                         # updated CLI dispatch tests
    test_config_models.py               # updated schema tests
    test_config_settings.py             # updated env-var tests
    test_resolver.py                    # updated profile chain tests
    test_duration.py                    # unchanged logic
    test_logging.py                     # unchanged logic
    test_platform.py                    # unchanged logic
    test_tmux.py                        # import path only
    test_smoke.py                       # entry-point name: tctl
    test_commands_config.py             # covers tctl config validate/show/schema — platform-level
    test_commands_init_config.py        # covers tctl init-config — platform-level
    workloads/
        __init__.py
        vllm/
            __init__.py
            test_manager.py             # was test_vllm_manager.py
            test_manager_integration.py # was test_vllm_manager_integration.py
            test_commands_serve.py      # was test_commands_serve.py
            test_commands_stop.py       # was test_commands_stop.py
            test_commands_rolling_restart.py  # was test_commands_rolling_restart.py
            test_commands_readonly.py   # was test_commands_readonly.py (info/args/preflight/profiles)
        haproxy/
            __init__.py
            test_manager.py             # was test_lb_manager_b.py
            test_render.py              # was test_lb_render.py
            test_runtime.py             # was test_lb_runtime_b.py
            test_state.py               # was test_lb_state.py
            test_routing.py             # was test_lb_routing.py
            test_reconciler.py          # was test_lb_reconciler.py
            test_reconciler_integration.py  # was test_lb_reconciler_integration.py
            test_prune.py               # merged from test_lb_prune_candidates.py + test_lb_prune_config.py
            test_scaling.py             # was test_lb_scaling_b.py; also absorbs test_commands_lb_scaling.py
            test_errors.py              # was test_lb_errors.py
            test_commands.py            # was test_commands_lb.py + test_commands_lb_info.py
                                        #   + test_commands_lb_list_health.py + test_commands_lb_prune.py
            test_installer.py           # was test_lb_installer.py
        lmms/
            __init__.py
            test_commands.py            # was test_commands_lmmseval.py (implicitly)
```

The `test_commit_c.py`, `test_commit_d.py`, `test_commit_e.py`, `test_f2_f3_f4.py`,
`test_f7_f8_f10.py`, and `test_coverage_supplement.py` files carry over with import
and path string updates only. Their test logic is unchanged.

### 5.3 New tests for v0.9.0 behaviour

Five new test areas require new test code (not just mechanical updates):

**CLI workload dispatch** (`tests/test_cli.py` — new sections):
- `tctl vllm <verb>` routes to `tctl.workloads.vllm.run`
- `tctl haproxy <verb>` routes to `tctl.workloads.haproxy.run`
- `tctl lmms <verb>` routes without appearing in `--help`
- Unknown workload name produces exit code 2 with a clear error

**New YAML schema** (`tests/test_config_models.py` — new sections):
- `ClusterFile` accepts `haproxy:` + `vllm:` shape
- `ClusterFile` rejects `lb:` (old field name) with `ValidationError`
- `ClusterFile` rejects top-level `profile:` (old field) with `ValidationError`
- `ClusterFile` rejects `apiVersion: vctl/v1` with `ValidationError`
- `VllmCluster` defaults `default_profile` to `None`
- `VllmCluster` with `default_profile: foo` round-trips through pydantic

**Profile resolution chain** (`tests/test_config_settings.py` — new sections):
- `TCTL_PROFILE=foo` is used when set
- `MODEL_PROFILE=foo` is IGNORED (not in resolution chain)
- `cluster.vllm.default_profile` is used as final fallback
- All three env-sentinel test keys updated to `TCTL_*` prefix

**`tctl vllm stop` merge** (`tests/workloads/vllm/test_commands_stop.py` — new):
- Stop path calls drain, then tmux kill, then process sweep — all three
- If tmux session absent, process sweep still runs (fallback path)
- If LB unreachable, stop still kills local vllm (non-fatal drain failure)

**State paths** (`tests/test_config_settings.py` + manager tests):
- `VllmManager` state dir uses `~/.tctl/vllm/`
- `LbManager` state dir uses `~/.tctl/haproxy/`
- Rolling-restart session file at `~/.tctl/vllm/rolling-restart/<pool>.json`

---

## 6. Acceptance Tests

### AT-1 — `tctl --help` lists visible workloads only; lmms is absent

```
Given: tctl is installed from src/tctl/
When:  `tctl --help` is run
Then:  Output contains "vllm" and "haproxy" and "config" and "init-config"
       Output does NOT contain "lmms" anywhere in the help text
       Exit code: 0
```

```python
def test_at1_help_lists_workloads_not_lmms(capsys):
    with pytest.raises(SystemExit) as exc:
        import tctl.cli as cli
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert "vllm" in out
    assert "haproxy" in out
    assert "config" in out
    assert "init-config" in out
    assert "lmms" not in out
    assert exc.value.code == 0
```

---

### AT-2 — `tctl vllm --help` lists all vllm sub-verbs

```
Given: tctl is installed
When:  `tctl vllm --help` is run
Then:  Output contains: info, profiles, args, preflight, serve, stop, rolling-restart
       Output does NOT contain "lb" or "lmmseval" (old names)
       Exit code: 0
```

```python
def test_at2_vllm_help_lists_all_verbs(capsys):
    with pytest.raises(SystemExit) as exc:
        import tctl.cli as cli
        cli.main(["vllm", "--help"])
    out = capsys.readouterr().out
    for verb in ("info", "profiles", "args", "preflight", "serve", "stop", "rolling-restart"):
        assert verb in out, f"expected {verb!r} in vllm --help"
    assert exc.value.code == 0
```

---

### AT-3 — `tctl haproxy --help` lists all haproxy sub-verbs

```
Given: tctl is installed
When:  `tctl haproxy --help` is run
Then:  Output contains: start, stop, status, reload, logs, config, health,
       add, remove, drain, scaling, prune
       Exit code: 0
```

```python
def test_at3_haproxy_help_lists_all_verbs(capsys):
    with pytest.raises(SystemExit) as exc:
        import tctl.cli as cli
        cli.main(["haproxy", "--help"])
    out = capsys.readouterr().out
    for verb in ("start", "stop", "status", "reload", "logs", "config",
                 "health", "add", "remove", "drain", "scaling", "prune"):
        assert verb in out, f"expected {verb!r} in haproxy --help"
    assert exc.value.code == 0
```

---

### AT-4 — `tctl lmms run-loop` is reachable despite being hidden

```
Given: tctl is installed; lmms workload registered with hidden=True
When:  `tctl lmms --help` is run (workload name given explicitly)
Then:  Help output for the lmms workload is shown, containing run-loop, stop, status
       Exit code: 0
When:  `tctl lmms run-loop --help` is invoked with a monkeypatched _cmd_run_loop
Then:  _cmd_run_loop is called (the hidden flag does not block execution)
```

```python
def test_at4_lmms_reachable_when_named_directly(monkeypatch, capsys):
    called = []
    import tctl.workloads.lmms.commands as _cmds
    monkeypatch.setattr(_cmds, "_cmd_run_loop", lambda ns, rest: called.append(1) or 0)
    import tctl.cli as cli
    rc = cli.main(["lmms", "run-loop"])
    assert rc == 0
    assert called, "lmms run-loop was not dispatched"
```

---

### AT-5 — `tctl init-config` scaffolds new-shape YAML

```
Given: a temporary directory with no cluster.yaml
When:  `tctl init-config --dir <tmp>` is run
Then:  <tmp>/cluster.yaml is created
       It contains "apiVersion: tctl/v1"
       It contains a "haproxy:" top-level key (not "lb:")
       It contains a "vllm:" top-level key with "default_profile" subkey
       It does NOT contain "apiVersion: vctl/v1" or a top-level "lb:" key
       It does NOT contain a bare top-level "profile:" key
       Exit code: 0
```

```python
def test_at5_init_config_new_shape(tmp_path):
    import tctl.cli as cli
    rc = cli.main(["init-config", "--dir", str(tmp_path)])
    assert rc == 0
    text = (tmp_path / "cluster.yaml").read_text()
    assert "apiVersion: tctl/v1" in text
    assert "haproxy:" in text
    assert "vllm:" in text
    assert "default_profile" in text
    assert "lb:" not in text
    assert "apiVersion: vctl/v1" not in text
```

---

### AT-6 — `tctl config validate` accepts new shape; rejects old shape

```
Given: a cluster.yaml using "apiVersion: tctl/v1" and "haproxy:" section
When:  `tctl config validate --config <file>` is run
Then:  Exit code: 0, output contains "valid"

Given: a cluster.yaml using "apiVersion: vctl/v1" and "lb:" section  (old shape)
When:  `tctl config validate --config <old_file>` is run
Then:  Exit code: 2 (config error)
       Stderr contains a ValidationError message referencing "lb" or "apiVersion"

Given: a cluster.yaml with top-level "profile: foo"  (old field)
When:  `tctl config validate --config <old_profile_file>` is run
Then:  Exit code: 2; stderr contains "profile"
```

```python
NEW_YAML = """
apiVersion: tctl/v1
cluster:
  venv: /venv
  state_dir: /tmp/state
haproxy:
  kind: haproxy
  host: 127.0.0.1
  admin: {bind_port: 9001}
  stats: {bind_port: 9000}
  pools: [{name: default, bind_port: 8000, served_models: ["*"]}]
vllm:
  default_profile: null
"""

OLD_YAML = NEW_YAML.replace("apiVersion: tctl/v1", "apiVersion: vctl/v1")\
                   .replace("haproxy:", "lb:")

def test_at6_validate_new_shape_ok(tmp_path):
    f = tmp_path / "cluster.yaml"
    f.write_text(NEW_YAML)
    import tctl.cli as cli
    assert cli.main(["config", "validate", "--config", str(f)]) == 0

def test_at6_validate_old_shape_rejects(tmp_path):
    f = tmp_path / "cluster.yaml"
    f.write_text(OLD_YAML)
    import tctl.cli as cli
    assert cli.main(["config", "validate", "--config", str(f)]) == 2
```

---

### AT-7 — `TCTL_PROFILE=foo` resolves; `MODEL_PROFILE=foo` is ignored

```
Given: $TCTL_PROFILE=myprofile is set in the environment
       No --profile flag, no cluster.vllm.default_profile
When:  Profile name is resolved via resolve_profile_name()
Then:  Returns "myprofile"
       Exit code: 0

Given: $MODEL_PROFILE=oldprofile is set; $TCTL_PROFILE is unset
When:  Profile name is resolved
Then:  Returns None (MODEL_PROFILE is not consulted)
```

```python
def test_at7_tctl_profile_used(monkeypatch):
    monkeypatch.setenv("TCTL_PROFILE", "myprofile")
    monkeypatch.delenv("MODEL_PROFILE", raising=False)
    from tctl.config.settings import resolve_profile_name
    from tctl.config.models import ClusterFile, ClusterSection, LbHaproxy, VllmCluster
    # build minimal ClusterFile with no default_profile
    cf = ClusterFile(
        apiVersion="tctl/v1",
        cluster=ClusterSection(venv="/venv"),
        haproxy=_minimal_haproxy(),
        vllm=VllmCluster(default_profile=None),
    )
    assert resolve_profile_name(None, cf) == "myprofile"

def test_at7_model_profile_ignored(monkeypatch):
    monkeypatch.setenv("MODEL_PROFILE", "oldprofile")
    monkeypatch.delenv("TCTL_PROFILE", raising=False)
    from tctl.config.settings import resolve_profile_name
    cf = _minimal_cluster_file()
    assert resolve_profile_name(None, cf) is None
```

---

### AT-8 — `tctl vllm stop` performs drain + tmux-kill + process-sweep (3-in-1)

```
Given: a running vllm worker registered in LB pool "default"
       TmuxSession("tctl-vllm-myprofile") exists with pane_pid=1234
When:  `tctl vllm stop --profile myprofile` is run
Then:  1. LB drain call is issued for this host's endpoint in pool "default"
       2. TmuxSession("tctl-vllm-myprofile").kill(tree=True) is called
       3. A process-tree sweep for any remaining vllm process is performed
       4. Exit code: 0
       Note: If drain fails (LB unreachable), steps 2-3 still execute (non-fatal)
```

```python
def test_at8_stop_drain_kill_sweep(monkeypatch):
    drain_calls = []
    kill_calls = []
    sweep_calls = []

    monkeypatch.setattr("tctl.workloads.vllm.commands._drain_from_lb",
                        lambda profile, cfg: drain_calls.append(profile))
    monkeypatch.setattr("tctl.workloads.vllm.commands._kill_tmux_session",
                        lambda name: kill_calls.append(name))
    monkeypatch.setattr("tctl.workloads.vllm.commands._sweep_local_vllm",
                        lambda: sweep_calls.append(True))

    import tctl.cli as cli
    rc = cli.main(["vllm", "stop", "--profile", "myprofile",
                   "--config", str(_write_minimal_cluster())])
    assert rc == 0
    assert drain_calls == ["myprofile"]
    assert "tctl-vllm-myprofile" in kill_calls
    assert sweep_calls
```

---

### AT-9 — Rolling-restart reads/writes session file at new `~/.tctl/vllm/rolling-restart/` path

```
Given: tctl configured with state_dir=~/.tctl
When:  `tctl vllm rolling-restart --pool foo` starts a new rolling restart
Then:  Session file written to ~/.tctl/vllm/rolling-restart/foo.json
       NOT to ~/.tctl/haproxy/rolling-restart/foo.json (old location)
       NOT to ~/.tctl/lb/rolling-restart/foo.json (very old location)
```

```python
def test_at9_rolling_restart_session_path(tmp_path, monkeypatch):
    from tctl.workloads.vllm.manager import VllmManager
    state_dir = tmp_path / ".tctl"
    # confirm session path construction
    expected = state_dir / "vllm" / "rolling-restart" / "foo.json"
    manager = VllmManager.__new__(VllmManager)
    actual = manager._rolling_restart_session_path("foo", state_dir=state_dir)
    assert actual == expected
```

---

### AT-10 — Tmux session names use `tctl-` prefix

```
Given: VllmManager("myprofile", ...) is initialised
When:  manager.session_name is accessed
Then:  Returns "tctl-vllm-myprofile"  (not "vctl-vllm-myprofile")

Given: LbManager(...) is initialised
When:  manager.session_name is accessed
Then:  Returns "tctl-haproxy"  (not "vctl-lb")
```

```python
def test_at10_vllm_session_name(minimal_config):
    from tctl.workloads.vllm.manager import VllmManager
    mgr = VllmManager("myprofile", minimal_config)
    assert mgr.session_name == "tctl-vllm-myprofile"
    assert "vctl" not in mgr.session_name

def test_at10_haproxy_session_name(minimal_lb_config):
    from tctl.workloads.haproxy.manager import LbManager
    mgr = LbManager(minimal_lb_config)
    assert mgr.session_name == "tctl-haproxy"
    assert "vctl" not in mgr.session_name
```

---

### AT-11 — `MODEL_PROFILE` env var is ignored; only `TCTL_PROFILE` resolves

```
Given: $MODEL_PROFILE=legacymodel is set; $TCTL_PROFILE is unset
       cluster.vllm.default_profile is null
When:  Profile resolution runs (e.g. `tctl vllm info` with no --profile flag)
Then:  Profile name is None (not "legacymodel")
       tctl exits with config error (exit 2): "no profile selected"
       NOT: tctl uses "legacymodel"
```

```python
def test_at11_model_profile_not_consulted(monkeypatch, tmp_path):
    monkeypatch.setenv("MODEL_PROFILE", "legacymodel")
    monkeypatch.delenv("TCTL_PROFILE", raising=False)
    cfg_file = _write_cluster_no_default_profile(tmp_path)
    import tctl.cli as cli
    rc = cli.main(["vllm", "info", "--config", str(cfg_file)])
    # Should exit 2 (no profile), not attempt to load "legacymodel"
    assert rc == 2
```

---

### AT-12 — Adding a new workload follows the cookbook; no core changes needed

```
Given: A developer creates src/tctl/workloads/demo/__init__.py (exports run(ns, argv_rest)->int)
       and src/tctl/workloads/demo/commands.py (exports _cmd_hello returning 0)
       and adds one line to cli.py _WORKLOADS: "demo": ("tctl.workloads.demo", False)
When:  `tctl demo hello` is run
Then:  _cmd_hello is dispatched; exit code 0
       No other file in src/tctl/ was modified
       `tctl --help` now lists "demo"
```

```python
def test_at12_new_workload_requires_only_3_steps(tmp_path, monkeypatch):
    # Simulate a new workload package registered in _WORKLOADS without touching any
    # existing module. Use monkeypatch to inject the workload at test time.
    called = []

    def fake_run(ns, argv_rest):
        called.append(argv_rest)
        return 0

    import tctl.cli as cli
    original = dict(cli._WORKLOADS)
    try:
        cli._WORKLOADS["demo"] = ("tctl.workloads.demo", False)
        monkeypatch.syspath_prepend(str(tmp_path))
        # write minimal workload package
        pkg = tmp_path / "tctl" / "workloads" / "demo"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text(
            "def run(ns, argv_rest):\n"
            "    return 0\n"
        )
        rc = cli.main(["demo", "--help"])
    finally:
        cli._WORKLOADS.clear()
        cli._WORKLOADS.update(original)
    assert rc == 0
```

---

## 7. Risks / Non-Goals

### Non-Goals

- **No generic `tctl run/ls/logs` verbs.** The identity of `tctl` is as a workload
  controller, not a generic process runner. Top-level verbs without a workload namespace
  are rejected.

- **No plugin system.** Workloads are Python packages in `src/tctl/workloads/`. There
  is no external plugin registry, no entry-point auto-discovery, no dynamic loading of
  third-party workload packages. The cookbook convention is sufficient.

- **No `WorkloadManager` base class.** A shared base class would be premature
  abstraction: `VllmManager`, `LbManager`, and the future lmdeploy manager all have
  different signatures, different state shapes, and different lifecycle contracts.
  The cookbook documents the convention (use `TmuxSession`, expose
  `start/stop/status`, write state under `~/.tctl/<workload>/`). Inheritance would
  enforce a false uniformity.

- **No migration tool for v0.8.0 deployments.** No `tctl migrate` command, no
  auto-detection of `~/.vctl/` on startup, no `vctl-to-tctl` shim. The operator
  re-inits. This is a pre-1.0 tool; the user base is small and capable.

- **No deprecation period.** v0.9.0 is the single cut-over point. `vctl` and `tctl`
  coexist on PyPI; the operator uninstalls one and installs the other.

### Risks

**Name collision with Temporal's `tctl`.** Temporal's v1.x CLI was also named `tctl`
(since superseded by `temporal` in v2). The two tools have no conflict at the binary
level (different PyPI packages, different install mechanisms), but operators who have
both could be confused. **Mitigation:** Document in README that this `tctl` is
unrelated to Temporal. The PyPI package and import namespace are both `tctl`; the
Temporal package is `temporalio`/`temporal-cli` — no import namespace collision.

**Rolling-restart orphan on upgrade.** Any rolling restart in progress at the moment
of upgrade (v0.8.0 → v0.9.0) writes its session file to `~/.vctl/lb/rolling-restart/`.
tctl does not read that path. The restart is silently orphaned. **Mitigation:** The
`tctl haproxy status` command can detect backends in MAINT state (leftover from the
drain phase) and warn the operator. No code change required in v0.9.0 — just document
in CHANGELOG and upgrade notes.

**Leaked tmux sessions.** Any `vctl-lb`, `vctl-vllm-*`, or `vctl-lmmseval` sessions
still running after upgrade are invisible to `tctl status` commands. **Mitigation:**
Operators must run `tmux ls` after upgrading and manually kill any `vctl-*` sessions.
Document in upgrade notes.

**Test coverage gate.** The test reorganisation moves and renames ~40 test files. If
any tests are accidentally dropped during the mechanical rename, the coverage gate
(`--cov-fail-under=50`) may not catch it. **Mitigation:** Run
`pytest --co -q | wc -l` before and after the rename to verify test count is
unchanged. CI matrix on Python 3.10/3.11/3.12 provides a second signal.

**`__all__` in scaling module.** `commands/lb_scaling.py` re-exports `_client`,
`_NoOpClient`, and `_name_for` via `__all__` for mypy strict compliance (documented in
CLAUDE.md). This pattern carries forward to `workloads/haproxy/scaling.py` — the
`__all__` must be preserved and the monkeypatch targets in scaling tests updated.

---

## 8. File Map

### New files (create)

| Path | Notes |
|---|---|
| `src/tctl/__init__.py` | package root; copy from `src/vctl/__init__.py` |
| `src/tctl/__main__.py` | copy + update import |
| `src/tctl/py.typed` | empty file; PEP 561 marker (copy from `src/vctl/py.typed`) |
| `src/tctl/cli.py` | rewrite dispatch for `_WORKLOADS` dict |
| `src/tctl/tmux.py` | copy verbatim — path change only |
| `src/tctl/platform.py` | copy + update import paths |
| `src/tctl/duration.py` | copy verbatim — no vctl refs |
| `src/tctl/resolver.py` | copy + update import paths |
| `src/tctl/logging.py` | copy + update import paths |
| `src/tctl/config/__init__.py` | copy verbatim |
| `src/tctl/config/models.py` | update: apiVersion literal, lb→haproxy field, VllmCluster |
| `src/tctl/config/settings.py` | update: TCTL_ prefix, _CLUSTER_TOPLEVEL whitelist, drop MODEL_PROFILE |
| `src/tctl/config/yaml_source.py` | copy + update import paths |
| `src/tctl/commands/__init__.py` | empty init |
| `src/tctl/commands/config_cmd.py` | copy from `src/vctl/commands/config_cmd.py` + update imports |
| `src/tctl/commands/init_config.py` | copy + update imports + update scaffold template |
| `src/tctl/workloads/__init__.py` | empty init |
| `src/tctl/workloads/vllm/__init__.py` | new workload dispatcher |
| `src/tctl/workloads/vllm/manager.py` | move from `src/vctl/vllm_manager.py` + update |
| `src/tctl/workloads/vllm/commands.py` | move + merge from `src/vctl/commands/` (info, profiles, args, preflight, serve, stop, rolling_restart) |
| `src/tctl/workloads/vllm/templates.py` | move from `src/vctl/commands/templates.py` + update scaffold |
| `src/tctl/workloads/haproxy/__init__.py` | new workload dispatcher |
| `src/tctl/workloads/haproxy/manager.py` | move from `src/vctl/lb/manager.py` + update |
| `src/tctl/workloads/haproxy/render.py` | move from `src/vctl/lb/render.py` + update imports |
| `src/tctl/workloads/haproxy/runtime.py` | move from `src/vctl/lb/runtime.py` + update imports |
| `src/tctl/workloads/haproxy/state.py` | move from `src/vctl/lb/state.py` + update state path prefix |
| `src/tctl/workloads/haproxy/routing.py` | move from `src/vctl/lb/routing.py` + update imports |
| `src/tctl/workloads/haproxy/reconciler.py` | move from `src/vctl/lb/reconciler.py` + update imports |
| `src/tctl/workloads/haproxy/errors.py` | move from `src/vctl/lb/errors.py` + update imports |
| `src/tctl/workloads/haproxy/prune.py` | move from `src/vctl/lb/prune.py` + update imports |
| `src/tctl/workloads/haproxy/scaling.py` | move from `src/vctl/commands/lb_scaling.py` + update imports + preserve `__all__` |
| `src/tctl/workloads/haproxy/installer.py` | move from `src/vctl/lb/installer.py` + update imports |
| `src/tctl/workloads/haproxy/probe.py` | move from `src/vctl/lb/probe.py` + update imports |
| `src/tctl/workloads/haproxy/commands.py` | move + merge from `src/vctl/commands/lb.py` + update |
| `src/tctl/workloads/lmms/__init__.py` | new workload dispatcher (hidden=True) |
| `src/tctl/workloads/lmms/commands.py` | move from `src/vctl/commands/lmmseval.py` + update session name |
| `tests/workloads/__init__.py` | new |
| `tests/workloads/vllm/__init__.py` | new |
| `tests/workloads/vllm/test_manager.py` | moved from `tests/test_vllm_manager.py` |
| `tests/workloads/vllm/test_manager_integration.py` | moved from `tests/test_vllm_manager_integration.py` |
| `tests/workloads/vllm/test_commands_serve.py` | moved from `tests/test_commands_serve.py` |
| `tests/workloads/vllm/test_commands_stop.py` | moved from `tests/test_commands_stop.py` (extended for merge) |
| `tests/workloads/vllm/test_commands_rolling_restart.py` | moved from `tests/test_commands_rolling_restart.py` |
| `tests/workloads/vllm/test_commands_readonly.py` | moved from `tests/test_commands_readonly.py` |
| `tests/workloads/haproxy/__init__.py` | new |
| `tests/workloads/haproxy/test_manager.py` | moved from `tests/test_lb_manager_b.py` |
| `tests/workloads/haproxy/test_render.py` | moved from `tests/test_lb_render.py` |
| `tests/workloads/haproxy/test_runtime.py` | moved from `tests/test_lb_runtime_b.py` |
| `tests/workloads/haproxy/test_state.py` | moved from `tests/test_lb_state.py` |
| `tests/workloads/haproxy/test_routing.py` | moved from `tests/test_lb_routing.py` |
| `tests/workloads/haproxy/test_reconciler.py` | moved from `tests/test_lb_reconciler.py` |
| `tests/workloads/haproxy/test_reconciler_integration.py` | moved from `tests/test_lb_reconciler_integration.py` |
| `tests/workloads/haproxy/test_prune.py` | merged from `tests/test_lb_prune_candidates.py` + `tests/test_lb_prune_config.py` |
| `tests/workloads/haproxy/test_scaling.py` | moved from `tests/test_lb_scaling_b.py` |
| `tests/workloads/haproxy/test_errors.py` | moved from `tests/test_lb_errors.py` |
| `tests/workloads/haproxy/test_commands.py` | merged from `tests/test_commands_lb.py` + `test_commands_lb_info.py` + `test_commands_lb_list_health.py` + `test_commands_lb_prune.py` |
| `tests/workloads/haproxy/test_installer.py` | moved from `tests/test_lb_installer.py` |
| `tests/workloads/lmms/__init__.py` | new |
| `tests/workloads/lmms/test_commands.py` | new: lmms dispatch + run-loop/stop/status |
| `docs/COOKBOOK-workloads.md` | new: step-by-step guide for adding a new workload |

### Modified files (in place)

| Path | Change summary |
|---|---|
| `pyproject.toml` | `name = "tctl"`, entry point `tctl = "tctl.cli:main"`, update version to `0.9.0` |
| `mypy.ini` | `files = src/tctl` |
| `.ruff.toml` | `target-version = py310` (unchanged); no package-specific settings |
| `.github/workflows/ci.yml` | `mypy --strict src/tctl`; no `VCTL_*` env vars present — no env-var changes needed |
| `src/tctl/workloads/haproxy/runtime.py` | rename `VCTL_*` literals to `TCTL_*` |
| `src/tctl/workloads/vllm/manager.py` | rename `VCTL_*` literals to `TCTL_*` |
| `src/tctl/workloads/vllm/commands.py` | rename `VCTL_*` literals to `TCTL_*`; keep `VLLM_ENGINE_READY_TIMEOUT_S` unchanged |
| `src/tctl/cli.py` | rename any `VCTL_*` literals to `TCTL_*`; keep `CLUSTER_CONFIG` unchanged |
| `tests/conftest.py` | update sweeper cmdline patterns: `vctl serve` → `tctl vllm serve`; `vctl-*` tmux references |
| `tests/test_cli.py` | new dispatch tests for workload routing; mechanical import updates |
| `tests/test_config_models.py` | new schema tests; mechanical updates |
| `tests/test_config_settings.py` | new profile chain tests; env var rename tests |
| `tests/test_resolver.py` | import + cluster.yaml fixture updates |
| `tests/test_tmux.py` | import path: `vctl.tmux` → `tctl.tmux` |
| `tests/test_smoke.py` | entry point: `vctl` → `tctl` |
| `tests/test_commands_config.py` | imports `vctl.*` → `tctl.*`; fixtures updated to new cluster.yaml shape (apiVersion, haproxy:, vllm:) |
| `tests/test_commands_init_config.py` | imports `vctl.*` → `tctl.*`; fixtures updated to new cluster.yaml shape (apiVersion, haproxy:, vllm:) |
| `tests/test_platform.py` | import path update |
| `tests/test_duration.py` | import path update |
| `tests/test_logging.py` | import path update |
| `tests/test_commit_c.py` | import + fixture updates |
| `tests/test_commit_d.py` | import + fixture updates |
| `tests/test_commit_e.py` | import + fixture updates |
| `tests/test_f2_f3_f4.py` | import + fixture updates |
| `tests/test_f7_f8_f10.py` | import + fixture updates |
| `tests/test_coverage_supplement.py` | import + fixture updates |
| `tests/test_integration_endtoend.py` | import + fixture updates |
| `CLAUDE.md` | update all `vctl` command references, source paths (`src/vctl` → `src/tctl`), state paths (`~/.vctl` → `~/.tctl`), tmux session names (`vctl-lb` → `tctl-haproxy`, etc.), gotchas section |
| `CHANGELOG.md` | v0.9.0 entry |
| `README.md` | rename + workload-command docs + tctl name explanation |
| `examples/cluster.yaml` | update to v0.9.0 shape |
| `examples/*.yaml` (profile examples) | apiVersion if present |

### Deleted files (from `src/vctl/`)

| Path | Replacement |
|---|---|
| `src/vctl/__init__.py` | `src/tctl/__init__.py` |
| `src/vctl/__main__.py` | `src/tctl/__main__.py` |
| `src/vctl/py.typed` | `src/tctl/py.typed` |
| `src/vctl/cli.py` | `src/tctl/cli.py` |
| `src/vctl/tmux.py` | `src/tctl/tmux.py` |
| `src/vctl/platform.py` | `src/tctl/platform.py` |
| `src/vctl/duration.py` | `src/tctl/duration.py` |
| `src/vctl/resolver.py` | `src/tctl/resolver.py` |
| `src/vctl/logging.py` | `src/tctl/logging.py` |
| `src/vctl/config/` (whole subtree) | `src/tctl/config/` |
| `src/vctl/commands/` (whole subtree) | split: platform cmds → `src/tctl/commands/`; workload cmds → `src/tctl/workloads/*/commands.py` |
| `src/vctl/lb/installer.py` | `src/tctl/workloads/haproxy/installer.py` |
| `src/vctl/lb/probe.py` | `src/tctl/workloads/haproxy/probe.py` |
| `src/vctl/lb/` (whole subtree) | `src/tctl/workloads/haproxy/` |
| `src/vctl/vllm_manager.py` | `src/tctl/workloads/vllm/manager.py` |
| `tests/test_lb_manager_b.py` | `tests/workloads/haproxy/test_manager.py` |
| `tests/test_lb_render.py` | `tests/workloads/haproxy/test_render.py` |
| `tests/test_lb_runtime_b.py` | `tests/workloads/haproxy/test_runtime.py` |
| `tests/test_lb_state.py` | `tests/workloads/haproxy/test_state.py` |
| `tests/test_lb_routing.py` | `tests/workloads/haproxy/test_routing.py` |
| `tests/test_lb_reconciler.py` | `tests/workloads/haproxy/test_reconciler.py` |
| `tests/test_lb_reconciler_integration.py` | `tests/workloads/haproxy/test_reconciler_integration.py` |
| `tests/test_lb_prune_candidates.py` | merged into `tests/workloads/haproxy/test_prune.py` |
| `tests/test_lb_prune_config.py` | merged into `tests/workloads/haproxy/test_prune.py` |
| `tests/test_lb_scaling_b.py` | `tests/workloads/haproxy/test_scaling.py` |
| `tests/test_commands_lb_scaling.py` | merged into `tests/workloads/haproxy/test_scaling.py` |
| `tests/test_lb_errors.py` | `tests/workloads/haproxy/test_errors.py` |
| `tests/test_commands_lb.py` | merged into `tests/workloads/haproxy/test_commands.py` |
| `tests/test_commands_lb_info.py` | merged into `tests/workloads/haproxy/test_commands.py` |
| `tests/test_commands_lb_list_health.py` | merged into `tests/workloads/haproxy/test_commands.py` |
| `tests/test_commands_lb_prune.py` | merged into `tests/workloads/haproxy/test_commands.py` |
| `tests/test_lb_installer.py` | `tests/workloads/haproxy/test_installer.py` |
| `tests/test_vllm_manager.py` | `tests/workloads/vllm/test_manager.py` |
| `tests/test_vllm_manager_integration.py` | `tests/workloads/vllm/test_manager_integration.py` |
| `tests/test_commands_serve.py` | `tests/workloads/vllm/test_commands_serve.py` |
| `tests/test_commands_stop.py` | `tests/workloads/vllm/test_commands_stop.py` |
| `tests/test_commands_rolling_restart.py` | `tests/workloads/vllm/test_commands_rolling_restart.py` |
| `tests/test_commands_readonly.py` | `tests/workloads/vllm/test_commands_readonly.py` |

---

## 9. Tech Stack

No new runtime or build dependencies. All tooling versions and versions of existing
dependencies are unchanged.

- **Python 3.10+** — `from __future__ import annotations` in all new modules.
  Union syntax `X | Y` requires this import for 3.10 compatibility; it is already
  present throughout the codebase.

- **Pydantic v2** — `config/models.py` changes are additive (new `VllmCluster` class)
  and one rename (`lb` → `haproxy`). No Pydantic API changes needed. All classes
  continue to inherit from `_Strict` (`extra="forbid"`).

- **hatchling** — build backend unchanged. `pyproject.toml` `[tool.hatch.build]`
  `include` path changes from `src/vctl` to `src/tctl`. The `packages` list in
  `[tool.hatch.build.targets.wheel]` changes from `["src/vctl"]` to `["src/tctl"]`.

- **mypy --strict** — `mypy.ini` target changes from `src/vctl` to `src/tctl`.
  The `lb_scaling.py` / `__all__` pattern that enables strict re-export checking
  carries forward identically to `workloads/haproxy/scaling.py`.

- **ruff** — `.ruff.toml` is path-agnostic; no changes needed. `target-version =
  py310`, `line-length = 100`, double-quote format enforced as before.

- **uv** — project's canonical tool runner. `uv pip install -e ".[dev,migrate]"` works
  after the package rename; the editable install points at the new `src/tctl` tree.

- **tmux 3.2+** — `TmuxSession` requirement carried forward unchanged. The deployed
  environment uses tmux 3.4.

- **psutil** — runtime dependency unchanged.

- **subprocess / argparse / importlib** — stdlib usage unchanged. The lazy-import
  dispatch pattern (sub-200ms startup) carries forward to the new two-level dispatch
  (`cli.py` → `workloads/__init__.py` → `commands.py`).

---

## Appendix A — Cookbook: Adding a New Workload

*This appendix is the source for `docs/COOKBOOK-workloads.md`.*

Adding a new workload (e.g. `lmdeploy`) requires exactly three steps and touches
no existing files.

### Step 1 — Create the workload package

```
src/tctl/workloads/lmdeploy/
    __init__.py     # exports run(ns, argv_rest) -> int
    manager.py      # LmdeployManager using TmuxSession
    commands.py     # sub-verbs: serve / stop / status / ...
```

**`__init__.py` minimal template:**

```python
from __future__ import annotations
import argparse

_VERBS: dict[str, str] = {
    "serve":  "_cmd_serve",
    "stop":   "_cmd_stop",
    "status": "_cmd_status",
}


def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    from tctl.workloads.lmdeploy import commands as _cmds  # lazy

    p = argparse.ArgumentParser(prog="tctl lmdeploy")
    sub = p.add_subparsers(dest="verb", metavar="VERB")
    sub.required = True
    _cmds.register_all(sub)
    parsed = p.parse_args(argv_rest, namespace=ns)
    return getattr(_cmds, _VERBS[parsed.verb])(parsed, [])  # type: ignore[no-any-return]
```

**`manager.py` skeleton:**

```python
from __future__ import annotations
from pathlib import Path
from tctl.tmux import TmuxSession   # ← always use this, never raw subprocess


class LmdeployManager:
    SESSION_PREFIX = "tctl-lmdeploy"

    def __init__(self, profile: str, resolved_cfg: object) -> None:
        self.profile = profile
        self.session_name = f"{self.SESSION_PREFIX}-{profile}"
        self._session = TmuxSession(self.session_name)

    def start(self, argv: list[str], log_path: Path | None = None) -> None:
        import os
        sess = TmuxSession(self.session_name, env=dict(os.environ), log_path=log_path)
        sess.start(argv)

    def stop(self, grace_s: float = 5.0) -> None:
        TmuxSession(self.session_name).kill(tree=True, grace_s=grace_s)

    def status(self) -> bool:
        return TmuxSession(self.session_name).exists()
```

**State convention:** write all state files under
`<state_dir>/lmdeploy/<host>/<profile>.*` — never under `haproxy/` or `vllm/`.

### Step 2 — Register in `cli.py` (one line)

```python
# src/tctl/cli.py
_WORKLOADS: dict[str, tuple[str, bool]] = {
    "vllm":      ("tctl.workloads.vllm",      False),
    "haproxy":   ("tctl.workloads.haproxy",   False),
    "lmms":      ("tctl.workloads.lmms",      True),
    "lmdeploy":  ("tctl.workloads.lmdeploy",  False),  # ← add this line
}
```

That is the only change to any existing file.

### Step 3 (optional) — Extend `cluster.yaml` schema

If the workload needs cluster-level config (e.g. `lmdeploy.default_profile`), add a
Pydantic model to `config/models.py` and a field to `ClusterFile`:

```python
# config/models.py
class LmdeployCluster(_Strict):
    default_profile: str | None = None

class ClusterFile(_Strict):
    apiVersion: Literal["tctl/v1"]
    cluster: ClusterSection
    haproxy: LbHaproxy
    vllm: VllmCluster = Field(default_factory=VllmCluster)
    lmdeploy: LmdeployCluster = Field(default_factory=LmdeployCluster)  # ← add
```

Update `_CLUSTER_TOPLEVEL` in `config/settings.py` to include `"lmdeploy"`.

### Tests

Place tests in `tests/workloads/lmdeploy/`. The `conftest.py` sweepers will catch
any leaked tmux sessions or processes that route artifacts through `tmp_path`.

---

*This spec covers v0.9.0. TmuxSession (v0.8.0) is unchanged and its design spec
remains at `docs/super-agent-skills/specs/2026-05-06-tmux-session-mgmt-design.md`.*
