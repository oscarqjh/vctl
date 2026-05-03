# Changelog

All notable changes to this project will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: Semver.

## [0.4.13] - 2026-05-03

### Fixed

- **vllm `FileNotFoundError: '/VLLM_OBJECT_STORAGE_SHM_BUFFER_<uuid>'` actually fixed.** v0.4.12's setpgrp change was based on a wrong hypothesis (the early `setsid manual` reproduction was misleading — when `setsid` was tested with a shorter wait the real bug had not yet had time to fire either). Iterative testing inside the actual pod identified the real root cause: vllm 0.19.2rc1 has a bug in `multimodal/registry.py:_get_cache_type` — the renderer (in API server) sees the global `data_parallel_size` and computes `is_ipc_supported = (api_process_count==1 AND (DP==1 OR external_lb))` → returns `"processor_only"`, so the writer (`ShmObjectStoreSenderCache`) is never instantiated. Each engine's worker process, however, sees engine-local `data_parallel_size=1` → `is_ipc_supported=True` → returns `"shm"`, so the worker tries to `ShmObjectStoreReceiverCache.attach(name)` to a shm the writer never created. Trigger combo: `api_server_count=1` + `data_parallel>1` + `mm-processor-cache-type=shm`. Confirmed by running 7 in-pod A/B tests: same combo fails consistently regardless of how vllm is launched (shell-direct, Python `Popen` no preexec, `Popen` with `setpgrp`, `Popen` with `setsid`); removing `--api-server-count` (vllm defaults it to `data_parallel`) lets the renderer see `_api_process_count=N==DP_N` → still `is_ipc_supported=False` but consistent → no shm path on either side → engine starts cleanly. Fixes:
  - **Built-in templates** (`commands/templates.py`): `api_server_count` defaults updated to match `data_parallel` (8 for Qwen3.5-9B, 4 for Qwen3-VL-30B-A3B). Comment explains the gotcha.
  - **`vctl serve` warning**: log a clear warning if profile combines `api_server_count=1` + `data_parallel>1` + `mm-processor-cache-type=shm`. Tells the operator exactly which knob to change.
  - **Existing user profiles**: must edit `~/.vctl/models/<profile>.yaml` manually to set `api_server_count` to match `data_parallel`. (Old profiles weren't generated through templates and don't get auto-migrated.)

### Note on v0.4.12

v0.4.12 swapped `subprocess.Popen(start_new_session=True)` for `preexec_fn=os.setpgrp` based on a wrong diagnosis. The setpgrp change is harmless (same SIGINT isolation, no controlling-tty detach) and is kept. It does not, however, fix the shm bug — that fix lives in this v0.4.13.

## [0.4.12] - 2026-05-03

### Misdiagnosed (superseded by v0.4.13)

- Replaced `start_new_session=True` with `preexec_fn=os.setpgrp` in `vctl serve`'s vllm `Popen` call, on the (wrong) belief that `setsid()` was breaking vllm's multimodal shm coordination. Subsequent in-pod testing showed the bug was actually `api_server_count=1` + DP>1 + shm-cache — see v0.4.13. The setpgrp change is retained because it's a strictly equivalent SIGINT-isolation primitive without controlling-tty detach. v0.4.12 itself **does not resolve** the shm crash.

## [0.4.11] - 2026-05-03

Empty release. Tag was published prematurely against the 0.4.10 commit because of a sandbox-denial / dirty-working-tree interaction during the release flow. No code change is associated with the v0.4.11 tag — the intended `setsid`→`setpgrp` fix shipped under v0.4.12 instead. Do not install v0.4.11 — it is identical to v0.4.10.

## [0.4.10] - 2026-05-03

### Fixed

- **`vctl preflight` now checks `server.http_port` is not already in use on localhost.** Previously, if a stale vllm from a crashed `vctl serve` was still bound to port 8000, preflight (which only checked `gpus`/`shm`/`venv`/`lb_route`) didn't catch it. The new `vllm serve` subprocess would fail to bind, but `_wait_for_ready` polled `localhost:8000/v1/models` and got a successful response from the **stale** process — so vctl serve happily registered the stale ep with the LB. Result: LB routes traffic to the wrong vllm instance, the new vllm subprocess silently exits, and the operator has no idea anything is wrong. New `_check_vllm_port_free` attempts to bind `127.0.0.1:port` and fails preflight with a clear message ("port already in use; run `vctl stop` or kill the stale process first") if occupied. Exits 4.

## [0.4.9] - 2026-05-03

### Fixed

