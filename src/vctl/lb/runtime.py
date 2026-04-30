"""Thin client over the HAProxy admin socket (Unix or TCP).

Encodes carry-overs from the bash prototype:
- srv_admin_state is a BITMASK (MAINT bits 0x07, DRAIN bits 0x38)
- srv_addr is IP-only; port decoded from server name (b_<ip>_<port>)
- add_server is idempotent (returns 'already_present' on duplicate)
"""

from __future__ import annotations

import contextlib
import socket
from dataclasses import dataclass
from typing import Literal

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


def _parse_endpoint_from_name(name: str, ip_only: str) -> str:
    if name.startswith("b_"):
        rest = name[2:]
        bits = rest.rsplit("_", 1)
        if len(bits) == 2 and bits[1].isdigit():
            return f"{ip_only}:{bits[1]}"
    return ip_only


class RuntimeClient:
    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock

    @classmethod
    def for_unix(cls, path: str) -> RuntimeClient:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(path)
        return cls(s)

    @classmethod
    def for_tcp(cls, host: str, port: int) -> RuntimeClient:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((host, port))
        return cls(s)

    @classmethod
    def for_unix_fd(cls, sock: socket.socket) -> RuntimeClient:
        return cls(sock)

    def _send(self, cmd: str) -> str:
        self._sock.sendall((cmd + "\n").encode("utf-8"))
        chunks: list[bytes] = []
        while True:
            data = self._sock.recv(4096)
            if not data:
                break
            chunks.append(data)
            if b"\n\n" in data or len(data) < 4096:
                break
        return b"".join(chunks).decode("utf-8", errors="replace")

    def add_server(self, backend: str, name: str, ep: str) -> Literal["new", "already_present"]:
        out = self._send(f"add server {backend}/{name} {ep} check")
        if "already exists" in out.lower() or "already present" in out.lower():
            return "already_present"
        return "new"

    def remove_server(self, backend: str, name: str) -> None:
        self._send(f"del server {backend}/{name}")

    def set_state(self, backend: str, name: str, state: Literal["ready", "maint", "drain"]) -> None:
        self._send(f"set server {backend}/{name} state {state}")

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
            ip = parts[4]
            rows.append(
                BackendStatus(
                    name=name,
                    endpoint=_parse_endpoint_from_name(name, ip),
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
