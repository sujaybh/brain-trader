"""Replay a thesis spec over stored daily bars with the live signals() and the live caps.

Timeline for each trading day D in the window:
    1. MarketData.view(bars, D): only bars dated before D (the previous close and earlier).
    2. signals(spec, data, state); the day's period() is marked done (as tools/run_strategy.py does).
    3. strategies.caps.apply_caps() with the previous close as the price estimate.
    4. Fills at D's open: buys at open * (1 + slippage), sells at open * (1 - slippage), where
       slippage defaults to limit_slippage_pct (the marketable-limit offset the broker uses;
       assuming the fill lands on the limit is the pessimistic case). A name with no bar on D
       does not fill; the signal is dropped and noted.
    5. Equity marked at D's close.

The thesis starts with capital_cap in cash and nothing else; idle cash earns nothing.
The benchmark buys capital_cap of SPY at the first day's open (same slippage) and holds.
No commissions (Alpaca has none), no dividends beyond what adjusted bars carry, no PDT model
(monthly rebalancing does not day-trade), no partial fills.
"""
from __future__ import annotations

import itertools
import math
from datetime import date
from typing import Any, Mapping, Optional, Sequence

from trader.spec import ThesisSpec

from . import for_spec
from .base import MarketData, ThesisState
from .caps import apply_caps

OVERFIT_WARNING = ("{n} parameter combinations were tried on the same data. The best one is picked "
                   "after the fact, so its result overstates what to expect going forward. Prefer the "
                   "default or a value chosen before looking, and treat small differences between runs "
                   "as noise.")


def _floor(x: float, nd: int) -> float:
    f = 10 ** nd
    return math.floor(x * f + 1e-9) / f


def _metrics(curve: list[tuple[str, float]], start_value: float) -> dict:
    if not curve:
        return {"total_return": None, "cagr": None, "max_drawdown": None, "end_value": None}
    end_value = curve[-1][1]
    tr = end_value / start_value - 1
    days = (date.fromisoformat(curve[-1][0]) - date.fromisoformat(curve[0][0])).days
    years = days / 365.25
    cagr = (1 + tr) ** (1 / years) - 1 if years > 0 and tr > -1 else None
    peak, mdd = start_value, 0.0
    for _, v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1 if peak else 0.0)
    return {"total_return": round(tr, 6), "cagr": None if cagr is None else round(cagr, 6),
            "max_drawdown": round(mdd, 6), "end_value": round(end_value, 2)}


def _sorted(bars: Mapping[str, Sequence[dict]]) -> dict[str, list[dict]]:
    return {s: sorted(rows, key=lambda r: r["date"]) for s, rows in bars.items()}


def default_start(spec: ThesisSpec, bars: Mapping[str, list[dict]], calendar: list[str]) -> Optional[str]:
    """First calendar day by which every universe name with any bars has `warmup` bars of history."""
    need = for_spec(spec).warmup(spec)
    firsts = []
    for s in spec.universe:
        rows = bars.get(s) or []
        if not rows:
            continue
        if len(rows) <= need:
            return None
        firsts.append(rows[need]["date"] if need else rows[0]["date"])
    if not firsts:
        return None
    d = max(firsts)
    return next((c for c in calendar if c >= d), None)


