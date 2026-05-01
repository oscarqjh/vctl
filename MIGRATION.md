# Migration: bash prototype → Python `vctl`

Steps to fully migrate from the legacy bash CLI at
`/mnt/umm/users/qianjianheng/workspace/vllm_test/multi_node_dp/cluster.sh`
(symlinked as `~/.local/bin/vctl`) to the Python `vctl` package on GitHub.

This is a **clean migration** — current LB and serve processes will be drained
and stopped. Plan for a low-traffic window. Approximate downtime: 1–3 minutes
per pod (LB pod last).

For a zero-downtime path that defers the haproxy daemon swap, see the
"Coexistence" appendix at the bottom.

---

## 0. Pre-migration sanity check

On any pod with the bash CLI:

```bash
vctl lb list      # snapshot current backends
vctl lb health    # confirm all are healthy (so you know what should come back)
ps aux | grep -E "vllm|cluster.sh" | grep -v grep   # snapshot live processes
```

Save this output somewhere so you can verify post-migration parity.

---

## 1. Drain + stop on each non-LB pod

For each pod with a running `vctl serve` (i.e., an attached vllm backend):

```bash
# A. Drain self from LB (no new requests, in-flight finishes)
vctl lb drain self
sleep 30

# B. Send Ctrl+C / SIGINT to the bash `vctl serve` process
#    (its trap will drain again + remove + reap the vllm subtree)
pkill -INT -f "cluster.sh serve"

# C. Verify nothing left
ps aux | grep -E "vllm|cluster.sh" | grep -v grep
# expected: empty

# D. Confirm pod is removed from pool
vctl lb list
# the dropped backend should NOT appear
```

Repeat for every non-LB pod in the fleet.

---

## 2. Stop bash haproxy on the LB host pod

On the pod with `lb.host` (e.g. `10.119.30.181`):

```bash
# A. Stop haproxy (bash version uses tmux session "lb")
vctl lb stop
tmux kill-session -t lb 2>/dev/null

# B. Confirm
ps aux | grep haproxy | grep -v grep
# expected: empty
```

At this point the LB is offline. Move quickly through the next steps to
minimize downtime.

---

## 3. Remove bash CLI symlink (every pod)

```bash
rm -f ~/.local/bin/vctl
which vctl   # expected: nothing or "vctl not found"
```

---

## 4. Install Python `vctl` (every pod)

```bash
uv tool install git+https://github.com/oscarqjh/vctl.git

# Verify
which vctl              # → ~/.local/share/uv/tools/vctl/bin/vctl
vctl --version          # → vctl 0.1.0
vctl --help             # see subcommand tree
```

`uv tool install` lives in `~/.local/share/uv/tools/vctl/` (shared FS). One
install propagates to every pod that shares this `$HOME`.

---

## 5. Bootstrap config (every pod, or once on shared FS)

```bash
# Pick a stable location. Suggested:
mkdir -p ~/vctl-cfg && cd ~/vctl-cfg

# Generate cluster.yaml + models/*.yaml using your real env defaults.
vctl init-config

# Created:
#   ./cluster.yaml              (lb.host, venv, state_dir already set)
#   ./models/qwen3_5-9b.yaml
#   ./models/qwen3-vl-30b-a3b.yaml

# Verify resolved config
vctl info
```

If you're sharing the config dir across pods (recommended on shared FS),
just do this once and `cd ~/vctl-cfg` from each pod.

To use a custom location:

```bash
vctl init-config --dir /mnt/aigc/users/qianjianheng/vctl-cfg
export CLUSTER_CONFIG=/mnt/aigc/users/qianjianheng/vctl-cfg/cluster.yaml
```

---

## 6. Start the new LB on the LB host pod

On the pod with `lb.host`:

```bash
cd ~/vctl-cfg

# One-off install of haproxy (locates existing or builds from source)
vctl lb is-host && vctl lb install

# Start haproxy in tmux session "vctl-lb"
vctl lb start
vctl lb status
# expected: running, pid=<int>, host=10.119.30.181

# Re-register any persisted backends from the state file
vctl lb auto-add
```

The shared state file (`/mnt/aigc/users/qianjianheng/.vllm-lb-state/<host>_backends.txt`)
is reused, so if there were stale entries from before draining, they're now
known to the new LB. Drain or remove them as needed:

```bash
vctl lb list      # see what came back
vctl lb health    # check which are alive
# Remove any stale entry:
vctl lb remove <ip:port>
```

---

## 7. Bring up vllm backends (every pod)

```bash
cd ~/vctl-cfg
vctl serve            # preflight → spawn vllm → wait for /v1/models
                      # → auto-attach to LB → trap SIGINT/SIGTERM
                      # → drain + remove + kill_tree on shutdown
```

`vctl serve` runs in the foreground. Background it with tmux/nohup if needed:

```bash
tmux new -d -s vllm "cd ~/vctl-cfg && vctl serve"
tmux attach -t vllm   # to monitor
# Ctrl+B, D to detach
```

---

## 8. Verify end-to-end

From any pod:

```bash
# A. LB sees all backends as ready
vctl lb list
vctl lb health
# expected: each backend → UP, ready, /health=200, loaded=yes

# B. Wait-ready blocks until threshold (sanity check)
vctl lb wait-ready 1     # exits 0 immediately if ≥1 backend taking traffic

# C. End-to-end completion through the LB front
curl http://10.119.30.181:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"Qwen/Qwen3-VL-30B-A3B-Thinking",
       "messages":[{"role":"user","content":"reply ok"}],
       "max_tokens": 8}'
# expected: 200 with chat completion JSON
```

