"""`tctl config <verb>` end-to-end."""

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
    cmd = [sys.executable, "-m", "tctl", *args]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, env=env, timeout=10)


def test_validate_good(tmp_path: Path) -> None:
    """AT-6 case A."""
    f = tmp_path / "good.yaml"
    f.write_text((FIX / "tctl_sample_cluster.yaml").read_text())
    proc = _vctl("config", "validate", str(f))
    assert proc.returncode == 0


def test_validate_bad(tmp_path: Path) -> None:
    """AT-6 case B."""
    f = tmp_path / "bad.yaml"
    src = (FIX / "tctl_sample_cluster.yaml").read_text()
    src = src.replace("bind_port: 8080", 'bind_port: "eighty-eight"')
    f.write_text(src)
    proc = _vctl("config", "validate", str(f))
    assert proc.returncode != 0
    assert "bind_port" in proc.stderr


def test_show_with_env_override(tmp_path: Path) -> None:
    """AT-5: env-overridden field appears in `show` output."""
    repo = tmp_path
    (repo / "cluster.yaml").write_text((FIX / "tctl_sample_cluster.yaml").read_text())
    (repo / "models").mkdir()
    (repo / "models" / "qwen3-9b.yaml").write_text((FIX / "tctl_sample_profile.yaml").read_text())
    env = {
        **os.environ,
        "TCTL_HAPROXY__HOST": "10.99.99.99",
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


# ---------------------------------------------------------------------------
# AT-6 acceptance tests
# ---------------------------------------------------------------------------

_NEW_YAML = """
apiVersion: tctl/v1
cluster:
  venv: /venv
  state_dir: /tmp/state
haproxy:
  kind: haproxy
  host: 127.0.0.1
  admin: {bind_port: 9001}
  stats: {bind_port: 9000}
  pools: [{name: default, bind_port: 8000, served_model: "*"}]
vllm:
  default_profile: null
"""

_OLD_YAML = _NEW_YAML.replace("apiVersion: tctl/v1", "apiVersion: vctl/v1").replace(
    "haproxy:", "lb:"
)


def test_at6_validate_new_shape_ok(tmp_path: Path) -> None:
    f = tmp_path / "cluster.yaml"
    f.write_text(_NEW_YAML)
    import tctl.cli as cli

    assert cli.main(["config", "validate", str(f)]) == 0


def test_at6_validate_old_shape_rejects(tmp_path: Path) -> None:
    f = tmp_path / "cluster.yaml"
    f.write_text(_OLD_YAML)
    import tctl.cli as cli

    assert cli.main(["config", "validate", str(f)]) == 2
