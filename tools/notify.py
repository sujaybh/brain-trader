#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402

from tools import _common as C  # noqa: E402
from trader import notify as N  # noqa: E402

DOC = "Push one notification to the phone (ntfy). Journals it. No-op if NTFY_TOPIC is unset."


def _main(argv):
    ap = argparse.ArgumentParser(description=DOC)
    ap.add_argument("text")
    ap.add_argument("--title")
    ap.add_argument("--priority", type=int, choices=range(1, 6), metavar="1-5")
    a = ap.parse_args(argv)
    sent = N.push(a.text, title=a.title, priority=a.priority)
    return {"ok": True, "sent": sent, "text": a.text, "title": a.title, "priority": a.priority}


def main(argv=None) -> int:
    return C.run(_main, argv)


if __name__ == "__main__":
    sys.exit(main())
