"""info / profiles / args end-to-end."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

FIX = Path(__file__).parent / "fixtures"


def _vctl(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    # Always `python -m vctl` to avoid PATH collision with the bash prototype.
    cmd = [sys.executable, "-m", "vctl", *args]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=10)


def _make_repo(tmp_path: Path) -> Path:
    (tmp_path / "cluster.yaml").write_text((FIX / "sample_cluster.yaml").read_text())
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "qwen3-9b.yaml").write_text((FIX / "sample_profile.yaml").read_text())
    return tmp_path


def test_info_prints_resolved_fields(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    proc = _vctl("info", cwd=repo)
    assert proc.returncode == 0, proc.stderr
    assert "Qwen/Qwen3.5-9B" in proc.stdout
    assert "qwen3-9b" in proc.stdout
    assert "10.0.0.1" in proc.stdout


def test_profiles_lists_models(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "models" / "other.yaml").write_text("apiVersion: vctl/v1\nkind: Profile\n")
    proc = _vctl("profiles", cwd=repo)
    assert proc.returncode == 0, proc.stderr
    assert "qwen3-9b" in proc.stdout
    assert "other" in proc.stdout
    assert "*" in proc.stdout or "default" in proc.stdout


def test_args_emits_one_flag_per_line(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    proc = _vctl("args", cwd=repo)
    assert proc.returncode == 0, proc.stderr
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert any(ln.startswith("--") for ln in lines)
    assert any("Qwen/Qwen3.5-9B" in ln for ln in lines)
    assert any("data-parallel-size" in ln or "--data-parallel" in ln for ln in lines)
