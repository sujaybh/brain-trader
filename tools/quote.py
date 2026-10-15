#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402

from tools import _common as C  # noqa: E402

DOC = "Latest quotes for one or more symbols, via the broker daemon's /quote."


def _main(argv):
    ap = argparse.ArgumentParser(description=DOC)
    ap.add_argument("symbols", nargs="+", metavar="SYM")
    a = ap.parse_args(argv)
    syms = [s.strip().upper() for s in a.symbols if s.strip()]
    return {"ok": True, "quotes": C.broker().quote(syms)}


def main(argv=None) -> int:
    return C.run(_main, argv)


if __name__ == "__main__":
    sys.exit(main())
