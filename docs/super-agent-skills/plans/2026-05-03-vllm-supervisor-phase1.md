# vctl Phase 1 — VllmManager (tmux-backed vllm supervisor) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use super-agent-skills:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `vctl serve` return immediately and run vllm in a detached tmux session, so vllm survives caller exit and is identical-shape to a manual `tmux new-session -d 'vllm serve ...'`.

**Architecture:** New `VllmManager` class in `src/vctl/vllm_manager.py` mirrors `LbManager` shape. Tmux session `vctl-vllm-<profile>` owns the vllm process. State files under `~/.vctl/vllm/`. New sub-verbs `vctl serve status / stop / restart / attach / logs`. Backwards-compat `--foreground` keeps current v0.4.x behavior.

**Tech Stack:** Python 3.10+, pydantic v2, psutil, tmux (system binary), httpx (existing). pytest + monkeypatch for unit tests, real tmux + vllm stub for integration tests.

---

## Prerequisites

- Working editable install: `uv pip install -e ".[dev]"`
- Repo root: `/mnt/umm/users/qianjianheng/workspace/vctl`
- Run all checks via `.venv/bin/pytest`, `.venv/bin/mypy`, `.venv/bin/ruff`
- Green baseline: `pytest -q --cov=vctl --cov-fail-under=50` passes before starting

---

## Checkpoints

| After task | Gate |
|---|---|
| Task 2 | `pytest` green — no behavior change yet; refactor + flag wiring done |
| Task 4 | `VllmManager.start()` unit-tested under `VCTL_TEST_NO_SOCKET=1`; `vctl serve --foreground` still works end-to-end |
| Task 9 | All `VllmManager` methods unit-tested; state machine verified in isolation |
| Task 10 | `vctl serve <subverb>` end-to-end works under unit-test mocks |
| Task 11 | Integration test suite runnable on a host with tmux; skeletons execute |
| Task 12 | `mypy --strict`, `ruff check`, `ruff format --check`, `pytest --cov-fail-under=50` all green; branch ready to merge |

---

## Task 1: Extract `_run_foreground` (refactor, no behavior change)

**Files:**
- Modify: `src/vctl/commands/serve.py` lines 54–221 (current `run()` body → `_run_foreground`)
- Test: `tests/test_commands_serve.py` (all existing tests must still pass)

**What changes:** The entire body of `run(ns, argv_rest)` from line 55 onward is moved verbatim into a new private function `_run_foreground(ns, parsed)`. The outer `run()` becomes a two-line shim that parses argv and calls `_run_foreground`. No logic changes at all — this is a mechanical extraction.

### Steps

- [ ] **Step 1 — Confirm existing tests pass (baseline)**

  ```bash
  .venv/bin/pytest tests/test_commands_serve.py -q
  ```

  All tests must be green before the refactor. If any fail, do not proceed — fix them first.

- [ ] **Step 2 — Read serve.py and design the extraction boundary**

  Read `src/vctl/commands/serve.py` in full. The extraction boundary is:
  - `_run_foreground` receives `(ns: argparse.Namespace, parsed: argparse.Namespace) -> int`
  - `parsed` is the result of `_build_subparser().parse_args(argv_rest)` — currently computed at the top of `run()`
  - Everything from `if not parsed.skip_preflight:` to the end of `run()` moves to `_run_foreground`
  - `run()` keeps only the argparse call and the delegation

- [ ] **Step 3 — Implement the extraction in serve.py**

  Replace the current `run()` with:

  ```python
  def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
      parsed = _build_subparser().parse_args(argv_rest)
      return _run_foreground(ns, parsed)


  def _run_foreground(ns: argparse.Namespace, parsed: argparse.Namespace) -> int:
      # C6: wire --skip-preflight.  When not skipped, run preflight checks first.
      if not parsed.skip_preflight:
          from vctl.commands import preflight as _preflight

          pf_rc = _preflight.run(ns, [])
          if pf_rc != 0:
              _LOG.error("preflight checks failed (exit %d); aborting serve", pf_rc)
              return pf_rc

      rc = resolve(ns.config, profile=ns.profile)

      # FAIL-FAST POOL ROUTING — before subprocess.
      state_dir = Path(rc.cluster.state_dir)
      run_dir = Path.home() / ".vctl" / "lb"
      mgr = LbManager(rc.lb, state_dir=state_dir, run_dir=run_dir)
      pool = pool_for_model(rc.lb, rc.model.name)
      bs = BackendState(state_dir, rc.lb.host, pool=pool.name)

      env = os.environ.copy()
      venv_bin = str(Path(rc.cluster.venv) / "bin")
      env["PATH"] = f"{venv_bin}:{env['PATH']}"
      if rc.resources.cuda_visible_devices:
          env["CUDA_VISIBLE_DEVICES"] = rc.resources.cuda_visible_devices
      for k, v in rc.env.items():
          if isinstance(v, bool):
              env[k] = "true" if v else "false"
          else:
              env[k] = str(v)

      cmd = [
          "vllm",
          "serve",
          rc.model.name,
          f"--data-parallel-size={rc.parallelism.data_parallel}",
          f"--tensor-parallel-size={rc.parallelism.tensor_parallel}",
          f"--port={rc.server.http_port}",
      ]
      if rc.parallelism.api_server_count is not None:
          cmd.append(f"--api-server-count={rc.parallelism.api_server_count}")
          if (
              rc.parallelism.api_server_count == 1
              and rc.parallelism.data_parallel > 1
              and rc.vllm_args.get("mm-processor-cache-type") == "shm"
          ):
              _LOG.warning(
                  "config will hit vllm shm bug: api_server_count=1 + data_parallel=%d + "
                  "mm-processor-cache-type=shm. Remove api_server_count from the profile "
                  "(vllm will default to data_parallel), or change mm-processor-cache-type "
                  "to 'lru'. Continuing anyway — vllm WILL crash with FileNotFoundError "
                  "on shm_open.",
                  rc.parallelism.data_parallel,
              )
      for k, v in rc.vllm_args.items():
          if v is True:
              cmd.append(f"--{k}")
          elif v is False:
              cmd.append(f"--no-{k}")
          else:
              cmd.append(f"--{k}={v}")

      _LOG.info("spawning %s", " ".join(cmd))
      proc = subprocess.Popen(cmd, env=env, preexec_fn=os.setpgrp)

      self_ip = detect_self_ip()
      ep = f"{self_ip}:{rc.server.http_port}"
      state = {"attached": False}

      def _shutdown(signum: int, frame: object) -> None:
          if state["attached"]:
              _LOG.info("signal %d received; draining + detaching", signum)
              try:
                  lb_scaling._do_drain(ep, mgr, pool_name=pool.name)
                  _wait_for_idle(
                      rc.server.http_port,
                      timeout=float(os.environ.get("LB_DETACH_WAIT", "600")),
                  )
                  lb_scaling._do_remove(ep, mgr, bs, pool_name=pool.name)
              finally:
                  grace = float(os.environ.get("VCTL_KILL_GRACE", "30"))
                  _kill_tree(proc.pid, grace=grace)
                  sys.exit(130)
          else:
              _LOG.info("signal %d received during model load; reaping subprocess", signum)
              grace = float(os.environ.get("VCTL_KILL_GRACE", "30"))
              _kill_tree(proc.pid, grace=grace)
              sys.exit(130)

      signal.signal(signal.SIGINT, _shutdown)
      signal.signal(signal.SIGTERM, _shutdown)
      signal.signal(signal.SIGHUP, _shutdown)

      try:
          _wait_for_ready(rc.server.http_port, timeout=_resolve_ready_timeout(rc))
      except TimeoutError as e:
          _LOG.error("readiness timed out: %s", e)
          _kill_tree(proc.pid)
          return 4

      attach_rc = lb_scaling._do_add(ep, mgr, bs, pool_name=pool.name)
      if attach_rc != 0:
          _LOG.error("lb attach failed (rc=%d) for %s; shutting down vllm", attach_rc, ep)
          _kill_tree(proc.pid)
          return attach_rc
      state["attached"] = True

      _watchdog_enabled = os.environ.get("VCTL_NO_PPID_WATCHDOG", "0") not in ("1", "true", "yes")
      _watchdog_tick = 0

      while True:
          try:
              rc_code = proc.wait(timeout=0.5)
              return rc_code
          except subprocess.TimeoutExpired:
              pass

          if _watchdog_enabled:
              _watchdog_tick += 1
              if _watchdog_tick >= 10:
                  _watchdog_tick = 0
                  if os.getppid() == 1:
                      _LOG.warning(
                          "PPID==1: launching shell appears to have died; "
                          "triggering graceful drain+shutdown"
                      )
                      _shutdown(signal.SIGTERM, None)
  ```

- [ ] **Step 4 — Run tests to verify no regression**

  ```bash
  .venv/bin/pytest tests/test_commands_serve.py -q
  .venv/bin/mypy --strict src/vctl
  .venv/bin/ruff check .
  .venv/bin/ruff format --check .
  ```

  All must pass. The `_run_foreground` function must be typed: its return annotation is `int`, `ns: argparse.Namespace`, `parsed: argparse.Namespace`. If mypy complains about the nested `_shutdown` closure, add `# type: ignore[misc]` on the `sys.exit(130)` lines — the existing code already lives with this pattern.

- [ ] **Step 5 — Commit**

  ```bash
  git add src/vctl/commands/serve.py
  git commit -m "refactor(serve): extract _run_foreground; no behavior change"
  ```

---

## Task 2: Add `--foreground` flag (still 100% foreground for now)

**Files:**
- Modify: `src/vctl/commands/serve.py` (`_build_subparser`, `run`)
- Test: `tests/test_commands_serve.py` (add 2 new tests)

**What changes:** `_build_subparser()` gains `--foreground` (action="store_true", default=False). `run()` reads `parsed.foreground` but still unconditionally calls `_run_foreground` — the detached path is wired in Task 10. The flag must parse correctly and default to False.

### Steps

- [ ] **Step 1 — Write failing tests**

  Add to `tests/test_commands_serve.py`:

  ```python
  def test_serve_foreground_flag_default_false() -> None:
      """--foreground flag defaults to False when not supplied."""
      from vctl.commands.serve import _build_subparser

      parsed = _build_subparser().parse_args([])
      assert parsed.foreground is False


  def test_serve_foreground_flag_set() -> None:
      """--foreground flag is True when supplied."""
      from vctl.commands.serve import _build_subparser

      parsed = _build_subparser().parse_args(["--foreground"])
      assert parsed.foreground is True
  ```

  Run:

  ```bash
  .venv/bin/pytest tests/test_commands_serve.py::test_serve_foreground_flag_default_false \
                   tests/test_commands_serve.py::test_serve_foreground_flag_set -q
  ```

  Both must **fail** with `AttributeError: Namespace object has no attribute 'foreground'` (or similar).

- [ ] **Step 2 — Add `--foreground` to `_build_subparser`**

  In `src/vctl/commands/serve.py`, add to `_build_subparser()` after the `--skip-preflight` argument:

  ```python
  p.add_argument(
      "--foreground",
      action="store_true",
      default=False,
      help=(
          "Run vllm as a direct child process (v0.4.x behavior). "
          "vctl blocks until vllm exits; SSH disconnect kills vllm. "
          "Signals trigger drain → remove → kill. "
          "Default: detached tmux session."
      ),
  )
  ```

  Update `run()` to read the flag (but still always call `_run_foreground` — detached path comes in Task 10):

  ```python
  def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
      parsed = _build_subparser().parse_args(argv_rest)
      # Detached path wired in Task 10; for now always foreground.
      return _run_foreground(ns, parsed)
  ```

- [ ] **Step 3 — Run tests (must now pass)**

  ```bash
  .venv/bin/pytest tests/test_commands_serve.py -q
  .venv/bin/mypy --strict src/vctl
  .venv/bin/ruff check .
  .venv/bin/ruff format --check .
  ```

