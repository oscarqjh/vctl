# tctl Rename + Workload Reorg Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use super-agent-skills:subagent-driven-development.

**Goal:** Rename `vctl` → `tctl` and reorganize the codebase into workload sub-trees (`tctl vllm`, `tctl haproxy`, `tctl lmms`) so future workloads (e.g. `tctl lmdeploy`) can be added by following a cookbook pattern. Breaking changes: cluster.yaml schema, env vars, state paths, tmux session names. No backwards compat. v0.8.0 → v0.9.0.

**Architecture:** New package root `src/tctl/`. Workloads under `src/tctl/workloads/{vllm,haproxy,lmms}/`. Platform commands stay at `src/tctl/commands/` (`config`, `init-config`). TmuxSession (`src/tctl/tmux.py`) unchanged. No `WorkloadManager` base class — cookbook convention only.

**Tech Stack:** Python 3.10+, pydantic v2, hatchling, mypy --strict, ruff E,F,W,I,B,UP,SIM,N. tmux 3.2+.

---

## File Map

### New files (create)

| Path | Notes |
|---|---|
| `src/tctl/__init__.py` | package root; `__version__ = "0.9.0"` |
| `src/tctl/__main__.py` | copy + update import path |
| `src/tctl/py.typed` | PEP 561 marker |
| `src/tctl/cli.py` | new two-level dispatch with `_WORKLOADS` dict |
| `src/tctl/tmux.py` | verbatim copy — path change only |
| `src/tctl/platform.py` | copy + update internal imports |
| `src/tctl/duration.py` | verbatim copy — no vctl refs |
| `src/tctl/resolver.py` | copy + update import paths |
| `src/tctl/logging.py` | copy + update import paths |
| `src/tctl/config/__init__.py` | empty init |
| `src/tctl/config/models.py` | update: apiVersion literal, `lb`→`haproxy`, `VllmCluster`, drop `profile` top-level |
| `src/tctl/config/settings.py` | update: `TCTL_` prefix, `_CLUSTER_TOPLEVEL`, drop `MODEL_PROFILE` |
| `src/tctl/config/yaml_source.py` | copy + update import paths |
| `src/tctl/commands/__init__.py` | empty init |
| `src/tctl/commands/config_cmd.py` | copy + update imports |
| `src/tctl/commands/init_config.py` | copy + update imports + new scaffold template |
| `src/tctl/workloads/__init__.py` | empty init |
| `src/tctl/workloads/vllm/__init__.py` | workload dispatcher (`run(ns, argv_rest) -> int`) |
| `src/tctl/workloads/vllm/manager.py` | from `src/vctl/vllm_manager.py` + updated session names, paths, env literals |
| `src/tctl/workloads/vllm/commands.py` | merge: info, profiles, args_cmd, preflight, serve, stop, rolling_restart |
| `src/tctl/workloads/vllm/templates.py` | from `src/vctl/commands/templates.py` + new YAML shape |
| `src/tctl/workloads/haproxy/__init__.py` | workload dispatcher |
| `src/tctl/workloads/haproxy/manager.py` | from `src/vctl/lb/manager.py` + updated session/paths |
| `src/tctl/workloads/haproxy/render.py` | from `src/vctl/lb/render.py` + updated imports |
| `src/tctl/workloads/haproxy/runtime.py` | from `src/vctl/lb/runtime.py` + `TCTL_TEST_NO_SOCKET` |
| `src/tctl/workloads/haproxy/state.py` | from `src/vctl/lb/state.py` + `~/.tctl/haproxy/` paths |
| `src/tctl/workloads/haproxy/routing.py` | from `src/vctl/lb/routing.py` + updated imports |
| `src/tctl/workloads/haproxy/reconciler.py` | from `src/vctl/lb/reconciler.py` + updated imports |
| `src/tctl/workloads/haproxy/errors.py` | from `src/vctl/lb/errors.py` + updated imports |
| `src/tctl/workloads/haproxy/prune.py` | from `src/vctl/lb/prune.py` + updated imports |
| `src/tctl/workloads/haproxy/scaling.py` | from `src/vctl/commands/lb_scaling.py` + preserve `__all__` |
| `src/tctl/workloads/haproxy/installer.py` | from `src/vctl/lb/installer.py` + updated imports |
| `src/tctl/workloads/haproxy/probe.py` | from `src/vctl/lb/probe.py` + updated imports |
| `src/tctl/workloads/haproxy/commands.py` | merge: `src/vctl/commands/lb.py` content |
| `src/tctl/workloads/lmms/__init__.py` | workload dispatcher (hidden) |
| `src/tctl/workloads/lmms/commands.py` | from `src/vctl/commands/lmmseval.py` + `tctl-lmms` session name |
| `tests/workloads/__init__.py` | new |
| `tests/workloads/vllm/__init__.py` | new |
| `tests/workloads/vllm/test_manager.py` | from `tests/test_vllm_manager.py` |
| `tests/workloads/vllm/test_manager_integration.py` | from `tests/test_vllm_manager_integration.py` |
| `tests/workloads/vllm/test_commands_serve.py` | from `tests/test_commands_serve.py` |
| `tests/workloads/vllm/test_commands_stop.py` | from `tests/test_commands_stop.py` + AT-8 merge tests |
| `tests/workloads/vllm/test_commands_rolling_restart.py` | from `tests/test_commands_rolling_restart.py` |
| `tests/workloads/vllm/test_commands_readonly.py` | from `tests/test_commands_readonly.py` |
| `tests/workloads/haproxy/__init__.py` | new |
| `tests/workloads/haproxy/test_manager.py` | from `tests/test_lb_manager_b.py` |
| `tests/workloads/haproxy/test_render.py` | from `tests/test_lb_render.py` |
| `tests/workloads/haproxy/test_runtime.py` | from `tests/test_lb_runtime_b.py` |
| `tests/workloads/haproxy/test_state.py` | from `tests/test_lb_state.py` |
| `tests/workloads/haproxy/test_routing.py` | from `tests/test_lb_routing.py` |
| `tests/workloads/haproxy/test_reconciler.py` | from `tests/test_lb_reconciler.py` |
| `tests/workloads/haproxy/test_reconciler_integration.py` | from `tests/test_lb_reconciler_integration.py` |
| `tests/workloads/haproxy/test_prune.py` | merged from `test_lb_prune_candidates.py` + `test_lb_prune_config.py` |
| `tests/workloads/haproxy/test_scaling.py` | from `test_lb_scaling_b.py` + absorbs `test_commands_lb_scaling.py` |
| `tests/workloads/haproxy/test_errors.py` | from `tests/test_lb_errors.py` |
| `tests/workloads/haproxy/test_commands.py` | merged: `test_commands_lb.py`, `test_commands_lb_info.py`, `test_commands_lb_list_health.py`, `test_commands_lb_prune.py` |
| `tests/workloads/haproxy/test_installer.py` | from `tests/test_lb_installer.py` |
| `tests/workloads/lmms/__init__.py` | new |
| `tests/workloads/lmms/test_commands.py` | new: lmms dispatch + run-loop/stop/status (AT-4 coverage) |
| `docs/COOKBOOK-workloads.md` | 3-step add-new-workload guide from spec Appendix A |

### Modified files (in place)

| Path | Change summary |
|---|---|
| `pyproject.toml` | `name = "tctl"`, script `tctl = "tctl.cli:main"`, `packages = ["src/tctl"]`, version `0.9.0` |
| `mypy.ini` | `files = src/tctl` |
| `.github/workflows/ci.yml` | `mypy --strict src/tctl` |
| `tests/conftest.py` | env-var unset list, sweeper pattern `"tctl" in cmd_str and "vllm"` |
| `tests/test_cli.py` | new workload dispatch tests + import updates |
| `tests/test_config_models.py` | new schema tests (haproxy/vllm shape, reject lb/profile/vctl-api) |
| `tests/test_config_settings.py` | new profile chain tests; env prefix rename |
| `tests/test_resolver.py` | import + YAML fixture updates |
| `tests/test_tmux.py` | import path only |
| `tests/test_smoke.py` | entry point `tctl`, version `0.9.0` |
| `tests/test_commands_config.py` | imports + new-shape YAML fixtures |
| `tests/test_commands_init_config.py` | imports + new-shape YAML fixtures |
| `tests/test_platform.py` | import path |
| `tests/test_duration.py` | import path |
| `tests/test_logging.py` | import path |
| `tests/test_commit_c.py` | import + fixture updates |
| `tests/test_commit_d.py` | import + fixture updates |
| `tests/test_commit_e.py` | import + fixture updates |
| `tests/test_f2_f3_f4.py` | import + fixture updates |
| `tests/test_f7_f8_f10.py` | import + fixture updates |
| `tests/test_coverage_supplement.py` | import + fixture updates |
| `tests/test_integration_endtoend.py` | import + fixture updates |
| `CLAUDE.md` | all `vctl` refs → `tctl`; source/state/session paths; gotchas section |
| `CHANGELOG.md` | prepend `## [0.9.0] - 2026-05-09` |
| `README.md` | rename, workload tree, `~/.tctl/` paths |
| `examples/cluster.yaml` | v0.9.0 shape |

### Deleted files

All of `src/vctl/` (Task 11). All old flat test files that were moved to `tests/workloads/` (Task 11).

---

## TASK ORDERING (12 tasks, single stream)

> Checkpoints after Tasks 3, 6, 9, 11 — natural pause points for review or hand-off.

---

### Task 1: pyproject + package skeleton + py.typed

**What we build:** Rename build metadata, create the `src/tctl/` skeleton (init files, `py.typed`), update `mypy.ini` and CI config. The new package co-exists with `src/vctl/` at this point. A smoke test confirms the new module is importable and carries the right version.

**Files:**
- Modify: `pyproject.toml`
- Modify: `mypy.ini`
- Modify: `.github/workflows/ci.yml`
- Create: `src/tctl/__init__.py`, `src/tctl/__main__.py`, `src/tctl/py.typed`
- Create: `src/tctl/config/__init__.py`, `src/tctl/commands/__init__.py`, `src/tctl/workloads/__init__.py`
- Create: `src/tctl/workloads/vllm/__init__.py` (stub — just `__all__: list[str] = []`)
- Create: `src/tctl/workloads/haproxy/__init__.py` (stub)
- Create: `src/tctl/workloads/lmms/__init__.py` (stub)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_smoke.py` (or a new section; import-guard against old name):

```python
# tests/test_smoke.py  — new section for Task 1
def test_tctl_package_importable() -> None:
    import importlib
    tctl = importlib.import_module("tctl")
    assert hasattr(tctl, "__version__")
    assert tctl.__version__ == "0.9.0"


def test_tctl_version_string_format() -> None:
    import tctl
    parts = tctl.__version__.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)
```

- [ ] **Step 2: Verify tests fail**

```bash
.venv/bin/pytest tests/test_smoke.py -x -q -k "tctl_package" 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'tctl'`

- [ ] **Step 3: Implement the skeleton**

`pyproject.toml` changes:
```toml
[project]
name = "tctl"
version = "0.9.0"
...
[project.scripts]
tctl = "tctl.cli:main"

