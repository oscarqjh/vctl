"""Duration string parser — pure stdlib, no tctl imports.

Used by tctl.config.models (field validator) and CLI flag handlers.
Kept standalone to avoid circular imports at config-load time.
"""

from __future__ import annotations

import re

_DURATION_RE = re.compile(r"^\d+[smhd]$")

_SUFFIX_MULTIPLIERS: dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
}


def _parse_duration(s: str) -> int:
    """Parse '300s', '5m', '2h', '1d' -> integer seconds.

    Raises ValueError on unrecognised format.
    Accepted suffixes: s (seconds), m (minutes), h (hours), d (days).
    Input must match ^\\d+[smhd]$; anything else raises ValueError.
    """
    if not _DURATION_RE.match(s):
        raise ValueError(f"invalid duration: {s!r}; expected format like '300s', '5m', '2h', '1d'")
    value = int(s[:-1])
    suffix = s[-1]
    return value * _SUFFIX_MULTIPLIERS[suffix]


#: Public alias — tctl exposes parse_duration without the leading underscore.
parse_duration = _parse_duration