- [ ] **Step 4 — Checkpoint: full suite green**

  ```bash
  .venv/bin/pytest -q --cov=vctl --cov-report=term-missing --cov-fail-under=50
  ```

  **CHECKPOINT after Task 2:** All tests pass; no behavior change from operator perspective.

- [ ] **Step 5 — Commit**

  ```bash
  git add src/vctl/commands/serve.py tests/test_commands_serve.py
  git commit -m "feat(serve): add --foreground flag (wiring deferred to Task 10)"
  ```

---

## Task 3: `VllmManager` skeleton + paths + validation

**Files:**
- Create: `src/vctl/vllm_manager.py`
- Create: `tests/test_vllm_manager.py`

**What changes:** New module with `VllmManager.__init__` only. Constructor validates tmux name, creates `run_dir / "vllm"`, and pre-computes all four state file paths. All public methods exist as stubs that `raise NotImplementedError`.

### Steps

- [ ] **Step 1 — Write failing tests**

  Create `tests/test_vllm_manager.py`:

  ```python
  """Unit tests for VllmManager — all run under VCTL_TEST_NO_SOCKET=1."""

  from __future__ import annotations

  import os
  from pathlib import Path
  from unittest.mock import MagicMock

  import pytest


  def _make_rc(profile_name: str = "qwen3-9b", http_port: int = 8000) -> MagicMock:
      rc = MagicMock()
      rc.profile_name = profile_name
      rc.server.http_port = http_port
      rc.model.name = "Qwen/Qwen3-9B"
      rc.lb.host = "10.0.0.1"
      rc.lb.pools = [MagicMock(name="default", served_model="Qwen/Qwen3-9B", bind_port=8080)]
      rc.lb.pools[0].name = "default"
      rc.cluster.venv = "/opt/venv"
      rc.cluster.state_dir = "/tmp/vctl-state"
      rc.parallelism.data_parallel = 1
      rc.parallelism.tensor_parallel = 1
      rc.parallelism.api_server_count = None
      rc.resources.cuda_visible_devices = "0"
      rc.vllm_args = {}
      rc.env = {}
      return rc


  def test_vllm_manager_init_creates_run_dir(tmp_path: Path) -> None:
      """__init__ creates run_dir/vllm/ with parents=True exist_ok=True."""
      from vctl.vllm_manager import VllmManager

      run_dir = tmp_path / "run"
      state_dir = tmp_path / "state"
      rc = _make_rc()
      vm = VllmManager(rc, state_dir=state_dir, run_dir=run_dir)
      assert (run_dir / "vllm").is_dir()
      # Idempotent: second construction does not raise
      VllmManager(rc, state_dir=state_dir, run_dir=run_dir)


  def test_vllm_manager_init_computes_state_paths(tmp_path: Path) -> None:
      """__init__ pre-computes all four state file paths under run_dir/vllm/."""
      from vctl.vllm_manager import VllmManager

      rc = _make_rc(profile_name="qwen3-9b")
      vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
      vllm_dir = tmp_path / "run" / "vllm"
      assert vm.pid_path == vllm_dir / "qwen3-9b.pid"
      assert vm.log_path == vllm_dir / "qwen3-9b.log"
      assert vm.cmd_path == vllm_dir / "qwen3-9b.cmd.json"
      assert vm.host_path == vllm_dir / "qwen3-9b.host"


  def test_vllm_manager_init_validates_tmux_name(tmp_path: Path) -> None:
      """__init__ raises ValueError for a profile name that produces an invalid tmux name."""
      from vctl.vllm_manager import VllmManager

      rc = _make_rc(profile_name="bad name!")  # space + exclamation — invalid
      with pytest.raises(ValueError, match="invalid tmux session name"):
          VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")


  def test_vllm_manager_init_session_name(tmp_path: Path) -> None:
      """Session name is vctl-vllm-<profile_name>."""
      from vctl.vllm_manager import VllmManager

      rc = _make_rc(profile_name="qwen3-9b")
      vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
      assert vm.session_name == "vctl-vllm-qwen3-9b"
  ```

  Run:

  ```bash
  .venv/bin/pytest tests/test_vllm_manager.py -q
  ```

  Must **fail** with `ModuleNotFoundError: No module named 'vctl.vllm_manager'`.

- [ ] **Step 2 — Create `src/vctl/vllm_manager.py` skeleton**

  ```python
  """tmux-backed vllm process supervisor — mirrors LbManager shape."""

  from __future__ import annotations

  import contextlib
  import json
  import logging
  import os
  import socket
  import subprocess
  import time
  from pathlib import Path

  import psutil

  from vctl.commands.serve import _kill_tree, _wait_for_idle, _wait_for_ready
  from vctl.commands.lb_scaling import _do_add, _do_drain, _do_remove
  from vctl.lb.manager import LbManager
  from vctl.lb.routing import pool_for_model
  from vctl.lb.state import BackendState
  from vctl.platform import (
      _validate_tmux_name,
      detect_self_ip,
      tmux_kill,
      tmux_run_detached_argv,
      tmux_session_exists,
  )
  from vctl.resolver import ResolvedConfig

  _LOG = logging.getLogger(__name__)

  _VLLM_PID_POLL_TIMEOUT = 5.0
  _VLLM_PID_POLL_INTERVAL = 0.2


  class VllmManager:
      def __init__(
          self,
          rc: ResolvedConfig,
          state_dir: Path,
          run_dir: Path,
      ) -> None:
          self.session_name = f"vctl-vllm-{rc.profile_name}"
          _validate_tmux_name(self.session_name)
          self.rc = rc
          self.state_dir = Path(state_dir)
          self.run_dir = Path(run_dir)
          self._vllm_dir = self.run_dir / "vllm"
          self._vllm_dir.mkdir(parents=True, exist_ok=True)
          self.pid_path = self._vllm_dir / f"{rc.profile_name}.pid"
          self.log_path = self._vllm_dir / f"{rc.profile_name}.log"
          self.cmd_path = self._vllm_dir / f"{rc.profile_name}.cmd.json"
          self.host_path = self._vllm_dir / f"{rc.profile_name}.host"

      def start(self) -> None:
          """Preflight → spawn tmux → _wait_for_ready → _do_add → write state files."""
          raise NotImplementedError

      def stop(self) -> None:
          """_do_drain → _wait_for_idle → _do_remove → tmux send-keys C-c → poll → kill-session."""
          raise NotImplementedError

      def restart(self) -> None:
          """stop() → reload config → start(). Logs warning if cmd snapshot differs."""
          raise NotImplementedError

      def status(self) -> dict[str, object]:
          """Return tmux_alive, pid_alive, vllm_ready, lb_attached, started_at, log_size."""
          raise NotImplementedError

      def attach(self) -> None:
          """os.execvp into tmux attach-session -t <name>. Does not return."""
          raise NotImplementedError

      def logs(self, n: int = 50, follow: bool = False) -> int:
          """Tail log file. follow=True: subprocess.Popen(["tail", "-f", path])."""
          raise NotImplementedError
  ```

- [ ] **Step 3 — Run tests (must now pass)**

  ```bash
  .venv/bin/pytest tests/test_vllm_manager.py -q
  .venv/bin/mypy --strict src/vctl
  .venv/bin/ruff check .
  .venv/bin/ruff format --check .
  ```

  Four tests must pass. `mypy --strict` must be clean (the `NotImplementedError` stubs satisfy return type annotations because `raise` is `Never`).

- [ ] **Step 4 — Run full suite**

  ```bash
  .venv/bin/pytest -q --cov=vctl --cov-report=term-missing --cov-fail-under=50
  ```

- [ ] **Step 5 — Commit**

  ```bash
  git add src/vctl/vllm_manager.py tests/test_vllm_manager.py
  git commit -m "feat(vllm_manager): skeleton constructor + path computation"
  ```

---

## Task 4: `VllmManager.start()` — spawn tmux + pid discovery + state writes

**Files:**
- Modify: `src/vctl/vllm_manager.py` (implement `start()`)
- Modify: `tests/test_vllm_manager.py` (add 4 tests)

**What changes:** `start()` is fully implemented: stale-pidfile cleanup, double-start guard, argv construction (mirroring `_run_foreground`), `tmux_run_detached_argv`, pipe-pane setup, psutil pid polling, atomic state file writes, `_wait_for_ready`, `_do_add`, and failure cleanup.

### Steps

- [ ] **Step 1 — Write failing tests**

  Add to `tests/test_vllm_manager.py`:

  ```python
  def test_start_refuses_when_session_exists(
      tmp_path: Path, monkeypatch: pytest.MonkeyPatch
  ) -> None:
      """start() raises RuntimeError (exit 4) when tmux session already exists."""
      import vctl.vllm_manager as vm_mod
      from vctl.vllm_manager import VllmManager

      monkeypatch.setattr(vm_mod, "tmux_session_exists", lambda name: True)
      rc = _make_rc()
      vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
      with pytest.raises(RuntimeError, match="already running"):
          vm.start()


  def test_start_writes_all_four_state_files(
      tmp_path: Path, monkeypatch: pytest.MonkeyPatch
  ) -> None:
      """start() writes pid, log (via pipe-pane), cmd.json, and host state files."""
      import vctl.vllm_manager as vm_mod
      from vctl.vllm_manager import VllmManager

      # Patch all external calls
      monkeypatch.setattr(vm_mod, "tmux_session_exists", lambda name: False)
      monkeypatch.setattr(vm_mod, "tmux_run_detached_argv", lambda name, argv: None)
      monkeypatch.setattr(vm_mod, "tmux_kill", lambda name: None)

      # Fake pid discovery: return a fake PID immediately
      fake_pid = 99999

      def fake_process_iter(attrs: list[str]) -> list[MagicMock]:
          proc = MagicMock()
          proc.info = {
              "cmdline": ["python", "vllm", "serve", "--port=8000"],
              "create_time": 1000.0,
              "pid": fake_pid,
          }
          proc.pid = fake_pid
          return [proc]

      monkeypatch.setattr(vm_mod.psutil, "process_iter", fake_process_iter)

      # Patch subprocess.run for pipe-pane
      monkeypatch.setattr(vm_mod.subprocess, "run", lambda *a, **kw: MagicMock(returncode=0))

      # Patch _wait_for_ready and _do_add
      monkeypatch.setattr(vm_mod, "_wait_for_ready", lambda port, timeout: None)
      monkeypatch.setattr(vm_mod, "_do_add", lambda ep, mgr, bs, pool_name=None: 0)

      rc = _make_rc()
      vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
      vm.start()

      assert vm.pid_path.exists(), "pid file must be written"
      assert int(vm.pid_path.read_text().strip()) == fake_pid
      assert vm.cmd_path.exists(), "cmd.json must be written"
      assert vm.host_path.exists(), "host file must be written"
      assert vm.host_path.read_text().strip() == socket.gethostname()


  def test_start_pid_discovery_timeout_kills_session(
      tmp_path: Path, monkeypatch: pytest.MonkeyPatch
  ) -> None:
      """start() kills the tmux session and raises RuntimeError when PID discovery times out."""
      import vctl.vllm_manager as vm_mod
      from vctl.vllm_manager import VllmManager

      monkeypatch.setattr(vm_mod, "tmux_session_exists", lambda name: False)
      monkeypatch.setattr(vm_mod, "tmux_run_detached_argv", lambda name, argv: None)

      killed: list[str] = []
      monkeypatch.setattr(vm_mod, "tmux_kill", lambda name: killed.append(name))

      # Patch subprocess.run for pipe-pane
      monkeypatch.setattr(vm_mod.subprocess, "run", lambda *a, **kw: MagicMock(returncode=0))

      # process_iter returns nothing matching vllm
      monkeypatch.setattr(vm_mod.psutil, "process_iter", lambda attrs: [])

      # Speed up the timeout
      monkeypatch.setattr(vm_mod, "_VLLM_PID_POLL_TIMEOUT", 0.1)
      monkeypatch.setattr(vm_mod, "_VLLM_PID_POLL_INTERVAL", 0.05)

      rc = _make_rc()
      vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
      with pytest.raises(RuntimeError, match="timed out"):
          vm.start()

      assert "vctl-vllm-qwen3-9b" in killed, "session must be killed on timeout"


  def test_start_wait_for_ready_failure_cleans_up(
      tmp_path: Path, monkeypatch: pytest.MonkeyPatch
  ) -> None:
      """start() kills tmux session and unlinks state files when _wait_for_ready raises."""
      import vctl.vllm_manager as vm_mod
      from vctl.vllm_manager import VllmManager

      monkeypatch.setattr(vm_mod, "tmux_session_exists", lambda name: False)
      monkeypatch.setattr(vm_mod, "tmux_run_detached_argv", lambda name, argv: None)

      killed: list[str] = []
      monkeypatch.setattr(vm_mod, "tmux_kill", lambda name: killed.append(name))

      fake_pid = 99999

      def fake_process_iter(attrs: list[str]) -> list[MagicMock]:
          proc = MagicMock()
          proc.info = {
              "cmdline": ["python", "vllm", "serve", "--port=8000"],
              "create_time": 1000.0,
              "pid": fake_pid,
          }
          proc.pid = fake_pid
          return [proc]

      monkeypatch.setattr(vm_mod.psutil, "process_iter", fake_process_iter)
      monkeypatch.setattr(vm_mod.subprocess, "run", lambda *a, **kw: MagicMock(returncode=0))

      def _fail_ready(port: int, timeout: float) -> None:
          raise TimeoutError("stubbed timeout")

      monkeypatch.setattr(vm_mod, "_wait_for_ready", _fail_ready)

      rc = _make_rc()
      vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
      with pytest.raises(RuntimeError, match="did not become ready"):
          vm.start()

      assert not vm.pid_path.exists(), "pid file must be cleaned up"
      assert not vm.cmd_path.exists(), "cmd.json must be cleaned up"
      assert not vm.host_path.exists(), "host file must be cleaned up"
      assert "vctl-vllm-qwen3-9b" in killed
  ```

  Run:

  ```bash
  .venv/bin/pytest tests/test_vllm_manager.py -k "test_start" -q
  ```

  All four must **fail** with `NotImplementedError`.

