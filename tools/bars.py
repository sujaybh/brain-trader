#!/usr/bin/env python3
"""Backfill or update daily bars (broker GET /bars, Alpaca's free IEX feed) into the `bars`
table. With no symbols: every active/paused thesis universe plus SPY (the benchmark).

A symbol with no stored bars is backfilled --days calendar days. Otherwise only the gap since
the last stored bar is fetched, with a few days of overlap; if an overlapping close moved by
more than 0.5% (a split or dividend re-adjusted history) the symbol is re-fetched in full.
Today's bar is only stored after 16:30 New York time, so a partial session never lands."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402
from datetime import date, datetime, time, timedelta  # noqa: E402

from tools import _common as C  # noqa: E402
from trader import config, db  # noqa: E402

BENCHMARK = "SPY"
DEFAULT_DAYS = 800       # ~2.2 years: a 252-day lookback plus two years to test on
OVERLAP_DAYS = 7
READJUST_TOLERANCE = 0.005
BATCH = 50               # broker limit per request


def default_symbols(conn) -> list[str]:
    syms = {BENCHMARK}
    for r in C.list_theses(conn, ("active", "paused")):
        syms.update(C.load_thesis(conn, r["id"])[1].universe)
    return sorted(syms)


def last_complete_day(now: datetime) -> date:
    now = now.astimezone(config.MARKET_TZ)
    return now.date() if now.time() >= time(16, 30) else now.date() - timedelta(days=1)


def _stored(conn, sym: str) -> tuple[str | None, str | None, int]:
    r = db.row(conn, "SELECT MIN(date) AS a, MAX(date) AS b, COUNT(*) AS n FROM bars WHERE symbol=?", (sym,))
    return r["a"], r["b"], int(r["n"] or 0)


def _fetch(client, syms: list[str], start: date, end: date) -> tuple[str, dict[str, list[dict]]]:
    source, out = "unknown", {}
    for i in range(0, len(syms), BATCH):
        res = client.bars(syms[i:i + BATCH], start=start.isoformat(), end=end.isoformat())
        source = res.get("source", source)
        out.update(res.get("bars") or {})
    return source, out


def update_bars(conn, client, symbols: list[str] | None = None, *, days: int = DEFAULT_DAYS,
                refresh: bool = False, now: datetime | None = None) -> dict:
    now = now or datetime.now(config.MARKET_TZ)
    end = last_complete_day(now)
    symbols = [s.strip().upper() for s in symbols] if symbols else default_symbols(conn)
    full_start = end - timedelta(days=days)
    plan: dict[date, list[str]] = {}
    stored = {s: _stored(conn, s) for s in symbols}
    for s in symbols:
        first, last, _ = stored[s]
        if refresh or last is None:
            start = min(full_start, date.fromisoformat(first)) if first else full_start
        else:
            start = date.fromisoformat(last) - timedelta(days=OVERLAP_DAYS)
        if start <= end:
            plan.setdefault(start, []).append(s)

    out = {"ok": True, "end": end.isoformat(), "symbols": {}, "errors": []}
    readjust: list[str] = []
    for start, syms in sorted(plan.items()):
        source, got = _fetch(client, syms, start, end)
        for s in syms:
            rows = got.get(s) or []
            info = {"fetched": len(rows), "from": start.isoformat(), "source": source}
            if not rows and (stored[s][1] is None or refresh):
                out["errors"].append({"symbol": s, "reason": "no bars returned (unknown symbol or no IEX data)"})
                out["symbols"][s] = info
                continue
            if stored[s][1] is not None and not refresh:
                old = {r["date"]: r["close"] for r in db.rows(
                    conn, "SELECT date, close FROM bars WHERE symbol=? AND date>=?", (s, start.isoformat()))}
                moved = [r["date"] for r in rows if r["date"] in old and old[r["date"]] and
                         abs(r["close"] / old[r["date"]] - 1) > READJUST_TOLERANCE]
                if moved:
                    readjust.append(s)
                    info["readjusted_from"] = moved[0]
                    out["symbols"][s] = info
                    continue
            with db.tx(conn):
                if refresh:  # one adjustment basis for the whole history
                    conn.execute("DELETE FROM bars WHERE symbol=?", (s,))
                db.upsert_bars(conn, s, rows, source)
            out["symbols"][s] = info
    if readjust:  # history was re-adjusted upstream: replace this symbol's bars wholesale
        first = min(date.fromisoformat(stored[s][0]) for s in readjust)
        source, got = _fetch(client, readjust, min(first, full_start), end)
        for s in readjust:
            rows = got.get(s) or []
            with db.tx(conn):
                conn.execute("DELETE FROM bars WHERE symbol=?", (s,))
                db.upsert_bars(conn, s, rows, source)
            out["symbols"][s].update({"refetched": len(rows), "source": source})
    for s in symbols:
        a, b, n = _stored(conn, s)
        out["symbols"].setdefault(s, {"fetched": 0})
        out["symbols"][s].update({"first": a, "last": b, "stored": n})
    out["ok"] = not out["errors"]
    return out


def _main(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbols", nargs="*", help="tickers (default: all thesis universes + SPY)")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS,
                    help=f"calendar days to backfill for a new symbol (default {DEFAULT_DAYS})")
    ap.add_argument("--refresh", action="store_true", help="replace stored history with a fresh fetch (--days, or back to the first stored bar)")
    ap.add_argument("--status", action="store_true", help="show what is stored; fetch nothing")
    a = ap.parse_args(argv)
    if a.days < 1 or a.days > 3650:
        raise C.ToolFailure("--days must be between 1 and 3650")
    conn = C.connect()
    if a.status:
        syms = [s.upper() for s in a.symbols] or default_symbols(conn)
        return {"ok": True, "symbols": {s: dict(zip(("first", "last", "stored"), _stored(conn, s))) for s in syms}}
    res = update_bars(conn, C.broker(), a.symbols or None, days=a.days, refresh=a.refresh)
    if res["errors"]:
        res["reason"] = "; ".join(f"{e['symbol']}: {e['reason']}" for e in res["errors"])
    return res


def main(argv=None) -> int:
    return C.run(_main, argv)


if __name__ == "__main__":
    sys.exit(main())
