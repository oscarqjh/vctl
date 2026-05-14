"""Allow `python -m tctl` invocation."""

from __future__ import annotations

import sys

if __name__ == "__main__":
    # tctl.cli is added in Task 9; this guard prevents ImportError until then.
    from tctl.cli import main  # type: ignore[import-not-found]  # noqa: PLC0415

    sys.exit(main())