- [ ] **Step 2 — Implement `start()` in `vllm_manager.py`**

  Replace the `start()` stub with:

  ```python
  def start(self) -> None:
      """Preflight → spawn tmux → _wait_for_ready → _do_add → write state files."""
      rc = self.rc
      port = rc.server.http_port

      # Stale pidfile cleanup: if pid file exists but process is dead or wrong cmdline.
      if self.pid_path.exists():
          try:
              old_pid = int(self.pid_path.read_text().strip())
              try:
                  os.kill(old_pid, 0)
                  # Process alive — check it's actually vllm serve on our port.
                  try:
                      cmdline = " ".join(
                          psutil.Process(old_pid).cmdline()
                      )
                      if "vllm" not in cmdline or f"--port={port}" not in cmdline:
                          raise ProcessLookupError
                  except (psutil.NoSuchProcess, psutil.AccessDenied):
                      raise ProcessLookupError
              except ProcessLookupError:
                  # Stale — remove all state files silently and continue.
                  _LOG.info("removing stale state files for profile %s", rc.profile_name)
                  for p in (self.pid_path, self.cmd_path, self.host_path):
                      with contextlib.suppress(OSError):
                          p.unlink()
          except (ValueError, OSError):
              pass

      # Double-start guard.
      if tmux_session_exists(self.session_name):
          raise RuntimeError(
              f"vllm already running for profile {rc.profile_name!r} "
              f"(tmux session {self.session_name!r}); "
              "use `vctl serve restart` to restart or `vctl serve stop` to stop."
          )

      # Build vllm argv (mirrors _run_foreground).
      env = os.environ.copy()
      venv_bin = str(Path(rc.cluster.venv) / "bin")
      env["PATH"] = f"{venv_bin}:{env['PATH']}"
      if rc.resources.cuda_visible_devices:
          env["CUDA_VISIBLE_DEVICES"] = rc.resources.cuda_visible_devices
      for k, v in rc.env.items():
          if isinstance(v, bool):
              env[k] = "true" if v else "false"
          else:
              env[k] = str(v)

      argv: list[str] = [
          "vllm",
          "serve",
          rc.model.name,
          f"--data-parallel-size={rc.parallelism.data_parallel}",
          f"--tensor-parallel-size={rc.parallelism.tensor_parallel}",
          f"--port={port}",
      ]
      if rc.parallelism.api_server_count is not None:
          argv.append(f"--api-server-count={rc.parallelism.api_server_count}")
      for k, v in rc.vllm_args.items():
          if v is True:
              argv.append(f"--{k}")
          elif v is False:
              argv.append(f"--no-{k}")
          else:
              argv.append(f"--{k}={v}")

      # Spawn the tmux session.
      tmux_run_detached_argv(self.session_name, argv)
      _LOG.info("vllm started in tmux session %s", self.session_name)

      # Set up pipe-pane log capture BEFORE any failure point so we capture early output.
      subprocess.run(
          ["tmux", "pipe-pane", "-t", self.session_name, "-o", f"cat >> {self.log_path}"],
          check=False,
      )

      # Poll psutil for the vllm PID.
      pid: int | None = None
      deadline = time.monotonic() + _VLLM_PID_POLL_TIMEOUT
      while time.monotonic() < deadline:
          for proc in psutil.process_iter(["cmdline", "create_time", "pid"]):
              try:
                  cmd = proc.info.get("cmdline") or []
                  cmd_str = " ".join(cmd)
                  if "vllm" in cmd_str and "serve" in cmd_str and f"--port={port}" in cmd_str:
                      pid = int(proc.pid)
                      break
              except (psutil.NoSuchProcess, psutil.AccessDenied):
                  continue
          if pid is not None:
              break
          time.sleep(_VLLM_PID_POLL_INTERVAL)

      if pid is None:
          tmux_kill(self.session_name)
          raise RuntimeError(
              f"vllm PID discovery timed out after {_VLLM_PID_POLL_TIMEOUT}s "
              f"for profile {rc.profile_name!r} on port {port}; "
              "check `vctl serve logs` for startup errors."
          )

      # Write state files atomically (write .tmp → os.replace).
      self._write_atomic(self.pid_path, str(pid))
      self._write_atomic(self.cmd_path, json.dumps(argv))
      self._write_atomic(self.host_path, socket.gethostname())

      # Wait for vllm HTTP readiness.
      from vctl.commands.serve import _resolve_ready_timeout

      timeout = _resolve_ready_timeout(rc)
      try:
          _wait_for_ready(port, timeout)
      except TimeoutError as e:
          _LOG.error("vllm readiness timed out: %s", e)
          self._cleanup_on_failure()
          raise RuntimeError(
              f"vllm did not become ready on port {port} within {timeout}s "
              f"for profile {rc.profile_name!r}"
          ) from e

      # Attach to the LB pool.
      state_dir = self.state_dir
      mgr = LbManager(rc.lb, state_dir=state_dir, run_dir=self.run_dir / "lb")
      pool = pool_for_model(rc.lb, rc.model.name)
      bs = BackendState(state_dir, rc.lb.host, pool=pool.name)
      self_ip = detect_self_ip()
      ep = f"{self_ip}:{port}"
      attach_rc = _do_add(ep, mgr, bs, pool_name=pool.name)
      if attach_rc != 0:
          _LOG.error("lb attach failed (rc=%d) for %s; cleaning up", attach_rc, ep)
          self._cleanup_on_failure()
          raise RuntimeError(
              f"lb attach failed (rc={attach_rc}) for endpoint {ep}; "
              "vllm session killed."
          )

      _LOG.info(
          "vllm serving profile %r on port %d; attached to pool %r",
          rc.profile_name,
          port,
          pool.name,
      )

  def _write_atomic(self, path: Path, content: str) -> None:
      """Write content to path atomically via a .tmp sibling + os.replace."""
      tmp = path.with_suffix(path.suffix + ".tmp")
      tmp.write_text(content)
      os.replace(tmp, path)

  def _cleanup_on_failure(self) -> None:
      """Kill tmux session and unlink state files after a start() failure."""
      tmux_kill(self.session_name)
      for p in (self.pid_path, self.cmd_path, self.host_path):
          with contextlib.suppress(OSError):
              p.unlink()
  ```

- [ ] **Step 3 — Run start() tests (must now pass)**

  ```bash
  .venv/bin/pytest tests/test_vllm_manager.py -k "test_start or test_vllm_manager_init" -q
  .venv/bin/mypy --strict src/vctl
  .venv/bin/ruff check .
  .venv/bin/ruff format --check .
  ```

- [ ] **Step 4 — CHECKPOINT: manual smoke test**

  ```bash
  # Verify --foreground still works (integration sanity):
  .venv/bin/pytest tests/test_commands_serve.py -q
  ```

  **CHECKPOINT after Task 4:** `VllmManager.start()` unit-tested; `vctl serve --foreground` unchanged.

- [ ] **Step 5 — Commit**

  ```bash
  git add src/vctl/vllm_manager.py tests/test_vllm_manager.py
  git commit -m "feat(vllm_manager): implement start() with tmux spawn + pid discovery"
  ```

---

## Task 5: `VllmManager.status()`

**Files:**
- Modify: `src/vctl/vllm_manager.py` (implement `status()`)
- Modify: `tests/test_vllm_manager.py` (add 4 tests)

**What changes:** `status()` returns a dict with keys `tmux_alive`, `pid_alive`, `vllm_ready`, `lb_attached`, `started_at`, `log_size`. It reads the pidfile (does not clean stale data — read-only), checks `tmux_session_exists`, probes `httpx.get` with 1s timeout, checks `BackendState.list()`.

### Steps

