# vctl rolling-restart — Phase 3 — Design Spec

## Objective

Phase 3 adds `vctl rolling-restart`, a sequential one-ep-at-a-time restart command
for a named pool in the vLLM fleet. It ssh-es into each worker, calls `vctl serve
restart`, waits until HAProxy reports that endpoint `UP` again, then moves to the
next. The command is idempotent: if interrupted or a worker fails, a per-pool session
file records exactly which endpoints are `completed`, `failed`, and `pending`. A
subsequent invocation auto-resumes from that file, verifying the failed endpoint's
health via `_fetch_haproxy_stats` before prompting the operator for next action. On
full success the session file is deleted. The feature targets the common cluster
maintenance pattern — model checkpoint swap, image update, config change — where
every worker must bounce without completely draining the pool.

### Success Criteria

1. Fresh run (no session file): writes session file with all eps in `pending`, sets
   `in_progress: true`, restarts each ep sequentially, deletes session file on full
   success.
2. `vctl serve restart` ssh failure on ep N: halts with non-zero exit; session file
   shows `completed[1..N-1]`, `failed=[N]`, `pending=[N+1..M]`.
3. Resume after failure: detects session file, verifies failed ep is `UP` via HAProxy
   stats (`_fetch_haproxy_stats`), logs "verified: ep was fixed externally", moves ep
   to `completed`, and continues with `pending`.
4. Resume with failed ep still DOWN/MAINT/etc.: prompts operator for (a) skip + mark
   completed, (b) re-attempt restart, (c) abort; exits cleanly if operator picks abort.
5. `--fresh` deletes any existing session file and forces a fresh run from all eps.
6. `--status` prints session file contents (or "no session in progress") and exits 0
   without making any changes.
7. `--abort` deletes session file (if present) and exits 0 without any other action.
8. `--dry-run` prints what-would-happen for each ep without ssh-ing or modifying
   anything (session file not written or deleted).
9. Concurrent `vctl rolling-restart --pool X` while `in_progress: true` in session
   file for pool X exits 4 with message `rolling-restart already in progress for pool
   X — kill the other invocation or use --abort`.
10. `mypy --strict`, `ruff check .`, `ruff format --check .`, and
    `pytest --cov=vctl --cov-fail-under=50` all pass after this change.

---

## Tech Stack

- Python 3.10+ (`from __future__ import annotations` everywhere).
- `subprocess.run(..., timeout=...)` for ssh — no paramiko, no new runtime dependency.
- `json` + `os.replace` for atomic session file read/write.
- `_fetch_haproxy_stats` from `vctl.commands.lb` (re-imported at use site in the new
  module so monkeypatching at `vctl.commands.rolling_restart._fetch_haproxy_stats`
  works cleanly in tests — same pattern as `vctl.lb.prune._fetch_haproxy_stats`).
- `BackendState.list()` from `vctl.lb.state` for pool endpoint enumeration.
- `LbManager` from `vctl.lb.manager` for admin socket access and pool config.
- `lb_admin_client` from `vctl.lb.runtime` for fresh-per-call HAProxy admin queries.
- `rich` for streamed per-ep progress output (already a runtime dependency); falls
  back gracefully to plain print when not connected to a terminal.
- Standard library `fcntl.flock` via a dedicated `_SessionFile` helper for the
  session file — no new locking libraries.

---

## Architecture

`vctl rolling-restart` is a sequential ssh-loop that reuses two existing Phase 1/2
primitives. The restart step invokes `vctl serve restart` on each remote worker via
ssh (`subprocess.run(["ssh", ..., "vctl serve restart"])`, batch mode, 5 s connect
timeout), exactly mirroring the single-ep atomic restart path that `VllmManager.restart`
takes locally: stop → reload config → start, LB detach included. The health-check step
calls `_fetch_haproxy_stats` (introduced in Phase 2 and already re-exported by
`lb/prune.py`) via a fresh `lb_admin_client` to poll the HAProxy `status` field for the
restarted ep until it reads `UP` or the `--ready-timeout` deadline expires. This keeps
health verification consistent with `vctl lb prune`'s eligibility check and
`vctl lb status`'s live-status display.

