"""`tctl init-config` scaffolds cluster.yaml + models."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tctl.config.settings import load_cluster_file, load_profile_file


def _vctl(*args: str, cwd: Path = Path(".")) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "tctl", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=10,
    )


def test_init_config_creates_files(tmp_path: Path) -> None:
    proc = _vctl("init-config", "--dir", str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "cluster.yaml").exists()
    for name in ("qwen3_5-9b", "qwen3-vl-30b-a3b"):
        assert (tmp_path / "models" / f"{name}.yaml").exists()


def test_init_config_files_validate(tmp_path: Path) -> None:
    """Generated configs must round-trip through pydantic validation."""
    _vctl("init-config", "--dir", str(tmp_path))
    cf = load_cluster_file(tmp_path / "cluster.yaml")
    assert cf.haproxy.kind == "haproxy"
    pf = load_profile_file(tmp_path / "models" / "qwen3_5-9b.yaml")
    assert pf.parallelism.data_parallel == 8
    assert pf.model.name == "Qwen/Qwen3.5-9B"


def test_init_config_refuses_overwrite_without_force(tmp_path: Path) -> None:
    (tmp_path / "cluster.yaml").write_text("existing: true\n")
    proc = _vctl("init-config", "--dir", str(tmp_path))
    assert proc.returncode == 2
    assert "refusing" in proc.stderr.lower() or "exist" in proc.stderr.lower()


def test_init_config_force_overwrites(tmp_path: Path) -> None:
    (tmp_path / "cluster.yaml").write_text("existing: true\n")
    proc = _vctl("init-config", "--dir", str(tmp_path), "--force")
    assert proc.returncode == 0
    assert "apiVersion: tctl/v1" in (tmp_path / "cluster.yaml").read_text()


def test_init_config_subset_profiles(tmp_path: Path) -> None:
    proc = _vctl("init-config", "--dir", str(tmp_path), "--profiles", "qwen3_5-9b")
    assert proc.returncode == 0
    assert (tmp_path / "models" / "qwen3_5-9b.yaml").exists()
    assert not (tmp_path / "models" / "qwen3-vl-30b-a3b.yaml").exists()


def test_init_config_unknown_profile_fails(tmp_path: Path) -> None:
    proc = _vctl("init-config", "--dir", str(tmp_path), "--profiles", "nonexistent")
    assert proc.returncode == 3
    assert "nonexistent" in proc.stderr


def test_init_config_files_validate_multi_pool(tmp_path: Path) -> None:
    """Generated cluster.yaml must use multi-pool layout with both models wired."""
    _vctl("init-config", "--dir", str(tmp_path))
    cf = load_cluster_file(tmp_path / "cluster.yaml")
    # New: multi-pool layout
    assert len(cf.haproxy.pools) == 2
    assert {p.served_model for p in cf.haproxy.pools} == {
        "Qwen/Qwen3.5-9B",
        "Qwen/Qwen3-VL-30B-A3B-Thinking",
    }
    pf = load_profile_file(tmp_path / "models" / "qwen3_5-9b.yaml")
    assert pf.parallelism.data_parallel == 8
    assert pf.model.name == "Qwen/Qwen3.5-9B"


def test_init_config_emits_multi_pool_cluster(tmp_path: Path) -> None:
    """The generated cluster.yaml must use haproxy.pools shape."""
    _vctl("init-config", "--dir", str(tmp_path))
    text = (tmp_path / "cluster.yaml").read_text()
    assert "pools:" in text
    # No legacy `client:` block (standalone haproxy.client field).
    # Note: "timeout_client:" is fine — check for the YAML key form only.
    assert "\n  client:\n" not in text
    assert "Qwen/Qwen3.5-9B" in text
    assert "Qwen/Qwen3-VL-30B-A3B-Thinking" in text


# ---------------------------------------------------------------------------
# AT-5 acceptance test
# ---------------------------------------------------------------------------


def test_at5_init_config_new_shape(tmp_path: Path) -> None:
    import tctl.cli as cli

    rc = cli.main(["init-config", "--dir", str(tmp_path)])
    assert rc == 0
    text = (tmp_path / "cluster.yaml").read_text()
    assert "apiVersion: tctl/v1" in text
    assert "haproxy:" in text
    assert "vllm:" in text
    assert "default_profile" in text
    assert "lb:" not in text
    assert "apiVersion: vctl/v1" not in text