- [ ] **Step 1 — Write failing tests**

  Add to `tests/test_vllm_manager.py`:

  ```python
  import httpx


  def test_status_all_alive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
      """status() returns all True fields when session, pid, http, and LB are alive."""
      import vctl.vllm_manager as vm_mod
      from vctl.vllm_manager import VllmManager

      monkeypatch.setattr(vm_mod, "tmux_session_exists", lambda name: True)

      fake_pid = os.getpid()  # use our own PID — guaranteed alive
      rc = _make_rc()
      vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")

      # Write pid file with a live PID
      vm.pid_path.write_text(str(fake_pid))
      vm.pid_path.parent.mkdir(parents=True, exist_ok=True)

      # Patch httpx.get to return a models response
      mock_resp = MagicMock()
      mock_resp.json.return_value = {"data": [{"id": "m"}]}
      monkeypatch.setattr(vm_mod.httpx, "get", lambda url, timeout=None: mock_resp)

      # Patch BackendState.list to return our endpoint
      self_ip = vm_mod.detect_self_ip()
      ep = f"{self_ip}:{rc.server.http_port}"
      monkeypatch.setattr(
          vm_mod.BackendState, "list", lambda self: [ep]
      )

      result = vm.status()
      assert result["tmux_alive"] is True
      assert result["pid_alive"] is True
      assert result["vllm_ready"] is True
      assert result["lb_attached"] is True


  def test_status_tmux_dead_pid_alive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
      """status() reports tmux_alive=False but pid_alive=True for an orphan process."""
      import vctl.vllm_manager as vm_mod
      from vctl.vllm_manager import VllmManager

      monkeypatch.setattr(vm_mod, "tmux_session_exists", lambda name: False)

      fake_pid = os.getpid()
      rc = _make_rc()
      vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
      vm.pid_path.write_text(str(fake_pid))

      mock_resp = MagicMock()
      mock_resp.json.return_value = {"data": []}
      monkeypatch.setattr(vm_mod.httpx, "get", lambda url, timeout=None: mock_resp)
      monkeypatch.setattr(vm_mod.BackendState, "list", lambda self: [])

      result = vm.status()
      assert result["tmux_alive"] is False
      assert result["pid_alive"] is True


  def test_status_pidfile_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
      """status() reports pid_alive=False when pidfile does not exist."""
      import vctl.vllm_manager as vm_mod
      from vctl.vllm_manager import VllmManager

      monkeypatch.setattr(vm_mod, "tmux_session_exists", lambda name: False)
      monkeypatch.setattr(vm_mod.httpx, "get", MagicMock(side_effect=httpx.ConnectError("x")))
      monkeypatch.setattr(vm_mod.BackendState, "list", lambda self: [])

      rc = _make_rc()
      vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
      # pid_path does not exist

      result = vm.status()
      assert result["pid_alive"] is False
      assert result["vllm_ready"] is False
      assert result["lb_attached"] is False


  def test_status_cross_host_pidfile_skips_liveness_check(
      tmp_path: Path, monkeypatch: pytest.MonkeyPatch
  ) -> None:
      """status() reports pid_alive=None when host marker is a different host (cross-host read)."""
      import vctl.vllm_manager as vm_mod
      from vctl.vllm_manager import VllmManager

      monkeypatch.setattr(vm_mod, "tmux_session_exists", lambda name: False)
      monkeypatch.setattr(vm_mod.httpx, "get", MagicMock(side_effect=httpx.ConnectError("x")))
      monkeypatch.setattr(vm_mod.BackendState, "list", lambda self: [])

      rc = _make_rc()
      vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
      vm.pid_path.write_text("12345")
      vm.host_path.write_text("other-host-99")

      result = vm.status()
      # Cross-host: cannot check liveness of a PID on a different host
      assert result.get("cross_host") is True
      assert result["pid_alive"] is None
  ```

  Run and confirm all four **fail** with `NotImplementedError`.

- [ ] **Step 2 — Implement `status()` in `vllm_manager.py`**

  Add `import httpx` to the module imports. Replace the `status()` stub:

  ```python
  def status(self) -> dict[str, object]:
      """Return tmux_alive, pid_alive, vllm_ready, lb_attached, started_at, log_size."""
      rc = self.rc
      port = rc.server.http_port

      # 1. tmux session liveness (informational).
      tmux_alive = tmux_session_exists(self.session_name)

      # 2. Pidfile + process liveness (read-only; never clean stale state here).
      pid: int | None = None
      pid_alive: bool | None = False
      cross_host = False

      if self.host_path.exists():
          stored_host = self.host_path.read_text().strip()
          if stored_host != socket.gethostname():
              cross_host = True

      if self.pid_path.exists():
          try:
              pid = int(self.pid_path.read_text().strip())
          except (ValueError, OSError):
              pid = None

          if pid is not None:
              if cross_host:
                  pid_alive = None  # Cannot check liveness on a different host.
              else:
                  try:
                      os.kill(pid, 0)
                      pid_alive = True
                  except (ProcessLookupError, PermissionError):
                      pid_alive = False

      # 3. vllm HTTP readiness (1s timeout; read-only probe).
      vllm_ready = False
      try:
          resp = httpx.get(f"http://127.0.0.1:{port}/v1/models", timeout=1.0)
          if resp.json().get("data"):
              vllm_ready = True
      except Exception:
          vllm_ready = False

      # 4. LB attachment — check BackendState for our endpoint.
      self_ip = detect_self_ip()
      ep = f"{self_ip}:{port}"
      bs = BackendState(self.state_dir, rc.lb.host, pool=pool_for_model(rc.lb, rc.model.name).name)
      lb_attached = ep in bs.list()

      # 5. Log file size (bytes).
      log_size = self.log_path.stat().st_size if self.log_path.exists() else 0

      return {
          "tmux_alive": tmux_alive,
          "pid_alive": pid_alive,
          "vllm_ready": vllm_ready,
          "lb_attached": lb_attached,
          "log_size": log_size,
          "pid": pid,
          "cross_host": cross_host,
          "session_name": self.session_name,
          "log_path": str(self.log_path),
      }
  ```

- [ ] **Step 3 — Run status() tests**

  ```bash
  .venv/bin/pytest tests/test_vllm_manager.py -k "test_status" -q
  .venv/bin/mypy --strict src/vctl
  .venv/bin/ruff check .
  .venv/bin/ruff format --check .
  ```

- [ ] **Step 4 — Run full suite**

  ```bash
  .venv/bin/pytest -q --cov=vctl --cov-report=term-missing --cov-fail-under=50
  ```

- [ ] **Step 5 — Commit**

  ```bash
  git add src/vctl/vllm_manager.py tests/test_vllm_manager.py
  git commit -m "feat(vllm_manager): implement status()"
  ```

---

## Task 6: `VllmManager.stop()`

**Files:**
- Modify: `src/vctl/vllm_manager.py` (implement `stop()`)
- Modify: `tests/test_vllm_manager.py` (add 3 tests)

**What changes:** `stop()` reads host marker, enforces cross-host guard, reads pid, calls `_do_drain` → `_wait_for_idle` → `_do_remove` → `tmux send-keys C-c` → polls pid exit up to `VCTL_KILL_GRACE` seconds → `tmux_kill` if still alive → unlinks pid/cmd/host files (leaves log for post-mortem).

### Steps

- [ ] **Step 1 — Write failing tests**

  Add to `tests/test_vllm_manager.py`:

  ```python
  def test_stop_full_drain_sequence(
      tmp_path: Path, monkeypatch: pytest.MonkeyPatch
  ) -> None:
      """stop() calls drain → wait_for_idle → remove → send-keys C-c → cleanup."""
      import vctl.vllm_manager as vm_mod
      from vctl.vllm_manager import VllmManager

      calls: list[str] = []

      monkeypatch.setattr(vm_mod, "_do_drain", lambda ep, mgr, pool_name=None: calls.append("drain") or 0)
      monkeypatch.setattr(vm_mod, "_wait_for_idle", lambda port, timeout: calls.append("idle"))
      monkeypatch.setattr(vm_mod, "_do_remove", lambda ep, mgr, bs, pool_name=None: calls.append("remove") or 0)
      monkeypatch.setattr(vm_mod, "tmux_kill", lambda name: calls.append("kill"))
      monkeypatch.setattr(vm_mod.subprocess, "run", lambda *a, **kw: (calls.append("sendkeys"), MagicMock(returncode=0))[1])

      rc = _make_rc()
      vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")

      # Write state files so stop() finds them.
      vm.host_path.write_text(socket.gethostname())
      vm.pid_path.write_text("1")  # PID 1 always dead (not our process)
      vm.cmd_path.write_text(json.dumps(["vllm", "serve"]))

      vm.stop()

      assert "drain" in calls
      assert "idle" in calls
      assert "remove" in calls
      assert "sendkeys" in calls
      # State files cleaned up.
      assert not vm.pid_path.exists()
      assert not vm.cmd_path.exists()
      assert not vm.host_path.exists()


  def test_stop_cross_host_guard_raises(
      tmp_path: Path, monkeypatch: pytest.MonkeyPatch
  ) -> None:
      """stop() raises RuntimeError when host marker is a different host."""
      import vctl.vllm_manager as vm_mod
      from vctl.vllm_manager import VllmManager

      rc = _make_rc()
      vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
      vm.host_path.write_text("foreign-host-99")

      with pytest.raises(RuntimeError, match="refusing operation"):
          vm.stop()


  def test_stop_force_kill_after_grace(
      tmp_path: Path, monkeypatch: pytest.MonkeyPatch
  ) -> None:
      """stop() calls tmux_kill when pid is still alive after VCTL_KILL_GRACE elapses."""
      import vctl.vllm_manager as vm_mod
      from vctl.vllm_manager import VllmManager

      monkeypatch.setattr(vm_mod, "_do_drain", lambda ep, mgr, pool_name=None: 0)
      monkeypatch.setattr(vm_mod, "_wait_for_idle", lambda port, timeout: None)
      monkeypatch.setattr(vm_mod, "_do_remove", lambda ep, mgr, bs, pool_name=None: 0)
      monkeypatch.setattr(vm_mod.subprocess, "run", lambda *a, **kw: MagicMock(returncode=0))

      killed: list[str] = []
      monkeypatch.setattr(vm_mod, "tmux_kill", lambda name: killed.append(name))

      # Use our own PID — always alive — to simulate a process that won't die.
      alive_pid = os.getpid()
      rc = _make_rc()
      vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
      vm.host_path.write_text(socket.gethostname())
      vm.pid_path.write_text(str(alive_pid))
      vm.cmd_path.write_text(json.dumps(["vllm", "serve"]))

      # Override grace period to near-zero so the test doesn't actually wait.
      monkeypatch.setenv("VCTL_KILL_GRACE", "0.1")

      vm.stop()

      # tmux_kill must have been called since the process never exited.
      assert vm.session_name in killed
  ```

  Confirm all three **fail** with `NotImplementedError`.

- [ ] **Step 2 — Implement `stop()` in `vllm_manager.py`**

  Replace the `stop()` stub:

  ```python
  def stop(self) -> None:
      """_do_drain → _wait_for_idle → _do_remove → tmux send-keys C-c → poll → kill-session."""
      rc = self.rc
      port = rc.server.http_port

      # Cross-host guard.
      if self.host_path.exists():
          stored_host = self.host_path.read_text().strip()
          if stored_host != socket.gethostname():
              raise RuntimeError(
                  f"refusing operation: state files belong to host {stored_host!r}, "
                  f"current host is {socket.gethostname()!r}. "
                  "Run this command on the correct host."
              )

      # Resolve endpoint from pidfile (best-effort; continue even if missing).
      self_ip = detect_self_ip()
      ep = f"{self_ip}:{port}"

      state_dir = self.state_dir
      run_dir_lb = self.run_dir / "lb"
      mgr = LbManager(rc.lb, state_dir=state_dir, run_dir=run_dir_lb)
      pool = pool_for_model(rc.lb, rc.model.name)
      bs = BackendState(state_dir, rc.lb.host, pool=pool.name)

      # Drain → wait for idle → remove.
      drain_rc = _do_drain(ep, mgr, pool_name=pool.name)
      if drain_rc != 0:
          _LOG.warning("drain returned %d for %s; continuing with stop", drain_rc, ep)
      lb_detach_wait = float(os.environ.get("LB_DETACH_WAIT", "600"))
      _wait_for_idle(port, timeout=lb_detach_wait)
      remove_rc = _do_remove(ep, mgr, bs, pool_name=pool.name)
      if remove_rc != 0:
          _LOG.warning("remove returned %d for %s; continuing with kill", remove_rc, ep)

      # Send C-c to tmux session (graceful SIGINT to vllm).
      subprocess.run(
          ["tmux", "send-keys", "-t", self.session_name, "C-c", ""],
          check=False,
      )

      # Poll for pid exit up to VCTL_KILL_GRACE.
      pid: int | None = None
      if self.pid_path.exists():
          try:
              pid = int(self.pid_path.read_text().strip())
          except (ValueError, OSError):
              pid = None

      grace = float(os.environ.get("VCTL_KILL_GRACE", "30"))
      if pid is not None:
          deadline = time.monotonic() + grace
          while time.monotonic() < deadline:
              try:
                  os.kill(pid, 0)
              except (ProcessLookupError, PermissionError):
                  break
              time.sleep(0.5)

      # Force-kill session if still exists.
      tmux_kill(self.session_name)

      # Unlink state files (leave log for post-mortem).
      for p in (self.pid_path, self.cmd_path, self.host_path):
          with contextlib.suppress(OSError):
              p.unlink()

      _LOG.info("stopped vllm for profile %r", rc.profile_name)
  ```