Resumability is the key design primitive: every state transition writes the session
file atomically (`.tmp` + `os.replace`) so a mid-run SIGKILL leaves the file
consistent. The session file lives at `~/.vctl/lb/rolling-restart/<pool>.json` with a
sibling `<pool>.lock` for `fcntl.flock`-based mutual exclusion. The concurrency guard
reads `in_progress` at startup and refuses with exit 4 if it is `true`, allowing
different pools to run in parallel (separate lock files) while preventing double-start
within the same pool. Flag interactions are clear by design: `--fresh` always precedes
the run loop, `--status` / `--abort` are inspection/cleanup-only exits with no session
mutation side-effects beyond what they advertise.

The command integrates cleanly into `cli.py`: it is added to `_COMMANDS` as
`"rolling-restart": "vctl.commands.rolling_restart"`. It does NOT join
`_PROFILE_AWARE` because it operates on a pool name, not a model profile. The
`--pool` flag is parsed inside `commands/rolling_restart.py`'s own subparser; the
top-level `--config` and `--profile` are available on `ns` but `--profile` is not
required and is silently unused (pool resolution goes through `LbManager` directly).

---

## Components

### 1. New top-level command `vctl rolling-restart` — `src/vctl/commands/rolling_restart.py`

Entry point `run(ns: argparse.Namespace, argv_rest: list[str]) -> int`. Builds its own
argparse parser inside `run()`, following the lazy-import contract in `CLAUDE.md`. The
argparse parser is `argparse.ArgumentParser(prog="vctl rolling-restart")`.

**Flags:**

| Flag | Type | Default | Description |
|---|---|---|---|
| `--pool NAME` | str, required | — | Target pool name. REQUIRED in v1; no multi-pool default. |
| `--fresh` | store_true | False | Delete existing session file before starting; force fresh run. |
| `--status` | store_true | False | Print session file (or "no session in progress"); exit 0. |
| `--abort` | store_true | False | Delete session file if present; exit 0. |
| `--dry-run` | store_true | False | Print what would happen without ssh-ing; no session file written. |
| `--ready-timeout` | int | 60 | Seconds to wait for HAProxy `UP` after ssh returns 0. |
| `--vllm-timeout` | int | 600 | Seconds vctl-serve-restart is allowed to take on the remote (ssh subprocess timeout). |
| `--quiet` | store_true | False | Suppress per-ep progress lines; print only final summary. |
| `--ssh-user` | str | `""` | Override ssh username. Default: use ssh config / key (no explicit -l flag). |

`--ready-timeout` and `--vllm-timeout` are two separate flags. `--ready-timeout`
governs the post-ssh HAProxy polling loop; `--vllm-timeout` governs the ssh subprocess
`timeout=` argument (covers the time vllm needs to load the model). Keeping them
separate makes each tunable independently: model load is typically 600 s while HAProxy
transitions `UP` within seconds of a healthy HTTP probe.

**Mutual exclusivity:** `--fresh`, `--status`, `--abort`, `--dry-run` are mutually
exclusive with each other (validated in `run()` before any I/O).

### 2. Session file format and atomic helpers — `_SessionFile` class

Defined inside `commands/rolling_restart.py` (not a separate module — it is small
enough). All reads and writes go through `_SessionFile`, which mirrors
`BackendState._locked()` by holding a `fcntl.flock` exclusive lock on a sibling
`<pool>.lock` file.

**Path:** `~/.vctl/lb/rolling-restart/<pool>.json`
**Lock:** `~/.vctl/lb/rolling-restart/<pool>.lock`

```python
class _SessionFile:
    def __init__(self, pool: str) -> None: ...
    def exists(self) -> bool: ...
    def read(self) -> dict[str, object]: ...
    def write(self, data: dict[str, object]) -> None: ...  # atomic via .tmp + os.replace
    def delete(self) -> None: ...
```

**JSON schema:**