If all three pass, migration is complete.

---

## 9. Archive the bash prototype

```bash
mv /mnt/umm/users/qianjianheng/workspace/vllm_test/multi_node_dp \
   /mnt/umm/users/qianjianheng/workspace/vllm_test/multi_node_dp.archive

# (Optional) Clean up bash haproxy runtime dir
rm -rf ~/.vllm-lb
```

The archive directory keeps the bash CLI as a fallback should you ever need
to roll back (see Rollback section below).

---

## 10. Update CLAUDE.md / team docs

If your repo or team docs reference the old paths:

- `multi_node_dp/cluster.sh` → `vctl`
- `~/.local/bin/vctl` (bash symlink) → `~/.local/bin/vctl` (uv shim)
- `multi_node_dp/cluster.yaml` → `~/vctl-cfg/cluster.yaml`
- `multi_node_dp/models/<x>.yaml` → `~/vctl-cfg/models/<x>.yaml`

Repo: <https://github.com/oscarqjh/vctl>
Issues / new profiles / feature requests: <https://github.com/oscarqjh/vctl/issues>

---

## Rollback (if anything breaks)

The bash prototype is preserved at
`/mnt/umm/users/qianjianheng/workspace/vllm_test/multi_node_dp.archive/`
(or `multi_node_dp/` if you skipped step 9).

```bash
# Drain Python vctl backends and stop haproxy
vctl lb drain self; sleep 30
pkill -INT -f "vctl serve"
vctl lb stop
tmux kill-session -t vctl-lb 2>/dev/null

# Uninstall Python vctl
uv tool uninstall vctl

# Restore bash symlink
ln -sf /mnt/umm/users/qianjianheng/workspace/vllm_test/multi_node_dp.archive/cluster.sh \
       ~/.local/bin/vctl

# Restart bash haproxy + serve
vctl lb start
vctl serve
```

The shared state file is reused — backends recover automatically.

---

## Per-pod checklist (cheat sheet)

```bash
# === Pre (on every pod with a running serve) ===
vctl lb drain self && sleep 30
pkill -INT -f "cluster.sh serve"

# === On LB host pod ===
vctl lb stop
tmux kill-session -t lb

# === On every pod ===
rm -f ~/.local/bin/vctl
uv tool install git+https://github.com/oscarqjh/vctl.git
mkdir -p ~/vctl-cfg && cd ~/vctl-cfg
vctl init-config

# === On LB host pod ===
vctl lb is-host && vctl lb install
vctl lb start
vctl lb auto-add

# === On every pod (LB host included) ===
cd ~/vctl-cfg
vctl serve

# === Verify (any pod) ===
vctl lb list
vctl lb health
curl http://10.119.30.181:8080/v1/models
```

---

## v0.2.0 — Multi-pool support

v0.2.0 adds multi-pool LB routing. Existing v0.1.0 deployments require **no
changes** — a single `lb.client.bind_port` config is automatically synthesised
into a default pool with `served_model: "*"` at load time.

To adopt the new schema explicitly, run the one-shot migration command:

```bash
vctl config migrate cluster.yaml -o cluster.yaml
```

This rewrites `lb.client.bind_port: <port>` to:

```yaml
lb:
  pools:
    - name: default
      served_model: "*"
      bind_port: <port>
```

State files are also backwards-compatible: the existing
`<state_dir>/<host>_backends.txt` is read as the `"default"` pool on first
access. No manual state file migration is needed.

See [CHANGELOG.md](CHANGELOG.md) `## [0.2.0]` for the full list of additions.

---

## Coexistence (defer the haproxy swap, zero downtime)

If you can't take a downtime window now but want the new CLI:

The bash and Python tools share **the same haproxy daemon** (independent of
who started it) and the **same state file path**. Only the CLI changes.

```bash
# 1. Swap the CLI on every pod
rm -f ~/.local/bin/vctl
uv tool install git+https://github.com/oscarqjh/vctl.git
mkdir -p ~/vctl-cfg && cd ~/vctl-cfg
vctl init-config

# 2. (LB host only) Symlink bash haproxy runtime files so Python `lb status`
#    and `lb reload` work against the still-running bash haproxy
mkdir -p ~/.vctl/lb
ln -sf ~/.vllm-lb/haproxy.pid  ~/.vctl/lb/haproxy.pid
ln -sf ~/.vllm-lb/haproxy.cfg  ~/.vctl/lb/haproxy.cfg
ln -sf ~/.vllm-lb/admin.sock   ~/.vctl/lb/haproxy.sock

# 3. Verify
vctl info
vctl lb list           # same backends as before (talks to TCP admin)
vctl lb status         # running (via symlinked pidfile)
```

**Caveats:**
- `vctl lb start` will refuse — port 8080 already bound by bash haproxy. Do
  not call `lb start` until the haproxy daemon swap window.
- `vctl lb stop` looks for tmux session `vctl-lb`; bash uses session `lb`.
  If you stop bash haproxy via Python `lb stop`, it's a no-op. To stop:
  `tmux kill-session -t lb` (bash session name).
- All other ops (`lb list/add/remove/drain/attach/detach/health/wait-ready`)
  work transparently — they use the TCP admin socket and shared state file.

When you're ready for the haproxy swap, follow steps 1–2 + 6–9 of the clean
migration above.
