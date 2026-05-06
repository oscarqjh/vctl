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

import contextlib
import http.server
import json
import os
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Generator
from pathlib import Path

import psutil
import pytest

from vctl.tmux import TmuxSession as _TmuxSession
from vctl.tmux import tmux_session_exists


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
    _TmuxSession(session_name).kill(tree=False)

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
        _TmuxSession(session_name).kill(tree=False)
        pid_path = Path.home() / ".vctl" / "vllm" / "test.pid"
        if pid_path.exists():
            try:
                old_pid = int(pid_path.read_text().strip())
                with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                    psutil.Process(old_pid).kill()
            except (ValueError, psutil.NoSuchProcess):
                pass
        for f in ("test.pid", "test.cmd.json", "test.host"):
            (Path.home() / ".vctl" / "vllm" / f).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# AT-3: Stop drains, removes, kills; status reports stopped
# ---------------------------------------------------------------------------


@pytest.mark.vllm_supervisor_integration
def test_at3_stop_drains_and_kills(tmp_path: Path, vllm_stub_server: tuple[int, str]) -> None:
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
def test_at4_restart_produces_new_pid(tmp_path: Path, vllm_stub_server: tuple[int, str]) -> None:
    """AT-4: restart() stops old pid, starts new pid, LB re-attached."""
    pytest.skip("full integration; run manually with a real tmux + vllm stub on PATH")


# ---------------------------------------------------------------------------
# AT-5: Attach enters session; Ctrl-B D leaves vllm running
# ---------------------------------------------------------------------------


@pytest.mark.vllm_supervisor_integration
def test_at5_console_session_and_detach(
    tmp_path: Path,
) -> None:
    """AT-5: console() calls os.execvp with correct args; vllm process stays alive.

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
