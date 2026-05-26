# tctl CLI Reference

Concise reference for every `tctl` command and sub-command.
Run `tctl <workload> <verb> --help` for the canonical help text (always up-to-date).

**tctl** is a typed Python CLI for managing tmux-supervised long-running processes; ships workloads for vllm, haproxy, and lmms.
Distributed via `uv tool install`. Current version: **0.9.1**.

---

## Synopsis

```
tctl [--config PATH] [--profile NAME] [--log-level LEVEL] <workload> <verb> [args]
tctl <platform-cmd> [args]
```

Workloads: `vllm`, `haproxy`, `lmms` (hidden).
Platform commands: `config`, `init-config`, `fast-rm`.

---

## Exit codes

| Code | Meaning |
|------|---------|
| 0    | success |
| 1    | generic failure |
| 2    | config error (missing/invalid `cluster.yaml` or profile YAML) |
| 3    | pool routing failure (no pool serves model, ambiguous, unknown pool name) |
| 4    | environment / runtime error (LB unreachable, port in use, host guard, timeout) |
| 130  | SIGINT (Ctrl-C) |

---

## `tctl vllm` — vLLM workload

**Synopsis:** `tctl vllm <verb> [flags]`

All verbs require a reachable `cluster.yaml`.

### `tctl vllm info`

**Synopsis:** `tctl vllm info`

Print the resolved cluster + profile config as a table: model, parallelism, vllm port,
LB host, per-pool URLs, venv, state_dir.

---

### `tctl vllm args`

**Synopsis:** `tctl vllm args`

Print the vLLM CLI args that would be passed for the active profile, one per line.
Useful for debugging or piping into a manual `vllm serve` invocation.

---

### `tctl vllm profiles`

**Synopsis:** `tctl vllm profiles [list | set <name>]`

List available model profiles from `models/*.yaml`.
`*` marks the active profile from `cluster.yaml` (`vllm.default_profile`).

| Sub-command    | Description |
|----------------|-------------|
| `list`         | List available profiles (default when no sub-command given) |
| `set <name>`   | Switch the active profile by rewriting `vllm.default_profile` in `cluster.yaml` |

---

### `tctl vllm preflight`

**Synopsis:** `tctl vllm preflight [--json]`

Run sanity checks before launching a vllm inference server:
- `gpus` — nvidia-smi present (or num_gpus=0)
- `shm` — /dev/shm ≥ 8 GB
- `venv` — `cluster.venv` path exists
- `lb_route` — TCP connection to `haproxy.host:pool.bind_port` succeeds
- `vllm_port` — `server.http_port` is free on localhost

Exits 0 when all checks pass, exit 4 on any failure.

| Flag     | Description |
|----------|-------------|
| `--json` | Emit results as JSON instead of human-readable lines |

---

### `tctl vllm serve`

**Synopsis:** `tctl vllm serve [--foreground] [--skip-preflight]`

Default (no flags): spawn vllm in a detached tmux session `tctl-vllm-<profile>`,
wait for readiness, attach to the LB pool, then return immediately.
The vllm process survives SSH disconnect.

| Flag               | Description |
|--------------------|-------------|
| `--foreground`     | Block until vllm exits. SSH disconnect kills vllm. Signals trigger drain → remove → kill. |
| `--skip-preflight` | Skip preflight checks before spawning. |

**Env vars:**

| Variable                      | Effect |
|-------------------------------|--------|
| `TCTL_READY_TIMEOUT`          | Readiness poll timeout in seconds (default: 1800) |
| `VLLM_ENGINE_READY_TIMEOUT_S` | Per-profile readiness override; wins over `TCTL_READY_TIMEOUT` |
| `LB_DETACH_WAIT`              | Seconds to wait for in-flight requests during drain (default: 600) |
| `TCTL_KILL_GRACE`             | SIGTERM → SIGKILL grace period in seconds (default: 30) |
| `TCTL_NO_PPID_WATCHDOG`       | Set to `1` to disable the PPID orphan watchdog (useful in containers) |

#### `tctl vllm serve` sub-commands

Run `tctl vllm serve <verb> --help` for per-verb help text.

##### `tctl vllm serve status`

Show tmux session name, pid liveness, vllm readiness, LB-attached state, and log size
for the active profile.

##### `tctl vllm serve restart`

