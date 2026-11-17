#!/usr/bin/env python3
"""Reconcile with Alpaca (the source of truth), then show the account, Alpaca's positions, and
positions attributed to each thesis from our own filled orders, at current prices.

Cost basis is average cost (trader/pnl.py). Closed positions drop out of `positions`; what they
made or lost is `realized_pl` (per thesis, per symbol in `realized_by_symbol`, and in `totals`).
`deployed` / `pct_deployed` stay on net filled notional, the number the broker's caps use."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402
import json  # noqa: E402

from tools import _common as C  # noqa: E402
from trader import db, pnl  # noqa: E402
from trader.spec import ThesisSpec  # noqa: E402

EPS = 1e-4  # below qty_decimals=4 granularity: rounding noise, not a real position


def portfolio(conn, client, *, reconcile: bool = True) -> dict:
    """Account + Alpaca positions + per-thesis attribution. Used by positions.py and snapshot.py."""
    recon = client.reconcile() if reconcile else None
    account = client.account()
    positions = client.positions()
    by_sym = C.positions_by_symbol(positions)
    deployed = db.deployed_by_thesis(conn)
    lots = pnl.lots_by_thesis(conn)
    qty = {tid: {s: x["qty"] for s, x in per.items()} for tid, per in lots.items()}

    attributed_syms = {s for d in qty.values() for s, q in d.items() if abs(q) > EPS}
    need_quote = sorted(attributed_syms - set(by_sym))
    quotes = client.quote(need_quote) if need_quote else {}

    def price(sym):
        p = by_sym.get(sym)
        if p and C.fnum(p.get("current_price")):
            return C.fnum(p["current_price"])
        return C.quote_price(quotes.get(sym))

    theses = {r["id"]: r for r in db.rows(conn, "SELECT * FROM theses")}
    grouped = {}
    attributed_qty: dict[str, float] = {}
    for tid in sorted(set(theses) | set(qty) | set(deployed)):
        r = theses.get(tid)
        cap = ThesisSpec(**json.loads(r["spec"])).capital_cap if r else None
        rows = []
        realized: dict[str, float] = {}
        for sym in sorted(lots.get(tid, {})):
            lot = lots[tid][sym]
            if abs(lot["realized"]) >= 0.005:
                realized[sym] = round(lot["realized"], 2)
            q = lot["qty"]
            if abs(q) <= EPS:  # closed: its P&L is realized, not a position
                continue
            cost = lot["cost"]
            attributed_qty[sym] = attributed_qty.get(sym, 0.0) + q
            px = price(sym)
            mv = round(q * px, 2) if px is not None else None
            rows.append({"symbol": sym, "qty": q, "price": px, "market_value": mv, "cost": round(cost, 2),
                         "unrealized_pl": round(mv - cost, 2) if mv is not None else None,
                         "unrealized_plpc": round((mv - cost) / cost * 100, 2) if mv is not None and cost > 0 else None,
                         "realized_pl": realized.get(sym, 0.0)})
        t_cost = round(sum(x["cost"] for x in rows), 2)
        t_mv = round(sum(x["market_value"] or 0 for x in rows), 2)
        t_dep = round(sum(max(v, 0.0) for v in deployed.get(tid, {}).values()), 2)
        grouped[tid] = {
            "name": r["name"] if r else None, "status": r["status"] if r else "unknown",
            "capital_cap": cap, "cost": t_cost, "market_value": t_mv,
            "unrealized_pl": round(t_mv - t_cost, 2),
            "realized_pl": round(sum(realized.values()), 2), "realized_by_symbol": realized,
            "deployed": t_dep,
            "pct_deployed": round(t_dep / cap * 100, 1) if cap else None,
            "positions": rows,
        }

    unattributed = []
    for sym in sorted(set(by_sym) | set(attributed_qty)):
        alp = C.fnum(by_sym.get(sym, {}).get("qty")) or 0.0
        diff = round(alp - attributed_qty.get(sym, 0.0), 6)
        if abs(diff) > EPS:
            px = price(sym)
            unattributed.append({"symbol": sym, "qty": diff, "alpaca_qty": alp,
                                 "attributed_qty": round(attributed_qty.get(sym, 0.0), 6), "price": px,
                                 "market_value": round(diff * px, 2) if px is not None else None,
                                 "note": "held at Alpaca but not explained by our orders" if diff > 0
                                 else "our orders say we hold more than Alpaca does (Alpaca wins)"})
    return {"reconciled": recon is not None, "reconcile_updated": (recon or {}).get("updated"),
            "account": account, "alpaca_positions": positions, "theses": grouped,
            "totals": _totals(grouped), "unattributed": unattributed}


def _totals(grouped: dict) -> dict:
    """Across theses: attributed cost / value / unrealized, and realized from closed lots."""
    return {k: round(sum(t[k] for t in grouped.values()), 2)
            for k in ("cost", "market_value", "unrealized_pl", "realized_pl")}


def _main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--thesis", help="only show this thesis in the per-thesis section")
    a = ap.parse_args(argv)
    conn = C.connect()
    if a.thesis:
        C.load_thesis(conn, a.thesis)  # validates the id
    p = portfolio(conn, C.broker())
    if a.thesis:
        p["theses"] = {a.thesis: p["theses"].get(a.thesis, {"positions": []})}
    return {"ok": True, **p}


def main(argv=None) -> int:
    return C.run(_main, argv)


if __name__ == "__main__":
    sys.exit(main())
