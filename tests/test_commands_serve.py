"""`vctl serve` lifecycle: ready → attach → drain on signal → kill subprocess tree."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

FIX = Path(__file__).parent / "fixtures"


def _free_port() -> int:
    """Bind to port 0 and return the OS-assigned free port."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_stub_vllm(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "vllm"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import http.server, time, os, signal, sys\n"
        "READY_AFTER = float(os.environ.get('STUB_READY_AFTER','1'))\n"
        "T0 = time.time()\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        if self.path=='/v1/models':\n"
        '            ready = \'{"data":[{"id":"m"}]}\'\n'
        "            data = ready if (time.time()-T0)>=READY_AFTER else '{\"data\":[]}'\n"
        "            self.send_response(200); self.end_headers(); self.wfile.write(data.encode())\n"
        "        elif self.path=='/health':\n"
        "            self.send_response(200); self.end_headers(); self.wfile.write(b'ok')\n"
        "        elif self.path=='/metrics':\n"
        "            self.send_response(200); self.end_headers()\n"
        "            self.wfile.write(b'vllm:num_requests_running 0.0\\n')\n"
        "        else: self.send_error(404)\n"
        "    def log_message(self, *a, **kw): pass\n"
        "port = 0\n"
        "for a in sys.argv:\n"
        "    if a.startswith('--port='): port=int(a.split('=')[1])\n"
        "srv = http.server.HTTPServer(('127.0.0.1', port), H)\n"
        "signal.signal(signal.SIGTERM, lambda *_: (srv.shutdown(), sys.exit(0)))\n"
        "srv.serve_forever()\n"
    )
    stub.chmod(0o755)
    return bin_dir


def _make_repo(tmp_path: Path) -> Path:
    (tmp_path / "cluster.yaml").write_text((FIX / "sample_cluster.yaml").read_text())
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "qwen3-9b.yaml").write_text((FIX / "sample_profile.yaml").read_text())
    return tmp_path


def test_serve_attach_then_sigint_exits_130(tmp_path: Path) -> None:
    """AT-7: ready → attach → SIGINT → drain → exit 130; no orphaned children."""
    repo = _make_repo(tmp_path)
    bin_dir = _make_stub_vllm(tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    port = _free_port()
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "VCTL_CLUSTER__STATE_DIR": str(state_dir),
        "VCTL_TEST_NO_SOCKET": "1",
        "STUB_READY_AFTER": "1",
        "VCTL_SERVER__HTTP_PORT": str(port),
        "VCTL_LB__CLIENT__BIND_PORT": "8080",
        "LB_DETACH_WAIT": "3",
        "VCTL_KILL_GRACE": "5",
    }
    cmd = [sys.executable, "-m", "vctl", "--log-format", "json", "serve", "--skip-preflight"]
    proc = subprocess.Popen(cmd, cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(3)
    state_file = state_dir / "10.0.0.1_backends.txt"
    assert state_file.exists(), f"attach should have registered (state_dir={state_dir})"
    assert state_file.read_text().strip(), "state file empty after attach"

    proc.send_signal(signal.SIGINT)
    rc = proc.wait(timeout=15)
    assert rc == 130
    assert not state_file.read_text().strip(), "state file should be empty after detach"