Stop the running vllm and start a fresh instance under the same profile.
Logs a warning if the stored `cmd.json` snapshot drifted from the current resolved config.

##### `tctl vllm serve console`

Attach the operator's terminal to the live vllm tmux session.
`Ctrl-B D` detaches without killing vllm.

##### `tctl vllm serve logs`

Print the last N lines of the vllm log, follow new lines, or prune the log file in-place
(preserves tmux pipe-pane fd).

| Flag           | Description |
|----------------|-------------|
| `-n N`         | Number of lines to show (default: 50) |
| `-f / --follow`| Stream new lines as they are written |
| `--prune`      | Trim log file in-place (mutually exclusive with `--follow`) |
| `--keep N`     | With `--prune`: keep last N lines (default: 10000) |
| `--all`        | With `--prune`: wipe everything instead of keeping last N |

---

### `tctl vllm stop`

**Synopsis:** `tctl vllm stop [--json]`

Drain and remove this host's vllm endpoint(s) from all LB pools, then kill the local
vllm subprocess tree. Consolidates the old separate `serve stop` and top-level `stop`
commands into one call: drain → tmux-kill → local-vllm-tree-kill.

| Flag     | Description |
|----------|-------------|
| `--json` | Emit actions/errors as JSON |

---

### `tctl vllm rolling-restart`

```
tctl vllm rolling-restart --pool <name> [FLAGS]
```

Sequential, halt-on-failure rolling restart of every endpoint in a named pool. For each endpoint: ssh to the worker host, run `tctl vllm serve restart`, poll HAProxy until the endpoint returns `UP`, then move to the next. State is persisted to `~/.tctl/vllm/rolling-restart/<pool>.json` so a failed or interrupted run can be resumed by re-running the same command.

**Required flag:**

| Flag | Description |
|---|---|
| `--pool NAME` | Target pool name. Must match a pool configured in `cluster.yaml`. |

**Mode flags (mutually exclusive):**

| Flag | Description |
|---|---|
| `--fresh` | Delete any existing session file before starting; force a fresh run over all endpoints. |
| `--status` | Print the current session file (or "no session in progress") and exit 0. No changes made. |
| `--abort` | Delete the session file if present and exit 0. |
| `--dry-run` | Print what would happen per endpoint without ssh-ing or writing a session file. |

**Tuning flags:**

| Flag | Default | Description |
|---|---|---|
| `--ready-timeout SECONDS` | 60 | Seconds to wait for HAProxy `UP` status after ssh returns 0. |
| `--vllm-timeout SECONDS` | 600 | Seconds the remote `tctl vllm serve restart` is allowed to take (ssh subprocess timeout). |
| `--ssh-user USER` | (ssh config) | Override ssh username for all worker connections. |
| `--remote-tctl-path PATH` | (login shell) | Absolute path to `tctl` on the remote host. If omitted, uses `bash -lc 'tctl vllm serve restart'` to ensure PATH is loaded. |
| `--quiet` | false | Suppress per-endpoint progress lines; print only the final summary. |

**Exit codes:**

| Code | Meaning |
|---|---|
| 0 | All endpoints restarted and verified UP. Session file deleted. |
| 1 | Restart or health-check failure on an endpoint, or operator aborted resume. Session file preserved. |
| 2 | Corrupt session file (invalid JSON). Run `tctl vllm rolling-restart --pool NAME --abort` to clear it. |
| 3 | Unknown pool name. |
| 4 | Rolling restart already in progress for this pool (`in_progress: true` in session file). Use `--abort` to clear it. |

**Session file location:** `~/.tctl/vllm/rolling-restart/<pool>.json`

---

## `tctl haproxy` — HAProxy workload

**Synopsis:** `tctl haproxy <verb> [flags]`

HAProxy lifecycle and scaling control. All verbs require a reachable `cluster.yaml`.

### Process management

