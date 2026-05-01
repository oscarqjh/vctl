# Backlog

## In progress
- v0.2.2 follow-up: pidfile/pgrep fallback for foreground haproxy, test isolation, etc.

## v0.2.2 hotfix queue
- [x] F1: `lb status`/`stop`/`reload` fall back to `pgrep -f cfg_path` when pidfile missing.
  Root cause: render.py never emitted `daemon` directive → haproxy runs foreground in tmux,
  ignores `-p pidfile` flag, pidfile never written. status() reported `pid:None, pid_alive:False`
  while haproxy was actually running. Fixed via `_find_haproxy_pid_by_cfg(cfg_path)` helper
  using psutil.process_iter; status/stop/reload all consult it after the pidfile path.
- [x] F2: integration tests `test_haproxy_register_drain_remove_cycle` and
  `test_haproxy_two_pools_with_distinct_backends` use the hard-coded `_TMUX_NAME = "vctl-lb"`
  and collide with a running real LB on dev machines. Parameterize tmux session name per
  test (e.g. `vctl-lb-test-<random>`) and ensure teardown kills the spawned haproxy.
- [x] F3: those same tests leak ~80 haproxy processes per CI run (each test spawns real
  haproxy without try/finally teardown). Audit teardown across the integration test file.
- [x] F4: `lb status` UX — when self-IP != lb.host, report "remote LB; pid is local-only"
  instead of pretending pid=None means trouble.
- [ ] F5: optional: emit `daemon` directive in render.py + redirect stdout log to a file
  so we get both pidfile AND captured logs. Tradeoff: tmux pane closes immediately, lose
  interactive `tmux attach`. Probably not worth — F1 already covers status/stop.
- [x] F6: `lb list` live-registration annotation — cross-reference state file against
  `show servers state` from haproxy admin socket so each entry is marked:
    ✓ live           — in state file AND registered in haproxy
    ⚠ tracked-only   — in state file, not in haproxy (auto-add will fix)
    ⚠ untracked      — in haproxy, not in state file (lb add adopts it)
  When LB is stopped, existing [LB STOPPED] banner is kept and no admin socket call is
  attempted. Admin socket errors degrade gracefully to a WARNING line + tracked-only
  annotation. Exit code stays 0 even when drift exists.

## Code review findings (2026-05-01) — v0.2.1 hardening

### Commit A — stop/serve/scaling correctness (Tier 1 critical)
- [x] A1: `vctl stop` broken on multi-pool: hard-codes `default` pool, crashes `ValueError` on >1 pool. Iterate `BackendState.list_pools(state_dir, lb_host)` and pass `pool_name=` through (stop.py:49,56,59)
- [x] A2: `vctl stop` only handles 1 endpoint match — drain/remove every match across every pool (stop.py:50-56)
- [x] A3: `serve` 120s `_wait_for_ready` timeout vs 1800s template — read `VLLM_ENGINE_READY_TIMEOUT_S` (or new `vctl_ready_timeout`), default 1800 (serve.py:107)
- [x] A4: `_do_drain` silent no-op when LB unreachable → raise/exit 4 with clear message (lb_scaling.py:146-152)
- [x] A5: `_do_add` reports success when haproxy admin add fails — propagate haproxy error (lb_scaling.py:111-127)
- [x] A6: `_do_remove` mutates state file BEFORE haproxy → call set_state maint + remove_server first, only then bs.remove(ep); haproxy errors surface (lb_scaling.py:130-143)
- [x] A7: `auto-add` skips force-set state ready → mirror `_do_add` final force-ready (lb_scaling.py:196-209)
- [x] A8: `_do_remove_cli` returns 0 on not-found anywhere — return 1 + try haproxy-side anyway (lb_scaling.py:75-86)

### Commit B — manager hygiene (Tier 1 + Tier 2 lb)
- [x] B1: `lb start` no double-start guard → call status() first, refuse if running unless --force (manager.py:44-56)
- [x] B2: `_send` recv loop terminates on `<4096` short-read → loop until buffer ends with `\n\n` + socket.settimeout(5.0) (runtime.py:69-79)
- [x] B3: `lb stop` no SIGKILL fallback → after SIGTERM, poll `os.kill(pid,0)` up to 10s, then SIGKILL (manager.py:67-79)
- [x] B4: `lb stop` no admin socket cleanup → unlink sock_path after kill (manager.py:67-79)
- [x] B5: pidfile staleness across reboot → after `os.kill(pid,0)` ok, also verify `/proc/<pid>/comm` contains haproxy (manager.py:118-130)
- [x] B6: `add_server` returns "new" on errors — parse for "New server registered." token, raise on others (runtime.py:81-85)
- [x] B7: `remove_server` ignores response — parse + raise on "Operation not permitted"/"No such server" (runtime.py:87-88)
- [x] B8: `show servers state` parser may pick `0.0.0.0` srv_addr — prefer IP from `b_<ip>_<port>` server name when name parses cleanly (runtime.py:93-120)
- [x] B9: `_do_health` probes `localhost:port` not the actual backend host — use endpoint host; rename `probe_local_vllm` (lb_scaling.py:212-249)
- [x] B10: `_do_health` returns unhealthy count as exit code (overflows >255) — return 1 if any unhealthy (lb_scaling.py:212-249)
- [x] B11: `lb reload` ignores stdout/stderr — capture, run `haproxy -c -f cfg` precheck, surface failures (manager.py:86-103)
- [x] B12: legacy state-file migration not flock-protected, races across processes — wrap in `_locked()` + `_atomic_write`, or move to LbManager.__init__ once (state.py:27-41)

