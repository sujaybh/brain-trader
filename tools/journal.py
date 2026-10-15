#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402
import json  # noqa: E402
from datetime import timezone  # noqa: E402

from tools import _common as C  # noqa: E402
from trader import db  # noqa: E402

DOC = "Read the journal (the system's memory), newest first."


def entries(conn, since=None, thesis=None, kind=None, limit=50):
    where, params = [], []
    if since is not None:
        where.append("ts >= ?")
        params.append(since.astimezone(timezone.utc).isoformat(timespec="seconds"))
    if thesis:
        where.append("thesis_id = ?")
        params.append(thesis)
    if kind:
        where.append("kind = ?")
        params.append(kind)
    sql = "SELECT * FROM journal" + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    out = db.rows(conn, sql, (*params, limit))
    for e in out:
        if e.get("data"):
            try:
                e["data"] = json.loads(e["data"])
            except ValueError:
                pass
    return out


def _main(argv):
    ap = argparse.ArgumentParser(description=DOC)
    ap.add_argument("--since", help="7d, 24h, 30m, YYYY-MM-DD, or ISO datetime")
    ap.add_argument("--thesis")
    ap.add_argument("--kind", help="order, refused, fill, thesis, trigger, skipped, expired, error, ...")
    ap.add_argument("--limit", type=int, default=50)
    a = ap.parse_args(argv)
    since = C.parse_since(a.since) if a.since else None
    rows = entries(C.connect(), since, a.thesis, a.kind, a.limit)
    return {"ok": True, "count": len(rows), "entries": rows}


def main(argv=None) -> int:
    return C.run(_main, argv)


if __name__ == "__main__":
    sys.exit(main())