- **HAProxy now retries failed requests on another backend** (`option redispatch` + `retries 3` in the rendered `defaults` block). Previously, when a backend connection failed (refused, reset, timeout), haproxy returned the error to the client. Eval/inference workloads saw `503 No server available` until the failed backend was finally marked DOWN by the health check (90s with `inter 30s fall 3` config). Now haproxy fans the failed request out to a healthy backend before giving up. Fundamental TCP-proxy limitation around mid-stream failover still applies — but for plain non-streaming REST calls (the common case), retry-on-failure now works.
- **Backends marked DOWN by the health check have their existing sessions force-closed immediately** (`on-marked-down shutdown-sessions` on every server line). Previously, when a vllm crashed mid-request, haproxy detected DOWN but the half-open TCP connections kept counting toward `cur_sess`, blocking subsequent `del server` calls. Now those sessions are flushed at the moment haproxy decides the server is DOWN; clients see a clean error and the backend can be removed.
- **Dynamic `add server` admin commands now include the same `on-marked-down shutdown-sessions` flag** as static-cfg server lines, so backends added via `vctl lb add` or `vctl serve` get the same behavior as those rendered into the cfg at `lb start` time.

### Operator action required

After upgrading, **restart haproxy** to pick up the new cfg flags. `vctl lb reload` re-renders the cfg and graceful-reloads haproxy without dropping connections. Without this step the new defaults don't apply to a running LB.

## [0.4.8] - 2026-05-03

### Added

- **`vctl lb detach --force` flag.** Force-closes active haproxy sessions for the local backend after the drain wait expires, then removes. Destructive — drops in-flight requests. Use when a backend is stuck because vllm crashed mid-stream and haproxy still counts `cur_sess > 0` from half-open TCP, blocking `del server`. New `RuntimeClient.shutdown_sessions_server(backend, name)` wraps haproxy's `shutdown sessions server <backend>/<name>` admin command, with idempotent handling of "no such server".

### Fixed

- **`docs/RESTART.md` updated** with the `vctl lb detach --force` escape hatch under the stuck-MAINT troubleshooting entry.

## [0.4.7] - 2026-05-03

### Changed

- **`LB_DETACH_WAIT` default bumped from 30s → 600s.** LLM eval/inference workloads commonly have multi-minute generation; the old 30s default caused premature drain timeouts → haproxy refusing `del server` because `scur > 0` even in MAINT → backend stuck in MAINT. Tune via `LB_DETACH_WAIT=<seconds>` env var if needed. Affects `vctl lb detach`, `vctl stop`, and `vctl serve`'s SIGTERM drain handler.
- **`vctl lb detach` drain-wait now polls both vllm `/metrics` AND haproxy `scur`.** Previously polled only vllm `num_requests_running`; if vllm reported 0 but haproxy still had connections in flight (LB queue depth, slow client disconnect), removal would fail. Now both must report 0 before removal proceeds. New helper `lb_scaling._haproxy_scur(cli, backend, server)` parses the `show stat` CSV to extract scur for one specific server.

### Fixed

- **Documented "stuck in MAINT" recovery in `docs/RESTART.md`.** New troubleshooting entry covers the case where `vctl lb info` shows `⚠ MAINT` and `remove_server failed`. Two paths: wait for in-flight to drain naturally, or force-close sessions via haproxy admin (destructive). Includes `LB_DETACH_WAIT` tuning guidance for very long generations.

## [0.4.6] - 2026-05-03

### Fixed

- **`BackendOpFailed` now surfaces the underlying haproxy error in its `str()` message.** Previously CLI output was just `haproxy remove_server failed for ep='X' in backend='Y'` — operators had no way to see what haproxy actually said (e.g. `Operation not permitted`, `No such server`, `Server is in maintenance`). The original `RuntimeError` was attached as `__cause__` but never printed. New optional `cause=` kwarg on `BackendOpFailed.__init__`; Reconciler passes the underlying exception. Output is now: `haproxy remove_server failed for ep='10.0.0.5:8000' in backend='pool_default': Operation not permitted`. The five `raise BackendOpFailed(...) from exc` sites in `reconciler.py` (`want_present.add_server`, `want_present.set_state`, `want_absent.set_state`, `want_absent.remove_server`, `want_draining.set_state`) all updated.

## [0.4.5] - 2026-05-03

### Fixed

- **`vctl lb detach` / `lb remove` / `lb auto-add` no longer exit 3 on stale state-file pools.** Previously, if the state directory contained a `<pool>_backends.txt` file for a pool no longer present in `cluster.yaml` (e.g., leftover from earlier single-pool config), the pool-iteration loop in these verbs would call `Reconciler.want_*` with the stale pool name → `PoolNotFound` → exit 3 on the first stale entry. Now a new helper `lb_scaling._state_pools_in_config(mgr, bs)` filters the state-file pool list to those present in `mgr.lb.pools`, prints a `warning: skipping stale state files for unconfigured pools: [...]` line to stderr, and continues with the valid subset. Operators can clean up the stale files manually (`rm <state_dir>/<lb_host>/<pool>_backends.{txt,lock}`) but vctl no longer fails because of them.

## [0.4.4] - 2026-05-03

### Added

- **`docs/RESTART.md`** — safe restart procedures for both `vctl serve`–managed vllm and bare `vllm serve` + `vctl lb attach` setups. Step-by-step with the exact commands per mode, sequence rules (one backend at a time, `wait-ready` between), and troubleshooting for common drift annotations from `vctl lb info`.

### Changed

