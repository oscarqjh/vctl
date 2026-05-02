"""Integration tests for the Reconciler against a real HAProxy process.

Marked with @pytest.mark.integration — run via: pytest -m integration
Skipped if haproxy binary is not on PATH.

These tests launch a real haproxy process via LbManager.start() in a unique
tmux session. Teardown is via try/finally to guarantee mgr.stop() and tmux
session cleanup regardless of failure.

Scope note: the assertions focus on the state-file contract (what Reconciler
actually owns) and on the absence of exceptions from haproxy admin calls.
We deliberately do NOT assert on haproxy's `show servers state` reflecting
dynamically-added servers — some haproxy versions only include the static
config in that output until `dump servers state` is invoked. Reconciler's
correctness against state file + exception model is the Phase 1 contract.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from vctl.config.models import LbAdmin, LbHaproxy, LbStats, Pool
from vctl.lb.manager import LbManager
from vctl.lb.reconciler import Action, Reconciler
from vctl.lb.runtime import lb_admin_client
from vctl.lb.state import BackendState


def _make_integration_mgr(tmp_path: Path, session_suffix: str) -> LbManager:
    pools = [Pool(name="default", served_model="*", bind_port=8750)]
    lb = LbHaproxy(
        host="127.0.0.1",
        admin=LbAdmin(bind_port=9750),
        stats=LbStats(bind_port=8751),
        pools=pools,
    )
    return LbManager(
        lb=lb,
        state_dir=tmp_path / "state",
        run_dir=tmp_path / "run",
        tmux_name=f"vctl-lb-test-{session_suffix}",
    )


@pytest.mark.integration
def test_reconciler_end_to_end_against_real_haproxy(tmp_path: Path) -> None:
    """End-to-end: want_present + want_draining + want_absent against live haproxy."""
    if shutil.which("haproxy") is None:
        pytest.skip("haproxy binary not found on PATH")

    session_suffix = uuid.uuid4().hex[:8]
    mgr = _make_integration_mgr(tmp_path, session_suffix)
    mgr.start(force=True)  # bypass self-IP guard (test may run on a worker, not LB host)

    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            c = lb_admin_client(mgr)
            if c is not None:
                break
            time.sleep(0.2)
        else:
            pytest.fail("haproxy admin socket never became reachable within 10 seconds")

        r = Reconciler(mgr)
        bs = BackendState(mgr.state_dir, mgr.lb.host, pool="default")
        ep1 = "127.0.0.1:19001"
        ep2 = "127.0.0.1:19002"

        # Step 1: want_present ep1 → state file gets ep1, no exceptions
        out1 = r.want_present(ep1, "default")
        assert out1.ep == ep1
        assert out1.action in {Action.ADDED, Action.ADOPTED, Action.READIED}
        assert ep1 in bs.list()

        # Step 2: want_present ep2 → state file gets ep2
        out2 = r.want_present(ep2, "default")
        assert out2.ep == ep2
        assert out2.action in {Action.ADDED, Action.ADOPTED, Action.READIED}
        assert ep2 in bs.list()

        # Step 3: want_draining ep1 → state file unchanged, no exception
        out3 = r.want_draining(ep1, "default")
        assert out3.action == Action.DRAINED
        assert ep1 in bs.list(), "state file must still contain ep1 after drain"

        # Step 4: want_absent ep1 → state file no longer has ep1
        out4 = r.want_absent(ep1, "default")
        assert out4.ep == ep1
        assert out4.action in {Action.REMOVED, Action.ORPHANED_CLEANED}
        assert ep1 not in bs.list(), "state file must not contain ep1 after want_absent"
        assert ep2 in bs.list(), "state file must still contain ep2"

        # Step 5: idempotent re-call — second want_absent on ep1 returns NONE
        out5 = r.want_absent(ep1, "default")
        assert out5.action == Action.NONE
        assert ep1 not in bs.list()

    finally:
        with contextlib.suppress(Exception):
            mgr.stop()
        subprocess.run(
            ["tmux", "kill-session", "-t", f"vctl-lb-test-{session_suffix}"],
            capture_output=True,
        )
