"""`vctl init-config` scaffolds cluster.yaml + models."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from vctl.config.settings import load_cluster_file, load_profile_file


def _vctl(*args: str, cwd: Path = Path(".")) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "vctl", *args],
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
    assert cf.lb.kind == "haproxy"
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
    assert "apiVersion: vctl/v1" in (tmp_path / "cluster.yaml").read_text()


def test_init_config_subset_profiles(tmp_path: Path) -> None:
    proc = _vctl("init-config", "--dir", str(tmp_path), "--profiles", "qwen3_5-9b")
    assert proc.returncode == 0
    assert (tmp_path / "models" / "qwen3_5-9b.yaml").exists()
    assert not (tmp_path / "models" / "qwen3-vl-30b-a3b.yaml").exists()


def test_init_config_unknown_profile_fails(tmp_path: Path) -> None:
    proc = _vctl("init-config", "--dir", str(tmp_path), "--profiles", "nonexistent")
    assert proc.returncode == 3
    assert "nonexistent" in proc.stderr
