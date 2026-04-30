"""`vctl lb {start,stop,status,is-host,where,wait-ready,...}` end-to-end."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

FIX = Path(__file__).parent / "fixtures"


def _vctl(
    *args: str, cwd: Path, env: dict[str, str] | None = None, timeout: float = 10
) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "vctl", *args]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, env=env, timeout=timeout)


def _make_repo(tmp_path: Path) -> Path:
    (tmp_path / "cluster.yaml").write_text((FIX / "sample_cluster.yaml").read_text())
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "qwen3-9b.yaml").write_text((FIX / "sample_profile.yaml").read_text())
    return tmp_path


def test_lb_where(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    proc = _vctl("lb", "where", cwd=repo)
    assert proc.returncode == 0
    assert "10.0.0.1:8080" in proc.stdout


def test_lb_wait_ready_timeout(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    proc = _vctl(
        "lb", "wait-ready", "1", cwd=repo, env={**os.environ, "LB_WAIT_TIMEOUT": "1"}, timeout=5
    )
    assert proc.returncode == 1
