"""Tests for the shared, I/O-free foundation: spec validation, describe(), due slots, DCA
allocation, order ids."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from trader.schedule import client_order_id, dca_allocation, due_slots, slot_key
from trader.spec import ThesisSpec, describe, manual_spec

NY = ZoneInfo("America/New_York")


def semis(**over):
    base = dict(
        id="semis-q4", name="Big semiconductors", universe=["NVDA", "AMD", "AVGO", "TSM"],
        capital_cap=300, max_position_pct=60,
        schedule={"type": "dca", "amount": 50, "cron": "0 10 * * 1", "tz": "America/New_York"},
        exit={"stop_loss_pct": 20, "review_only": True},
        triggers=[{"type": "drawdown_pct", "value": 15, "action": "review"}],
        source_text="invest in big semis",
    )
    base.update(over)
    return ThesisSpec(**base)


def test_spec_normalizes_symbols_and_dedups():
    s = semis(universe=[" nvda", "NVDA", "amd"])
    assert s.universe == ["NVDA", "AMD"]


@pytest.mark.parametrize("bad", [
    dict(id="Bad_ID"), dict(universe=[]), dict(universe=["N@"]), dict(capital_cap=0),
    dict(max_position_pct=0), dict(max_position_pct=101),
    dict(schedule={"type": "dca", "amount": 500, "cron": "0 10 * * 1"}),   # amount > cap
    dict(schedule={"type": "dca", "amount": 5, "cron": "not a cron"}),
    dict(schedule={"type": "dca", "amount": 5, "cron": "0 10 * * 1", "tz": "Mars/Olympus"}),
    dict(triggers=[{"type": "drawdown_pct", "action": "review"}]),           # missing value
    dict(mode="yolo"),
])
def test_spec_rejects(bad):
    with pytest.raises(ValidationError):
        semis(**bad)


def test_describe_is_one_paragraph_with_all_rules():
    text = describe(semis())
    assert text.startswith("Every Monday at 10:00am, buy $50 split across NVDA/AMD/AVGO/TSM")
    for frag in ["$300 total", "$180 in one name", "drops 15%", "20% loss", "ask before acting", "review monthly"]:
        assert frag in text
    assert "\n" not in text


def test_manual_spec_is_valid():
    m = manual_spec(200)
    assert m.id == "manual" and m.schedule.type == "none"
    assert "No scheduled buying" in describe(m)


def test_due_slots_within_lookback_only():
    s = semis()
    now = datetime(2026, 10, 20, 9, 0, tzinfo=NY)             # Tuesday 9am
    assert [slot_key(d) for d in due_slots(s, now, 48)] == ["2026-10-19T10:00"]
    assert due_slots(s, now, 20) == []                         # Monday 10am is 23h ago
    now_sun = datetime(2026, 10, 18, 12, 0, tzinfo=NY)
    assert due_slots(s, now_sun, 48) == []
    assert due_slots(semis(schedule={"type": "none", "amount": 1}), now, 48) == []


def test_due_slots_respects_thesis_tz_not_caller_tz():
    s = semis()
    now_utc = datetime(2026, 10, 19, 14, 5, tzinfo=ZoneInfo("UTC"))  # 10:05 NY
    assert [slot_key(d) for d in due_slots(s, now_utc, 1)] == ["2026-10-19T10:00"]


def test_client_order_id_deterministic_and_distinct():
    a = client_order_id("semis-q4", "NVDA", "2026-10-19T10:00")
    assert a == client_order_id("semis-q4", "NVDA", "2026-10-19T10:00")
    assert a != client_order_id("semis-q4", "AMD", "2026-10-19T10:00")
    assert a != client_order_id("semis-q4", "NVDA", "2026-10-26T10:00")
    assert a != client_order_id("semis-q4", "NVDA", "2026-10-19T10:00", side="sell")
    assert len(a) <= 128 and ":" not in a


def test_dca_equal_split():
    assert dca_allocation(semis(), {}) == {"NVDA": 12.5, "AMD": 12.5, "AVGO": 12.5, "TSM": 12.5}


def test_dca_skips_capped_name_and_redistributes():
    alloc = dca_allocation(semis(), {"NVDA": 180})
    assert "NVDA" not in alloc
    assert set(alloc) == {"AMD", "AVGO", "TSM"}
    assert abs(sum(alloc.values()) - 50) < 0.05


def test_dca_clips_to_remaining_capital():
    alloc = dca_allocation(semis(), {"NVDA": 180, "AMD": 90, "AVGO": 10})  # 280 deployed, 20 room
    assert abs(sum(alloc.values()) - 20) < 0.05
    assert dca_allocation(semis(), {"NVDA": 180, "AMD": 120}) == {}            # fully deployed


def test_dca_partial_room_in_one_name_waterfills():
    alloc = dca_allocation(semis(), {"NVDA": 175, "AMD": 175, "AVGO": 175})   # 525 > cap? no: cap 300
    # deployed 525 exceeds cap, so nothing
    assert alloc == {}
    alloc = dca_allocation(semis(capital_cap=1000, max_position_pct=20), {"NVDA": 195})  # 5 room in NVDA
    assert alloc["NVDA"] == 5.0 and abs(sum(alloc.values()) - 50) < 0.05


def test_dca_min_notional():
    assert dca_allocation(semis(schedule={"type": "dca", "amount": 2, "cron": "0 10 * * 1"}), {}) == {}
