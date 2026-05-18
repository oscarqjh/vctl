# vctl TmuxSession — Design Spec

## 1. Objective

Introduce a new `TmuxSession` class in `src/vctl/tmux.py` that provides a single,
well-tested abstraction for every place in `vctl` that spawns or manages a detached
tmux session. The three current callers — `LbManager` (haproxy), `VllmManager` (vllm),
and `commands/lmmseval.py` (lmms-eval run-loop) — each re-implement environment
propagation, log capture, and process kill using overlapping but inconsistent patterns.
`TmuxSession` eliminates the env-passing footgun caused by the tmux server caching its
environment from when it was first started (the root cause of three separate bug reports
across v0.5.x–v0.7.x), reduces duplicated kill/log/PID-discovery code to a single
auditable location, and gives all three callers a uniform interface so future feature
additions (rolling-restart, health monitoring) can reason about session lifecycle in one
place.

---

## 2. Background / Pain Points

Three shipped bugs trace directly to the tmux server's stale environment cache:

**v0.5.4 — vllm tmux launch missing PATH (vllm/ninja not found)**
`VllmManager.start()` called `tmux_run_detached_argv(name, [vllm_bin, ...])` without
setting PATH inside the new session. The tmux server, started during user login, held
a minimal PATH from that moment. Inside the detached pane, `vllm` resolved correctly
(absolute path was used), but `vllm` spawns `ninja` via subprocess for FlashInfer JIT
compilation, and `ninja` was not on the server-cached PATH. Workers crashed with
`[Errno 2] No such file or directory: 'ninja'` at model warmup.

**v0.5.6 — PATH propagation hardening**
`VllmManager.start()` was patched to prepend `env K=V ...` to the tmux command:
```python
env_cmd = ["env", f"PATH={venv_bin}:{os.environ['PATH']}", ...]
env_cmd.extend(argv)
tmux_run_detached_argv(name, env_cmd)
```
This worked for PATH but introduced a new pattern inconsistency: `LbManager` and
`lmmseval` still had no env propagation. Any variable not in the tmux server cache
remained invisible in those sessions.

**v0.7.3–v0.7.4 — lmmseval missing HF_HOME / TRANSFORMERS_OFFLINE in tmux**
`_cmd_run_loop` in `commands/lmmseval.py` builds a shell string with explicit
`export K=V;` lines for every env var matching `_ENV_PROPAGATE_PREFIXES`, plus hard-coded
`_FORCED_ENV` entries. This prefix-whitelist approach was fragile: a new var (e.g.
`HF_DATASETS_OFFLINE`) missing from `_ENV_PROPAGATE_PREFIXES` was silently dropped, and
the hard-coded offline vars embedded in the module were an operational surprise for
operators who expected to override them in their shell.

**Root cause shared by all three:** `tmux new-session` inherits the tmux server's
environment, not the calling process's environment. The server's env was snapshotted at
server start — typically the login shell, many hours or days before the `vctl` command
runs. `TmuxSession` fixes this at the `new-session` call site by passing all required
env vars via `-e KEY=VALUE` flags (tmux 3.2+, deployed environment is tmux 3.4).

---

## 3. Architecture

### 3.1 Module layout

```
src/vctl/
    platform.py          # keep: detect_self_ip, which
                         # remove: tmux_run_detached, tmux_run_detached_argv,
                         #         tmux_kill, tmux_session_exists,
                         #         _validate_tmux_name
    tmux.py              # NEW: TmuxSession + moved helpers
```

`_validate_tmux_name` and `tmux_session_exists` move from `platform.py` to `tmux.py`.
The four deprecated helpers (`tmux_run_detached`, `tmux_run_detached_argv`, `tmux_kill`,
`tmux_session_exists`) are deleted from `platform.py` after all callers are migrated.
During migration, `platform.py` can re-export from `tmux.py` under the old names for
one commit, then the re-exports are removed when all call sites are updated.

### 3.2 Class signature

```python
# src/vctl/tmux.py
from __future__ import annotations

import contextlib
import logging
import os
import re
import shlex
import signal
import subprocess
from pathlib import Path

import psutil

_LOG = logging.getLogger(__name__)
_TMUX_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")


def _validate_tmux_name(name: str) -> None:
    """Raise ValueError if name is not a safe tmux session name."""
    if not _TMUX_NAME_RE.fullmatch(name):
        raise ValueError(
            f"invalid tmux session name {name!r}; must match [A-Za-z0-9_.-]+"
        )


def tmux_session_exists(name: str) -> bool:
    """Return True if a tmux session with this name exists."""
    _validate_tmux_name(name)
    try:
        proc = subprocess.run(
            ["tmux", "has-session", "-t", name],
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0
    except FileNotFoundError:
        raise RuntimeError("tmux not installed") from None


class TmuxSession:
    def __init__(
        self,
        name: str,
        env: dict[str, str] | None = None,
        log_path: Path | None = None,
    ) -> None: ...

    def start(self, argv: list[str] | str) -> None: ...

    def exists(self) -> bool: ...

    def pane_pid(self) -> int | None: ...

    def kill(self, *, tree: bool = True, grace_s: float = 5.0) -> None: ...
```

### 3.3 `__init__` — env snapshot semantics