[tool.hatch.build.targets.wheel]
packages = ["src/tctl"]
```

`src/tctl/__init__.py`:
```python
"""tctl — tmux controller for multi-workload GPU fleets."""
from __future__ import annotations

__version__ = "0.9.0"
```

`src/tctl/__main__.py`:
```python
"""Allow `python -m tctl` invocation."""
from __future__ import annotations

from tctl.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
```

`src/tctl/py.typed`: empty file (PEP 561 marker).

`src/tctl/config/__init__.py`, `src/tctl/commands/__init__.py`, `src/tctl/workloads/__init__.py`: empty files with `from __future__ import annotations` header.

Stub `__init__.py` for each workload sub-package (will be filled in later tasks):
```python
# src/tctl/workloads/vllm/__init__.py
from __future__ import annotations

__all__: list[str] = []
```

`mypy.ini` — change `files = src/vctl` to `files = src/tctl`.

`.github/workflows/ci.yml` — update the mypy step: `mypy --strict src/tctl`.

Reinstall the package:
```bash
uv pip install -e ".[dev,migrate]"
```

- [ ] **Step 4: Verify tests pass**

```bash
.venv/bin/pytest tests/test_smoke.py -x -q -k "tctl_package" 2>&1 | head -20
```

- [ ] **Step 5: Gates + commit**

```bash
.venv/bin/ruff check src/tctl && .venv/bin/ruff format --check src/tctl
.venv/bin/mypy --strict src/tctl
.venv/bin/pytest tests/test_smoke.py -x -q -k "tctl_package"
```

```bash
git add src/tctl/ pyproject.toml mypy.ini .github/workflows/ci.yml tests/test_smoke.py
git commit -m "refactor(scaffold): add src/tctl/ skeleton, bump pyproject to tctl v0.9.0"
```

---

### Task 2: Move foundational modules (tmux, platform, duration, resolver, logging)

**What we build:** Copy the five foundational modules into `src/tctl/`, rewriting all internal imports from `vctl.X` → `tctl.X`. Update the corresponding test files with import-path rewrites only. No behaviour change.

**Files:**
- Create: `src/tctl/tmux.py`, `src/tctl/platform.py`, `src/tctl/duration.py`, `src/tctl/resolver.py`, `src/tctl/logging.py`
- Modify: `tests/test_tmux.py`, `tests/test_platform.py`, `tests/test_duration.py`, `tests/test_logging.py`, `tests/test_resolver.py`

- [ ] **Step 1: Write failing tests**

In each test file, add an import-guard at the top that imports from `tctl.*`:

```python
# tests/test_tmux.py — add at top, alongside existing vctl imports
def test_tctl_tmux_importable() -> None:
    from tctl.tmux import TmuxSession, _validate_tmux_name, tmux_session_exists
    assert callable(TmuxSession)
    assert callable(_validate_tmux_name)
    assert callable(tmux_session_exists)
```

```python
# tests/test_platform.py — add:
def test_tctl_platform_importable() -> None:
    from tctl.platform import detect_self_ip
    assert callable(detect_self_ip)
```

```python
# tests/test_duration.py — add:
def test_tctl_duration_importable() -> None:
    from tctl.duration import parse_duration
    assert callable(parse_duration)
```

```python
# tests/test_logging.py — add:
def test_tctl_logging_importable() -> None:
    from tctl.logging import configure
    assert callable(configure)
```

```python
# tests/test_resolver.py — add:
def test_tctl_resolver_importable() -> None:
    from tctl.resolver import resolve
    assert callable(resolve)
```

- [ ] **Step 2: Verify tests fail**

```bash
.venv/bin/pytest tests/test_tmux.py tests/test_platform.py tests/test_duration.py \
    tests/test_logging.py tests/test_resolver.py -x -q -k "tctl_" 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'tctl.tmux'`

- [ ] **Step 3: Implement — copy and rewrite imports**

Copy each file and perform the single substitution `from vctl.` → `from tctl.` and `import vctl.` → `import tctl.`. The `tmux.py` file has no internal `vctl` references — verbatim copy. The others reference `vctl.config.*` or `vctl.tmux` which must be updated.

```bash
# Example for each file:
cp src/vctl/tmux.py src/tctl/tmux.py
sed -i 's/from vctl\./from tctl./g; s/import vctl\./import tctl./g' src/tctl/tmux.py

cp src/vctl/platform.py src/tctl/platform.py
sed -i 's/from vctl\./from tctl./g; s/import vctl\./import tctl./g' src/tctl/platform.py

cp src/vctl/duration.py src/tctl/duration.py
# duration.py has no vctl refs — verbatim copy

cp src/vctl/resolver.py src/tctl/resolver.py
sed -i 's/from vctl\./from tctl./g; s/import vctl\./import tctl./g' src/tctl/resolver.py

cp src/vctl/logging.py src/tctl/logging.py
sed -i 's/from vctl\./from tctl./g; s/import vctl\./import tctl./g' src/tctl/logging.py
```

Note: `resolver.py` imports from `tctl.config.*` — since config modules aren't created until Task 3, the module will import but `resolve()` will fail at call time until Task 3. The importability test passes; full resolver tests are gated by Task 3.

- [ ] **Step 4: Verify tests pass**

```bash
.venv/bin/pytest tests/test_tmux.py tests/test_platform.py tests/test_duration.py \
    tests/test_logging.py tests/test_resolver.py -x -q -k "tctl_" 2>&1 | head -20
```

- [ ] **Step 5: Gates + commit**

```bash
.venv/bin/ruff check src/tctl && .venv/bin/ruff format --check src/tctl
.venv/bin/mypy --strict src/tctl
.venv/bin/pytest tests/test_tmux.py tests/test_platform.py tests/test_duration.py \
    tests/test_logging.py -x -q
```

```bash
git add src/tctl/tmux.py src/tctl/platform.py src/tctl/duration.py \
        src/tctl/resolver.py src/tctl/logging.py \
        tests/test_tmux.py tests/test_platform.py tests/test_duration.py \
        tests/test_logging.py tests/test_resolver.py
git commit -m "refactor(tctl): copy foundational modules (tmux, platform, duration, resolver, logging)"
```

---

### Task 3: Move config layer + update cluster.yaml schema

**CHECKPOINT 1 — after this task the new config surface is locked.**

**What we build:** Copy the config package to `src/tctl/config/`, update Pydantic schemas for the v0.9.0 YAML shape (`apiVersion: tctl/v1`, `haproxy:` instead of `lb:`, `VllmCluster` replacing top-level `profile:`), update the env-var prefix and `_CLUSTER_TOPLEVEL` whitelist, and drop `MODEL_PROFILE` from the profile resolution chain. Existing `test_config_models.py` and `test_config_settings.py` gain new tests for the breaking schema changes (AT-6, AT-7).

**Files:**
- Create: `src/tctl/config/models.py`, `src/tctl/config/settings.py`, `src/tctl/config/yaml_source.py`
- Modify: `tests/test_config_models.py` (new tests + import updates)
- Modify: `tests/test_config_settings.py` (new tests + import updates)
- Modify: `tests/test_resolver.py` (YAML fixture updates)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_config_models.py`:

```python
# ---- Task 3: tctl/v1 schema tests ----

def _minimal_haproxy() -> dict:  # type: ignore[type-arg]
    return {
        "kind": "haproxy",
        "host": "127.0.0.1",
        "admin": {"bind_port": 9001},
        "stats": {"bind_port": 9000},
        "pools": [{"name": "default", "bind_port": 8000, "served_models": ["*"]}],
    }


def test_tctl_cluster_file_accepts_new_shape() -> None:
    from tctl.config.models import ClusterFile, ClusterSection, LbHaproxy, VllmCluster
    cf = ClusterFile(
        apiVersion="tctl/v1",
        cluster=ClusterSection(venv="/venv", state_dir="/tmp/state"),
        haproxy=LbHaproxy(**_minimal_haproxy()),
        vllm=VllmCluster(default_profile=None),
    )
    assert cf.apiVersion == "tctl/v1"
    assert cf.vllm.default_profile is None


def test_tctl_cluster_file_rejects_old_lb_key() -> None:
    from pydantic import ValidationError
    from tctl.config.models import ClusterFile
    import pytest
    with pytest.raises(ValidationError):
        ClusterFile.model_validate({
            "apiVersion": "tctl/v1",
            "cluster": {"venv": "/venv", "state_dir": "/tmp"},
            "lb": _minimal_haproxy(),  # old key — should be rejected
        })


def test_tctl_cluster_file_rejects_top_level_profile() -> None:
    from pydantic import ValidationError
    from tctl.config.models import ClusterFile
    import pytest
    with pytest.raises(ValidationError):
        ClusterFile.model_validate({
            "apiVersion": "tctl/v1",
            "cluster": {"venv": "/venv", "state_dir": "/tmp"},
            "haproxy": _minimal_haproxy(),
            "profile": "foo",   # old top-level field — must be rejected
        })


def test_tctl_cluster_file_rejects_old_api_version() -> None:
    from pydantic import ValidationError
    from tctl.config.models import ClusterFile
    import pytest
    with pytest.raises(ValidationError):
        ClusterFile.model_validate({
            "apiVersion": "vctl/v1",   # old literal
            "cluster": {"venv": "/venv", "state_dir": "/tmp"},
            "haproxy": _minimal_haproxy(),
        })


def test_tctl_vllm_cluster_default_profile_none() -> None:
    from tctl.config.models import VllmCluster
    vc = VllmCluster()
    assert vc.default_profile is None


def test_tctl_vllm_cluster_default_profile_roundtrip() -> None:
    from tctl.config.models import VllmCluster
    vc = VllmCluster(default_profile="foo")
    assert vc.default_profile == "foo"
```

Append to `tests/test_config_settings.py`:

```python
# ---- Task 3: TCTL_* env override + profile chain tests ----

def _minimal_cluster_file():  # type: ignore[return]
    from tctl.config.models import ClusterFile, ClusterSection, LbHaproxy, VllmCluster
    return ClusterFile(
        apiVersion="tctl/v1",
        cluster=ClusterSection(venv="/venv", state_dir="/tmp/state"),
        haproxy=LbHaproxy(
            kind="haproxy",
            host="127.0.0.1",
            admin={"bind_port": 9001},
            stats={"bind_port": 9000},
            pools=[{"name": "default", "bind_port": 8000, "served_models": ["*"]}],
        ),
        vllm=VllmCluster(default_profile=None),
    )


def test_tctl_profile_env_var_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TCTL_PROFILE", "myprofile")
    monkeypatch.delenv("MODEL_PROFILE", raising=False)
    from tctl.config.settings import resolve_profile_name
    assert resolve_profile_name(None, _minimal_cluster_file()) == "myprofile"


def test_tctl_model_profile_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_PROFILE", "oldprofile")
    monkeypatch.delenv("TCTL_PROFILE", raising=False)
    from tctl.config.settings import resolve_profile_name
    assert resolve_profile_name(None, _minimal_cluster_file()) is None


def test_tctl_cluster_default_profile_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TCTL_PROFILE", raising=False)
    monkeypatch.delenv("MODEL_PROFILE", raising=False)
    from tctl.config.models import VllmCluster
    from tctl.config.settings import resolve_profile_name
    cf = _minimal_cluster_file()
    object.__setattr__(cf.vllm, "default_profile", "cluster-default")
    assert resolve_profile_name(None, cf) == "cluster-default"


def test_tctl_env_prefix_applies() -> None:
    from tctl.config.settings import _apply_env_overrides
    env = {"TCTL_CLUSTER__VENV": "/new/venv"}
    result = _apply_env_overrides(
        {"cluster": {"venv": "/old"}}, environ=env,
        allowed_toplevel=frozenset({"cluster", "haproxy", "vllm"})
    )
    assert result["cluster"]["venv"] == "/new/venv"


def test_tctl_vctl_prefix_not_applied() -> None:
    from tctl.config.settings import _apply_env_overrides
    env = {"VCTL_CLUSTER__VENV": "/should-be-ignored"}
    result = _apply_env_overrides(
        {"cluster": {"venv": "/original"}}, environ=env,
        allowed_toplevel=frozenset({"cluster", "haproxy", "vllm"})
    )
    assert result["cluster"]["venv"] == "/original"
```

