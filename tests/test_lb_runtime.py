"""HAProxy admin-socket client tests using socketpair."""

from __future__ import annotations

import socket
import threading

from vctl.lb.runtime import RuntimeClient


def _serve_replies(sock: socket.socket, scripted: list[bytes]) -> None:
    """Serve one scripted reply per request, then close the socket."""
    for resp in scripted:
        sock.recv(4096)
        sock.sendall(resp)
    sock.close()


def test_add_server_new_then_already_present() -> None:
    a, b = socket.socketpair()
    threading.Thread(
        target=_serve_replies,
        args=(
            b,
            [
                b"New server registered.\n\n",
                b"Server pool/x already exists.\n\n",
            ],
        ),
        daemon=True,
    ).start()
    rc = RuntimeClient.for_unix_fd(a)
    assert rc.add_server("pool", "x", "10.0.0.5:8000") == "new"
    assert rc.add_server("pool", "x", "10.0.0.5:8000") == "already_present"


def test_show_servers_state_parses_endpoint_from_name() -> None:
    a, b = socket.socketpair()
    payload = (
        b"# be_id be_name srv_id srv_name srv_addr srv_op_state\n"
        b"1 pool 1 b_10_0_0_5_8000 10.0.0.5 2\n\n"
    )
    threading.Thread(target=_serve_replies, args=(b, [payload]), daemon=True).start()
    rc = RuntimeClient.for_unix_fd(a)
    rows = rc.show_servers_state()
    assert rows[0].endpoint == "10.0.0.5:8000"
