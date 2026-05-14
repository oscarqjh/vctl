"""Shared pytest fixtures."""

from __future__ import annotations

import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Keep tests env-deterministic.
for var in ("TCTL_PROFILE", "TCTL_HAPROXY__HOST", "TCTL_TEST_NO_SOCKET"):
    os.environ.pop(var, None)


# ---------------------------------------------------------------------------
# Auto-skip integration markers unless explicitly requested via -m
# ---------------------------------------------------------------------------

_SLOW_MARKERS = ("integration", "vllm_supervisor_integration")


def pytest_collection_modifyitems(config: pytest.Config, items: Sequence[pytest.Item]) -> None:
    """Skip tests decorated with slow integration markers unless the marker is
    explicitly requested on the command line (e.g. ``-m vllm_supervisor_integration``).
    """
    marker_expr = config.option.markexpr if hasattr(config.option, "markexpr") else ""
    for marker_name in _SLOW_MARKERS:
        if marker_name in marker_expr:
            # Marker was explicitly requested — let pytest run them as-is.
            continue
        skip_mark = pytest.mark.skip(reason=f"requires real environment; run with -m {marker_name}")
        for item in items:
            if item.get_closest_marker(marker_name) is not None:
                item.add_marker(skip_mark, append=False)


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


# ---------------------------------------------------------------------------
# F7: helpers + session-scoped autouse fixture — sweep any vctl-serve /
# fake-vllm orphans whose cmdline references a pytest temp path.
# Matches ONLY on /tmp/pytest-of-* or /tmp/tmp*/bin/vllm patterns so we
# never touch production serve processes (which live under ~/.vctl/... or
# /mnt/... paths).
# ---------------------------------------------------------------------------


def _force_cleanup_vctl_serve_for_path(stub_path: Path) -> int:
    """SIGKILL any vctl-serve / fake-vllm orphans whose cmdline contains stub_path.

    Returns count of killed processes. Specific to test paths; never matches prod.
    The *stub_path* argument should be the test fixture's temporary bin directory
    (e.g. ``tmp_path / "bin"``).  Only processes whose cmdline includes that path
    as a substring are killed, so ``~/.vctl/`` paths are never matched.
    """
    try:
        import psutil
    except ImportError:
        return 0

    target = str(stub_path)
    killed = 0
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmd = proc.info.get("cmdline") or []
            cmd_str = " ".join(cmd)
            if target in cmd_str:
                proc.send_signal(signal.SIGKILL)
                killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return killed


@pytest.fixture(scope="session", autouse=True)
def _sweep_leaked_vctl_serve_at_session_end() -> pytest.Generator[None, None, None]:
    """Yield during tests; after the session SIGKILL any ``python -m vctl.*serve``
    or fake-vllm process whose cmdline contains ``/tmp/pytest-of-`` or whose
    path matches ``/tmp/tmp*/bin/vllm``.

    This is the F7 counterpart to F3's haproxy sweep.  Never matches production
    serve invocations (which run under ``~/.vctl/``, ``/mnt/aigc/``, etc.).
    """
    yield  # tests run here

    try:
        import psutil
    except ImportError:
        return

    _pytest_markers = ("/tmp/pytest-of-", "/tmp/tmp")

    killed = 0
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmd = proc.info.get("cmdline") or []
            if not cmd:
                continue
            cmd_str = " ".join(cmd)
            # Must look like a tctl vllm serve invocation OR a fake-vllm under a pytest tmpdir.
            is_vctl_serve = "tctl" in cmd_str and "vllm" in cmd_str and "serve" in cmd_str
            is_fake_vllm = "/bin/vllm" in cmd_str
            if not (is_vctl_serve or is_fake_vllm):
                continue
            # Restrict to pytest-owned paths — never match production.
            if any(marker in cmd_str for marker in _pytest_markers):
                proc.send_signal(signal.SIGKILL)
                killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if killed:
        import warnings

        warnings.warn(
            f"[F7] swept {killed} leaked tctl-vllm-serve/fake-vllm process(es) pointing at "
            "pytest temp paths after session end",
            stacklevel=1,
        )
