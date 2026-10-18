#!/usr/bin/env python3
"""Scheduler tick (every 15 min during market hours, plus catch-up after sleep): run the due
DCA slots of every active thesis. One JSON summary on stdout. Exits 0 even when orders were
refused (refusals are journaled and pushed); non-zero only when something crashed."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402
import json  # noqa: E402
import traceback  # noqa: E402

from tools import _common as C  # noqa: E402
from tools.run_thesis import run_thesis  # noqa: E402
from trader import db  # noqa: E402


def tick(now=None, dry_run=False, conn=None, client=None) -> tuple[dict, int]:
    conn = conn or C.connect()
    clock_cache: dict = {}  # /clock is fetched at most once, and only if some slot is pending
    out = {"ok": True, "results": [], "crashed": []}
    client_box = {"c": client}

    def get_client():
        if client_box["c"] is None:
            client_box["c"] = C.broker()
        return client_box["c"]

    # Fills arrive asynchronously. Reconcile at every tick when we have open orders so a fill
    # is journaled and pushed within 15 minutes, not at the next DCA slot or the nightly job.
    open_orders = db.rows(conn, "SELECT client_order_id FROM orders WHERE status IN ('submitted','partially_filled')")
    if open_orders:
        try:
            out["reconcile"] = get_client().reconcile().get("updated", 0)
        except Exception as e:  # noqa: BLE001
            out["reconcile_error"] = f"{type(e).__name__}: {e}"
            try:
                db.journal(conn, "error", f"tick reconcile failed: {e}")
            except Exception:  # noqa: BLE001
                pass

    for r in C.list_theses(conn, ("active",)):
        try:
            res = run_thesis(r["id"], now=now, dry_run=dry_run, conn=conn, client=_Lazy(get_client),
                             clock_cache=clock_cache)
            out["results"].append(res)
            if any(e.get("status") != 403 for e in res["errors"]):
                out["crashed"].append({"thesis": r["id"], "error": res["errors"]})
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            out["crashed"].append({"thesis": r["id"], "error": f"{type(e).__name__}: {e}"})
            try:
                db.journal(conn, "error", f"tick failed for {r['id']}: {e}", thesis_id=r["id"])
            except Exception:  # noqa: BLE001
                pass
    if "clock" in clock_cache:
        out["clock"] = clock_cache["clock"]
    out["placed"] = sum(x["placed"] for x in out["results"])
    out["refused"] = sum(x["refused"] for x in out["results"])
    out["ok"] = not out["crashed"]
    return out, (0 if not out["crashed"] else 2)


class _Lazy:
    """Defers constructing the broker client until a thesis actually needs it."""

    def __init__(self, factory):
        self._f = factory

    def __getattr__(self, name):
        return getattr(self._f(), name)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--now", help="pretend it is this ISO time (naive = America/New_York)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        out, code = tick(C.parse_dt(a.now) if a.now else None, a.dry_run)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        out, code = {"ok": False, "reason": f"{type(e).__name__}: {e}"}, 2
    print(json.dumps(out, indent=2, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main())
