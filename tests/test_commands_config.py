"""`vctl config <verb>` end-to-end."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

FIX = Path(__file__).parent / "fixtures"


def _vctl(
    *args: str, cwd: Path | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
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
    env = {
        **os.environ,
        "VCTL_LB__HOST": "10.99.99.99",
        "CLUSTER_CONFIG": str(repo / "cluster.yaml"),
    }
    proc = _vctl("config", "show", cwd=repo, env=env)
    assert proc.returncode == 0
    assert "10.99.99.99" in proc.stdout


def test_schema_outputs_json() -> None:
    proc = _vctl("config", "schema")
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert "ClusterFile" in payload or "cluster" in payload


def test_migrate_dryrun_default(tmp_path: Path) -> None:
    """C4: migrate without --write prints diff to stdout, original unchanged."""
    src = tmp_path / "cluster.yaml"
    original = (FIX / "old_cluster.yaml").read_text()
    src.write_text(original)
    proc = _vctl("config", "migrate", str(src))
    assert proc.returncode == 0
    # Original file must be untouched.
    assert src.read_text() == original
    # No .bak should exist.
    assert not src.with_suffix(".yaml.bak").exists()


def test_migrate_write_creates_bak_and_updates_file(tmp_path: Path) -> None:
    """C4: --write creates .bak and rewrites the file with vctl/v1 content."""
    src = tmp_path / "cluster.yaml"
    original = (FIX / "old_cluster.yaml").read_text()
    src.write_text(original)
    proc = _vctl("config", "migrate", "--write", str(src))
    assert proc.returncode == 0
    text = src.read_text()
    assert "apiVersion: vctl/v1" in text
    assert "kind: Cluster" in text
    bak = src.with_suffix(".yaml.bak")
    assert bak.exists()
    assert bak.read_text() == original


def test_migrate_write_refuses_existing_bak_without_force(tmp_path: Path) -> None:
    """C4: --write refuses to clobber existing .bak unless --force is passed."""
    src = tmp_path / "cluster.yaml"
    src.write_text((FIX / "old_cluster.yaml").read_text())
    bak = src.with_suffix(".yaml.bak")
    bak.write_text("precious backup\n")
    proc = _vctl("config", "migrate", "--write", str(src))
    assert proc.returncode != 0
    assert bak.read_text() == "precious backup\n"


def test_migrate_write_force_overwrites_bak(tmp_path: Path) -> None:
    """C4: --write --force overwrites existing .bak."""
    src = tmp_path / "cluster.yaml"
    original = (FIX / "old_cluster.yaml").read_text()
    src.write_text(original)
    bak = src.with_suffix(".yaml.bak")
    bak.write_text("old backup\n")
    proc = _vctl("config", "migrate", "--write", "--force", str(src))
    assert proc.returncode == 0
    assert "apiVersion: vctl/v1" in src.read_text()
    # .bak now contains the original (not "old backup").
    assert bak.read_text() == original
