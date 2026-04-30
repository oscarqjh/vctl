"""`vctl config <verb>` end-to-end."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

FIX = Path(__file__).parent / "fixtures"


def _vctl(*args: str, cwd: Path | None = None,
          env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "vctl", *args]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, env=env, timeout=10)


def test_validate_good(tmp_path: Path) -> None:
    """AT-6 case A."""
    f = tmp_path / "good.yaml"
    f.write_text((FIX / "sample_cluster.yaml").read_text())
    proc = _vctl("config", "validate", str(f))
    assert proc.returncode == 0


def test_validate_bad(tmp_path: Path) -> None:
    """AT-6 case B."""
    f = tmp_path / "bad.yaml"
    src = (FIX / "sample_cluster.yaml").read_text()
    src = src.replace("bind_port: 8080", 'bind_port: "eighty-eight"')
    f.write_text(src)
    proc = _vctl("config", "validate", str(f))
    assert proc.returncode != 0
    assert "lb" in proc.stderr and "bind_port" in proc.stderr


def test_show_with_env_override(tmp_path: Path) -> None:
    """AT-5: env-overridden field appears in `show` output."""
    repo = tmp_path
    (repo / "cluster.yaml").write_text((FIX / "sample_cluster.yaml").read_text())
    (repo / "models").mkdir()
    (repo / "models" / "qwen3-9b.yaml").write_text((FIX / "sample_profile.yaml").read_text())
    env = {**os.environ, "VCTL_LB__HOST": "10.99.99.99"}
    proc = _vctl("config", "show", cwd=repo, env=env)
    assert proc.returncode == 0
    assert "10.99.99.99" in proc.stdout


def test_schema_outputs_json() -> None:
    proc = _vctl("config", "schema")
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert "ClusterFile" in payload or "cluster" in payload


def test_migrate_writes_v1_doc(tmp_path: Path) -> None:
    """AT-4."""
    src = tmp_path / "cluster.yaml"
    src.write_text((FIX / "old_cluster.yaml").read_text())
    proc = _vctl("config", "migrate", str(src))
    assert proc.returncode == 0
    text = src.read_text()
    assert "apiVersion: vctl/v1" in text
    assert "kind: Cluster" in text