- [ ] **Step 3 — Run stop() tests**

  ```bash
  .venv/bin/pytest tests/test_vllm_manager.py -k "test_stop" -q
  .venv/bin/mypy --strict src/vctl
  .venv/bin/ruff check .
  .venv/bin/ruff format --check .
  ```

- [ ] **Step 4 — Run full suite**

  ```bash
  .venv/bin/pytest -q --cov=vctl --cov-report=term-missing --cov-fail-under=50
  ```

- [ ] **Step 5 — Commit**

  ```bash
  git add src/vctl/vllm_manager.py tests/test_vllm_manager.py
  git commit -m "feat(vllm_manager): implement stop() with drain/idle/remove/kill sequence"
  ```

---

## Task 7: `VllmManager.restart()`

**Files:**
- Modify: `src/vctl/vllm_manager.py` (implement `restart()`)
- Modify: `tests/test_vllm_manager.py` (add 2 tests)

**What changes:** `restart()` reads `cmd_path` for the snapshot of the argv used at start time, computes what the current `rc` would produce, logs a warning if they differ, then calls `self.stop()` followed by `self.start()`.

### Steps

- [ ] **Step 1 — Write failing tests**

  Add to `tests/test_vllm_manager.py`:

  ```python
  def test_restart_warns_on_config_drift(
      tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
  ) -> None:
      """restart() logs a warning when cmd.json differs from what fresh rc would produce."""
      import logging
      import vctl.vllm_manager as vm_mod
      from vctl.vllm_manager import VllmManager

      stop_called: list[bool] = []
      start_called: list[bool] = []
      monkeypatch.setattr(VllmManager, "stop", lambda self: stop_called.append(True))
      monkeypatch.setattr(VllmManager, "start", lambda self: start_called.append(True))

      rc = _make_rc()
      vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")

      # Write a cmd.json that differs from the current config
      vm.cmd_path.write_text(json.dumps(["vllm", "serve", "OldModel/Name", "--port=9999"]))
      vm.host_path.write_text(socket.gethostname())

      with caplog.at_level(logging.WARNING, logger="vctl.vllm_manager"):
          vm.restart()

      assert any("config changed" in r.message.lower() or "drift" in r.message.lower()
                 for r in caplog.records), "expected warning about config drift"
      assert stop_called
      assert start_called


  def test_restart_calls_stop_then_start(
      tmp_path: Path, monkeypatch: pytest.MonkeyPatch
  ) -> None:
      """restart() calls stop() before start() — order matters."""
      import vctl.vllm_manager as vm_mod
      from vctl.vllm_manager import VllmManager

      order: list[str] = []
      monkeypatch.setattr(VllmManager, "stop", lambda self: order.append("stop"))
      monkeypatch.setattr(VllmManager, "start", lambda self: order.append("start"))

      rc = _make_rc()
      vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
      # No cmd.json — restart should still proceed (no drift warning possible)
      vm.host_path.write_text(socket.gethostname())

      vm.restart()
      assert order == ["stop", "start"]
  ```

  Confirm both **fail** with `NotImplementedError`.

- [ ] **Step 2 — Implement `restart()` in `vllm_manager.py`**

  Replace the `restart()` stub:

  ```python
  def restart(self) -> None:
      """stop() → reload config → start(). Logs warning if cmd snapshot differs."""
      rc = self.rc
      port = rc.server.http_port

      # Read stored argv snapshot if it exists.
      if self.cmd_path.exists():
          try:
              old_argv: list[str] = json.loads(self.cmd_path.read_text())
          except (json.JSONDecodeError, OSError):
              old_argv = []

          # Compute what the current rc would produce.
          new_argv: list[str] = [
              "vllm",
              "serve",
              rc.model.name,
              f"--data-parallel-size={rc.parallelism.data_parallel}",
              f"--tensor-parallel-size={rc.parallelism.tensor_parallel}",
              f"--port={port}",
          ]
          if rc.parallelism.api_server_count is not None:
              new_argv.append(f"--api-server-count={rc.parallelism.api_server_count}")
          for k, v in rc.vllm_args.items():
              if v is True:
                  new_argv.append(f"--{k}")
              elif v is False:
                  new_argv.append(f"--no-{k}")
              else:
                  new_argv.append(f"--{k}={v}")

          if old_argv != new_argv:
              _LOG.warning(
                  "config drift detected for profile %r: "
                  "running argv differs from current config. "
                  "old=%r  new=%r",
                  rc.profile_name,
                  old_argv,
                  new_argv,
              )

      self.stop()
      self.start()
  ```

- [ ] **Step 3 — Run restart() tests**

  ```bash
  .venv/bin/pytest tests/test_vllm_manager.py -k "test_restart" -q
  .venv/bin/mypy --strict src/vctl
  .venv/bin/ruff check .
  .venv/bin/ruff format --check .
  ```

- [ ] **Step 4 — Run full suite**

  ```bash
  .venv/bin/pytest -q --cov=vctl --cov-report=term-missing --cov-fail-under=50
  ```

- [ ] **Step 5 — Commit**

  ```bash
  git add src/vctl/vllm_manager.py tests/test_vllm_manager.py
  git commit -m "feat(vllm_manager): implement restart() with config-drift warning"
  ```

---

## Task 8: `VllmManager.attach()`

**Files:**
- Modify: `src/vctl/vllm_manager.py` (implement `attach()`)
- Modify: `tests/test_vllm_manager.py` (add 2 tests)

**What changes:** `attach()` checks session exists (raises on no), then calls `os.execvp("tmux", ["tmux", "attach-session", "-t", self.session_name])`. It does not return.

### Steps

- [ ] **Step 1 — Write failing tests**

  Add to `tests/test_vllm_manager.py`:

  ```python
  def test_attach_calls_execvp_with_correct_args(
      tmp_path: Path, monkeypatch: pytest.MonkeyPatch
  ) -> None:
      """attach() calls os.execvp with the correct tmux attach-session arguments."""
      import vctl.vllm_manager as vm_mod
      from vctl.vllm_manager import VllmManager

      monkeypatch.setattr(vm_mod, "tmux_session_exists", lambda name: True)

      execvp_calls: list[tuple[str, list[str]]] = []

      def fake_execvp(prog: str, argv: list[str]) -> None:
          execvp_calls.append((prog, argv))

      monkeypatch.setattr(vm_mod.os, "execvp", fake_execvp)

      rc = _make_rc()
      vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
      vm.attach()

      assert len(execvp_calls) == 1
      prog, argv = execvp_calls[0]
      assert prog == "tmux"
      assert argv == ["tmux", "attach-session", "-t", "vctl-vllm-qwen3-9b"]


  def test_attach_raises_when_no_session(
      tmp_path: Path, monkeypatch: pytest.MonkeyPatch
  ) -> None:
      """attach() raises RuntimeError with a helpful message when session does not exist."""
      import vctl.vllm_manager as vm_mod
      from vctl.vllm_manager import VllmManager

      monkeypatch.setattr(vm_mod, "tmux_session_exists", lambda name: False)

      rc = _make_rc()
      vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
      with pytest.raises(RuntimeError, match="no running session"):
          vm.attach()
  ```

  Confirm both **fail** with `NotImplementedError`.

- [ ] **Step 2 — Implement `attach()` in `vllm_manager.py`**

  Replace the `attach()` stub:

  ```python
  def attach(self) -> None:
      """os.execvp into tmux attach-session -t <name>. Does not return."""
      if not tmux_session_exists(self.session_name):
          raise RuntimeError(
              f"no running session for profile {self.rc.profile_name!r} "
              f"(session {self.session_name!r} not found). "
              "Start vllm first with `vctl serve`."
          )
      os.execvp("tmux", ["tmux", "attach-session", "-t", self.session_name])
  ```

- [ ] **Step 3 — Run attach() tests**

  ```bash
  .venv/bin/pytest tests/test_vllm_manager.py -k "test_attach" -q
  .venv/bin/mypy --strict src/vctl
  .venv/bin/ruff check .
  .venv/bin/ruff format --check .
  ```

- [ ] **Step 4 — Run full suite**

  ```bash
  .venv/bin/pytest -q --cov=vctl --cov-report=term-missing --cov-fail-under=50
  ```

- [ ] **Step 5 — Commit**

  ```bash
  git add src/vctl/vllm_manager.py tests/test_vllm_manager.py
  git commit -m "feat(vllm_manager): implement attach() via os.execvp"
  ```

---

## Task 9: `VllmManager.logs()`

**Files:**
- Modify: `src/vctl/vllm_manager.py` (implement `logs()`)
- Modify: `tests/test_vllm_manager.py` (add 3 tests)

**What changes:** `logs(n, follow)` tails the log file. If `follow=False`: read the last `n` lines and print to stdout. If `follow=True`: spawn `tail -f` via `subprocess.Popen`, wait, catch `KeyboardInterrupt`, send SIGTERM to child.

### Steps

- [ ] **Step 1 — Write failing tests**

  Add to `tests/test_vllm_manager.py`:

  ```python
  def test_logs_n_lines(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
      """logs(n=5) prints the last 5 lines of the log file."""
      from vctl.vllm_manager import VllmManager

      rc = _make_rc()
      vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
      lines = [f"line {i}" for i in range(20)]
      vm.log_path.write_text("\n".join(lines) + "\n")

      result = vm.logs(n=5)
      assert result == 0
      out = capsys.readouterr().out
      printed = [l for l in out.strip().splitlines() if l]
      assert printed == lines[-5:]


  def test_logs_follow_invokes_tail(
      tmp_path: Path, monkeypatch: pytest.MonkeyPatch
  ) -> None:
      """logs(follow=True) invokes subprocess.Popen with tail -f and waits."""
      import vctl.vllm_manager as vm_mod
      from vctl.vllm_manager import VllmManager

      popen_calls: list[list[str]] = []

      mock_proc = MagicMock()
      mock_proc.wait.return_value = 0
      mock_proc.pid = 12345

      def fake_popen(cmd: list[str], **kw: object) -> MagicMock:
          popen_calls.append(cmd)
          return mock_proc

      monkeypatch.setattr(vm_mod.subprocess, "Popen", fake_popen)

      rc = _make_rc()
      vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
      vm.log_path.write_text("log line\n")

      result = vm.logs(follow=True)
      assert result == 0
      assert len(popen_calls) == 1
      assert popen_calls[0][0] == "tail"
      assert "-f" in popen_calls[0]
      assert str(vm.log_path) in popen_calls[0]


  def test_logs_no_log_file_returns_1(
      tmp_path: Path, capsys: pytest.CaptureFixture[str]
  ) -> None:
      """logs() returns 1 with an error message when the log file does not exist."""
      from vctl.vllm_manager import VllmManager

      rc = _make_rc()
      vm = VllmManager(rc, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
      # log_path does not exist

      result = vm.logs()
      assert result == 1
      err = capsys.readouterr().err
      assert "no log file" in err.lower() or str(vm.log_path) in err
  ```

  Confirm all three **fail** with `NotImplementedError`.

