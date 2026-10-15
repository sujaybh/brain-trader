#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402
import json  # noqa: E402

from tools import _common as C  # noqa: E402
from trader import db  # noqa: E402
from trader.spec import ThesisSpec, describe  # noqa: E402

DOC = "List theses (active and paused by default) with deployed capital."


def _main(argv):
    ap = argparse.ArgumentParser(description=DOC)
    ap.add_argument("--all", action="store_true", help="include closed theses")
    a = ap.parse_args(argv)
    conn = C.connect()
    deployed = db.deployed_by_thesis(conn)
    out = []
    for r in C.list_theses(conn, None if a.all else ("active", "paused")):
        spec = ThesisSpec(**json.loads(r["spec"]))
        dep = round(sum(deployed.get(r["id"], {}).values()), 2)
        out.append({"id": r["id"], "name": r["name"], "status": r["status"], "mode": r["mode"],
                    "universe": spec.universe, "capital_cap": spec.capital_cap, "deployed": dep,
                    "pct_deployed": round(dep / spec.capital_cap * 100, 1) if spec.capital_cap else None,
                    "schedule": spec.schedule.model_dump(), "created": r["created"],
                    "summary": describe(spec)})
    return {"ok": True, "count": len(out), "theses": out}


def main(argv=None) -> int:
    return C.run(_main, argv)


if __name__ == "__main__":
    sys.exit(main())
