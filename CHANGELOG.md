# Changelog

All notable changes to this project will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: Semver.

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