- **Documentation moved into `docs/`.** `BACKLOG.md`, `CHANGELOG.md`, `RELEASE.md` relocated from repo root → `docs/`. `README.md` stays at root and gains a Documentation section linking to the new paths. `tests/test_smoke.py` updated to read `docs/CHANGELOG.md`. `CLAUDE.md` stays at root (Claude Code reads it from there by convention).

## [0.4.3] - 2026-05-03

### Added

- **`--pool` flag now accepts a bind_port in addition to a pool name.** `vctl lb wait-ready 2 --pool 8080` resolves to the pool whose `bind_port` is 8080. Same for `lb add --pool`, `lb drain --pool`, `lb where --pool`. Resolution rule: if the value parses as digits-only → port lookup; else → name lookup. New helper `vctl.lb.routing.resolve_pool_ref(lb, ref)` is the single resolution point used by every CLI consumer.

### Changed (BREAKING)

- **Pool names cannot be pure digits.** Schema enforces `Pool.name` rejects values matching `^\d+$`. Reserved so the unified `--pool` flag (above) can disambiguate names from ports without ambiguity. Existing configs using non-digit pool names (e.g. `default`, `qwen3-5-9b`, `pool_a`) are unaffected; mixed digit/letter names like `p1` are still allowed.

## [0.4.2] - 2026-05-03

### Changed

- **`vctl lb wait-ready` success message now separates checked pools from skipped-empty pools.** Was: `all pools ready: A=4backends/200, B=empty` (read as if B was also ready when in fact it was skipped). Now: `ready: A=4backends/200 (skipped empty: B)`. Behavior is unchanged — empty pools are still skipped (not blocking). Operators that need to block on a specific pool already had the answer via `vctl lb wait-ready N --pool <name>`, which continues to work and now produces a clean single-pool message.

## [0.4.1] - 2026-05-03

### Fixed

