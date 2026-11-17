"""Spec strategy block, strategies/ (signals, caps) and the backtester. Pure: no DB, no broker."""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from pydantic import ValidationError

import strategies
from broker.backends import FakeBackend
from conftest import TEST_LIMITS
from strategies import LookaheadError, MarketData, Signal, ThesisState
from strategies import momentum
from strategies.backtest import OVERFIT_WARNING, _metrics, parse_grid, run_backtest, sweep
from strategies.caps import apply_caps
from trader.schedule import dca_allocation
from trader.spec import ThesisSpec, describe

LIMITS = {**TEST_LIMITS, "global_cap": 1000}
UNI = ["NVDA", "AMD", "AVGO", "TSM"]
DCA = dict(id="semis", name="Semis", universe=UNI, capital_cap=300, max_position_pct=60,
           schedule={"type": "dca", "amount": 50, "cron": "0 10 * * 1"})
MOM = dict(id="mom", name="Momentum", universe=UNI, capital_cap=300, max_position_pct=50,
           schedule={"type": "none"}, strategy={"type": "momentum", "lookback_days": 20, "top_n": 2})


def weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def linear_bars(days: list[date], start: float, step: float) -> list[dict]:
    """open == close == start + i*step, so fills and returns are easy to check by hand."""
    return [{"date": d.isoformat(), "open": start + i * step, "high": start + i * step,
             "low": start + i * step, "close": start + i * step, "volume": 1.0, "vwap": None}
            for i, d in enumerate(days)]


@pytest.fixture(scope="module")
def fake_bars():
    return FakeBackend().daily_bars(UNI + ["SPY"], date(2024, 1, 1), date(2026, 10, 30))


# ------------------------------------------------------------------ spec

def test_v1_spec_is_unchanged():
    spec = ThesisSpec(**DCA)
    assert spec.strategy is None and spec.strategy_type() == "dca"
    assert "strategy" not in spec.model_dump() and "strategy" not in spec.model_dump_json()
    assert describe(spec) == ("Every Monday at 10:00am, buy $50 split across NVDA/AMD/AVGO/TSM; never more "
                              "than $300 total; never more than $180 in one name; ask before acting; review monthly.")
    none = ThesisSpec(**{**DCA, "schedule": {"type": "none", "amount": 1}})
    assert none.strategy_type() == "none"


def test_momentum_spec_validates_and_reads_back():
    spec = ThesisSpec(**MOM)
    assert spec.strategy_type() == "momentum" and spec.strategy.rebalance == "monthly"
    assert spec.schedule.amount == 1  # {"type": "none"} no longer needs a dummy amount
    text = describe(spec)
    assert "Once a month, rank NVDA/AMD/AVGO/TSM by their 20-trading-day return" in text
    assert "top 2 in equal weight (about $150 each)" in text and "propose every trade" in text
    again = ThesisSpec.model_validate_json(spec.model_dump_json())
    assert again == spec
    assert ThesisSpec(**{**DCA, "strategy": {"type": "dca"}}).strategy_type() == "dca"


@pytest.mark.parametrize("patch, msg", [
    ({"schedule": {"type": "dca", "amount": 50}}, "schedule.type none"),
    ({"strategy": {"type": "momentum", "top_n": 5}}, "larger than the universe"),
    ({"strategy": {"type": "momentum", "lookbak_days": 10}}, "Extra inputs"),
    ({"strategy": {"type": "momentum", "lookback_days": 2}}, "greater than or equal"),
    ({"strategy": {"type": "astrology"}}, "does not match any of the expected tags"),
    ({"entry": {"type": "manual"}}, "entry.type schedule_only"),
])
def test_bad_strategy_blocks_are_rejected(patch, msg):
    with pytest.raises(ValidationError, match=msg):
        ThesisSpec(**{**MOM, **patch})


def test_dca_strategy_needs_a_dca_schedule():
    with pytest.raises(ValidationError, match="strategy.type dca needs"):
        ThesisSpec(**{**DCA, "schedule": {"type": "none"}, "strategy": {"type": "dca"}})


# ------------------------------------------------------------------ types

def test_signal_validation():
    with pytest.raises(ValueError):
        Signal("NVDA", "buy")
    with pytest.raises(ValueError):
        Signal("NVDA", "buy", notional=5, qty=1)
    with pytest.raises(ValueError):
        Signal("NVDA", "short", notional=5)
    s = Signal("NVDA", "sell", qty=0.5, reason="r")
    assert s.action() == {"type": "order", "side": "sell", "symbol": "NVDA", "notional": None, "qty": 0.5}


def test_marketdata_refuses_lookahead_and_view_slices():
    days = weekdays(date(2026, 1, 5), 10)
    bars = {"NVDA": linear_bars(days, 100, 1)}
    with pytest.raises(LookaheadError):
        MarketData(asof=days[-1], bars=bars)
    v = MarketData.view(bars, days[5])
    assert v.last_date("NVDA") == days[4].isoformat() and v.price("NVDA") == 104
    assert MarketData.view(bars, days[5], prices={"NVDA": 200}).price("NVDA") == 200


