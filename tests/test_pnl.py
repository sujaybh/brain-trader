"""Realized vs unrealized P&L (average cost), snapshot realized_pl + migration, return vs SPY,
and how the dashboard / thesis page / /api/state show them."""
from __future__ import annotations

import sqlite3
from itertools import count

import pytest
from fastapi.testclient import TestClient

from conftest import insert_thesis
from tools.positions import portfolio
from tools.snapshot import take_snapshot
from trader import client as tclient
from trader import config, db, pnl
from web import app as webapp

_ids = count()


def fill(conn, thesis, symbol, side, qty, price, created="2026-10-19T14:00:00+00:00"):
    n = round(qty * price, 4)
    conn.execute("INSERT INTO orders(client_order_id, thesis_id, symbol, side, notional, qty, limit_price, status, "
                 "filled_qty, filled_avg_price, filled_notional, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (f"{thesis}-{symbol}-{side}-{next(_ids)}", thesis, symbol, side, n, qty, price, "filled", qty, price, n,
                  created, created))


class Client:
    """What portfolio() needs from the broker client."""

    def __init__(self, positions=(), prices=None):
        self._positions = list(positions)
        self.prices = prices or {}

    def reconcile(self):
        return {"updated": 0}

    def account(self):
        return {"cash": 1000.0, "equity": 1100.0, "portfolio_value": 1100.0, "buying_power": 1000.0}

    def positions(self):
        return self._positions

    def quote(self, symbols):
        return {s: {"bid": self.prices[s], "ask": self.prices[s], "last": self.prices[s]} for s in symbols
                if s in self.prices}


@pytest.fixture
def conn():
    c = db.connect()
    yield c
    c.close()


def real_spy_round_trip(conn):
    """The real manual SPY round trip from Oct 2026 (two $5 buys, one sell of everything)."""
    fill(conn, "manual", "SPY", "buy", 0.0064, 772.188, "2026-10-16T13:35:06+00:00")
    fill(conn, "manual", "SPY", "buy", 0.0065, 766.658, "2026-10-22T13:43:09+00:00")
    fill(conn, "manual", "SPY", "sell", 0.0129, 763.176, "2026-10-22T17:30:23+00:00")


# ---------------------------------------------------------------- average cost

def test_closed_round_trip_is_realized_not_a_position(conn):
    real_spy_round_trip(conn)
    lot = pnl.lots_by_thesis(conn)["manual"]["SPY"]
    assert lot["qty"] == 0 and lot["cost"] == 0
    assert lot["realized"] == pytest.approx(9.8450 - 4.9420 - 4.9833, abs=1e-3)  # -0.08

    p = portfolio(conn, Client())
    m = p["theses"]["manual"]
    assert m["positions"] == []  # no qty-0 row with a fake "unrealized" loss
    assert m["unrealized_pl"] == 0 and m["cost"] == 0
    assert m["realized_pl"] == -0.08 and m["realized_by_symbol"] == {"SPY": -0.08}
    assert p["totals"]["realized_pl"] == -0.08 and p["totals"]["unrealized_pl"] == 0


def test_partial_sell_uses_average_cost(conn):
    fill(conn, "manual", "NVDA", "buy", 1.0, 100.0)
    fill(conn, "manual", "NVDA", "buy", 1.0, 200.0)   # avg 150
    fill(conn, "manual", "NVDA", "sell", 0.5, 180.0)  # realized 0.5 * (180 - 150) = 15
    p = portfolio(conn, Client(positions=[{"symbol": "NVDA", "qty": "1.5", "current_price": "160"}]))
    t = p["theses"]["manual"]
    row = t["positions"][0]
    assert row["qty"] == 1.5 and row["cost"] == 225.0       # 1.5 x 150
    assert row["unrealized_pl"] == 15.0                     # 240 - 225
    assert row["realized_pl"] == 15.0 and t["realized_pl"] == 15.0
    # caps still see net filled notional (300 - 90), not the average-cost basis
    assert t["deployed"] == 210.0
    assert p["unattributed"] == []


def test_realized_is_per_thesis(conn):
    insert_thesis(config.DB_PATH)
    fill(conn, "semis", "AMD", "buy", 1.0, 100.0)
    fill(conn, "semis", "AMD", "sell", 1.0, 90.0)
    fill(conn, "manual", "AMD", "buy", 1.0, 100.0)
    p = portfolio(conn, Client(positions=[{"symbol": "AMD", "qty": "1", "current_price": "110"}]))
    assert p["theses"]["semis"]["realized_pl"] == -10.0 and p["theses"]["semis"]["positions"] == []
    assert p["theses"]["manual"]["realized_pl"] == 0 and p["theses"]["manual"]["unrealized_pl"] == 10.0
    assert p["totals"] == {"cost": 100.0, "market_value": 110.0, "unrealized_pl": 10.0, "realized_pl": -10.0}