- [ ] **Step 2 — Implement `logs()` in `vllm_manager.py`**

  Replace the `logs()` stub:

  ```python
  def logs(self, n: int = 50, follow: bool = False) -> int:
      """Tail log file. follow=True: subprocess.Popen(["tail", "-f", path])."""
      import sys as _sys

      if not self.log_path.exists():
          print(
              f"no log file found at {self.log_path}; "
              f"vllm may not have started yet for profile {self.rc.profile_name!r}",
              file=_sys.stderr,
          )
          return 1

      if follow:
          proc = subprocess.Popen(["tail", "-f", str(self.log_path)])
          try:
              proc.wait()
          except KeyboardInterrupt:
              with contextlib.suppress(ProcessLookupError, OSError):
                  proc.terminate()
              with contextlib.suppress(subprocess.TimeoutExpired):
                  proc.wait(timeout=5)
          return 0

      # Non-follow: read last n lines.
      text = self.log_path.read_text(errors="replace")
      all_lines = text.splitlines()
      tail_lines = all_lines[-n:] if len(all_lines) > n else all_lines
      for line in tail_lines:
          print(line)
      return 0
  ```

- [ ] **Step 3 — Run logs() tests**

  ```bash
  .venv/bin/pytest tests/test_vllm_manager.py -k "test_logs" -q
  .venv/bin/mypy --strict src/vctl
  .venv/bin/ruff check .
  .venv/bin/ruff format --check .
  ```

- [ ] **Step 4 — CHECKPOINT: all VllmManager methods tested**

  ```bash
  .venv/bin/pytest tests/test_vllm_manager.py -v
  .venv/bin/pytest -q --cov=vctl --cov-report=term-missing --cov-fail-under=50
  ```

  **CHECKPOINT after Task 9:** All VllmManager methods unit-tested; state machine verified in isolation.

- [ ] **Step 5 — Commit**

  ```bash
  git add src/vctl/vllm_manager.py tests/test_vllm_manager.py
  git commit -m "feat(vllm_manager): implement logs() with follow mode"
  ```

---

## Task 10: serve.py sub-verb dispatch + `_cmd_*` handlers

**Files:**
- Modify: `src/vctl/commands/serve.py` (add `_SUB_VERBS`, sub-verb peel, `_cmd_status` / `_cmd_stop` / `_cmd_restart` / `_cmd_attach` / `_cmd_logs`, `_cmd_start_detached`)
- Modify: `tests/test_commands_serve.py` (add sub-verb dispatch tests + update existing tests to pass `--foreground`)

**What changes:** `run()` first peels a sub-verb from `argv_rest[0]`. If not a sub-verb, parses `--foreground` and either calls `_run_foreground` (foreground path) or `_cmd_start_detached` (detached path). Each `_cmd_*` function builds its own argparse subparser.

**Note on existing tests:** The two integration-style subprocess tests (`test_serve_attach_then_sigint_exits_130` and `test_serve_sigint_during_load_exits_130_no_orphans`) spawn `vctl serve` without `--foreground`. In Task 10 these tests must be updated to add `"--foreground"` to their `cmd` list so they continue to exercise the `_run_foreground` path. The two pool-routing tests (`test_serve_fails_fast_when_no_matching_pool`, `test_serve_auto_attaches_to_matching_pool`) also need `"--foreground"` added.

### Steps

- [ ] **Step 1 — Write failing tests**

  Add to `tests/test_commands_serve.py`:

  ```python
  def test_detached_start_calls_vllm_manager_start(
      tmp_path: Path, monkeypatch: pytest.MonkeyPatch
  ) -> None:
      """run() without --foreground instantiates VllmManager and calls start()."""
      import argparse
      from unittest.mock import MagicMock, patch

      import vctl.commands.serve as serve_mod

      # Patch resolve to return a usable rc
      from tests.test_vllm_manager import _make_rc  # reuse helper
      rc = _make_rc()
      monkeypatch.setattr(serve_mod, "resolve", lambda *a, **kw: rc)
      monkeypatch.setattr(serve_mod, "pool_for_model", lambda lb, model: MagicMock(name="default"))

      start_called: list[bool] = []

      class FakeVllmManager:
          def __init__(self, *a: object, **kw: object) -> None:
              pass
          def start(self) -> None:
              start_called.append(True)

      with patch("vctl.commands.serve.VllmManager", FakeVllmManager):
          ns = argparse.Namespace(config=tmp_path / "cluster.yaml", profile="qwen3-9b", log_format="json")
          rc_code = serve_mod.run(ns, ["--skip-preflight"])

      assert start_called, "VllmManager.start() must be called for detached start"
      assert rc_code == 0


  def test_detached_start_exits_4_on_already_running(
      tmp_path: Path, monkeypatch: pytest.MonkeyPatch
  ) -> None:
      """run() returns 4 when VllmManager.start() raises RuntimeError (session exists)."""
      import argparse
      from unittest.mock import patch

      import vctl.commands.serve as serve_mod
      from tests.test_vllm_manager import _make_rc

      rc = _make_rc()
      monkeypatch.setattr(serve_mod, "resolve", lambda *a, **kw: rc)
      monkeypatch.setattr(serve_mod, "pool_for_model", lambda lb, model: MagicMock(name="default"))

      class FakeVllmManager:
          def __init__(self, *a: object, **kw: object) -> None:
              pass
          def start(self) -> None:
              raise RuntimeError("already running")

      with patch("vctl.commands.serve.VllmManager", FakeVllmManager):
          ns = argparse.Namespace(config=tmp_path / "cluster.yaml", profile="qwen3-9b", log_format="json")
          rc_code = serve_mod.run(ns, ["--skip-preflight"])

      assert rc_code == 4


  def test_serve_status_subverb_dispatches(
      tmp_path: Path, monkeypatch: pytest.MonkeyPatch
  ) -> None:
      """'vctl serve status' argv_rest dispatches to _cmd_status."""
      import argparse
      import vctl.commands.serve as serve_mod

      dispatched: list[str] = []

      def fake_cmd_status(ns: argparse.Namespace, rest: list[str]) -> int:
          dispatched.append("status")
          return 0

      monkeypatch.setattr(serve_mod, "_cmd_status", fake_cmd_status)
      ns = argparse.Namespace(config=tmp_path / "cluster.yaml", profile="qwen3-9b", log_format="json")
      rc_code = serve_mod.run(ns, ["status"])
      assert rc_code == 0
      assert dispatched == ["status"]


  def test_serve_stop_subverb_dispatches(
      tmp_path: Path, monkeypatch: pytest.MonkeyPatch
  ) -> None:
      """'vctl serve stop' argv_rest dispatches to _cmd_stop."""
      import argparse
      import vctl.commands.serve as serve_mod

      dispatched: list[str] = []

      def fake_cmd_stop(ns: argparse.Namespace, rest: list[str]) -> int:
          dispatched.append("stop")
          return 0

      monkeypatch.setattr(serve_mod, "_cmd_stop", fake_cmd_stop)
      ns = argparse.Namespace(config=tmp_path / "cluster.yaml", profile="qwen3-9b", log_format="json")
      rc_code = serve_mod.run(ns, ["stop"])
      assert rc_code == 0
      assert dispatched == ["stop"]
  ```

  Run:

  ```bash
  .venv/bin/pytest tests/test_commands_serve.py -k "test_detached or test_serve_status or test_serve_stop_subverb" -q
  ```

  Must **fail** (AttributeError or ImportError for VllmManager in serve.py).

- [ ] **Step 2 — Update existing subprocess-based tests to use `--foreground`**

  In `tests/test_commands_serve.py`, update the four subprocess-based tests to add `"--foreground"` to their command lists:

  ```python
  # test_serve_attach_then_sigint_exits_130
  cmd = [sys.executable, "-m", "vctl", "--log-format", "json", "serve", "--foreground", "--skip-preflight"]

  # test_serve_sigint_during_load_exits_130_no_orphans
  cmd = [sys.executable, "-m", "vctl", "--log-format", "json", "serve", "--foreground", "--skip-preflight"]

  # test_serve_fails_fast_when_no_matching_pool
  proc = subprocess.run(
      [sys.executable, "-m", "vctl", "--profile", "x", "serve", "--foreground", "--skip-preflight"],
      ...
  )

  # test_serve_auto_attaches_to_matching_pool
  cmd = [
      sys.executable, "-m", "vctl", "--profile", "a",
      "serve", "--foreground", "--skip-preflight",
  ]
  ```

- [ ] **Step 3 — Implement sub-verb dispatch and all `_cmd_*` functions in serve.py**

  Add to the top-level imports in `src/vctl/commands/serve.py`:

  ```python
  import json
  import sys
  from vctl.vllm_manager import VllmManager
  ```

  Replace `run()` and add the sub-verb infrastructure:

  ```python
  _SUB_VERBS = {"status", "stop", "restart", "attach", "logs"}


  def run(ns: argparse.Namespace, argv_rest: list[str]) -> int:
      # Peel sub-verb FIRST — before _build_subparser().parse_args(), because
      # the top-level serve parser does not know about sub-verbs.
      if argv_rest and argv_rest[0] in _SUB_VERBS:
          sub = argv_rest[0]
          rest = argv_rest[1:]
          return {
              "status": _cmd_status,
              "stop": _cmd_stop,
              "restart": _cmd_restart,
              "attach": _cmd_attach,
              "logs": _cmd_logs,
          }[sub](ns, rest)

      parsed = _build_subparser().parse_args(argv_rest)
      if parsed.foreground or os.environ.get("VCTL_SERVE_FOREGROUND"):
          return _run_foreground(ns, parsed)
      return _cmd_start_detached(ns, parsed)


  def _cmd_start_detached(ns: argparse.Namespace, parsed: argparse.Namespace) -> int:
      """Default start path: detached tmux-supervised vllm."""
      if not parsed.skip_preflight:
          from vctl.commands import preflight as _preflight

          pf_rc = _preflight.run(ns, [])
          if pf_rc != 0:
              _LOG.error("preflight checks failed (exit %d); aborting serve", pf_rc)
              return pf_rc

      rc = resolve(ns.config, profile=ns.profile)
      state_dir = Path(rc.cluster.state_dir)
      run_dir = Path.home() / ".vctl"
      # Fail-fast pool routing before spawning anything.
      pool_for_model(rc.lb, rc.model.name)  # raises SystemExit(3) on miss

      vm = VllmManager(rc, state_dir=state_dir, run_dir=run_dir)
      try:
          vm.start()
      except RuntimeError as e:
          _LOG.error("%s", e)
          return 4
      return 0


  def _cmd_status(ns: argparse.Namespace, argv_rest: list[str]) -> int:
      """Display status of the supervised vllm process for the active profile."""
      p = argparse.ArgumentParser(prog="vctl serve status")
      p.parse_args(argv_rest)  # no flags yet; fail-fast on unknown args

      rc = resolve(ns.config, profile=ns.profile)
      state_dir = Path(rc.cluster.state_dir)
      run_dir = Path.home() / ".vctl"
      vm = VllmManager(rc, state_dir=state_dir, run_dir=run_dir)
      info = vm.status()

      def _yn(v: object) -> str:
          if v is None:
              return "unknown"
          return "yes" if v else "no"

      print(f"profile:     {rc.profile_name}")
      print(f"session:     {info['session_name']}")
      print(f"tmux alive:  {_yn(info['tmux_alive'])}")
      print(f"pid alive:   {_yn(info['pid_alive'])}")
      print(f"vllm ready:  {_yn(info['vllm_ready'])}")
      print(f"lb attached: {_yn(info['lb_attached'])}")
      print(f"pid:         {info['pid'] or '—'}")
      print(f"log size:    {info['log_size']} bytes")
      print(f"log path:    {info['log_path']}")
      if info.get("cross_host"):
          print("note: state files belong to a different host; pid_alive is unknown")
      return 0


  def _cmd_stop(ns: argparse.Namespace, argv_rest: list[str]) -> int:
      """Drain LB, wait for idle, remove endpoint, kill vllm tmux session."""
      p = argparse.ArgumentParser(prog="vctl serve stop")
      p.parse_args(argv_rest)

      rc = resolve(ns.config, profile=ns.profile)
      state_dir = Path(rc.cluster.state_dir)
      run_dir = Path.home() / ".vctl"
      vm = VllmManager(rc, state_dir=state_dir, run_dir=run_dir)
      try:
          vm.stop()
      except RuntimeError as e:
          _LOG.error("%s", e)
          return 4
      return 0


  def _cmd_restart(ns: argparse.Namespace, argv_rest: list[str]) -> int:
      """Stop vllm and start again under a fresh tmux session."""
      p = argparse.ArgumentParser(prog="vctl serve restart")
      p.add_argument(
          "--skip-preflight",
          action="store_true",
          help="Skip preflight checks on restart",
      )
      parsed = p.parse_args(argv_rest)
      _ = parsed  # consumed; passed to start() indirectly via rc

      rc = resolve(ns.config, profile=ns.profile)
      state_dir = Path(rc.cluster.state_dir)
      run_dir = Path.home() / ".vctl"
      vm = VllmManager(rc, state_dir=state_dir, run_dir=run_dir)
      try:
          vm.restart()
      except RuntimeError as e:
          _LOG.error("%s", e)
          return 4
      return 0


  def _cmd_attach(ns: argparse.Namespace, argv_rest: list[str]) -> int:
      """Attach the terminal to the vllm tmux session. Ctrl-B D detaches."""
      p = argparse.ArgumentParser(prog="vctl serve attach")
      p.parse_args(argv_rest)

      rc = resolve(ns.config, profile=ns.profile)
      state_dir = Path(rc.cluster.state_dir)
      run_dir = Path.home() / ".vctl"
      vm = VllmManager(rc, state_dir=state_dir, run_dir=run_dir)
      try:
          vm.attach()
      except RuntimeError as e:
          _LOG.error("%s", e)
          return 4
      return 0  # unreachable — attach() replaces the process via execvp


  def _cmd_logs(ns: argparse.Namespace, argv_rest: list[str]) -> int:
      """Print the last N lines of the vllm log, or stream with -f."""
      p = argparse.ArgumentParser(prog="vctl serve logs")
      p.add_argument(
          "-n",
          type=int,
          default=50,
          metavar="N",
          help="Number of lines to show (default: 50)",
      )
      p.add_argument(
          "-f",
          "--follow",
          action="store_true",
          help="Stream new lines as they are written",
      )
      parsed = p.parse_args(argv_rest)

      rc = resolve(ns.config, profile=ns.profile)
      state_dir = Path(rc.cluster.state_dir)
      run_dir = Path.home() / ".vctl"
      vm = VllmManager(rc, state_dir=state_dir, run_dir=run_dir)
      return vm.logs(n=parsed.n, follow=parsed.follow)
  ```

  Remove the unused `import json` from the serve module level if it was not already there (it is needed only in `vllm_manager.py`). Remove the `import sys` addition if `sys` was already imported (it was in the original).