| Verb      | Synopsis                              | Description |
|-----------|---------------------------------------|-------------|
| `start`   | `tctl haproxy start [--force]`        | Start HAProxy in tmux session `tctl-haproxy`. Guards against starting on a worker node (exit 4). `--force` bypasses the self-IP guard. |
| `stop`    | `tctl haproxy stop`                   | SIGTERM HAProxy (via pidfile) and tear down the tmux session. |
| `status`  | `tctl haproxy status`                 | Unified dashboard: process panel + per-pool table with scur/qcur/running/waiting. Always exits 0. |
| `reload`  | `tctl haproxy reload`                 | Re-render `haproxy.cfg` and send `-sf <pid>` (graceful, zero-downtime reload). |
| `logs`    | `tctl haproxy logs`                   | Print contents of `haproxy.log`. |
| `config`  | `tctl haproxy config`                 | Print the rendered `haproxy.cfg`. |

### Discovery and health

| Verb         | Synopsis                                  | Description |
|--------------|-------------------------------------------|-------------|
| `health`     | `tctl haproxy health`                     | Probe each registered backend; exit non-zero on any unhealthy. Use as a scripting gate. |

### Backend scaling

| Verb       | Synopsis                                           | Description |
|------------|----------------------------------------------------|-------------|
| `add`      | `tctl haproxy add <ep> [--pool <name>]`            | Register `ip:port` in a pool (idempotent). Auto-routes by `/v1/models` probe; `--pool` skips the probe. |
| `remove`   | `tctl haproxy remove <ep>`                         | Drop `ip:port` from its pool (set MAINT then delete). |
| `drain`    | `tctl haproxy drain <ep> [--pool <name>]`          | Mark backend as DRAIN — stops new traffic, finishes in-flight requests. |
| `scaling`  | `tctl haproxy scaling <attach\|detach\|auto-add>`  | Self-registration scaling sub-commands (see below). |
| `prune`    | `tctl haproxy prune [--pool P] [--threshold D] [--dry-run]` | Remove health-check-failed (DOWN) backends that have been down past the threshold. |

#### `tctl haproxy scaling` sub-commands

| Sub-verb   | Description |
|------------|-------------|
| `attach [port]`  | Probe `localhost:<port>/v1/models` then add self to the matching pool (default port: 8000). |
| `detach [--force]` | Drain self, wait for in-flight to drain, remove. `--force` drops active sessions before removal. |
| `auto-add` | Re-register every backend from the state file (post-restart recovery). |

**`tctl haproxy prune` flags:**

| Flag              | Description |
|-------------------|-------------|
| `--pool <name>`   | Scope to one pool (default: all pools); exit 3 on unknown pool name |
| `--threshold D`   | Override dead threshold (e.g. `5m`, `300s`, `2h`); default: `cluster.haproxy.prune.threshold` or `5m` |
| `--dry-run`       | Print candidates without removing; exit 0 |

---

## `tctl lmms` — lmms-eval workload (hidden)

**Synopsis:** `tctl lmms <verb>`

Manages the lmms-eval run loop. Hidden from top-level help but fully functional.

| Verb       | Synopsis                | Description |
|------------|-------------------------|-------------|
| `run-loop` | `tctl lmms run-loop`    | Start `run_loop.sh` in a detached tmux session `tctl-lmms` (with venv activated). |
| `stop`     | `tctl lmms stop`        | Kill the `tctl-lmms` tmux session. |
| `status`   | `tctl lmms status`      | Show whether `tctl-lmms` is running. |

---

## `tctl config` — schema and inspection

**Synopsis:** `tctl config <verb>`

| Verb              | Synopsis                          | Description |
|-------------------|-----------------------------------|-------------|
| `validate <path>` | `tctl config validate <path>`     | Validate `cluster.yaml` or `models/<name>.yaml` against the Pydantic schema. Exit 2 on any error. |
| `show`            | `tctl config show`                | Print the fully-resolved runtime config with env overrides applied (YAML). |
| `schema`          | `tctl config schema`              | Dump the JSON Schema for `ClusterFile` and `ProfileFile`. |

---

## `tctl init-config` — bootstrap

**Synopsis:** `tctl init-config [--dir <path>] [--force] [--profiles a,b,...]`

Scaffold `cluster.yaml` and `models/*.yaml` from canonical templates into `~/.tctl/`
(or `--dir`). Every field is documented inline.

| Flag                | Description |
|---------------------|-------------|
| `--dir <path>`      | Write into `<path>` instead of `~/.tctl/` |
| `--force`           | Overwrite existing files without prompting |
| `--profiles a,b,c`  | Scaffold only the named profiles (default: all built-in) |

Built-in profiles: `qwen3_5-9b`, `qwen3-vl-30b-a3b`.