- **`_name_for` now validates endpoint format** before deriving the haproxy server name. Rejects anything that doesn't match `\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d{1,5}` with a `ValueError`. Closes the haproxy admin-socket command-injection vector that surfaced in the Phase 1 code review (a malformed ep with embedded newline could inject a second admin command via the `add server <backend>/<name> <ep> check` interpolation). Practical risk was low (state file is admin-only) but the one-line fullmatch is cheap insurance.
- **`Reconciler._haproxy_servers` now filters by backend section** instead of returning all rows. `BackendStatus` gains a `backend: str` field populated from the haproxy `show servers state` response (the row's `parts[1]`). Rows with an empty `backend` (legacy haproxy versions or the `_NoOpClient` test stub) pass through unfiltered for back-compat. Resolves the documented per-pool filtering no-op from Phase 1.

### Internal

- `tests/test_lb_routing.py`: 10 new parametrized cases covering `_name_for` rejection of malformed / injection-shaped inputs.
- `tests/test_lb_reconciler.py`: 2 new tests for the `_haproxy_servers` section filter (filters when backend column populated; passes through when backend column empty).
- 411 tests passing (was 399 in v0.4.0; +12 new).

## [0.4.0] - 2026-05-03

### Removed (BREAKING)

- **`vctl config migrate` command removed.** The bash/python prototype it once converted from is no longer supported. Operators with old-format YAML still in production must run `vctl config migrate` from a v0.3.x install first, then upgrade to v0.4.0.
- **`src/vctl/config/migrate.py` module deleted.** Public functions `detect_kind`, `migrate_cluster`, `migrate_profile`, `dump_yaml` are gone.
- **`[migrate]` optional dependency dropped** from `pyproject.toml`. `ruamel.yaml>=0.18` is no longer pulled in; `uv tool install "git+...vctl.git[migrate]"` will fail.
- **`MIGRATION.md` removed** from the repo root.
- **`BackendState.migrate_if_needed` classmethod removed** from `src/vctl/lb/state.py`. The v0.1.0 → v0.2.x state-file layout migration (flat `<state_dir>/<lb_host>_backends.txt` → per-pool `<state_dir>/<lb_host>/<pool>_backends.txt`) is gone. State files in the v0.1.0 layout must be migrated by running a v0.2.x–v0.3.x release first.
- **`Model._drop_deprecated_served_as` validator shim removed** from `src/vctl/config/models.py`. Profile YAML files still carrying a `served_as:` field now fail schema validation (`ValidationError`, extra fields forbidden) instead of being silently dropped with a deprecation warning. Remove `served_as:` from every profile YAML before upgrading.

### Changed

- README "Migration from bash prototype" section deleted; install instructions no longer reference the `[migrate]` extra.
- `vctl config -h` no longer lists `migrate`; verbs are `validate / show / schema`.
- Inline comments in `lb/runtime.py`, `commands/serve.py`, `tests/test_commands_readonly.py` no longer reference the bash prototype.

### Internal

- `tests/test_lb_state_b.py` deleted (was the `migrate_if_needed` concurrency test suite).
- `tests/test_migrate.py` deleted (was the `config migrate` end-to-end suite).
- Two legacy-migration tests removed from `tests/test_lb_state.py`; four C4 migrate tests removed from `tests/test_commit_c.py`.
- `served_as` test in `tests/test_config_models.py` flipped to assert `ValidationError` under the v0.4.0 strict schema.
- `LbManager.__init__` no longer calls `BackendState.migrate_if_needed`.
- 399 tests passing (was 414 in v0.3.0; net −15 from migration test removals).

## [0.3.0] - 2026-05-03

### Changed (BREAKING)

- **`lb add` / `lb remove` / `lb drain` now exit 4 against a stopped LB** (was: silent state-file write or exit 0). The Reconciler enforces the haproxy-ack-before-state-write invariant; if the admin socket is unreachable it raises `LbUnreachable` and propagates as exit 4. Operators that relied on offline pre-population of the state file via `lb add` must run `lb start` first, then `lb add`.
- **`lb remove <ep>` for an ep absent from all pool state files now exits 4 against a stopped LB** (was: exit 1 with "not found"). The fallback loop iterates configured pools and probes haproxy via Reconciler; if the LB is unreachable, the first probe raises `LbUnreachable` and propagates as exit 4 instead of completing as not-found.
- **`lb auto-add` now exits 1 when any pool's reconcile fails** (was: silent suppress of all haproxy admin errors, always exit 0). Stderr identifies the failing pool. Closes F12.
- **`lb add` no longer writes the state file when haproxy refuses.** Closes F11. The state-first ordering bug in the legacy `_do_add` is fixed by routing through `Reconciler.want_present`, which reads pre-state, mutates haproxy first, and only writes the state file after a successful ack.
- **Stderr success messages surface `Outcome.action.name`** instead of legacy `(new)` / `(already present)` strings. Examples: `add 10.0.0.5:8000 ADDED (pool: default)`, `add 10.0.0.5:8000 READIED (pool: default)`, `add 10.0.0.5:8000 ADOPTED (pool: default)`. Carries the four-case distinction that was previously invisible (drift adoption, idempotent re-heal).
- **`lb add` no longer distinguishes `No such backend` (exit 3) from other haproxy admin errors (exit 1)** at the exit-code level. All `RuntimeError` from haproxy admin maps to `BackendOpFailed` → exit 1; the specific message remains in stderr. Operators inspecting exit codes for branching should match on stderr instead.

### Fixed

- **F11**: state-first ordering bug in `_do_add`. State file no longer holds entries that haproxy never acknowledged. Verified: no `bs.add(ep)` / `bs.remove(ep)` call precedes any haproxy admin call across all six migrated verbs.
- **F12**: silent suppress in `_do_auto_add`. `contextlib.suppress(Exception)` removed from haproxy admin calls; per-pool failures are surfaced and accumulated.

### Internal

- Six scaling verbs migrated to delegate state mutations to the Reconciler module shipped in v0.2.x: `_do_add`, `_do_remove`, `_do_drain`, `_do_auto_add`, `_do_remove_cli`, `_do_detach`. Each function body is now a try/except wrapper around a Reconciler call; argparse + pool-resolution + stderr formatting unchanged. Function signatures preserved for caller compatibility.
- New `_exit_for(exc: ReconcilerError) -> int` helper centralizes the ReconcilerError → exit code mapping (`LbUnreachable` → 4, `PoolNotFound` → 3, `BackendOpFailed` and others → 1).
- `import contextlib` removed from `lb_scaling.py` — no longer needed after F12 fix.
- `vctl serve`'s LB-attach step now checks `_do_add`'s return code; if attach fails (e.g. LB down at model-ready time), vllm is killed and `serve` exits non-zero rather than serving traffic that never reaches haproxy. Previously the rc was ignored — pre-Phase 2 this was masked by `_do_add` writing to the state file, but Phase 2 makes the failure visible and actionable.
- 414 tests passing including 7 new exit-4/exit-1 tests for the migrated verbs (covering AC-2, AC-4, AC-6, AC-7 LbUnreachable + BackendOpFailed paths, and the AC-? `_do_detach` LbUnreachable path).

## [0.2.13] - 2026-05-02

### Removed
- **`--served-model-name=` is no longer emitted** by `vctl serve` /
  `vctl args`. vllm defaults to `model.name` when not passed, which is the
  canonical HF id and matches our pool routing key. Setting it explicitly
  was redundant after v0.2.8 dropped the `served_as` alias mechanism.

## [0.2.12] - 2026-05-02

### Fixed
- **Export `CUDA_VISIBLE_DEVICES` from `profile.resources` to vllm subprocess.**
  Previously the field was only used for human-readable info display; the
  subprocess inherited whatever was in the parent shell's env (often unset
  or overly broad). Symptom: `--mm-processor-cache-type=shm` worker startup
  crashes with `FileNotFoundError: ... VLLM_OBJECT_STORAGE_SHM_BUFFER_*`
  because workers couldn't attach to the parent's shm segment when the GPU
  set wasn't deterministic. Now `serve` sets
  `env["CUDA_VISIBLE_DEVICES"] = rc.resources.cuda_visible_devices` before
  spawning vllm, matching what manual `CUDA_VISIBLE_DEVICES=... vllm serve`
  invocations do.

## [0.2.11] - 2026-05-02

