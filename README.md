# tctl

**tctl** is a typed Python CLI for managing tmux-supervised long-running processes; ships workloads for vllm, haproxy, and lmms. Pydantic v2 schema, atomic state management, structured logging, and a `tctl <workload> <verb>` command tree — spin up, scale, and tear down inference backends with a single command.

---

## Install

```bash
uv tool install git+https://github.com/oscarqjh/tctl.git
```

Pin to a release tag (recommended):
```bash
uv tool install git+https://github.com/oscarqjh/tctl.git@v0.9.0
```

Verify the install:

```bash
tctl --help          # < 200 ms startup
tctl --version
```

---

## Where tctl looks for `cluster.yaml`

In order:
1. `--config <path>` CLI flag
2. `CLUSTER_CONFIG` env var
3. `~/.tctl/cluster.yaml` (canonical default)

If none of these point to a readable file, tctl exits 2 with a clear message and tells you to run `tctl init-config`.

Bootstrap a config in the default location so `tctl` works from any directory:

```bash
tctl init-config         # writes to ~/.tctl/ by default
```

---

## Quickstart

### 0. Bootstrap your config (recommended)

The fastest way to start is with `tctl init-config`, which scaffolds a fully-documented `cluster.yaml` and a default set of model profiles into `~/.tctl/`:

```bash
tctl init-config
# Created:
#   ~/.tctl/cluster.yaml
#   ~/.tctl/models/qwen3_5-9b.yaml
#   ~/.tctl/models/qwen3-vl-30b-a3b.yaml
```

Runtime artifacts (haproxy.cfg, haproxy.pid, haproxy.sock) live alongside under `~/.tctl/haproxy/` — single home for everything tctl-related.

Every field is documented inline. Edit `cluster.yaml` to set `haproxy.host`, `cluster.venv`, and `cluster.state_dir` for your environment.

Options:

| Flag | Description |
|---|---|
| `--dir <path>` | Write files into `<path>` instead of `~/.tctl/`. |
| `--force` | Overwrite existing files without prompting. |
| `--profiles a,b,c` | Scaffold only the named profiles (default: all built-in). |

Example — scaffold only one profile into a new directory:

```bash
tctl init-config --dir /opt/myconfig --profiles qwen3_5-9b
```

Available built-in profiles: `qwen3_5-9b`, `qwen3-vl-30b-a3b`.

### 1. Write a cluster config (manual alternative)

```yaml
# ~/.tctl/cluster.yaml
apiVersion: tctl/v1
cluster:
  host: 10.1.2.100
  venv: /opt/venvs/vllm
  state_dir: ~/.tctl/state
haproxy:
  kind: haproxy
  host: 10.1.2.3
  pools:
    - name: qwen3-5-9b
      served_model: "Qwen/Qwen3.5-9B"
      bind_port: 8080
vllm:
  default_profile: qwen3_5-9b
```

See [`examples/cluster.yaml`](examples/cluster.yaml) for an annotated template.

### 2. Write a model profile

```bash
mkdir -p ~/.tctl/models
cp examples/models/qwen3-vl-30b-a3b.yaml ~/.tctl/models/
# edit: adjust GPU count / parallelism as needed
```

See [`examples/models/`](examples/models/) for ready-to-use templates.

### 3. Inspect the resolved config

```bash
tctl vllm info
```

Prints a resolved table of cluster + profile settings sourced from `cluster.yaml` and the active profile.

### 4. Launch vLLM and attach to the LB

```bash
tctl vllm serve                        # uses default_profile from cluster.yaml
tctl vllm serve models/qwen3_5-9b.yaml   # positional shortcut (see below)
```

`tctl vllm serve` will:
1. Run `tctl vllm preflight` (port / GPU checks).
2. Launch the vLLM API server processes.
3. Wait for all backends to be healthy (`/v1/models`).
4. Attach each backend to the HAProxy pool (`haproxy add`).

---

## Multi-pool fleets

When you serve more than one model from the same HAProxy instance, configure
one pool per model in `cluster.yaml`:

```yaml
haproxy:
  kind: haproxy
  host: 10.1.2.3
  pools:
    - name: model-a
      served_model: "Org/ModelA"
      bind_port: 8080
    - name: model-b
      served_model: "Org/ModelB"
      bind_port: 8081
```

### Schema: `haproxy.pools: [...]`

Each entry is a `Pool` object with three fields:

| Field | Type | Description |
|---|---|---|
| `name` | str | Short identifier used in HAProxy frontend/backend names and `--pool` flags. |
| `served_model` | str | Full model name as reported by `/v1/models`. Use `"*"` to match any model (legacy single-pool). |
| `bind_port` | int | Frontend port this pool listens on. |