- [ ] **Step 2: Verify tests fail**

```bash
.venv/bin/pytest tests/test_config_models.py tests/test_config_settings.py \
    -x -q -k "tctl_" 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'tctl.config.models'`

- [ ] **Step 3: Implement config layer**

`src/tctl/config/yaml_source.py`: copy + rewrite `vctl.` → `tctl.` imports.

`src/tctl/config/models.py`: copy from `src/vctl/config/models.py` then apply these targeted changes:

1. Change `ApiVersion = Literal["vctl/v1"]` → `ApiVersion = Literal["tctl/v1"]`

2. The `Lb` type alias (currently `LbHaproxy` in the existing code) — ensure the class name is `LbHaproxy` throughout. (Check: the existing code may use `Lb = LbHaproxy`; normalise to just `LbHaproxy`.)

3. Replace the `ClusterFile` class:

```python
class VllmCluster(_Strict):
    """vllm-scoped cluster settings."""
    default_profile: str | None = None


class ClusterFile(BaseModel):
    """Top-level cluster.yaml document (v0.9.0 shape).

    Breaking from v0.8.0: apiVersion tctl/v1, haproxy: (was lb:),
    vllm.default_profile (was top-level profile:).
    """
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    apiVersion: ApiVersion  # noqa: N815
    cluster: ClusterSection
    haproxy: LbHaproxy
    vllm: VllmCluster = Field(default_factory=VllmCluster)
```

`src/tctl/config/settings.py`: copy from `src/vctl/config/settings.py` then:

1. `ENV_PREFIX = "TCTL_"` (was `"VCTL_"`)
2. `_CLUSTER_TOPLEVEL = frozenset({"apiversion", "cluster", "haproxy", "vllm"})` (drop `"lb"`, `"profile"`; note `"apiversion"` stays lowercase)
3. `resolve_profile_name` signature: `(cli_value: str | None, cluster: ClusterFile) -> str | None` — updated body:

```python
def resolve_profile_name(cli_value: str | None, cluster: ClusterFile) -> str | None:
    """Profile selection: CLI > $TCTL_PROFILE > cluster.vllm.default_profile.

    MODEL_PROFILE is intentionally NOT consulted (dropped in v0.9.0).
    """
    if cli_value:
        return cli_value
    env = os.environ.get("TCTL_PROFILE")
    if env:
        return env
    return cluster.vllm.default_profile
```

4. All internal imports: `from vctl.config.*` → `from tctl.config.*`

Update `tests/test_resolver.py`: replace `from vctl.config` → `from tctl.config`; update all inline YAML fixtures that use `apiVersion: vctl/v1` and `lb:` to use `apiVersion: tctl/v1` and `haproxy:`.

- [ ] **Step 4: Verify tests pass**

```bash
.venv/bin/pytest tests/test_config_models.py tests/test_config_settings.py \
    tests/test_resolver.py -x -q 2>&1 | head -30
```

- [ ] **Step 5: Gates + commit**

```bash
.venv/bin/ruff check src/tctl/config && .venv/bin/ruff format --check src/tctl/config
.venv/bin/mypy --strict src/tctl
.venv/bin/pytest tests/test_config_models.py tests/test_config_settings.py \
    tests/test_resolver.py -q
```

```bash
git add src/tctl/config/ tests/test_config_models.py tests/test_config_settings.py \
        tests/test_resolver.py
git commit -m "refactor(config): move config layer to tctl; tctl/v1 schema, TCTL_* env prefix, drop MODEL_PROFILE"
```

---

### Task 4: Move haproxy subsystem

**What we build:** Move the entire `src/vctl/lb/` subtree into `src/tctl/workloads/haproxy/`, rewriting imports and updating the three categories of hardcoded strings: tmux session names (`vctl-lb` → `tctl-haproxy`, `vctl-lb-watch` → `tctl-haproxy-watch`), state paths (`~/.vctl/lb/` → `~/.tctl/haproxy/`), and env literals (`VCTL_TEST_NO_SOCKET` → `TCTL_TEST_NO_SOCKET`). `scaling.py` (moved from `commands/lb_scaling.py`) preserves its `__all__` export list. Move corresponding test files to `tests/workloads/haproxy/`.

**Files:**
- Create: `src/tctl/workloads/haproxy/` (all 11 files)
- Create: `tests/workloads/__init__.py`, `tests/workloads/haproxy/__init__.py`
- Create: `tests/workloads/haproxy/test_manager.py`, `test_render.py`, `test_runtime.py`, `test_state.py`, `test_routing.py`, `test_reconciler.py`, `test_reconciler_integration.py`, `test_prune.py`, `test_errors.py`, `test_installer.py`
- Create: `tests/workloads/haproxy/test_scaling.py` (merged from `test_lb_scaling_b.py` + `test_commands_lb_scaling.py`)

- [ ] **Step 1: Write failing tests**

Add a canary import test in a new file:

```python
# tests/workloads/haproxy/test_haproxy_importable.py  (temp; deleted in Task 11)
from __future__ import annotations


def test_haproxy_manager_importable() -> None:
    from tctl.workloads.haproxy.manager import LbManager
    assert LbManager is not None


def test_haproxy_session_name() -> None:
    """AT-10 partial: LbManager session_name is tctl-haproxy."""
    # We can't instantiate without a full config, so test the constant.
    from tctl.workloads.haproxy import manager as m
    assert m._HAPROXY_SESSION_NAME == "tctl-haproxy"


def test_haproxy_state_path_prefix(tmp_path: pytest.TempPathFactory) -> None:
    from tctl.workloads.haproxy.state import BackendState
    # The default state root should contain "tctl" not "vctl"
    bs = BackendState.__new__(BackendState)
    import inspect
    src = inspect.getsource(BackendState)
    assert ".tctl" in src
    assert ".vctl" not in src
```

- [ ] **Step 2: Verify tests fail**

```bash
.venv/bin/pytest tests/workloads/haproxy/test_haproxy_importable.py -x -q 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'tctl.workloads.haproxy.manager'`

- [ ] **Step 3: Implement — copy and rewrite**

Copy files and apply systematic replacements:

```bash
mkdir -p src/tctl/workloads/haproxy

# Core lb modules
for f in manager render runtime state routing reconciler errors prune installer probe; do
    cp src/vctl/lb/${f}.py src/tctl/workloads/haproxy/${f}.py
done

# scaling comes from commands/
cp src/vctl/commands/lb_scaling.py src/tctl/workloads/haproxy/scaling.py

# Batch import rewrites
for f in src/tctl/workloads/haproxy/*.py; do
    sed -i 's/from vctl\.lb\./from tctl.workloads.haproxy./g' "$f"
    sed -i 's/from vctl\.commands\.lb_scaling/from tctl.workloads.haproxy.scaling/g' "$f"
    sed -i 's/from vctl\./from tctl./g' "$f"
    sed -i 's/import vctl\./import tctl./g' "$f"
done
```

Targeted string replacements in specific files:

**`src/tctl/workloads/haproxy/manager.py`** — update session name constants:
- Replace `"vctl-lb"` → `"tctl-haproxy"` (add `_HAPROXY_SESSION_NAME = "tctl-haproxy"` constant)
- Replace `"vctl-lb-watch"` → `"tctl-haproxy-watch"`
- Replace `Path.home() / ".vctl" / "lb"` → `Path.home() / ".tctl" / "haproxy"` (all occurrences)

**`src/tctl/workloads/haproxy/runtime.py`** — env literal:
- Replace `"VCTL_TEST_NO_SOCKET"` → `"TCTL_TEST_NO_SOCKET"`

**`src/tctl/workloads/haproxy/state.py`** — state path prefix:
- Replace all `".vctl"` / `"lb"` path components → `".tctl"` / `"haproxy"` in default path construction
- The `migrate_if_needed` method checks for the old flat layout; keep its logic but update path references to `~/.tctl/haproxy/`

**`src/tctl/workloads/haproxy/scaling.py`** — preserve `__all__` exactly:
```python
# Keep this __all__ intact — required for mypy --strict re-export + test monkeypatching
__all__ = ["_client", "_NoOpClient", "_name_for"]
```
Also update any `lb_admin_client` import reference: `from tctl.workloads.haproxy.runtime import lb_admin_client`.

Create `tests/workloads/__init__.py` and `tests/workloads/haproxy/__init__.py` (empty).

Move test files (copy + update imports + update monkeypatch targets + update YAML fixtures + update session name strings + update env var strings):

```bash
# Example: test_lb_manager_b.py → tests/workloads/haproxy/test_manager.py
cp tests/test_lb_manager_b.py tests/workloads/haproxy/test_manager.py
# Then in the new file:
# - from vctl.lb.manager → from tctl.workloads.haproxy.manager
# - monkeypatch.setattr("vctl.lb.manager.*") → ("tctl.workloads.haproxy.manager.*")
# - "vctl-lb" → "tctl-haproxy" in all string literals
# - YAML apiVersion: vctl/v1 → tctl/v1; lb: → haproxy:
```

Repeat for all 10 haproxy test files. For `test_prune.py`, merge content of both `test_lb_prune_candidates.py` and `test_lb_prune_config.py` into the single new file. For `test_scaling.py`, merge content of `test_lb_scaling_b.py` and `test_commands_lb_scaling.py`.

- [ ] **Step 4: Verify tests pass**

```bash
.venv/bin/pytest tests/workloads/haproxy/ -x -q 2>&1 | head -40
```

- [ ] **Step 5: Gates + commit**

```bash
.venv/bin/ruff check src/tctl/workloads/haproxy && \
    .venv/bin/ruff format --check src/tctl/workloads/haproxy
.venv/bin/mypy --strict src/tctl
.venv/bin/pytest tests/workloads/haproxy/ -q
```

```bash
git add src/tctl/workloads/haproxy/ tests/workloads/
git commit -m "refactor(haproxy): move lb/ → workloads/haproxy/; tctl-haproxy session name, ~/.tctl/haproxy paths, TCTL_TEST_NO_SOCKET"
```

