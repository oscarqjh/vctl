# vctl

**vctl** is a typed Python CLI for orchestrating a multi-pod vLLM fleet behind an HAProxy load balancer. It replaces a bash/python prototype with a Pydantic v2 schema, atomic state management, structured logging, and a clean subcommand tree — so you can spin up, scale, and tear down vLLM inference backends with a single command instead of a pile of scripts.

---

## Install

```bash
uv tool install git+https://github.com/oscarqjh/vctl.git
```

Pin to a release tag (recommended):
```bash
uv tool install git+https://github.com/oscarqjh/vctl.git@v0.2.0
```

To include the optional migration helper (converts the old bash-prototype YAML to `vctl/v1`):

```bash
uv tool install "git+https://github.com/oscarqjh/vctl.git[migrate]"
```

Verify the install:

```bash
vctl --help          # < 200 ms startup
vctl --version
```

---

## Where vctl looks for `cluster.yaml`

In order:
1. `--config <path>` CLI flag
2. `CLUSTER_CONFIG` env var
3. `~/.vctl/cluster.yaml` (canonical default)

If none of these point to a readable file, vctl exits 2 with a clear message and tells you to run `vctl init-config`.

Bootstrap a config in the default location so `vctl` works from any directory:

```bash
vctl init-config         # writes to ~/.vctl/ by default
```

Now `vctl info`, `vctl serve`, etc. work from any directory.

---

## Quickstart

### 0. Bootstrap your config (recommended)

The fastest way to start is with `vctl init-config`, which scaffolds a fully-documented `cluster.yaml` and a default set of model profiles into `~/.vctl/`:

```bash
vctl init-config
# Created:
#   ~/.vctl/cluster.yaml
#   ~/.vctl/models/qwen3_5-9b.yaml
#   ~/.vctl/models/qwen3-vl-30b-a3b.yaml
```

Runtime artifacts (haproxy.cfg, haproxy.pid, haproxy.sock) live alongside under `~/.vctl/lb/` — single home for everything vctl-related.

Every field is documented inline. Edit `cluster.yaml` to set `lb.host`, `cluster.venv`, and `cluster.state_dir` for your environment, then pick a profile as your default.

Options:

| Flag | Description |
|---|---|
| `--dir <path>` | Write files into `<path>` instead of `~/.vctl/`. |
| `--force` | Overwrite existing files without prompting. |
| `--profiles a,b,c` | Scaffold only the named profiles (default: all built-in). |

Example — scaffold only one profile into a new directory:

```bash
vctl init-config --dir /opt/myconfig --profiles qwen3_5-9b
```

Available built-in profiles: `qwen3_5-9b`, `qwen3-vl-30b-a3b`.

### 1. Write a cluster config (manual alternative)

```bash
cp examples/cluster.yaml cluster.yaml
# edit: set lb.host to your HAProxy pod IP, adjust venv/state_dir
```

See [`examples/cluster.yaml`](examples/cluster.yaml) for an annotated template.

### 2. Write a model profile

```bash
mkdir -p models
cp examples/models/qwen3-vl-30b-a3b.yaml models/
# edit: adjust GPU count / parallelism as needed
```

See [`examples/models/`](examples/models/) for ready-to-use templates.

### 3. Inspect the resolved config

```bash
vctl info
```

Prints a resolved table of cluster + profile settings sourced from `cluster.yaml` and the active profile.

### 4. Launch vLLM and attach to the LB

```bash
vctl serve                        # uses profile from cluster.yaml
vctl serve models/qwen3_5-9b.yaml   # positional shortcut (see below)
```

`vctl serve` will:
1. Run `vctl preflight` (port / GPU checks).
2. Launch the vLLM API server processes.
3. Wait for all backends to be healthy (`/v1/models`).
4. Attach each backend to the HAProxy pool (`lb auto-add`).

---

## Multi-pool fleets

