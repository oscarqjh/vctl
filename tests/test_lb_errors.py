"""Tests for vctl.lb.errors exception hierarchy."""

from __future__ import annotations

import pytest

from vctl.lb.errors import BackendOpFailed, LbUnreachable, PoolNotFound, ReconcilerError


def test_reconciler_error_is_exception() -> None:
    err = ReconcilerError("base error")
    assert isinstance(err, Exception)
    assert str(err) == "base error"


def test_lb_unreachable_is_reconciler_error() -> None:
    err = LbUnreachable(sock="/run/haproxy.sock", tcp="10.0.0.1:9999")
    assert isinstance(err, ReconcilerError)
    msg = str(err)
    assert "/run/haproxy.sock" in msg
    assert "10.0.0.1:9999" in msg


def test_pool_not_found_is_reconciler_error() -> None:
    err = PoolNotFound(requested="missing", available=["default", "gpu"])
    assert isinstance(err, ReconcilerError)
    msg = str(err)
    assert "missing" in msg
    assert "default" in msg
    assert "gpu" in msg


def test_backend_op_failed_is_reconciler_error() -> None:
    cause = RuntimeError("haproxy returned garbage")
    err = BackendOpFailed(op="add_server", ep="10.0.0.5:8000", backend="pool_default")
    err.__cause__ = cause
    assert isinstance(err, ReconcilerError)
    msg = str(err)
    assert "add_server" in msg
    assert "10.0.0.5:8000" in msg
    assert "pool_default" in msg


def test_catching_base_class_catches_all_subclasses() -> None:
    errors: list[ReconcilerError] = [
        LbUnreachable(sock="/s", tcp="h:1"),
        PoolNotFound(requested="x", available=[]),
        BackendOpFailed(op="set_state", ep="1.2.3.4:8000", backend="pool_a"),
    ]
    for err in errors:
        with pytest.raises(ReconcilerError):
            raise err


def test_lb_unreachable_str_contains_both_paths() -> None:
    err = LbUnreachable(sock="/var/run/haproxy.sock", tcp="192.168.1.1:9999")
    assert "sock=/var/run/haproxy.sock" in str(err)
    assert "tcp=192.168.1.1:9999" in str(err)


def test_pool_not_found_empty_available_list() -> None:
    err = PoolNotFound(requested="nope", available=[])
    assert "nope" in str(err)
    assert isinstance(err, PoolNotFound)