---

### Task 5: Build haproxy workload command dispatcher

**What we build:** Create `src/tctl/workloads/haproxy/commands.py` containing all sub-verbs from the old `src/vctl/commands/lb.py` (start, stop, status, reload, logs, config, health, add, remove, drain, scaling, prune). Replace the stub `__init__.py` with the real workload dispatcher. Move `tests/test_commands_lb.py` and the three sibling files into the merged `tests/workloads/haproxy/test_commands.py`.

**Files:**
- Create: `src/tctl/workloads/haproxy/commands.py`
- Modify: `src/tctl/workloads/haproxy/__init__.py` (replace stub)
- Create: `tests/workloads/haproxy/test_commands.py` (merged from 4 old files)

- [ ] **Step 1: Write failing tests**

Create `tests/workloads/haproxy/test_commands.py` with AT-3 acceptance test and one functional test:

```python
"""tests/workloads/haproxy/test_commands.py — haproxy workload command tests."""
from __future__ import annotations

import pytest


# AT-3
def test_at3_haproxy_help_lists_all_verbs(capsys: pytest.CaptureFixture[str]) -> None:
    import tctl.cli as cli
    with pytest.raises(SystemExit) as exc:
        cli.main(["haproxy", "--help"])
    out = capsys.readouterr().out
    for verb in ("start", "stop", "status", "reload", "logs", "config",
                 "health", "add", "remove", "drain", "scaling", "prune"):
        assert verb in out, f"expected {verb!r} in haproxy --help"
    assert exc.value.code == 0


def test_haproxy_commands_module_importable() -> None:
    from tctl.workloads.haproxy import commands as _cmds
    assert hasattr(_cmds, "register_all")
    assert hasattr(_cmds, "_cmd_start")
    assert hasattr(_cmds, "_cmd_stop")
```

Note: AT-3 requires `tctl.cli` (Task 9) — mark with `pytest.mark.skip` initially if cli isn't built yet. Add a conditional skip guard:

```python
# At top of file:
pytestmark = pytest.mark.skipif(
    not __import__("importlib").util.find_spec("tctl.cli"),
    reason="tctl.cli not yet built (Task 9)"
)
```

- [ ] **Step 2: Verify tests fail**

```bash
.venv/bin/pytest tests/workloads/haproxy/test_commands.py -x -q 2>&1 | head -20
```

- [ ] **Step 3: Implement**

`src/tctl/workloads/haproxy/commands.py`: copy from `src/vctl/commands/lb.py`, rewrite all imports (`vctl.lb.*` → `tctl.workloads.haproxy.*`, `vctl.commands.lb_scaling` → `tctl.workloads.haproxy.scaling`). Rename all `_cmd_lb_*` functions to drop the `_lb_` infix (or keep as-is if the old names were already clean). Add a `register_all(sub: argparse._SubParsersAction) -> None` function that registers each sub-verb parser — this is the hook the `__init__.py` dispatcher calls.

`src/tctl/workloads/haproxy/__init__.py` (replace stub):

```python
"""tctl haproxy workload — HAProxy load-balancer management."""
from __future__ import annotations

import argparse

_VERBS: dict[str, str] = {
    "start":   "_cmd_start",
    "stop":    "_cmd_stop",
    "status":  "_cmd_status",
    "reload":  "_cmd_reload",
    "logs":    "_cmd_logs",
    "config":  "_cmd_config",
    "health":  "_cmd_health",
    "add":     "_cmd_add",
    "remove":  "_cmd_remove",
    "drain":   "_cmd_drain",
    "scaling": "_cmd_scaling",
    "prune":   "_cmd_prune",
}


def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    """Entry point called by cli._dispatch for `tctl haproxy <verb>`."""
    from tctl.workloads.haproxy import commands as _cmds  # lazy import

    p = argparse.ArgumentParser(prog="tctl haproxy")
    sub = p.add_subparsers(dest="verb", metavar="VERB")
    sub.required = True
    _cmds.register_all(sub)
    parsed = p.parse_args(argv_rest, namespace=ns)
    fn_name = _VERBS[parsed.verb]
    return getattr(_cmds, fn_name)(parsed, [])  # type: ignore[no-any-return]
```

Move and merge the four old test files into `tests/workloads/haproxy/test_commands.py`:
- Merge content from `tests/test_commands_lb.py`, `tests/test_commands_lb_info.py`, `tests/test_commands_lb_list_health.py`, `tests/test_commands_lb_prune.py`.
- Replace all `from vctl.commands.lb import` → `from tctl.workloads.haproxy.commands import`.
- Replace all `monkeypatch.setattr("vctl.commands.lb.*")` → `"tctl.workloads.haproxy.commands.*"`.
- Replace `monkeypatch.setattr("vctl.lb.*")` → `"tctl.workloads.haproxy.*"`.
- Update YAML fixtures: `apiVersion: vctl/v1` → `tctl/v1`, `lb:` → `haproxy:`.

- [ ] **Step 4: Verify tests pass**

```bash
.venv/bin/pytest tests/workloads/haproxy/test_commands.py -x -q 2>&1 | head -30
```

- [ ] **Step 5: Gates + commit**

```bash
.venv/bin/ruff check src/tctl/workloads/haproxy && \
    .venv/bin/ruff format --check src/tctl/workloads/haproxy
.venv/bin/mypy --strict src/tctl
.venv/bin/pytest tests/workloads/haproxy/ -q
```

```bash
git add src/tctl/workloads/haproxy/__init__.py \
        src/tctl/workloads/haproxy/commands.py \
        tests/workloads/haproxy/test_commands.py
git commit -m "feat(haproxy): build haproxy workload dispatcher (start/stop/status/reload/logs/config/health/add/remove/drain/scaling/prune)"
```

---

### Task 6: Move vllm subsystem (manager + state paths)

**CHECKPOINT 2 — after this task both workload subsystems exist; cli wiring is next.**

**What we build:** Move `src/vctl/vllm_manager.py` → `src/tctl/workloads/vllm/manager.py`. Update: tmux session name prefix (`vctl-vllm-` → `tctl-vllm-`), state paths (`~/.vctl/vllm/` → `~/.tctl/vllm/`), env literals (`VCTL_KILL_GRACE` → `TCTL_KILL_GRACE`). Add the `_rolling_restart_session_path(pool, state_dir)` method to `VllmManager` (replaces the old module-level `_SESSION_DIR` constant). Move and update both vllm manager test files.

**Files:**
- Create: `src/tctl/workloads/vllm/manager.py`
- Create: `tests/workloads/vllm/__init__.py`
- Create: `tests/workloads/vllm/test_manager.py` (from `tests/test_vllm_manager.py`)
- Create: `tests/workloads/vllm/test_manager_integration.py` (from `tests/test_vllm_manager_integration.py`)

- [ ] **Step 1: Write failing tests**

Create `tests/workloads/vllm/__init__.py` (empty).

Create `tests/workloads/vllm/test_manager.py` with the AT-9 and AT-10 acceptance tests plus canary import:

```python
"""tests/workloads/vllm/test_manager.py — VllmManager unit tests."""
from __future__ import annotations

from pathlib import Path
import pytest


def test_vllm_manager_importable() -> None:
    from tctl.workloads.vllm.manager import VllmManager
    assert VllmManager is not None


# AT-9
def test_at9_rolling_restart_session_path(tmp_path: Path) -> None:
    from tctl.workloads.vllm.manager import VllmManager
    state_dir = tmp_path / ".tctl"
    expected = state_dir / "vllm" / "rolling-restart" / "foo.json"
    manager = VllmManager.__new__(VllmManager)
    actual = manager._rolling_restart_session_path("foo", state_dir=state_dir)
    assert actual == expected
    assert actual.parent.exists(), "parent directory should be created"


def test_at9_rolling_restart_default_state_dir() -> None:
    from tctl.workloads.vllm.manager import VllmManager
    import inspect
    src = inspect.getsource(VllmManager._rolling_restart_session_path)
    assert ".tctl" in src
    assert "rolling-restart" in src


# AT-10 partial
def test_at10_vllm_session_name_prefix() -> None:
    import inspect
    from tctl.workloads.vllm import manager as m
    src = inspect.getsource(m)
    assert "tctl-vllm-" in src
    assert "vctl-vllm-" not in src
```

- [ ] **Step 2: Verify tests fail**

```bash
.venv/bin/pytest tests/workloads/vllm/test_manager.py -x -q 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'tctl.workloads.vllm.manager'`

- [ ] **Step 3: Implement**

```bash
cp src/vctl/vllm_manager.py src/tctl/workloads/vllm/manager.py
sed -i 's/from vctl\./from tctl./g; s/import vctl\./import tctl./g' \
    src/tctl/workloads/vllm/manager.py
```

Targeted edits in `src/tctl/workloads/vllm/manager.py`:

1. Session name: replace `f"vctl-vllm-{profile}"` → `f"tctl-vllm-{profile}"`.
2. State paths: replace `Path.home() / ".vctl" / "vllm"` → `Path.home() / ".tctl" / "vllm"` (all occurrences).
3. Env literal: `"VCTL_KILL_GRACE"` → `"TCTL_KILL_GRACE"`.
4. Add `_rolling_restart_session_path` method:

```python
def _rolling_restart_session_path(
    self, pool: str, state_dir: Path | None = None
) -> Path:
    """Return <state_dir>/vllm/rolling-restart/<pool>.json.

    Default state_dir: Path.home() / ".tctl"
    """
    base = (state_dir if state_dir is not None else Path.home() / ".tctl") / "vllm" / "rolling-restart"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{pool}.json"
```

5. Remove any reference to `_SESSION_DIR` module-level constant (it no longer lives here).

Move test files:

```bash
cp tests/test_vllm_manager.py tests/workloads/vllm/test_manager.py
# Merge with the new tests written in Step 1 (the existing test body)
```

```bash
cp tests/test_vllm_manager_integration.py tests/workloads/vllm/test_manager_integration.py
```

In both moved files:
- `from vctl.vllm_manager` → `from tctl.workloads.vllm.manager`
- `monkeypatch.setattr("vctl.vllm_manager.*")` → `"tctl.workloads.vllm.manager.*"`
- `"vctl-vllm-"` → `"tctl-vllm-"` in string literals
- `"VCTL_KILL_GRACE"` → `"TCTL_KILL_GRACE"`
- YAML fixtures: `apiVersion: vctl/v1` → `tctl/v1`, `lb:` → `haproxy:`

- [ ] **Step 4: Verify tests pass**

```bash
.venv/bin/pytest tests/workloads/vllm/test_manager.py -x -q 2>&1 | head -30
```

- [ ] **Step 5: Gates + commit**

```bash
.venv/bin/ruff check src/tctl/workloads/vllm && \
    .venv/bin/ruff format --check src/tctl/workloads/vllm
.venv/bin/mypy --strict src/tctl
.venv/bin/pytest tests/workloads/vllm/ -q
```

