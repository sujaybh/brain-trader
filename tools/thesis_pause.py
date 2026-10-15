#!/usr/bin/env python3
"""pause a thesis. Run with --help."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import _common as C  # noqa: E402


def _main(argv):
    return C.change_status_tool(argv, target="paused", allowed_from="active", verb="pause")


def main(argv=None) -> int:
    return C.run(_main, argv)


if __name__ == "__main__":
    sys.exit(main())
