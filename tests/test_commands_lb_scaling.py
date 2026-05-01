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
    state = tmp_path / "state"
    state.mkdir()
    return tmp_path


def test_lb_add_idempotent_first_then_dup(tmp_path: Path) -> None:
    """AT-9: first call says (new), second says (already present)."""
    repo = _make_repo(tmp_path)
    env = {
        **os.environ,
        "VCTL_CLUSTER__STATE_DIR": str(tmp_path / "state"),
        "VCTL_TEST_NO_SOCKET": "1",
    }

    p1 = _vctl("lb", "add", "10.0.0.5:8000", cwd=repo, env=env)
    assert p1.returncode == 0
    assert "(new)" in p1.stderr
    p2 = _vctl("lb", "add", "10.0.0.5:8000", cwd=repo, env=env)
    assert p2.returncode == 0
    assert "(already present)" in p2.stderr


def test_lb_remove_after_add(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    env = {
        **os.environ,
        "VCTL_CLUSTER__STATE_DIR": str(tmp_path / "state"),
        "VCTL_TEST_NO_SOCKET": "1",
    }
    _vctl("lb", "add", "10.0.0.5:8000", cwd=repo, env=env)
    p = _vctl("lb", "remove", "10.0.0.5:8000", cwd=repo, env=env)
    assert p.returncode == 0


def test_lb_attach_refuses_when_model_not_loaded(tmp_path: Path) -> None:
    """AT-10: empty data array → exit 1, no state mutation."""
    repo = _make_repo(tmp_path)
    env = {
        **os.environ,
        "VCTL_CLUSTER__STATE_DIR": str(tmp_path / "state"),
        "VCTL_TEST_NO_SOCKET": "1",
        "VCTL_TEST_PROBE_RESULT": "empty",
    }
    p = _vctl("lb", "attach", "8000", cwd=repo, env=env)
    assert p.returncode == 1
    assert "not loaded" in p.stderr.lower() or "empty" in p.stderr.lower()


# ---------------------------------------------------------------------------
# Multi-pool helpers + tests
# ---------------------------------------------------------------------------


def _make_two_pool_repo(tmp_path: Path) -> Path:
    """Cluster with two pools (a serves M/A, b serves M/B)."""
    (tmp_path / "cluster.yaml").write_text(
        "apiVersion: vctl/v1\nkind: Cluster\n"
        "cluster:\n  venv: /v\n  state_dir: /tmp/state\n  env: {}\n"
        "profile: a\n"
        "lb:\n"
        "  kind: haproxy\n  host: 10.0.0.1\n"
        "  admin: { bind_port: 9001 }\n"
        "  stats: { bind_port: 9000 }\n"
        "  algorithm: leastconn\n"
        "  health: { path: /health, check_interval: 5s, fall: 3, rise: 2 }\n"
        "  defaults: { maxconn_per_backend: 256, slowstart: 30s, "
        "timeout_connect: 5s, timeout_client: 1h, timeout_server: 1h }\n"
        "  pools:\n"
        "    - { name: a, served_model: M/A, bind_port: 8080 }\n"
        "    - { name: b, served_model: M/B, bind_port: 8081 }\n"
    )
    (tmp_path / "models").mkdir()
    for n, m in [("a", "M/A"), ("b", "M/B")]:
        (tmp_path / "models" / f"{n}.yaml").write_text(
            f"apiVersion: vctl/v1\nkind: Profile\n"
            f"model: {{ name: {m}, served_as: {n} }}\n"
            f"resources: {{ num_gpus: 1, cuda_visible_devices: '0' }}\n"
            f"parallelism: {{ data_parallel: 1, tensor_parallel: 1, api_server_count: 1 }}\n"
            f"server: {{ http_port: 8000 }}\nvllm_args: {{}}\nenv: {{}}\n"
        )
    return tmp_path


def test_lb_add_with_explicit_pool_flag(tmp_path: Path) -> None:
    """`lb add 10.x.x.x:8000 --pool a` registers in pool a's state."""
    repo = _make_two_pool_repo(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    env = {
        **os.environ,
        "VCTL_CLUSTER__STATE_DIR": str(state),
        "VCTL_TEST_NO_SOCKET": "1",
    }
    p = _vctl("lb", "add", "10.0.0.5:8000", "--pool", "a", cwd=repo, env=env)
    assert p.returncode == 0, p.stderr
    assert "(new)" in p.stderr
    assert (state / "10.0.0.1" / "a_backends.txt").read_text().strip() == "10.0.0.5:8000"
    assert (
        not (state / "10.0.0.1" / "b_backends.txt").exists()
        or not (state / "10.0.0.1" / "b_backends.txt").read_text().strip()
    )


def test_lb_add_unknown_pool_exits_3(tmp_path: Path) -> None:
    repo = _make_two_pool_repo(tmp_path)
    state = tmp_path / "state"
    env = {
        **os.environ,
        "VCTL_CLUSTER__STATE_DIR": str(state),
        "VCTL_TEST_NO_SOCKET": "1",
    }
    p = _vctl("lb", "add", "10.0.0.5:8000", "--pool", "nonexistent", cwd=repo, env=env)
    assert p.returncode == 3
    assert "nonexistent" in p.stderr or "unknown pool" in p.stderr.lower()


def test_lb_remove_finds_pool_automatically(tmp_path: Path) -> None:
    repo = _make_two_pool_repo(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    env = {
        **os.environ,
        "VCTL_CLUSTER__STATE_DIR": str(state),
        "VCTL_TEST_NO_SOCKET": "1",
    }
    # Add to pool a
    _vctl("lb", "add", "10.0.0.5:8000", "--pool", "a", cwd=repo, env=env)
    # Remove without --pool — should find it in pool a
    p = _vctl("lb", "remove", "10.0.0.5:8000", cwd=repo, env=env)
    assert p.returncode == 0
    assert (state / "10.0.0.1" / "a_backends.txt").read_text().strip() == ""
