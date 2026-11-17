"""Core types every strategy shares: Signal, MarketData, ThesisState, and the registry.

A strategy is a module with three pure functions:

    signals(spec, data, state) -> list[Signal]   what to trade now (no I/O, deterministic)
    period(spec, asof) -> str                    rebalance-period key; one decision per period
    warmup(spec) -> int                          daily bars of history needed before deciding

The caller (tools/run_strategy.py live, strategies/backtest.py offline) builds `data` and
`state`, calls signals(), records `period()` as done, and pushes the result through
strategies.caps.apply_caps() so both apply exactly the same limits.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Literal, Mapping, Optional, Sequence

from trader.spec import ThesisSpec


class LookaheadError(ValueError):
    """MarketData was handed a bar dated on or after its decision day."""


@dataclass(frozen=True)
class Signal:
    symbol: str
    side: Literal["buy", "sell"]
    notional: Optional[float] = None   # USD (buys)
    qty: Optional[float] = None        # shares (sells)
    reason: str = ""
    confidence: float = 1.0            # 0..1, informational
    expires: Optional[str] = None      # ISO date/datetime after which the signal is stale

    def __post_init__(self):
        if self.side not in ("buy", "sell"):
            raise ValueError(f"signal side must be buy or sell, not {self.side!r}")
        if (self.notional is None) == (self.qty is None):
            raise ValueError("signal needs exactly one of notional or qty")
        size = self.notional if self.notional is not None else self.qty
        if not size > 0:
            raise ValueError("signal size must be > 0")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be in [0, 1]")

    def action(self) -> dict:
        """The proposal action this signal becomes (web/proposals.py format)."""
        return {"type": "order", "side": self.side, "symbol": self.symbol,
                "notional": self.notional, "qty": self.qty}

    def as_dict(self) -> dict:
        return {"symbol": self.symbol, "side": self.side, "notional": self.notional, "qty": self.qty,
                "reason": self.reason, "confidence": self.confidence, "expires": self.expires}


@dataclass(frozen=True)
class MarketData:
    """What a strategy may see when deciding on `asof` (a trading day, decided before its open):
    daily bars dated strictly before `asof`, plus optional current prices for sizing (live:
    broker quotes; backtest: none, so the last close is used). Construction refuses any bar on
    or after `asof`, so a strategy cannot peek at the day it trades on."""
    asof: date
    bars: Mapping[str, Sequence[dict]]
    prices: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self):
        cutoff = self.asof.isoformat()
        for sym, rows in self.bars.items():  # rows are sorted by date (load_bars / view guarantee it)
            if rows and rows[-1]["date"] >= cutoff:
                raise LookaheadError(f"{sym} bar dated {rows[-1]['date']} is not before decision day {cutoff}")

    @classmethod
    def view(cls, all_bars: Mapping[str, Sequence[dict]], asof: date,
             prices: Optional[Mapping[str, float]] = None,
             dates: Optional[Mapping[str, Sequence[str]]] = None) -> "MarketData":
        """Slice sorted per-symbol bars to those before `asof`. `dates` (symbol -> sorted date
        strings) can be passed to avoid recomputing them on every call."""
        cutoff = asof.isoformat()
        out = {}
        for sym, rows in all_bars.items():
            ds = dates[sym] if dates is not None else [r["date"] for r in rows]
            out[sym] = rows[:bisect.bisect_left(ds, cutoff)]
        return cls(asof=asof, bars=out, prices=dict(prices or {}))

    def closes(self, symbol: str) -> list[float]:
        return [float(r["close"]) for r in self.bars.get(symbol, ())]

    def last_date(self, symbol: str) -> Optional[str]:
        rows = self.bars.get(symbol) or ()
        return rows[-1]["date"] if rows else None

    def price(self, symbol: str) -> Optional[float]:
        p = self.prices.get(symbol)
        if p:
            return float(p)
        rows = self.bars.get(symbol) or ()
        return float(rows[-1]["close"]) if rows else None


@dataclass
class ThesisState:
    qty: dict[str, float] = field(default_factory=dict)        # shares this thesis holds
    deployed: dict[str, float] = field(default_factory=dict)   # net notional per name (buys - sells), as the broker counts it
    last_period: Optional[str] = None                          # period() key of the last decision taken
    other_deployed: float = 0.0                                # deployed by all other theses (global cap)

    def total_deployed(self) -> float:
        return sum(max(v, 0.0) for v in self.deployed.values())


@dataclass(frozen=True)
class StrategyImpl:
    type: str
    signals: Callable[[ThesisSpec, MarketData, ThesisState], list[Signal]]
    period: Callable[[ThesisSpec, date], str]
    warmup: Callable[[ThesisSpec], int]


REGISTRY: dict[str, StrategyImpl] = {}


def register(type_: str, *, signals, period, warmup) -> StrategyImpl:
    impl = StrategyImpl(type_, signals, period, warmup)
    REGISTRY[type_] = impl
    return impl


def get(type_: str) -> StrategyImpl:
    try:
        return REGISTRY[type_]
    except KeyError:
        raise KeyError(f"no strategy registered for type {type_!r} (have: {', '.join(sorted(REGISTRY))})")


def for_spec(spec: ThesisSpec) -> StrategyImpl:
    return get(spec.strategy_type())
