"""tests/test_tctl_cli.py — tctl CLI dispatch + positional hoister tests."""

from __future__ import annotations

import subprocess
import sys

import pytest

# ---------------------------------------------------------------------------
# AT-1: --help lists workloads; lmms is absent
# ---------------------------------------------------------------------------


def test_at1_help_lists_workloads_not_lmms(capsys: pytest.CaptureFixture[str]) -> None:
    import tctl.cli as cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert "vllm" in out
    assert "haproxy" in out
    assert "config" in out
    assert "init-config" in out
    assert "lmms" not in out
    assert exc.value.code == 0


def test_help_lists_visible_workloads(capsys: pytest.CaptureFixture[str]) -> None:
    from tctl.cli import build_parser

    parser = build_parser()
    help_text = parser.format_help()
    assert "vllm" in help_text
    assert "haproxy" in help_text
    assert "lmms" not in help_text  # hidden
    assert "config" in help_text
    assert "init-config" in help_text


# ---------------------------------------------------------------------------
# AT-2: tctl vllm --help lists all vllm sub-verbs
# ---------------------------------------------------------------------------


def test_at2_vllm_help_lists_all_verbs(capsys: pytest.CaptureFixture[str]) -> None:
    import tctl.cli as cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["vllm", "--help"])
    out = capsys.readouterr().out
    for verb in ("info", "profiles", "args", "preflight", "serve", "stop", "rolling-restart"):
        assert verb in out, f"expected {verb!r} in vllm --help"
    assert exc.value.code == 0


# ---------------------------------------------------------------------------
# AT-4: lmms reachable when named directly; absent from top-level help
# ---------------------------------------------------------------------------


def test_at4_lmms_reachable_when_named_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[int] = []
    import tctl.workloads.lmms.commands as _cmds

    monkeypatch.setattr(_cmds, "_cmd_run_loop", lambda ns, rest: called.append(1) or 0)
    import tctl.cli as cli

    rc = cli.main(["lmms", "run-loop"])
    assert rc == 0
    assert called, "lmms run-loop was not dispatched"


def test_dispatch_lmms_works_even_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    """tctl lmms run-loop dispatches even though lmms hidden from --help"""
    called: list[object] = []
    import tctl.workloads.lmms.commands as _cmds

    monkeypatch.setattr(_cmds, "_cmd_run_loop", lambda ns, argv: called.append(argv) or 0)
    from tctl.cli import main

    rc = main(["lmms", "run-loop"])
    assert rc == 0


# ---------------------------------------------------------------------------
# AT-11: MODEL_PROFILE is ignored by cli
# ---------------------------------------------------------------------------


def test_at11_model_profile_not_consulted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pytest.TempPathFactory,
) -> None:
    monkeypatch.setenv("MODEL_PROFILE", "legacymodel")
    monkeypatch.delenv("TCTL_PROFILE", raising=False)
    from pathlib import Path

    cfg_file = Path(str(tmp_path)) / "cluster.yaml"
    cfg_file.write_text(
        "apiVersion: tctl/v1\ncluster:\n  venv: /venv\n  state_dir: /tmp/state\n"
        "haproxy:\n  kind: haproxy\n  host: 127.0.0.1\n"
        "  admin: {bind_port: 9001}\n  stats: {bind_port: 9000}\n"
        "  pools: [{name: default, bind_port: 8000, served_model: '*'}]\n"
        "vllm:\n  default_profile: null\n"
    )
    import tctl.cli as cli

    # --config must come before the workload name so global argparse sees it
    rc = cli.main(["--config", str(cfg_file), "vllm", "info"])
    # Should exit 2 (no profile), NOT use "legacymodel"
    assert rc == 2


# ---------------------------------------------------------------------------
# AT-12: New workload requires only 3 steps; no core file changes
# ---------------------------------------------------------------------------


