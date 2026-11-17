"""Cross-sectional momentum: once per rebalance period, rank the universe by its
`lookback_days` (trading days) close-to-close return, hold the `top_n` best in equal weight,
sell names that dropped out. Two parameters that matter (lookback, top_n), both with a
published rationale (Jegadeesh & Titman 1993; 6-12 month lookbacks are the standard).

Sizing: target per name = min(capital_cap / top_n, the spec's per-name cap), valued at the
current price. Held names drifting more than BAND from target are trimmed / topped up; within
the band they are left alone so a rebalance does not churn pennies. Buys are sized as if the
period's sells have already happened (approve the sells first in confirm mode); the caps in
strategies/caps.py and the broker still apply to every order."""
from __future__ import annotations

import math
from datetime import date, timedelta

from trader.spec import ThesisSpec

from .base import MarketData, Signal, ThesisState, register

BAND = 0.10       # leave a held winner alone within +/-10% of its target value
MIN_TRADE = 1.0   # USD; Alpaca's minimum notional


def period(spec: ThesisSpec, asof: date) -> str:
    if spec.strategy.rebalance == "weekly":
        y, w, _ = asof.isocalendar()
        return f"{y}-W{w:02d}"
    return asof.strftime("%Y-%m")


def warmup(spec: ThesisSpec) -> int:
    return spec.strategy.lookback_days + 1


def ranking(spec: ThesisSpec, data: MarketData) -> list[tuple[str, float]]:
    """(symbol, lookback return), best first; names without enough history are left out.
    Ties break on the symbol so the order is deterministic."""
    lb = spec.strategy.lookback_days
    scores = []
    for s in spec.universe:
        c = data.closes(s)
        if len(c) > lb and c[-1 - lb] > 0:
            scores.append((s, c[-1] / c[-1 - lb] - 1))
    return sorted(scores, key=lambda x: (-x[1], x[0]))


def _floor(x: float, nd: int) -> float:
    f = 10 ** nd
    return math.floor(x * f + 1e-9) / f


def signals(spec: ThesisSpec, data: MarketData, state: ThesisState) -> list[Signal]:
    st = spec.strategy
    key = period(spec, data.asof)
    if state.last_period == key:
        return []
    ranked = ranking(spec, data)
    if not ranked:
        return []
    rank = {s: i + 1 for i, (s, _) in enumerate(ranked)}
    ret = dict(ranked)
    picks = [s for s, _ in ranked[:st.top_n]]
    target = min(spec.capital_cap / st.top_n, spec.max_position_usd())
    expires = (data.asof + timedelta(days=1 if st.rebalance == "weekly" else 3)).isoformat()
    lb = st.lookback_days

    def why(s: str) -> str:
        r = f"{ret[s]:+.1%} over {lb}d, #{rank[s]} of {len(ranked)}" if s in ret else "no ranking (not enough bars)"
        return f"momentum {key}: {s} {r}"

    def conf(s: str) -> float:
        return round(1 - (rank.get(s, len(ranked)) - 1) / max(len(ranked), 1), 3)

    sells: list[Signal] = []
    buys: list[Signal] = []
    dep = dict(state.deployed)
    value: dict[str, float] = {}
    for s in sorted(state.qty):
        q = state.qty[s]
        px = data.price(s)
        if q <= 1e-9 or px is None:
            continue
        value[s] = q * px
        if s not in picks:
            sells.append(Signal(s, "sell", qty=q, reason=f"{why(s)}; out of the top {st.top_n}, sell all",
                                confidence=conf(s), expires=expires))
            dep[s] = dep.get(s, 0.0) - q * px
            value[s] = 0.0
        elif value[s] > target * (1 + BAND):
            q_sell = _floor((value[s] - target) / px, 4)
            if q_sell * px >= MIN_TRADE:
                sells.append(Signal(s, "sell", qty=q_sell, confidence=conf(s), expires=expires,
                                    reason=f"{why(s)}; ${value[s]:.2f} is above the ${target:.2f} target, trim"))
                dep[s] = dep.get(s, 0.0) - q_sell * px
                value[s] -= q_sell * px

    room = spec.capital_cap - sum(max(v, 0.0) for v in dep.values())
    for s in picks:
        if data.price(s) is None:
            continue
        have = value.get(s, 0.0)
        if have >= target * (1 - BAND):
            continue
        amt = min(target - have, spec.max_position_usd() - max(dep.get(s, 0.0), 0.0), room)
        amt = _floor(amt, 2)
        if amt < MIN_TRADE:
            continue
        buys.append(Signal(s, "buy", notional=amt, confidence=conf(s), expires=expires,
                           reason=f"{why(s)}; in the top {st.top_n}, bring to ${target:.2f}"))
        room -= amt
        dep[s] = dep.get(s, 0.0) + amt
    return sells + buys


register("momentum", signals=signals, period=period, warmup=warmup)
