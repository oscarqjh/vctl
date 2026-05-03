# VllmManager — tmux-backed vllm process supervisor (Phase 1 of 3) — Design Spec

**Status:** Approved — A1  
**Date:** 2026-05-03  
**Author:** design session with oscarqjh  

---

## Objective

Decouple the vllm process lifetime from the `vctl serve` caller terminal. Today `vctl serve` runs vllm as a direct child process; if the SSH session drops, the vllm process dies with it. Phase 1 replaces this with a tmux-backed supervisor: `vctl serve` starts vllm inside a detached tmux session, waits for readiness, attaches to the LB, then returns 0 — leaving vllm running independently of the caller. From any shell on the same host operators can inspect, restart, or stop the supervised process via sub-verbs (`status`, `stop`, `restart`, `attach`, `logs`). The `--foreground` flag preserves the existing v0.4.x blocking behavior for automated tests and edge cases.

**Success criteria:**

1. After `vctl serve` returns, SSH disconnect / shell hangup does not kill vllm.
2. From a fresh shell on the same host, `vctl serve status` shows accurate state.
3. `vctl serve stop` drains LB, waits idle (≤ `LB_DETACH_WAIT`), removes endpoint, kills vllm tree. After return, `vctl serve status` reports stopped.
4. `vctl serve restart` stops + starts in-place. New vllm pid differs from old.
5. `vctl serve attach` enters the live tmux session; Ctrl-B D detaches without killing vllm.
6. `vctl serve logs -n 100` prints last 100 lines; `-f` streams live.
7. Running `vctl serve` twice for the same profile exits 4 with a clear "already running" message.
8. Existing tests in `tests/test_commands_serve.py` pass (some explicitly updated for `--foreground`). New tests for status/stop/restart/attach/logs cover supervisor behavior under `VCTL_TEST_NO_SOCKET=1`.
9. `vctl serve --foreground` retains existing v0.4.x behavior: vllm is a direct child of vctl, signals trigger drain+remove+kill, vctl blocks until vllm exits.
10. `mypy --strict` + `ruff check` + `ruff format --check` pass. `pytest --cov-fail-under=50` passes.

---

## Tech Stack

- **Python 3.10+** — matches existing `target-version = py310`.
- **tmux** — already a system requirement for `LbManager`; `vctl.platform` already wraps `tmux_run_detached_argv`, `tmux_session_exists`, `tmux_kill`, `_validate_tmux_name`.
- **`psutil`** — already a runtime dependency; used in `_kill_tree` and `vctl stop`.
- **`httpx`** — already a runtime dependency; used in `_wait_for_ready` / `_wait_for_idle`.
- **`fcntl.flock`** — stdlib; used in `lb/state.py` for atomic state writes (same pattern here for pidfile).
- **No new top-level dependencies.**

---

## Architecture

`VllmManager` is a direct structural mirror of `LbManager` (`src/vctl/lb/manager.py`). The constructor takes a resolved profile object plus explicit path parameters (`state_dir`, `run_dir`) and pre-computes all file paths. Every public method (`start`, `stop`, `status`, `restart`, `attach`, `logs`) corresponds to a callable action that `commands/serve.py` invokes directly. This keeps command logic thin — one function call per verb — and makes `VllmManager` independently unit-testable without a running CLI.

vllm runs inside a tmux session named `vctl-vllm-<profile>` (validated via `_validate_tmux_name` from `vctl.platform`). After `tmux_run_detached_argv` returns, vllm is a child of the tmux server process, **not** of the vctl Python interpreter. The caller process can exit, the SSH session can disconnect, and vllm continues running exactly as it would if an operator had typed `tmux new-session -d 'vllm serve ...'` manually. This is identical to how `LbManager.start()` launches HAProxy.

Two design alternatives were evaluated and rejected: **A2** (supervisord/systemd unit) was rejected because it requires root or custom unit-file installation steps that conflict with `uv tool install` user-space deployment; **A3** (nohup + background `&`) was rejected because it provides no session attachment, no structured log piping, and no clean process grouping for `tmux send-keys C-c` shutdown signaling — all of which are free with tmux and already tested by the LbManager path.

---

## Components