### Changed
- **Built-in profile templates default `api_server_count: 1`** (was 8 / 4).
  When the OpenAI HTTP layer is run with multiple api-server workers AND
  the cache directories live on a shared/NFS filesystem, FlashInfer's
  FileLock-based compile cache deadlocks across api-server processes.
  Symptom: vllm crashes mid-request with `Deadlock: lock '...gdn_prefill_sm90.lock'
  is already held by a different FileLock instance`. 1 worker per backend
  is the right default; bump only after measuring the HTTP frontend (not GPU)
  as the bottleneck. Comment in templates updated to explain.

## [0.2.10] - 2026-05-02

### Fixed
- **`_client` falls through to TCP when unix-socket connect fails.** On a
  worker pod with `~/.vctl/` on a shared/NFS-mounted filesystem, the
  haproxy.sock FILE exists but `connect()` raises EOPNOTSUPP / ECONNREFUSED
  because the socket is bound on a different host. Previously `_client`
  saw `sock.exists()=True`, attempted unix, hit OSError, and returned None
  — never trying the TCP fallback. `lb info` then showed all backends as
  ⚠ tracked-only on workers despite the LB being reachable. Now: unix
  attempt is wrapped in its own try/except, and TCP is always attempted on
  unix failure.

## [0.2.9] - 2026-05-02

### Fixed
- **`lb info` Status column reflects HAProxy health.** Previously `✓ live`
  meant only "registered with haproxy admin" — a backend failing health
  checks (DOWN, MAINT, DRAIN) was still displayed as live. Now reads
  haproxy's `status` column from `show stat csv` and surfaces it as
  `⚠ DOWN` / `⚠ MAINT` / `⚠ DRAIN` / etc. UP backends still show `✓ live`.

## [0.2.8] - 2026-05-01

### Removed
- **`model.served_as` field dropped.** vllm is now always served under
  `model.name` (the canonical HuggingFace id, e.g. `Qwen/Qwen3.5-9B`).
  Previously a profile could set `served_as: qwen3_5-9b` so vllm registered a
  short name; clients sending the canonical id got a 404 "model does not
  exist". The alias added no value when there is only one served name.
  - `vctl serve` and `vctl args` now pass
    `--served-model-name={model.name}` (always the canonical HF id).
  - The `served_as:` line is removed from both built-in profile templates.
  - `config migrate` strips `served_as` from any old-schema YAML it touches.
  - **Backwards-compat:** a `model_validator(mode="before")` on `Model` pops
    `served_as` from any pre-existing profile YAML and emits a deprecation
    warning. Existing `~/.vctl/models/*.yaml` files keep loading — operators
    can remove the field at leisure.

## [0.2.7] - 2026-05-02

### Fixed
- **Boolean vllm flags now emit BooleanOptionalAction-compatible form.**
  Previously `vllm_args: { enable-prefix-caching: true }` produced
  `--enable-prefix-caching=true`, which vllm rejects with
  `"ignored explicit argument 'true'"`. Now emits bare `--enable-prefix-caching`
  for true, `--no-enable-prefix-caching` for false. Affects both
  `vctl serve` (subprocess argv) and `vctl args` (printed flags).

## [0.2.6] - 2026-05-02

### Fixed
- **`fetch_vllm_metrics` undercount with data parallelism:** with
  `--data-parallel-size N`, vLLM emits one Prometheus line per dp engine
  (e.g. `vllm:num_requests_running{engine="0"} 2`). The previous parser
  was last-wins so it reported a single engine's value instead of the
  per-backend total. Now sums across all engine labels — `running` /
  `waiting` columns in `lb info` reflect actual in-flight count for the
  whole vLLM process.

## [0.2.5] - 2026-05-02

### Fixed
- **`vctl lb info` live registry false-negative:** every backend was annotated
  `⚠ tracked-only` even when haproxy was actively forwarding traffic. Root
  cause: `RuntimeClient` reuses a single socket, but the haproxy admin socket
  closes after each command — the second `_send` (for `show servers state`)
  hit a closed socket and raised, while `show stat` (run first) had already
  populated. Now each `show ...` query opens its own client.
- **`uptime` column (was `last-check`):** the value `lastchg` is *seconds since
  the last UP↔DOWN transition*, not "seconds since the last health probe".
  For a healthy backend that's been stable, it grows continuously and looked
  alarming (`3569s`). Renamed to `uptime` and formatted compactly (`1h4m`,
  `2d3h`) so it reads as the backend's continuous-up time at a glance.

## [0.2.4] - 2026-05-02

**BREAKING**: `vctl lb status`, `vctl lb stats`, and `vctl lb list` have been removed.
Their output is now part of `vctl lb info`. `vctl lb health` is retained for scripting.