HAProxy emits one `frontend pool_<name>` + `backend pool_<name>` block per
pool, so traffic to `lb:8080` reaches only Model A workers and traffic to
`lb:8081` reaches only Model B workers.

### `tctl vllm serve` auto-routes via `pool_for_model`

`tctl vllm serve` resolves the active profile's `served_model` to the matching pool
entry via `pool_for_model`. If no pool claims the model, it exits with code 3
immediately — before spawning vLLM — so misconfiguration is caught early.

### `tctl haproxy add --pool` flag for manual override

By default `tctl haproxy add <ep>` probes `/v1/models` on the endpoint and
auto-selects the correct pool. Pass `--pool <name>` to skip the probe and
place the endpoint directly:

```bash
tctl haproxy add 10.1.2.5:8000 --pool model-a
tctl haproxy drain 10.1.2.5:8000 --pool model-a
```

### Dashboard (`tctl haproxy status`)

`tctl haproxy status` shows everything in one screen:

```
╭─ HAProxy Process ──────────────────────────────────────────────╮
│ pid 708023  alive=true  admin=0.0.0.0:9001 (reachable)        │
│ tmux: tctl-haproxy    is_local_host=true                      │
│ cfg: /home/x/.tctl/haproxy/haproxy.cfg                        │
│ stats UI: http://10.119.30.181:9000                           │
╰────────────────────────────────────────────────────────────────╯

pool: qwen3-5-9b → http://10.119.30.181:8080   (Qwen/Qwen3.5-9B)
  Endpoint              Status   scur  qcur  running  waiting  last-check
  10.119.17.241:8000    ✓ live      0     0        0        0  2s
  10.119.27.91:8000     ✓ live      2     0        2        1  2s
  10.119.30.181:8000    ✓ live      0     0        0        0  1s
  totals: scur=2  qcur=0  running=2  waiting=1
```

---

## Command reference

For the complete reference (all flags, exit codes, examples) see
[`docs/CLI-REFERENCE.md`](docs/CLI-REFERENCE.md).

For adding a new workload see [`docs/COOKBOOK-workloads.md`](docs/COOKBOOK-workloads.md).

### `tctl vllm` — vLLM workload

| Command | Description |
|---|---|
| `tctl vllm info` | Print resolved cluster + profile config as a table. |
| `tctl vllm args` | Print the vLLM CLI args for the active profile. |
| `tctl vllm profiles [list\|set <name>]` | List available profile YAMLs; `set` switches the active profile. |
| `tctl vllm preflight [--json]` | Validate environment: GPU count, /dev/shm, port availability, venv. |
| `tctl vllm serve [--foreground] [--skip-preflight]` | Spawn vLLM in a detached tmux session and attach to LB pool. |
| `tctl vllm stop [--json]` | Drain from all LB pools, kill tmux session, sweep local vLLM process tree. |
| `tctl vllm rolling-restart --pool <name>` | Sequential rolling restart of a pool's endpoints; idempotent, auto-resumes. |

`tctl vllm serve` sub-commands:

| Command | Description |
|---|---|
| `tctl vllm serve status` | Show tmux/pid/lb-attached state and log size for the active profile. |
| `tctl vllm serve restart` | Stop + start in-place (preserves profile). |
| `tctl vllm serve console` | Attach terminal to live vllm tmux session. `Ctrl-B D` detaches. |
| `tctl vllm serve logs [-n N] [-f] [--prune]` | Tail / follow / prune the vllm log. |

### `tctl haproxy` — HAProxy workload

| Command | Description |
|---|---|
| `tctl haproxy start [--force]` | Start HAProxy in tmux session `tctl-haproxy` (guards against self-IP, exit 4). |
| `tctl haproxy stop` | Stop HAProxy and tear down the tmux session. |
| `tctl haproxy status` | Unified dashboard: process panel + per-pool table. Always exits 0. |
| `tctl haproxy reload` | Re-render config and reload HAProxy without dropping connections. |
| `tctl haproxy logs` | Print HAProxy log file. |
| `tctl haproxy config` | Print the rendered HAProxy config. |
| `tctl haproxy health` | Probe each registered backend; exit non-zero on any unhealthy. |
| `tctl haproxy add <ep> [--pool <name>]` | Register endpoint in a pool (idempotent, auto-routes). |
| `tctl haproxy remove <ep>` | Remove an endpoint from its pool. |
| `tctl haproxy drain <ep> [--pool <name>]` | Set backend to DRAIN state. |
| `tctl haproxy scaling attach/detach/auto-add` | Self-registration scaling sub-commands. |
| `tctl haproxy prune [--pool P] [--threshold D] [--dry-run]` | Remove DOWN backends past threshold. |