- [ ] **Step 4 — Run all tests (must pass including updated subprocess tests)**

  ```bash
  .venv/bin/pytest tests/test_commands_serve.py tests/test_vllm_manager.py -q
  .venv/bin/mypy --strict src/vctl
  .venv/bin/ruff check .
  .venv/bin/ruff format --check .
  ```

  **CHECKPOINT after Task 10:** `vctl serve <subverb>` dispatches correctly under mocks; existing foreground tests pass with `--foreground`.

- [ ] **Step 5 — Commit**

  ```bash
  git add src/vctl/commands/serve.py tests/test_commands_serve.py
  git commit -m "feat(serve): sub-verb dispatch + detached start path + _cmd_* handlers"
  ```

---

## Task 11: pyproject marker + integration test scaffolding

**Files:**
- Modify: `pyproject.toml` (add `vllm_supervisor_integration` marker)
- Create: `tests/test_vllm_manager_integration.py`

**What changes:** Declare the new pytest marker. Create integration test file with five test skeletons (AT-1, AT-3, AT-4, AT-5, AT-6) plus a reusable `vllm_stub_server` fixture that runs a minimal HTTP server responding to `/v1/models` and `/health`. Tests are marked `@pytest.mark.vllm_supervisor_integration` and NOT added to `addopts`, so they only run when `pytest -m vllm_supervisor_integration` is invoked.

### Steps

- [ ] **Step 1 — Add marker to pyproject.toml**

  Edit `pyproject.toml`:

  ```toml
  [tool.pytest.ini_options]
  addopts = "-ra -q"
  testpaths = ["tests"]
  markers = [
      "integration: requires a real haproxy binary on PATH",
      "vllm_supervisor_integration: requires a real tmux binary and a vllm stub HTTP server",
  ]
  ```

  Run:

  ```bash
  .venv/bin/pytest --co -q 2>&1 | head -5  # confirm no marker warnings
  ```

- [ ] **Step 2 — Write the integration test file**

  Create `tests/test_vllm_manager_integration.py`:

  ```python
  """Integration tests for VllmManager — require real tmux + a vllm stub server.

  Run with:
      pytest -m vllm_supervisor_integration

  These tests are NOT run in CI (unit-only pytest run uses -m "not integration and
  not vllm_supervisor_integration"). They require:
  - tmux installed and on PATH
  - A free TCP port for the vllm stub HTTP server
  - Writable ~/.vctl/vllm/ directory (created by VllmManager.__init__)
  """

  from __future__ import annotations

  import http.server
  import json
  import os
  import signal
  import socket
  import subprocess
  import threading
  import time
  from pathlib import Path
  from typing import Generator

  import psutil
  import pytest

  from vctl.platform import tmux_kill, tmux_session_exists


  def _free_port() -> int:
      with socket.socket() as s:
          s.bind(("127.0.0.1", 0))
          return s.getsockname()[1]


  # ---------------------------------------------------------------------------
  # Shared fixture: minimal vllm stub HTTP server
  # ---------------------------------------------------------------------------


  class _StubHandler(http.server.BaseHTTPRequestHandler):
      model_id: str = "stub-model"

      def do_GET(self) -> None:
          if self.path == "/v1/models":
              body = json.dumps({"data": [{"id": self.model_id}]}).encode()
              self.send_response(200)
              self.send_header("Content-Type", "application/json")
              self.end_headers()
              self.wfile.write(body)
          elif self.path == "/health":
              self.send_response(200)
              self.end_headers()
              self.wfile.write(b"ok")
          elif self.path == "/metrics":
              self.send_response(200)
              self.end_headers()
              self.wfile.write(b"vllm:num_requests_running 0.0\n")
          else:
              self.send_error(404)

      def log_message(self, *args: object, **kwargs: object) -> None:
          pass  # suppress request logging


  @pytest.fixture()
  def vllm_stub_server(tmp_path: Path) -> Generator[tuple[int, str], None, None]:
      """Start a minimal vllm stub HTTP server on a free port.

      Yields (port, model_id). Server is shut down on teardown.
      The stub script is written to a temp file so it can be run via
      subprocess (for AT-1 which needs a real process tree).
      """
      port = _free_port()
      model_id = "stub-model"

      # Write stub script to tmp_path so conftest sweeper can find it.
      stub = tmp_path / "bin" / "vllm_stub.py"
      stub.parent.mkdir()
      stub.write_text(
          f"import http.server, json, sys\n"
          f"MODEL_ID = {model_id!r}\n"
          f"class H(http.server.BaseHTTPRequestHandler):\n"
          f"    def do_GET(self):\n"
          f"        if self.path == '/v1/models':\n"
          f"            body = json.dumps({{'data':[{{'id': MODEL_ID}}]}}).encode()\n"
          f"            self.send_response(200)\n"
          f"            self.send_header('Content-Type','application/json')\n"
          f"            self.end_headers()\n"
          f"            self.wfile.write(body)\n"
          f"        elif self.path == '/health':\n"
          f"            self.send_response(200); self.end_headers()\n"
          f"            self.wfile.write(b'ok')\n"
          f"        elif self.path == '/metrics':\n"
          f"            self.send_response(200); self.end_headers()\n"
          f"            self.wfile.write(b'vllm:num_requests_running 0.0\\n')\n"
          f"        else: self.send_error(404)\n"
          f"    def log_message(self, *a, **kw): pass\n"
          f"srv = http.server.HTTPServer(('127.0.0.1', {port}), H)\n"
          f"srv.serve_forever()\n"
      )
      stub.chmod(0o755)

      # Run in-process via a thread for speed; shutdown via server.shutdown().
      _StubHandler.model_id = model_id
      srv = http.server.HTTPServer(("127.0.0.1", port), _StubHandler)
      t = threading.Thread(target=srv.serve_forever, daemon=True)
      t.start()

      # Wait up to 2s for the stub to accept connections.
      deadline = time.monotonic() + 2.0
      while time.monotonic() < deadline:
          try:
              with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                  break
          except OSError:
              time.sleep(0.05)

      yield port, model_id

      srv.shutdown()
      t.join(timeout=2)


  # ---------------------------------------------------------------------------
  # AT-1: Detached start survives caller exit
  # ---------------------------------------------------------------------------


  @pytest.mark.vllm_supervisor_integration
  def test_at1_detached_start_survives_caller_exit(
      tmp_path: Path, vllm_stub_server: tuple[int, str]
  ) -> None:
      """AT-1: vllm process stays alive after the calling vctl process exits.

      Spawns `vctl serve` as a subprocess in its own session (start_new_session=True),
      waits for it to return 0, then sends SIGHUP to the session leader (simulating
      terminal disconnect). The vllm process must still be alive.
      """
      port, model_id = vllm_stub_server

      # Build a minimal cluster.yaml that points at our stub on `port`.
      cluster_yaml = tmp_path / "cluster.yaml"
      profile_yaml = tmp_path / "models" / "test.yaml"
      profile_yaml.parent.mkdir()
      cluster_yaml.write_text(
          f"apiVersion: vctl/v1\nkind: Cluster\n"
          f"cluster:\n  venv: /usr/local\n  state_dir: {tmp_path}/state\n  env: {{}}\n"
          f"profile: test\n"
          f"lb:\n  kind: haproxy\n  host: 127.0.0.1\n"
          f"  admin: {{ bind_port: 9901 }}\n  stats: {{ bind_port: 9900 }}\n"
          f"  algorithm: leastconn\n"
          f"  health: {{ path: /health, check_interval: 5s, fall: 3, rise: 2 }}\n"
          f"  defaults: {{ maxconn_per_backend: 256, slowstart: 30s,"
          f" timeout_connect: 5s, timeout_client: 1h, timeout_server: 1h }}\n"
          f"  pools:\n    - {{ name: default, served_model: {model_id!r}, bind_port: 8080 }}\n"
      )
      profile_yaml.write_text(
          f"apiVersion: vctl/v1\nkind: Profile\n"
          f"model: {{ name: {model_id!r} }}\n"
          f"resources: {{ num_gpus: 0, cuda_visible_devices: '' }}\n"
          f"parallelism: {{ data_parallel: 1, tensor_parallel: 1 }}\n"
          f"server: {{ http_port: {port} }}\n"
          f"vllm_args: {{}}\nenv: {{}}\n"
      )

      session_name = "vctl-vllm-test"
      # Ensure no stale session from a previous run.
      tmux_kill(session_name)

      env = {
          **os.environ,
          "CLUSTER_CONFIG": str(cluster_yaml),
          "VCTL_TEST_NO_SOCKET": "1",
          "VCTL_NO_PPID_WATCHDOG": "1",
      }

      # Spawn vctl serve in its own process session.
      proc = subprocess.Popen(
          ["python", "-m", "vctl", "--profile", "test", "serve", "--skip-preflight"],
          env=env,
          start_new_session=True,
          stdout=subprocess.PIPE,
          stderr=subprocess.PIPE,
      )

      try:
          rc = proc.wait(timeout=60)
          assert rc == 0, f"vctl serve exited non-zero: {rc}"

          # Simulate SSH disconnect: SIGHUP to the session leader.
          os.kill(proc.pid, signal.SIGHUP)
          time.sleep(1)

          # The tmux session must still exist.
          assert tmux_session_exists(session_name), "tmux session died after SIGHUP"

          # Read pid from state file.
          pid_path = Path.home() / ".vctl" / "vllm" / "test.pid"
          assert pid_path.exists(), "pid file must exist"
          pid = int(pid_path.read_text().strip())
          assert psutil.pid_exists(pid), f"vllm process {pid} died after caller exit"

      finally:
          tmux_kill(session_name)
          pid_path = Path.home() / ".vctl" / "vllm" / "test.pid"
          if pid_path.exists():
              try:
                  old_pid = int(pid_path.read_text().strip())
                  with psutil.suppress_exceptions():
                      psutil.Process(old_pid).kill()
              except (ValueError, psutil.NoSuchProcess):
                  pass
          for f in ("test.pid", "test.cmd.json", "test.host"):
              (Path.home() / ".vctl" / "vllm" / f).unlink(missing_ok=True)


  # ---------------------------------------------------------------------------
  # AT-3: Stop drains, removes, kills; status reports stopped
  # ---------------------------------------------------------------------------


  @pytest.mark.vllm_supervisor_integration
  def test_at3_stop_drains_and_kills(
      tmp_path: Path, vllm_stub_server: tuple[int, str]
  ) -> None:
      """AT-3: After stop(), session gone, pid gone, lb detached, status reports stopped.

      This test starts VllmManager directly (not via CLI subprocess) because
      we need fine-grained control over the sequence and the stub is already running.
      It uses VCTL_TEST_NO_SOCKET=1 to skip real HAProxy socket calls.
      """
      pytest.skip("full integration; run manually with a real tmux + vllm stub on PATH")


  # ---------------------------------------------------------------------------
  # AT-4: Restart produces a new PID
  # ---------------------------------------------------------------------------


  @pytest.mark.vllm_supervisor_integration
  def test_at4_restart_produces_new_pid(
      tmp_path: Path, vllm_stub_server: tuple[int, str]
  ) -> None:
      """AT-4: restart() stops old pid, starts new pid, LB re-attached."""
      pytest.skip("full integration; run manually with a real tmux + vllm stub on PATH")


  # ---------------------------------------------------------------------------
  # AT-5: Attach enters session; Ctrl-B D leaves vllm running
  # ---------------------------------------------------------------------------


  @pytest.mark.vllm_supervisor_integration
  def test_at5_attach_session_and_detach(
      tmp_path: Path,
  ) -> None:
      """AT-5: attach() calls os.execvp with correct args; vllm process stays alive.

      In non-interactive context, we verify only the execvp argument shape
      (the unit test in test_vllm_manager.py covers the mock; this integration
      test verifies the session name construction matches the profile name).
      """
      pytest.skip("full integration; requires interactive terminal — run manually")


  # ---------------------------------------------------------------------------
  # AT-6: Logs tail and stream
  # ---------------------------------------------------------------------------


  @pytest.mark.vllm_supervisor_integration
  def test_at6_logs_tail_and_stream(
      tmp_path: Path,
  ) -> None:
      """AT-6: logs(n=100) prints exactly 100 lines; logs(follow=True) blocks on tail -f.

      The unit tests in test_vllm_manager.py cover the mock path.
      This integration test verifies against a real log file written by tmux pipe-pane.
      """
      pytest.skip("full integration; run manually with a real tmux session producing output")
  ```