### Added
- **`vctl lb info` — unified dashboard:** single rich-table command that replaces the
  four separate read-only commands (`status`, `stats`, `list`, and the process fields
  previously scattered across them). Shows (in one screen):
  - LB process panel: pid, pid_alive, admin reachable, admin_bind, tmux session,
    cfg path, is_local_host, stats UI URL.
  - Per-pool table with columns: `Endpoint | Status | scur | qcur | running | waiting |
    last-check`. `scur` / `qcur` come from HAProxy `show stat csv`; `running` / `waiting`
    come from each backend's vLLM Prometheus `/metrics` endpoint.
  - Per-pool totals footer: `totals: scur=N  qcur=N  running=N  waiting=N`.
  - Drift section when any endpoint is in HAProxy but not in the state file.
  - Graceful degradation: LB stopped → compact state-file listing; admin socket
    unreachable → WARNING line + state-file fallback; vLLM `/metrics` error → `--` in
    running/waiting columns. Always exits 0.
- **`fetch_vllm_metrics(host, port, timeout=2.0)`** in `vctl.lb.probe`: fetches vLLM
  Prometheus text exposition from `/metrics`, strips optional `{label}` blocks, and
  returns `{"running": N | None, "waiting": N | None}`. Returns `None` on any network
  or HTTP error without raising. 2 s timeout by default.

### Removed (BREAKING)
- `vctl lb status` — process info now in `vctl lb info` LB process panel.
- `vctl lb stats` — stats UI URL now shown in `vctl lb info` LB process panel.
- `vctl lb list` — backend listing + live/tracked/untracked annotation now in
  `vctl lb info` per-pool tables.

### Retained
- `vctl lb health` — exit 0/1 gate for scripting (unchanged).

## [0.2.3] - 2026-05-01

Hotfix release picking up F9 (already on main since ea6cfb0) plus three new
fixes: subprocess teardown hardening (F7), PPID watchdog self-exit (F8), and
multi-process haproxy reload/stop safety (F10).

### Fixed
- **F9 — `lb reload` stale config (backport bump):** `LbManager.reload()` now
  re-renders `cluster.yaml` + state into `haproxy.cfg` before calling
  `haproxy -sf`. Previously, editing `cluster.yaml` and then running
  `vctl lb reload` silently re-executed haproxy on the stale on-disk file; the
  new config was only picked up after a full stop+start cycle. (Commit ea6cfb0
  landed on main before this version bump; this entry records the release.)
- **F7 — `vctl serve` test teardown:** `tests/conftest.py` gains
  `_force_cleanup_vctl_serve_for_path(stub_path)` — a helper that SIGKILLs any
  vctl-serve or fake-vllm orphan whose cmdline references a pytest temp path.
  A session-scoped autouse fixture (`_sweep_leaked_vctl_serve_at_session_end`)
  sweeps any such orphans at the end of the full pytest run. Matching is
  restricted to `/tmp/pytest-of-*` and `/tmp/tmp*` paths so production serve
  processes are never touched. All subprocess-spawning tests in
  `test_commands_serve.py` now wrap their bodies in `try/finally` that calls
  the cleanup helper and kills the subprocess on assertion failure.
- **F8 — `vctl serve` PPID watchdog:** after attaching to the LB pool, the
  idle-poll loop checks `os.getppid() == 1` every ~10 iterations (~5 s). When
  the launching shell has been reaped by init, this condition fires and runs the
  same drain+kill_tree shutdown path as SIGTERM (drain LB, wait for idle, remove
  from pool, SIGTERM→SIGKILL vllm, exit 0). Opt-out:
  `VCTL_NO_PPID_WATCHDOG=1` (default ON). Watchdog is disabled in all subprocess
  tests to avoid false triggers in CI containers where PPID may legitimately be 1.
- **F10 — `_find_haproxy_pid_by_cfg` multi-process safety:** new
  `_find_all_haproxy_pids_by_cfg(cfg_path) -> list[int]` returns all haproxy
  processes sharing a cfg, sorted oldest-first by `create_time`.
  `_find_haproxy_pid_by_cfg` now returns the **youngest** match (the one
  currently bound to the cfg; older siblings are mid-drain reload orphans).
  `stop()` SIGTERMs all matched PIDs with a shared 10 s deadline and SIGKILL
  escalation per survivor (no multiplied timeouts). `reload()` builds the `-sf`
  argument from the full PID list (`-sf 100 200 300`) so a new haproxy takes
  over from all stacked processes in a single reload, preventing the reload-race
  stack accumulation.

## [0.2.2] - 2026-05-02

Hotfix release. v0.2.1 surfaced two real-world issues during smoke testing
on the LB host: `lb status`/`stop` couldn't track foreground haproxy because
the renderer never emits the `daemon` directive (so the `-p pidfile` flag is
silently ignored), and integration tests leaked ~80 real haproxy processes
per CI run because they collided on a hard-coded tmux session name and
lacked teardown.

### Fixed
- **F1 — pidfile fallback for foreground haproxy:** `LbManager.status()`,
  `stop()`, and `reload()` now consult `_find_haproxy_pid_by_cfg(cfg_path)`
  (psutil scan for `haproxy -f <our cfg>`) when the pidfile is missing or
  empty. `lb status` now correctly reports the live PID even though haproxy
  is running in tmux without daemonization.
