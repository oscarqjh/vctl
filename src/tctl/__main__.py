"""Allow `python -m tctl` invocation."""

from __future__ import annotations

import sys

from tctl.cli import main

if __name__ == "__main__":
    sys.exit(main())