### `VllmManager` — `src/vctl/vllm_manager.py`

**Responsibility:** Own the full lifecycle of a single tmux-supervised vllm process for one profile: spawn, readiness-wait, LB attachment, graceful drain+stop, status introspection, attach, log streaming.

**Interfaces:**

```python
class VllmManager:
    def __init__(
        self,
        rc: ResolvedConfig,
        state_dir: Path,
        run_dir: Path,
    ) -> None: ...

    def start(self) -> None:
        """Preflight → spawn tmux → _wait_for_ready → _do_add → write state files."""

    def stop(self) -> None:
        """_do_drain → _wait_for_idle → _do_remove → tmux send-keys C-c → poll → kill-session."""

    def restart(self) -> None:
        """stop() → reload config → start(). Logs warning if cmd snapshot differs."""

    def status(self) -> dict[str, object]:
        """Return tmux_alive, pid_alive, vllm_ready, lb_attached, started_at, log_size."""

    def attach(self) -> None:
        """os.execvp into tmux attach-session -t <name>. Does not return."""

    def logs(self, n: int = 50, follow: bool = False) -> int:
        """Tail log file. follow=True: subprocess.Popen(["tail", "-f", path])."""
```

**State files** (all under `~/.vctl/vllm/`):

| File | Content | Written by |
|------|---------|------------|
| `<profile>.pid` | vllm PID as ASCII integer; atomic via `os.replace` on a `.tmp` sibling | `start()` after tmux spawn |
| `<profile>.log` | vllm stdout+stderr via `tmux pipe-pane -o` | set up in `start()` alongside the session |
| `<profile>.cmd.json` | JSON array of the full vllm argv used at start | `start()` |
| `<profile>.host` | `socket.gethostname()` at start | `start()` |

The `run_dir` is `~/.vctl/vllm/`; `run_dir.mkdir(parents=True, exist_ok=True)` is called in `__init__`, idempotent.

**Tmux session:** `vctl-vllm-<profile>` — e.g., `vctl-vllm-qwen3_5-9b`. Profile names are already validated against a path-safe regex by `config/settings.py:resolve_profile_name` before reaching the supervisor. `_validate_tmux_name` provides a final defense-in-depth check in the constructor.

**PID discovery:** After `tmux_run_detached_argv` spawns the session, `VllmManager` polls `/proc` (via `psutil.process_iter`) for a process matching ALL of: cmdline contains `vllm`, cmdline contains `serve`, cmdline contains `--port=<rc.server.http_port>`. Matching on `--port=<port>` (not just `<model>`) is the unique discriminator — two profiles serving the same model on different ports must produce distinct matches. This mirrors `LbManager._find_haproxy_pid_by_cfg`'s use of the unique cfg path. If PID discovery times out (5 s polling), the session is killed and start fails with exit 4.

**Stale pidfile detection:** On `start()`, if a pidfile exists but `os.kill(pid, 0)` raises `ProcessLookupError`, or if `/proc/<pid>/cmdline` does not contain `vllm serve`, the pidfile is stale. `start()` unlinks all state files for that profile and continues. This is NOT performed on `stop()` or `status()` — those report the stale state rather than silently cleaning it.

**Cross-host guard:** `<profile>.host` records `socket.gethostname()` at `start()`. Any `VllmManager` operation that would mutate state (`stop`, `restart`) checks this marker; if it does not match the current host, it exits 4 with: `"refusing operation: state files belong to host <other>, current host is <self>"`. `status` and `logs` are read-only and skip the check.

---

### `commands/serve.py` — subcommand router

**Responsibility:** Parse the `vctl serve [subverb] [args]` command line and dispatch to `VllmManager` or the `--foreground` path.

**Argv parsing flow** (inside `run(ns, argv_rest) -> int`):

