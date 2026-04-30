"""Entry point so `python -m vctl` works."""

from vctl.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