def test_registry():
    assert {"dca", "momentum", "none"} <= set(strategies.REGISTRY)
    with pytest.raises(KeyError):
        strategies.get("astrology")


# ------------------------------------------------------------------ momentum

def _mom_data(asof_idx=30):
    days = weekdays(date(2026, 1, 5), 40)
    bars = {"NVDA": linear_bars(days, 100, 2.0), "AMD": linear_bars(days, 100, 1.0),
            "AVGO": linear_bars(days, 100, -1.0), "TSM": linear_bars(days, 100, 0.5)}
    return MarketData.view(bars, days[asof_idx]), days


def test_momentum_buys_top_n_equal_weight_and_is_deterministic():
    spec = ThesisSpec(**MOM)
    data, _ = _mom_data()
    sigs = strategies.signals(spec, data, ThesisState())
    assert [(s.symbol, s.side, s.notional) for s in sigs] == [("NVDA", "buy", 150.0), ("AMD", "buy", 150.0)]
    assert "#1 of 4" in sigs[0].reason and sigs[0].confidence == 1.0 and sigs[0].expires
    assert strategies.signals(spec, data, ThesisState()) == sigs
    # one decision per period
    key = momentum.period(spec, data.asof)
    assert strategies.signals(spec, data, ThesisState(last_period=key)) == []


def test_momentum_sells_dropouts_before_buying_and_respects_per_name_cap():
    spec = ThesisSpec(**{**MOM, "max_position_pct": 40})  # $120 per name < $150 equal weight
    data, _ = _mom_data()
    state = ThesisState(qty={"AVGO": 1.0, "NVDA": 0.5}, deployed={"AVGO": 100.0, "NVDA": 70.0})
    sigs = strategies.signals(spec, data, state)
    assert sigs[0].side == "sell" and sigs[0].symbol == "AVGO" and sigs[0].qty == 1.0
    buys = {s.symbol: s.notional for s in sigs if s.side == "buy"}
    nvda_value = 0.5 * data.price("NVDA")
    assert buys["AMD"] == 120.0  # per-name cap binds
    assert "NVDA" not in buys or buys["NVDA"] <= 120 - nvda_value + 0.01


def test_momentum_needs_history():
    spec = ThesisSpec(**MOM)
    data, _ = _mom_data(asof_idx=10)  # only 10 bars, lookback is 20
    assert strategies.signals(spec, data, ThesisState()) == []


# ------------------------------------------------------------------ dca adapter

def test_dca_adapter_reproduces_the_tick_allocation():
    spec = ThesisSpec(**DCA)
    monday, tuesday = date(2026, 10, 19), date(2026, 10, 20)
    dep = {"NVDA": 180.0}
    sigs = strategies.signals(spec, MarketData(asof=monday, bars={}), ThesisState(deployed=dep, last_period="2026-10-16"))
    assert {s.symbol: s.notional for s in sigs} == dca_allocation(spec, dep)
    assert all(s.reason == "dca slot 2026-10-19T10:00" for s in sigs)
    assert strategies.signals(spec, MarketData(asof=tuesday, bars={}), ThesisState(last_period="2026-10-19")) == []
    # a holiday Monday's slot is caught up on Tuesday, a week-old one is not
    assert strategies.signals(spec, MarketData(asof=tuesday, bars={}), ThesisState(last_period="2026-10-16"))
    assert not strategies.signals(spec, MarketData(asof=date(2026, 10, 26) + timedelta(days=3), bars={}),
                                  ThesisState(last_period="2026-10-20"))


# ------------------------------------------------------------------ caps

def test_caps_split_clip_and_drop():
    spec = ThesisSpec(**{**MOM, "capital_cap": 900, "max_position_pct": 100})
    prices = {"NVDA": 100.0, "AMD": 50.0}
    state = ThesisState(qty={"NVDA": 3.0}, deployed={"NVDA": 300.0}, other_deployed=500.0)
    sigs = [Signal("AMD", "buy", notional=450), Signal("NVDA", "sell", qty=5), Signal("AMD", "buy", notional=0.5)]
    orders, notes = apply_caps(sigs, spec, state, LIMITS, prices)
    sells = [o for o in orders if o.side == "sell"]
    assert sum(o.qty for o in sells) == 3.0 and len(sells) == 2   # clipped to held, split under $200
    assert all(o.qty * 100 * 1.003 <= 200 for o in sells)
    buys = [o for o in orders if o.side == "buy"]
    # after the sell frees ~$299, global room is 1000 - 500 - ~1 = ~499: the $450 buy fits, split in 3
    assert round(sum(o.notional for o in buys), 2) == 450 and all(o.notional <= 200 for o in buys)
    whys = {(n["action"], n["why"]) for n in notes}
    assert ("clipped", "thesis holds only 3") in whys and ("dropped", "below the minimum order size") in whys
    state = ThesisState(other_deployed=900.0)
    orders, notes = apply_caps([Signal("AMD", "buy", notional=150)], spec, state, LIMITS, prices)
    assert [o.notional for o in orders] == [100.0] and notes[0]["why"] == "global_cap"