```json
{
  "pool": "qwen3-5-9b",
  "started_at": "2026-05-04T15:00:00Z",
  "completed": ["10.0.0.1:8000", "10.0.0.2:8000"],
  "failed": ["10.0.0.3:8000"],
  "pending": ["10.0.0.4:8000", "10.0.0.5:8000"],
  "in_progress": true
}
```

- `started_at`: ISO 8601 UTC timestamp at first write (fresh run). Preserved on resume.
- `completed`: eps that have been restarted and verified `UP`. Only appended.
- `failed`: eps where ssh or health-check failed; at most one ep at halt time.
- `pending`: eps not yet started. Shrinks as the run progresses.
- `in_progress`: `true` while the run loop is active; set to `false` on a clean halt
  (failure) so a subsequent invocation can resume. NOTE: a SIGKILL or crash leaves
  `in_progress: true`; the `--abort` flag clears it.

**`_write_session_atomic(path, data)`** writes to `<path>.tmp` then calls `os.replace`.
This preserves the invariant that the session file is either absent or contains a valid
complete JSON document — no partial writes are ever visible to another invocation.

### 3. `_restart_one_ep(ep, idx, total, pool_name, session, ssh_user, vllm_timeout, ready_timeout, dry_run, quiet)` — per-ep helper

Returns `"ok" | "failed"`. Performs the following steps for a single endpoint:

1. **Print progress prefix** to stderr (unless `--quiet`):
   `[{idx}/{total}] {ep}  draining → restarting...`
2. **If `--dry-run`**: print `[{idx}/{total}] {ep}  would restart` and return `"ok"`
   immediately (no ssh, no state mutation).
3. **Build ssh argv** — `["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
   "-o", "StrictHostKeyChecking=accept-new", <target>, <remote_cmd>]` where
   `<target>` is `{ssh_user}@{ep_host}` if `ssh_user` is non-empty, else `{ep_host}`.
   `ep_host` is extracted from `ep` by splitting on `:` and taking the first component.

   **`<remote_cmd>` construction** — non-interactive ssh shells do NOT source
   `~/.bashrc` / `~/.zshrc`, so `vctl` (installed via `uv tool install` at
   `~/.local/bin/vctl`) may not be on PATH. Use a login shell to ensure profile
   loading:

   ```python
   remote_cmd = "bash -lc 'vctl serve restart'"
   # bash -l → login shell → sources ~/.bash_profile or ~/.profile (which uv tool
   # installer adds itself to). zsh equivalent: 'zsh -lc'.
   ```

   Add `--remote-vctl-path PATH` flag (default `None`). If set, override the
   above with `f"{remote_vctl_path} serve restart"` — bypasses login shell entirely
   for operators with non-standard installs (e.g. system-wide `/opt/vctl/bin/vctl`).
4. **Run ssh** via `subprocess.run(argv, timeout=vllm_timeout, capture_output=True,
   text=True)`.
5. **On non-zero returncode or `subprocess.TimeoutExpired`**: return `"failed"` (caller
   logs and halts).
6. **Poll HAProxy for UP**: call `_verify_ep_up(ep, pool_name, mgr, ready_timeout)`.
   If it returns `False`, return `"failed"`.
7. **If all succeeded**: return `"ok"`.

### 4. `_verify_ep_up(ep, pool_name, mgr, timeout_s)` — health check helper

Polls `_fetch_haproxy_stats` in a tight loop until the HAProxy `status` field for `ep`
in `pool_{pool_name}` starts with `"UP"`, or `timeout_s` elapses.

```python
def _verify_ep_up(
    ep: str,
    pool_name: str,
    mgr: LbManager,
    timeout_s: int,
) -> bool:
    ...
```

**Algorithm:**

```
deadline = time.monotonic() + timeout_s
while time.monotonic() < deadline:
    cli = lb_admin_client(mgr)
    if cli is None:
        time.sleep(2); continue  # LB temporarily unreachable — keep trying
    stats = _fetch_haproxy_stats(cli)
    pool_section = f"pool_{pool_name}"
    for srv_data in stats.get(pool_section, {}).values():
        if srv_data.get("ep") == ep:
            status = str(srv_data.get("status", ""))
            if status.startswith("UP"):
                return True
            break
    time.sleep(2)
return False
```

