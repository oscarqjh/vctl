"""`vctl lb health` probes registered backends."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

FIX = Path(__file__).parent / "fixtures"


def test_lb_health_ok_when_all_healthy(tmp_path: Path) -> None:
    (tmp_path / "cluster.yaml").write_text((FIX / "sample_cluster.yaml").read_text())
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "qwen3-9b.yaml").write_text((FIX / "sample_profile.yaml").read_text())
    env = {
        **os.environ,
        "VCTL_CLUSTER__STATE_DIR": str(tmp_path / "state"),
        "VCTL_TEST_PROBE_RESULT": "ok",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "vctl", "lb", "health"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=10,
    )
    # Exit 0 (no backends registered → healthy by default).
    assert proc.returncode == 0