```bash
git add src/tctl/workloads/vllm/manager.py \
        tests/workloads/vllm/
git commit -m "refactor(vllm): move VllmManager to workloads/vllm/; tctl-vllm- session prefix, ~/.tctl/vllm paths, TCTL_KILL_GRACE, _rolling_restart_session_path method"
```

---

### Task 7: Build vllm workload command dispatcher + merged stop

**What we build:** Create `src/tctl/workloads/vllm/commands.py` by merging the seven old command modules (info, profiles, args_cmd, preflight, serve, stop, rolling_restart). Implement the **merged `tctl vllm stop`** (drain + tmux-kill + process-sweep, per spec §4.7). Move `templates.py`. Replace the vllm workload `__init__.py` stub with the real dispatcher. Move and update the four vllm command test files, extending `test_commands_stop.py` with AT-8 tests.

**Files:**
- Create: `src/tctl/workloads/vllm/commands.py`
- Create: `src/tctl/workloads/vllm/templates.py`
- Modify: `src/tctl/workloads/vllm/__init__.py` (replace stub)
- Create: `tests/workloads/vllm/test_commands_serve.py`
- Create: `tests/workloads/vllm/test_commands_stop.py` (+ AT-8)
- Create: `tests/workloads/vllm/test_commands_rolling_restart.py`
- Create: `tests/workloads/vllm/test_commands_readonly.py`

- [ ] **Step 1: Write failing tests**

New `tests/workloads/vllm/test_commands_stop.py` (AT-8):

```python
"""tests/workloads/vllm/test_commands_stop.py — merged stop command tests."""
from __future__ import annotations

from pathlib import Path
import pytest


# AT-8
def test_at8_stop_calls_drain_kill_sweep(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    drain_calls: list[str] = []
    kill_calls: list[str] = []
    sweep_calls: list[bool] = []

    monkeypatch.setattr(
        "tctl.workloads.vllm.commands._drain_from_lb",
        lambda profile, cfg: drain_calls.append(profile),
    )
    monkeypatch.setattr(
        "tctl.workloads.vllm.commands._kill_tmux_session",
        lambda name: kill_calls.append(name),
    )
    monkeypatch.setattr(
        "tctl.workloads.vllm.commands._sweep_local_vllm",
        lambda: sweep_calls.append(True),
    )

    # Build minimal cluster.yaml in tmp_path
    cfg_file = tmp_path / "cluster.yaml"
    cfg_file.write_text(
        "apiVersion: tctl/v1\n"
        "cluster:\n  venv: /venv\n  state_dir: /tmp/state\n"
        "haproxy:\n  kind: haproxy\n  host: 127.0.0.1\n"
        "  admin: {bind_port: 9001}\n  stats: {bind_port: 9000}\n"
        "  pools: [{name: default, bind_port: 8000, served_models: ['*']}]\n"
        "vllm:\n  default_profile: null\n"
    )

    import tctl.cli as cli
    rc = cli.main(["vllm", "stop", "--profile", "myprofile", "--config", str(cfg_file)])
    assert rc == 0
    assert drain_calls == ["myprofile"]
    assert "tctl-vllm-myprofile" in kill_calls
    assert sweep_calls, "process sweep must run"


def test_at8_stop_sweep_runs_even_if_drain_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sweep_calls: list[bool] = []

    def _drain_raises(profile: str, cfg: object) -> None:
        raise RuntimeError("LB unreachable")

    monkeypatch.setattr("tctl.workloads.vllm.commands._drain_from_lb", _drain_raises)
    monkeypatch.setattr(
        "tctl.workloads.vllm.commands._kill_tmux_session", lambda name: None
    )
    monkeypatch.setattr(
        "tctl.workloads.vllm.commands._sweep_local_vllm", lambda: sweep_calls.append(True)
    )

    cfg_file = tmp_path / "cluster.yaml"
    cfg_file.write_text(
        "apiVersion: tctl/v1\ncluster:\n  venv: /venv\n  state_dir: /tmp/state\n"
        "haproxy:\n  kind: haproxy\n  host: 127.0.0.1\n"
        "  admin: {bind_port: 9001}\n  stats: {bind_port: 9000}\n"
        "  pools: [{name: default, bind_port: 8000, served_models: ['*']}]\n"
        "vllm:\n  default_profile: null\n"
    )

    import tctl.cli as cli
    rc = cli.main(["vllm", "stop", "--profile", "myprofile", "--config", str(cfg_file)])
    # Non-fatal drain failure — stop still cleans up
    assert rc == 0
    assert sweep_calls, "sweep must run even when drain fails"
```

- [ ] **Step 2: Verify tests fail**

```bash
.venv/bin/pytest tests/workloads/vllm/test_commands_stop.py -x -q 2>&1 | head -20
```

Expected: import error on `tctl.workloads.vllm.commands` or `tctl.cli`.

- [ ] **Step 3: Implement**

`src/tctl/workloads/vllm/templates.py`: copy from `src/vctl/commands/templates.py`; update the scaffold YAML template to emit the v0.9.0 shape:
- `apiVersion: tctl/v1`
- `haproxy:` top-level key (was `lb:`)
- `vllm:\n  default_profile: null` (replaces top-level `profile: null`)
- Replace any `"vctl"` strings in template text with `"tctl"`.
- Update internal imports: `from vctl.*` → `from tctl.*`.

`src/tctl/workloads/vllm/commands.py`: merge content from the seven old modules into a single file. Each old module contributes one or more `_cmd_*` functions plus a `register_*` helper. The merged file exposes:
- `register_all(sub: argparse._SubParsersAction) -> None` — calls each `_register_<verb>(sub)`.
- `_cmd_info`, `_cmd_profiles`, `_cmd_args`, `_cmd_preflight`, `_cmd_serve`, `_cmd_stop`, `_cmd_rolling_restart`.

**Merged `_cmd_stop` implementation** (all three steps in sequence, non-fatal drain):

```python
def _cmd_stop(ns: argparse.Namespace, _rest: list[str]) -> int:
    """tctl vllm stop — drain from LB, kill tmux session, sweep local process."""
    import logging
    _log = logging.getLogger(__name__)
    from tctl.config.settings import load_cluster_file, resolve_profile_name
    cfg = load_cluster_file(ns.config)
    profile = resolve_profile_name(ns.profile, cfg)
    if not profile:
        print("error: no profile selected (use --profile or $TCTL_PROFILE)", file=sys.stderr)
        return 2
    session_name = f"tctl-vllm-{profile}"

    # Step 1: drain from LB (non-fatal)
    try:
        _drain_from_lb(profile, cfg)
    except Exception as exc:
        _log.warning("drain failed (non-fatal): %s", exc)

    # Step 2: kill tmux session
    _kill_tmux_session(session_name)

    # Step 3: process sweep fallback
    _sweep_local_vllm()
    return 0


def _drain_from_lb(profile: str, cfg: object) -> None:
    """Drain this host's vllm endpoint(s) from all LB pools."""
    # Delegates to haproxy scaling drain logic (same as old vctl stop drain step)
    from tctl.workloads.haproxy import scaling as _sc
    _sc.drain_local_endpoints(profile, cfg)  # type: ignore[attr-defined]


def _kill_tmux_session(name: str) -> None:
    """Send C-c then kill the named tmux session."""
    from tctl.tmux import TmuxSession
    sess = TmuxSession(name)
    if sess.exists():
        sess.kill(tree=True, grace_s=float(os.environ.get("TCTL_KILL_GRACE", "5")))


def _sweep_local_vllm() -> None:
    """SIGTERM any remaining local vllm process trees."""
    # Delegates to the same sweep logic from old vctl/commands/stop.py
    from tctl.workloads.vllm import _process_sweep as _ps
    _ps.sweep()
```

Note: The exact function names for `_drain_from_lb` and `_sweep_local_vllm` are what the AT-8 tests monkeypatch — these names are locked. The internal delegation (to `scaling.drain_local_endpoints` and a `_process_sweep` helper) is implementation detail.

Update all env literals in `commands.py`: `VCTL_READY_TIMEOUT` → `TCTL_READY_TIMEOUT`, `VCTL_KILL_GRACE` → `TCTL_KILL_GRACE`. Keep `VLLM_ENGINE_READY_TIMEOUT_S` unchanged (vllm-native).

Update rolling_restart: the `_SESSION_DIR` constant is gone; instead call:
```python
session_path = VllmManager.__new__(VllmManager)._rolling_restart_session_path(pool, state_dir)
```

`src/tctl/workloads/vllm/__init__.py` (replace stub):

```python
"""tctl vllm workload — vLLM inference worker management."""
from __future__ import annotations

import argparse

_VERBS: dict[str, str] = {
    "info":             "_cmd_info",
    "profiles":         "_cmd_profiles",
    "args":             "_cmd_args",
    "preflight":        "_cmd_preflight",
    "serve":            "_cmd_serve",
    "stop":             "_cmd_stop",
    "rolling-restart":  "_cmd_rolling_restart",
}


def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    """Entry point called by cli._dispatch for `tctl vllm <verb>`."""
    from tctl.workloads.vllm import commands as _cmds  # lazy import

    p = argparse.ArgumentParser(prog="tctl vllm")
    sub = p.add_subparsers(dest="verb", metavar="VERB")
    sub.required = True
    _cmds.register_all(sub)
    parsed = p.parse_args(argv_rest, namespace=ns)
    fn_name = _VERBS[parsed.verb]
    return getattr(_cmds, fn_name)(parsed, [])  # type: ignore[no-any-return]
```

Move and update the four vllm command test files (same pattern as Task 4/5):
- `test_commands_serve.py`: `from vctl.commands.serve` → `from tctl.workloads.vllm.commands`; monkeypatch targets; session names; env vars.
- `test_commands_stop.py`: merge with the AT-8 tests written in Step 1.
- `test_commands_rolling_restart.py`: update `_SESSION_DIR` references to use `manager._rolling_restart_session_path`; update import + monkeypatch paths.
- `test_commands_readonly.py`: update imports.

- [ ] **Step 4: Verify tests pass**

```bash
.venv/bin/pytest tests/workloads/vllm/ -x -q 2>&1 | head -40
```

- [ ] **Step 5: Gates + commit**

```bash
.venv/bin/ruff check src/tctl/workloads/vllm && \
    .venv/bin/ruff format --check src/tctl/workloads/vllm
.venv/bin/mypy --strict src/tctl
.venv/bin/pytest tests/workloads/vllm/ -q
```

```bash
git add src/tctl/workloads/vllm/ tests/workloads/vllm/
git commit -m "feat(vllm): build vllm workload dispatcher; merge stop (drain+kill+sweep); update env literals to TCTL_*"
```

---

### Task 8: Move lmms workload

**What we build:** Move `src/vctl/commands/lmmseval.py` → `src/tctl/workloads/lmms/commands.py`. Update the tmux session name (`vctl-lmmseval` → `tctl-lmms`). Replace the lmms stub `__init__.py` with the real dispatcher. Create `tests/workloads/lmms/test_commands.py` with AT-4 coverage.

