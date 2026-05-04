# vctl CLI Reference

Concise reference for every `vctl` command and sub-command.
Run `vctl <cmd> --help` for the canonical help text (always up-to-date).

**vctl** is a typed Python CLI for orchestrating a multi-pod vLLM fleet behind HAProxy.
Distributed via `uv tool install`. Current version: **0.7.0**.

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

## Commands

### `vctl info`

**Synopsis:** `vctl info`

Print the resolved cluster + profile config as a table: model, parallelism, vllm port,
LB host, per-pool URLs, venv, state_dir.

---

### `vctl args`

**Synopsis:** `vctl args`

Print the vLLM CLI args that would be passed for the active profile, one per line.
Useful for debugging or piping into a manual `vllm serve` invocation.

---

### `vctl preflight`

**Synopsis:** `vctl preflight [--json]`

Run sanity checks before launching a vllm inference server:
- `gpus` — nvidia-smi present (or num_gpus=0)
- `shm` — /dev/shm ≥ 8 GB
- `venv` — `cluster.venv` path exists
- `lb_route` — TCP connection to `lb.host:pool.bind_port` succeeds
- `vllm_port` — `server.http_port` is free on localhost

Exits 0 when all checks pass, exit 4 on any failure.

| Flag     | Description |
|----------|-------------|
| `--json` | Emit results as JSON instead of human-readable lines |

---

### `vctl serve`

**Synopsis:** `vctl serve [--foreground] [--skip-preflight]`

Default (no flags): spawn vllm in a detached tmux session `vctl-vllm-<profile>`,
wait for readiness, attach to the LB pool, then return immediately.
The vllm process survives SSH disconnect.

| Flag               | Description |
|--------------------|-------------|
| `--foreground`     | Block until vllm exits (v0.4.x behavior). SSH disconnect kills vllm. Signals trigger drain → remove → kill. |
| `--skip-preflight` | Skip preflight checks before spawning. |

**Env vars:**

| Variable                      | Effect |
|-------------------------------|--------|
| `VCTL_READY_TIMEOUT`          | Readiness poll timeout in seconds (default: 1800) |
| `VLLM_ENGINE_READY_TIMEOUT_S` | Per-profile readiness override; wins over `VCTL_READY_TIMEOUT` |
| `LB_DETACH_WAIT`              | Seconds to wait for in-flight requests during drain (default: 600) |
| `VCTL_KILL_GRACE`             | SIGTERM → SIGKILL grace period in seconds (default: 30) |
| `VCTL_NO_PPID_WATCHDOG`       | Set to `1` to disable the PPID orphan watchdog (useful in containers) |

#### Sub-commands

Run `vctl serve <verb> --help` for per-verb help text.

##### `vctl serve status`

Show tmux session name, pid liveness, vllm readiness, LB-attached state, and log size
for the active profile.

##### `vctl serve stop`

Gracefully shut down the running vllm:
drain LB endpoint → wait for in-flight requests → remove from LB →
SIGTERM tmux session → SIGKILL if grace exceeded.

##### `vctl serve restart`

Stop the running vllm and start a fresh instance under the same profile.
Logs a warning if the stored `cmd.json` snapshot drifted from the current resolved config.

##### `vctl serve console`

Attach the operator's terminal to the live vllm tmux session.
`Ctrl-B D` detaches without killing vllm.
Use to inspect live model output, debug warmup, or send Ctrl-C inside the session.

##### `vctl serve logs`

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

### `vctl stop`

**Synopsis:** `vctl stop [--json]`

Drain and remove this host's vllm endpoint(s) from all LB pools, then kill the local
vllm subprocess tree. Operates on all pools; safe to run even when partially attached.

| Flag     | Description |
|----------|-------------|
| `--json` | Emit actions/errors as JSON |

---

### `vctl profiles`

**Synopsis:** `vctl profiles [list | set <name>]`

List available model profiles from `models/*.yaml`.
`*` marks the active profile from `cluster.yaml`.

| Sub-command    | Description |
|----------------|-------------|
| `list`         | List available profiles (default when no sub-command given) |
| `set <name>`   | Switch the active profile by rewriting `profile:` in `cluster.yaml` |

---

### `vctl rolling-restart`

```
vctl rolling-restart --pool <name> [FLAGS]
```

