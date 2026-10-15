"""Unit tests for the pure limit chain in broker/limits.py."""
from __future__ import annotations

import pytest

from broker.limits import CHAIN, LazyContext, OrderReq, floor_to, limit_price, run_chain, sizing
from conftest import TEST_LIMITS


def base_ctx(**over) -> dict:
    """A context in which a $50 manual NVDA buy passes every check."""
    ctx = {
        "halted": False,
        "clock": {"is_open": True},
        "assets": {"NVDA": {"tradable": True, "fractionable": True, "asset_class": "us_equity"}},
        "quotes": {"NVDA": {"bid": 100.0, "ask": 100.0, "last": 100.0}},
        "account": {"cash": 1000.0, "daytrade_count": 0},
        "positions": [{"symbol": "NVDA", "qty": 2.0}],
        "held_qty": {"manual": {"NVDA": 2.0}},
        "deployed": {"manual": {"NVDA": 0.0}},
        "pending_buys": 0.0,
        "thesis": {"id": "manual", "capital_cap": 200.0, "max_position_usd": 200.0},
        "pdt": {"logged": 0, "bought_today": set(), "sold_today": set()},
    }
    ctx.update(over)
    return ctx


def buy(notional=50.0, **kw) -> OrderReq:
    return OrderReq(**{"thesis_id": "manual", "side": "buy", "symbol": "NVDA", "notional": notional, **kw})


def sell(qty=1.0, **kw) -> OrderReq:
    return OrderReq(**{"thesis_id": "manual", "side": "sell", "symbol": "NVDA", "qty": qty, **kw})


def check(req, ctx, limits=None):
    r = run_chain(req, ctx, limits or dict(TEST_LIMITS))
    return r[0] if r else None


def test_chain_order_is_the_contract_order():
    assert [n for n, _ in CHAIN] == ["halt", "market_hours", "asset", "side", "order_type", "per_order_cap",
                                     "thesis_cap", "position_cap", "global_cap", "cash", "pdt"]


def test_happy_path_passes():
    assert check(buy(), base_ctx()) is None
    assert check(sell(), base_ctx()) is None


# ------------------------------------------------------------------ sizing

def test_marketable_limit_prices():
    q = {"bid": 183.10, "ask": 183.20}
    assert limit_price("buy", q, 0.3) == 183.75   # 183.20 * 1.003 = 183.7496
    assert limit_price("sell", q, 0.3) == 182.55  # 183.10 * 0.997 = 182.5507
    assert limit_price("buy", {"bid": 1, "ask": 0}, 0.3) is None


def test_fractional_qty_floors():
    assert floor_to(50 / 183.75, 4) == 0.2721
    assert floor_to(0.29999999, 4) == 0.2999
    ctx = base_ctx(quotes={"NVDA": {"bid": 183.10, "ask": 183.20}}, limits=dict(TEST_LIMITS))
    s = sizing(buy(50), ctx)
    assert (s.limit_price, s.qty, s.notional, s.fractional) == (183.75, 0.2721, 50.0, True)
    s = sizing(buy(None, qty=2), ctx)
    assert (s.qty, s.notional, s.fractional) == (2.0, 367.5, False)


# ------------------------------------------------------------------ each check

def test_halt():
    assert check(buy(), base_ctx(halted=True)) == "halt"


def test_market_hours():
    assert check(buy(), base_ctx(clock={"is_open": False})) == "market_hours"


@pytest.mark.parametrize("asset", [
    None,
    {"tradable": True, "fractionable": True, "asset_class": "crypto"},
    {"tradable": False, "fractionable": True, "asset_class": "us_equity"},
    {"tradable": True, "fractionable": False, "asset_class": "us_equity"},  # notional => fractional
])
def test_asset(asset):
    assert check(buy(), base_ctx(assets={"NVDA": asset})) == "asset"


def test_asset_whole_shares_ok_when_not_fractionable():
    ctx = base_ctx(assets={"NVDA": {"tradable": True, "fractionable": False, "asset_class": "us_equity"}})
    assert check(buy(None, qty=1), ctx) is None


def test_side_rejects_unknown_side():
    assert check(OrderReq("manual", "short", "NVDA", qty=1), base_ctx()) == "side"


def test_side_no_short_beyond_thesis_holding():
    assert check(sell(3), base_ctx()) == "side"
    # thesis holds 2 but the account only has 1 (e.g. sold elsewhere): still capped
    assert check(sell(1.5), base_ctx(positions=[{"symbol": "NVDA", "qty": 1.0}])) == "side"
    assert check(sell(1), base_ctx(held_qty={})) == "side"


def test_order_type_needs_quote_and_nonzero_qty():
    assert check(buy(), base_ctx(quotes={"NVDA": None})) == "order_type"
    assert check(buy(0.5), base_ctx()) == "order_type"  # below $1 minimum / rounds tiny
    assert check(buy(0.001), base_ctx()) == "order_type"  # rounds to 0 shares


