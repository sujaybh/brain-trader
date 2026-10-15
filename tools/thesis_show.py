#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402

from tools import _common as C  # noqa: E402
from tools.journal import entries  # noqa: E402
from trader import db  # noqa: E402
from trader.spec import describe  # noqa: E402

DOC = "Show one thesis: spec, plain-English read-back, deployed capital, last 10 journal entries."


def _main(argv):
    ap = argparse.ArgumentParser(description=DOC)
    ap.add_argument("id")
    a = ap.parse_args(argv)
    conn = C.connect()
    r, spec = C.load_thesis(conn, a.id)
    dep = db.deployed_by_thesis(conn).get(a.id, {})
    qty = db.qty_by_thesis(conn).get(a.id, {})
    total = round(sum(dep.values()), 2)
    return {
        "ok": True, "id": r["id"], "name": r["name"], "status": r["status"], "mode": r["mode"],
        "created": r["created"], "updated": r["updated"],
        "describe": describe(spec), "spec": spec.model_dump(),
        "deployed": {"total": total, "capital_cap": spec.capital_cap,
                     "pct": round(total / spec.capital_cap * 100, 1), "by_symbol": dep, "qty_by_symbol": qty},
        "journal": entries(conn, thesis=a.id, limit=10),
    }


def main(argv=None) -> int:
    return C.run(_main, argv)


if __name__ == "__main__":
    sys.exit(main())