Sequential, halt-on-failure rolling restart of every endpoint in a named pool. For each endpoint: ssh to the worker host, run `vctl serve restart`, poll HAProxy until the endpoint returns `UP`, then move to the next. State is persisted to `~/.vctl/lb/rolling-restart/<pool>.json` so a failed or interrupted run can be resumed by re-running the same command.

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
| `--vllm-timeout SECONDS` | 600 | Seconds the remote `vctl serve restart` is allowed to take (ssh subprocess timeout). |
| `--ssh-user USER` | (ssh config) | Override ssh username for all worker connections. |
| `--remote-vctl-path PATH` | (login shell) | Absolute path to `vctl` on the remote host. If omitted, uses `bash -lc 'vctl serve restart'` to ensure PATH is loaded. |
| `--quiet` | false | Suppress per-endpoint progress lines; print only the final summary. |

**Exit codes:**

| Code | Meaning |
|---|---|
| 0 | All endpoints restarted and verified UP. Session file deleted. |
| 1 | Restart or health-check failure on an endpoint, or operator aborted resume. Session file preserved. |
| 2 | Corrupt session file (invalid JSON). Run `vctl rolling-restart --pool NAME --abort` to clear it. |
| 3 | Unknown pool name. |
| 4 | Rolling restart already in progress for this pool (`in_progress: true` in session file). Use `--abort` to clear it. |

**Session file location:** `~/.vctl/lb/rolling-restart/<pool>.json`

**Resume behaviour:** After a halt-on-failure (exit 1), re-running `vctl rolling-restart --pool <name>` automatically resumes: first verifies the failed endpoint's HAProxy status (5s probe); if `UP` it is moved to `completed`; if still DOWN the operator is prompted for (a) skip, (b) retry, or (c) abort.

---

### `vctl lb`

**Synopsis:** `vctl lb <verb> [flags]`

LB lifecycle and scaling control. All verbs require a reachable `cluster.yaml`.

#### Process management

| Verb      | Synopsis                        | Description |
|-----------|---------------------------------|-------------|
| `install` | `vctl lb install`               | Install HAProxy (conda → source-build fallback). SHA256-pinned. |
| `start`   | `vctl lb start [--force]`       | Start HAProxy in tmux session `vctl-lb`. Guards against starting on a worker node (exit 4). `--force` bypasses the self-IP guard. |
| `stop`    | `vctl lb stop`                  | SIGTERM HAProxy (via pidfile) and tear down the tmux session. |
| `status`  | `vctl lb status`                | Unified dashboard: LB process panel + per-pool table with scur/qcur/running/waiting. Always exits 0. |
| `reload`  | `vctl lb reload`                | Re-render `haproxy.cfg` and send `-sf <pid>` (graceful, zero-downtime reload). |
| `logs`    | `vctl lb logs`                  | Print contents of `haproxy.log`. |
| `config`  | `vctl lb config`                | Print the rendered `haproxy.cfg`. |

#### Discovery and health

| Verb         | Synopsis                             | Description |
|--------------|--------------------------------------|-------------|
| `is-host`    | `vctl lb is-host`                    | Exit 0 if this machine's IP matches `lb.host`, else exit 1. |
| `where`      | `vctl lb where [--pool <name>]`      | Print `lb.host:bind_port`. With `--pool` prints only the matching pool's URL; exit 3 on unknown pool. |
| `wait-ready` | `vctl lb wait-ready [N] [--pool P]`  | Block until ≥N backends pass health checks AND the LB front returns HTTP 200. `--pool` scopes to a single pool. Env: `LB_WAIT_TIMEOUT` (seconds, 0=forever). |
| `health`     | `vctl lb health`                     | Probe each registered backend; exit non-zero on any unhealthy. Use as a scripting gate. |

#### Backend scaling

| Verb       | Synopsis                                    | Description |
|------------|---------------------------------------------|-------------|
| `add`      | `vctl lb add <ep> [--pool <name>]`          | Register `ip:port` in a pool (idempotent). Auto-routes by `/v1/models` probe; `--pool` skips the probe. |
| `remove`   | `vctl lb remove <ep>`                       | Drop `ip:port` from its pool (set MAINT then delete). |
| `drain`    | `vctl lb drain <ep> [--pool <name>]`        | Mark backend as DRAIN — stops new traffic, finishes in-flight requests. |
| `attach`   | `vctl lb attach [port]`                     | Probe `localhost:<port>/v1/models` then add self to the matching pool (default port: 8000). |
| `detach`   | `vctl lb detach [--force]`                  | Drain self, wait for in-flight to drain, remove. `--force` drops active sessions before removal. |
| `auto-add` | `vctl lb auto-add`                          | Re-register every backend from the state file (post-restart recovery). |
| `prune`    | `vctl lb prune [--pool P] [--threshold D] [--dry-run]` | Remove health-check-failed (DOWN) backends that have been down past the threshold. |

