"""`vctl preflight` returns structured pass/fail."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

FIX = Path(__file__).parent / "fixtures"


def _make_repo(tmp_path: Path) -> Path:
    (tmp_path / "cluster.yaml").write_text((FIX / "sample_cluster.yaml").read_text())
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "qwen3-9b.yaml").write_text((FIX / "sample_profile.yaml").read_text())
    return tmp_path


def test_preflight_json(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "vctl", "preflight", "--json"],
        capture_output=True,
        text=True,
        cwd=repo,
        timeout=10,
    )
    assert proc.returncode in (0, 3)
    payload = json.loads(proc.stdout)
    assert "checks" in payload