- **F2 — tmux session name parameter:** `LbManager(..., tmux_name=...)`.
  Defaults to `"vctl-lb"` (production) but tests pass unique names so they
  don't collide with a running real LB on dev machines or with parallel
  test workers.
- **F3 — integration test teardown:** the two real-haproxy integration tests
  now wrap their bodies in `try/finally` with a `_force_cleanup_haproxy_for_cfg`
  helper that SIGKILLs any haproxy whose cmdline matches the test's specific
  `cfg_path`. A session-scoped autouse fixture sweeps any `/tmp/pytest-of-*`
  haproxy at session end. Production paths are never matched.
- **F4 — `lb status` UX for remote LB:** when running on a worker pod
  (`detect_self_ip() != lb.host`), `lb status` now prints a compact
  `remote LB at <host>:<port> — admin_reachable=…` line instead of the full
  pidfile/tmux table (which is local-only and meaningless from a worker).
- **F6 — `lb list` live-vs-tracked annotation:** each entry is now marked
  `✓ live` (in state file AND haproxy admin), `⚠ tracked-only` (state file
  only — `vctl lb auto-add` will reconcile), or `⚠ untracked` (haproxy only —
  `vctl lb add <ep>` adopts it). Cross-references `show servers state` from
  the admin socket. Degrades gracefully when admin socket is unreachable.

## [0.2.1] - 2026-05-02

Hardening sweep — 48 fixes from a multi-axis code review (correctness, UX, security).
No schema breakage; existing v0.2.0 configs continue to load. New optional fields
(`lb.admin.bind_addr`) default to backwards-compatible behavior.

### Fixed (v0.2.1 hardening — Commit A: stop/serve/scaling correctness)
- **A1+A2 — `vctl stop` multi-pool:** iterate every pool from `BackendState.list_pools`
  and drain+remove every matching endpoint. Was: hardcoded `default` pool, crashed
  with `ValueError` on multi-pool configs.
- **A3 — `serve` ready timeout:** read from `VLLM_ENGINE_READY_TIMEOUT_S` (template
  env var) → `VCTL_READY_TIMEOUT` → default 1800s. Was: hardcoded 120s, killed 30B
  model loads mid-weight-load.
- **A4 — `lb drain` LB-down handling:** raises and exits 4 when the admin socket is
  unreachable. Was: silent no-op masquerading as success.
- **A5 — `lb add` error propagation:** non-idempotent admin-socket errors are now
  surfaced (exit 3 for "No such backend", 1 for generic). State file is rolled back
  on failure. Was: any error swallowed and reported as success.
- **A6 — `lb remove` ordering:** `set_state maint` → `remove_server` → state-file mutation.
  State file unchanged when haproxy refuses or is unreachable. Was: state-file-first
  ordering produced split-brain on partial failure.
- **A7 — `lb auto-add` force-ready:** explicit `set_state ready` after every `add_server`
  during recovery. Was: backends stuck in maint after LB restart, served no traffic.
- **A8 — `lb remove` not-found semantics:** scripting-friendly — returns 1 when
  nothing was removed; tries haproxy-side cleanup against all pools when state-file
  has no entry.

### Fixed (v0.2.1 hardening — Commit B: LB manager hygiene)
- **B1 — `lb start` double-start guard:** refuses when haproxy already running; `--force`
  triggers stop-then-start.
- **B2 — admin socket recv:** `socket.settimeout(5.0)` + recv-until-`\n\n` terminator.
  Was: `<4096` short-read terminator could truncate `show servers state` parsing.
- **B3+B4 — `lb stop` reliability:** poll for SIGTERM exit, SIGKILL fallback after 10s,
  unlink admin socket file. Was: pidfile unlinked immediately even when haproxy still
  draining; admin socket leaked across restarts.
- **B5 — pidfile staleness:** verify `/proc/<pid>/comm` matches "haproxy" before
  trusting the pid. Was: PID-recycle after reboot reported false `running`.
- **B6+B7 — admin socket response parsing:** `add_server` checks for "New server
  registered." token; `remove_server` raises on "Operation not permitted" / unknown
  errors. Was: any output was reported as success.
- **B8 — `show servers state` parsing:** prefer IP/port from server name `b_<ip>_<port>`
  over `srv_addr` column when name parses cleanly. Was: rows with `srv_addr=0.0.0.0`
  produced bogus endpoints.
- **B9+B10 — `lb health`:** probes each backend's actual host (not localhost); exit
  code 1 not unhealthy-count (could overflow >255).
- **B11 — `lb reload`:** `haproxy -c -f` precheck; captures stdout/stderr on failure.
  Was: traceback only.
- **B12 — legacy state-file migration:** flock-protected, atomic, runs once via
  `LbManager.__init__` bootstrap. Was: race across concurrent processes.

### Fixed (v0.2.1 hardening — Commit C: config + UX)
- **C1 — schema strictness:** `ClusterFile`/`ProfileFile` switched to `extra="forbid"`.
  Top-level typos like `Profile:` (capital) now hard-fail. Was: silently swallowed.
- **C2+C3 — `profiles set`:** rejects block-scalar `profile: |` and duplicate
  top-level `profile:` lines (exit 3). Atomic write via `tempfile + os.replace`,
  explicit UTF-8.
