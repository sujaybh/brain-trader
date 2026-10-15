"""Thesis spec: the structured rules a prose thesis is compiled into. Validated once at
creation; executed deterministically by the scheduler. See docs/thesis-spec.md."""
from __future__ import annotations

import re
from datetime import date
from typing import Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from pydantic import BaseModel, Field, field_validator, model_validator

from . import config

SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")


class Schedule(BaseModel):
    type: Literal["dca", "none"] = "dca"
    amount: float = Field(gt=0, description="USD per scheduled run, split across the universe")
    cron: str = "0 10 * * 1"
    tz: str = "America/New_York"

    @field_validator("cron")
    @classmethod
    def _cron(cls, v: str) -> str:
        if not croniter.is_valid(v):
            raise ValueError(f"invalid cron expression: {v!r}")
        return v

    @field_validator("tz")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError:
            raise ValueError(f"unknown timezone: {v!r}")
        return v


class Entry(BaseModel):
    type: Literal["schedule_only", "manual"] = "schedule_only"


class Exit(BaseModel):
    stop_loss_pct: Optional[float] = Field(default=None, gt=0, le=100)
    take_profit_pct: Optional[float] = Field(default=None, gt=0)
    review_only: bool = True  # True: stop/take-profit produce proposals, never automatic sells


class Trigger(BaseModel):
    type: Literal["drawdown_pct", "news", "price_below", "price_above"]
    value: Optional[float] = None
    symbol: Optional[str] = None  # for price_* triggers; None = any symbol in universe
    action: Literal["review", "pause", "propose_exit"] = "review"

    @model_validator(mode="after")
    def _needs_value(self):
        if self.type in ("drawdown_pct", "price_below", "price_above") and self.value is None:
            raise ValueError(f"trigger {self.type} needs a value")
        return self


class ThesisSpec(BaseModel):
    id: str
    name: str = Field(min_length=1, max_length=80)
    rationale: str = ""
    universe: list[str] = Field(min_length=1, max_length=25)
    capital_cap: float = Field(gt=0)
    max_position_pct: float = Field(default=100, gt=0, le=100)
    schedule: Schedule
    entry: Entry = Entry()
    exit: Exit = Exit()
    triggers: list[Trigger] = []
    mode: Literal["confirm", "auto"] = "confirm"
    review: Literal["weekly", "monthly", "quarterly"] = "monthly"
    status: Literal["active", "paused", "closed"] = "active"
    created: str = Field(default_factory=lambda: date.today().isoformat())
    source_text: str = ""

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not ID_RE.match(v):
            raise ValueError("id must be 2-40 chars of lowercase letters, digits, hyphens")
        return v

    @field_validator("universe")
    @classmethod
    def _universe(cls, v: list[str]) -> list[str]:
        out = []
        for s in v:
            s = s.strip().upper()
            if not SYMBOL_RE.match(s):
                raise ValueError(f"bad symbol: {s!r}")
            if s not in out:
                out.append(s)
        return out

    @model_validator(mode="after")
    def _consistency(self):
        if self.schedule.type == "dca" and self.schedule.amount > self.capital_cap:
            raise ValueError("schedule.amount exceeds capital_cap")
        if self.max_position_pct * len(self.universe) < 100 and self.schedule.type == "dca":
            # not fatal, but the cap can never be fully deployed; allow it (user may want that)
            pass
        return self

    def max_position_usd(self) -> float:
        return round(self.capital_cap * self.max_position_pct / 100, 2)


def manual_spec(cap: float) -> ThesisSpec:
    """The implicit thesis that quick trades from chat land in."""
    return ThesisSpec(
        id=config.MANUAL_THESIS_ID, name="Manual trades", rationale="Ad-hoc trades from chat.",
        universe=["SPY"], capital_cap=cap, schedule=Schedule(type="none", amount=1),
        entry=Entry(type="manual"), mode="confirm", review="monthly",
        source_text="(implicit) trades placed from chat with no thesis attached",
    )


_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _cron_english(cron: str, tz: str) -> str:
    """Best-effort English for the common cron shapes; falls back to the raw expression."""
    m, h, dom, mon, dow = cron.split()
    try:
        hh, mm = int(h), int(m)
    except ValueError:
        return f"on cron `{cron}` ({tz})"
    t = f"{(hh - 1) % 12 + 1}:{mm:02d}{'am' if hh < 12 else 'pm'}"
    if dom == "*" and mon == "*":
        if dow == "*":
            return f"every day at {t}"
        if dow == "1-5":
            return f"every weekday at {t}"
        days = []
        for part in dow.split(","):
            if part.isdigit():
                days.append(_WEEKDAYS[(int(part) - 1) % 7] if int(part) != 0 else "Sunday")
        if days:
            return f"every {' and '.join(days)} at {t}"
    if dow == "*" and mon == "*" and dom.isdigit():
        return f"on the {dom}{'st' if dom=='1' else 'th'} of every month at {t}"
    return f"on cron `{cron}` at {t} ({tz})"


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def describe(spec: ThesisSpec) -> str:
    """One plain-English paragraph the user confirms before thesis_create stores the spec."""
    names = "/".join(spec.universe)
    parts = []
    if spec.schedule.type == "dca":
        parts.append(f"{_cap(_cron_english(spec.schedule.cron, spec.schedule.tz))}, "
                     f"buy ${spec.schedule.amount:g} split across {names}")
    else:
        parts.append(f"No scheduled buying; trades in {names} are placed manually")
    parts.append(f"never more than ${spec.capital_cap:g} total")
    if spec.max_position_pct < 100:
        parts.append(f"never more than ${spec.max_position_usd():g} in one name")
    for t in spec.triggers:
        what = {"review": "pause and ask me", "pause": "pause buying", "propose_exit": "propose selling"}[t.action]
        if t.type == "drawdown_pct":
            parts.append(f"{what} if any position drops {t.value:g}%")
        elif t.type == "news":
            parts.append(f"{what} on a significant news flag")
        elif t.type == "price_below":
            parts.append(f"{what} if {t.symbol or 'any name'} falls below ${t.value:g}")
        elif t.type == "price_above":
            parts.append(f"{what} if {t.symbol or 'any name'} rises above ${t.value:g}")
    if spec.exit.stop_loss_pct:
        parts.append(f"{'propose an exit' if spec.exit.review_only else 'sell'} at a {spec.exit.stop_loss_pct:g}% loss")
    if spec.exit.take_profit_pct:
        parts.append(f"{'propose taking profit' if spec.exit.review_only else 'take profit'} at +{spec.exit.take_profit_pct:g}%")
    parts.append(f"{'ask before acting' if spec.mode == 'confirm' else 'act and tell me'}")
    parts.append(f"review {spec.review}")
    return "; ".join(parts) + "."
