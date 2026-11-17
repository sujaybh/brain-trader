#!/usr/bin/env python3
"""Reconcile, then write today's snapshot rows: one per thesis x symbol (attributed qty x current
price), one per unattributed symbol (thesis_id NULL), and a `$ACCOUNT` row (equity/cash).
`realized_pl` is cumulative realized P&L (average cost) for that thesis x symbol; a symbol the
thesis has fully sold gets a row with qty 0, value 0, cost 0 and only `realized_pl` set, so
summing a date's rows still gives the thesis's value and cost.
Idempotent: re-running for the same date replaces that date's rows."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402
import json  # noqa: E402
from datetime import date, datetime  # noqa: E402

from tools import _common as C  # noqa: E402
from tools.positions import portfolio  # noqa: E402
from trader import config, db  # noqa: E402

BENCHMARK = "SPY"

UPSERT = """INSERT INTO snapshots(date, symbol, thesis_id, qty, price, market_value, cost_basis, unrealized_pl, data,
  realized_pl)
VALUES (?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(date, symbol, thesis_id) DO UPDATE SET qty=excluded.qty, price=excluded.price,
  market_value=excluded.market_value, cost_basis=excluded.cost_basis,
  unrealized_pl=excluded.unrealized_pl, data=excluded.data, realized_pl=excluded.realized_pl"""


def take_snapshot(conn, client, day: str | None = None) -> dict:
    day = day or datetime.now(config.MARKET_TZ).date().isoformat()
    p = portfolio(conn, client)
    acct = p["account"]
    rows = []
    for tid, t in p["theses"].items():
        for x in t["positions"]:
            rows.append((day, x["symbol"], tid, x["qty"], x["price"], x["market_value"], x["cost"],
                         x["unrealized_pl"], json.dumps({"unrealized_plpc": x["unrealized_plpc"]}),
                         x.get("realized_pl", 0.0)))
        open_syms = {x["symbol"] for x in t["positions"]}
        for sym, rpl in t.get("realized_by_symbol", {}).items():
            if sym not in open_syms:  # closed: realized only
                rows.append((day, sym, tid, 0.0, None, 0.0, 0.0, 0.0, json.dumps({"closed": True}), rpl))
    alp = C.positions_by_symbol(p["alpaca_positions"])
    null_rows = []
    for u in p["unattributed"]:
        ap = alp.get(u["symbol"], {})
        null_rows.append((day, u["symbol"], None, u["qty"], u["price"], u["market_value"], None, None,
                          json.dumps({"unattributed": True, "alpaca_qty": u["alpaca_qty"],
                                      "avg_entry_price": ap.get("avg_entry_price")}), None))
    try:  # benchmark row: thesis_id '$BENCH' so it never collides with a real position row
        bq = client.quote([BENCHMARK]).get(BENCHMARK) or {}
        bpx = C.fnum(bq.get("last") or bq.get("ask") or bq.get("bid"))
        if bpx:
            rows.append((day, BENCHMARK, "$BENCH", None, bpx, None, None, None, json.dumps({"benchmark": True}), None))
    except Exception as e:  # a missing benchmark must not block the snapshot
        print(f"[snapshot] benchmark quote failed: {e}", file=sys.stderr)
    null_rows.append((day, "$ACCOUNT", None, None, None, C.fnum(acct.get("equity")), None, None,
                      json.dumps({k: acct.get(k) for k in ("cash", "equity", "portfolio_value", "buying_power")}),
                      None))
    with db.tx(conn):
        # UNIQUE(date, symbol, thesis_id) treats NULL thesis_ids as distinct, so NULL rows are
        # replaced explicitly; rows for positions that disappeared since an earlier run today go too.
        keep = {(r[1], r[2]) for r in rows}
        for old in db.rows(conn, "SELECT id, symbol, thesis_id FROM snapshots WHERE date=?", (day,)):
            if old["thesis_id"] is None or (old["symbol"], old["thesis_id"]) not in keep:
                conn.execute("DELETE FROM snapshots WHERE id=?", (old["id"],))
        for r in rows:
            conn.execute(UPSERT, r)
        for r in null_rows:
            conn.execute(UPSERT, r)
    return {"date": day, "rows": len(rows) + len(null_rows), "thesis_rows": len(rows),
            "unattributed_rows": len(null_rows) - 1, "equity": C.fnum(acct.get("equity")),
            "cash": C.fnum(acct.get("cash")), "realized_pl": p["totals"]["realized_pl"],
            "unrealized_pl": p["totals"]["unrealized_pl"]}


def _main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="YYYY-MM-DD (default: today, America/New_York)")
    a = ap.parse_args(argv)
    if a.date:
        try:
            date.fromisoformat(a.date)
        except ValueError:
            raise C.ToolFailure(f"bad --date {a.date!r}; use YYYY-MM-DD")
    return {"ok": True, **take_snapshot(C.connect(), C.broker(), a.date)}


def main(argv=None) -> int:
    return C.run(_main, argv)


if __name__ == "__main__":
    sys.exit(main())
