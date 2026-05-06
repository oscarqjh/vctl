"""Unit tests for TmuxSession (Tasks 1-5) + integration tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_ok() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


# ---------------------------------------------------------------------------
# Task 1 — _validate_tmux_name + TmuxSession.__init__
# ---------------------------------------------------------------------------


def test_validate_tmux_name_valid() -> None:
    from vctl.tmux import _validate_tmux_name

    _validate_tmux_name("vctl-lb")  # no exception
    _validate_tmux_name("vctl.lb")  # dots allowed
    _validate_tmux_name("sess_1")  # underscores allowed


def test_validate_tmux_name_rejects_slash() -> None:
    from vctl.tmux import _validate_tmux_name

    with pytest.raises(ValueError, match="invalid tmux session name"):
        _validate_tmux_name("bad/name")


def test_validate_tmux_name_rejects_empty() -> None:
    from vctl.tmux import _validate_tmux_name

    with pytest.raises(ValueError, match="invalid tmux session name"):
        _validate_tmux_name("")


def test_validate_tmux_name_rejects_space() -> None:
    from vctl.tmux import _validate_tmux_name

    with pytest.raises(ValueError, match="invalid tmux session name"):
        _validate_tmux_name("bad name")


# AT-10: TmuxSession raises ValueError on invalid name at __init__ time
@pytest.mark.parametrize("bad_name", ["bad/name", "bad name", "", "has\ttab"])
def test_at10_invalid_name_raises_at_init(bad_name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run_calls: list[list[str]] = []
    monkeypatch.setattr(
        "vctl.tmux.subprocess.run",
        lambda a, **k: run_calls.append(a) or _fake_ok(),
    )
    from vctl.tmux import TmuxSession

    with pytest.raises(ValueError, match="invalid tmux session name"):
        TmuxSession(bad_name)
    assert not run_calls


# ---------------------------------------------------------------------------
# Task 2 — TmuxSession.start(): env flags, validation, double-start guard
# ---------------------------------------------------------------------------


def test_start_list_form_passes_env_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """List-form argv is shlex-joined; env dict becomes -e KEY=VAL flags."""
    calls: list[list[str]] = []
    monkeypatch.setattr("vctl.tmux._TMUX_VERSION_OK", True)
    monkeypatch.setattr(
        "vctl.tmux.subprocess.run",
        lambda argv, **kw: calls.append(list(argv)) or _fake_ok(),
    )
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    from vctl.tmux import TmuxSession

    sess = TmuxSession("test-sess", env={"FOO": "bar", "BAZ": "qux"})
    sess.start(["echo", "hello"])
    new_sess_call = calls[0]
    assert "-e" in new_sess_call
    assert "FOO=bar" in new_sess_call
    assert "BAZ=qux" in new_sess_call
    # shlex-joined list form: "echo hello"
    assert new_sess_call[-1] == "echo hello"


def test_start_str_form_passed_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """Str-form argv is forwarded verbatim as the final tmux argument."""
    calls: list[list[str]] = []
    monkeypatch.setattr("vctl.tmux._TMUX_VERSION_OK", True)
    monkeypatch.setattr(
        "vctl.tmux.subprocess.run",
        lambda argv, **kw: calls.append(list(argv)) or _fake_ok(),
    )
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    from vctl.tmux import TmuxSession

    sess = TmuxSession("test-sess", env={})
    sess.start("source /venv/bin/activate && bash run.sh")
    assert calls[0][-1] == "source /venv/bin/activate && bash run.sh"


def test_env_none_snapshots_at_start_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Headline behavior: env=None snapshots os.environ at start() call time,
    NOT at __init__ time.  This is what fixes the stale-tmux-server-cache footgun."""
    calls: list[list[str]] = []
    monkeypatch.setattr("vctl.tmux._TMUX_VERSION_OK", True)
    monkeypatch.setattr(
        "vctl.tmux.subprocess.run",
        lambda argv, **kw: calls.append(list(argv)) or _fake_ok(),
    )
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    monkeypatch.setenv("VCTL_TEST_KEY", "before_init")
    from vctl.tmux import TmuxSession

    sess = TmuxSession("test-sess")  # env=None — must NOT snapshot os.environ now
    monkeypatch.setenv("VCTL_TEST_KEY", "after_init_before_start")
    sess.start(["echo"])
    new_sess_call = calls[0]
    # Value at start() time must win, not value at __init__ time.
    assert "VCTL_TEST_KEY=after_init_before_start" in new_sess_call
    assert "VCTL_TEST_KEY=before_init" not in new_sess_call