```python
_SUB_VERBS = {"status", "stop", "restart", "attach", "logs"}

def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
    # Peel sub-verb FIRST — before _build_subparser().parse_args(), because
    # the top-level serve parser does not know about sub-verbs and would
    # otherwise reject "vctl serve stop" with "unrecognized arguments: stop".
    if argv_rest and argv_rest[0] in _SUB_VERBS:
        sub = argv_rest[0]
        rest = argv_rest[1:]
        return {
            "status":  _cmd_status,
            "stop":    _cmd_stop,
            "restart": _cmd_restart,
            "attach":  _cmd_attach,
            "logs":    _cmd_logs,
        }[sub](ns, rest)

    # Default path: detached start (or --foreground).
    parsed = _build_subparser().parse_args(argv_rest)
    if parsed.foreground or os.environ.get("VCTL_SERVE_FOREGROUND"):
        return _run_foreground(ns, parsed)
    return _cmd_start_detached(ns, parsed)
```

The existing `_build_subparser()` gains `--foreground` and keeps `--skip-preflight`. Each sub-verb function builds its OWN argparse subparser inline (same lazy-import pattern as the rest of the CLI — no eager imports at module level). Sub-verb parsers do NOT inherit `--foreground` or `--skip-preflight`; those are only meaningful for the start path.

Concrete examples of accepted argv:

| Operator types | `argv_rest` after `cli.py` peel | Result |
|---|---|---|
| `vctl serve` | `[]` | detached start |
| `vctl serve --foreground` | `["--foreground"]` | foreground (v0.4.x compat) |
| `vctl serve --skip-preflight` | `["--skip-preflight"]` | detached start, no preflight |
| `vctl serve stop` | `["stop"]` | `_cmd_stop` |
| `vctl serve logs -n 100 -f` | `["logs", "-n", "100", "-f"]` | `_cmd_logs` |
| `vctl serve restart --skip-preflight` | `["restart", "--skip-preflight"]` | `_cmd_restart` (its own parser handles `--skip-preflight`) |

Mixing flags before sub-verb (`vctl serve --foreground stop`) is rejected — the sub-verb peel only inspects `argv_rest[0]`. If `argv_rest[0]` is a flag (`--foreground`), no sub-verb dispatch happens and the start parser handles it. If a user wants `vctl serve --skip-preflight restart`, the correct order is `vctl serve restart --skip-preflight`.

**`--foreground` compat path:** When `--foreground` is passed (or `VCTL_SERVE_FOREGROUND=1`), `run()` falls through to the original v0.4.x implementation: `subprocess.Popen` → signal handlers → `_wait_for_ready` → `_do_add` → `proc.wait()` poll loop. No `VllmManager` is instantiated. This path is the one exercised by all existing `tests/test_commands_serve.py` tests (updated to pass `--foreground` or set the env flag).

**Cross-module imports inside `vllm_manager.py`** (explicit so monkeypatching is unambiguous):

```python
# At the top of src/vctl/vllm_manager.py:
from vctl.commands.serve import _wait_for_ready, _wait_for_idle, _kill_tree
from vctl.commands.lb_scaling import _do_add, _do_drain, _do_remove
from vctl.platform import (
    tmux_run_detached_argv,
    tmux_session_exists,
    tmux_kill,
    _validate_tmux_name,
)
```

These helpers stay where they are — `_wait_for_*` and `_kill_tree` in `commands/serve.py`, `_do_*` in `commands/lb_scaling.py`. The cross-layer dependency (top-level `vctl.vllm_manager` → `vctl.commands.*`) is acceptable because `vllm_manager` is an orchestrator that legitimately consumes both serve helpers and LB-scaling primitives. mypy `--strict` allows this; there is no import cycle (commands import from vllm_manager only via the `--foreground` path, which is internal to `commands/serve.py` — `commands/serve.py` does NOT import `vctl.vllm_manager` directly; only `vctl.cli._dispatch` does, lazily). Test code monkeypatches at `vctl.vllm_manager._wait_for_ready` (the imported alias inside the module), NOT at `vctl.commands.serve._wait_for_ready`.

---

### Pidfile / log / cmd snapshot / host marker

- **`<profile>.pid`**: written atomically — write to `<profile>.pid.tmp`, then `os.replace` to `<profile>.pid`. This is the same pattern as `lb/state.py:BackendState._locked()` (write + `os.replace`) adapted for a single integer file that does not need `fcntl.flock` because only one `VllmManager` instance per profile can be active at a time (enforced by the tmux-session double-start guard).
- **`<profile>.log`**: set up via `tmux pipe-pane -o 'cat >> <path>'` after `tmux_run_detached_argv` spawns the session. All vllm stdout/stderr is captured there. The caller process inherits none of it.
- **`<profile>.cmd.json`**: `json.dumps(argv_list)` — the exact list passed to `tmux_run_detached_argv`. Used by `restart()` to detect config drift between the running instance and what a fresh `resolve()` would produce, and logged as a warning if they differ.
- **`<profile>.host`**: `socket.gethostname()` as a plain text string.