```python
def __init__(
    self,
    name: str,
    env: dict[str, str] | None = None,
    log_path: Path | None = None,
) -> None:
    _validate_tmux_name(name)
    self.name = name
    # env=None means: snapshot os.environ at start() time.
    # Caller passes env={**os.environ, "FOO": "bar"} to override specific keys.
    self._env = env
    self.log_path = log_path
```

`env=None` does NOT mean "inherit tmux server env". It means `os.environ` will be
snapshotted when `start()` is called, which is the calling process's live environment
at that moment. This is the safe default that eliminates the stale-cache footgun.

Callers that need full control pass an explicit `dict`. Callers that need the current
shell environment with a few overrides pass `env={**os.environ, "CUDA_VISIBLE_DEVICES": "0,1"}`.

### 3.4 `start` — exec form vs shell line

```python
def start(self, argv: list[str] | str) -> None:
    """Spawn a new detached tmux session running argv.

    If argv is a list, it is joined with shlex.join before being passed to
    tmux (exec-form safety). If argv is a str, it is passed as-is (shell-line
    form, for callers like lmmseval that build a shell pipeline with `source`).

    Raises RuntimeError if the session already exists (idempotency contract).
    Raises RuntimeError if tmux is not installed.
    Raises ValueError on invalid session name (caught at __init__).
    """
    if self.exists():
        raise RuntimeError(
            f"tmux session {self.name!r} already exists; "
            "call kill() first or use a different name"
        )

    env = self._env if self._env is not None else dict(os.environ)
    _validate_env(env)  # see below — rejects keys/values that break tmux -e parsing
    cmd = shlex.join(argv) if isinstance(argv, list) else argv

    # Build `tmux new-session -d -s NAME -e K=V -e K=V ... CMD`.
    # tmux 3.2+ accepts multiple -e flags; deployed env is 3.4.
    tmux_argv: list[str] = ["tmux", "new-session", "-d", "-s", self.name]
    for k, v in env.items():
        tmux_argv += ["-e", f"{k}={v}"]
    tmux_argv.append(cmd)

    try:
        subprocess.run(tmux_argv, check=True)
    except FileNotFoundError:
        raise RuntimeError("tmux not installed") from None

    if self.log_path is not None:
        subprocess.run(
            [
                "tmux", "pipe-pane", "-t", self.name, "-o",
                f"cat >> {self.log_path}",
            ],
            check=False,
        )
```

**Env-value sanitization.** `tmux -e KEY=VALUE` parses by splitting on the first `=`,
so `=` inside values is fine. But newlines and NUL bytes break the flag silently
(tmux discards them with no error). `_validate_env` is a small helper that raises
`ValueError` on any key containing `=` or any value containing `\n` / `\x00`.
Implementer signature:

```python
def _validate_env(env: dict[str, str]) -> None:
    """Raise ValueError on env entries that would be silently mangled by tmux -e."""
    for k, v in env.items():
        if "=" in k or not k:
            raise ValueError(f"invalid env key {k!r}: empty or contains '='")
        if "\n" in v or "\x00" in v:
            raise ValueError(f"invalid env value for {k!r}: contains newline or NUL")
```

**`log_path` precondition.** The caller is responsible for ensuring `log_path.parent`
exists before calling `start()`. `pipe-pane` runs `cat >> {path}` inside the session;
if the parent directory is missing, `cat` exits non-zero and log capture silently
no-ops (the rest of the session continues fine). `TmuxSession.start()` does NOT
`mkdir -p` because per-feature managers (`VllmManager`, `LbManager`) already own
their state directory layout and should not have that responsibility split.

The `-e KEY=VALUE` flag was introduced in tmux 3.2 and sets an environment variable
in the new session, overriding whatever the tmux server cached. Passing every key from
`env` via `-e` means the session sees exactly the environment the caller intended,
regardless of server age or session-level `update-environment` settings.

`shlex.join` on list-form argv matches the existing `tmux_run_detached_argv` behavior
(E3 requirement from `platform.py` comments).

### 3.5 `exists`

```python
def exists(self) -> bool:
    return tmux_session_exists(self.name)
```

Thin delegation — keeps callers from importing both `TmuxSession` and
`tmux_session_exists` separately.

### 3.6 `pane_pid`

```python
def pane_pid(self) -> int | None:
    """Return the PID of the foreground process in the session's first pane.

    Uses `tmux list-panes -t NAME -F '#{pane_pid}'`.
    Returns None if the session does not exist or the PID cannot be parsed.
    """
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-t", self.name, "-F", "#{pane_pid}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        raw = result.stdout.strip().splitlines()
        if not raw:
            return None
        return int(raw[0])
    except (FileNotFoundError, ValueError):
        return None
```

`pane_pid` returns the shell or exec PID inside the pane. For vllm this is the shell
launched by tmux that then exec'd `env ... vllm serve ...`; `kill(tree=True)` uses
`psutil.Process(pane_pid).children(recursive=True)` to reach the actual workers.

### 3.7 `kill` — SIGTERM tree → wait → SIGKILL → kill-session

