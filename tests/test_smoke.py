"""Smoke tests for vctl CLI startup."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time


def test_help_runs_quickly() -> None:
    """`vctl --help` must complete in <200 ms (AT-1)."""
    binary = shutil.which("vctl") or sys.executable
    args = [binary] if binary != sys.executable else [sys.executable, "-m", "vctl"]
    t0 = time.perf_counter()
    proc = subprocess.run([*args, "--help"], capture_output=True, text=True, timeout=5)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert proc.returncode == 0, proc.stderr
    assert "vctl" in proc.stdout.lower()
    assert elapsed_ms < 200, f"help took {elapsed_ms:.0f} ms (>200 ms budget)"


def test_module_entry_point() -> None:
    """`python -m vctl --help` must work."""
    proc = subprocess.run(
        [sys.executable, "-m", "vctl", "--help"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert proc.returncode == 0
