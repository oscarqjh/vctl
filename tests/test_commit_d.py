"""Regression tests for Commit D — env/coerce/resolver hardening (D1-D13)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from vctl.config.settings import _apply_env_overrides, _coerce_scalar
from vctl.config.yaml_source import load_yaml
from vctl.resolver import _deep_merge, _validate_profile_name

FIX = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# D1 — env override scalar→dict guard
# ---------------------------------------------------------------------------


def test_d1_scalar_to_dict_descent_raises() -> None:
    """VCTL_LB__HOST__FOO=1 must raise when lb.host is already a scalar."""
    base: dict[str, Any] = {"lb": {"host": "10.0.0.1"}}
    env = {"VCTL_LB__HOST__FOO": "1"}
    with pytest.raises(ValueError, match="non-mapping"):
        _apply_env_overrides(base, environ=env)


def test_d1_no_error_when_parent_missing() -> None:
    """If the intermediate key does not exist, create the dict (no error)."""
    base: dict[str, Any] = {"lb": {}}
    env = {"VCTL_LB__NEWKEY__FOO": "1"}
    result = _apply_env_overrides(base, environ=env)
    assert result["lb"]["newkey"]["foo"] == 1


def test_d1_no_error_when_parent_is_dict() -> None:
    """If the intermediate key is already a dict, recurse normally."""
    base: dict[str, Any] = {"lb": {"client": {"bind_port": 8080}}}
    env = {"VCTL_LB__CLIENT__BIND_PORT": "9090"}
    result = _apply_env_overrides(base, environ=env)
    assert result["lb"]["client"]["bind_port"] == 9090


# ---------------------------------------------------------------------------
# D2 — empty key segments
# ---------------------------------------------------------------------------


def test_d2_trailing_double_underscore_raises() -> None:
    """VCTL_LB__=8080 → empty segment after split."""
    env = {"VCTL_LB__": "8080"}
    with pytest.raises(ValueError, match="empty key segment"):
        _apply_env_overrides({}, environ=env)


def test_d2_leading_double_underscore_raises() -> None:
    """VCTL___HOST=x → empty segment before HOST."""
    env = {"VCTL___HOST": "x"}
    with pytest.raises(ValueError, match="empty key segment"):
        _apply_env_overrides({}, environ=env)


def test_d2_triple_underscore_in_middle_raises() -> None:
    """VCTL_LB____HOST=x → empty segment between LB and HOST."""
    env = {"VCTL_LB____HOST": "x"}
    with pytest.raises(ValueError, match="empty key segment"):
        _apply_env_overrides({}, environ=env)


# ---------------------------------------------------------------------------
# D3 — _coerce_scalar strict int/float
# ---------------------------------------------------------------------------


def test_d3_scientific_notation_stays_string() -> None:
    assert _coerce_scalar("1e3") == "1e3"
    assert isinstance(_coerce_scalar("1e3"), str)


def test_d3_nan_stays_string() -> None:
    assert _coerce_scalar("nan") == "nan"
    assert isinstance(_coerce_scalar("nan"), str)


def test_d3_inf_stays_string() -> None:
    assert _coerce_scalar("inf") == "inf"
    assert isinstance(_coerce_scalar("inf"), str)


def test_d3_hex_stays_string() -> None:
    assert _coerce_scalar("0x10") == "0x10"
    assert isinstance(_coerce_scalar("0x10"), str)


def test_d3_plain_int_coerced() -> None:
    assert _coerce_scalar("123") == 123
    assert isinstance(_coerce_scalar("123"), int)


def test_d3_negative_int_coerced() -> None:
    assert _coerce_scalar("-5") == -5
    assert isinstance(_coerce_scalar("-5"), int)


def test_d3_plain_float_coerced() -> None:
    assert _coerce_scalar("1.5") == 1.5
    assert isinstance(_coerce_scalar("1.5"), float)


def test_d3_negative_float_coerced() -> None:
    assert _coerce_scalar("-3.14") == -3.14
    assert isinstance(_coerce_scalar("-3.14"), float)


def test_d3_bool_true_coerced() -> None:
    assert _coerce_scalar("true") is True
    assert _coerce_scalar("True") is True


def test_d3_bool_false_coerced() -> None:
    assert _coerce_scalar("false") is False


# ---------------------------------------------------------------------------
# D4 — _deep_merge None as delete
# ---------------------------------------------------------------------------


def test_d4_none_deletes_key() -> None:
    result = _deep_merge({"a": "x"}, {"a": None})
    assert result == {}


def test_d4_none_deletes_key_leaving_others() -> None:
    result = _deep_merge({"a": "x", "b": "y"}, {"a": None})
    assert result == {"b": "y"}


def test_d4_none_in_b_missing_from_a_is_noop() -> None:
    """None in b for a key not in a should not add the key."""
    result = _deep_merge({"b": "y"}, {"a": None})
    assert result == {"b": "y"}


def test_d4_none_does_not_recurse_into_nested() -> None:
    result = _deep_merge({"nest": {"x": 1, "y": 2}}, {"nest": None})
    assert result == {}


def test_d4_normal_merge_still_works() -> None:
    result = _deep_merge({"a": 1}, {"b": 2})
    assert result == {"a": 1, "b": 2}


# ---------------------------------------------------------------------------
# D5 — detect_self_ip fallback chain
# ---------------------------------------------------------------------------


def test_d5_fallback_to_gethostbyname_on_connect_error() -> None:
    """OSError on UDP connect → fall back to gethostbyname."""
    from vctl.platform import detect_self_ip

    with patch("socket.socket") as mock_sock_cls:
        mock_sock = MagicMock()
        mock_sock_cls.return_value = mock_sock
        mock_sock.__enter__ = lambda s: s
        mock_sock.__exit__ = MagicMock(return_value=False)
        mock_sock.connect.side_effect = OSError("network unreachable")

        result = detect_self_ip()

    assert isinstance(result, str)
    assert len(result) > 0


def test_d5_fallback_to_loopback_when_all_fail() -> None:
    """Both UDP probe and gethostbyname fail → must return 127.0.0.1."""
    from vctl.platform import detect_self_ip

    with (
        patch("socket.socket") as mock_sock_cls,
        patch("socket.gethostbyname", side_effect=OSError("no resolver")),
    ):
        mock_sock = MagicMock()
        mock_sock_cls.return_value = mock_sock
        mock_sock.__enter__ = lambda s: s
        mock_sock.__exit__ = MagicMock(return_value=False)
        mock_sock.connect.side_effect = OSError("network unreachable")

        result = detect_self_ip()

    assert result == "127.0.0.1"


def test_d5_fallback_never_raises() -> None:
    """detect_self_ip must never raise regardless of network state."""
    from vctl.platform import detect_self_ip

    with (
        patch("socket.socket") as mock_sock_cls,
        patch("socket.gethostbyname", side_effect=OSError("fail")),
    ):
        mock_sock = MagicMock()
        mock_sock_cls.return_value = mock_sock
        mock_sock.__enter__ = lambda s: s
        mock_sock.__exit__ = MagicMock(return_value=False)
        mock_sock.connect.side_effect = OSError("fail")

        result = detect_self_ip()

    assert result == "127.0.0.1"


# ---------------------------------------------------------------------------
# D6 — pydantic Field range constraints
# ---------------------------------------------------------------------------


def test_d6_bind_port_zero_rejected() -> None:
    from pydantic import ValidationError

    from vctl.config.models import Pool

    with pytest.raises(ValidationError):
        Pool(name="x", served_model="y", bind_port=0)


def test_d6_bind_port_too_large_rejected() -> None:
    from pydantic import ValidationError

    from vctl.config.models import Pool

    with pytest.raises(ValidationError):
        Pool(name="x", served_model="y", bind_port=70000)


def test_d6_bind_port_valid_boundaries() -> None:
    from vctl.config.models import Pool

    Pool(name="x", served_model="y", bind_port=1)
    Pool(name="x", served_model="y", bind_port=65535)


def test_d6_num_gpus_negative_rejected() -> None:
    from pydantic import ValidationError

    from vctl.config.models import Resources

    with pytest.raises(ValidationError):
        Resources(num_gpus=-1, cuda_visible_devices="0")


def test_d6_num_gpus_zero_allowed() -> None:
    from vctl.config.models import Resources

    r = Resources(num_gpus=0, cuda_visible_devices="")
    assert r.num_gpus == 0


def test_d6_data_parallel_zero_rejected() -> None:
    from pydantic import ValidationError

    from vctl.config.models import Parallelism

    with pytest.raises(ValidationError):
        Parallelism(data_parallel=0, tensor_parallel=1, api_server_count=1)


def test_d6_tensor_parallel_zero_rejected() -> None:
    from pydantic import ValidationError

    from vctl.config.models import Parallelism

    with pytest.raises(ValidationError):
        Parallelism(data_parallel=1, tensor_parallel=0, api_server_count=1)


def test_d6_api_server_count_zero_rejected() -> None:
    from pydantic import ValidationError

    from vctl.config.models import Parallelism

    with pytest.raises(ValidationError):
        Parallelism(data_parallel=1, tensor_parallel=1, api_server_count=0)


def test_d6_lb_health_fall_zero_rejected() -> None:
    from pydantic import ValidationError

    from vctl.config.models import LbHealth

    with pytest.raises(ValidationError):
        LbHealth(fall=0)


def test_d6_lb_health_rise_zero_rejected() -> None:
    from pydantic import ValidationError

    from vctl.config.models import LbHealth

    with pytest.raises(ValidationError):
        LbHealth(rise=0)


def test_d6_health_path_must_start_with_slash() -> None:
    from pydantic import ValidationError

    from vctl.config.models import LbHealth

    with pytest.raises(ValidationError, match="'/'"):
        LbHealth(path="health")


def test_d6_health_path_with_slash_ok() -> None:
    from vctl.config.models import LbHealth

    h = LbHealth(path="/health")
    assert h.path == "/health"


# ---------------------------------------------------------------------------
# D7 — ~ expansion in ClusterSection
# ---------------------------------------------------------------------------


def test_d7_tilde_expansion_in_venv() -> None:
    from vctl.config.models import ClusterSection

    cs = ClusterSection(venv="~/.venv", state_dir="/tmp/state")
    assert not cs.venv.startswith("~")
    assert cs.venv.startswith("/")


def test_d7_tilde_expansion_in_state_dir() -> None:
    from vctl.config.models import ClusterSection

    cs = ClusterSection(venv="/opt/venv", state_dir="~/state")
    assert not cs.state_dir.startswith("~")
    assert cs.state_dir.startswith("/")


def test_d7_absolute_path_unchanged() -> None:
    from vctl.config.models import ClusterSection

    cs = ClusterSection(venv="/opt/venv", state_dir="/var/lib/vctl")
    assert cs.venv == "/opt/venv"
    assert cs.state_dir == "/var/lib/vctl"


def test_d7_empty_venv_rejected() -> None:
    from pydantic import ValidationError

    from vctl.config.models import ClusterSection

    with pytest.raises(ValidationError):
        ClusterSection(venv="", state_dir="/tmp/state")


def test_d7_empty_state_dir_rejected() -> None:
    from pydantic import ValidationError

    from vctl.config.models import ClusterSection

    with pytest.raises(ValidationError):
        ClusterSection(venv="/opt/venv", state_dir="")


# ---------------------------------------------------------------------------
# D8 — --profile path traversal
# ---------------------------------------------------------------------------


def test_d8_path_traversal_rejected() -> None:
    with pytest.raises(ValueError, match="invalid profile name"):
        _validate_profile_name("../etc/passwd")


def test_d8_slash_rejected() -> None:
    with pytest.raises(ValueError, match="invalid profile name"):
        _validate_profile_name("foo/bar")


def test_d8_leading_dot_rejected() -> None:
    with pytest.raises(ValueError, match="invalid profile name"):
        _validate_profile_name(".hidden")


def test_d8_leading_double_dot_rejected() -> None:
    with pytest.raises(ValueError, match="invalid profile name"):
        _validate_profile_name("..foo")


def test_d8_valid_name_with_dot_in_middle() -> None:
    _validate_profile_name("qwen3.5-9b")  # should not raise


def test_d8_valid_alphanumeric() -> None:
    _validate_profile_name("my-profile_v2")  # should not raise


def test_d8_valid_starts_with_digit() -> None:
    _validate_profile_name("9b-model")  # should not raise


# ---------------------------------------------------------------------------
# D9 — host/served_model non-empty
# ---------------------------------------------------------------------------


def test_d9_lbhaproxy_empty_host_rejected() -> None:
    from pydantic import ValidationError

    from vctl.config.models import LbAdmin, LbHaproxy, LbStats

    with pytest.raises(ValidationError):
        LbHaproxy(
            host="",
            admin=LbAdmin(bind_port=9001),
            stats=LbStats(bind_port=9000),
            client=None,
            pools=[],
        )


def test_d9_pool_empty_served_model_rejected() -> None:
    from pydantic import ValidationError

    from vctl.config.models import Pool

    with pytest.raises(ValidationError):
        Pool(name="x", served_model="", bind_port=8080)


def test_d9_wildcard_served_model_allowed() -> None:
    from vctl.config.models import Pool

    p = Pool(name="default", served_model="*", bind_port=8080)
    assert p.served_model == "*"


# ---------------------------------------------------------------------------
# D10 — _kill_tree NoSuchProcess on children()
# ---------------------------------------------------------------------------


def test_d10_kill_tree_tolerates_no_such_process_on_children() -> None:
    import psutil

    from vctl.commands.serve import _kill_tree

    mock_root = MagicMock(spec=psutil.Process)
    mock_root.children.side_effect = psutil.NoSuchProcess(pid=9999)

    with patch("psutil.Process", return_value=mock_root):
        # Must not raise
        _kill_tree(9999, grace=0.0)


def test_d10_kill_tree_pid_gone_before_process_call() -> None:
    import psutil

    from vctl.commands.serve import _kill_tree

    with patch("psutil.Process", side_effect=psutil.NoSuchProcess(pid=9999)):
        # Already handled in original code — must still not raise
        _kill_tree(9999, grace=0.0)


# ---------------------------------------------------------------------------
# D11 — Popen start_new_session=True
# ---------------------------------------------------------------------------


def test_d11_popen_has_start_new_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """serve.run() must pass start_new_session=True to Popen."""
    import argparse
    from unittest.mock import patch as _patch

    # Build a minimal repo
    (tmp_path / "cluster.yaml").write_text((FIX / "sample_cluster.yaml").read_text())
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "qwen3-9b.yaml").write_text((FIX / "sample_profile.yaml").read_text())

    captured_kwargs: dict[str, Any] = {}

    def fake_popen(cmd: list[str], **kwargs: Any) -> MagicMock:
        captured_kwargs.update(kwargs)
        m = MagicMock()
        m.pid = 99999
        # Raise immediately in wait() so run() doesn't block
        m.wait.side_effect = Exception("stop")
        return m

    monkeypatch.setenv("VCTL_TEST_NO_SOCKET", "1")
    ns = argparse.Namespace(
        config=str(tmp_path / "cluster.yaml"),
        profile=None,
        log_level="WARNING",
        log_format="text",
    )

    from vctl.commands import serve as serve_mod

    with (
        _patch.object(serve_mod, "subprocess") as mock_subprocess,
        _patch("vctl.commands.serve.pool_for_model") as mock_pool,
        _patch("vctl.commands.serve.LbManager"),
        _patch("vctl.commands.serve.BackendState"),
        _patch("vctl.commands.serve.detect_self_ip", return_value="127.0.0.1"),
        _patch("vctl.commands.serve._wait_for_ready"),
        _patch("vctl.commands.serve.lb_scaling._do_add"),
        _patch("vctl.commands.serve.lb_scaling._do_drain"),
        _patch("vctl.commands.serve.lb_scaling._do_remove"),
    ):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.wait.return_value = 0
        mock_subprocess.Popen.return_value = mock_proc
        mock_pool.return_value = MagicMock(name="default")

        serve_mod.run(ns, ["--skip-preflight"])

    popen_kwargs = mock_subprocess.Popen.call_args
    assert popen_kwargs is not None, "Popen was not called"
    kwargs = popen_kwargs.kwargs if popen_kwargs.kwargs else popen_kwargs[1]
    assert kwargs.get("start_new_session") is True, (
        f"start_new_session not True in Popen kwargs: {kwargs}"
    )


# ---------------------------------------------------------------------------
# D12 — env value bool serialization (lowercase)
# ---------------------------------------------------------------------------


def test_d12_bool_env_serialized_lowercase(tmp_path: Path) -> None:
    """Booleans in rc.env must become 'true'/'false', not 'True'/'False'."""
    # Build a minimal resolve scenario that exercises the env-copy loop
    import argparse
    from unittest.mock import patch as _patch

    (tmp_path / "cluster.yaml").write_text((FIX / "sample_cluster.yaml").read_text())
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "qwen3-9b.yaml").write_text(
        "apiVersion: vctl/v1\n"
        "kind: Profile\n"
        "model: { name: Qwen/Qwen3.5-9B, served_as: qwen3-9b }\n"
        "resources: { num_gpus: 8, cuda_visible_devices: '0,1,2,3,4,5,6,7' }\n"
        "parallelism: { data_parallel: 8, tensor_parallel: 1, api_server_count: 8 }\n"
        "server: { http_port: 8000 }\n"
        "vllm_args: {}\n"
        "env: { MY_BOOL: true, MY_NUM: 5, MY_STR: hello }\n"
    )

    captured_env: dict[str, str] = {}

    from vctl.commands import serve as serve_mod

    def fake_popen(cmd: list[str], env: dict[str, str] | None = None, **kw: Any) -> MagicMock:
        if env:
            captured_env.update(env)
        m = MagicMock()
        m.pid = 12345
        m.wait.return_value = 0
        return m

    ns = argparse.Namespace(
        config=str(tmp_path / "cluster.yaml"),
        profile=None,
        log_level="WARNING",
        log_format="text",
    )

    with (
        _patch("vctl.commands.serve.subprocess.Popen", side_effect=fake_popen),
        _patch("vctl.commands.serve.pool_for_model") as mock_pool,
        _patch("vctl.commands.serve.LbManager"),
        _patch("vctl.commands.serve.BackendState"),
        _patch("vctl.commands.serve.detect_self_ip", return_value="127.0.0.1"),
        _patch("vctl.commands.serve._wait_for_ready"),
        _patch("vctl.commands.serve.lb_scaling._do_add"),
        _patch("vctl.commands.serve.lb_scaling._do_drain"),
        _patch("vctl.commands.serve.lb_scaling._do_remove"),
    ):
        mock_pool.return_value = MagicMock(name="default")
        serve_mod.run(ns, ["--skip-preflight"])

    # After run(), captured_env should have lowercase boolean
    assert captured_env.get("MY_BOOL") == "true", (
        f"expected 'true' got {captured_env.get('MY_BOOL')!r}"
    )
    assert captured_env.get("MY_NUM") == "5"
    assert captured_env.get("MY_STR") == "hello"


# ---------------------------------------------------------------------------
# D13 — yaml duplicate-key rejection
# ---------------------------------------------------------------------------


def test_d13_duplicate_key_raises_constructor_error(tmp_path: Path) -> None:
    """YAML with duplicate mapping keys must raise ConstructorError."""
    yaml_text = "host: 1.1.1.1\nhost: 2.2.2.2\n"
    f = tmp_path / "dup.yaml"
    f.write_text(yaml_text)
    with pytest.raises(yaml.constructor.ConstructorError, match="duplicate key"):
        load_yaml(f)


def test_d13_no_error_on_unique_keys(tmp_path: Path) -> None:
    """Normal YAML (no duplicates) loads without error."""
    yaml_text = "host: 1.1.1.1\nport: 80\n"
    f = tmp_path / "ok.yaml"
    f.write_text(yaml_text)
    result = load_yaml(f)
    assert result["host"] == "1.1.1.1"
    assert result["port"] == 80


def test_d13_nested_duplicate_raises(tmp_path: Path) -> None:
    """Duplicate key in nested mapping also raises."""
    yaml_text = "lb:\n  host: a\n  host: b\n"
    f = tmp_path / "nested_dup.yaml"
    f.write_text(yaml_text)
    with pytest.raises(yaml.constructor.ConstructorError, match="duplicate key"):
        load_yaml(f)