```python
def kill(self, *, tree: bool = True, grace_s: float = 5.0) -> None:
    """Terminate the session's process tree and then kill the tmux session.

    Algorithm:
      1. If tree=True: collect pane_pid() + all psutil descendants.
         SIGTERM all; poll for exit up to grace_s; SIGKILL survivors.
      2. tmux kill-session -t NAME (idempotent — check=False).

    Idempotent: safe to call when session does not exist.
    """
    if not self.exists():
        return

    if tree:
        pid = self.pane_pid()
        if pid is not None:
            try:
                root = psutil.Process(pid)
                procs = root.children(recursive=True) + [root]
            except psutil.NoSuchProcess:
                procs = []

            for p in procs:
                with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                    p.send_signal(signal.SIGTERM)

            _, alive = psutil.wait_procs(procs, timeout=grace_s)
            for p in alive:
                with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                    p.send_signal(signal.SIGKILL)

    try:
        subprocess.run(
            ["tmux", "kill-session", "-t", self.name],
            check=False,
            capture_output=True,
        )
    except FileNotFoundError:
        pass  # tmux gone — nothing to clean up
```

`kill(tree=False)` is available for callers (like `LbManager.stop`) where the process
of interest has already been terminated via pidfile and only the empty tmux pane
remains. `kill(tree=True)` (default) covers vllm and lmmseval where the session IS the
process tree.

---

## 4. Migration Plan

| Caller | Before | After | Behavior change |
|---|---|---|---|
| `LbManager.start()` | `tmux_run_detached_argv(name, [haproxy, ...])` — no env | `TmuxSession(name, env={**os.environ}).start([haproxy, ...])` | haproxy session now receives caller's full env. **Risk:** if haproxy reads an env var that conflicts with its config (unlikely; haproxy is env-agnostic), behavior changes. Documented below. |
| `LbManager.stop()` | `tmux_kill(name)` | `TmuxSession(name).kill(tree=False)` | Equivalent; `kill(tree=False)` skips psutil tree-kill (haproxy daemon was already killed via pidfile SIGTERM before this call). |
| `LbManager.status()` | `tmux_session_exists(name)` | `TmuxSession(name).exists()` | Identical behavior. |
| `VllmManager.start()` | `tmux_run_detached_argv(name, env_cmd)` where `env_cmd = ["env", "PATH=...", "CUDA=...", ..., vllm_bin, ...]` + separate `pipe-pane` call | `TmuxSession(name, env=env_overrides, log_path=self.log_path).start([vllm_bin, ...])` | env_overrides dict unchanged in content; `-e` flag mechanism replaces the `env(1)` prefix wrapper. log_path moves into constructor. |
| `VllmManager._cleanup_on_failure()` | `tmux_kill(name)` | `TmuxSession(name).kill(tree=True)` | tree-kill catches any spawned workers even in failure; previous code just killed the tmux session. Minor improvement. |
| `VllmManager.stop()` | `subprocess.run(["tmux", "send-keys", ..., "C-c", ""])` + pidfile poll + `tmux_kill(name)` | `subprocess.run(["tmux", "send-keys", ..., "C-c", ""])` + `TmuxSession(name).kill(tree=True, grace_s=grace)` | C-c step preserved (vllm clean shutdown contract). `kill(tree=True)` replaces the pidfile-poll + `tmux_kill` pair with one tree-kill that also catches escaped accelerate workers. See §4.2. |
| `VllmManager.start()` double-start guard | `tmux_session_exists(self.session_name)` | `TmuxSession(self.session_name).exists()` | Identical. Was missed in earlier draft of this table. |
| `VllmManager.console()` | `tmux_session_exists(name)` | `TmuxSession(name).exists()` | Identical. |
| `VllmManager.status()` | `tmux_session_exists(name)` | `TmuxSession(name).exists()` | Identical. |
| `lmmseval._cmd_run_loop()` | `_build_env_exports()` shell string with `export K=V;` prefix-whitelist approach + `tmux_run_detached(name, cmd_str)` | `TmuxSession(name, env={**os.environ, **_FORCED_ENV}).start(shell_body)` where `shell_body` is the `source venv/activate && bash run_loop.sh ...` line | Full env snapshot replaces prefix-whitelist. `_FORCED_ENV` still applied as explicit overrides. All env vars from operator shell are now available — no more missing HF_HOME / TRANSFORMERS_OFFLINE. |
| `lmmseval._cmd_stop()` | `tmux_kill(name)` | `TmuxSession(name).kill(tree=True)` | tree-kill reaches run_loop.sh + accelerate + 8 worker processes. Previous `tmux_kill` only killed the pane; workers were orphaned. |
| `lmmseval._cmd_status()` | `tmux_session_exists(name)` | `TmuxSession(name).exists()` | Identical. |

### 4.1 LbManager env propagation — risk note

`LbManager.start()` currently calls `tmux_run_detached_argv` with no env, meaning
haproxy runs with whatever the tmux server cached at server start. After migration,
haproxy's tmux pane will receive the operator's full `os.environ`. HAProxy reads only
a small set of env vars (`HAPROXY_CFGFILES`, `HAPROXY_MASTER_CLI`, internal ones).
No documented haproxy behavior depends on inheriting a specific PATH or any
Python-related env var. The practical risk is near zero, but if a future bug appears
where haproxy behaves differently depending on an unexpected env var, `LbManager.start()`
can be narrowed: `TmuxSession(name, env={"PATH": os.environ.get("PATH", "")})`.
This is documented here rather than implemented — the full-snapshot default is correct
for the common case and matches `VllmManager`'s existing behavior.

### 4.2 VllmManager stop() consolidation — behavior change detail

Before migration, `VllmManager.stop()` does:
1. `subprocess.run(["tmux", "send-keys", "-t", session, "C-c", ""])` — sends SIGINT to
   vllm's foreground process.
