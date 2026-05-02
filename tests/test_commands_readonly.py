"""info / profiles / args end-to-end."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

FIX = Path(__file__).parent / "fixtures"


def _vctl(*args: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    # Always `python -m vctl` to avoid PATH collision with the bash prototype.
    cmd = [sys.executable, "-m", "vctl", *args]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, env=env, timeout=10)


def _make_repo(tmp_path: Path) -> Path:
    (tmp_path / "cluster.yaml").write_text((FIX / "sample_cluster.yaml").read_text())
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "qwen3-9b.yaml").write_text((FIX / "sample_profile.yaml").read_text())
    return tmp_path


def test_info_prints_resolved_fields(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    env = {**os.environ, "CLUSTER_CONFIG": str(repo / "cluster.yaml")}
    proc = _vctl("info", cwd=repo, env=env)
    assert proc.returncode == 0, proc.stderr
    assert "Qwen/Qwen3.5-9B" in proc.stdout
    assert "qwen3-9b" in proc.stdout
    assert "10.0.0.1" in proc.stdout


def test_profiles_lists_models(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "models" / "other.yaml").write_text("apiVersion: vctl/v1\nkind: Profile\n")
    env = {**os.environ, "CLUSTER_CONFIG": str(repo / "cluster.yaml")}
    proc = _vctl("profiles", cwd=repo, env=env)
    assert proc.returncode == 0, proc.stderr
    assert "qwen3-9b" in proc.stdout
    assert "other" in proc.stdout
    assert "*" in proc.stdout or "default" in proc.stdout


def test_profiles_set_changes_active_profile(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "models" / "other.yaml").write_text("apiVersion: vctl/v1\nkind: Profile\n")
    env = {**os.environ, "CLUSTER_CONFIG": str(repo / "cluster.yaml")}

    proc = _vctl("profiles", "set", "other", cwd=repo, env=env)
    assert proc.returncode == 0, proc.stderr
    assert "active profile: other" in proc.stdout

    text = (repo / "cluster.yaml").read_text()
    assert "profile: other" in text
    assert "profile: qwen3-9b" not in text

    proc2 = _vctl("profiles", cwd=repo, env=env)
    assert proc2.returncode == 0
    assert "* other" in proc2.stdout


def test_profiles_set_rejects_unknown(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    env = {**os.environ, "CLUSTER_CONFIG": str(repo / "cluster.yaml")}
    before = (repo / "cluster.yaml").read_text()

    proc = _vctl("profiles", "set", "nope", cwd=repo, env=env)
    assert proc.returncode == 3
    assert "unknown profile" in proc.stderr
    assert (repo / "cluster.yaml").read_text() == before


def test_profiles_set_preserves_comments(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / "cluster.yaml").write_text(
        "# top comment\n"
        "apiVersion: vctl/v1\n"
        "kind: Cluster\n"
        "cluster: { venv: /v, state_dir: /s, env: {} }\n"
        "profile: a  # current\n"
        "lb:\n"
        "  kind: haproxy\n"
        "  host: 10.0.0.1\n"
        "  admin: { bind_port: 9001 }\n"
        "  stats: { bind_port: 9000 }\n"
        "  pools:\n"
        "    - { name: a, served_model: M/A, bind_port: 8080 }\n"
    )
    (repo / "models").mkdir()
    (repo / "models" / "a.yaml").write_text("apiVersion: vctl/v1\nkind: Profile\n")
    (repo / "models" / "b.yaml").write_text("apiVersion: vctl/v1\nkind: Profile\n")
    env = {**os.environ, "CLUSTER_CONFIG": str(repo / "cluster.yaml")}

    proc = _vctl("profiles", "set", "b", cwd=repo, env=env)
    assert proc.returncode == 0, proc.stderr
    text = (repo / "cluster.yaml").read_text()
    assert text.startswith("# top comment\n")
    assert "profile: b" in text


def test_args_emits_one_flag_per_line(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    env = {**os.environ, "CLUSTER_CONFIG": str(repo / "cluster.yaml")}
    proc = _vctl("args", cwd=repo, env=env)
    assert proc.returncode == 0, proc.stderr
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert any(ln.startswith("--") for ln in lines)
    assert any("Qwen/Qwen3.5-9B" in ln for ln in lines)
    assert any("data-parallel-size" in ln or "--data-parallel" in ln for ln in lines)


def test_info_shows_per_pool_urls(tmp_path: Path) -> None:
    """vctl info lists each pool's URL annotated with served_model."""
    # Use the multi-pool fixture pattern
    (tmp_path / "cluster.yaml").write_text(
        "apiVersion: vctl/v1\nkind: Cluster\n"
        "cluster: { venv: /v, state_dir: /s, env: {} }\n"
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
    (tmp_path / "models" / "a.yaml").write_text(
        "apiVersion: vctl/v1\nkind: Profile\n"
        "model: { name: M/A }\n"
        "resources: { num_gpus: 1, cuda_visible_devices: '0' }\n"
        "parallelism: { data_parallel: 1, tensor_parallel: 1, api_server_count: 1 }\n"
        "server: { http_port: 8000 }\nvllm_args: {}\nenv: {}\n"
    )
    env = {**os.environ, "CLUSTER_CONFIG": str(tmp_path / "cluster.yaml")}
    proc = _vctl("info", cwd=tmp_path, env=env)
    assert proc.returncode == 0, proc.stderr
    assert "pool[a]" in proc.stdout
    assert "pool[b]" in proc.stdout
    assert "M/A" in proc.stdout and "M/B" in proc.stdout
    assert "http://10.0.0.1:8080" in proc.stdout
    assert "http://10.0.0.1:8081" in proc.stdout