def test_validate_env_rejects_key_with_equals(monkeypatch: pytest.MonkeyPatch) -> None:
    """_validate_env rejects env keys that contain '='."""
    monkeypatch.setattr("vctl.tmux._TMUX_VERSION_OK", True)
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    from vctl.tmux import TmuxSession

    sess = TmuxSession("test-sess", env={"K=BAD": "v"})
    with pytest.raises(ValueError, match="invalid env key"):
        sess.start(["echo"])


def test_validate_env_rejects_value_with_newline(monkeypatch: pytest.MonkeyPatch) -> None:
    """_validate_env rejects env values containing newline."""
    monkeypatch.setattr("vctl.tmux._TMUX_VERSION_OK", True)
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    from vctl.tmux import TmuxSession

    sess = TmuxSession("test-sess", env={"K": "value\nwith\nnewline"})
    with pytest.raises(ValueError, match="newline or NUL"):
        sess.start(["echo"])


# AT-7: start() raises RuntimeError when session already exists
def test_at7_start_raises_on_existing_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """AT-7: RuntimeError raised if session already exists; no new-session issued."""
    monkeypatch.setattr("vctl.tmux._TMUX_VERSION_OK", True)
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: True)
    run_calls: list[list[str]] = []
    monkeypatch.setattr(
        "vctl.tmux.subprocess.run",
        lambda a, **k: run_calls.append(list(a)) or _fake_ok(),
    )
    from vctl.tmux import TmuxSession

    with pytest.raises(RuntimeError, match="already exists"):
        TmuxSession("vctl-lb", env={}).start(["haproxy"])
    assert not run_calls


def test_start_raises_runtime_if_tmux_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """start() raises RuntimeError when tmux binary is not on PATH."""
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)

    def fake_run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError

    monkeypatch.setattr("vctl.tmux.subprocess.run", fake_run)
    # Reset version cache so the FileNotFoundError bubbles from _check_tmux_version
    monkeypatch.setattr("vctl.tmux._TMUX_VERSION_OK", None)
    from vctl.tmux import TmuxSession

    sess = TmuxSession("vctl-lb", env={})
    with pytest.raises(RuntimeError, match="tmux not installed"):
        sess.start(["haproxy"])