2. Polls pidfile PID with `os.kill(pid, 0)` for `VCTL_KILL_GRACE` seconds.
3. `tmux_kill(session)` — kills the pane regardless.

After migration, `TmuxSession.kill(tree=True, grace_s=grace)` replaces steps 2–3 with
a psutil tree-kill from `pane_pid()`. **Decision: keep step 1 (C-c via send-keys)
explicitly in `VllmManager.stop()`.** vllm responds to SIGINT with a clean shutdown
(flushes KV cache, releases GPU memory cleanly). `TmuxSession.kill(tree=True)` sends
SIGTERM, which vllm also handles, but C-c is the documented path. Migration table
row reflects: VllmManager.stop() still calls `send-keys C-c` first, THEN
`TmuxSession.kill(tree=True, grace_s=grace)`:

```python
# VllmManager.stop() after migration
subprocess.run(
    ["tmux", "send-keys", "-t", self.session_name, "C-c", ""],
    check=False,
)
TmuxSession(self.session_name).kill(tree=True, grace_s=grace)
```

Net effect: equivalent to before for the graceful-shutdown path, adds tree-kill of
accelerate worker processes that might survive `tmux kill-session` if vllm's child
escapes the pane.

---

## 5. Tests

### 5.1 Unit tests — `tests/test_tmux.py`

All tests use `monkeypatch` to stub `subprocess.run`. No real tmux required.

**`_validate_tmux_name`**

```python
def test_validate_tmux_name_valid():
    _validate_tmux_name("vctl-lb")          # no exception

def test_validate_tmux_name_rejects_slash():
    with pytest.raises(ValueError, match="invalid tmux session name"):
        _validate_tmux_name("bad/name")

def test_validate_tmux_name_rejects_empty():
    with pytest.raises(ValueError):
        _validate_tmux_name("")

def test_validate_tmux_name_rejects_space():
    with pytest.raises(ValueError):
        _validate_tmux_name("bad name")
```

**`TmuxSession.__init__` — name validation**

```python
def test_init_rejects_invalid_name():
    with pytest.raises(ValueError):
        TmuxSession("bad/name")
```

**`TmuxSession.start` — -e flags**

```python
def test_start_list_form_passes_env_flags(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "vctl.tmux.subprocess.run",
        lambda argv, **kw: calls.append(argv) or _fake_ok(),
    )
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    sess = TmuxSession("test-sess", env={"FOO": "bar", "BAZ": "qux"})
    sess.start(["echo", "hello"])
    tmux_call = calls[0]
    assert "-e" in tmux_call
    assert "FOO=bar" in tmux_call
    assert "BAZ=qux" in tmux_call
    assert "echo hello" in tmux_call[-1]  # shlex-joined

def test_start_str_form_passed_verbatim(monkeypatch):
    calls = []
    monkeypatch.setattr("vctl.tmux.subprocess.run", lambda argv, **kw: calls.append(argv) or _fake_ok())
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    sess = TmuxSession("test-sess", env={})
    sess.start("source /venv/bin/activate && bash run.sh")
    assert calls[0][-1] == "source /venv/bin/activate && bash run.sh"

def test_env_none_snapshots_at_start_time(monkeypatch):
    """Headline behavior: env=None snapshots os.environ at start() call time,
    NOT at __init__ time. This is what fixes the stale-tmux-server-cache footgun."""
    calls = []
    monkeypatch.setattr(
        "vctl.tmux.subprocess.run",
        lambda argv, **kw: calls.append(argv) or _fake_ok(),
    )
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    monkeypatch.setenv("VCTL_TEST_KEY", "before_init")
    sess = TmuxSession("test-sess")  # env=None — should NOT snapshot now
    monkeypatch.setenv("VCTL_TEST_KEY", "after_init_before_start")
    sess.start(["echo"])
    tmux_call = calls[0]
    # The value at start() time must win, not the value at __init__ time.
    assert "VCTL_TEST_KEY=after_init_before_start" in tmux_call
    assert "VCTL_TEST_KEY=before_init" not in tmux_call

def test_validate_env_rejects_bad_keys_and_values(monkeypatch):
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    sess1 = TmuxSession("test-sess", env={"K=BAD": "v"})
    with pytest.raises(ValueError, match="invalid env key"):
        sess1.start(["echo"])
    sess2 = TmuxSession("test-sess", env={"K": "value\nwith\nnewline"})
    with pytest.raises(ValueError, match="newline or NUL"):
        sess2.start(["echo"])
```

**`TmuxSession.start` — double-start guard**

```python
def test_start_raises_if_session_exists(monkeypatch):
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: True)
    sess = TmuxSession("vctl-lb", env={})
    with pytest.raises(RuntimeError, match="already exists"):
        sess.start(["haproxy", "-f", "/tmp/ha.cfg"])
```

**`TmuxSession.start` — log_path triggers pipe-pane**

```python
def test_start_with_log_path_calls_pipe_pane(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("vctl.tmux.subprocess.run", lambda argv, **kw: calls.append(argv) or _fake_ok())
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    log = tmp_path / "out.log"
    sess = TmuxSession("test-sess", env={}, log_path=log)
    sess.start(["echo", "hi"])
    pipe_call = next((c for c in calls if "pipe-pane" in c), None)
    assert pipe_call is not None
    assert str(log) in " ".join(pipe_call)
```

**`TmuxSession.kill` — idempotent when session gone**

