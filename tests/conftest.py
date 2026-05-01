"""Shared pytest fixtures."""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Keep tests env-deterministic.
for var in ("VCTL_PROFILE", "MODEL_PROFILE", "VCTL_LB__HOST"):
    os.environ.pop(var, None)


# ---------------------------------------------------------------------------
# F3: session-scoped autouse fixture — sweep any haproxy whose cmdline points
# at /tmp/pytest-of-* at the end of the full test session.
# This catches cross-test leaks without ever touching ~/.vctl/lb/ (production).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _sweep_leaked_haproxy_at_session_end() -> pytest.Generator[None, None, None]:
    """Yield during tests; after the session, SIGKILL any haproxy pointing at
    /tmp/pytest-of-* paths (pytest's temp directory for all test workers)."""
    yield  # tests run here

    try:
        import psutil
    except ImportError:
        return

    killed = 0
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            name = proc.info.get("name") or ""
            cmd = proc.info.get("cmdline") or []
            if "haproxy" not in name and not any("haproxy" in p for p in cmd[:1]):
                continue
            # Only kill haproxy processes whose config file is under /tmp/pytest-of-
            if any("/tmp/pytest-of-" in tok for tok in cmd):
                proc.send_signal(signal.SIGKILL)
                killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if killed:
        import warnings

        warnings.warn(
            f"[F3] swept {killed} leaked haproxy process(es) pointing at "
            "/tmp/pytest-of-* after session end",
            stacklevel=1,
        )