def test_check_tmux_version_rejects_old_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_check_tmux_version raises RuntimeError for tmux < 3.2."""
    import subprocess as _sp

    monkeypatch.setattr("vctl.tmux._TMUX_VERSION_OK", None)
    monkeypatch.setattr(
        "vctl.tmux.subprocess.run",
        lambda argv, **kw: _sp.CompletedProcess(
            args=argv, returncode=0, stdout="tmux 3.1\n", stderr=""
        ),
    )
    from vctl.tmux import _check_tmux_version

    with pytest.raises(RuntimeError, match="tmux 3.2\\+ required"):
        _check_tmux_version()


# AT-1: vllm PATH in env flags
def test_at1_vllm_path_in_env_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """AT-1: PATH set in env dict appears as -e PATH=... in tmux new-session call."""
    calls: list[list[str]] = []
    monkeypatch.setattr("vctl.tmux._TMUX_VERSION_OK", True)
    monkeypatch.setattr(
        "vctl.tmux.subprocess.run",
        lambda a, **k: calls.append(list(a)) or _fake_ok(),
    )
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    from vctl.tmux import TmuxSession

    sess = TmuxSession("vctl-vllm-qwen", env={"PATH": "/venv/bin:/usr/bin"})
    sess.start(["vllm", "serve", "model"])
    new_sess_call = calls[0]
    assert any(arg == "PATH=/venv/bin:/usr/bin" for arg in new_sess_call)


# AT-2: lb manager env propagated
def test_at2_lb_manager_env_propagated(monkeypatch: pytest.MonkeyPatch) -> None:
    """AT-2: CUSTOM_VAR in env dict appears in tmux new-session argv."""
    calls: list[list[str]] = []
    monkeypatch.setattr("vctl.tmux._TMUX_VERSION_OK", True)
    monkeypatch.setattr(
        "vctl.tmux.subprocess.run",
        lambda a, **k: calls.append(list(a)) or _fake_ok(),
    )
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    from vctl.tmux import TmuxSession

    sess = TmuxSession("vctl-lb", env={"CUSTOM_VAR": "sentinel"})
    sess.start(["haproxy", "-f", "/tmp/ha.cfg"])
    assert any(arg == "CUSTOM_VAR=sentinel" for arg in calls[0])


def test_validate_env_rejects_empty_key():
    from vctl.tmux import _validate_env

    with pytest.raises(ValueError, match="empty or contains '='"):
        _validate_env({"": "v"})


def test_validate_env_rejects_nul_value():
    from vctl.tmux import _validate_env

    with pytest.raises(ValueError, match="newline or NUL"):
        _validate_env({"K": "value\x00with\x00nul"})


def test_check_tmux_version_accepts_3_4(monkeypatch):
    """tmux 3.4 (deployed env) is accepted; cached flag set True."""
    import vctl.tmux as t

    t._TMUX_VERSION_OK = None
    monkeypatch.setattr(
        "vctl.tmux.subprocess.run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout="tmux 3.4\n", stderr=""),
    )
    t._check_tmux_version()
    assert t._TMUX_VERSION_OK is True
    # Calling again should be a no-op (cached) — stub now raises if called.
    monkeypatch.setattr(
        "vctl.tmux.subprocess.run",
        lambda argv, **kw: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    t._check_tmux_version()  # cached, no subprocess


# ---------------------------------------------------------------------------
# Task 3 — log_path + pipe-pane
# ---------------------------------------------------------------------------


# AT-9: log_path triggers pipe-pane call
def test_at9_log_path_emits_pipe_pane(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """AT-9: When log_path is given, a second subprocess.run with pipe-pane is issued."""
    calls: list[list[str]] = []
    monkeypatch.setattr("vctl.tmux._TMUX_VERSION_OK", True)
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    monkeypatch.setattr(
        "vctl.tmux.subprocess.run",
        lambda a, **k: calls.append(list(a)) or _fake_ok(),
    )
    log = tmp_path / "out.log"
    from vctl.tmux import TmuxSession

    TmuxSession("vctl-vllm-qwen", env={}, log_path=log).start(["vllm"])
    pipe_calls = [c for c in calls if "pipe-pane" in c]
    assert len(pipe_calls) == 1
    assert str(log) in " ".join(pipe_calls[0])


def test_no_log_path_skips_pipe_pane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without log_path, no pipe-pane subprocess is issued."""
    calls: list[list[str]] = []
    monkeypatch.setattr("vctl.tmux._TMUX_VERSION_OK", True)
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    monkeypatch.setattr(
        "vctl.tmux.subprocess.run",
        lambda a, **k: calls.append(list(a)) or _fake_ok(),
    )
    from vctl.tmux import TmuxSession

    TmuxSession("vctl-lb", env={}).start(["haproxy", "-f", "/tmp/h.cfg"])
    pipe_calls = [c for c in calls if "pipe-pane" in c]
    assert len(pipe_calls) == 0


# Integration test (requires real tmux 3.2+ on PATH)
@pytest.mark.integration
def test_log_path_captures_output(tmp_path: Path) -> None:
    """Integration: log_path receives stdout via pipe-pane."""
    import time

    from vctl.tmux import TmuxSession

    name = "vctl-test-log"
    log = tmp_path / "session.log"
    sess = TmuxSession(name, env={}, log_path=log)
    sess.start(["echo", "captured-output"])
    try:
        time.sleep(1)
        text = log.read_text() if log.exists() else ""
        assert "captured-output" in text
    finally:
        sess.kill(tree=False)