### Commit C — config + UX (Tier 1 + Tier 2 regressions)
- [x] C1: ClusterFile/ProfileFile use `extra="ignore"` at top level — silently swallows typos like `Profile:` (capital). Switch to `extra="forbid"` (models.py:118-141)
- [x] C2: `profiles set` regex would mangle `profile: |` block scalar — reject if matched line is block-scalar header; reject multiple top-level `profile:` lines (profiles.py:52-56)
- [x] C3: `profiles set` no atomic write or utf-8 encoding — write to tmp + os.replace; encoding="utf-8" explicit (profiles.py:51)
- [x] C4: `config migrate` clobbers w/o backup — make `--write` opt-in (default = diff only), validate `new` round-trips through ClusterFile, write `<path>.bak` before clobber (config_cmd.py:98-108)
- [x] C5: `vctl config -h` shows bare verbs (regresses prior `lb` fix) — add `help=` per `add_parser`, `description=` on parent (config_cmd.py:24-33)
- [x] C6: `serve`/`stop`/`preflight` subparsers missing `description=`/help; `--skip-preflight` is no-op — wire it up or remove (serve.py:28-31, stop.py:20-23, preflight.py:16-18)
- [x] C7: `--profile`/`--log-level`/`--log-format` no `help=` text (cli.py:69-91)
- [x] C8: Exit code drift across modules vs documented mapping (0 ok, 1 generic, 2 config, 3 user, 4 environment): profiles.py:55→3, serve.py:111→4, preflight.py:66→4, lb.py:247→4
- [x] C9: `cli.py` catch-all `except FileNotFoundError` returns 2 even for non-cluster.yaml errors — narrow predicate
- [x] C10: `vctl lb where` shows only `pools[0]` — list all pools when >1 (lb.py:104-105)
- [x] C11: `init_config` partial clobber on `--force` — pre-flight existence sweep, fail before any write (init_config.py:44-58)
- [x] C12: `templates.py` ships hard-coded `/mnt/umm/...` and `/mnt/aigc/...` site-specific paths — replace with `<EDIT_ME>` sentinels or `${HOME}` template

### Commit D — env / coerce / resolver hardening
- [x] D1: env override silently overwrites scalar with dict (`VCTL_LB__HOST__FOO=1` clobbers `lb.host`) — raise ValueError on non-mapping descent (settings.py:16-37)
- [x] D2: env override empty key segments produce cryptic Pydantic errors — reject any empty segment (settings.py:26-28)
- [x] D3: `_coerce_scalar` accepts `nan/inf/1e3` — restrict ints to `re.fullmatch(r"-?\d+", s)` and floats to `r"-?\d+\.\d+"` (settings.py:40-52)
- [x] D4: `_deep_merge` None override becomes literal `"None"` env var via `serve.py str(v)` — treat None in `b` as "delete key from out" (resolver.py:60-61)
- [x] D5: `detect_self_ip` no fallback (air-gapped fail) — fall back to `gethostbyname(gethostname())` then `127.0.0.1` (platform.py:10-17)
- [x] D6: pydantic missing range constraints — `bind_port: ge=1,le=65535`; `num_gpus: ge=0`; `data_parallel/tensor_parallel/api_server_count: ge=1`; `fall/rise: ge=1` (models.py:23-110)
- [x] D7: `cluster.venv`/`state_dir` no `~` expansion — field_validator with `os.path.expanduser` (models.py:17-19)
- [x] D8: `--profile` no path-traversal sanitation — reject names with `/`, `..`, leading `.` (resolver.py:49-58)
- [x] D9: `LbHaproxy.host`/`Pool.served_model` accept empty string — `min_length=1` (models.py:50-90)
- [x] D10: `_kill_tree` `root.children(recursive=True)` raises NoSuchProcess on dead root — wrap in try (serve.py:154-167)
- [x] D11: `serve` Popen no `start_new_session=True` — SIGINT to parent double-delivers via PGID (serve.py:72)
- [x] D12: `rc.env` `True` → `"True"` (capital) breaks env vars — pydantic-validate types to str|int|float|bool, lowercase booleans
- [x] D13: yaml duplicate-key tolerance — custom SafeLoader that errors on duplicates (yaml_source.py:11-16)

### Commit E — security
- [x] E1: HAProxy admin TCP socket bound `*:9001` with `level admin`, no ACL → LAN takeover. Bind to 127.0.0.1 OR add ACL src netmask OR doc loudly (render.py:60-61)
- [x] E2: source-build haproxy install no SHA256/PGP verification (installer.py:50-55)
- [x] E3: `tmux_run_detached` shell-quoting: validate session name regex `^[A-Za-z0-9_.-]+$`; shlex.quote cmd components (platform.py:36-40)

## Up next (post-v0.1.0)
- Prometheus metrics endpoint (`vctl lb metrics`)
- Multi-cluster support (`~/.config/vctl/clusters/<name>.yaml` + `--cluster <name>`)

## Ideas (unprioritized)
- vLLM Router (cache-aware) as alternative `lb.kind`
- Self-update check
- Daemon mode for LB supervision
- REST API for orchestration
- Audit log
- Hash-based sticky routing docs
- Profile inheritance (`extends:`)
- Dry-run mode for `serve`
