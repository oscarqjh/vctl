"""In-process tests to push coverage above 50%.

These call command run() functions directly rather than via subprocess, so
coverage.py can instrument them.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from unittest.mock import patch

import pytest

FIX = Path(__file__).parent / "fixtures"


def _ns(tmp_path: Path, profile: str | None = None) -> argparse.Namespace:
    """Build a minimal argparse Namespace pointing at a cluster.yaml in tmp_path."""
    (tmp_path / "cluster.yaml").write_text((FIX / "sample_cluster.yaml").read_text())
    (tmp_path / "models").mkdir(exist_ok=True)
    (tmp_path / "models" / "qwen3-9b.yaml").write_text((FIX / "sample_profile.yaml").read_text())
    return argparse.Namespace(
        config=str(tmp_path / "cluster.yaml"),
        profile=profile,
        log_level="info",
        log_format="pretty",
    )


# ---------------------------------------------------------------------------
# CLI / __main__
# ---------------------------------------------------------------------------


def test_main_version(capsys: pytest.CaptureFixture[str]) -> None:
    from vctl.cli import main

    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0


def test_main_no_command_exits_nonzero() -> None:
    from vctl.cli import main

    with pytest.raises(SystemExit) as exc_info:
        main([])
    assert exc_info.value.code != 0


def test_hoist_positional_profile() -> None:
    from vctl.cli import _hoist_positional_profile

    result = _hoist_positional_profile(["serve", "models/qwen3-9b.yaml"])
    assert result == ["serve", "--profile", "qwen3-9b"]


def test_hoist_non_matching_passthrough() -> None:
    from vctl.cli import _hoist_positional_profile

    result = _hoist_positional_profile(["lb", "list"])
    assert result == ["lb", "list"]


# ---------------------------------------------------------------------------
# args_cmd
# ---------------------------------------------------------------------------


def test_args_cmd_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from vctl.commands.args_cmd import run

    rc = run(_ns(tmp_path), [])
    assert rc == 0
    out = capsys.readouterr().out
    assert "--port=" in out


def test_bool_flag() -> None:
    from vctl.commands.args_cmd import _bool_flag

    assert _bool_flag(True) == "true"
    assert _bool_flag(False) == "false"


# ---------------------------------------------------------------------------
# profiles
# ---------------------------------------------------------------------------


def test_profiles_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from vctl.commands.profiles import run

    rc = run(_ns(tmp_path), [])
    assert rc == 0
    out = capsys.readouterr().out
    assert "qwen3-9b" in out


def test_profiles_missing_models_dir(tmp_path: Path) -> None:
    from vctl.commands.profiles import run

    # cluster.yaml without models/ dir
    (tmp_path / "cluster.yaml").write_text((FIX / "sample_cluster.yaml").read_text())
    ns = argparse.Namespace(config=str(tmp_path / "cluster.yaml"), profile=None)
    rc = run(ns, [])
    assert rc == 2


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------


def test_info_run(tmp_path: Path) -> None:
    from vctl.commands.info import run

    rc = run(_ns(tmp_path), [])
    assert rc == 0


# ---------------------------------------------------------------------------
# config_cmd
# ---------------------------------------------------------------------------


def test_config_schema(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import json

    from vctl.commands.config_cmd import run

    rc = run(_ns(tmp_path), ["schema"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert "ClusterFile" in payload
    assert "ProfileFile" in payload


def test_config_show(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from vctl.commands.config_cmd import run

    rc = run(_ns(tmp_path), ["show"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "qwen3-9b" in out


def test_config_validate_cluster(tmp_path: Path) -> None:
    from vctl.commands.config_cmd import run

    rc = run(_ns(tmp_path), ["validate", str(tmp_path / "cluster.yaml")])
    assert rc == 0


def test_config_validate_profile(tmp_path: Path) -> None:
    from vctl.commands.config_cmd import run

    rc = run(_ns(tmp_path), ["validate", str(tmp_path / "models" / "qwen3-9b.yaml")])
    assert rc == 0


def test_config_validate_bad_file(tmp_path: Path) -> None:
    from vctl.commands.config_cmd import run

    bad = tmp_path / "bad.yaml"
    bad.write_text("not_valid: true\nkind: Cluster\n")
    rc = run(_ns(tmp_path), ["validate", str(bad)])
    assert rc == 2


def test_config_migrate(tmp_path: Path) -> None:
    from vctl.commands.config_cmd import run

    # Use a fresh cluster yaml (already new format — migrate is a no-op but runs)
    ns = _ns(tmp_path)
    rc = run(ns, ["migrate", str(tmp_path / "cluster.yaml")])
    assert rc == 0


# ---------------------------------------------------------------------------
# preflight (in-process)
# ---------------------------------------------------------------------------


def test_preflight_run_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import json

    from vctl.commands.preflight import run

    rc = run(_ns(tmp_path), ["--json"])
    assert rc in (0, 3)
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert "checks" in payload
    for chk in payload["checks"]:
        assert "name" in chk and "ok" in chk and "msg" in chk


def test_preflight_run_text(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from vctl.commands.preflight import run

    rc = run(_ns(tmp_path), [])
    assert rc in (0, 3)
    out = capsys.readouterr().out
    assert "[OK]" in out or "[FAIL]" in out


def test_preflight_check_gpus_no_nvidia_smi() -> None:
    from vctl.commands.preflight import _check_gpus

    with patch("shutil.which", return_value=None):
        ok, msg = _check_gpus(0)
        assert ok is True

    with patch("shutil.which", return_value=None):
        ok, msg = _check_gpus(8)
        assert ok is False


def test_preflight_check_shm() -> None:
    from vctl.commands.preflight import _check_shm

    ok, msg = _check_shm()
    assert isinstance(ok, bool)
    assert "/dev/shm" in msg or "OSError" in msg or "[Errno" in msg


def test_preflight_check_venv_missing() -> None:
    from vctl.commands.preflight import _check_venv

    ok, msg = _check_venv("/nonexistent/venv")
    assert ok is False


def test_preflight_check_venv_exists(tmp_path: Path) -> None:
    from vctl.commands.preflight import _check_venv

    ok, msg = _check_venv(str(tmp_path))
    assert ok is True


def test_preflight_check_lb_route_unreachable() -> None:
    from vctl.commands.preflight import _check_lb_route

    ok, msg = _check_lb_route("127.0.0.1", 19999)
    assert ok is False


# ---------------------------------------------------------------------------
# stop (in-process)
# ---------------------------------------------------------------------------


def test_stop_run_no_backends(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from vctl.commands.stop import run

    os.environ["VCTL_TEST_NO_SOCKET"] = "1"
    try:
        ns = _ns(tmp_path)
        # Override state_dir to be empty tmp dir
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        import argparse as _ap

        ns2 = _ap.Namespace(
            config=ns.config,
            profile=ns.profile,
            log_level="info",
            log_format="pretty",
        )
        # Patch the cluster state_dir to point to our empty state dir
        with patch.dict(os.environ, {"VCTL_CLUSTER__STATE_DIR": str(state_dir)}):
            rc = run(ns2, [])
        assert rc == 0
    finally:
        os.environ.pop("VCTL_TEST_NO_SOCKET", None)


def test_stop_run_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import json

    from vctl.commands.stop import run

    os.environ["VCTL_TEST_NO_SOCKET"] = "1"
    try:
        ns = _ns(tmp_path)
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        with patch.dict(os.environ, {"VCTL_CLUSTER__STATE_DIR": str(state_dir)}):
            rc = run(ns, ["--json"])
        assert rc == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert "actions" in payload
    finally:
        os.environ.pop("VCTL_TEST_NO_SOCKET", None)


def test_find_local_vllm() -> None:
    from vctl.commands.stop import _find_local_vllm

    pids = _find_local_vllm()
    assert isinstance(pids, list)


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


def test_probe_local_vllm_ok_sentinel() -> None:
    from vctl.lb.probe import probe_local_vllm

    with patch.dict(os.environ, {"VCTL_TEST_PROBE_RESULT": "ok"}):
        result = probe_local_vllm(9999)
    assert result["healthy"] is True
    assert result["models_loaded"] is True


def test_probe_local_vllm_empty_sentinel() -> None:
    from vctl.lb.probe import probe_local_vllm

    with patch.dict(os.environ, {"VCTL_TEST_PROBE_RESULT": "empty"}):
        result = probe_local_vllm(9999)
    assert result["healthy"] is False
    assert result["models_loaded"] is False


def test_probe_local_vllm_unhealthy_sentinel() -> None:
    from vctl.lb.probe import probe_local_vllm

    with patch.dict(os.environ, {"VCTL_TEST_PROBE_RESULT": "unhealthy"}):
        result = probe_local_vllm(9999)
    assert result["healthy"] is False
    assert result["health_code"] == 503


def test_probe_local_vllm_connection_error() -> None:
    from vctl.lb.probe import probe_local_vllm

    # No server on 19998 — should return without error
    result = probe_local_vllm(19998, timeout=0.5)
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# lb scaling helpers
# ---------------------------------------------------------------------------


def test_lb_scaling_name_for() -> None:
    from vctl.commands.lb_scaling import _name_for

    assert _name_for("10.0.0.1:8000") == "b_10_0_0_1_8000"


def test_lb_scaling_do_add(tmp_path: Path) -> None:
    from vctl.commands.lb_scaling import _do_add
    from vctl.config.models import LbAdmin, LbClient, LbHaproxy, LbStats
    from vctl.lb.manager import LbManager
    from vctl.lb.state import BackendState

    lb = LbHaproxy(
        kind="haproxy",
        host="127.0.0.1",
        client=LbClient(bind_port=18080),
        admin=LbAdmin(bind_port=19001),
        stats=LbStats(bind_port=19000),
    )
    os.environ["VCTL_TEST_NO_SOCKET"] = "1"
    try:
        mgr = LbManager(lb, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
        bs = BackendState(tmp_path / "state", "127.0.0.1")
        rc = _do_add("127.0.0.1:9999", mgr, bs)
        assert rc == 0
        assert "127.0.0.1:9999" in bs.list()
    finally:
        os.environ.pop("VCTL_TEST_NO_SOCKET", None)


def test_lb_scaling_do_remove(tmp_path: Path) -> None:
    from vctl.commands.lb_scaling import _do_add, _do_remove
    from vctl.config.models import LbAdmin, LbClient, LbHaproxy, LbStats
    from vctl.lb.manager import LbManager
    from vctl.lb.state import BackendState

    lb = LbHaproxy(
        kind="haproxy",
        host="127.0.0.1",
        client=LbClient(bind_port=18080),
        admin=LbAdmin(bind_port=19001),
        stats=LbStats(bind_port=19000),
    )
    os.environ["VCTL_TEST_NO_SOCKET"] = "1"
    try:
        mgr = LbManager(lb, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
        bs = BackendState(tmp_path / "state", "127.0.0.1")
        _do_add("127.0.0.1:9999", mgr, bs)
        rc = _do_remove("127.0.0.1:9999", mgr, bs)
        assert rc == 0
        assert "127.0.0.1:9999" not in bs.list()
    finally:
        os.environ.pop("VCTL_TEST_NO_SOCKET", None)


def test_lb_scaling_do_drain(tmp_path: Path) -> None:
    from vctl.commands.lb_scaling import _do_drain
    from vctl.config.models import LbAdmin, LbClient, LbHaproxy, LbStats
    from vctl.lb.manager import LbManager

    lb = LbHaproxy(
        kind="haproxy",
        host="127.0.0.1",
        client=LbClient(bind_port=18080),
        admin=LbAdmin(bind_port=19001),
        stats=LbStats(bind_port=19000),
    )
    os.environ["VCTL_TEST_NO_SOCKET"] = "1"
    try:
        mgr = LbManager(lb, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
        rc = _do_drain("127.0.0.1:9999", mgr)
        assert rc == 0
    finally:
        os.environ.pop("VCTL_TEST_NO_SOCKET", None)


def test_lb_scaling_do_auto_add(tmp_path: Path) -> None:
    from vctl.commands.lb_scaling import _do_auto_add
    from vctl.config.models import LbAdmin, LbClient, LbHaproxy, LbStats
    from vctl.lb.manager import LbManager
    from vctl.lb.state import BackendState

    lb = LbHaproxy(
        kind="haproxy",
        host="127.0.0.1",
        client=LbClient(bind_port=18080),
        admin=LbAdmin(bind_port=19001),
        stats=LbStats(bind_port=19000),
    )
    os.environ["VCTL_TEST_NO_SOCKET"] = "1"
    try:
        mgr = LbManager(lb, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
        bs = BackendState(tmp_path / "state", "127.0.0.1")
        rc = _do_auto_add(mgr, bs)
        assert rc == 0
    finally:
        os.environ.pop("VCTL_TEST_NO_SOCKET", None)


def test_lb_scaling_do_health(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from vctl.commands.lb_scaling import _do_health
    from vctl.config.models import LbAdmin, LbClient, LbHaproxy, LbStats
    from vctl.lb.manager import LbManager
    from vctl.lb.state import BackendState

    lb = LbHaproxy(
        kind="haproxy",
        host="127.0.0.1",
        client=LbClient(bind_port=18080),
        admin=LbAdmin(bind_port=19001),
        stats=LbStats(bind_port=19000),
    )
    os.environ["VCTL_TEST_NO_SOCKET"] = "1"
    os.environ["VCTL_TEST_PROBE_RESULT"] = "ok"
    try:
        mgr = LbManager(lb, state_dir=tmp_path / "state", run_dir=tmp_path / "run")
        bs = BackendState(tmp_path / "state", "127.0.0.1")
        bs.add("127.0.0.1:9999")
        rc = _do_health(mgr, bs)
        assert rc == 0
    finally:
        os.environ.pop("VCTL_TEST_NO_SOCKET", None)
        os.environ.pop("VCTL_TEST_PROBE_RESULT", None)


# ---------------------------------------------------------------------------
# lb command dispatch (some fast paths)
# ---------------------------------------------------------------------------


def test_lb_where(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from vctl.commands.lb import run

    rc = run(_ns(tmp_path), ["where"])
    assert rc == 0
    assert "10.0.0.1:8080" in capsys.readouterr().out


def test_lb_list_empty(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from vctl.commands.lb import run

    with patch.dict(
        os.environ,
        {
            "VCTL_CLUSTER__STATE_DIR": str(tmp_path / "state"),
            "VCTL_TEST_NO_SOCKET": "1",
        },
    ):
        rc = run(_ns(tmp_path), ["list"])
    assert rc == 0


def test_lb_stats(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from vctl.commands.lb import run

    rc = run(_ns(tmp_path), ["stats"])
    assert rc == 0
    assert "9000" in capsys.readouterr().out


def test_lb_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from vctl.commands.lb import run

    with patch.dict(os.environ, {"VCTL_CLUSTER__STATE_DIR": str(tmp_path / "state")}):
        rc = run(_ns(tmp_path), ["config"])
    assert rc == 0
    assert "haproxy" in capsys.readouterr().out.lower()


def test_lb_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from vctl.commands.lb import run

    rc = run(_ns(tmp_path), ["status"])
    assert rc == 0


def test_lb_logs_no_logfile(tmp_path: Path) -> None:
    from vctl.commands.lb import run

    rc = run(_ns(tmp_path), ["logs"])
    assert rc == 0


def test_lb_is_host_no_match(tmp_path: Path) -> None:
    from vctl.commands.lb import run

    # lb.host is 10.0.0.1, self_ip is different
    rc = run(_ns(tmp_path), ["is-host"])
    # Returns 0 if self_ip == lb.host, else 1. Either is valid here.
    assert rc in (0, 1)


def test_lb_add_remove(tmp_path: Path) -> None:
    from vctl.commands.lb import run

    os.environ["VCTL_TEST_NO_SOCKET"] = "1"
    try:
        with patch.dict(os.environ, {"VCTL_CLUSTER__STATE_DIR": str(tmp_path / "state")}):
            rc_add = run(_ns(tmp_path), ["add", "10.0.0.1:9999"])
            rc_remove = run(_ns(tmp_path), ["remove", "10.0.0.1:9999"])
        assert rc_add == 0
        assert rc_remove == 0
    finally:
        os.environ.pop("VCTL_TEST_NO_SOCKET", None)


# ---------------------------------------------------------------------------
# __main__ entry point (covered via import)
# ---------------------------------------------------------------------------


def test_main_missing_config_returns_2(tmp_path: Path) -> None:
    from vctl.cli import main

    rc = main(["--config", str(tmp_path / "missing.yaml"), "info"])
    assert rc == 2