Each iteration opens a fresh `lb_admin_client` (per-command socket contract from
`CLAUDE.md`). The 2 s sleep between polls matches `vctl lb wait-ready`'s cadence.
Returning `False` after timeout causes the caller to mark the ep as `failed` and halt.

### 5. `_handle_resume(session_data, session_file, mgr, ssh_user, vllm_timeout, ready_timeout, dry_run, quiet)` — resume logic

Called when a session file is found at startup (and `--fresh` was NOT passed).

Returns `(failed_eps_to_retry, pending_eps)` after resolving the `failed` list. If
`failed` is empty, returns `([], session_data["pending"])` immediately.

For each ep in `failed`:

1. **Check HAProxy status** via `_verify_ep_up(ep, pool_name, mgr, timeout_s=5)` with
   a short 5 s window (one stat read, not a long poll — just a snapshot).
   - `UP`: log `"verified: {ep} was fixed externally — moving to completed"`, move ep
     from `failed` to `completed`, persist session file.
   - DOWN/MAINT/other: prompt interactively (unless `--dry-run`, in which case print
     and skip):
     ```
     ep 10.0.0.3:8000 is still DOWN. Choose:
       (a) skip — mark as completed and continue
       (b) retry — re-attempt restart
       (c) abort — exit now (session file preserved)
     ```
     Read from `sys.stdin` (one character). On `a`: move to `completed`, persist. On
     `b`: add to `to_retry` list. On `c`: print `"Aborted. Session file preserved."`,
     exit 1.

Returns `(to_retry, remaining_pending)`. The caller prepends `to_retry` to
`remaining_pending` and processes them in order, so retried eps are handled before
truly pending ones.

---

## Data Flow

### Fresh run sequence

```
vctl rolling-restart --pool qwen3-5-9b
  │
  ├─ Parse args; validate --pool exists in mgr.lb.pools (exit 3 if not)
  ├─ _SessionFile.exists() → False
  ├─ Load eps = BackendState(state_dir, lb_host, pool="qwen3-5-9b").list()
  │    exit 0 with "no backends" warning if list is empty
  ├─ Build initial session data:
  │    {pool, started_at (now UTC), completed=[], failed=[], pending=eps, in_progress=True}
  ├─ _SessionFile.write(data)    ← atomic
  │
  ├─ for each ep in pending (in list order):
  │    ├─ move ep from pending → in-flight (not persisted; in_progress already True)
  │    ├─ _restart_one_ep(ep, ...) → "ok" | "failed"
  │    │    ├─ ssh to ep_host: vctl serve restart
  │    │    │    └─ on failure → return "failed"
  │    │    └─ _verify_ep_up(ep, pool, mgr, ready_timeout)
  │    │         └─ poll _fetch_haproxy_stats until UP or timeout → return True/False
  │    │
  │    ├─ if "ok":
  │    │    session.completed.append(ep); session.pending.remove(ep)
  │    │    _SessionFile.write(session)   ← atomic checkpoint after EACH ep
  │    │
  │    └─ if "failed":
  │         session.failed.append(ep); session.pending.remove(ep)
  │         session.in_progress = False
  │         _SessionFile.write(session)
  │         print stderr: "HALTING. Run --resume after fixing."
  │         return 1                      ← non-zero exit
  │
  ├─ All eps succeeded → session.in_progress = False
  │   _SessionFile.delete()
  └─ return 0
```

### Resume sequence