- **C4 — `config migrate` safety:** `--write` opt-in (default = diff to stdout);
  validates round-trip through `ClusterFile.model_validate` before clobbering;
  `.bak` written first; `--force` to overwrite existing `.bak`.
- **C5 — `vctl config -h`:** per-verb help + parent description (regression of
  earlier `lb` subcommand fix).
- **C6 — `serve`/`stop`/`preflight` subparsers:** `description=` populated;
  `--skip-preflight` actually wired (was a no-op flag).
- **C7 — root flags:** `help=` text on `--profile`, `--log-level`, `--log-format`.
- **C8 — exit code drift:** aligned with documented mapping (0 ok, 1 generic, 2
  config, 3 user, 4 environment).
- **C9 — `FileNotFoundError` catch:** narrowed — only cluster.yaml missing returns 2;
  other FileNotFoundErrors re-raise.
- **C10 — `lb where` multi-pool:** per-pool output for >1 pools; `--pool <name>` filter.
- **C11 — `init-config` partial-clobber guard:** pre-flight existence sweep; never
  writes a partial scaffold.
- **C12 — templates:** site-specific paths (`/mnt/...`) replaced with `<EDIT_ME>`
  sentinels.

### Fixed (v0.2.1 hardening — Commit D: env / coerce / resolver)
- **D1 — env override scalar→dict guard:** `VCTL_LB__HOST__FOO=1` now raises ValueError
  instead of silently overwriting `lb.host` with a dict.
- **D2 — env override empty key segments:** `VCTL_LB__=8080` and `VCTL___HOST=x` now
  raise ValueError.
- **D3 — `_coerce_scalar` strict regex:** rejects `nan`, `inf`, `1e3`, `0x10`. Only
  `^-?\d+$` parses as int; only `^-?\d+\.\d+$` parses as float.
- **D4 — `_deep_merge` None as delete:** profiles can unset cluster-level env vars
  by setting them to null. Was: literal `"None"` string exported as env value.
- **D5 — `detect_self_ip` fallback chain:** UDP probe → `gethostbyname(gethostname())`
  → `127.0.0.1`. Air-gapped hosts no longer crash on every CLI invocation.
- **D6 — pydantic Field constraints:** all `bind_port` ge=1 le=65535; `num_gpus` ge=0;
  parallelism fields ge=1; health `fall`/`rise` ge=1; health `path` must start with `/`.
- **D7 — `~` expansion:** `cluster.venv` and `cluster.state_dir` now expanduser;
  `min_length=1`.
- **D8 — `--profile` path-traversal:** rejects names containing `/`, `\`, `..`, or
  starting with `.`.
- **D9 — `host`/`served_model`:** `min_length=1` (rejects empty strings).
- **D10 — `_kill_tree` race:** tolerates `psutil.NoSuchProcess` at every step.
- **D11 — `serve` Popen:** `start_new_session=True` so SIGINT to vctl doesn't
  double-deliver to vllm via the same process group.
- **D12 — env value serialization:** booleans lowercased (`True` → `"true"`) for
  POSIX env compatibility.
- **D13 — yaml duplicate keys:** custom `_StrictLoader` raises `ConstructorError` on
  duplicate mapping keys. Was: silent last-wins.

### Security (v0.2.1 hardening — Commit E)
- **E1 — HAProxy admin socket bind address (`lb.admin.bind_addr`):** New optional field on
  `LbAdmin` (default `"0.0.0.0"` for backwards compatibility). Set to `"127.0.0.1"` to
  restrict the admin TCP socket to the LB host only. `manager.start()` now emits a WARNING
  when the socket is bound on `0.0.0.0` with `level admin`. The rendered haproxy.cfg now
  always uses an explicit IPv4 (e.g. `0.0.0.0:9001`) rather than the legacy `*:9001`
  shorthand. `lb status` output includes the `admin_bind` field.
- **E2 — Source-build SHA256 pinning:** The haproxy installer now downloads tarballs to
  memory via httpx, verifies SHA256 against a per-version pin table before writing to disk,
  and refuses to build unknown versions unless `VCTL_INSTALLER_INSECURE=1` is set.
- **E3 — tmux session name validation + argv quoting:** `tmux_run_detached`, `tmux_kill`,
  and `tmux_session_exists` now validate the session name against `[A-Za-z0-9_.-]+` and
  raise `ValueError` on invalid names. All three raise `RuntimeError("tmux not installed")`
  when tmux is not on PATH. New `tmux_run_detached_argv(name, argv)` helper uses
  `shlex.join` for safe composition from path components. `manager.start()` migrated to the
  argv form.

### Removed
- `./cluster.yaml` (cwd) and `~/vctl-cfg/cluster.yaml` (legacy home) config fallbacks.
  Resolution is now explicit: `--config` flag, `CLUSTER_CONFIG` env var, or `~/.vctl/cluster.yaml`.
  Tests that relied on cwd-based discovery now pass `CLUSTER_CONFIG` explicitly.

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
