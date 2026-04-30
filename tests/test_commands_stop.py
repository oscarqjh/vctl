"""`vctl stop` drains + removes self + reaps tree."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

FIX = Path(__file__).parent / "fixtures"


def test_stop_no_running_serve_is_noop(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / "cluster.yaml").write_text((FIX / "sample_cluster.yaml").read_text())
    (repo / "models").mkdir()
    (repo / "models" / "qwen3-9b.yaml").write_text((FIX / "sample_profile.yaml").read_text())
    env = {
        **os.environ,
        "VCTL_CLUSTER__STATE_DIR": str(tmp_path / "state"),
        "VCTL_TEST_NO_SOCKET": "1",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "vctl", "stop"],
        capture_output=True,
        text=True,
        cwd=repo,
        env=env,
        timeout=10,
    )
    assert proc.returncode == 0
