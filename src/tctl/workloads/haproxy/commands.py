"""HAProxy workload commands (stub for Task 4 — full implementation in Task 5).

Only ``_fetch_haproxy_stats`` is provided here so that
``tctl.workloads.haproxy.prune`` can import it at module level (required for
test monkeypatching at ``prune._fetch_haproxy_stats``).

Task 5 replaces this file with the full command dispatcher.
"""

from __future__ import annotations


def _fetch_haproxy_stats(cli: object) -> dict[str, dict[str, dict[str, int | str]]]:
    """Parse ``show stat csv`` from haproxy admin socket.

    Returns backend_section -> server_name -> {scur, qcur, lastchg, ep, status}.
    Numeric fields are int; ``ep`` and ``status`` are str.
    ``status`` is HAProxy's view: UP, DOWN, MAINT, DRAIN, NOLB, etc.
    Only SERVER rows (svname != BACKEND/FRONTEND) are returned.
    Falls back to empty dict on any error.
    """
    from tctl.workloads.haproxy.runtime import RuntimeClient, _parse_endpoint_from_name

    stats: dict[str, dict[str, dict[str, int | str]]] = {}
    if not isinstance(cli, RuntimeClient):
        return stats

    try:
        raw = cli._send("show stat")  # noqa: SLF001
    except Exception:
        return stats

    # HAProxy CSV header: # pxname,svname,qcur,qmax,scur,...,lastchg,...
    # Column indices are defined by the header line (starts with #).
    col_pxname = 0
    col_svname = 1
    col_qcur = 2
    col_scur = 4
    col_lastchg = 23  # typical haproxy 2.x offset; may vary
    col_status = 17  # typical; may vary
    col_addr = 73  # typical; may vary

    header_cols: list[str] = []

    for line in raw.splitlines():
        if line.startswith("# "):
            header_cols = [c.strip() for c in line[2:].split(",")]
            # Build index map from actual header.
            idx = {c: i for i, c in enumerate(header_cols)}
            col_pxname = idx.get("pxname", 0)
            col_svname = idx.get("svname", 1)
            col_qcur = idx.get("qcur", 2)
            col_scur = idx.get("scur", 4)
            col_lastchg = idx.get("lastchg", 23)
            col_status = idx.get("status", 17)
            col_addr = idx.get("addr", 73)
            continue
        if not line or line.startswith("#"):
            continue
        parts = line.split(",")
        if len(parts) < max(col_pxname, col_svname, col_qcur, col_scur) + 1:
            continue
        svname = parts[col_svname] if len(parts) > col_svname else ""
        if svname in ("BACKEND", "FRONTEND", ""):
            continue
        pxname = parts[col_pxname] if len(parts) > col_pxname else ""

        def _int(s: str) -> int:
            try:
                return int(s)
            except (ValueError, IndexError):
                return 0

        scur = _int(parts[col_scur]) if len(parts) > col_scur else 0
        qcur = _int(parts[col_qcur]) if len(parts) > col_qcur else 0
        lastchg = _int(parts[col_lastchg]) if len(parts) > col_lastchg else 0
        status = parts[col_status].strip() if len(parts) > col_status else ""
        addr_raw = parts[col_addr].strip() if len(parts) > col_addr else ""

        # Decode endpoint from server name (b_<ip>_<port>) or addr column.
        ep = ""
        parsed = _parse_endpoint_from_name(svname)
        if parsed is not None:
            ep = f"{parsed[0]}:{parsed[1]}"
        elif addr_raw and addr_raw not in ("0.0.0.0", "-"):
            # addr column may include port as "ip:port" or just "ip".
            ep = addr_raw.split(" ")[0]  # strip any trailing flags

        stats.setdefault(pxname, {})[svname] = {
            "scur": scur,
            "qcur": qcur,
            "lastchg": lastchg,
            "status": status,
            "ep": ep,
        }

    return stats
