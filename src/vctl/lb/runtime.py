"""Thin client over the HAProxy admin socket (Unix or TCP).

Wire format conventions:
- srv_admin_state is a BITMASK (MAINT bits 0x07, DRAIN bits 0x38)
- srv_addr is IP-only; port decoded from server name (b_<ip>_<port>)
- add_server is idempotent (returns 'already_present' on duplicate)
"""

from __future__ import annotations

import contextlib
import os
import socket
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from vctl.lb.manager import LbManager

LB_ADMIN_MAINT_MASK = 0x07
LB_ADMIN_DRAIN_MASK = 0x38


@dataclass(frozen=True)
class BackendStatus:
    name: str
    endpoint: str
    op_state: int
    admin_state: int = 0

    @property
    def op(self) -> Literal["UP", "DOWN"]:
        return "UP" if self.op_state == 2 else "DOWN"

    @property
    def admin(self) -> Literal["ready", "maint", "drain"]:
        if self.admin_state & LB_ADMIN_MAINT_MASK:
            return "maint"
        if self.admin_state & LB_ADMIN_DRAIN_MASK:
            return "drain"
        return "ready"


def _parse_endpoint_from_name(name: str) -> tuple[str, str] | None:
    """B8: Parse 'b_<ip_underscores>_<port>' server name.

    Returns (ip, port) if the name matches cleanly, else None.
    E.g. 'b_10_1_2_5_8000' → ('10.1.2.5', '8000').
    """
    if not name.startswith("b_"):
        return None
    rest = name[2:]
    # Last segment is port; preceding 4 segments are IP octets.
    parts = rest.split("_")
    if len(parts) != 5:
        return None
    port = parts[4]
    if not port.isdigit():
        return None
    ip_parts = parts[:4]
    if not all(p.isdigit() and 0 <= int(p) <= 255 for p in ip_parts):
        return None
    return ".".join(ip_parts), port


class RuntimeClient:
    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock

    @classmethod
    def for_unix(cls, path: str) -> RuntimeClient:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5.0)  # B2
        s.connect(path)
        return cls(s)

    @classmethod
    def for_tcp(cls, host: str, port: int) -> RuntimeClient:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5.0)  # B2
        s.connect((host, port))
        return cls(s)

    @classmethod
    def for_unix_fd(cls, sock: socket.socket) -> RuntimeClient:
        return cls(sock)

    def _send(self, cmd: str) -> str:
        # B2: loop recv until \n\n terminator or connection closed.
        self._sock.sendall((cmd + "\n").encode("utf-8"))
        chunks: list[bytes] = []
        try:
            while True:
                data = self._sock.recv(4096)
                if not data:
                    break
                chunks.append(data)
                if b"".join(chunks).endswith(b"\n\n"):
                    break
        except TimeoutError as exc:
            raise RuntimeError("haproxy admin socket timeout") from exc
        return b"".join(chunks).decode("utf-8", errors="replace")

    def add_server(self, backend: str, name: str, ep: str) -> Literal["new", "already_present"]:
        # B6: parse success/already-present tokens; raise on errors.
        out = self._send(f"add server {backend}/{name} {ep} check")
        stripped = out.strip()
        low = stripped.lower()
        if low.startswith("new server") or stripped == "":
            return "new"
        if "already exists" in low or "already present" in low:
            return "already_present"
        raise RuntimeError(f"haproxy add_server failed: {stripped}")

    def remove_server(self, backend: str, name: str) -> None:
        # B7: parse response and raise on errors; tolerate "no such server".
        out = self._send(f"del server {backend}/{name}")
        stripped = out.strip()
        if stripped == "" or stripped.lower().startswith("server deleted"):
            return
        low = stripped.lower()
        if "no such server" in low:
            # Already gone — idempotent, don't raise.
            return
        raise RuntimeError(f"haproxy remove_server failed: {stripped}")

    def set_state(self, backend: str, name: str, state: Literal["ready", "maint", "drain"]) -> None:
        # Empty response = success. Non-empty = error message from haproxy
        # (e.g. "No such server.", "...already in maint mode."). Mirrors the
        # parse-then-raise contract used by add_server and remove_server.
        out = self._send(f"set server {backend}/{name} state {state}")
        stripped = out.strip()
        if not stripped:
            return
        raise RuntimeError(f"haproxy set_state failed: {stripped}")

    def show_servers_state(self) -> list[BackendStatus]:
        raw = self._send("show servers state")
        rows: list[BackendStatus] = []
        for line in raw.splitlines():
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 6:
                continue
            try:
                op_state = int(parts[5])
            except ValueError:
                continue
            admin_state = 0
            if len(parts) >= 7:
                with contextlib.suppress(ValueError):
                    admin_state = int(parts[6])
            name = parts[3]
            srv_addr = parts[4]
            # B8: prefer IP decoded from name; skip rows where srv_addr=0.0.0.0
            # and name doesn't parse (unresolved/garbage).
            parsed = _parse_endpoint_from_name(name)
            if parsed is not None:
                endpoint = f"{parsed[0]}:{parsed[1]}"
            elif srv_addr == "0.0.0.0":
                continue  # unresolvable row — skip
            else:
                endpoint = srv_addr
            rows.append(
                BackendStatus(
                    name=name,
                    endpoint=endpoint,
                    op_state=op_state,
                    admin_state=admin_state,
                )
            )
        return rows

    def show_info(self) -> dict[str, str]:
        raw = self._send("show info")
        out: dict[str, str] = {}
        for line in raw.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                out[k.strip()] = v.strip()
        return out


class _NoOpClient:
    """Drop-in stub for RuntimeClient used when VCTL_TEST_NO_SOCKET=1.

    All haproxy admin operations succeed silently so tests that don't care
    about haproxy interactions still pass. Tests that *do* want to assert
    on haproxy calls should monkeypatch ``lb_admin_client`` directly to inject
    a ``unittest.mock.MagicMock``.
    """

    def add_server(self, backend: str, name: str, ep: str) -> str:
        return "new"

    def remove_server(self, backend: str, name: str) -> None:
        pass

    def set_state(self, backend: str, name: str, state: str) -> None:
        pass

    def show_servers_state(self) -> list[BackendStatus]:
        return []


def lb_admin_client(mgr: LbManager) -> RuntimeClient | None:
    """Return a RuntimeClient for the HAProxy admin socket, or None if unreachable.

    Resolution order:
      1. If VCTL_TEST_NO_SOCKET=1 → return _NoOpClient (no real socket attempt).
      2. If the unix socket file exists → try RuntimeClient.for_unix().
         On OSError (NFS mirage workaround) → fall through to TCP.
      3. Try RuntimeClient.for_tcp(host, port).
      4. Both failed → return None.
    """
    if os.environ.get("VCTL_TEST_NO_SOCKET") == "1":
        return _NoOpClient()  # type: ignore[return-value]
    sock = mgr.sock_path
    if sock.exists():
        try:
            return RuntimeClient.for_unix(str(sock))
        except OSError:
            pass  # NFS mirage; fall through to TCP
    try:
        return RuntimeClient.for_tcp(mgr.lb.host, mgr.lb.admin.bind_port)
    except OSError:
        return None