```python
def test_kill_is_noop_if_session_gone(monkeypatch):
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    sess = TmuxSession("vctl-lb", env={})
    sess.kill()  # must not raise
```

**`TmuxSession.kill` — tree=False skips psutil**

```python
def test_kill_tree_false_skips_psutil(monkeypatch):
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: True)
    monkeypatch.setattr("vctl.tmux.subprocess.run", lambda argv, **kw: _fake_ok())
    monkeypatch.setattr("vctl.tmux.TmuxSession.pane_pid", lambda self: 99)
    import psutil
    pane_pid_calls = []
    monkeypatch.setattr(psutil, "Process", lambda pid: pane_pid_calls.append(pid))
    sess = TmuxSession("vctl-lb", env={})
    sess.kill(tree=False)
    assert not pane_pid_calls  # psutil.Process never called
```

**`TmuxSession.pane_pid` — parses output**

```python
def test_pane_pid_parses_output(monkeypatch):
    import subprocess as _sp
    result = _sp.CompletedProcess(args=[], returncode=0, stdout="12345\n")
    monkeypatch.setattr("vctl.tmux.subprocess.run", lambda *a, **kw: result)
    sess = TmuxSession("vctl-vllm-qwen", env={})
    assert sess.pane_pid() == 12345

def test_pane_pid_returns_none_if_session_gone(monkeypatch):
    import subprocess as _sp
    result = _sp.CompletedProcess(args=[], returncode=1, stdout="")
    monkeypatch.setattr("vctl.tmux.subprocess.run", lambda *a, **kw: result)
    sess = TmuxSession("gone", env={})
    assert sess.pane_pid() is None
```

**`TmuxSession` — no real tmux on PATH raises RuntimeError**

```python
def test_start_raises_runtime_if_tmux_not_installed(monkeypatch):
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    def fake_run(argv, **kw):
        raise FileNotFoundError
    monkeypatch.setattr("vctl.tmux.subprocess.run", fake_run)
    sess = TmuxSession("vctl-lb", env={})
    with pytest.raises(RuntimeError, match="tmux not installed"):
        sess.start(["haproxy"])
```

### 5.2 Migration regression tests

Existing test files **must pass without modification** (or with import-path-only changes
if `tmux_session_exists` moves from `platform` to `tmux`):

- `tests/test_lb_manager.py` — all `monkeypatch.setattr("vctl.lb.manager.tmux_*", ...)`
  targets must be updated to `"vctl.lb.manager.TmuxSession"` if manager.py switches
  imports. Alternatively: re-export `TmuxSession` from `platform.py` transiently.
- `tests/test_vllm_manager.py` — same pattern.
- `tests/test_commands_lmmseval.py` — same.

Strategy: update import sites in the three caller modules first, then update
`monkeypatch` targets in tests in the same commit. No test logic changes.

### 5.3 Integration tests — `tests/test_tmux.py` (continued)

```python
@pytest.mark.integration
def test_session_start_exists_kill_roundtrip(tmp_path):
    """Requires tmux 3.2+ on PATH."""
    name = "vctl-test-integration"
    sess = TmuxSession(name, env={"VCTL_TMUX_TEST": "1"})
    sess.start(["sleep", "60"])
    try:
        assert sess.exists()
        pid = sess.pane_pid()
        assert pid is not None and pid > 0
        sess.kill(tree=True)
        assert not sess.exists()
    finally:
        sess.kill(tree=False)  # idempotent cleanup

@pytest.mark.integration
def test_env_propagation_into_session(tmp_path):
    """Verify that env vars passed via -e reach the pane process."""
    name = "vctl-test-env"
    marker_file = tmp_path / "env_marker.txt"
    # Session runs: sh -c 'echo $VCTL_MARKER > <file>'
    sess = TmuxSession(
        name,
        env={"VCTL_MARKER": "hello-from-test"},
    )
    sess.start(f"sh -c 'echo $VCTL_MARKER > {marker_file}; sleep 5'")
    try:
        import time
        time.sleep(1)  # give sh time to write
        assert marker_file.exists()
        assert "hello-from-test" in marker_file.read_text()
    finally:
        sess.kill(tree=False)

@pytest.mark.integration
def test_log_path_captures_output(tmp_path):
    """Verify that log_path receives stdout via pipe-pane."""
    name = "vctl-test-log"
    log = tmp_path / "session.log"
    sess = TmuxSession(name, env={}, log_path=log)
    sess.start(["echo", "captured-output"])
    try:
        import time
        time.sleep(1)
        text = log.read_text() if log.exists() else ""
        assert "captured-output" in text
    finally:
        sess.kill(tree=False)
```

---

## 6. Acceptance Tests

**AT-1** — `vctl serve` propagates caller PATH (vllm + ninja resolve)

```
Given: os.environ["PATH"] = "/venv/bin:/usr/bin", VllmManager.start() called
When:  TmuxSession("vctl-vllm-qwen", env={**os.environ, "PATH": "/venv/bin:/usr/bin"}).start([...])
       is called and tmux new-session runs
Then:  `tmux list-panes -t vctl-vllm-qwen -F '#{pane_pid}'` returns a PID;
       `psutil.Process(pid).environ()["PATH"]` contains "/venv/bin";
       the process tree includes a ninja subprocess during JIT warmup
```

