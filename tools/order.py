#!/usr/bin/env python3
"""Place one order through the broker daemon (which enforces every limit). With no --thesis
the order lands in the implicit `manual` thesis. A refusal prints ok:false and exits 1."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

from tools import _common as C  # noqa: E402
from trader import config  # noqa: E402
from trader import notify as N  # noqa: E402


def _main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--side", required=True, choices=["buy", "sell"])
    ap.add_argument("--symbol", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--notional", type=float, help="USD amount")
    g.add_argument("--qty", type=float, help="shares (fractional ok)")
    ap.add_argument("--thesis", default=config.MANUAL_THESIS_ID)
    ap.add_argument("--reason", default="manual order from chat")
    a = ap.parse_args(argv)
    if (a.notional is not None and a.notional <= 0) or (a.qty is not None and a.qty <= 0):
        raise C.ToolFailure("--notional/--qty must be positive")
    symbol = a.symbol.strip().upper()

    conn = C.connect()
    C.ensure_manual_thesis(conn)
    r, _ = C.load_thesis(conn, a.thesis)  # unknown thesis -> ok:false, exit 1
    if r["status"] == "closed":
        raise C.ToolFailure(f"thesis {a.thesis} is closed")

    # No client_order_id: the broker derives one (and detects a same-second double submit).
    # The broker also pushes the fill or the refusal, so this tool stays quiet.
    order = C.broker().place_order(thesis_id=a.thesis, side=a.side, symbol=symbol, notional=a.notional,
                                   qty=a.qty, reason=a.reason)
    return {"ok": True, "thesis": a.thesis, "order": order}


def main(argv=None) -> int:
    return C.run(_main, argv)


if __name__ == "__main__":
    sys.exit(main())