**`vctl lb prune` flags:**

| Flag              | Description |
|-------------------|-------------|
| `--pool <name>`   | Scope to one pool (default: all pools); exit 3 on unknown pool name |
| `--threshold D`   | Override dead threshold (e.g. `5m`, `300s`, `2h`); default: `cluster.lb.prune.threshold` or `5m` |
| `--dry-run`       | Print candidates without removing; exit 0 |

---

### `vctl config`

**Synopsis:** `vctl config <verb>`

Inspect and manage vctl configuration files.

| Verb              | Synopsis                         | Description |
|-------------------|----------------------------------|-------------|
| `validate <path>` | `vctl config validate <path>`    | Validate `cluster.yaml` or `models/<name>.yaml` against the Pydantic schema. Exit 2 on any error. |
| `show`            | `vctl config show`               | Print the fully-resolved runtime config with env overrides applied (YAML). |
| `schema`          | `vctl config schema`             | Dump the JSON Schema for `ClusterFile` and `ProfileFile`. |

---

### `vctl init-config`

**Synopsis:** `vctl init-config [--dir <path>] [--force] [--profiles a,b,...]`

Scaffold `cluster.yaml` and `models/*.yaml` from canonical templates into `~/.vctl/`
(or `--dir`). Every field is documented inline.

| Flag                | Description |
|---------------------|-------------|
| `--dir <path>`      | Write into `<path>` instead of `~/.vctl/` |
| `--force`           | Overwrite existing files without prompting |
| `--profiles a,b,c`  | Scaffold only the named profiles (default: all built-in) |

Built-in profiles: `qwen3_5-9b`, `qwen3-vl-30b-a3b`.

---

## Configuration

Schema classes live in `src/vctl/config/models.py` (Pydantic v2, `extra="forbid"`).

**Config resolution order:**

1. `--config <path>` CLI flag
2. `CLUSTER_CONFIG` env var
3. `~/.vctl/cluster.yaml`

**Profile resolution order:**

1. `--profile <name>` CLI flag
2. `VCTL_PROFILE` env var
3. `MODEL_PROFILE` env var
4. `cluster.profile` field in `cluster.yaml`

**VCTL_* env overrides:** pattern `VCTL_<TOPLEVEL>__<NESTED>__...=value`.
Double underscore is the nesting delimiter; first segment must match a top-level field.

---

## State files

| Path | Purpose |
|------|---------|
| `~/.vctl/cluster.yaml` | Cluster config |
| `~/.vctl/models/<profile>.yaml` | Per-profile config |
| `~/.vctl/lb/haproxy.cfg` | Rendered HAProxy config |
| `~/.vctl/lb/haproxy.pid` | HAProxy PID file |
| `~/.vctl/lb/haproxy.sock` | HAProxy admin socket (local) |
| `~/.vctl/lb/watch.pid` | Sentinel for `tmux:vctl-lb-watch` watcher session |
| `<state_dir>/<lb_host>/<pool>_backends.txt` | Per-pool backend list (flock-protected) |
| `<state_dir>/<lb_host>/<pool>_backends.lock` | Lock sidecar (stable across `os.replace`) |
| `~/.vctl/lb/rolling-restart/<pool>.json` | Rolling-restart session file (per-pool) |
| `~/.vctl/vllm/<host>/<profile>.pid` | Tracked vllm PID |
| `~/.vctl/vllm/<host>/<profile>.log` | vllm log (piped from tmux) |
| `~/.vctl/vllm/<host>/<profile>.cmd.json` | Snapshot of resolved config at start time |
| `~/.vctl/vllm/<host>/<profile>.host` | Originating host IP |

---

## See also

- [`docs/CHANGELOG.md`](CHANGELOG.md) — version history
- [`docs/RESTART.md`](RESTART.md) — safe restart procedures
- [`README.md`](../README.md) — getting started and multi-pool guide