Unit skeleton:
```python
def test_at1_vllm_path_in_env_flags(monkeypatch):
    calls = []
    monkeypatch.setattr("vctl.tmux.subprocess.run", lambda a, **k: calls.append(a) or _ok())
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    sess = TmuxSession("vctl-vllm-qwen", env={"PATH": "/venv/bin:/usr/bin"})
    sess.start(["vllm", "serve", "model"])
    new_sess_call = calls[0]
    assert any(arg == "PATH=/venv/bin:/usr/bin" for arg in new_sess_call)
```

---

**AT-2** — `vctl lb start` propagates caller env to haproxy session

```
Given: os.environ contains "CUSTOM_VAR=sentinel"; LbManager.start() called
When:  TmuxSession("vctl-lb", env={**os.environ}).start([haproxy_binary, ...])
Then:  tmux new-session argv contains "-e CUSTOM_VAR=sentinel"
```

Unit skeleton:
```python
def test_at2_lb_manager_env_propagated(monkeypatch):
    calls = []
    monkeypatch.setattr("vctl.tmux.subprocess.run", lambda a, **k: calls.append(a) or _ok())
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    sess = TmuxSession("vctl-lb", env={"CUSTOM_VAR": "sentinel"})
    sess.start(["haproxy", "-f", "/tmp/ha.cfg"])
    assert any("CUSTOM_VAR=sentinel" == arg for arg in calls[0])
```

---

**AT-3** — `vctl lmmseval run-loop` propagates HF_* / CUDA_* / TRANSFORMERS_OFFLINE

```
Given: os.environ["HF_HOME"] = "/data/hf"; _FORCED_ENV = {"TRANSFORMERS_OFFLINE": "1"}
When:  TmuxSession("vctl-lmmseval", env={**os.environ, **_FORCED_ENV}).start(shell_body)
Then:  new-session argv contains "-e HF_HOME=/data/hf" and "-e TRANSFORMERS_OFFLINE=1"
       shell_body is passed as the final verbatim argument
```

Unit skeleton:
```python
def test_at3_lmmseval_hf_env_propagated(monkeypatch):
    calls = []
    monkeypatch.setattr("vctl.tmux.subprocess.run", lambda a, **k: calls.append(a) or _ok())
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    env = {"HF_HOME": "/data/hf", "TRANSFORMERS_OFFLINE": "1"}
    sess = TmuxSession("vctl-lmmseval", env=env)
    sess.start("source /venv/bin/activate && bash run_loop.sh task.sh 0 5")
    args = calls[0]
    assert "-e" in args and "HF_HOME=/data/hf" in args
    assert "TRANSFORMERS_OFFLINE=1" in args
    assert args[-1] == "source /venv/bin/activate && bash run_loop.sh task.sh 0 5"
```

---

**AT-4** — `vctl serve stop` tree-kills vllm + accelerate workers

```
Given: TmuxSession("vctl-vllm-qwen") with pane_pid=1000;
       psutil.Process(1000).children(recursive=True) = [Process(1001), Process(1002)]
When:  TmuxSession("vctl-vllm-qwen").kill(tree=True, grace_s=5.0)
Then:  SIGTERM sent to PIDs 1001, 1002, 1000 (all children + root);
       after grace_s, any survivors receive SIGKILL;
       tmux kill-session invoked after psutil cleanup
```

Unit skeleton:
```python
def test_at4_serve_stop_tree_kills_workers(monkeypatch):
    sigtermed = []
    killed = []
    class FakeProc:
        def __init__(self, pid): self.pid = pid
        def send_signal(self, sig):
            (sigtermed if sig == signal.SIGTERM else killed).append(self.pid)
        def children(self, recursive=False):
            return [FakeProc(1001), FakeProc(1002)]
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: True)
    monkeypatch.setattr("vctl.tmux.TmuxSession.pane_pid", lambda s: 1000)
    monkeypatch.setattr("vctl.tmux.psutil.Process", lambda pid: FakeProc(pid))
    monkeypatch.setattr("vctl.tmux.psutil.wait_procs", lambda procs, timeout: ([], []))
    monkeypatch.setattr("vctl.tmux.subprocess.run", lambda *a, **k: _ok())
    TmuxSession("vctl-vllm-qwen").kill(tree=True)
    assert set(sigtermed) == {1000, 1001, 1002}
```

---

**AT-5** — `vctl lmmseval stop` tree-kills run_loop.sh + accelerate + workers

```
Given: TmuxSession("vctl-lmmseval") with pane_pid=2000;
       psutil.Process(2000).children(recursive=True) = [Process(p) for p in range(2001, 2009)]
When:  TmuxSession("vctl-lmmseval").kill(tree=True, grace_s=5.0)
Then:  SIGTERM sent to pids 2000..2008 (9 total — root + 8 workers);
       tmux kill-session invoked
```

Unit skeleton: same pattern as AT-4 with 8 fake children.

---

**AT-6** — `vctl lb stop` daemon-kills haproxy + cleans empty pane

```
Given: LbManager.stop() has already sent SIGTERM to haproxy via pidfile;
       haproxy PID is confirmed dead; tmux session "vctl-lb" still exists (empty pane)
When:  TmuxSession("vctl-lb").kill(tree=False)
Then:  psutil tree-kill NOT performed (tree=False);
       tmux kill-session -t vctl-lb IS called;
       call is idempotent if session already gone (no error raised)
```