### `tctl lmms` — lmms-eval workload (hidden)

| Command | Description |
|---|---|
| `tctl lmms run-loop` | Start the lmms-eval run loop in a detached tmux session. |
| `tctl lmms stop` | Kill the `tctl-lmms` tmux session. |
| `tctl lmms status` | Show whether `tctl-lmms` is running. |

### `tctl config` — schema and inspection

| Command | Description |
|---|---|
| `tctl config validate <path>` | Validate `cluster.yaml` or a profile YAML. Exit 2 on error. |
| `tctl config show` | Print the fully-merged resolved config as YAML. |
| `tctl config schema` | Dump the JSON Schema for `ClusterFile` and `ProfileFile`. |

### `tctl init-config` — bootstrap

| Command | Description |
|---|---|
| `tctl init-config [--dir] [--force] [--profiles]` | Scaffold `cluster.yaml` + model profiles into `~/.tctl/`. |

### `tctl fast-rm` — parallel deletion

| Command | Description |
|---|---|
| `tctl fast-rm <path>...` | Parallel directory deletion with safety rails. `-d` for tmux supervision. |

---

## Positional profile shortcut

When a path (or bare name) is given as the first positional argument to `tctl vllm serve` or `tctl vllm stop`, tctl treats it as `--profile`:

```bash
tctl vllm serve models/qwen3_5-9b.yaml
# equivalent to:
tctl vllm serve --profile qwen3_5-9b
```

The `.yaml` extension and any leading directory components are stripped to derive the profile name.

---

## Environment variable overrides

All `haproxy.*` cluster fields can be overridden at runtime using `TCTL_HAPROXY__<FIELD>` (double underscore = nesting):

```bash
TCTL_HAPROXY__HOST=10.1.2.3 tctl haproxy start
```

| Variable | Effect |
|---|---|
| `TCTL_HAPROXY__HOST` | Override `haproxy.host` (LB pod IP). |
| `TCTL_PROFILE` | Active profile name (equivalent to `--profile`). |
| `LB_WAIT_TIMEOUT` | Seconds to wait in scaling `wait-ready` (default: 300). |
| `LB_DETACH_WAIT` | Seconds to wait for drain in detach (default: 60). |
| `TCTL_KILL_GRACE` | Seconds between SIGTERM and SIGKILL in vllm shutdown (default: 10). |

---

## Troubleshooting

### LB host mismatch

```
Error: self-IP guard: current machine IP matches haproxy.host — cannot start HAProxy on the compute node
```

`tctl haproxy start` exits with code 4 if the haproxy host IP matches one of the current machine's IPs. Set `haproxy.host` in `cluster.yaml` to the correct LB pod IP or use `TCTL_HAPROXY__HOST`.

### Drain stuck

```
tctl haproxy scaling detach  # hangs waiting for connections to drain
```

Override the drain timeout: `LB_DETACH_WAIT=10 tctl haproxy scaling detach`.

### Port collision

```
preflight: port 8000 is already in use on this host
```

Another process holds the port. Either stop it or change `server.http_port` in the profile YAML. `tctl vllm preflight` reports all conflicting ports before `serve` launches anything.

---

## Security

### HAProxy admin socket

By default the HAProxy admin TCP socket (used by `tctl haproxy add/remove/drain` from worker nodes)
is bound on `0.0.0.0:<haproxy.admin.bind_port>` with `level admin`. Any host that can reach this
port can take full control of the load balancer.

**Risk:** In a network without a firewall this is a LAN takeover vector.

**Recommended hardening:**

Option A — firewall the admin port (keep cross-host scaling working):
```bash
# Allow only your tctl worker subnet; block everything else
iptables -I INPUT -p tcp --dport 9001 ! -s 10.0.0.0/24 -j DROP
```

Option B — restrict to loopback (all `tctl haproxy` scaling commands must run on the haproxy host):
```yaml
# cluster.yaml
haproxy:
  admin:
    bind_port: 9001
    bind_addr: 127.0.0.1   # <-- add this line
```

When `bind_addr` is `0.0.0.0` (the default), `tctl haproxy start` will emit a WARNING reminding
you to firewall the port or switch to `127.0.0.1`.

---

## Documentation

- [`docs/CLI-REFERENCE.md`](docs/CLI-REFERENCE.md) — complete command reference: every flag, exit code, and sub-command.
- [`docs/COOKBOOK-workloads.md`](docs/COOKBOOK-workloads.md) — guide for adding a new workload.
- [`docs/CHANGELOG.md`](docs/CHANGELOG.md) — release history.
- [`docs/BACKLOG.md`](docs/BACKLOG.md) — open work + ideas.
- [`docs/RELEASE.md`](docs/RELEASE.md) — release process notes.