---

## `tctl fast-rm` — parallel directory deletion

### `tctl fast-rm` — parallel directory deletion

Synopsis:
```
tctl fast-rm <PATH> [<PATH>...] [-f LIST_FILE | --list-file LIST_FILE]
                    [-j N | --jobs N]
                    [-y | --yes]
                    [-q | --quiet]
                    [--dry-run]
                    [-d | --detach]
```

Deletes one or more directory trees with OS-level parallelism (`find -type f | xargs -P N rm -f`). Much faster than `rm -rf` on trees with millions of small files. Safety-railed (refuses dangerous literals, system paths, `$HOME`, shallow paths).

**Flags:**

| Flag | Description |
|---|---|
| `-j N` / `--jobs N` | Parallel rm jobs (default `nproc`) |
| `-y` / `--yes` | Skip confirmation prompt |
| `-q` / `--quiet` | Skip pre-scan (file count + size) |
| `-f` / `--list-file` | Read paths from file (one per line, `#` comments, blank lines OK) |
| `--dry-run` | Validate + scan + report; no deletion. Overrides `--detach`. |
| `-d` / `--detach` | Spawn in tmux session `tctl-fastrm-<6hex>`. Survives SSH disconnect. Multiple `-d` coexist. Log at `~/.tctl/fastrm/<id>.log`. |

**Safety rails (rejected paths):**

- Dangerous literals: `""`, `"."`, `".."`, `"~"`, `"/"`, `"/*"`
- System paths: `/`, `/home`, `/root`, `/etc`, `/var`, `/usr`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/mnt`, `/proc`, `/sys`, `/dev`, `/boot`, `/tmp`
- `$HOME` exactly
- Paths with fewer than 3 segments (e.g. `/a/b`)
- Paths that don't exist or aren't directories

**Exit codes:**

- 0: all OK / dry-run
- 1: some per-target deletion failures
- 2: validation error (no valid paths, missing list file)
- 130: Ctrl-C

**Discover active detached runs:**

```
tmux ls | grep '^tctl-fastrm-'
```

---

## Configuration

Schema classes live in `src/tctl/config/models.py` (Pydantic v2, `extra="forbid"`).

**Config resolution order:**

1. `--config <path>` CLI flag
2. `CLUSTER_CONFIG` env var
3. `~/.tctl/cluster.yaml`

**Profile resolution order:**

1. `--profile <name>` CLI flag
2. `TCTL_PROFILE` env var
3. `cluster.vllm.default_profile` field in `cluster.yaml`

**TCTL_* env overrides:** pattern `TCTL_<TOPLEVEL>__<NESTED>__...=value`.
Double underscore is the nesting delimiter; first segment must match a top-level field.

---

## State files

| Path | Purpose |
|------|---------|
| `~/.tctl/cluster.yaml` | Cluster config |
| `~/.tctl/models/<profile>.yaml` | Per-profile config |
| `~/.tctl/haproxy/haproxy.cfg` | Rendered HAProxy config |
| `~/.tctl/haproxy/haproxy.pid` | HAProxy PID file |
| `~/.tctl/haproxy/haproxy.sock` | HAProxy admin socket (local) |
| `~/.tctl/haproxy/watch.pid` | Sentinel for `tmux:tctl-haproxy-watch` watcher session |
| `<state_dir>/<lb_host>/<pool>_backends.txt` | Per-pool backend list (flock-protected) |
| `<state_dir>/<lb_host>/<pool>_backends.lock` | Lock sidecar (stable across `os.replace`) |
| `~/.tctl/vllm/rolling-restart/<pool>.json` | Rolling-restart session file (per-pool) |
| `~/.tctl/vllm/<host>/<profile>.pid` | Tracked vllm PID |
| `~/.tctl/vllm/<host>/<profile>.log` | vllm log (piped from tmux) |
| `~/.tctl/vllm/<host>/<profile>.cmd.json` | Snapshot of resolved config at start time |
| `~/.tctl/vllm/<host>/<profile>.host` | Originating host IP |

---

## See also

- [`docs/CHANGELOG.md`](CHANGELOG.md) — version history
- [`docs/COOKBOOK-workloads.md`](COOKBOOK-workloads.md) — adding a new workload
- [`README.md`](../README.md) — getting started and multi-pool guide