---

### Tmux session

The session name `vctl-vllm-<profile>` is passed to `tmux_run_detached_argv` (from `vctl.platform`), which calls `tmux new-session -d -s <name> <cmd>`. The command string is produced by `shlex.join(vllm_argv)` inside `tmux_run_detached_argv`. Log piping is set up with a second `subprocess.run(["tmux", "pipe-pane", "-t", name, "-o", f"cat >> {log_path}"])` call immediately after spawn.

Session lifecycle:

- **Created** by `start()`.
- **Graceful stop** via `tmux send-keys -t <name> C-c ""` then polling pidfile for process exit up to `VCTL_KILL_GRACE` seconds (default 30 s).
- **Force kill** via `tmux kill-session -t <name>` if the process has not exited after the grace period. This is identical to `LbManager.stop()` calling `tmux_kill`.
- **Attach** via `os.execvp("tmux", ["tmux", "attach-session", "-t", name])` — replaces the vctl process entirely; Ctrl-B D detaches the operator without sending signals to the session.

---

### `--foreground` compat path

The original `run()` body in `commands/serve.py` is extracted verbatim into a private function `_run_foreground(ns, parsed)` and called when `--foreground` is present. The outer `run()` dispatches to it before reaching the supervisor path. All existing test monkeypatches (`monkeypatch.setattr(lb_scaling, "_client", ...)`, `monkeypatch.setattr(serve, "_wait_for_ready", ...)`) continue to work because they target module-level names, which `_run_foreground` still uses.

---

## Data Flow

### `vctl serve` (detached start)

```
run(ns, argv_rest)
  ├── parse: no sub-verb, no --foreground
  ├── preflight (unless --skip-preflight)
  ├── resolve(ns.config, profile=ns.profile)       → ResolvedConfig
  ├── pool_for_model(rc.lb, rc.model.name)          → fail-fast pool routing (exit 3 on miss)
  ├── vm = VllmManager(rc, state_dir, run_dir)
  │     └── __init__: _validate_tmux_name, mkdir run_dir
  └── vm.start()
        ├── cross-host guard: <profile>.host check (skip if no file)
        ├── stale pidfile cleanup (if pid not alive or cmdline mismatch)
        ├── tmux_session_exists("vctl-vllm-<profile>") → exit 4 if alive
        ├── build vllm argv (identical to foreground path)
        ├── tmux_run_detached_argv("vctl-vllm-<profile>", vllm_argv)
        ├── tmux pipe-pane → <profile>.log
        ├── poll psutil for vllm PID → write <profile>.pid (exit 4 if timeout)
        ├── write <profile>.cmd.json, <profile>.host
        ├── _wait_for_ready(rc.server.http_port, timeout)  [exit 4 on TimeoutError → cleanup]
        └── _do_add(ep, mgr, bs, pool_name=pool.name)      [exit per error map → cleanup]
  → return 0 (caller terminal can disconnect; vllm lives in tmux)
```

Cleanup on failure inside `start()`: kill tmux session, unlink all four state files.

### `vctl serve stop`

```
_cmd_stop(ns, argv_rest)
  ├── resolve(ns.config, profile=ns.profile)       → ResolvedConfig
  ├── vm = VllmManager(rc, state_dir, run_dir)
  └── vm.stop()
        ├── cross-host guard: read <profile>.host, compare to socket.gethostname()
        ├── read ep from <profile>.pid + detect_self_ip() + rc.server.http_port
        ├── _do_drain(ep, mgr, pool_name=pool.name)
        ├── _wait_for_idle(rc.server.http_port,
        │     timeout=float(os.environ.get("LB_DETACH_WAIT", "600")))
        ├── _do_remove(ep, mgr, bs, pool_name=pool.name)
        ├── subprocess.run(["tmux", "send-keys", "-t", session, "C-c", ""])
        ├── poll os.kill(pid, 0) up to VCTL_KILL_GRACE seconds
        ├── tmux_kill(session) if session still exists
        └── unlink <profile>.pid, <profile>.host, <profile>.cmd.json
            (leave <profile>.log for post-mortem)
  → return 0 on success; return 1 on drain/remove error (errors printed to stderr)
```