# ------------------------------------------------------------------ backtest

def test_backtest_fills_at_next_open_with_slippage():
    days = weekdays(date(2026, 1, 5), 60)
    bars = {s: linear_bars(days, 100, step) for s, step in
            (("NVDA", 2.0), ("AMD", 1.0), ("AVGO", -1.0), ("TSM", 0.5), ("SPY", 0.2))}
    r = run_backtest(ThesisSpec(**MOM), bars, LIMITS)
    assert r["ok"] and r["start"] == days[21].isoformat()  # lookback 20 needs 21 closes
    first = r["trade_log"][0]
    i = [d.isoformat() for d in days].index(first["date"])
    assert first["signal_data_through"] == days[i - 1].isoformat()
    assert first["price"] == pytest.approx(bars[first["symbol"]][i]["open"] * 1.003)
    for t in r["trade_log"]:
        assert t["signal_data_through"] < t["date"]
    bench = r["benchmark"]
    spy0, spy1 = bars["SPY"][21]["open"] * 1.003, bars["SPY"][-1]["close"]
    q = int(300 / spy0 * 10000) / 10000
    assert bench["total_return"] == pytest.approx((300 - q * spy0 + q * spy1) / 300 - 1, abs=1e-6)
    assert "overfitting_warning" not in r


def test_backtest_has_no_lookahead(fake_bars):
    """Scrambling every bar after a cutoff must not change any trade decided on or before it."""
    spec = ThesisSpec(**{**MOM, "strategy": {"type": "momentum", "lookback_days": 63, "top_n": 2}})
    base = run_backtest(spec, fake_bars, LIMITS, start="2025-01-02")
    cutoff = "2025-09-15"
    scrambled = {s: [r if r["date"] <= cutoff else {**r, "open": r["open"] * (3 if i % 2 else 0.3),
                                                    "close": r["close"] * (0.2 if i % 3 else 4)}
                     for i, r in enumerate(rows)] for s, rows in fake_bars.items()}
    other = run_backtest(spec, scrambled, LIMITS, start="2025-01-02")
    keep = lambda res: [t for t in res["trade_log"] if t["date"] <= cutoff]  # noqa: E731
    assert keep(base) and keep(base) == keep(other)
    assert [t for t in base["trade_log"] if t["date"] > cutoff] != [t for t in other["trade_log"] if t["date"] > cutoff]


def test_backtest_dca_replay_respects_caps(fake_bars):
    r = run_backtest(ThesisSpec(**DCA), fake_bars, LIMITS, start="2025-01-01", end="2025-12-31")
    assert r["ok"] and r["buys"] == 24 and r["sells"] == 0   # $300 cap / $50 a week = 6 Mondays x 4 names
    spent = sum(t["notional"] for t in r["trade_log"])
    assert spent <= 300 + 1e-6 and r["final_cash"] == pytest.approx(300 - spent, abs=0.05)
    assert all(t["notional"] <= 180 for t in r["trade_log"])


def test_backtest_reports_and_refuses_cleanly(fake_bars):
    r = run_backtest(ThesisSpec(**MOM), {"NVDA": fake_bars["NVDA"][:5]}, LIMITS)
    assert not r["ok"] and "not enough bars" in r["reason"]
    spec = ThesisSpec(**MOM)
    r = run_backtest(spec, {s: v for s, v in fake_bars.items() if s != "SPY"}, LIMITS, start="2025-06-02")
    assert r["ok"] and r["benchmark"]["total_return"] is None and "excess_return" not in r
    for k in ("total_return", "cagr", "max_drawdown", "turnover", "trades"):
        assert k in r


def test_sweep_carries_overfitting_warning(fake_bars):
    grid = parse_grid(["lookback_days=21,63", "top_n=1,2"])
    assert grid == {"lookback_days": [21, 63], "top_n": [1, 2]}
    s = sweep(ThesisSpec(**MOM), fake_bars, LIMITS, grid, start="2025-06-02")
    assert s["ok"] and s["combinations"] == 4 and len(s["runs"]) == 4
    assert s["overfitting_warning"] == OVERFIT_WARNING.format(n=4)
    assert s["best"]["total_return"] == max(r["total_return"] for r in s["runs"])
    with pytest.raises(ValueError):
        sweep(ThesisSpec(**DCA), fake_bars, LIMITS, grid)


def test_metrics_drawdown_and_cagr():
    m = _metrics([("2025-01-01", 100), ("2025-07-01", 150), ("2025-10-01", 75), ("2026-01-01", 121)], 100)
    assert m["max_drawdown"] == pytest.approx(-0.5)
    assert m["total_return"] == pytest.approx(0.21) and m["cagr"] == pytest.approx(0.21, abs=0.002)