```
vctl rolling-restart --pool qwen3-5-9b   (session file present)
  │
  ├─ _SessionFile.read() → session_data
  ├─ in_progress == True → exit 4 (concurrency guard)
  │   in_progress == False → proceed
  │
  ├─ set in_progress = True; _SessionFile.write(session)
  │
  ├─ _handle_resume(session, ...)
  │    ├─ for each ep in failed:
  │    │    probe via _fetch_haproxy_stats (5 s window)
  │    │    UP  → move to completed, log
  │    │    DOWN → prompt (a/b/c)
  │    └─ return (to_retry, remaining_pending)
  │
  ├─ work_queue = to_retry + remaining_pending
  │
  └─ run restart loop on work_queue  (same as fresh run loop above)
        → on full success: _SessionFile.delete(); return 0
        → on failure: write session, in_progress=False, return 1
```

### Single-ep restart sequence

```
_restart_one_ep(ep="10.0.0.3:8000", pool="qwen3-5-9b", ...)
  │
  ├─ ep_host = "10.0.0.3"
  ├─ argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
  │           "-o", "StrictHostKeyChecking=accept-new",
  │           "10.0.0.3", "vctl serve restart"]
  │
  ├─ subprocess.run(argv, timeout=vllm_timeout, capture_output=True, text=True)
  │    returncode != 0 or TimeoutExpired → return "failed"
  │
  ├─ print "waiting for UP..."   (unless --quiet)
  │
  └─ _verify_ep_up("10.0.0.3:8000", "qwen3-5-9b", mgr, ready_timeout)
       poll every 2 s up to ready_timeout
       status "UP*" → return True  → return "ok"
       timeout      → return False → return "failed"
```

---

## Error Handling

| Condition | Exit code | Message |
|---|---|---|
| `--pool` not found in `mgr.lb.pools` | 3 | `unknown pool: '<name>'; available: <list>` |
| Session file `in_progress: true` at startup | 4 | `rolling-restart already in progress for pool <X> — kill the other invocation or use --abort` |
| ssh non-zero exit | 1 | `[N/M] <ep>  ssh failed: <stderr snippet>. HALTING.` |
| ssh timeout (`subprocess.TimeoutExpired`) | 1 | `[N/M] <ep>  timed out after <vllm-timeout>s. HALTING.` |
| `_verify_ep_up` timeout (ready-timeout expired) | 1 | `[N/M] <ep>  did not become UP within <ready-timeout>s. HALTING.` |
| LB admin socket unreachable during health poll | non-fatal retry | Logged at DEBUG; keeps polling until ready-timeout |
| Resume prompt: operator picks abort | 1 | `Aborted. Session file preserved at <path>.` |
| `--abort` flag: session file absent | 0 | `no session file for pool <X>` (informational) |
| `--status` flag: session file absent | 0 | `no session in progress for pool <X>` |
| HAProxy not reachable at all (cli is None for every poll iteration) | 1 | Surfaced by `_verify_ep_up` returning False after timeout |
| `BackendState.list()` returns empty | 0 | `pool <X> has no registered backends; nothing to restart` |
| Conflicting flags (`--fresh` + `--status`) | 2 | `error: --fresh, --status, --abort, and --dry-run are mutually exclusive` |

All progress and error messages go to `sys.stderr`. `--quiet` suppresses per-ep
progress lines but does NOT suppress error messages or the final summary. `--dry-run`
writes nothing to the session file and prints all actions as `would restart` prefixed
lines.

**Session file integrity:** if `_SessionFile.read()` raises `json.JSONDecodeError`
(e.g. a previous crash mid-write left a `.tmp` file but the original was not replaced),
the command prints `"corrupted session file at <path>; use --abort to clear it"` and
exits 1. This is conservative — never silently clobber a session file that might contain
valid state information (even though the atomic write pattern makes corruption unlikely).

---

## Testing Strategy

### New test file: `tests/test_commands_rolling_restart.py`

Follows the same patterns as `tests/test_commands_lb_prune.py`:
- Unit tests use `monkeypatch` to stub `subprocess.run`, `_fetch_haproxy_stats`, and
  `lb_admin_client`; no real ssh or HAProxy required.
- Integration tests (requiring real ssh to localhost) are marked
  `@pytest.mark.integration` and are skipped in the default CI run.

**Correct monkeypatch targets:**

