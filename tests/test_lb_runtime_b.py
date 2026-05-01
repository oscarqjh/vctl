"""Commit-B tests for RuntimeClient: B2, B6, B7, B8."""

from __future__ import annotations

import socket
import threading
import time

import pytest

from vctl.lb.runtime import RuntimeClient, _parse_endpoint_from_name

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serve_replies(sock: socket.socket, scripted: list[bytes]) -> None:
    """Serve one reply per request, then close."""
    for resp in scripted:
        sock.recv(4096)
        sock.sendall(resp)
    sock.close()


def _make_pair_client(replies: list[bytes]) -> RuntimeClient:
    """Return a RuntimeClient backed by a socketpair with scripted replies."""
    a, b = socket.socketpair()
    threading.Thread(target=_serve_replies, args=(b, replies), daemon=True).start()
    return RuntimeClient.for_unix_fd(a)


# ---------------------------------------------------------------------------
# B2: recv loop waits for \n\n terminator across multiple chunks
# ---------------------------------------------------------------------------


def test_send_reassembles_chunked_response() -> None:
    """B2: _send must wait until buffer ends with \\n\\n even across chunks."""
    a, b = socket.socketpair()

    def chunky_server(sock: socket.socket) -> None:
        sock.recv(4096)
        # Send response in two pieces with small delay.
        sock.sendall(b"hello ")
        time.sleep(0.05)
        sock.sendall(b"world\n\n")
        sock.close()

    threading.Thread(target=chunky_server, args=(b,), daemon=True).start()
    rc = RuntimeClient.for_unix_fd(a)
    resp = rc._send("any cmd")
    assert resp == "hello world\n\n"


def test_send_timeout_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """B2: socket.timeout/TimeoutError during recv must raise RuntimeError."""
    a, b = socket.socketpair()

    # Make the server never respond (just sit idle).
    # Set a very short timeout on the client side.
    a.settimeout(0.1)
    rc = RuntimeClient.for_unix_fd(a)

    # Drain the send buffer by having the server recv but not reply.
    def silent_server(sock: socket.socket) -> None:
        sock.recv(4096)
        time.sleep(5)  # never responds

    threading.Thread(target=silent_server, args=(b,), daemon=True).start()

    with pytest.raises(RuntimeError, match="timeout"):
        rc._send("any cmd")

    b.close()


# ---------------------------------------------------------------------------
# B6: add_server raises on error tokens
# ---------------------------------------------------------------------------


def test_add_server_new_token() -> None:
    """B6: 'New server registered.' response → return 'new'."""
    rc = _make_pair_client([b"New server registered.\n\n"])
    assert rc.add_server("be", "srv", "1.2.3.4:8000") == "new"


def test_add_server_already_present_token() -> None:
    """B6: 'already exists' response → return 'already_present'."""
    rc = _make_pair_client([b"Server be/srv already exists.\n\n"])
    assert rc.add_server("be", "srv", "1.2.3.4:8000") == "already_present"


def test_add_server_error_raises() -> None:
    """B6: non-success response (e.g. 'No such backend') must raise RuntimeError."""
    rc = _make_pair_client([b"No such backend.\n\n"])
    with pytest.raises(RuntimeError, match="haproxy add_server failed"):
        rc.add_server("be", "srv", "1.2.3.4:8000")


def test_add_server_invalid_server_address_raises() -> None:
    """B6: 'Invalid server address' must raise RuntimeError."""
    rc = _make_pair_client([b"Invalid server address.\n\n"])
    with pytest.raises(RuntimeError, match="haproxy add_server failed"):
        rc.add_server("be", "srv", "bad-address")


# ---------------------------------------------------------------------------
# B7: remove_server raises on error tokens
# ---------------------------------------------------------------------------


def test_remove_server_success_empty() -> None:
    """B7: empty response after del server → no error."""
    rc = _make_pair_client([b"\n\n"])
    rc.remove_server("be", "srv")  # must not raise


def test_remove_server_no_such_server_tolerated() -> None:
    """B7: 'No such server' → idempotent, no raise."""
    rc = _make_pair_client([b"No such server.\n\n"])
    rc.remove_server("be", "srv")  # must not raise


def test_remove_server_operation_not_permitted_raises() -> None:
    """B7: 'Operation not permitted' → must raise RuntimeError."""
    rc = _make_pair_client([b"Operation not permitted.\n\n"])
    with pytest.raises(RuntimeError, match="haproxy remove_server failed"):
        rc.remove_server("be", "srv")


def test_remove_server_unknown_error_raises() -> None:
    """B7: any unknown non-empty non-success token must raise."""
    rc = _make_pair_client([b"Something went wrong.\n\n"])
    with pytest.raises(RuntimeError):
        rc.remove_server("be", "srv")


# ---------------------------------------------------------------------------
# B8: show_servers_state prefers name-encoded IP; skips 0.0.0.0 garbage
# ---------------------------------------------------------------------------


def test_show_servers_state_0_0_0_0_with_parseable_name() -> None:
    """B8: srv_addr=0.0.0.0 but name parses → use IP from name."""
    a, b = socket.socketpair()
    payload = (
        b"# be_id be_name srv_id srv_name srv_addr srv_op_state\n"
        b"1 pool 1 b_10_1_2_5_8000 0.0.0.0 2\n\n"
    )
    threading.Thread(target=_serve_replies, args=(b, [payload]), daemon=True).start()
    rc = RuntimeClient.for_unix_fd(a)
    rows = rc.show_servers_state()
    assert len(rows) == 1
    assert rows[0].endpoint == "10.1.2.5:8000"


def test_show_servers_state_skips_0_0_0_0_unparseable_name() -> None:
    """B8: srv_addr=0.0.0.0 and name doesn't parse → row skipped."""
    a, b = socket.socketpair()
    payload = (
        b"# be_id be_name srv_id srv_name srv_addr srv_op_state\n"
        b"1 pool 1 bad_name 0.0.0.0 2\n\n"
    )
    threading.Thread(target=_serve_replies, args=(b, [payload]), daemon=True).start()
    rc = RuntimeClient.for_unix_fd(a)
    rows = rc.show_servers_state()
    assert rows == [], f"Garbage row with 0.0.0.0 must be skipped; got {rows}"


def test_parse_endpoint_from_name_valid() -> None:
    """B8: parse b_<ip_underscores>_<port> correctly."""
    assert _parse_endpoint_from_name("b_10_1_2_5_8000") == ("10.1.2.5", "8000")
    assert _parse_endpoint_from_name("b_192_168_0_1_9000") == ("192.168.0.1", "9000")


def test_parse_endpoint_from_name_invalid() -> None:
    """B8: non-matching names return None."""
    assert _parse_endpoint_from_name("bad_name") is None
    assert _parse_endpoint_from_name("b_10_0_0") is None  # only 3 octets + no port
    assert _parse_endpoint_from_name("b_10_0_0_1") is None  # no port segment