# ---------------------------------------------------------------- snapshot

def test_snapshot_records_realized_and_closed_rows(conn):
    real_spy_round_trip(conn)
    fill(conn, "manual", "NVDA", "buy", 1.0, 100.0)
    c = Client(positions=[{"symbol": "NVDA", "qty": "1", "current_price": "110"}], prices={"SPY": 760.0})
    out = take_snapshot(conn, c, "2026-10-23")
    assert out["realized_pl"] == -0.08 and out["unrealized_pl"] == 10.0
    rs = {(r["symbol"], r["thesis_id"]): r for r in db.rows(conn, "SELECT * FROM snapshots WHERE date='2026-10-23'")}
    spy = rs[("SPY", "manual")]
    assert spy["qty"] == 0 and spy["market_value"] == 0 and spy["cost_basis"] == 0 and spy["realized_pl"] == -0.08
    assert rs[("NVDA", "manual")]["realized_pl"] == 0 and rs[("NVDA", "manual")]["unrealized_pl"] == 10.0
    assert rs[("SPY", "$BENCH")]["price"] == 760.0
    take_snapshot(conn, c, "2026-10-23")  # still idempotent
    assert len(db.rows(conn, "SELECT id FROM snapshots WHERE date='2026-10-23'")) == len(rs)


def test_old_snapshots_table_gets_realized_column(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE snapshots (id INTEGER PRIMARY KEY, date TEXT NOT NULL, symbol TEXT NOT NULL, "
                "thesis_id TEXT, qty REAL, price REAL, market_value REAL, cost_basis REAL, unrealized_pl REAL, "
                "data TEXT, UNIQUE(date, symbol, thesis_id))")
    old.execute("INSERT INTO snapshots(date, symbol, thesis_id, price) VALUES ('2026-10-15','SPY','$BENCH',771.68)")
    old.commit()
    old.close()
    c = db.connect(path)
    cols = {r[1] for r in c.execute("PRAGMA table_info(snapshots)")}
    assert "realized_pl" in cols
    assert db.row(c, "SELECT price, realized_pl FROM snapshots") == {"price": 771.68, "realized_pl": None}
    c.close()
    db.connect(path).close()  # second connect is a no-op


# ---------------------------------------------------------------- vs SPY

def bench(conn, day, price):
    conn.execute("INSERT INTO snapshots(date, symbol, thesis_id, price, data) VALUES (?,?,?,?,?)",
                 (day, "SPY", "$BENCH", price, '{"benchmark": true}'))


def snap(conn, day, thesis, symbol, price, qty=1.0):
    conn.execute("INSERT INTO snapshots(date, symbol, thesis_id, qty, price, market_value) VALUES (?,?,?,?,?,?)",
                 (day, symbol, thesis, qty, price, qty * price if price else None))


def test_vs_spy_not_enough_history(conn):
    assert pnl.vs_benchmark(conn, "semis")["reason"].startswith("not enough history")
    fill(conn, "semis", "NVDA", "buy", 1.0, 100.0, "2026-10-19T14:00:00+00:00")
    bench(conn, "2026-10-19", 500.0)
    snap(conn, "2026-10-19", "semis", "NVDA", 105.0)
    v = pnl.vs_benchmark(conn, "semis")  # only the fill day itself
    assert v["ok"] is False and "not enough history" in v["reason"]


def test_vs_spy_same_dollars(conn):
    # $100 on Mon at SPY 500, $100 on the next Mon at SPY 400; SPY ends at 450, NVDA doubled then flat.
    fill(conn, "semis", "NVDA", "buy", 1.0, 100.0, "2026-10-19T14:00:00+00:00")
    fill(conn, "semis", "NVDA", "buy", 0.5, 200.0, "2026-10-26T14:00:00+00:00")
    bench(conn, "2026-10-19", 500.0)
    bench(conn, "2026-10-26", 400.0)
    bench(conn, "2026-10-27", 450.0)
    snap(conn, "2026-10-27", "semis", "NVDA", 200.0, 1.5)
    v = pnl.vs_benchmark(conn, "semis", "2026-10-15T21:00:00+00:00")
    assert v["ok"] and v["start"] == "2026-10-19" and v["end"] == "2026-10-27"
    assert v["invested"] == 200.0 and v["value"] == 300.0 and v["return_pct"] == 50.0
    # SPY: 0.2 + 0.25 shares = 0.45 x 450 = 202.5 -> +1.25%
    assert v["spy_return_pct"] == 1.25 and v["excess_pct"] == 48.75


def test_vs_spy_counts_realized(conn):
    fill(conn, "manual", "SPY", "buy", 0.01, 500.0, "2026-10-16T13:35:00+00:00")   # $5
    fill(conn, "manual", "SPY", "sell", 0.01, 510.0, "2026-10-19T15:00:00+00:00")  # +$0.10 realized
    bench(conn, "2026-10-15", 500.0)   # nearest $BENCH to the Friday fill (no row on the 16th)
    bench(conn, "2026-10-19", 510.0)
    v = pnl.vs_benchmark(conn, "manual")
    assert v["ok"] and v["value"] == 0 and v["pl"] == 0.1 and v["return_pct"] == 2.0
    assert v["spy_return_pct"] == 2.0  # same symbol, same days: identical


def test_vs_spy_skips_snapshot_missing_a_held_price(conn):
    fill(conn, "semis", "NVDA", "buy", 1.0, 100.0, "2026-10-19T14:00:00+00:00")
    bench(conn, "2026-10-19", 500.0)
    bench(conn, "2026-10-20", 510.0)
    bench(conn, "2026-10-21", 520.0)
    snap(conn, "2026-10-20", "semis", "NVDA", 110.0)
    snap(conn, "2026-10-21", "semis", "NVDA", None)   # price missing on the latest day
    v = pnl.vs_benchmark(conn, "semis")
    assert v["ok"] and v["end"] == "2026-10-20" and v["return_pct"] == 10.0 and v["spy_return_pct"] == 2.0


def test_vs_spy_no_bench_near_fill(conn):
    fill(conn, "semis", "NVDA", "buy", 1.0, 100.0, "2026-10-01T14:00:00+00:00")
    bench(conn, "2026-10-19", 500.0)
    snap(conn, "2026-10-19", "semis", "NVDA", 105.0)
    v = pnl.vs_benchmark(conn, "semis")
    assert not v["ok"] and "no SPY price near 2026-10-01" in v["reason"]


# ---------------------------------------------------------------- web

class WebStub:
    def __init__(self, *a, **k):
        pass

    def account(self):
        return {"equity": "1000", "cash": "900", "buying_power": "900"}

    def positions(self):
        return [{"symbol": "NVDA", "qty": "1.5", "current_price": "200", "avg_entry_price": "133.33"}]


@pytest.fixture
def web(monkeypatch):
    monkeypatch.setattr(config, "WEB_TOKEN", "t")
    monkeypatch.setattr(tclient, "BrokerClient", WebStub)
    return TestClient(webapp.create_app())


def test_web_shows_realized_and_vs_spy(web, conn):
    insert_thesis(config.DB_PATH)
    conn.execute("UPDATE theses SET created='2026-10-15T21:00:00+00:00'")
    fill(conn, "semis", "NVDA", "buy", 1.0, 100.0, "2026-10-19T14:00:00+00:00")
    fill(conn, "semis", "NVDA", "buy", 0.5, 200.0, "2026-10-26T14:00:00+00:00")
    for d, px in (("2026-10-19", 500.0), ("2026-10-26", 400.0), ("2026-10-27", 450.0)):
        bench(conn, d, px)
    snap(conn, "2026-10-27", "semis", "NVDA", 200.0, 1.5)
    real_spy_round_trip(conn)

    st = web.get("/api/state?token=t").json()
    semis = next(t for t in st["theses"] if t["id"] == "semis")
    assert semis["vs_spy"]["ok"] and semis["vs_spy"]["return_pct"] == 50.0
    groups = {g["thesis_id"]: g for g in st["positions"]}
    assert groups["manual"]["rows"] == [] and groups["manual"]["realized_pl"] == -0.08
    assert groups["semis"]["rows"][0]["cost"] == 200.0 and groups["semis"]["rows"][0]["pl"] == 100.0

    html = web.get("/?token=t").text
    assert "vs SPY" in html and "+50.00%" in html and "+1.25%" in html
    assert "Realized" in html and "-$0.08" in html

    page = web.get("/thesis/semis?token=t").text
    assert "+50.00%" in page and "vs SPY" in page


def test_web_thesis_page_without_history(web, conn):
    insert_thesis(config.DB_PATH)
    page = web.get("/thesis/semis?token=t").text
    assert "not enough history" in page
    assert web.get("/api/state?token=t").json()["theses"][0]["vs_spy"]["ok"] is False