```python
# Stub subprocess.run (covers both ssh and any internal subprocesses):
monkeypatch.setattr("vctl.commands.rolling_restart.subprocess.run", fake_run)

# Stub _fetch_haproxy_stats at its import site inside rolling_restart:
monkeypatch.setattr("vctl.commands.rolling_restart._fetch_haproxy_stats", fake_stats)

# Stub lb_admin_client at its import site inside rolling_restart:
monkeypatch.setattr("vctl.commands.rolling_restart.lb_admin_client", fake_client_fn)
```

The module-level import pattern (import at top of `commands/rolling_restart.py`, not
inside the function) makes these patches straightforward and mirrors the established
pattern in `vctl.lb.prune`.

**Key test cases:**

| Test | Asserts |
|---|---|
| `test_fresh_run_all_success` | Session file created then deleted; `subprocess.run` called N times; exit 0. |
| `test_fresh_run_ssh_failure_on_ep2` | Session file shows `completed=["10.0.0.1:8000"]`, `failed=["10.0.0.2:8000"]`, `pending=["10.0.0.3:8000"]`; exit 1. |
| `test_fresh_run_verify_timeout` | ssh returns 0 but `_fetch_haproxy_stats` never returns UP; exit 1; session file written. |
| `test_resume_failed_ep_now_up` | Session file with `failed=["ep"]`; stats return UP → ep moved to completed; run continues; exit 0. |
| `test_resume_failed_ep_still_down_skip` | Stats return DOWN; interactive prompt returns `a`; ep marked completed; run continues. |
| `test_resume_failed_ep_still_down_retry` | Stats return DOWN; prompt returns `b`; ep retried (ssh called again). |
| `test_resume_failed_ep_still_down_abort` | Stats return DOWN; prompt returns `c`; exit 1; session file preserved. |
| `test_fresh_flag_deletes_existing_session` | Pre-existing session file present; `--fresh` deletes it; new run starts from all eps. |
| `test_status_flag_no_session` | No session file; `--status` prints "no session in progress"; exit 0; nothing written. |
| `test_status_flag_with_session` | Session file present; `--status` prints its content; exit 0; file unchanged. |
| `test_abort_flag_clears_session` | Session file present; `--abort` deletes it; exit 0. |
| `test_abort_flag_no_session` | No session file; `--abort` exits 0 with informational message. |
| `test_dry_run_no_ssh_no_file` | `--dry-run`; `subprocess.run` never called; session file never written; exit 0. |
| `test_concurrency_guard_in_progress` | Session file with `in_progress: true`; second invocation exits 4 with message. |
| `test_unknown_pool_exit3` | `--pool nonexistent`; exit 3 before any ssh or stat call. |
| `test_empty_pool_no_restart` | `BackendState.list()` returns `[]`; exit 0 with "nothing to restart". |
| `test_ready_timeout_flag` | `--ready-timeout 5`; stats never return UP within 5 iterations; exit 1. |
| `test_vllm_timeout_flag` | ssh `subprocess.run` raises `TimeoutExpired`; exit 1; halts. |
| `test_quiet_flag_suppresses_progress` | `--quiet`; per-ep progress lines absent from stderr. |
| `test_mutual_exclusion_flags` | `--fresh --status` together; exit 2 with usage error. |

### Session file helper unit tests

```python
def test_session_file_write_read_roundtrip(tmp_path): ...
def test_session_file_atomic_write_no_partial(tmp_path): ...
def test_session_file_missing_returns_none(tmp_path): ...
def test_session_file_corrupted_raises(tmp_path): ...
```

### Integration test (optional, marked)

```python
@pytest.mark.integration
def test_rolling_restart_localhost_integration(tmp_path):
    """Requires ssh to localhost with BatchMode; skipped in CI."""
    ...
```

---

## Boundaries

### Always do

- Issue `vctl serve restart` as a single atomic ssh call — never split into
  `stop` + `start` as separate ssh calls, as that would leave the ep absent from the
  pool between steps.
- Write the session file checkpoint atomically after every ep (both on success and on
  halt), so a crash between eps never loses progress.