# ---------------------------------------------------------------------------
# Task 4 — exists() + pane_pid() + kill(tree=False)
# ---------------------------------------------------------------------------


def test_exists_delegates_to_tmux_session_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exists() is a thin wrapper around tmux_session_exists."""
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: True)
    from vctl.tmux import TmuxSession

    assert TmuxSession("vctl-lb").exists() is True
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    assert TmuxSession("vctl-lb").exists() is False


def test_pane_pid_parses_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """pane_pid() parses the integer from tmux list-panes -F '#{pane_pid}'."""
    import subprocess as _sp

    result = _sp.CompletedProcess(args=[], returncode=0, stdout="12345\n", stderr="")
    monkeypatch.setattr("vctl.tmux.subprocess.run", lambda *a, **kw: result)
    from vctl.tmux import TmuxSession

    assert TmuxSession("vctl-vllm-qwen", env={}).pane_pid() == 12345


def test_pane_pid_returns_none_if_session_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """pane_pid() returns None when tmux list-panes returns non-zero."""
    import subprocess as _sp

    result = _sp.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
    monkeypatch.setattr("vctl.tmux.subprocess.run", lambda *a, **kw: result)
    from vctl.tmux import TmuxSession

    assert TmuxSession("gone", env={}).pane_pid() is None


def test_pane_pid_returns_none_on_empty_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pane_pid() returns None when list-panes returns 0 but empty stdout."""
    import subprocess as _sp

    result = _sp.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    monkeypatch.setattr("vctl.tmux.subprocess.run", lambda *a, **kw: result)
    from vctl.tmux import TmuxSession

    assert TmuxSession("vctl-lb", env={}).pane_pid() is None


# AT-8: kill is idempotent when session already gone
def test_at8_kill_idempotent_when_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    """AT-8: kill() is a no-op and raises no error when session does not exist."""
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: False)
    run_calls: list[list[str]] = []
    monkeypatch.setattr(
        "vctl.tmux.subprocess.run",
        lambda a, **k: run_calls.append(list(a)) or _fake_ok(),
    )
    from vctl.tmux import TmuxSession

    TmuxSession("vctl-lb").kill()  # must not raise
    assert not run_calls


# AT-6: kill(tree=False) skips psutil, still calls kill-session
def test_at6_lb_stop_tree_false_no_psutil(monkeypatch: pytest.MonkeyPatch) -> None:
    """AT-6: kill(tree=False) skips psutil.Process; tmux kill-session IS called."""
    run_calls: list[list[str]] = []
    monkeypatch.setattr("vctl.tmux.tmux_session_exists", lambda _: True)
    monkeypatch.setattr(
        "vctl.tmux.subprocess.run",
        lambda a, **k: run_calls.append(list(a)) or _fake_ok(),
    )
    monkeypatch.setattr("vctl.tmux.TmuxSession.pane_pid", lambda self: 999)
    psutil_calls: list[int] = []
    monkeypatch.setattr(
        "vctl.tmux.psutil.Process",
        lambda pid: psutil_calls.append(pid),
    )
    from vctl.tmux import TmuxSession

    TmuxSession("vctl-lb").kill(tree=False)
    assert not psutil_calls
    assert any("kill-session" in " ".join(c) for c in run_calls)


# Integration: full roundtrip (requires real tmux)
@pytest.mark.integration
def test_session_start_exists_kill_roundtrip(tmp_path: Path) -> None:
    """Integration: start → exists → pane_pid → kill roundtrip."""
    import time

    from vctl.tmux import TmuxSession

    name = "vctl-test-integration"
    sess = TmuxSession(name, env={"VCTL_TMUX_TEST": "1"})
    sess.start(["sleep", "60"])
    try:
        assert sess.exists()
        pid = sess.pane_pid()
        assert pid is not None and pid > 0
        sess.kill(tree=True)
        time.sleep(0.5)
        assert not sess.exists()
    finally:
        sess.kill(tree=False)  # idempotent cleanup
