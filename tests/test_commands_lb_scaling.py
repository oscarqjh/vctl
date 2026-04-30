"""LB scaling commands — add (idempotent), remove, drain, attach, detach, auto-add."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

FIX = Path(__file__).parent / "fixtures"


def _vctl(*args: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "vctl", *args]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, env=env, timeout=10)


def _make_repo(tmp_path: Path) -> Path:
    (tmp_path / "cluster.yaml").write_text((FIX / "sample_cluster.yaml").read_text())
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "qwen3-9b.yaml").write_text((FIX / "sample_profile.yaml").read_text())
    state = tmp_path / "state"; state.mkdir()
    return tmp_path


def test_lb_add_idempotent_first_then_dup(tmp_path: Path) -> None:
    """AT-9: first call says (new), second says (already present)."""
    repo = _make_repo(tmp_path)
    env = {**os.environ,
           "VCTL_CLUSTER__STATE_DIR": str(tmp_path / "state"),
           "VCTL_TEST_NO_SOCKET": "1"}

    p1 = _vctl("lb", "add", "10.0.0.5:8000", cwd=repo, env=env)
    assert p1.returncode == 0
    assert "(new)" in p1.stderr
    p2 = _vctl("lb", "add", "10.0.0.5:8000", cwd=repo, env=env)
    assert p2.returncode == 0
    assert "(already present)" in p2.stderr


def test_lb_remove_after_add(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    env = {**os.environ,
           "VCTL_CLUSTER__STATE_DIR": str(tmp_path / "state"),
           "VCTL_TEST_NO_SOCKET": "1"}
    _vctl("lb", "add", "10.0.0.5:8000", cwd=repo, env=env)
    p = _vctl("lb", "remove", "10.0.0.5:8000", cwd=repo, env=env)
    assert p.returncode == 0


def test_lb_attach_refuses_when_model_not_loaded(tmp_path: Path) -> None:
    """AT-10: empty data array → exit 1, no state mutation."""
    repo = _make_repo(tmp_path)
    env = {**os.environ,
           "VCTL_CLUSTER__STATE_DIR": str(tmp_path / "state"),
           "VCTL_TEST_NO_SOCKET": "1",
           "VCTL_TEST_PROBE_RESULT": "empty"}
    p = _vctl("lb", "attach", "8000", cwd=repo, env=env)
    assert p.returncode == 1
    assert "not loaded" in p.stderr.lower() or "empty" in p.stderr.lower()
