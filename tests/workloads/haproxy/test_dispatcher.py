"""tests/workloads/haproxy/test_dispatcher.py — haproxy workload dispatcher tests (Task 5)."""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Import-level canary tests
# ---------------------------------------------------------------------------


def test_haproxy_commands_module_importable() -> None:
    """commands.py is importable and exposes the expected public symbols."""
    from tctl.workloads.haproxy import commands as _cmds

    assert hasattr(_cmds, "register_all"), "register_all must exist"
    assert hasattr(_cmds, "_cmd_start"), "_cmd_start must exist"
    assert hasattr(_cmds, "_cmd_stop"), "_cmd_stop must exist"
    assert hasattr(_cmds, "_cmd_status"), "_cmd_status must exist"
    assert hasattr(_cmds, "_cmd_reload"), "_cmd_reload must exist"
    assert hasattr(_cmds, "_cmd_logs"), "_cmd_logs must exist"
    assert hasattr(_cmds, "_cmd_config"), "_cmd_config must exist"
    assert hasattr(_cmds, "_cmd_health"), "_cmd_health must exist"
    assert hasattr(_cmds, "_cmd_add"), "_cmd_add must exist"
    assert hasattr(_cmds, "_cmd_remove"), "_cmd_remove must exist"
    assert hasattr(_cmds, "_cmd_drain"), "_cmd_drain must exist"
    assert hasattr(_cmds, "_cmd_scaling"), "_cmd_scaling must exist"
    assert hasattr(_cmds, "_cmd_prune"), "_cmd_prune must exist"
    assert hasattr(_cmds, "_fetch_haproxy_stats"), "_fetch_haproxy_stats must be kept"
    assert hasattr(_cmds, "run"), "run must exist"


def test_haproxy_init_importable_and_has_run() -> None:
    """tctl.workloads.haproxy is importable and exports run()."""
    import tctl.workloads.haproxy as _pkg

    assert callable(_pkg.run), "haproxy workload must export run()"


def test_haproxy_workload_run_is_callable() -> None:
    """tctl.workloads.haproxy.run can be imported and is callable."""
    from tctl.workloads.haproxy import run

    assert callable(run)


# ---------------------------------------------------------------------------
# AT-3: _build_subparser() lists all expected sub-verbs
# ---------------------------------------------------------------------------

_EXPECTED_VERBS = [
    "start",
    "stop",
    "status",
    "reload",
    "logs",
    "config",
    "health",
    "add",
    "remove",
    "drain",
    "scaling",
    "prune",
]


def test_at3_haproxy_subparser_lists_all_verbs() -> None:
    """AT-3: _build_subparser() help text contains every required sub-verb."""
    from tctl.workloads.haproxy.commands import _build_subparser

    parser = _build_subparser()
    help_text = parser.format_help()
    for verb in _EXPECTED_VERBS:
        assert verb in help_text, f"missing sub-verb {verb!r} in tctl haproxy --help"


def test_at3_scaling_subparser_lists_scaling_verbs() -> None:
    """The scaling sub-verb parser lists attach / detach / auto-add."""
    from tctl.workloads.haproxy.commands import _build_subparser

    parser = _build_subparser()
    # Parse `scaling --help` — argparse writes to stdout/stderr and raises SystemExit(0)
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            parser.parse_args(["scaling", "--help"])
    except SystemExit:
        pass
    help_text = buf.getvalue()
    for sv in ("attach", "detach", "auto-add"):
        assert sv in help_text, f"missing scaling sub-verb {sv!r}"


def test_register_all_is_callable() -> None:
    """register_all is a callable that accepts an argparse._SubParsersAction."""
    import argparse

    from tctl.workloads.haproxy.commands import register_all

    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="verb")
    # Must not raise:
    register_all(sub)
    help_text = p.format_help()
    for verb in _EXPECTED_VERBS:
        assert verb in help_text, f"register_all did not register {verb!r}"


# ---------------------------------------------------------------------------
# _fetch_haproxy_stats is still exported (prune.py depends on it)
# ---------------------------------------------------------------------------


def test_fetch_haproxy_stats_still_exported() -> None:
    """_fetch_haproxy_stats is accessible for prune.py monkeypatching."""
    from tctl.workloads.haproxy.commands import _fetch_haproxy_stats

    # Called with a non-RuntimeClient → returns empty dict (not raise)
    result = _fetch_haproxy_stats(object())
    assert result == {}


def test_prune_can_import_fetch_haproxy_stats() -> None:
    """prune.py imports _fetch_haproxy_stats from commands (module-level import still works)."""
    import tctl.workloads.haproxy.prune as prune_mod

    # The attribute must be present at module level for monkeypatching to work
    assert hasattr(prune_mod, "_fetch_haproxy_stats")


# ---------------------------------------------------------------------------
# Prog strings contain "tctl haproxy" (not "vctl lb")
# ---------------------------------------------------------------------------


def test_prog_string_is_tctl_haproxy() -> None:
    """The top-level parser prog= is 'tctl haproxy', not 'vctl lb'."""
    from tctl.workloads.haproxy.commands import _build_subparser

    parser = _build_subparser()
    assert "tctl haproxy" in parser.prog
    assert "vctl" not in parser.prog