**Files:**
- Create: `src/tctl/workloads/lmms/commands.py`
- Modify: `src/tctl/workloads/lmms/__init__.py` (replace stub)
- Create: `tests/workloads/lmms/__init__.py`
- Create: `tests/workloads/lmms/test_commands.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/workloads/lmms/test_commands.py
from __future__ import annotations
import pytest


def test_lmms_commands_importable() -> None:
    from tctl.workloads.lmms.commands import register_all
    assert callable(register_all)


def test_lmms_session_name_uses_tctl_prefix() -> None:
    import inspect
    from tctl.workloads.lmms import commands as _cmds
    src = inspect.getsource(_cmds)
    assert "tctl-lmms" in src
    assert "vctl-lmmseval" not in src


# AT-4 (partial — full AT-4 requires cli.py from Task 9)
def test_at4_lmms_run_loop_dispatched(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[int] = []
    from tctl.workloads.lmms import commands as _cmds
    monkeypatch.setattr(_cmds, "_cmd_run_loop", lambda ns, rest: called.append(1) or 0)

    from tctl.workloads.lmms import run
    import argparse
    ns = argparse.Namespace()
    rc = run(ns, ["run-loop"])
    assert rc == 0
    assert called, "lmms run-loop was not dispatched"
```

- [ ] **Step 2: Verify tests fail**

```bash
.venv/bin/pytest tests/workloads/lmms/ -x -q 2>&1 | head -20
```

- [ ] **Step 3: Implement**

```bash
mkdir -p src/tctl/workloads/lmms
cp src/vctl/commands/lmmseval.py src/tctl/workloads/lmms/commands.py
sed -i 's/from vctl\./from tctl./g; s/import vctl\./import tctl./g' \
    src/tctl/workloads/lmms/commands.py
```

Targeted edits:
- Replace `"vctl-lmmseval"` → `"tctl-lmms"` (session name string literal).
- Rename `run(ns, argv_rest)` to `_cmd_run_loop` / `_cmd_stop` / `_cmd_status` pattern (matching other workloads).
- Add `register_all(sub)` function that registers `run-loop`, `stop`, `status` sub-parsers.

`src/tctl/workloads/lmms/__init__.py`:

```python
"""tctl lmms workload — lmms-eval job management (hidden from top-level --help)."""
from __future__ import annotations

import argparse

_VERBS: dict[str, str] = {
    "run-loop": "_cmd_run_loop",
    "stop":     "_cmd_stop",
    "status":   "_cmd_status",
}


def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    """Entry point called by cli._dispatch for `tctl lmms <verb>`."""
    from tctl.workloads.lmms import commands as _cmds  # lazy import

    p = argparse.ArgumentParser(prog="tctl lmms")
    sub = p.add_subparsers(dest="verb", metavar="VERB")
    sub.required = True
    _cmds.register_all(sub)
    parsed = p.parse_args(argv_rest, namespace=ns)
    fn_name = _VERBS[parsed.verb]
    return getattr(_cmds, fn_name)(parsed, [])  # type: ignore[no-any-return]
```

Create `tests/workloads/lmms/__init__.py` (empty).

- [ ] **Step 4: Verify tests pass**

```bash
.venv/bin/pytest tests/workloads/lmms/ -x -q 2>&1 | head -30
```

- [ ] **Step 5: Gates + commit**

```bash
.venv/bin/ruff check src/tctl/workloads/lmms && \
    .venv/bin/ruff format --check src/tctl/workloads/lmms
.venv/bin/mypy --strict src/tctl
.venv/bin/pytest tests/workloads/lmms/ -q
```

```bash
git add src/tctl/workloads/lmms/ tests/workloads/lmms/
git commit -m "refactor(lmms): move lmmseval → workloads/lmms/; tctl-lmms session name"
```

---

### Task 9: Build cli.py with workload + platform dispatch + positional hoister

**CHECKPOINT 3 — after this task `python -m tctl` works end-to-end.**

**What we build:** The new `src/tctl/cli.py` with two-level dispatch (`_WORKLOADS` dict + `_PLATFORM_COMMANDS`), the updated `_hoist_positional_profile` (2-token form), and the hidden-workload filter. Update `tests/test_cli.py` with AT-1, AT-2, AT-3, AT-4, AT-11, AT-12 acceptance tests.

**Files:**
- Create: `src/tctl/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_cli.py` (or create it fresh):

```python
"""tests/test_cli.py — tctl CLI dispatch tests."""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# AT-1: --help lists workloads; lmms is absent
# ---------------------------------------------------------------------------

def test_at1_help_lists_workloads_not_lmms(capsys: pytest.CaptureFixture[str]) -> None:
    import tctl.cli as cli
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert "vllm" in out
    assert "haproxy" in out
    assert "config" in out
    assert "init-config" in out
    assert "lmms" not in out
    assert exc.value.code == 0


# ---------------------------------------------------------------------------
# AT-2: tctl vllm --help lists all vllm sub-verbs
# ---------------------------------------------------------------------------

def test_at2_vllm_help_lists_all_verbs(capsys: pytest.CaptureFixture[str]) -> None:
    import tctl.cli as cli
    with pytest.raises(SystemExit) as exc:
        cli.main(["vllm", "--help"])
    out = capsys.readouterr().out
    for verb in ("info", "profiles", "args", "preflight", "serve", "stop", "rolling-restart"):
        assert verb in out, f"expected {verb!r} in vllm --help"
    assert exc.value.code == 0


# ---------------------------------------------------------------------------
# AT-4: lmms reachable when named directly; absent from top-level help
# ---------------------------------------------------------------------------

def test_at4_lmms_reachable_when_named_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[int] = []
    import tctl.workloads.lmms.commands as _cmds
    monkeypatch.setattr(_cmds, "_cmd_run_loop", lambda ns, rest: called.append(1) or 0)
    import tctl.cli as cli
    rc = cli.main(["lmms", "run-loop"])
    assert rc == 0
    assert called, "lmms run-loop was not dispatched"


# ---------------------------------------------------------------------------
# AT-11: MODEL_PROFILE is ignored by cli
# ---------------------------------------------------------------------------

def test_at11_model_profile_not_consulted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    monkeypatch.setenv("MODEL_PROFILE", "legacymodel")
    monkeypatch.delenv("TCTL_PROFILE", raising=False)
    from pathlib import Path
    cfg_file = Path(str(tmp_path)) / "cluster.yaml"
    cfg_file.write_text(
        "apiVersion: tctl/v1\ncluster:\n  venv: /venv\n  state_dir: /tmp/state\n"
        "haproxy:\n  kind: haproxy\n  host: 127.0.0.1\n"
        "  admin: {bind_port: 9001}\n  stats: {bind_port: 9000}\n"
        "  pools: [{name: default, bind_port: 8000, served_models: ['*']}]\n"
        "vllm:\n  default_profile: null\n"
    )
    import tctl.cli as cli
    rc = cli.main(["vllm", "info", "--config", str(cfg_file)])
    # Should exit 2 (no profile), NOT use "legacymodel"
    assert rc == 2


# ---------------------------------------------------------------------------
# AT-12: New workload requires only 3 steps; no core file changes
# ---------------------------------------------------------------------------

def test_at12_new_workload_requires_only_3_steps(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pathlib import Path
    import sys

    pkg = Path(str(tmp_path)) / "tctl" / "workloads" / "demo"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        "def run(ns, argv_rest):\n    return 0\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    import tctl.cli as cli
    original = dict(cli._WORKLOADS)
    try:
        cli._WORKLOADS["demo"] = ("tctl.workloads.demo", False)
        with pytest.raises(SystemExit) as exc:
            cli.main(["demo", "--help"])
    finally:
        cli._WORKLOADS.clear()
        cli._WORKLOADS.update(original)
        # Remove injected module from sys.modules
        for key in list(sys.modules.keys()):
            if "demo" in key:
                del sys.modules[key]
    assert exc.value.code == 0


# ---------------------------------------------------------------------------
# Hoister: 2-token positional profile rewrite
# ---------------------------------------------------------------------------

def test_hoist_vllm_serve_profile_yaml() -> None:
    from tctl.cli import _hoist_positional_profile
    result = _hoist_positional_profile(["vllm", "serve", "models/qwen3_5-9b.yaml"])
    assert result == ["vllm", "serve", "--profile", "qwen3_5-9b"]


def test_hoist_only_vllm_workload() -> None:
    from tctl.cli import _hoist_positional_profile
    # haproxy has no profile-aware verbs; passthrough unchanged
    result = _hoist_positional_profile(["haproxy", "start", "models/foo.yaml"])
    assert result == ["haproxy", "start", "models/foo.yaml"]


def test_unknown_workload_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    import tctl.cli as cli
    rc = cli.main(["notaworkload", "something"])
    assert rc == 2
```

- [ ] **Step 2: Verify tests fail**

```bash
.venv/bin/pytest tests/test_cli.py -x -q -k "at1_help or at2_vllm or at4_lmms or hoist" 2>&1 | head -30
```

- [ ] **Step 3: Implement `src/tctl/cli.py`**

