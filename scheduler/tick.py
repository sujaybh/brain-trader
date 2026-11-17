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
from datetime import datetime, timezone  # noqa: E402

from tools import _common as C  # noqa: E402
from tools.run_thesis import run_thesis  # noqa: E402
from trader import db  # noqa: E402
from trader.client import BrokerError, wait_for_broker  # noqa: E402


def tick(now=None, dry_run=False, conn=None, client=None) -> tuple[dict, int]:
    conn = conn or C.connect()
    clock_cache: dict = {}  # /clock is fetched at most once, and only if some slot is pending
    out = {"ok": True, "results": [], "crashed": []}
    client_box = {"c": client, "unready": None}

    def get_client():
        # The broker is only contacted when there is work (open orders or a pending slot). The
        # first time, wait (bounded, BROKER_WAIT_S) for /health: at boot the timer can fire
        # before the broker listens. If it never comes up: one journaled error, no retries.
        if client_box["unready"]:
            raise BrokerError(0, client_box["unready"])
        if client_box["c"] is None:
            c = C.broker()
            if not dry_run:
                ok, why = wait_for_broker("tick", conn=conn, client=c)
                if not ok:
                    client_box["unready"] = why
                    out["broker_unready"] = why
                    raise BrokerError(0, why)
            client_box["c"] = c
        return client_box["c"]

    def journal_error(msg, **kw):
        if client_box["unready"]:
            return  # already journaled once by wait_for_broker
        try:
            db.journal(conn, "error", msg, **kw)
        except Exception:  # noqa: BLE001
            pass

    # Fills arrive asynchronously. Reconcile at every tick when we have open orders so a fill
    # is journaled and pushed within 15 minutes, not at the next DCA slot or the nightly job.
    open_orders = db.rows(conn, "SELECT client_order_id FROM orders WHERE status IN ('submitted','partially_filled')")
    if open_orders:
        try:
            out["reconcile"] = get_client().reconcile().get("updated", 0)
        except Exception as e:  # noqa: BLE001
            out["reconcile_error"] = f"{type(e).__name__}: {e}"
            journal_error(f"tick reconcile failed: {e}")

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
            journal_error(f"tick failed for {r['id']}: {e}", thesis_id=r["id"])
    if "clock" in clock_cache:
        out["clock"] = clock_cache["clock"]
    out["placed"] = sum(x["placed"] for x in out["results"])
    out["refused"] = sum(x["refused"] for x in out["results"])
    out["ok"] = not out["crashed"] and not out.get("broker_unready")
    if out["ok"] and not dry_run:
        db.set_setting(conn, "job_ok:tick", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return out, (0 if out["ok"] else 2)


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
