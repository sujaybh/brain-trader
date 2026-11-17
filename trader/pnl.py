"""Cost basis, realized P&L and return vs SPY, all derived from our own filled orders.

Cost basis method: **average cost**, per thesis per symbol. A buy adds its filled notional to
the basis; a sell removes `qty x average cost` from the basis and books
`proceeds - qty x average cost` as realized P&L. Chosen over FIFO because:
  - Alpaca shows `avg_entry_price` (an average) and the dashboard's unattributed rows use it,
    so the thesis rows and the account rows mean the same thing by "cost";
  - DCA buys many small fractional lots; FIFO would make the remaining cost depend on which
    $12 lot a $2 sell happened to hit, which says nothing about the thesis;
  - there is no tax-lot tracking anywhere else (wash-sale is warn-only); tax lots are Alpaca's
    1099, not ours.
For buy-only positions this equals the old "net filled notional". `db.deployed_by_thesis`
(net notional, what the money limits use) is deliberately left unchanged.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from . import config

BENCHMARK = "SPY"
BENCH_TAG = "$BENCH"          # snapshots.thesis_id of the nightly benchmark row
BENCH_MAX_GAP_DAYS = 3        # a fill is priced against the nearest $BENCH row at most this far away


def _fill_rows(conn: sqlite3.Connection, thesis_id: str | None = None) -> list[dict]:
    sql = ("SELECT thesis_id, symbol, side, filled_qty, filled_notional, created FROM orders "
           "WHERE filled_qty > 0")  # incl. canceled/expired after a partial fill
    params: tuple = ()
    if thesis_id is not None:
        sql += " AND thesis_id=?"
        params = (thesis_id,)
    return [dict(r) for r in conn.execute(sql + " ORDER BY created, id", params)]


def _apply(lot: dict, side: str, q: float, notional: float) -> None:
    if side == "buy":
        lot["qty"] += q
        lot["cost"] += notional
        lot["bought"] += notional
        return
    lot["sold"] += notional
    held = max(lot["qty"], 0.0)
    matched = min(q, held)
    avg = lot["cost"] / held if held > 1e-12 else 0.0
    basis = matched * avg
    lot["realized"] += notional - basis  # a sell beyond what we hold (never allowed) is all proceeds
    lot["cost"] -= basis
    lot["qty"] -= q
    if lot["qty"] <= 1e-9:  # fully closed: no leftover basis from float noise
        lot["cost"] = 0.0


def lots_by_thesis(conn: sqlite3.Connection) -> dict[str, dict[str, dict]]:
    """{thesis_id: {symbol: {qty, cost, realized, bought, sold}}} by average cost (see module doc).
    `cost` is the basis of the shares still held; `realized` is cumulative realized P&L."""
    out: dict[str, dict[str, dict]] = {}
    for r in _fill_rows(conn):
        lot = out.setdefault(r["thesis_id"], {}).setdefault(
            r["symbol"], {"qty": 0.0, "cost": 0.0, "realized": 0.0, "bought": 0.0, "sold": 0.0})
        _apply(lot, r["side"], float(r["filled_qty"] or 0), float(r["filled_notional"] or 0))
    for per in out.values():
        for lot in per.values():
            lot["qty"] = round(lot["qty"], 6)
            for k in ("cost", "realized", "bought", "sold"):
                lot[k] = round(lot[k], 4)
    return out


# ------------------------------------------------------------------ return vs SPY

def _market_date(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(config.MARKET_TZ).date().isoformat()


def _nearest(bench: dict[str, float], day: str) -> float | None:
    if day in bench:
        return bench[day]
    d = datetime.fromisoformat(day).date()
    for gap in range(1, BENCH_MAX_GAP_DAYS + 1):
        for cand in ((d - timedelta(days=gap)).isoformat(), (d + timedelta(days=gap)).isoformat()):
            if cand in bench:
                return bench[cand]
    return None


def vs_benchmark(conn: sqlite3.Connection, thesis_id: str, created: str | None = None) -> dict:
    """Thesis return since creation vs SPY over the same window, from the nightly snapshots.

    Same-dollars comparison (right for DCA): every filled buy/sell of the thesis is mirrored by
    a buy/sell of the same dollars of SPY at that day's `$BENCH` price. Both sides are valued at
    the latest snapshot date where every held symbol has a price, and
    return = (value + sell proceeds - buys) / buys.
    Returns {"ok": False, "reason": "not enough history ..."} instead of estimating when there is
    no fill, no snapshot after the first fill day, or a fill day with no `$BENCH` price nearby."""
    no = lambda why: {"ok": False, "reason": f"not enough history: {why}"}  # noqa: E731
    since = _market_date(created) if created else None
    fills = [f for f in _fill_rows(conn, thesis_id) if not since or _market_date(f["created"]) >= since]
    if not fills:
        return no("no fills yet")
    first = _market_date(fills[0]["created"])
    bench = {r["date"]: float(r["price"]) for r in conn.execute(
        "SELECT date, price FROM snapshots WHERE symbol=? AND thesis_id=? AND price > 0", (BENCHMARK, BENCH_TAG))}
    later = sorted((d for d in bench if d > first), reverse=True)
    if not later:
        return no(f"no {BENCHMARK} snapshot after {first}")
    for end in later:
        prices = {r["symbol"]: r["price"] for r in conn.execute(
            "SELECT symbol, price FROM snapshots WHERE date=? AND thesis_id=? AND price IS NOT NULL",
            (end, thesis_id))}
        lots: dict[str, dict] = {}
        spy_qty = invested = proceeds = 0.0
        ok = True
        for f in fills:
            day = _market_date(f["created"])
            if day > end:
                continue
            bpx = _nearest(bench, day)
            if bpx is None:
                return no(f"no {BENCHMARK} price near {day}")
            n = float(f["filled_notional"] or 0)
            lot = lots.setdefault(f["symbol"], {"qty": 0.0, "cost": 0.0, "realized": 0.0, "bought": 0.0, "sold": 0.0})
            _apply(lot, f["side"], float(f["filled_qty"] or 0), n)
            if f["side"] == "buy":
                invested += n
                spy_qty += n / bpx
            else:
                proceeds += n
                spy_qty -= n / bpx
        value = 0.0
        for sym, lot in lots.items():
            if lot["qty"] > 1e-4:
                if prices.get(sym) is None:
                    ok = False
                    break
                value += lot["qty"] * float(prices[sym])
        if not ok or invested <= 0:
            continue  # this snapshot lacks a price for something we held; try an earlier one
        pl = value + proceeds - invested
        spy_pl = spy_qty * bench[end] + proceeds - invested
        ret, spy_ret = pl / invested * 100, spy_pl / invested * 100
        return {"ok": True, "start": first, "end": end, "invested": round(invested, 2),
                "value": round(value, 2), "pl": round(pl, 2), "return_pct": round(ret, 2),
                "spy_pl": round(spy_pl, 2), "spy_return_pct": round(spy_ret, 2),
                "excess_pct": round(ret - spy_ret, 2), "benchmark": BENCHMARK,
                "method": "same dollars into SPY on each fill day; return on gross buys"}
    return no("no snapshot with prices for every held symbol")
