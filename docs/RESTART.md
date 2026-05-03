# Restarting a vLLM backend safely

When a vllm process needs a restart (KV cache fragmentation after long uptime, OOM, config change, model swap), follow the procedure for **your** start mode below. Both procedures keep the LB pool in a safe state — the backend is drained from the LB before the process dies, so in-flight requests finish cleanly and no traffic is sent to a dead port.

> **Sequence rule.** Restart **one backend at a time** in a multi-backend pool. After each restart, confirm the backend is back in service (`vctl lb wait-ready 1 --pool <name>`) before moving to the next. This keeps pool capacity at `N-1` minimum throughout.

---

## Mode A — vllm started via `vctl serve` (recommended)

If your tmux pane is running `vctl serve <profile>`, the existing signal handler does the safe drain + detach + kill cycle for you.

**On the backend host:**

```bash
# 1. Attach to the tmux session running `vctl serve`
tmux attach -t <session-name>     # commonly: vctl-vllm

# 2. Send Ctrl-C in the vctl serve pane.
#    vctl serve will:
#      - drain self from LB (set haproxy state = drain)
#      - wait for in-flight requests to finish (LB_DETACH_WAIT, default 30s)
#      - remove self from LB (set maint, del server, clean state file)
#      - kill the vllm subprocess tree
#      - exit 130
#    You'll see log lines for each step.

# 3. Once vctl serve has exited, relaunch:
vctl serve <profile>              # e.g. vctl serve qwen3_5-9b

# 4. Detach from tmux when vllm is up.
#    Ctrl-b d     (default tmux detach binding)
```

**From the LB host (or any host with vctl + LB reach), after the backend is back up:**

```bash
vctl lb wait-ready 1 --pool <pool-name>   # blocks until ≥1 ready in that pool
# or
vctl lb wait-ready 1 --pool <bind_port>   # by port (v0.4.3+)
```

When `wait-ready` exits 0, move on to the next backend.

### Quick reference

| Step | Command | Why |
|---|---|---|
| 1 | `tmux attach -t <session>` | Get to the vctl serve pane |
| 2 | `Ctrl-C` | Triggers drain → wait idle → remove → kill vllm → exit 130 |
| 3 | `vctl serve <profile>` | Relaunch; auto-attaches to LB on `/v1/models` ready |
| 4 | `vctl lb wait-ready 1 --pool <name>` (from LB host) | Confirm before next backend |

### Why this is safe

- `Ctrl-C` (`SIGINT`) and `SIGTERM` are both handled by `vctl serve` — drain happens before kill.
- The state file is cleaned only after haproxy acknowledges the removal (Reconciler haproxy-first invariant).
- If anything fails mid-way, `vctl lb info` will show the inconsistency; `vctl lb auto-add` reconciles.

---

## Mode B — bare `vllm serve` + manual `vctl lb attach`

If vllm was started as a plain `vllm serve ...` command (not wrapped by `vctl serve`), vctl doesn't own the process. You drive the kill+restart yourself; vctl handles the LB bookkeeping.

**On the backend host:**

```bash
# 1. Drain self from the LB (haproxy stops sending new requests).
#    `lb detach` finds eps in any pool's state file matching this host's IP,
#    drains them, waits for in-flight to drain (LB_DETACH_WAIT, default 30s),
#    then removes them via the haproxy admin socket.
vctl lb detach

# 2. Kill the bare vllm process.
#    Use SIGTERM first; SIGKILL only if it doesn't exit in ~30s.
pkill -TERM -f "vllm serve"
# Wait for it to exit
while pgrep -f "vllm serve" > /dev/null; do sleep 1; done

# 3. Relaunch with your original vllm argv.
#    (vctl doesn't know your original argv; you must remember/script it.)
vllm serve <model> --port 8000 [your other flags...]

# 4. Once vllm is healthy (probe /v1/models returns 200),
#    re-register with the LB.
vctl lb attach 8000               # or whatever port vllm is on
```

**From the LB host (or any host with vctl + LB reach):**

```bash
vctl lb wait-ready 1 --pool <pool-name>
```

Move to the next backend when ready.

### Quick reference

| Step | Command | Why |
|---|---|---|
| 1 | `vctl lb detach` | Drain self from LB; wait idle; remove from state + haproxy |
| 2 | `pkill -TERM -f "vllm serve"` | Kill vllm |
| 3 | `vllm serve <model> ...` | Relaunch with original argv |
| 4 | `vctl lb attach 8000` | Re-register with LB |
| 5 | `vctl lb wait-ready 1 --pool <name>` (from LB host) | Confirm before next backend |

### Why this is safe

- `vctl lb detach` waits for in-flight requests to drain before removing the backend (`LB_DETACH_WAIT` env var, default 30s).
- `vctl lb attach` probes `/v1/models` before adding to the LB, so a half-loaded vllm won't receive traffic.
- HAProxy's own health checks will mark the new backend `UP` once it answers — give it a few seconds after `lb attach`.

---

## Mode-mixing within a single pool

If different backends in the same pool use different start modes, just follow the appropriate procedure per backend. The LB doesn't care how vllm was started — only that the state file and haproxy agree on what's registered.

---

## Restart-everything (cluster-wide)

Same procedures, applied sequentially across all backends in a pool. **Do not parallelize** — keep at least `N-1` backends serving at any time.

```bash
# Example loop — operator runs on each backend host in turn
for backend in 10.0.0.5 10.0.0.6 10.0.0.7 10.0.0.8; do
  echo "now restart vllm on $backend; press Enter when wait-ready confirms"
  read -r
done
```

Or write a per-host crontab to stagger restarts every N hours.

---

## Troubleshooting

### `vctl lb detach` exits 4

LB admin socket unreachable. Either the LB process is down (`vctl lb info` to confirm), or this host can't reach `lb.host:lb.admin.bind_port`. Fix the LB / network first; do NOT manually edit the state file.

### After `vctl lb attach`, `vctl lb info` shows `⚠ DOWN`

vllm is registered but failing haproxy's health check (probe to `/v1/models` returns non-200). Either vllm hasn't finished loading the model yet (wait), or it's crashed (check tmux pane for vllm logs).

### `vctl lb info` shows `⚠ tracked-only` for a backend

State file claims it's there but haproxy doesn't have it. Run `vctl lb auto-add` — it reconciles via Reconciler.

### `vctl lb info` shows `⚠ untracked` for a backend

HAProxy has it but state file doesn't. Run `vctl lb add <ep>` to adopt it into the state file (idempotent).
