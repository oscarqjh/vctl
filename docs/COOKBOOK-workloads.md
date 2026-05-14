# COOKBOOK: Adding a New Workload to tctl

This guide shows how to add a new workload (e.g. `tctl lmdeploy`) in exactly
three steps. No base class. No plugin system. Convention-driven.

---

## Overview

A workload is a Python package under `src/tctl/workloads/<name>/` containing:

```
src/tctl/workloads/<name>/
    __init__.py     # exports run(ns, argv_rest) -> int
    manager.py      # <Name>Manager using TmuxSession
    commands.py     # sub-verbs: serve / stop / status / ...
```

Registration is a single line in `src/tctl/cli.py`.

---

## Step 1 — Create the workload package

Create `src/tctl/workloads/lmdeploy/` with three files.

### `__init__.py` — workload entry point

```python
from __future__ import annotations
import argparse

_VERBS: dict[str, str] = {
    "serve":  "_cmd_serve",
    "stop":   "_cmd_stop",
    "status": "_cmd_status",
}


def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    from tctl.workloads.lmdeploy import commands as _cmds  # lazy import

    p = argparse.ArgumentParser(prog="tctl lmdeploy")
    sub = p.add_subparsers(dest="verb", metavar="VERB")
    sub.required = True
    _cmds.register_all(sub)
    parsed = p.parse_args(argv_rest, namespace=ns)
    return getattr(_cmds, _VERBS[parsed.verb])(parsed, [])  # type: ignore[no-any-return]
```

The `run` function is what `cli.py` calls. Keep the import lazy — it runs on
every `tctl lmdeploy ...` invocation, not on startup.

### `manager.py` — tmux session management

```python
from __future__ import annotations
from pathlib import Path
from tctl.tmux import TmuxSession   # always use this, never raw subprocess


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

Key points:
- Always use `TmuxSession` from `tctl.tmux` — never `subprocess.Popen` directly.
- Pass `env=dict(os.environ)` to `TmuxSession` so all operator shell variables
  (PATH, CUDA_VISIBLE_DEVICES, HF_HOME, etc.) are available inside the session.
- Use `tree=True` in `kill()` to terminate child processes (workers, accelerate, etc.)
  that survive pane kill.

### `commands.py` — argparse sub-verbs

```python
from __future__ import annotations
import argparse


def register_all(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    sub.add_parser("serve",  help="start lmdeploy in a detached tmux session")
    sub.add_parser("stop",   help="kill the lmdeploy tmux session")
    sub.add_parser("status", help="show whether lmdeploy is running")


def _cmd_serve(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    from tctl.resolver import resolve
    from tctl.workloads.lmdeploy.manager import LmdeployManager

    resolved = resolve(ns)
    mgr = LmdeployManager(resolved.profile, resolved)
    mgr.start(argv=[...])
    return 0


def _cmd_stop(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    from tctl.workloads.lmdeploy.manager import LmdeployManager

    mgr = LmdeployManager(profile="default", resolved_cfg=None)
    mgr.stop()
    return 0


def _cmd_status(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    from tctl.workloads.lmdeploy.manager import LmdeployManager

    mgr = LmdeployManager(profile="default", resolved_cfg=None)
    running = mgr.status()
    print("running" if running else "stopped")
    return 0
```

### State file convention

Write all state files under `<state_dir>/lmdeploy/<host>/<profile>.*`.
Never write under `haproxy/` or `vllm/` — each workload owns its own subtree.

### Minimal example — lmms-shaped workload

The `lmms` workload (`src/tctl/workloads/lmms/`) is the simplest example in the
codebase. It has no manager class — just a `commands.py` that calls `TmuxSession`
directly, because lmms has a single fixed session name (`tctl-lmms`) and no
per-profile state. Use it as a reference for workloads that run a single global
session.

---

## Step 2 — Register in `cli.py` (one line)

Open `src/tctl/cli.py` and add your workload to `_WORKLOADS`:

```python
# src/tctl/cli.py
_WORKLOADS: dict[str, tuple[str, bool]] = {
    "vllm":      ("tctl.workloads.vllm",      False),
    "haproxy":   ("tctl.workloads.haproxy",   False),
    "lmms":      ("tctl.workloads.lmms",      True),   # True = hidden from top-level help
    "lmdeploy":  ("tctl.workloads.lmdeploy",  False),  # ← add this line
}
```

The second element of the tuple controls visibility in `tctl --help`:
- `False` — shown in the top-level command list.
- `True` — hidden (still fully functional; useful for internal/experimental workloads).

That is the **only change to any existing file**.

---

## Step 3 (optional) — Extend `cluster.yaml` schema

If the workload needs cluster-level config (e.g. `lmdeploy.default_profile`), add a
Pydantic model to `src/tctl/config/models.py` and a field to `ClusterFile`:

```python
# src/tctl/config/models.py
class LmdeployCluster(_Strict):
    default_profile: str | None = None


class ClusterFile(_Strict):
    apiVersion: Literal["tctl/v1"]
    cluster: ClusterSection
    haproxy: LbHaproxy
    vllm: VllmCluster = Field(default_factory=VllmCluster)
    lmdeploy: LmdeployCluster = Field(default_factory=LmdeployCluster)  # ← add
```

Then update `_CLUSTER_TOPLEVEL` in `src/tctl/config/settings.py` to include `"lmdeploy"`:

```python
_CLUSTER_TOPLEVEL: frozenset[str] = frozenset({
    "cluster", "haproxy", "vllm", "lmdeploy",  # ← add lmdeploy
})
```

This allows `TCTL_LMDEPLOY__DEFAULT_PROFILE=my-profile` env overrides to work.

---

## Tests

Place tests in `tests/workloads/lmdeploy/`. The `conftest.py` sweepers catch
any leaked tmux sessions or processes that route artifacts through `tmp_path`.

Minimal test structure:

```
tests/workloads/lmdeploy/
    __init__.py
    test_manager.py      # unit tests for LmdeployManager (mock TmuxSession)
    test_commands.py     # CLI dispatch tests (mock manager)
```

Example pattern (mock `TmuxSession` so no real tmux needed in unit tests):

```python
from unittest.mock import patch, MagicMock
from tctl.workloads.lmdeploy.manager import LmdeployManager


def test_stop_calls_kill(tmp_path):
    with patch("tctl.workloads.lmdeploy.manager.TmuxSession") as mock_cls:
        inst = MagicMock()
        mock_cls.return_value = inst
        mgr = LmdeployManager(profile="test", resolved_cfg=None)
        mgr.stop()
        inst.kill.assert_called_once_with(tree=True, grace_s=5.0)
```

Mark any tests that require a real `tmux` binary with `@pytest.mark.integration`.

---

## Checklist

- [ ] `src/tctl/workloads/<name>/__init__.py` with `run(ns, argv_rest) -> int`
- [ ] `src/tctl/workloads/<name>/manager.py` using `TmuxSession`
- [ ] `src/tctl/workloads/<name>/commands.py` with `register_all(sub)` + verb functions
- [ ] One-line addition to `_WORKLOADS` in `src/tctl/cli.py`
- [ ] (Optional) Pydantic model in `config/models.py` + `_CLUSTER_TOPLEVEL` update
- [ ] Tests under `tests/workloads/<name>/`
- [ ] Entry in `docs/CHANGELOG.md` under the appropriate version