def run_backtest(spec: ThesisSpec, bars: Mapping[str, Sequence[dict]], limits: Mapping[str, Any], *,
                 start: Optional[str] = None, end: Optional[str] = None, benchmark: str = "SPY",
                 slippage_pct: Optional[float] = None) -> dict:
    impl = for_spec(spec)
    bars = _sorted(bars)
    limits = dict(limits)
    if slippage_pct is not None:
        limits["limit_slippage_pct"] = slippage_pct
    slip = float(limits["limit_slippage_pct"]) / 100
    qd = int(limits.get("qty_decimals", 4))
    universe = list(spec.universe)
    missing = [s for s in universe if not bars.get(s)]
    calendar = sorted({r["date"] for s in universe for r in bars.get(s) or []})
    start = start or default_start(spec, bars, calendar)
    if start is None:
        return {"ok": False, "reason": f"not enough bars: {impl.type} needs {impl.warmup(spec)} days of "
                                       f"history per name", "missing": missing}
    window = [d for d in calendar if d >= start and (end is None or d <= end)]
    if len(window) < 2:
        return {"ok": False, "reason": "window has fewer than two trading days of bars", "missing": missing}

    by_date = {s: {r["date"]: r for r in rows} for s, rows in bars.items()}
    dates = {s: [r["date"] for r in rows] for s, rows in bars.items()}
    view_bars = {s: bars[s] for s in universe if s in bars}

    cash = float(spec.capital_cap)
    state = ThesisState()
    trades: list[dict] = []
    adjustments: list[dict] = []
    curve: list[tuple[str, float]] = []
    last_close: dict[str, float] = {}
    traded = 0.0
    invested_sum = 0.0

    for d in window:
        asof = date.fromisoformat(d)
        data = MarketData.view(view_bars, asof, dates=dates)
        sigs = impl.signals(spec, data, state)
        state.last_period = impl.period(spec, asof)
        prices = {s: p for s in universe if (p := data.price(s)) is not None}
        orders, notes = apply_caps(sigs, spec, state, limits, prices)
        adjustments += [{"date": d, **n} for n in notes]
        for o in orders:
            bar = by_date.get(o.symbol, {}).get(d)
            if bar is None:
                adjustments.append({"date": d, "signal": o.as_dict(), "action": "dropped", "why": "no bar to fill on"})
                continue
            if o.side == "buy":
                px = round(bar["open"] * (1 + slip), 4)
                notional = min(o.notional, cash)
                q = _floor(notional / px, qd)
                if q <= 0:
                    continue
                n = round(q * px, 4)
                cash -= n
                state.qty[o.symbol] = state.qty.get(o.symbol, 0.0) + q
                state.deployed[o.symbol] = state.deployed.get(o.symbol, 0.0) + n
            else:
                px = round(bar["open"] * (1 - slip), 4)
                q = min(o.qty, state.qty.get(o.symbol, 0.0))
                if q <= 0:
                    continue
                n = round(q * px, 4)
                cash += n
                state.qty[o.symbol] -= q
                state.deployed[o.symbol] = state.deployed.get(o.symbol, 0.0) - n
                if state.qty[o.symbol] <= 1e-9:
                    state.qty.pop(o.symbol)
            traded += n
            trades.append({"date": d, "symbol": o.symbol, "side": o.side, "qty": q, "price": px,
                           "notional": round(n, 2), "reason": o.reason,
                           "signal_data_through": data.last_date(o.symbol)})
        for s in universe:
            r = by_date.get(s, {}).get(d)
            if r is not None:
                last_close[s] = float(r["close"])
        invested = sum(q * last_close.get(s, 0.0) for s, q in state.qty.items())
        invested_sum += invested
        curve.append((d, cash + invested))

    m = _metrics(curve, spec.capital_cap)
    years = max((date.fromisoformat(window[-1]) - date.fromisoformat(window[0])).days / 365.25, 1e-9)
    avg_equity = sum(v for _, v in curve) / len(curve)
    out = {
        "ok": True, "thesis": spec.id, "strategy": spec.strategy.model_dump() if spec.strategy else
        {"type": impl.type}, "universe": universe, "start": window[0], "end": window[-1],
        "trading_days": len(window), "capital": spec.capital_cap, **m,
        "turnover": round(traded / avg_equity / years, 3) if avg_equity else None,  # traded notional / avg equity / year
        "traded_notional": round(traded, 2), "trades": len(trades),
        "buys": sum(t["side"] == "buy" for t in trades), "sells": sum(t["side"] == "sell" for t in trades),
        "avg_invested_pct": round(100 * invested_sum / len(curve) / avg_equity, 1) if avg_equity else None,
        "final_positions": {s: round(q, 6) for s, q in sorted(state.qty.items())},
        "final_cash": round(cash, 2),
        "fill_model": f"next open after the signal's data, {slip * 100:g}% slippage against you",
        "limits": {k: limits.get(k) for k in ("per_order_cap", "global_cap", "limit_slippage_pct",
                                              "min_order_notional", "qty_decimals")},
        "missing_bars": missing,
        "trade_log": trades,
        "adjustments": adjustments,
        "equity_curve": [[d, round(v, 2)] for d, v in curve],
    }
    out["benchmark"] = _benchmark(benchmark, by_date.get(benchmark), window, spec.capital_cap, slip, qd)
    if out["benchmark"].get("total_return") is not None and m["total_return"] is not None:
        out["excess_return"] = round(m["total_return"] - out["benchmark"]["total_return"], 6)
    return out


def _benchmark(sym: str, rows: Optional[dict], window: list[str], capital: float, slip: float, qd: int) -> dict:
    if not rows or window[0] not in rows:
        return {"symbol": sym, "total_return": None, "reason": f"no {sym} bar on {window[0]}; run tools/bars.py {sym}"}
    px = rows[window[0]]["open"] * (1 + slip)
    q = _floor(capital / px, qd)
    cash = capital - q * px
    curve, last = [], None
    for d in window:
        if d in rows:
            last = rows[d]["close"]
        curve.append((d, cash + q * last))
    return {"symbol": sym, "strategy": "buy and hold", **_metrics(curve, capital)}


def parse_grid(items: Sequence[str]) -> dict[str, list]:
    """["lookback_days=63,126,252", "top_n=2,3"] -> {"lookback_days": [63, 126, 252], ...}"""
    grid: dict[str, list] = {}
    for it in items:
        if "=" not in it:
            raise ValueError(f"sweep {it!r} is not key=v1,v2")
        k, vs = it.split("=", 1)
        vals = []
        for v in vs.split(","):
            v = v.strip()
            try:
                vals.append(int(v))
            except ValueError:
                try:
                    vals.append(float(v))
                except ValueError:
                    vals.append(v)
        grid[k.strip()] = vals
    return grid


SUMMARY_KEYS = ("total_return", "cagr", "max_drawdown", "turnover", "trades", "excess_return")


def sweep(spec: ThesisSpec, bars: Mapping[str, Sequence[dict]], limits: Mapping[str, Any],
          grid: Mapping[str, list], **kw) -> dict:
    """Backtest every combination of strategy parameters in `grid`. Always carries the
    overfitting warning when more than one combination ran."""
    if spec.strategy is None:
        raise ValueError("only a spec with a strategy block can be swept")
    keys = list(grid)
    runs = []
    base = spec.model_dump()
    for combo in itertools.product(*(grid[k] for k in keys)):
        params = dict(zip(keys, combo))
        s = ThesisSpec.model_validate({**base, "strategy": {**base["strategy"], **params}})
        r = run_backtest(s, bars, limits, **kw)
        runs.append({"params": params, "ok": r["ok"], **({k: r.get(k) for k in SUMMARY_KEYS} if r["ok"]
                                                          else {"reason": r.get("reason")})})
    good = [r for r in runs if r["ok"] and r["total_return"] is not None]
    best = max(good, key=lambda r: r["total_return"]) if good else None
    out = {"ok": bool(good), "thesis": spec.id, "swept": keys, "combinations": len(runs), "runs": runs,
           "best": best}
    if len(runs) > 1:
        out["overfitting_warning"] = OVERFIT_WARNING.format(n=len(runs))
    if not good:
        out["reason"] = "no combination could be backtested"
    return out