def test_at12_new_workload_requires_only_3_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """New workload can be added by: create __init__.py with run(), add to _WORKLOADS."""
    import types

    import tctl.cli as cli

    # Simulate a new workload module (step 3: add entry to _WORKLOADS)
    fake_mod = types.ModuleType("tctl.workloads.demo")
    fake_mod.run = lambda ns, argv_rest: 0  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tctl.workloads.demo", fake_mod)

    original = dict(cli._WORKLOADS)
    try:
        cli._WORKLOADS["demo"] = ("tctl.workloads.demo", False)
        rc = cli.main(["demo"])
    finally:
        cli._WORKLOADS.clear()
        cli._WORKLOADS.update(original)
        sys.modules.pop("tctl.workloads.demo", None)
    assert rc == 0


# ---------------------------------------------------------------------------
# Dispatch tests
# ---------------------------------------------------------------------------


def test_dispatch_vllm_workload(monkeypatch: pytest.MonkeyPatch) -> None:
    """tctl vllm <verb> dispatches to tctl.workloads.vllm.run"""
    calls: list[tuple[object, object]] = []

    def fake_run(ns: object, argv_rest: object) -> int:
        calls.append((ns, argv_rest))
        return 0

    monkeypatch.setattr("tctl.workloads.vllm.run", fake_run)
    from tctl.cli import main

    rc = main(["vllm", "info"])
    assert rc == 0
    assert calls


def test_unknown_workload_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    import tctl.cli as cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["notaworkload", "something"])
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# Hoister: 2-token positional profile rewrite
# ---------------------------------------------------------------------------


def test_positional_hoister_two_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """tctl vllm serve models/foo.yaml → tctl vllm serve --profile foo"""
    from tctl.cli import _hoist_positional_profile

    out = _hoist_positional_profile(["vllm", "serve", "models/foo.yaml"])
    assert out == ["vllm", "serve", "--profile", "foo"]


def test_hoist_vllm_serve_profile_yaml() -> None:
    from tctl.cli import _hoist_positional_profile

    result = _hoist_positional_profile(["vllm", "serve", "models/qwen3_5-9b.yaml"])
    assert result == ["vllm", "serve", "--profile", "qwen3_5-9b"]


def test_positional_hoister_not_profile_aware_unchanged() -> None:
    """tctl vllm rolling-restart models/foo.yaml unchanged (not in _PROFILE_AWARE)"""
    from tctl.cli import _hoist_positional_profile

    out = _hoist_positional_profile(["vllm", "rolling-restart", "models/foo.yaml"])
    assert out == ["vllm", "rolling-restart", "models/foo.yaml"]


def test_positional_hoister_haproxy_unchanged() -> None:
    """tctl haproxy start models/foo.yaml unchanged (haproxy not in _PROFILE_AWARE)"""
    from tctl.cli import _hoist_positional_profile

    out = _hoist_positional_profile(["haproxy", "start", "models/foo.yaml"])
    assert out == ["haproxy", "start", "models/foo.yaml"]


def test_hoist_only_vllm_workload() -> None:
    from tctl.cli import _hoist_positional_profile

    # haproxy has no profile-aware verbs; passthrough unchanged
    result = _hoist_positional_profile(["haproxy", "start", "models/foo.yaml"])
    assert result == ["haproxy", "start", "models/foo.yaml"]


def test_hoist_non_profile_aware_verb_unchanged() -> None:
    from tctl.cli import _hoist_positional_profile

    # rolling-restart is not in _PROFILE_AWARE; passthrough unchanged
    result = _hoist_positional_profile(["vllm", "rolling-restart", "models/foo.yaml"])
    assert result == ["vllm", "rolling-restart", "models/foo.yaml"]


# ---------------------------------------------------------------------------
# python -m tctl smoke
# ---------------------------------------------------------------------------


def test_python_m_tctl_help_works(capsys: pytest.CaptureFixture[str]) -> None:
    """python -m tctl --help exits 0"""
    res = subprocess.run(
        [sys.executable, "-m", "tctl", "--help"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    assert "vllm" in res.stdout