When you serve more than one model from the same HAProxy instance, configure
one pool per model in `cluster.yaml`:

```yaml
lb:
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

### Schema: `lb.pools: [...]`

Each entry is a `Pool` object with three fields:

| Field | Type | Description |
|---|---|---|
| `name` | str | Short identifier used in HAProxy frontend/backend names and `--pool` flags. |
| `served_model` | str | Full model name as reported by `/v1/models`. Use `"*"` to match any model (legacy single-pool). |
| `bind_port` | int | Frontend port this pool listens on. |

HAProxy emits one `frontend pool_<name>` + `backend pool_<name>` block per
pool, so traffic to `lb:8080` reaches only Model A workers and traffic to
`lb:8081` reaches only Model B workers.

### One frontend port per model

Clients should use the pool-specific URL that matches the model they want:

```bash
# Model A clients
curl http://lb:8080/v1/chat/completions ...

# Model B clients
curl http://lb:8081/v1/chat/completions ...
```

### `vctl init-config` emits multi-pool by default

Running `vctl init-config` now generates a `cluster.yaml` with a `lb.pools`
block (one pool per built-in profile). Edit it to match your model names and
ports.

### `vctl serve` auto-routes via `pool_for_model`

`vctl serve` resolves the active profile's `served_model` to the matching pool
entry via `pool_for_model`. If no pool claims the model, it exits with code 3
immediately — before spawning vLLM — so misconfiguration is caught early.

### `vctl lb add --pool` flag for manual override

By default `vctl lb add <ep>` probes `/v1/models` on the endpoint and
auto-selects the correct pool. Pass `--pool <name>` to skip the probe and
place the endpoint directly:

```bash
vctl lb add 10.1.2.5:8000 --pool model-a
vctl lb drain 10.1.2.5:8000 --pool model-a
vctl lb wait-ready 2 --pool model-a
```

---

## Concepts

| Term | Description |
|---|---|
| **profile** | A `vctl/v1 Profile` YAML describing a single model deployment (GPU layout, parallelism, vllm args). |
| **LB host** | The IP of the pod/machine where HAProxy runs. `vctl lb is-host` checks whether the current machine is the LB host. |
| **backend** | A single vLLM API server process (one per data-parallel shard). Tracked in the state file. |
| **state file** | An `fcntl.flock`-protected JSON file at `cluster.state_dir/<profile>.json` recording the PIDs and ports of live backends. A `.lock` sidecar is used for stable lock semantics across process restarts (AT-11). |

Config is loaded from `cluster.yaml` (or `--config`) and a profile YAML. Settings can also be overridden via environment variables (see below).

---

## Command reference

### Top-level

| Command | Description |
|---|---|
| `vctl info` | Print resolved cluster + profile config as a table. |
| `vctl profiles` | List available profile YAML files; `*` marks the active one. |
| `vctl profiles set <name>` | Switch the active profile by rewriting `profile:` in `cluster.yaml`. |
| `vctl args` | Print the vLLM CLI args that would be used for the active profile. |
| `vctl preflight` | Validate environment: GPU count, port availability, venv existence. |
| `vctl serve [PROFILE]` | Launch vLLM backends and auto-attach to LB. |
| `vctl stop` | Gracefully stop all backends for the active profile and detach from LB. |

### `vctl lb` — load-balancer management

| Command | Description |
|---|---|
| `lb install` | Download and install HAProxy binary (uses `HAPROXY_VERSION`). |
| `lb start` | Start HAProxy on the LB host (guards against self-IP conflict, exit 4). |
| `lb stop` | Stop the HAProxy process. |
| `lb status` | Show HAProxy process status. |
| `lb is-host` | Exit 0 if this machine is the configured LB host, else exit 1. |
| `lb where` | Print the LB host IP. |
| `lb list` | List registered backends from the state file. |
| `lb wait-ready` | Block until `ready_count` backends pass health checks AND the LB front returns HTTP 200 (AT-12). |
| `lb stats` | Dump HAProxy stats via admin socket. |
| `lb logs` | Tail HAProxy logs. |
| `lb config` | Print the rendered HAProxy config. |
| `lb reload` | Reload HAProxy config without dropping connections. |
| `lb add <ep> [--pool <name>]` | Add a backend to HAProxy (idempotent). Auto-routes by `/v1/models` probe; `--pool` overrides. |
| `lb remove` | Remove a backend from HAProxy. |
| `lb drain <ep> [--pool <name>]` | Set a backend to DRAIN state (stops new requests, waits for in-flight). |
| `lb attach` | Register a backend and wait for its `/v1/models` health probe to pass. |
| `lb detach` | Drain then remove a backend. |
| `lb auto-add` | Discover live backends from the state file and attach all. |
| `lb health` | Check health of all registered backends, grouped by pool. |
| `lb wait-ready [N] [--pool <name>]` | Wait for ≥N ready backends in every non-empty pool (or one named pool). |

### `vctl config` — schema and migration

| Command | Description |
|---|---|
| `config validate` | Validate `cluster.yaml` (and optional profile) against the Pydantic schema. |
| `config show` | Print the fully-merged resolved config as YAML. |
| `config schema` | Print the JSON schema for `cluster.yaml` or a profile. |
| `config migrate OLD.yaml` | Convert old bash-prototype YAML to `vctl/v1` format. |

---

## Positional profile shortcut

When a path (or bare name) is given as the first positional argument to `serve` or `stop`, vctl treats it as `--profile`:

```bash
vctl serve models/qwen3_5-9b.yaml
# equivalent to:
vctl serve --profile qwen3_5-9b
```

The `.yaml` extension and any leading directory components are stripped to derive the profile name.

---

## Environment variable overrides

All `lb.*` cluster fields can be overridden at runtime using `VCTL_LB__<FIELD>` (double underscore = nesting):

```bash
VCTL_LB__HOST=10.1.2.3 vctl lb start
```

| Variable | Effect |
|---|---|
| `VCTL_LB__HOST` | Override `lb.host` (LB pod IP). |
| `MODEL_PROFILE` | Alias for `--profile` (legacy compatibility). |
| `LB_WAIT_TIMEOUT` | Seconds to wait in `lb wait-ready` (default: 300). |
| `LB_DETACH_WAIT` | Seconds to wait for drain in `lb detach` (default: 60). |
| `HAPROXY_VERSION` | HAProxy version to install via `lb install` (default: `2.9.x`). |
| `VCTL_KILL_GRACE` | Seconds between SIGTERM and SIGKILL in `serve` shutdown (default: 10). |

---

## Troubleshooting

### LB host mismatch

```
Error: self-IP guard: current machine IP matches lb.host — cannot start LB on the compute node
```

`lb start` exits with code 4 if the LB host IP matches one of the current machine's IPs. This prevents accidentally starting HAProxy on a GPU worker. Set `lb.host` in `cluster.yaml` to the correct LB pod IP or use `VCTL_LB__HOST`.

### Drain stuck

```
lb detach  # hangs waiting for connections to drain
```

Override the drain timeout: `LB_DETACH_WAIT=10 vctl lb detach --backend <addr>`. Or force-remove: `vctl lb remove --backend <addr> --force`.

### Port collision

```
preflight: port 8000 is already in use on this host
```

Another process holds the port. Either stop it or change `server.http_port` in the profile YAML. `vctl preflight` reports all conflicting ports before `serve` launches anything.

---

## Migration from bash prototype

If you have an old `cluster.yaml` in the `multi_node_dp/` prototype format:

```bash
vctl config migrate old_cluster.yaml          # prints migrated YAML to stdout
vctl config migrate old_cluster.yaml -o cluster.yaml   # write in-place
```

The migrated file will have `apiVersion: vctl/v1` at the top and the new grouped schema. Run `vctl config validate` afterwards to confirm.
