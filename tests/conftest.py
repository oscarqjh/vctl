"""Shared pytest fixtures."""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Keep tests env-deterministic.
for var in ("VCTL_PROFILE", "MODEL_PROFILE", "VCTL_LB__HOST"):
    os.environ.pop(var, None)