### `vctl serve restart`

```
_cmd_restart(ns, argv_rest)
  ├── resolve(ns.config, profile=ns.profile)       → ResolvedConfig
  ├── vm = VllmManager(rc, state_dir, run_dir)
  └── vm.restart()
        ├── read <profile>.cmd.json → old_argv
        ├── compute new_argv from fresh rc
        ├── if old_argv != new_argv: _LOG.warning("config changed: ...")
        ├── vm.stop()   (full drain+kill sequence)
        └── vm.start()  (full spawn+ready+attach sequence)
  → return 0 on success
```

---

## Error Handling

| Condition | Where detected | Exit code | Cleanup |
|-----------|---------------|-----------|---------|
| Tmux session already exists for profile | `start()` → `tmux_session_exists` | 4 | none (process still running) |
| Pidfile from another host | `stop()` / `restart()` → host marker check | 4 | none |
| vllm doesn't become ready in `VLLM_ENGINE_READY_TIMEOUT_S` | `start()` → `_wait_for_ready` raises `TimeoutError` | 4 | kill tmux session + unlink all state files |
| LB attach fails after vllm ready | `start()` → `_do_add` returns non-zero | per `_exit_for` mapping (4 = LbUnreachable, 3 = PoolNotFound, 1 = generic) | kill tmux session + unlink all state files |
| No pool serves this model | `run()` → `pool_for_model` | 3 | none (vllm never spawned) |
| Stale pidfile (pid not alive / wrong cmdline) | `start()` on entry | none (silent cleanup) | unlink stale state files, continue |
| Pool routing fails during `stop()` (LB unreachable) | `_do_drain` / `_do_remove` return non-zero | 1 | stop still kills vllm tree; errors printed to stderr |
| `attach()` called when no session exists | `attach()` → `tmux_session_exists` check | 4 with message | none |
| `logs()` called when log file missing | `logs()` → path check | 1 with message | none |
| tmux not installed | any tmux call in `vctl.platform` | `RuntimeError` propagated | none |
| Missing/invalid `cluster.yaml` or profile YAML | upstream of `VllmManager` (in `cli.main` → `_missing_path_is_config`) | 2 | handled before `VllmManager` is ever instantiated; nothing for the supervisor to clean up |

All cleanup in `start()` failures is guarded with `contextlib.suppress(OSError)` for the unlink calls, mirroring `LbManager.stop()`.

Exit code mapping is consistent with existing conventions: 2 = config error, 3 = routing failure, 4 = environment/runtime error, 1 = generic failure, 130 = signal.

---

## Testing Strategy

### Unit tests — `tests/test_vllm_manager.py` (new)

All tests run under `VCTL_TEST_NO_SOCKET=1`. Tmux calls are intercepted via `monkeypatch.setattr` on `vctl.platform.tmux_run_detached_argv`, `vctl.platform.tmux_session_exists`, and `vctl.platform.tmux_kill`. `subprocess.run` calls for `tmux pipe-pane` and `tmux send-keys` are patched at the `vctl.vllm_manager` module level. `_wait_for_ready` and `_do_add` / `_do_drain` / `_do_remove` are patched at `vctl.vllm_manager` to avoid real HTTP and socket calls.

Coverage targets: `VllmManager.__init__`, `start` (happy path + each failure mode), `stop` (happy path + host guard + missing session), `status` (all combinations of tmux/pid/vllm_ready flags), `restart` (config-drift warning branch + no-drift branch), `attach` (session missing path), `logs` (with and without `-f`).

### Updates to `tests/test_commands_serve.py`

Existing tests that call `run(ns, argv_rest)` with `argv_rest=[]` must add `"--foreground"` (or set `VCTL_SERVE_FOREGROUND=1`) so they exercise the `_run_foreground` path unchanged. The monkeypatches on `serve._wait_for_ready`, `lb_scaling._client`, etc. remain valid because `_run_foreground` still references those same module-level names.

