"""End-to-end tests of the broker daemon against the in-memory fake backend."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from broker.app import create_app
from broker.backends import FakeBackend
from conftest import TEST_LIMITS, TOKEN, insert_thesis
from trader import db

H = {"X-Broker-Token": TOKEN}


@pytest.fixture
def env(tmp_path):
    class Env:
        backend = FakeBackend(cash=10000, market_open=True)
        db_path = tmp_path / "trader.db"
        halt_path = tmp_path / "HALT"

        def make(self, limits=None):
            app = create_app(self.backend, db_path=self.db_path, halt_path=self.halt_path,
                             token=TOKEN, limits={**TEST_LIMITS, **(limits or {})})
            return TestClient(app)

        def journal(self, kind):
            conn = db.connect(self.db_path)
            try:
                return db.rows(conn, "SELECT * FROM journal WHERE kind=? ORDER BY id", (kind,))
            finally:
                conn.close()

        def query(self, sql, params=()):
            conn = db.connect(self.db_path)
            try:
                return db.rows(conn, sql, params)
            finally:
                conn.close()

    e = Env()
    e.client = e.make()
    return e


def order(client, **body):
    payload = {"thesis_id": "manual", "side": "buy", "symbol": "NVDA", "notional": None, "qty": None}
    payload.update(body)
    if payload["notional"] is None and payload["qty"] is None:
        payload["notional"] = 50
    return client.post("/orders", json=payload, headers=H)


def assert_refused(resp, check, env=None):
    assert resp.status_code == 403, resp.text
    body = resp.json()
    assert body["refused"] is True and body["check"] == check and body["reason"]
    if env is not None:
        last = env.journal("refused")[-1]
        assert json.loads(last["data"])["check"] == check
        assert env.journal("notify"), "refusal was not pushed"


# ------------------------------------------------------------------ read endpoints & auth

def test_health_needs_no_token(env):
    r = env.client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "backend": "fake", "paper": True, "halted": False}


@pytest.mark.parametrize("path", ["/clock", "/account", "/positions", "/quote?symbols=NVDA", "/limits",
                                  "/orders", "/assets?symbols=NVDA"])
def test_auth_required(env, path):
    assert env.client.get(path).status_code == 401
    assert env.client.get(path, headers={"X-Broker-Token": "wrong"}).status_code == 401
    assert env.client.get(path, headers=H).status_code == 200


def test_auth_required_for_writes(env):
    assert env.client.post("/orders", json={"side": "buy", "symbol": "NVDA", "notional": 5}).status_code == 401
    assert env.client.post("/reconcile").status_code == 401
    assert env.client.delete("/orders/x").status_code == 401
    assert env.query("SELECT * FROM orders") == []


def test_empty_token_config_rejects_everything(env, tmp_path):
    app = create_app(env.backend, db_path=env.db_path, halt_path=env.halt_path, token="")
    c = TestClient(app)
    assert c.get("/clock", headers={"X-Broker-Token": ""}).status_code == 401


def test_clock(env):
    r = env.client.get("/clock", headers=H).json()
    assert r["is_open"] is True and {"next_open", "next_close", "timestamp"} <= r.keys()


def test_quote(env):
    r = env.client.get("/quote?symbols=nvda,AMD,ZZZZ", headers=H).json()
    assert r["NVDA"]["ask"] > r["NVDA"]["bid"] > 0
    assert set(r["AMD"]) == {"bid", "ask", "last", "ts"}
    assert r["ZZZZ"] is None


def test_assets(env):
    r = env.client.get("/assets?symbols=NVDA,BTCUSD,ZZZZ", headers=H).json()
    assert r["NVDA"]["tradable"] and r["NVDA"]["fractionable"] and r["NVDA"]["asset_class"] == "us_equity"
    assert r["BTCUSD"]["asset_class"] == "crypto"
    assert r["ZZZZ"] is None


def test_account_and_limits(env):
    a = env.client.get("/account", headers=H).json()
    assert a["cash"] == 10000 and a["daytrade_count"] == 0 and a["currency"] == "USD"
    assert env.client.get("/limits", headers=H).json()["per_order_cap"] == 200


def test_validation_is_422(env):
    r = order(env.client, notional=50, qty=1)
    assert r.status_code == 422 and "reason" in r.json()
    r = env.client.post("/orders", json={"side": "buy", "symbol": "NVDA"}, headers=H)
    assert r.status_code == 422


# ------------------------------------------------------------------ placing

def test_successful_fractional_buy(env):
    r = order(env.client, notional=50, client_order_id="t-1", reason="test")
    assert r.status_code == 201, r.text
    o = r.json()
    # NVDA min(ask 183.20, last 183.15) * 1.003 = 183.6995 -> 183.70; 50 / 183.70 = 0.27218... -> 0.2721
    assert o["type"] == "limit" and o["limit_price"] == 183.70 and o["qty"] == 0.2721
    assert o["notional"] == 50 and o["status"] == "filled" and o["filled_qty"] == 0.2721
    assert o["filled_avg_price"] == 183.70 and o["broker_order_id"] and o["submitted_at"]
    sent = env.backend.orders[o["broker_order_id"]]
    assert sent["qty"] == 0.2721 and sent["limit_price"] == 183.70 and sent["client_order_id"] == "t-1"
    row = env.query("SELECT * FROM orders WHERE client_order_id='t-1'")[0]
    assert row["filled_notional"] == pytest.approx(0.2721 * 183.70)
    assert len(env.journal("order")) == 1 and len(env.journal("fill")) == 1
    pos = env.client.get("/positions", headers=H).json()
    assert pos[0]["symbol"] == "NVDA" and pos[0]["qty"] == 0.2721


def test_manual_order_id_is_derived(env):
    o = order(env.client, notional=20).json()
    assert o["client_order_id"].startswith("manual-") and "-NVDA-b-" in o["client_order_id"]


def test_duplicate_client_order_id(env):
    first = order(env.client, notional=50, client_order_id="dup-1")
    assert first.status_code == 201
    again = order(env.client, notional=50, client_order_id="dup-1")
    assert again.status_code == 200
    body = again.json()
    assert body["duplicate"] is True and body["broker_order_id"] == first.json()["broker_order_id"]
    assert len(env.backend.orders) == 1
    assert len(env.query("SELECT * FROM orders")) == 1


# ------------------------------------------------------------------ refusals

def test_halt_refuses(env):
    env.halt_path.write_text("stop")
    assert env.client.get("/health").json()["halted"] is True
    assert_refused(order(env.client), "halt", env)
    assert env.backend.orders == {}


def test_market_closed_refuses(env):
    env.backend.market_open = False
    assert_refused(order(env.client), "market_hours", env)


def test_unknown_thesis_refuses(env):
    assert_refused(order(env.client, thesis_id="nope"), "thesis_cap", env)


def test_per_order_cap(env):
    assert_refused(order(env.client, notional=201), "per_order_cap", env)


def test_thesis_cap(env):
    insert_thesis(env.db_path)  # cap 300, 60% per name = 180
    assert order(env.client, thesis_id="semis", symbol="NVDA", notional=150).status_code == 201
    assert order(env.client, thesis_id="semis", symbol="AMD", notional=150).status_code == 201
    assert_refused(order(env.client, thesis_id="semis", symbol="AVGO", notional=10), "thesis_cap", env)


def test_manual_thesis_cap(env):
    assert order(env.client, symbol="NVDA", notional=150).status_code == 201
    assert_refused(order(env.client, symbol="AMD", notional=60), "thesis_cap")


def test_position_cap(env):
    insert_thesis(env.db_path)
    assert order(env.client, thesis_id="semis", symbol="NVDA", notional=150).status_code == 201
    assert_refused(order(env.client, thesis_id="semis", symbol="NVDA", notional=50), "position_cap", env)
    assert order(env.client, thesis_id="semis", symbol="NVDA", notional=25).status_code == 201


def test_global_cap(env):
    insert_thesis(env.db_path)
    c = env.make(limits={"global_cap": 250})
    assert order(c, symbol="SPY", notional=150).status_code == 201
    assert_refused(order(c, thesis_id="semis", symbol="NVDA", notional=150), "global_cap", env)


def test_cash(env):
    env.backend.cash = 100
    assert_refused(order(env.client, notional=150), "cash", env)


def test_short_sell_refused(env):
    assert_refused(order(env.client, side="sell", notional=None, qty=1), "side", env)
    # holding some in another thesis does not let this thesis sell it
    insert_thesis(env.db_path)
    assert order(env.client, thesis_id="semis", symbol="NVDA", notional=100).status_code == 201
    assert_refused(order(env.client, side="sell", qty=0.1), "side")


def test_sell_what_we_hold(env):
    o = order(env.client, notional=100).json()
    r = order(env.client, side="sell", notional=None, qty=o["filled_qty"])
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["limit_price"] == round(183.15 * 0.997, 2) and s["status"] == "filled"  # max(bid, last) * 0.997
    assert env.client.get("/positions", headers=H).json() == []


def test_crypto_refused(env):
    assert_refused(order(env.client, symbol="BTCUSD", notional=50), "asset", env)
    assert_refused(order(env.client, symbol="ZZZZ", notional=50), "asset")


def test_pdt_refuses_fourth_day_trade(env):
    def round_trip_leg(i, side):
        if side == "buy":
            r = order(env.client, notional=20, client_order_id=f"pdt-{i}")
        else:
            held = env.client.get("/positions", headers=H).json()[0]["qty"]
            r = order(env.client, side="sell", notional=None, qty=held, client_order_id=f"pdt-{i}")
        assert r.status_code == 201, r.text

    for i, side in enumerate(("buy", "sell", "buy", "sell")):  # s, b, s each close a round trip
        round_trip_leg(i, side)
    assert len(env.query("SELECT * FROM pdt_log")) == 3
    assert env.client.get("/account", headers=H).json()["daytrade_count"] == 3
    assert_refused(order(env.client, notional=20, client_order_id="pdt-4"), "pdt", env)
    # a symbol with no same-day round trip is still fine
    assert order(env.client, symbol="AMD", notional=20, client_order_id="pdt-5").status_code == 201


def test_manual_same_second_double_submit_is_duplicate(env):
    a = order(env.client, notional=20)
    b = order(env.client, notional=20)
    c = order(env.client, notional=30)
    assert a.status_code == 201
    if b.json()["client_order_id"] == a.json()["client_order_id"]:  # same second
        assert b.status_code == 200 and b.json()["duplicate"] is True
    assert c.status_code == 201 and c.json()["client_order_id"] != a.json()["client_order_id"]


# ------------------------------------------------------------------ fills, reconcile, cancel

def test_partial_fill_then_reconcile(env):
    env.backend.partial_fill = 0.5
    o = order(env.client, notional=100, client_order_id="pf-1").json()
    assert o["status"] == "partially_filled" and o["filled_qty"] == pytest.approx(o["qty"] / 2)
    # the unfilled half still counts against the manual cap (200): 100 + 101 > 200
    assert_refused(order(env.client, symbol="AMD", notional=101), "thesis_cap")
    env.backend.fill_remaining(o["broker_order_id"])
    r = env.client.post("/reconcile", headers=H).json()
    assert r["updated"] == 1 and r["positions"][0]["symbol"] == "NVDA"
    row = env.client.get("/orders/pf-1", headers=H).json()
    assert row["status"] == "filled" and row["filled_qty"] == o["qty"]
    assert row["filled_notional"] == pytest.approx(o["qty"] * o["limit_price"], abs=1e-4)
    assert len(env.journal("fill")) == 2
    assert env.client.post("/reconcile", headers=H).json()["updated"] == 0


def test_cancel(env):
    env.backend.auto_fill = False
    o = order(env.client, notional=50, client_order_id="c-1").json()
    assert o["status"] == "submitted"
    assert [x["client_order_id"] for x in env.client.get("/orders?status=open", headers=H).json()] == ["c-1"]
    r = env.client.delete("/orders/c-1", headers=H)
    assert r.status_code == 200 and r.json()["status"] == "canceled"
    assert env.client.get("/orders?status=open", headers=H).json() == []
    assert env.client.get("/orders?status=closed", headers=H).json()[0]["status"] == "canceled"
    assert env.client.delete("/orders/c-1", headers=H).status_code == 409
    assert env.client.delete("/orders/nope", headers=H).status_code == 404


def test_canceled_after_partial_fill_keeps_attribution(env):
    env.backend.partial_fill = 0.5
    o = order(env.client, notional=100, client_order_id="cp-1").json()
    env.client.delete("/orders/cp-1", headers=H)
    conn = db.connect(env.db_path)
    try:
        deployed = db.deployed_by_thesis(conn)["manual"]["NVDA"]
    finally:
        conn.close()
    assert deployed == pytest.approx(o["filled_qty"] * o["limit_price"], abs=0.01)


def test_get_unknown_order_404(env):
    assert env.client.get("/orders/nope", headers=H).status_code == 404


def test_upstream_error_is_502_and_frees_the_id(env):
    from broker.backends import BackendError

    def boom(**kw):
        raise BackendError("Alpaca: insufficient something")

    original = env.backend.submit_limit_order
    env.backend.submit_limit_order = boom
    r = order(env.client, notional=50, client_order_id="e-1")
    assert r.status_code == 502 and "insufficient" in r.json()["reason"]
    assert env.query("SELECT * FROM orders") == []
    env.backend.submit_limit_order = original
    assert order(env.client, notional=50, client_order_id="e-1").status_code == 201