- Open a fresh `lb_admin_client` per `_fetch_haproxy_stats` call — the HAProxy admin
  socket closes after each response (see `CLAUDE.md` gotcha).
- Use `fcntl.flock` on the session lock file for all reads and writes; two pools can
  run concurrently (separate lock files), but two invocations for the same pool must
  not.
- Validate `--pool` against `mgr.lb.pools` before touching the session file — fail
  fast with exit 3 on unknown pool.
- Write all progress and error messages to `sys.stderr`; keep stdout clean for any
  future machine-parseable output.
- Respect `--dry-run` strictly: no ssh, no session file writes, no HAProxy queries.

### Ask first

- Changing the ssh username discovery beyond the `--ssh-user` flag (e.g. reading from
  `cluster.yaml` `lb.ssh_user` field) — touches schema.
- Adding parallel restart mode (`--parallel N`) — changes the session file schema and
  the progress output format.
- Persisting the session file on full success instead of deleting it (for audit trail)
  — operator preference decision.
- Supporting `--pool` as optional with a multi-pool default — the `--pool` flag is
  intentionally required in v1.
- Changing the prompt interface for resume (e.g. adding option `d` = "diff config and
  restart") — affects interactive UX contract.

### Never do

- Re-use a single `lb_admin_client` instance across two separate admin commands.
- Call `vctl serve stop` and `vctl serve start` as separate ssh commands instead of
  `vctl serve restart` — partial states are unrecoverable remotely.
- Write to the session file without holding the `_SessionFile` lock.
- Skip the HAProxy health check after ssh returns 0 and assume the ep is ready.
- Silently clobber a session file that is marked `in_progress: true` (unless `--abort`
  or `--fresh` is explicitly passed).
- Prune or remove the backend from the pool during restart — the ep should remain in
  `BackendState` throughout; HAProxy's own health check will temporarily mark it DOWN
  and then UP again as vllm restarts.
- Add imports of `commands/rolling_restart.py` to `cli.py` at module level — only add
  a string entry to `_COMMANDS`; the module must remain out of the cold import path.
- Use interactive TTY features (curses, readline) — the progress output is plain
  stderr lines so it works in scripts and log files.

---

## Acceptance Tests

**AT-1** (SC-1 — fresh run, full success)
Given pool `qwen3-5-9b` with two backends `["10.0.0.1:8000", "10.0.0.2:8000"]` in
`BackendState`; no session file; `subprocess.run` returns `CompletedProcess(returncode=0)`
for both ssh calls; `_fetch_haproxy_stats` returns `status="UP"` for each ep
immediately after the corresponding ssh call;
When `vctl rolling-restart --pool qwen3-5-9b` is invoked;
Then the session file is written before any ssh calls, both eps appear in `completed`
after their respective restarts, the session file is deleted on exit, `subprocess.run`
is called exactly twice with `"vctl serve restart"` in argv, and the command exits 0.

**AT-2** (SC-2 — ssh failure halts and records state)
Given pool with three backends `["ep1", "ep2", "ep3"]`; ssh for ep2 returns
`CompletedProcess(returncode=255, stderr="Permission denied")`; ep1 ssh and health
check succeed;
When `vctl rolling-restart --pool P` is invoked;
Then the session file on disk shows `completed=["ep1"]`, `failed=["ep2"]`,
`pending=["ep3"]`, `in_progress=false`; stderr contains `"ssh failed"` and
`"HALTING"`; command exits 1.

**AT-3** (SC-3 — resume when failed ep is now UP)
Given a session file with `completed=["ep1"]`, `failed=["ep2"]`, `pending=["ep3"]`,
`in_progress=false`; `_fetch_haproxy_stats` returns `status="UP"` for `ep2` on the
initial probe; subsequent ssh and health checks for ep3 succeed;
When `vctl rolling-restart --pool P` is invoked;
Then stderr contains `"verified: ep2 was fixed externally"`, ep2 and ep3 both appear in
the completed list, session file is deleted, command exits 0.

**AT-4** (SC-4 — resume prompt: operator picks abort)
Given a session file with `failed=["ep2"]`, `in_progress=false`; `_fetch_haproxy_stats`
returns `status="DOWN"` for ep2; user input is `"c"`;
When `vctl rolling-restart --pool P` is invoked;
Then the interactive prompt is printed to stderr; `subprocess.run` (ssh) is NOT called;
the session file remains on disk unchanged; command exits 1.

**AT-5** (SC-5 — `--fresh` forces new run)
Given a session file with `completed=["ep1"]`, `pending=["ep2", "ep3"]` (partial run);
all ssh and health checks succeed;
When `vctl rolling-restart --pool P --fresh` is invoked;
Then the existing session file is deleted before any ssh calls; a new session file is
written covering ALL three eps (`completed` starts empty, `pending` covers all three);
all three ssh calls are made; session file is deleted on exit; exits 0.

**AT-6** (SC-6 — `--status` reads without modifying)
Given a session file with `completed=["ep1"]`, `pending=["ep2"]`, `in_progress=false`;
When `vctl rolling-restart --pool P --status` is invoked;
Then the session file content is printed to stdout (or stderr); no ssh call is made;
session file is not modified or deleted; command exits 0.

**AT-7** (SC-7 — `--abort` clears session)
Given a session file present with `in_progress=false`;
When `vctl rolling-restart --pool P --abort` is invoked;
Then the session file is deleted; no ssh call is made; command exits 0.
Given no session file present; When `--abort` is invoked; Then command exits 0 with
informational message `"no session file for pool P"`.

**AT-8** (SC-8 — `--dry-run` makes no changes)
Given pool with two backends; no session file;
When `vctl rolling-restart --pool P --dry-run` is invoked;
Then stderr contains two `"would restart"` lines; `subprocess.run` (ssh) is never
called; no session file is created; `_fetch_haproxy_stats` is never called; command
exits 0.

**AT-9** (SC-9 — concurrency guard exits 4)
Given a session file for pool `P` with `in_progress=true`;
When `vctl rolling-restart --pool P` is invoked;
Then command exits 4 immediately; stderr contains `"rolling-restart already in progress
for pool P"` and mentions `--abort`; no ssh call is made; session file is not modified.

**AT-10** (SC-10 — CI gates pass)
Given the full changeset implementing this spec (new `commands/rolling_restart.py`,
updated `cli.py`, new `tests/test_commands_rolling_restart.py`);
When `ruff check .`, `ruff format --check .`, `mypy --strict src/vctl`, and
`pytest --cov=vctl --cov-fail-under=50` are each run in the repo root;
Then all four commands exit 0 with no errors, warnings, or coverage gate failures.

---

## File Map

| File | Change |
|---|---|
| `src/vctl/commands/rolling_restart.py` | New module: `run()`, `_SessionFile`, `_restart_one_ep()`, `_verify_ep_up()`, `_handle_resume()` |
| `src/vctl/cli.py` | Add `"rolling-restart": "vctl.commands.rolling_restart"` to `_COMMANDS` |
| `tests/test_commands_rolling_restart.py` | New: all unit tests (mock ssh + mock `_fetch_haproxy_stats`) |
| `CHANGELOG.md` | v0.7.0 entry |
| `pyproject.toml` | Bump version `0.6.0` → `0.7.0` |

No new runtime dependencies. No schema changes (`cluster.yaml` / `models.py` unchanged
— pool names are read from existing `LbHaproxy.pools`). No migration required.

---

## Out of Scope

- Parallel restart across N eps in the same pool concurrently.
- `vctl rolling-restart` without `--pool` (multi-pool default).
- ssh key auto-bootstrap (operator's `~/.ssh/config` or default key is assumed
  already configured).
- Cross-cluster orchestration (multiple `cluster.yaml` files in one run).
- Automatic rollback if the new vllm version fails health check (Phase 4 candidate).
- Draining the ep from HAProxy before restart — HAProxy's own health checks handle
  this transparently; the ep stays registered throughout.

---

*Version bump: `0.6.0` → `0.7.0` (new user-visible feature, minor bump per semver).*