New test cases added to this file:

- `test_detached_start_calls_vllm_manager_start` — asserts `VllmManager.start` is called when no `--foreground` flag.
- `test_detached_start_exits_4_on_already_running` — `tmux_session_exists` returns True → exit 4.
- `test_serve_status_subverb` — `status` sub-verb dispatches to `_cmd_status`.
- `test_serve_stop_subverb` — `stop` sub-verb dispatches to `_cmd_stop`.

### Integration tests — `@pytest.mark.vllm_supervisor_integration`

New marker declared in `pyproject.toml` under `[tool.pytest.ini_options] markers`. Concrete declaration:

```toml
[tool.pytest.ini_options]
addopts = "-ra -q"
testpaths = ["tests"]
markers = [
    "integration: requires a real haproxy binary on PATH",
    "vllm_supervisor_integration: requires a real tmux binary and a vllm stub HTTP server",
]
```

Default `addopts` are NOT modified — integration tests are not auto-excluded from `pytest`. Operators run the integration suite explicitly via `pytest -m vllm_supervisor_integration`. CI runs the unit suite via `pytest -m "not integration and not vllm_supervisor_integration"` (existing pattern). Tests in `tests/test_vllm_manager_integration.py` require a real `tmux` binary and a real vllm stub (a minimal HTTP server that responds to `/v1/models`). The integration suite covers acceptance tests 1, 3, 4, 5, 6 end-to-end (AT-1 specifically requires a real process tree to test SIGHUP behavior — see Acceptance Tests for unit/integration labels).

---

## Boundaries

### Always do

- Mirror `LbManager` constructor shape exactly: `__init__(self, rc, state_dir, run_dir)`.
- Validate the tmux session name via `_validate_tmux_name` in `__init__` before any tmux call.
- Write all state files atomically (write `.tmp`, then `os.replace`).
- Pipe vllm output to the log file via `tmux pipe-pane`; never inherit it to the caller's stdout/stderr.
- Log vctl's own actions to `stderr` of the caller process (via `_LOG` / `print(..., file=sys.stderr)`).
- Clean up all four state files + tmux session on any `start()` failure after the session was created.
- Check the host marker before any mutating operation (`stop`, `restart`).
- Keep `--foreground` behavior identical to the current v0.4.x implementation; run all existing serve tests against it.
- Declare the new `vllm_supervisor_integration` marker in `pyproject.toml` before writing any integration test that uses it.

### Ask first

- Moving `_wait_for_ready`, `_wait_for_idle`, or `_kill_tree` to a shared module (e.g., `vctl/commands/_serve_helpers.py`) if it would break the existing `from vctl.commands.serve import _kill_tree` import in `commands/stop.py`.
- Changing the tmux session naming scheme (e.g., adding a hostname suffix) if cross-host name collisions on a shared tmux server become a concern.
- Adding log rotation or a log-size cap; out of scope for Phase 1 but straightforward to add.

### Never do

- Remove or rename `_wait_for_ready`, `_wait_for_idle`, `_kill_tree` from `commands/serve.py` without updating `commands/stop.py` which imports them directly (`from vctl.commands.serve import _kill_tree, _wait_for_idle`).
- Use `tmux send-keys C-c` as the primary stop mechanism without the polling fallback + `tmux kill-session` escalation — C-c is not guaranteed to reach vllm if the session is in a weird state.
- Skip the cross-host guard — operating on another host's state files would corrupt the LB state.
- Invoke real tmux or real HAProxy sockets in unit tests; all socket/tmux calls must be monkeypatched.
- Add new entries to `_COMMANDS` in `cli.py` for the sub-verbs (`status`, `stop`, etc.) — they are dispatched inside `commands/serve.py::run()`, not at the top-level CLI dispatch table.
- Implement cross-host control, worker reaper, rolling restart, or auto-revive — all Phase 2/3.

---

## Acceptance Tests

### AT-1: Detached start survives caller exit  *(integration)*