def test_per_order_cap():
    assert check(buy(201), base_ctx()) == "per_order_cap"
    # qty orders are capped on qty * limit: 3 * 100.30 = 300.90 > 200
    assert check(buy(None, qty=3), base_ctx()) == "per_order_cap"
    assert check(buy(None, qty=1), base_ctx()) is None


def test_thesis_cap_and_unknown_thesis():
    assert check(buy(), base_ctx(thesis=None)) == "thesis_cap"
    assert check(sell(), base_ctx(thesis=None)) == "thesis_cap"
    ctx = base_ctx(deployed={"manual": {"AMD": 160.0}})
    assert check(buy(50), ctx) == "thesis_cap"
    assert check(buy(40), ctx) is None
    assert check(sell(), ctx) is None  # sells reduce deployment


def test_position_cap():
    ctx = base_ctx(thesis={"id": "semis", "capital_cap": 300, "max_position_usd": 180},
                   deployed={"semis": {"NVDA": 150.0}})
    assert check(buy(50, thesis_id="semis"), ctx) == "position_cap"
    assert check(buy(30, thesis_id="semis"), ctx) is None


def test_global_cap():
    ctx = base_ctx(deployed={"a": {"X": 600.0}, "b": {"Y": 380.0}})
    assert check(buy(50), ctx) == "global_cap"
    assert check(buy(20), ctx) is None
    # negative net (sold at a gain) does not free up room in someone else's thesis
    ctx = base_ctx(deployed={"a": {"X": 980.0, "Z": -500.0}})
    assert check(buy(50), ctx) == "global_cap"


def test_cash_not_buying_power():
    assert check(buy(50), base_ctx(account={"cash": 40.0, "buying_power": 80.0, "daytrade_count": 0})) == "cash"
    assert check(buy(50), base_ctx(pending_buys=960.0)) == "cash"


def test_pdt():
    pdt = {"logged": 3, "bought_today": {"NVDA"}, "sold_today": set()}
    assert check(sell(), base_ctx(pdt=pdt)) == "pdt"
    assert check(buy(), base_ctx(pdt=pdt)) is None  # buying again is not a round trip
    pdt = {"logged": 0, "bought_today": set(), "sold_today": {"NVDA"}}
    acct = {"cash": 1000.0, "daytrade_count": 3}
    assert check(buy(), base_ctx(pdt=pdt, account=acct)) == "pdt"  # Alpaca's count also counts
    assert check(buy(), base_ctx(pdt={**pdt, "logged": 2})) is None


# ------------------------------------------------------------------ ordering

def test_ordering_first_failure_wins():
    everything_bad = base_ctx(
        halted=True, clock={"is_open": False}, assets={"NVDA": None}, quotes={"NVDA": None},
        thesis=None, account={"cash": 0.0, "daytrade_count": 9},
        pdt={"logged": 9, "bought_today": {"NVDA"}, "sold_today": {"NVDA"}},
        deployed={"x": {"Y": 10_000.0}},
    )
    order = ["halt", "market_hours", "asset", "order_type", "thesis_cap"]
    fixes = [("halted", False), ("clock", {"is_open": True}),
             ("assets", {"NVDA": {"tradable": True, "fractionable": True, "asset_class": "us_equity"}}),
             ("quotes", {"NVDA": {"bid": 100.0, "ask": 100.0}}),
             ("thesis", {"id": "manual", "capital_cap": 200.0, "max_position_usd": 200.0})]
    ctx = dict(everything_bad)
    for expected, (key, good) in zip(order, fixes):
        assert check(buy(), ctx) == expected
        ctx[key] = good
    assert check(buy(), ctx) == "global_cap"
    ctx["deployed"] = {}
    assert check(buy(), ctx) == "cash"
    ctx["account"] = {"cash": 1000.0, "daytrade_count": 9}
    assert check(buy(), ctx) == "pdt"
    assert check(buy(250), ctx) == "per_order_cap"  # per-order beats thesis/global/cash/pdt


def test_side_before_order_type_and_caps():
    ctx = base_ctx(held_qty={}, thesis=None)
    assert check(sell(5), ctx) == "side"


def test_lazy_context_only_loads_what_is_needed():
    loaded = []

    def loader(name, value):
        def f(ctx):
            loaded.append(name)
            return value
        return f

    full = base_ctx()
    ctx = LazyContext({k: loader(k, v) for k, v in full.items()})
    ctx["halted"] = True
    assert check(buy(), ctx) == "halt"
    assert loaded == []
    ctx = LazyContext({k: loader(k, v) for k, v in full.items()})
    ctx["clock"] = {"is_open": False}
    assert check(buy(), ctx) == "market_hours"
    assert loaded == ["halted"]
