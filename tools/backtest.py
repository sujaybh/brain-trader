#!/usr/bin/env python3
"""Backtest a thesis spec over stored daily bars with the same signals() and caps the live
system uses (strategies/backtest.py). Read-only: opens the DB read-only and never talks to
the broker. Bars come from tools/bars.py.

  backtest.py --spec spec.json                       a compiled spec that is not stored yet
  backtest.py --thesis semis-q4                      an existing thesis
  backtest.py --spec m.json --sweep lookback_days=63,126,252 --sweep top_n=2,3

Prints total return, CAGR, max drawdown, turnover and trade count, next to SPY buy-and-hold
over the same window. --trades adds the trade log, --curve the daily equity curve."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402
import json  # noqa: E402
import sqlite3  # noqa: E402

from pydantic import ValidationError  # noqa: E402

from tools import _common as C  # noqa: E402
from trader import config, db  # noqa: E402
from trader.spec import ThesisSpec  # noqa: E402

from strategies.backtest import parse_grid, run_backtest, sweep  # noqa: E402

BENCHMARK = "SPY"


def _load_spec(a) -> ThesisSpec:
    if a.spec:
        try:
            data = json.loads(sys.stdin.read() if a.spec == "-" else Path(a.spec).read_text())
            return ThesisSpec(**data)
        except (OSError, ValueError) as e:
            if isinstance(e, ValidationError):
                raise C.ToolFailure("spec is invalid: " + "; ".join(
                    f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()))
            raise C.ToolFailure(f"cannot read spec: {e}")
    conn = _ro(a.db)
    try:
        r = db.row(conn, "SELECT spec FROM theses WHERE id=?", (a.thesis,))
    finally:
        conn.close()
    if not r:
        raise C.ToolFailure(f"no thesis with id {a.thesis!r}")
    return ThesisSpec(**json.loads(r["spec"]))


def _ro(path: str | None) -> sqlite3.Connection:
    p = Path(path) if path else config.DB_PATH
    if not p.exists():
        raise C.ToolFailure(f"no database at {p}")
    return db.connect_ro(p)


def _main(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--spec", help="spec JSON file ('-' for stdin)")
    src.add_argument("--thesis", help="id of a stored thesis")
    ap.add_argument("--start", help="first trading day YYYY-MM-DD (default: earliest with enough history)")
    ap.add_argument("--end", help="last trading day YYYY-MM-DD (default: newest stored bar)")
    ap.add_argument("--sweep", action="append", default=[], metavar="KEY=V1,V2",
                    help="try several values of a strategy parameter; repeatable")
    ap.add_argument("--slippage-pct", type=float, help="override limit_slippage_pct from limits.json")
    ap.add_argument("--db", help="database to read bars (and --thesis) from (default data/trader.db)")
    ap.add_argument("--trades", action="store_true", help="include the trade log and cap adjustments")
    ap.add_argument("--curve", action="store_true", help="include the daily equity curve")
    a = ap.parse_args(argv)

    spec = _load_spec(a)
    conn = _ro(a.db)
    try:
        bars = db.load_bars(conn, sorted(set(spec.universe) | {BENCHMARK}), end=a.end)
    except sqlite3.OperationalError as e:
        raise C.ToolFailure(f"cannot read bars ({e}); run tools/bars.py first")
    finally:
        conn.close()
    missing = [s for s in spec.universe if not bars.get(s)]
    if missing:
        raise C.ToolFailure(f"no stored bars for {', '.join(missing)}; run tools/bars.py {' '.join(missing)}",
                            missing=missing)
    limits = config.load_limits()
    kw = {"start": a.start, "end": a.end, "benchmark": BENCHMARK, "slippage_pct": a.slippage_pct}
    if a.sweep:
        try:
            grid = parse_grid(a.sweep)
            return sweep(spec, bars, limits, grid, **kw)
        except (ValueError, ValidationError) as e:
            raise C.ToolFailure(f"bad --sweep: {e}")
    res = run_backtest(spec, bars, limits, **kw)
    if not a.trades:
        res.pop("trade_log", None)
        res.pop("adjustments", None)
    if not a.curve:
        res.pop("equity_curve", None)
    return res


def main(argv=None) -> int:
    return C.run(_main, argv)


if __name__ == "__main__":
    sys.exit(main())