**Given** a vllm stub listening on the configured port, a valid `cluster.yaml` and profile, and no existing tmux session for the profile.  
**When** `vctl serve --profile <p>` returns 0 and the calling shell terminates (test spawns `vctl serve` as a subprocess in its own session via `subprocess.Popen(start_new_session=True)`, waits for return, sends SIGHUP to that session leader, then waits for it to exit).  
**Then** `tmux_session_exists("vctl-vllm-<p>")` returns True and the PID in `~/.vctl/vllm/<p>.pid` is still alive after the SIGHUP.

### AT-2: Status reports accurate state from a fresh shell  *(unit)*

**Given** vllm running in tmux (started via `VllmManager.start()`), with the LB attached.  
**When** `VllmManager.status()` is called (or `vctl serve status` via CLI).  
**Then** the returned dict has `tmux_alive=True`, `pid_alive=True`, `vllm_ready=True`, `lb_attached=True`, and `started_at` is a non-empty ISO-format string.

### AT-3: Stop drains, removes, kills; status reports stopped  *(integration)*

**Given** vllm running and LB-attached.  
**When** `VllmManager.stop()` returns 0.  
**Then** (a) the endpoint is absent from all LB pool state files, (b) `tmux_session_exists("vctl-vllm-<p>")` is False, (c) the PID is no longer alive, and (d) a subsequent `VllmManager.status()` returns `tmux_alive=False`, `pid_alive=False`, `lb_attached=False`.

### AT-4: Restart produces a new PID  *(integration)*

**Given** vllm running with pid `P1`.  
**When** `VllmManager.restart()` returns 0.  
**Then** `~/.vctl/vllm/<p>.pid` contains a PID `P2 != P1`, `P2` is alive, and the LB endpoint is re-attached.

### AT-5: Attach enters session; detach leaves vllm running  *(unit)*

**Given** vllm running in tmux session `vctl-vllm-<p>`.  
**When** `VllmManager.attach()` is called (in a test, intercepted before `os.execvp` by monkeypatching `os.execvp`).  
**Then** `os.execvp` is called with args `("tmux", ["tmux", "attach-session", "-t", "vctl-vllm-<p>"])` and the vllm process remains alive (not killed by the attach call).

### AT-6: Logs tail and stream  *(unit)*

**Given** `~/.vctl/vllm/<p>.log` contains at least 200 lines.  
**When** `VllmManager.logs(n=100)` is called.  
**Then** exactly 100 lines are printed to stdout. When `VllmManager.logs(follow=True)` is called, `subprocess.Popen` is invoked with `["tail", "-f", <log_path>]` and the method blocks until the child exits (simulated by sending SIGINT to the tail process in the test).

### AT-7 (negative): Double-start exits 4 with clear message  *(unit)*

**Given** tmux session `vctl-vllm-<p>` already exists (simulated by `tmux_session_exists` monkeypatched to return True).  
**When** `VllmManager.start()` is called (or `vctl serve --profile <p>`).  
**Then** a `RuntimeError` is raised (mapped to exit 4 in the CLI) with message text containing `"already running"` and `"vctl serve restart"`.

### AT-8: Existing `--foreground` tests pass unchanged  *(unit)*

**Given** the existing test suite in `tests/test_commands_serve.py` with `--foreground` added to each `argv_rest` invocation.  
**When** `pytest tests/test_commands_serve.py` is run.  
**Then** all tests that passed before Phase 1 still pass. No new failures introduced by the subcommand router or `_run_foreground` extraction.

### AT-9: `--foreground` blocks and drains on signal  *(unit)*

**Given** `vctl serve --foreground --profile <p>` is running with vllm alive and LB-attached.  
**When** `SIGTERM` is delivered to the vctl process.  
**Then** `_do_drain` is called, `_wait_for_idle` is called, `_do_remove` is called, `_kill_tree` is called, and the process exits 130 — identical to v0.4.x behavior.

### AT-10: CI quality gates pass  *(meta)*

**Given** the Phase 1 implementation is complete.  
**When** the CI matrix steps are run locally: `ruff check .`, `ruff format --check .`, `mypy --strict src/vctl`, `pytest -q --cov=vctl --cov-report=term-missing --cov-fail-under=50`.  
**Then** all four commands exit 0 with no errors or warnings introduced by the new code.