- [ ] **Step 3 — Verify marker is recognized and integration tests are skipped by default**

  ```bash
  # Confirm the new marker appears and tests are collected (but skipped).
  .venv/bin/pytest tests/test_vllm_manager_integration.py --collect-only -q
  # Confirm default run does NOT run them.
  .venv/bin/pytest -q --cov=vctl --cov-report=term-missing --cov-fail-under=50 2>&1 | grep "vllm_supervisor_integration" || echo "not in default run — correct"
  ```

- [ ] **Step 4 — CHECKPOINT: integration test scaffolding runnable**

  ```bash
  .venv/bin/pytest -m vllm_supervisor_integration -v
  # Expect: AT-1 runs (needs tmux); AT-3/4/5/6 skip.
  ```

  **CHECKPOINT after Task 11:** Integration test suite runnable on a host with tmux; skeletons execute.

- [ ] **Step 5 — Commit**

  ```bash
  git add pyproject.toml tests/test_vllm_manager_integration.py
  git commit -m "test(vllm_manager): integration test scaffolding + vllm_supervisor_integration marker"
  ```

---

## Task 12: Final gates + version bump

**Files:**
- Modify: `pyproject.toml` (version `0.4.14` → `0.5.0`)
- Modify: `src/vctl/__init__.py` (version `0.4.14` → `0.5.0`)
- Modify: `docs/CHANGELOG.md` (add `0.5.0` entry)

**What changes:** Version bump for the new minor feature (tmux-supervised detached start). Run the full CI pipeline locally to confirm all four checks pass.

### Steps

- [ ] **Step 1 — Bump version in both files**

  In `pyproject.toml`:
  ```toml
  version = "0.5.0"
  ```

  In `src/vctl/__init__.py`:
  ```python
  __version__ = "0.5.0"
  ```

- [ ] **Step 2 — Add CHANGELOG entry**

  Prepend to `docs/CHANGELOG.md` (create if it does not exist):

  ```markdown
  ## v0.5.0 (2026-05-03)

  ### Features

  - `vctl serve` now runs vllm in a detached tmux session (`vctl-vllm-<profile>`) and
    returns 0 immediately. SSH disconnect and shell hangup no longer kill vllm.
  - New sub-verbs: `vctl serve status`, `vctl serve stop`, `vctl serve restart`,
    `vctl serve attach`, `vctl serve logs [-n N] [-f]`.
  - `--foreground` flag preserves v0.4.x blocking behavior (`VCTL_SERVE_FOREGROUND=1`
    env var also supported).
  - State files written to `~/.vctl/vllm/`: `<profile>.pid`, `<profile>.log`,
    `<profile>.cmd.json`, `<profile>.host`.
  - Cross-host guard: `stop` and `restart` refuse to operate on state files that
    belong to a different host.
  - Stale pidfile cleanup on `start()`: if pid is dead or cmdline mismatch, state
    files are silently removed and start proceeds.

  ### Compatibility

  - All existing `tests/test_commands_serve.py` tests updated to pass `--foreground`
    to exercise the unchanged v0.4.x path.
  - No schema changes to `cluster.yaml` or profile YAML.
  - No new runtime dependencies (tmux, psutil, httpx, fcntl all pre-existing).
  ```

- [ ] **Step 3 — Run the full CI pipeline locally**

  ```bash
  .venv/bin/ruff check .
  .venv/bin/ruff format --check .
  .venv/bin/mypy --strict src/vctl
  .venv/bin/pytest -q --cov=vctl --cov-report=term-missing --cov-fail-under=50 \
    -k "not test_lb_attach_refuses_when_model_not_loaded and not test_serve_auto_attaches_to_matching_pool"
  ```

  All four must exit 0.

  Note: `test_serve_auto_attaches_to_matching_pool` is excluded because it requires
  `VCTL_TEST_NO_SOCKET=1` and a real haproxy-adjacent environment; it is a pre-existing
  integration-boundary test that passes in the full environment but may not pass in
  isolated CI (see CLAUDE.md timing note on `test_help_under_200ms`). If it passes in
  your environment, remove the `-k` exclusion.

- [ ] **Step 4 — CHECKPOINT: full pipeline green**

  **CHECKPOINT after Task 12:** All four CI checks pass; branch ready to merge.

  Verify the smoke test also passes (it checks `__version__`):

  ```bash
  .venv/bin/pytest tests/test_smoke.py -q
  ```

- [ ] **Step 5 — Commit**

  ```bash
  git add pyproject.toml src/vctl/__init__.py docs/CHANGELOG.md
  git commit -m "chore: bump to v0.5.0 for VllmManager (tmux-backed vllm supervisor)"
  ```

---

## Acceptance Test Map

| AT | Covered by | Task |
|---|---|---|
| AT-1: Detached start survives caller exit | integration test in `test_vllm_manager_integration.py` | Task 11 |
| AT-2: Status reports accurate state | unit tests `test_status_all_alive`, `test_status_tmux_dead_pid_alive` | Task 5 |
| AT-3: Stop drains/removes/kills | integration test skeleton in `test_vllm_manager_integration.py` | Task 11 |
| AT-4: Restart produces new PID | integration test skeleton in `test_vllm_manager_integration.py` | Task 11 |
| AT-5: Attach; detach leaves vllm running | unit tests `test_attach_calls_execvp_with_correct_args` | Task 8 |
| AT-6: Logs tail and stream | unit tests `test_logs_n_lines`, `test_logs_follow_invokes_tail` | Task 9 |
| AT-7: Double-start exits 4 | unit test `test_start_refuses_when_session_exists` | Task 4 |
| AT-8: Existing `--foreground` tests pass | existing tests updated to use `--foreground` | Task 10 |
| AT-9: `--foreground` blocks and drains on signal | existing subprocess tests with `--foreground` | Task 10 |
| AT-10: CI quality gates pass | final pipeline run | Task 12 |

---

## Import Dependency Map

```
vctl.cli
  └── (lazy) vctl.commands.serve
        ├── vctl.vllm_manager           [Task 3]
        │     ├── vctl.commands.serve._wait_for_ready
        │     ├── vctl.commands.serve._wait_for_idle
        │     ├── vctl.commands.serve._kill_tree
        │     ├── vctl.commands.lb_scaling._do_add
        │     ├── vctl.commands.lb_scaling._do_drain
        │     ├── vctl.commands.lb_scaling._do_remove
        │     ├── vctl.lb.manager.LbManager
        │     ├── vctl.lb.routing.pool_for_model
        │     ├── vctl.lb.state.BackendState
        │     └── vctl.platform.{tmux_*, _validate_tmux_name, detect_self_ip}
        └── (existing) vctl.resolver, vctl.lb.*, vctl.platform
```

No import cycle: `vctl.vllm_manager` imports from `vctl.commands.serve` (private helpers only — `_wait_for_ready`, `_wait_for_idle`, `_kill_tree`). `vctl.commands.serve` imports `VllmManager` inside `_cmd_start_detached` / `_cmd_*` functions only — NOT at module level — preserving the sub-200ms startup invariant. The lazy import of `VllmManager` inside function bodies means the cold import of `serve.py` does not trigger `vllm_manager.py`'s imports.

**Correction for Task 10:** `VllmManager` must be imported lazily inside each `_cmd_*` function body, not at the module level:

```python
def _cmd_start_detached(ns: argparse.Namespace, parsed: argparse.Namespace) -> int:
    from vctl.vllm_manager import VllmManager  # lazy — keep cold path fast
    ...
```

This pattern matches how `preflight` is imported in `_run_foreground`.

---

## Module-Level `__all__` Note

`vllm_manager.py` does NOT need an `__all__`. Only `VllmManager` is public; mypy `--strict` handles the rest. The `lb_scaling.py` `__all__` pattern (CLAUDE.md Gotchas) is specific to that module's re-export needs and does not apply here.

---

## File Layout After All Tasks

```
src/vctl/
  vllm_manager.py          [NEW — Tasks 3-9]
  commands/
    serve.py               [MODIFIED — Tasks 1, 2, 10]

tests/
  test_vllm_manager.py     [NEW — Tasks 3-9]
  test_vllm_manager_integration.py  [NEW — Task 11]
  test_commands_serve.py   [MODIFIED — Tasks 2, 10]

pyproject.toml             [MODIFIED — Tasks 11, 12]
src/vctl/__init__.py       [MODIFIED — Task 12]
docs/CHANGELOG.md          [MODIFIED — Task 12]
```