```python
"""tctl CLI — two-level dispatch: workloads + platform commands."""
from __future__ import annotations

import argparse
import importlib
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

from tctl import __version__

_LOG = logging.getLogger(__name__)

# Workload sub-trees: name → (module_path, hidden_from_help)
_WORKLOADS: dict[str, tuple[str, bool]] = {
    "vllm":    ("tctl.workloads.vllm",    False),
    "haproxy": ("tctl.workloads.haproxy", False),
    "lmms":    ("tctl.workloads.lmms",    True),
}

# Platform commands (no workload namespace)
_PLATFORM_COMMANDS: dict[str, str] = {
    "config":      "tctl.commands.config_cmd",
    "init-config": "tctl.commands.init_config",
}

# Profile-aware verbs per workload: positional models/*.yaml → --profile
_PROFILE_AWARE: dict[str, set[str]] = {
    "vllm": {"info", "args", "preflight", "serve", "stop"},
    # haproxy / lmms have no profile-aware verbs
}

_TCTL_HOME = Path.home() / ".tctl"
_CONFIG_DEFAULT_HOME = _TCTL_HOME / "cluster.yaml"
_CONFIG_SENTINEL = "<auto>"


def _resolve_config_path(cli_arg: str | None) -> str:
    if cli_arg:
        return cli_arg
    env = os.environ.get("CLUSTER_CONFIG")
    if env:
        return env
    return str(_CONFIG_DEFAULT_HOME)


def _hoist_positional_profile(argv: list[str]) -> list[str]:
    """Rewrite `tctl <workload> <verb> models/<x>.yaml` → `tctl <workload> <verb> --profile <x>`."""
    out = list(argv)
    # Skip leading global flags
    i = 0
    while i < len(out) and out[i].startswith("-"):
        if out[i] in ("--log-level", "--log-format", "--config", "--profile"):
            i += 2
            continue
        i += 1
    # Consume workload token
    if i >= len(out) or out[i] not in _WORKLOADS:
        return out
    workload = out[i]
    workload_idx = i
    # Consume verb token
    j = workload_idx + 1
    while j < len(out) and out[j].startswith("-"):
        j += 1
    if j >= len(out):
        return out
    verb = out[j]
    if verb not in _PROFILE_AWARE.get(workload, set()):
        return out
    # Look for models/*.yaml positional after verb
    k = j + 1
    while k < len(out) and out[k].startswith("-"):
        k += 1
    if k < len(out):
        token = out[k]
        if token.startswith("models/") and token.endswith(".yaml"):
            stem = token[len("models/"):-len(".yaml")]
            return out[:j + 1] + ["--profile", stem] + out[j + 1:k] + out[k + 1:]
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tctl", description="Tmux controller for multi-workload GPU fleets.")
    p.add_argument("-V", "--version", action="version", version=f"tctl {__version__}")
    p.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    p.add_argument("--log-format", default="pretty", choices=["pretty", "json"])
    p.add_argument(
        "--config", default=_CONFIG_SENTINEL,
        help="path to cluster.yaml (default: $CLUSTER_CONFIG → ~/.tctl/cluster.yaml)",
    )
    p.add_argument("--profile", default=None, help="profile name (overrides $TCTL_PROFILE and cluster.vllm.default_profile)")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")
    # Register visible workloads
    for name, (_, hidden) in _WORKLOADS.items():
        if not hidden:
            sp = sub.add_parser(name, help=f"see `tctl {name} --help`", add_help=False)
            sp.set_defaults(_subname=name)
    # Register platform commands
    for name in _PLATFORM_COMMANDS:
        sp = sub.add_parser(name, help=f"see `tctl {name} --help`", add_help=False)
        sp.set_defaults(_subname=name)
    return p


def _dispatch(name: str, argv_rest: list[str], ns: argparse.Namespace) -> int:
    if name in _WORKLOADS:
        mod_path, _ = _WORKLOADS[name]
    elif name in _PLATFORM_COMMANDS:
        mod_path = _PLATFORM_COMMANDS[name]
    else:
        print(f"tctl: unknown command {name!r}", file=sys.stderr)
        return 2
    mod = importlib.import_module(mod_path)
    handler: Callable[[argparse.Namespace, list[str]], int] = mod.run
    return handler(ns, argv_rest)


def main(argv: list[str] | None = None) -> int:
    raw = list(argv if argv is not None else sys.argv[1:])
    raw = _hoist_positional_profile(raw)

    # Hidden workloads bypass global argparse
    for i, tok in enumerate(raw):
        if tok.startswith("-"):
            continue
        if tok in _WORKLOADS and _WORKLOADS[tok][1]:  # hidden=True
            from tctl.logging import configure as _configure_logging
            _configure_logging()
            ns = argparse.Namespace(command=tok, config=str(_CONFIG_DEFAULT_HOME), profile=None)
            return _dispatch(tok, raw[i + 1:], ns)
        break

    parser = build_parser()
    ns, rest = parser.parse_known_args(raw)

    from tctl.logging import configure as _configure_logging
    _configure_logging(level=ns.log_level, fmt=ns.log_format)

    ns.config = _resolve_config_path(None if ns.config == _CONFIG_SENTINEL else ns.config)

    try:
        return _dispatch(ns.command, rest, ns)
    except FileNotFoundError as exc:
        if _missing_path_is_config(exc):
            print(f"error: {exc}", file=sys.stderr)
            return 2
        raise
    except KeyboardInterrupt:
        return 130


def _missing_path_is_config(exc: FileNotFoundError) -> bool:
    msg = str(exc)
    return "cluster.yaml" in msg or ".tctl" in msg
```

- [ ] **Step 4: Verify tests pass**

```bash
.venv/bin/pytest tests/test_cli.py -x -q 2>&1 | head -40
```

- [ ] **Step 5: Gates + commit**

```bash
.venv/bin/ruff check src/tctl/cli.py && .venv/bin/ruff format --check src/tctl/cli.py
.venv/bin/mypy --strict src/tctl
.venv/bin/pytest tests/test_cli.py -q
```

```bash
git add src/tctl/cli.py tests/test_cli.py
git commit -m "feat(cli): build tctl two-level workload dispatcher; hidden lmms; positional profile hoister"
```

---

### Task 10: Move platform commands + update conftest.py + mechanical test updates

**What we build:** Copy `config_cmd.py` and `init_config.py` to `src/tctl/commands/`, updating the `init_config` scaffold to emit v0.9.0 YAML. Update `tests/conftest.py` with the exact replacements from spec §5.1. Apply mechanical `vctl.*` → `tctl.*` import and monkeypatch-target rewrites to all remaining test files that haven't been moved yet.

**Files:**
- Create: `src/tctl/commands/config_cmd.py`
- Create: `src/tctl/commands/init_config.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_smoke.py`
- Modify: `tests/test_commands_config.py`, `tests/test_commands_init_config.py`
- Modify: `tests/test_commit_c.py`, `tests/test_commit_d.py`, `tests/test_commit_e.py`
- Modify: `tests/test_f2_f3_f4.py`, `tests/test_f7_f8_f10.py`, `tests/test_coverage_supplement.py`
- Modify: `tests/test_integration_endtoend.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_commands_init_config.py`:

```python
# AT-5 acceptance test
def test_at5_init_config_new_shape(tmp_path: Path) -> None:
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

Add to `tests/test_commands_config.py`:

```python
# AT-6 acceptance tests
_NEW_YAML = """
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

_OLD_YAML = _NEW_YAML.replace("apiVersion: tctl/v1", "apiVersion: vctl/v1").replace("haproxy:", "lb:")


def test_at6_validate_new_shape_ok(tmp_path: Path) -> None:
    f = tmp_path / "cluster.yaml"
    f.write_text(_NEW_YAML)
    import tctl.cli as cli
    assert cli.main(["config", "validate", "--config", str(f)]) == 0


def test_at6_validate_old_shape_rejects(tmp_path: Path) -> None:
    f = tmp_path / "cluster.yaml"
    f.write_text(_OLD_YAML)
    import tctl.cli as cli
    assert cli.main(["config", "validate", "--config", str(f)]) == 2
```

- [ ] **Step 2: Verify tests fail**

```bash
.venv/bin/pytest tests/test_commands_init_config.py tests/test_commands_config.py \
    -x -q -k "at5 or at6" 2>&1 | head -30
```

- [ ] **Step 3: Implement**

`src/tctl/commands/config_cmd.py`: copy from `src/vctl/commands/config_cmd.py`; rewrite `from vctl.` → `from tctl.`; update any `"vctl"` display strings to `"tctl"`.

`src/tctl/commands/init_config.py`: copy from `src/vctl/commands/init_config.py`; rewrite imports; update the scaffold template (by calling `tctl.workloads.vllm.templates` instead of the old `vctl.commands.templates`). Ensure `--dir` flag writes `cluster.yaml` containing `apiVersion: tctl/v1`, `haproxy:`, and `vllm.default_profile`.

`tests/conftest.py` — exact replacements per spec §5.1:

1. Env-var unset list at top:
   - Old: `for var in ("VCTL_PROFILE", "MODEL_PROFILE", "VCTL_LB__HOST"):`
   - New: `for var in ("TCTL_PROFILE", "TCTL_HAPROXY__HOST", "TCTL_TEST_NO_SOCKET"):`

2. SIGKILL sweeper `_sweep_leaked_vctl_serve_at_session_end` — line ~149:
   - Old: `is_vctl_serve = "vctl" in cmd_str and "serve" in cmd_str`
   - New: `is_vctl_serve = "tctl" in cmd_str and "vllm" in cmd_str and "serve" in cmd_str`

3. Update warning message: `"[F7] swept {killed} leaked vctl-serve/fake-vllm"` → `"[F7] swept {killed} leaked tctl-vllm-serve/fake-vllm"`

`tests/test_smoke.py`: update entry-point name (`vctl` → `tctl`), version string (`0.8.0` → `0.9.0`), any `~/.vctl/` references to `~/.tctl/`.

Mechanical rewrites for the remaining test files (`test_commit_c.py`, `test_commit_d.py`, `test_commit_e.py`, `test_f2_f3_f4.py`, `test_f7_f8_f10.py`, `test_coverage_supplement.py`, `test_integration_endtoend.py`):
- All `from vctl.X` → `from tctl.X`
- All `import vctl.X` → `import tctl.X`
- All `monkeypatch.setattr("vctl.lb.manager.*")` → `"tctl.workloads.haproxy.manager.*"` (and other path mappings)
- All YAML fixtures: `apiVersion: vctl/v1` → `tctl/v1`, `lb:` → `haproxy:`, top-level `profile:` → `vllm:\n  default_profile:`
- All session name strings: `vctl-lb` → `tctl-haproxy`, `vctl-vllm-` → `tctl-vllm-`, `vctl-lmmseval` → `tctl-lmms`
- All env var strings: `VCTL_PROFILE` → `TCTL_PROFILE`, `VCTL_TEST_NO_SOCKET` → `TCTL_TEST_NO_SOCKET`, etc.

Also update `tests/test_commands_config.py` and `tests/test_commands_init_config.py`: same mechanical rewrites plus the AT-5/AT-6 tests from Step 1.

- [ ] **Step 4: Verify tests pass**

```bash
.venv/bin/pytest tests/test_commands_config.py tests/test_commands_init_config.py \
    tests/test_smoke.py tests/conftest.py -x -q 2>&1 | head -30
# Run the broader suite excluding src/vctl tests
.venv/bin/pytest tests/ -x -q --ignore=tests/workloads 2>&1 | head -50
```

- [ ] **Step 5: Gates + commit**

```bash
.venv/bin/ruff check src/tctl && .venv/bin/ruff format --check src/tctl
.venv/bin/mypy --strict src/tctl
.venv/bin/pytest tests/ -q --ignore=tests/workloads 2>&1 | tail -20
```

```bash
git add src/tctl/commands/ tests/conftest.py tests/test_smoke.py \
        tests/test_commands_config.py tests/test_commands_init_config.py \
        tests/test_commit_c.py tests/test_commit_d.py tests/test_commit_e.py \
        tests/test_f2_f3_f4.py tests/test_f7_f8_f10.py \
        tests/test_coverage_supplement.py tests/test_integration_endtoend.py
git commit -m "refactor(platform-cmds): add tctl config/init-config; update conftest sweeper; mechanical vctl→tctl rewrites in remaining tests"
```

---

### Task 11: Delete `src/vctl/` + old test files + full suite verification

**CHECKPOINT 4 — after this task the dual-package period ends; only `src/tctl/` remains.**

**What we build:** Delete the old `src/vctl/` package tree and all old flat test files that were superseded by the `tests/workloads/` hierarchy. Run the full test suite. Verify all 12 ATs pass. Verify exit codes 2/3/4/130 are preserved. Check that test count is unchanged relative to before this task.

**Files:**
- Delete: `src/vctl/` (entire tree)
- Delete: flat test files that were moved (see file map §Deleted files in this plan)

- [ ] **Step 1: Write verification tests**

No new test code needed — this task's "test" is confirming all existing tests still pass after deletion. Record the test count before deletion:

```bash
.venv/bin/pytest --co -q 2>&1 | tail -3
# Record: e.g. "157 tests collected"
```

- [ ] **Step 2: Delete old package and superseded test files**

```bash
# Delete the old package
rm -rf src/vctl/

# Delete flat test files that are now in tests/workloads/
rm -f tests/test_lb_manager_b.py tests/test_lb_render.py tests/test_lb_runtime_b.py \
    tests/test_lb_state.py tests/test_lb_routing.py tests/test_lb_reconciler.py \
    tests/test_lb_reconciler_integration.py tests/test_lb_prune_candidates.py \
    tests/test_lb_prune_config.py tests/test_lb_scaling_b.py tests/test_commands_lb_scaling.py \
    tests/test_lb_errors.py tests/test_commands_lb.py tests/test_commands_lb_info.py \
    tests/test_commands_lb_list_health.py tests/test_commands_lb_prune.py \
    tests/test_lb_installer.py tests/test_vllm_manager.py \
    tests/test_vllm_manager_integration.py tests/test_commands_serve.py \
    tests/test_commands_stop.py tests/test_commands_rolling_restart.py \
    tests/test_commands_readonly.py
```

- [ ] **Step 3: Run full suite and verify count matches**

```bash
.venv/bin/pytest --co -q 2>&1 | tail -3
# Confirm test count is within ±5 of pre-deletion count (any delta = moved tests, not dropped)
.venv/bin/pytest -q 2>&1 | tail -20
```

- [ ] **Step 4: Verify all 12 ATs pass**

```bash
.venv/bin/pytest -q -k "test_at1 or test_at2 or test_at3 or test_at4 or test_at5 or \
    test_at6 or test_at7 or test_at8 or test_at9 or test_at10 or test_at11 or test_at12" \
    -v 2>&1 | tail -30
```

All 12 must show `PASSED`.

- [ ] **Step 5: Verify exit codes**

```bash
# Exit 2: missing config
python -m tctl vllm info --config /nonexistent/cluster.yaml; echo "exit: $?"

# Exit 3: no pool routing (requires a valid but mis-routed config)
# Run manually or via an existing integration test that covers pool_for_model failure.

# Exit 130: KeyboardInterrupt — verified by existing test_cli.py test
.venv/bin/pytest tests/test_cli.py -q -k "keyboard_interrupt" 2>&1

# Exit 0: basic --help
python -m tctl --help; echo "exit: $?"
```

- [ ] **Step 6: Gates + commit**

```bash
.venv/bin/ruff check src/tctl && .venv/bin/ruff format --check src/tctl
.venv/bin/mypy --strict src/tctl
.venv/bin/pytest -q --cov=tctl --cov-report=term-missing --cov-fail-under=50 2>&1 | tail -20
```

```bash
git add -A
git commit -m "refactor(cleanup): delete src/vctl/ and superseded flat test files; tctl-only codebase"
```

---

### Task 12: Docs + CLAUDE.md + COOKBOOK + CHANGELOG + smoke + version bump

**What we build:** Write `docs/COOKBOOK-workloads.md` (Appendix A from spec). Update `CLAUDE.md` (every `vctl` reference), `README.md`, `CHANGELOG.md`, and `examples/cluster.yaml`. Run the four smoke commands and final CI-gate commands. Tag v0.9.0.

**Files:**
- Create: `docs/COOKBOOK-workloads.md`
- Modify: `CLAUDE.md`
- Modify: `CHANGELOG.md`
- Modify: `README.md`
- Modify: `examples/cluster.yaml`
- Modify: `examples/*.yaml` (profile examples — update `apiVersion: vctl/v1` if present)

- [ ] **Step 1: Write failing smoke test**

```python
# Append to tests/test_smoke.py
def test_tctl_python_m_entrypoint() -> None:
    """python -m tctl --help must exit 0 and contain 'vllm' in output."""
    import subprocess, sys
    result = subprocess.run(
        [sys.executable, "-m", "tctl", "--help"],
        capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "vllm" in result.stdout
    assert "haproxy" in result.stdout
    assert "lmms" not in result.stdout  # hidden
```

```bash
.venv/bin/pytest tests/test_smoke.py -x -q -k "python_m_entrypoint" 2>&1 | head -20
```

This should already pass if Task 11 completed successfully. If not, diagnose before proceeding.

- [ ] **Step 2: Write `docs/COOKBOOK-workloads.md`**

Transcribe spec Appendix A verbatim with the three-step guide:
1. Create `src/tctl/workloads/<name>/` package with `__init__.py` (exports `run`), `manager.py` (uses `TmuxSession`), `commands.py` (exports `register_all` + `_cmd_*` functions).
2. Add one line to `cli.py` `_WORKLOADS` dict.
3. (Optional) extend `config/models.py` `ClusterFile` with a new workload section.

Include the `lmdeploy` example from spec Appendix A with all three code blocks.

- [ ] **Step 3: Update CLAUDE.md**

Systematic replacements:
- Every `vctl` → `tctl` in command examples (e.g. `vctl --help` → `tctl --help`)
- `src/vctl/` → `src/tctl/`
- `~/.vctl/` → `~/.tctl/`
- `vctl-lb` → `tctl-haproxy`, `vctl-vllm-` → `tctl-vllm-`, `vctl-lmmseval` → `tctl-lmms`
- `VCTL_*` → `TCTL_*` (but keep `CLUSTER_CONFIG` unchanged)
- Architecture section: update module layout, workload paths, command examples
- Gotchas section: update `lb_scaling.py` gotcha to reference `workloads/haproxy/scaling.py`; update `VCTL_TEST_NO_SOCKET` → `TCTL_TEST_NO_SOCKET`
- Common commands section: `pytest ... --cov=vctl` → `--cov=tctl`; `mypy --strict src/vctl` → `src/tctl`

- [ ] **Step 4: Update CHANGELOG.md**

Prepend:

```markdown
## [0.9.0] - 2026-05-09

### Breaking Changes

- **Package renamed** `vctl` → `tctl`. Uninstall with `uv tool uninstall vctl`; reinstall with `uv tool install tctl`.
- **CLI entry point** renamed: `vctl` → `tctl`.
- **cluster.yaml schema**: `apiVersion: vctl/v1` → `tctl/v1`; `lb:` top-level key → `haproxy:`; top-level `profile:` removed, replaced by `vllm.default_profile`.
- **Env var prefix**: `VCTL_*` → `TCTL_*`. `MODEL_PROFILE` dropped — use `TCTL_PROFILE`.
- **State paths**: `~/.vctl/` → `~/.tctl/`; `~/.tctl/lb/` → `~/.tctl/haproxy/`; rolling-restart sessions moved to `~/.tctl/vllm/rolling-restart/`.
- **Tmux session names**: `vctl-lb` → `tctl-haproxy`; `vctl-lb-watch` → `tctl-haproxy-watch`; `vctl-vllm-<profile>` → `tctl-vllm-<profile>`; `vctl-lmmseval` → `tctl-lmms`.
- **Command structure**: all commands move under workload namespace (`tctl vllm`, `tctl haproxy`, `tctl lmms`). `vctl stop` + `vctl serve stop` merged into single `tctl vllm stop` (drain + tmux-kill + process-sweep).

### New

- `docs/COOKBOOK-workloads.md` — 3-step guide for adding new workloads.
- `tctl vllm stop` — unified stop (drain from LB + kill tmux session + process sweep). Drain failure is non-fatal; cleanup always completes.
- `VllmManager._rolling_restart_session_path(pool, state_dir)` — method replaces module-level `_SESSION_DIR` constant.

### Upgrade path

See spec §4 for full operator upgrade steps. No migration tool is provided; operators drain, stop, uninstall vctl, install tctl, re-init config, restart.
```

- [ ] **Step 5: Update README.md and examples/**

`README.md`: rename throughout; update command table; add note that `tctl` is unrelated to Temporal's `tctl`; update `~/.vctl/` references to `~/.tctl/`.

`examples/cluster.yaml`: rewrite to v0.9.0 shape (`apiVersion: tctl/v1`, `haproxy:`, `vllm:\n  default_profile:`).

`examples/*.yaml` (profile files): update `apiVersion: vctl/v1` → `tctl/v1` if present.

- [ ] **Step 6: Final smoke runs**

```bash
python -m tctl --help          # must exit 0; output contains vllm, haproxy
python -m tctl vllm --help     # must exit 0; output contains info, profiles, serve, stop
python -m tctl haproxy --help  # must exit 0; output contains start, stop, status, prune
python -m tctl init-config --help  # must exit 0
```

- [ ] **Step 7: Full CI-gate pass**

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy --strict src/tctl
.venv/bin/pytest -q --cov=tctl --cov-report=term-missing --cov-fail-under=50
```

All four gates must be green.

- [ ] **Step 8: Commit**

```bash
git add docs/COOKBOOK-workloads.md CLAUDE.md CHANGELOG.md README.md \
        examples/ tests/test_smoke.py
git commit -m "docs(v0.9.0): COOKBOOK, CLAUDE.md, README, CHANGELOG; smoke all exit 0; bump v0.9.0"
```

---

## Checkpoint summary

| Checkpoint | After task | What is stable |
|---|---|---|
| CP-1 | Task 3 | Config schema locked: `tctl/v1`, `haproxy:`, `VllmCluster`, `TCTL_*` env prefix |
| CP-2 | Task 6 | Both haproxy and vllm subsystems moved; session names, state paths, env literals updated |
| CP-3 | Task 9 | `python -m tctl` works end-to-end; all workloads dispatchable |
| CP-4 | Task 11 | `src/vctl/` deleted; single-package codebase; all 12 ATs green |

## AT coverage map

| AT | Covered in task | Test function |
|---|---|---|
| AT-1 (`--help` workload list) | Task 9 | `test_at1_help_lists_workloads_not_lmms` |
| AT-2 (`vllm --help` verbs) | Task 9 | `test_at2_vllm_help_lists_all_verbs` |
| AT-3 (`haproxy --help` verbs) | Task 5 | `test_at3_haproxy_help_lists_all_verbs` |
| AT-4 (lmms reachable + hidden) | Task 8 / Task 9 | `test_at4_lmms_reachable_when_named_directly` |
| AT-5 (`init-config` new shape) | Task 10 | `test_at5_init_config_new_shape` |
| AT-6 (`config validate` new/old) | Task 10 | `test_at6_validate_new_shape_ok`, `test_at6_validate_old_shape_rejects` |
| AT-7 (`TCTL_PROFILE` used; `MODEL_PROFILE` ignored) | Task 3 | `test_tctl_profile_env_var_used`, `test_tctl_model_profile_ignored` |
| AT-8 (stop: drain+kill+sweep) | Task 7 | `test_at8_stop_calls_drain_kill_sweep`, `test_at8_stop_sweep_runs_even_if_drain_fails` |
| AT-9 (rolling-restart path) | Task 6 | `test_at9_rolling_restart_session_path` |
| AT-10 (session name prefixes) | Task 4 / Task 6 | `test_at10_vllm_session_name_prefix`, `test_at10_haproxy_session_name` |
| AT-11 (`MODEL_PROFILE` ignored end-to-end) | Task 9 | `test_at11_model_profile_not_consulted` |
| AT-12 (new workload cookbook) | Task 9 | `test_at12_new_workload_requires_only_3_steps` |
