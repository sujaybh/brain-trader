"""DCA as a strategy, so the backtester can replay v1 theses. Live DCA is still executed by
tools/run_thesis.py (the tick); this adapter reuses the same cron math and the same
`dca_allocation()` so a replay buys what the tick would have bought.

Daily-bar approximation: a slot dated D is decided before D's open and fills at D's open
(live: 10:00 on D). A slot that falls on a non-trading day fills on the next trading day if
that is within CATCHUP_DAYS (live: `catchup_hours`, 48h by default); older slots expire."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from trader.schedule import dca_allocation, due_slots, slot_key
from trader.spec import ThesisSpec

from .base import MarketData, Signal, ThesisState, register

CATCHUP_DAYS = 2


def period(spec: ThesisSpec, asof: date) -> str:
    return asof.isoformat()


def warmup(spec: ThesisSpec) -> int:
    return 0


def slots_for(spec: ThesisSpec, asof: date, last: date | None) -> list[datetime]:
    """Cron occurrences dated in (last, asof], no older than CATCHUP_DAYS before asof."""
    if spec.schedule.type != "dca":
        return []
    if last is None:  # first decision: only today's slots
        since = asof - timedelta(days=1)
    else:
        since = max(last, asof - timedelta(days=CATCHUP_DAYS + 1))
    tz = ZoneInfo(spec.schedule.tz)
    end = datetime.combine(asof, time(23, 59, 59), tz)
    hours = (asof - since).days * 24
    return [s for s in due_slots(spec, end, hours) if since < s.date() <= asof]


def signals(spec: ThesisSpec, data: MarketData, state: ThesisState) -> list[Signal]:
    key = period(spec, data.asof)
    if state.last_period == key:
        return []
    last = date.fromisoformat(state.last_period) if state.last_period else None
    dep = dict(state.deployed)
    out: list[Signal] = []
    for slot in slots_for(spec, data.asof, last):
        k = slot_key(slot)
        for sym, usd in dca_allocation(spec, dep).items():
            out.append(Signal(sym, "buy", notional=usd, reason=f"dca slot {k}",
                              expires=(slot.date() + timedelta(days=CATCHUP_DAYS)).isoformat()))
            dep[sym] = round(dep.get(sym, 0.0) + usd, 2)
    return out


register("dca", signals=signals, period=period, warmup=warmup)
register("none", signals=lambda spec, data, state: [], period=period, warmup=warmup)