Unit skeleton:
```python
def test_at6_lb_stop_tree_false_no_psutil(monkeypatch):
    run_calls = []
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: True)
    monkeypatch.setattr("vctl.tmux.TmuxSession.pane_pid", lambda s: 999)
    monkeypatch.setattr("vctl.tmux.subprocess.run",
                        lambda a, **k: run_calls.append(a) or _ok())
    psutil_calls = []
    monkeypatch.setattr("vctl.tmux.psutil.Process",
                        lambda pid: psutil_calls.append(pid))
    TmuxSession("vctl-lb").kill(tree=False)
    assert not psutil_calls
    assert any("kill-session" in " ".join(c) for c in run_calls)
```

---

**AT-7** — `TmuxSession.start` raises if session already exists

```
Given: tmux_session_exists("vctl-lb") returns True
When:  TmuxSession("vctl-lb").start(["haproxy", "-f", "ha.cfg"])
Then:  RuntimeError raised with message containing "already exists";
       no tmux new-session command is issued
```

Unit skeleton:
```python
def test_at7_start_raises_on_existing_session(monkeypatch):
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: True)
    run_calls = []
    monkeypatch.setattr("vctl.tmux.subprocess.run",
                        lambda a, **k: run_calls.append(a) or _ok())
    with pytest.raises(RuntimeError, match="already exists"):
        TmuxSession("vctl-lb", env={}).start(["haproxy"])
    assert not run_calls
```

---

**AT-8** — `TmuxSession.kill` is idempotent when session already gone

```
Given: tmux_session_exists("vctl-lb") returns False (session never existed or already killed)
When:  TmuxSession("vctl-lb").kill() called (with default tree=True)
Then:  No exception raised; no subprocess.run called; no psutil calls
```

Unit skeleton:
```python
def test_at8_kill_idempotent_when_gone(monkeypatch):
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    run_calls = []
    monkeypatch.setattr("vctl.tmux.subprocess.run",
                        lambda a, **k: run_calls.append(a) or _ok())
    TmuxSession("vctl-lb").kill()  # must not raise
    assert not run_calls
```

---

**AT-9** — `TmuxSession(name, log_path=...)` captures stdout/stderr to log file

```
Given: log_path=/tmp/pytest-xxx/session.log
When:  TmuxSession("vctl-vllm-qwen", env={}, log_path=log_path).start([...])
Then:  After tmux new-session succeeds, subprocess.run is called a second time
       with argv containing ["tmux", "pipe-pane", "-t", name, "-o", "cat >> /tmp/pytest-xxx/session.log"]
```

Unit skeleton:
```python
def test_at9_log_path_emits_pipe_pane(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    monkeypatch.setattr("vctl.tmux.subprocess.run",
                        lambda a, **k: calls.append(a) or _ok())
    log = tmp_path / "out.log"
    TmuxSession("vctl-vllm-qwen", env={}, log_path=log).start(["vllm"])
    pipe_calls = [c for c in calls if "pipe-pane" in c]
    assert len(pipe_calls) == 1
    assert str(log) in " ".join(pipe_calls[0])
```

---

**AT-10** — `TmuxSession` rejects invalid session names

```
Given: name = "bad/name" (contains slash) OR "bad name" (contains space) OR "" (empty)
When:  TmuxSession(name) is constructed
Then:  ValueError raised with message matching "invalid tmux session name"
       for each case; no subprocess is ever invoked
```

Unit skeleton:
```python
@pytest.mark.parametrize("bad_name", ["bad/name", "bad name", "", "has\ttab"])
def test_at10_invalid_name_raises_at_init(bad_name, monkeypatch):
    run_calls = []
    monkeypatch.setattr("vctl.tmux.subprocess.run",
                        lambda a, **k: run_calls.append(a) or _ok())
    with pytest.raises(ValueError, match="invalid tmux session name"):
        TmuxSession(bad_name)
    assert not run_calls
```

---

## 7. Risks / Non-Goals

### Risks

**tmux version floor (3.2+):** `-e KEY=VALUE` for `new-session` was added in tmux 3.2.
The deployed environment uses tmux 3.4 (confirmed). Any host running tmux < 3.2 will
receive an "unknown option" error from tmux on `start()`. **Mitigation (concrete):**
`start()` runs a one-shot `subprocess.run(["tmux", "-V"], capture_output=True, text=True)`
on first call per process; result is cached in a module-level `_TMUX_VERSION_OK: bool | None`.
Parses `tmux 3.4` → tuple `(3, 4)`; if `(major, minor) < (3, 2)` raises
`RuntimeError("tmux 3.2+ required; found {ver}")`. Caching avoids re-running for every
session. Example:

```python
_TMUX_VERSION_OK: bool | None = None

def _check_tmux_version() -> None:
    global _TMUX_VERSION_OK
    if _TMUX_VERSION_OK is True:
        return
    proc = subprocess.run(["tmux", "-V"], capture_output=True, text=True, check=False)
    m = re.match(r"tmux (\d+)\.(\d+)", proc.stdout)
    if not m or (int(m.group(1)), int(m.group(2))) < (3, 2):
        raise RuntimeError(f"tmux 3.2+ required; found {proc.stdout.strip()!r}")
    _TMUX_VERSION_OK = True
```

Call at the top of `start()`.

**Large env dict in argv:** passing every `os.environ` key via `-e` flags can produce
a long argument list on systems with many env vars (e.g. conda environments with hundreds
of vars). On Linux, `ARG_MAX` is typically 2 MB — well above any realistic env size.
No mitigation needed, but note this if running on embedded systems.

