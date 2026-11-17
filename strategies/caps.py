"""The money limits, applied to signals before they become proposals (live) or fills
(backtest). Mirrors the broker's chain in broker/limits.py for the size checks, so a backtest
cannot buy what the broker would refuse and the live runner does not propose orders the broker
is certain to refuse. The broker still re-checks everything at execution time.

    sells: never more than the thesis holds (no shorts)
    buys:  thesis capital_cap, per-name cap (capital_cap * max_position_pct), global_cap
    both:  min_order_notional; orders above per_order_cap are split into chunks
"""
from __future__ import annotations

import math
from dataclasses import replace
from typing import Mapping

from trader.spec import ThesisSpec

from .base import Signal, ThesisState

EPS = 1e-6


def _floor(x: float, nd: int) -> float:
    f = 10 ** nd
    return math.floor(x * f + 1e-9) / f


def _chunks(total: float, n: int, nd: int) -> list[float]:
    each = _floor(total / n, nd)
    parts = [each] * (n - 1)
    parts.append(round(total - each * (n - 1), nd))
    return [p for p in parts if p > 0]


def apply_caps(signals: list[Signal], spec: ThesisSpec, state: ThesisState, limits: Mapping,
               prices: Mapping[str, float]) -> tuple[list[Signal], list[dict]]:
    """Return (orders, adjustments). Sells are processed before buys and reduce `deployed` by
    their estimated proceeds, the way the broker's attribution will once they fill.
    `adjustments` lists every signal that was clipped, split or dropped, with the reason."""
    slip = float(limits["limit_slippage_pct"]) / 100
    per_order = float(limits["per_order_cap"])
    gcap = float(limits["global_cap"])
    min_n = float(limits.get("min_order_notional", 1.0))
    qd = int(limits.get("qty_decimals", 4))
    qty = dict(state.qty)
    dep = dict(state.deployed)
    out: list[Signal] = []
    notes: list[dict] = []

    def total() -> float:
        return sum(max(v, 0.0) for v in dep.values())

    ordered = [s for s in signals if s.side == "sell"] + [s for s in signals if s.side == "buy"]
    for sig in ordered:
        px = prices.get(sig.symbol)
        if not px or px <= 0:
            notes.append({"signal": sig.as_dict(), "action": "dropped", "why": "no price"})
            continue
        if sig.side == "sell":
            held = max(qty.get(sig.symbol, 0.0), 0.0)
            want = sig.qty if sig.qty is not None else sig.notional / (px * (1 - slip))
            q = _floor(min(want, held), qd)
            if q < want - 10 ** -qd:
                notes.append({"signal": sig.as_dict(), "action": "clipped", "why": f"thesis holds only {held:g}"})
            if q <= 0 or q * px * (1 - slip) < min_n:
                notes.append({"signal": sig.as_dict(), "action": "dropped", "why": "below the minimum order size"})
                continue
            value_hi = q * px * (1 + slip)  # the broker prices per_order_cap on the limit; be conservative
            n = max(1, math.ceil(value_hi / per_order - EPS))
            parts = _chunks(q, n, qd)
            if len(parts) > 1:
                notes.append({"signal": sig.as_dict(), "action": "split", "why": f"per_order_cap ${per_order:g}",
                              "parts": len(parts)})
            for part in parts:
                out.append(replace(sig, qty=part, notional=None))
            qty[sig.symbol] = held - q
            dep[sig.symbol] = dep.get(sig.symbol, 0.0) - q * px * (1 - slip)
            continue
        want = sig.notional if sig.notional is not None else sig.qty * px * (1 + slip)
        limits_left = {
            "capital_cap": spec.capital_cap - total(),
            "per-name cap": spec.max_position_usd() - max(dep.get(sig.symbol, 0.0), 0.0),
            "global_cap": gcap - state.other_deployed - total(),
        }
        amt = _floor(max(min(want, *limits_left.values()), 0.0), 2)
        if amt < want - 0.005:
            binding = min(limits_left, key=limits_left.get)
            notes.append({"signal": sig.as_dict(), "action": "clipped", "why": binding, "to": amt})
        if amt < min_n:
            notes.append({"signal": sig.as_dict(), "action": "dropped", "why": "below the minimum order size"})
            continue
        n = max(1, math.ceil(amt / per_order - EPS))
        parts = _chunks(amt, n, 2)
        if len(parts) > 1:
            notes.append({"signal": sig.as_dict(), "action": "split", "why": f"per_order_cap ${per_order:g}",
                          "parts": len(parts)})
        for part in parts:
            out.append(replace(sig, notional=part, qty=None))
        dep[sig.symbol] = dep.get(sig.symbol, 0.0) + amt
    return out, notes
