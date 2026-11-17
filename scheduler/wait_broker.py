#!/usr/bin/env python3
"""Block until the broker daemon answers GET /health, or fail after --timeout seconds.

Used as `ExecStartPost=` of thesis-broker.service: the broker is Type=simple, so without this
the unit counts as started the moment the process forks, and units ordered After= it (tick,
nightly, review, web) can run before it listens ("Connection refused" at boot). With it the
unit is only `active` once /health answers; if it never does, the start fails and
Restart=always tries again. Works the same for user and system units. No token needed.
Prints one JSON line. Exit 0 ready, 1 not ready."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402
import json  # noqa: E402

from trader.client import BrokerClient  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--timeout", type=float, default=60, help="seconds to wait (default 60)")
    ap.add_argument("--interval", type=float, default=0.5, help="poll interval in seconds")
    ap.add_argument("--url", help="broker URL (default from config: BROKER_URL / BROKER_PORT)")
    a = ap.parse_args(argv)
    c = BrokerClient(url=a.url, timeout=2)
    ok, why = c.wait_ready(a.timeout, a.interval)
    print(json.dumps({"ok": ok, "url": c.url, **({} if ok else {"reason": why})}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