**haproxy env sensitivity:** as noted in Section 4.1, propagating the full caller env
to the haproxy tmux session is a behavior change. The risk is low but real for exotic
deployments. Document in CHANGELOG.

**psutil accuracy for pane_pid:** `#{pane_pid}` returns the tmux pane's shell PID (the
login shell tmux spawned). If the command was run via a shell line (str-form `start()`),
the pane shell exec'd the shell string, so `pane_pid()` returns the shell PID and
`children(recursive=True)` correctly reaches the actual workers. If the command was
run via exec form with a short-lived process (e.g. `echo hello`), `pane_pid()` may
return 0 or None by the time `kill()` is called. This is not a problem in practice
(no caller needs tree-kill of a short-lived command) but is documented here.

### Non-Goals

- **Not replacing per-feature state files.** `TmuxSession` owns nothing on disk. State
  files (`haproxy.pid`, `vllm/<host>/<profile>.pid`, `vllm/<host>/<profile>.log`, etc.)
  remain the responsibility of `LbManager` and `VllmManager`. `TmuxSession` only manages
  the tmux session lifetime.
- **Not adding cross-host tmux management.** All `TmuxSession` calls are local. SSH-based
  remote tmux sessions (used in `rolling-restart` via `bash -lc 'vctl serve restart'`)
  are a different code path and are intentionally out of scope.
- **Not touching SSH-spawned tmux sessions.** Rolling-restart (Phase 3) uses
  `subprocess.run(["ssh", ..., "bash -lc 'vctl serve restart'"])` which invokes vctl on
  the remote host; that remote vctl creates its own `TmuxSession` locally. No
  cross-host TmuxSession abstraction is added.
- **Not adding a session registry or cross-session coordination.** Each `TmuxSession`
  instance is independent. There is no singleton tracking which sessions are "vctl-managed".
- **Not touching `vctl.config.models` / `cluster.yaml`.** No schema changes. No new
  profile fields.
- **Not touching the haproxy reload path.** `LbManager.reload()` uses `subprocess.run`
  directly (not tmux) — the refactored process is not inside a session. No change.

---

## 8. Tech Stack

- **Python 3.10+** — `from __future__ import annotations` in `tmux.py`. Union syntax
  `dict[str, str] | None` requires `from __future__ import annotations` for 3.10
  compatibility.
- **`subprocess`** (stdlib) — all tmux interactions. No shell=True except where the
  caller passes a str to `start()`, which is forwarded verbatim as the tmux command
  argument (tmux itself runs it through `/bin/sh`).
- **`psutil`** (already a runtime dep via `vllm_manager.py`) — `Process.children()` and
  `wait_procs()` for tree-kill. No new dependency.
- **`re`** (stdlib) — `_TMUX_NAME_RE` pattern, moved from `platform.py`.
- **`signal`** (stdlib) — `SIGTERM` / `SIGKILL` constants in `kill()`.
- **`contextlib`** (stdlib) — `contextlib.suppress` for `psutil.NoSuchProcess` /
  `psutil.AccessDenied` during kill.
- **Pydantic** — NOT involved. `TmuxSession` is a plain Python class with no schema
  validation. `env` is a plain `dict[str, str]`.
- **`mypy --strict`** — all code in `src/vctl/tmux.py` must type-check under strict
  mode. `env: dict[str, str] | None` with a runtime `dict(os.environ)` fallback is
  fully typed. `argv: list[str] | str` is an explicit union.
- **Ruff** — `target-version = py310`, `line-length = 100`. The `from __future__ import
  annotations` import satisfies UP007 (use `X | Y` union syntax) while remaining 3.10
  compatible.
- **tmux 3.2+** — `-e KEY=VALUE` flag for `new-session`. Deployed env is tmux 3.4.
  Version check recommended in `start()` for clear error messages on older hosts.

---

## File Map

| File | Change |
|---|---|
| `src/vctl/tmux.py` | **New** — `TmuxSession`, `_validate_tmux_name`, `tmux_session_exists` |
| `src/vctl/platform.py` | Remove `tmux_run_detached`, `tmux_run_detached_argv`, `tmux_kill`, `tmux_session_exists`, `_validate_tmux_name` |
| `src/vctl/lb/manager.py` | Import `TmuxSession` from `vctl.tmux`; replace all `tmux_*` calls |
| `src/vctl/vllm_manager.py` | Import `TmuxSession` from `vctl.tmux`; replace all `tmux_*` calls + `env_cmd` prefix pattern |
| `src/vctl/commands/lmmseval.py` | Import `TmuxSession` from `vctl.tmux`; replace `_build_env_exports` + `tmux_run_detached` |
| `tests/test_tmux.py` | **New** — unit + integration tests for `TmuxSession` |
| `tests/test_lb_manager.py` | Update `monkeypatch` targets for `TmuxSession` |
| `tests/test_vllm_manager.py` | Update `monkeypatch` targets for `TmuxSession` |
| `tests/test_commands_lmmseval.py` | Update `monkeypatch` targets for `TmuxSession` |
| `CHANGELOG.md` | Entry for this refactor |

No new runtime dependencies. No schema changes. No migration helper changes.

---

*This spec targets the next minor bump after v0.7.x. Env propagation in `LbManager` is
the only externally observable behavior change — document in CHANGELOG as a minor
improvement (haproxy sessions now correctly inherit the operator's shell environment).*
