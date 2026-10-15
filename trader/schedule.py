"""Deterministic scheduling math: which slots are due, idempotent order ids, DCA allocation.
No I/O here so it is trivially testable."""
from __future__ import annotations

import hashlib
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import croniter

from .spec import ThesisSpec


def slot_key(dt: datetime) -> str:
    """Slot identity: the cron occurrence time in the thesis timezone, minute precision."""
    return dt.strftime("%Y-%m-%dT%H:%M")


def due_slots(spec: ThesisSpec, now: datetime, lookback_hours: float) -> list[datetime]:
    """Cron occurrences in (now - lookback, now], in the thesis timezone, oldest first.
    The caller filters out slots that already have orders (idempotency) and decides whether
    a missed slot is still worth executing."""
    if spec.schedule.type != "dca":
        return []
    tz = ZoneInfo(spec.schedule.tz)
    now_tz = now.astimezone(tz)
    start = now_tz - timedelta(hours=lookback_hours)
    it = croniter(spec.schedule.cron, start, ret_type=datetime)
    out: list[datetime] = []
    while True:
        nxt = it.get_next(datetime)
        if nxt > now_tz:
            break
        out.append(nxt)
        if len(out) > 1000:  # pathological cron; refuse to loop forever
            break
    return out


def client_order_id(thesis_id: str, symbol: str, slot: str, side: str = "buy") -> str:
    """Deterministic, unique per (thesis, slot, symbol, side). Alpaca allows <=128 chars; we
    keep it short and readable, with a hash tail guarding against truncation collisions."""
    raw = f"{thesis_id}:{slot}:{symbol}:{side}"
    tail = hashlib.sha1(raw.encode()).hexdigest()[:6]
    base = f"{thesis_id}-{slot.replace(':', '')}-{symbol}-{side[0]}"
    return f"{base[:60]}-{tail}"


def manual_order_id(thesis_id: str, symbol: str, side: str, now: datetime) -> str:
    return client_order_id(thesis_id, symbol, now.strftime("%Y-%m-%dT%H:%M:%S"), side)


def dca_allocation(spec: ThesisSpec, deployed: dict[str, float], amount: float | None = None,
                   min_notional: float = 1.0) -> dict[str, float]:
    """Split `amount` equally across the universe, skipping names already at their position
    cap and never exceeding the thesis capital cap. Deterministic. Returns symbol -> USD.

    `deployed` is net filled notional per symbol attributed to this thesis."""
    amount = spec.schedule.amount if amount is None else amount
    total_deployed = sum(max(v, 0.0) for v in deployed.values())
    room_total = max(spec.capital_cap - total_deployed, 0.0)
    budget = min(amount, room_total)
    if budget < min_notional:
        return {}
    per_name_cap = spec.max_position_usd()
    room = {s: max(per_name_cap - max(deployed.get(s, 0.0), 0.0), 0.0) for s in spec.universe}
    eligible = [s for s in spec.universe if room[s] >= min_notional]
    alloc: dict[str, float] = {}
    remaining = budget
    # water-fill: equal split, redistribute leftovers from capped names
    while eligible and remaining >= min_notional:
        share = remaining / len(eligible)
        progressed = False
        for s in list(eligible):
            take = min(share, room[s])
            take = math.floor(take * 100) / 100
            if take >= min_notional:
                alloc[s] = round(alloc.get(s, 0.0) + take, 2)
                room[s] = round(room[s] - take, 2)
                remaining = round(remaining - take, 2)
                progressed = True
            if room[s] < min_notional:
                eligible.remove(s)
        if not progressed:
            break
    return {s: v for s, v in alloc.items() if v >= min_notional}


def is_market_day(d: datetime) -> bool:
    return d.weekday() < 5
