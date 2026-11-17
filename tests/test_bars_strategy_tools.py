"""Broker GET /bars (fake backend), BrokerClient.bars, tools/bars.py, tools/run_strategy.py and
tools/backtest.py. Tools run against the StubBroker from test_tools (no daemon, no network)."""
from __future__ import annotations

import json
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from broker.app import create_app
from broker.backends import FakeBackend, fake_bar
from conftest import TEST_LIMITS, TOKEN
from trader import client as client_mod
from trader import config, db
from test_tools import SEMIS, StubBroker, call, create

from tools import backtest as backtest_tool
from tools import bars as bars_tool
from tools import run_strategy

H = {"X-Broker-Token": TOKEN}
MOM = {"id": "mom-semis", "name": "Semis momentum", "universe": ["NVDA", "AMD", "AVGO", "TSM"],
       "capital_cap": 300, "max_position_pct": 50, "schedule": {"type": "none"},
       "strategy": {"type": "momentum", "lookback_days": 63, "top_n": 2}, "mode": "confirm",
       "source_text": "hold the two strongest semis"}


def ny(s):
    return datetime.fromisoformat(s).replace(tzinfo=config.MARKET_TZ)


@pytest.fixture
def broker(monkeypatch):
    stub = StubBroker()
    stub.bar_calls = []
    real = stub.bars

    def bars(symbols, start=None, end=None, days=None):
        stub.bar_calls.append((tuple(symbols), start, end))
        return real(symbols, start, end, days)
    stub.bars = bars
    monkeypatch.setattr(client_mod, "BrokerClient", lambda *a, **k: stub)
    return stub


def conn():
    return db.connect()


# ------------------------------------------------------------------ broker endpoint

@pytest.fixture
def api(tmp_path):
    app = create_app(FakeBackend(), db_path=tmp_path / "b.db", halt_path=tmp_path / "HALT", token=TOKEN,
                     limits=TEST_LIMITS)
    return TestClient(app), tmp_path / "b.db"


def test_bars_endpoint_is_deterministic_weekdays_and_read_only(api):
    c, path = api
    assert c.get("/bars?symbols=NVDA").status_code == 401
    r = c.get("/bars", params={"symbols": "NVDA,SPY,ZZZZ", "start": "2026-10-01", "end": "2026-10-14"}, headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "fake" and body["start"] == "2026-10-01" and body["end"] == "2026-10-14"
    nvda = body["bars"]["NVDA"]
    assert len(nvda) == 10 and all(date.fromisoformat(b["date"]).weekday() < 5 for b in nvda)
    assert nvda[0] == fake_bar("NVDA", date(2026, 10, 1)) and body["bars"]["ZZZZ"] == []
    again = c.get("/bars", params={"symbols": "NVDA", "start": "2026-10-08", "end": "2026-10-09"}, headers=H).json()
    assert again["bars"]["NVDA"] == nvda[5:7]   # same bar whatever window is asked for
    for b in nvda:
        assert b["low"] <= min(b["open"], b["close"]) <= max(b["open"], b["close"]) <= b["high"]
    d = db.connect(path)
    assert db.rows(d, "SELECT * FROM journal") == [] and db.rows(d, "SELECT * FROM bars") == []


@pytest.mark.parametrize("params", [{"start": "2026-13-01"}, {"start": "2026-10-10", "end": "2026-10-01"},
                                    {"start": "2010-01-01", "end": "2026-01-01"}, {"days": 0}])
def test_bars_endpoint_validates(api, params):
    c, _ = api
    r = c.get("/bars", params={"symbols": "NVDA", **params}, headers=H)
    assert r.status_code == 422 and r.json()["reason"]


def test_client_bars_sends_params(monkeypatch):
    seen = {}

    class R:
        status_code = 200

        def json(self):
            return {"bars": {}}

    def fake_request(method, url, headers=None, timeout=None, **kw):
        seen.update(method=method, url=url, params=kw.get("params"), token=headers["X-Broker-Token"])
        return R()
    monkeypatch.setattr(client_mod.requests, "request", fake_request)
    client_mod.BrokerClient(url="http://x", token="t").bars(["NVDA", "SPY"], start="2026-01-01")
    assert seen == {"method": "GET", "url": "http://x/bars", "params": {"symbols": "NVDA,SPY", "start": "2026-01-01"},
                    "token": "t"}


# ------------------------------------------------------------------ tools/bars.py

def test_bars_backfill_then_incremental(broker, tmp_path, capsys):
    create(tmp_path, capsys)  # semis-q4: NVDA AMD AVGO TSM
    res = bars_tool.update_bars(conn(), broker, days=100, now=ny("2026-10-30T18:45"))
    assert res["ok"] and set(res["symbols"]) == {"SPY", *SEMIS["universe"]}
    assert res["symbols"]["NVDA"]["last"] == "2026-10-30" and res["symbols"]["NVDA"]["stored"] > 60
    assert len(broker.bar_calls) == 1  # one batched request for the backfill
    broker.bar_calls.clear()
    res = bars_tool.update_bars(conn(), broker, days=100, now=ny("2026-11-03T10:00"))  # mid-session Tuesday
    assert res["symbols"]["NVDA"]["last"] == "2026-11-02"     # today's partial bar is not stored
    assert broker.bar_calls[0][1] == "2026-10-23" and res["symbols"]["NVDA"]["fetched"] <= 8


def test_bars_refetches_when_history_was_readjusted(broker, capsys):
    bars_tool.update_bars(conn(), broker, ["NVDA"], days=60, now=ny("2026-10-30T18:45"))
    c = conn()
    c.execute("UPDATE bars SET close=close*2 WHERE symbol='NVDA'")   # as if a 2:1 split re-adjusted history
    c.execute("INSERT INTO bars(symbol, date, open, high, low, close, fetched) VALUES ('NVDA','2020-01-02',1,1,1,1,'x')")
    res = bars_tool.update_bars(c, broker, ["NVDA"], days=60, now=ny("2026-11-02T18:45"))
    assert res["symbols"]["NVDA"]["readjusted_from"] and res["symbols"]["NVDA"]["refetched"] > 40
    row = db.row(c, "SELECT close FROM bars WHERE symbol='NVDA' AND date='2026-10-30'")
    assert row["close"] == fake_bar("NVDA", date(2026, 10, 30))["close"]


def test_bars_cli_reports_unknown_symbol(broker, capsys):
    code, out = call(bars_tool, ["ZZZZ", "--days", "10"], capsys)
    assert code == 1 and "ZZZZ" in out["reason"]
    code, out = call(bars_tool, ["NVDA", "--days", "10"], capsys)
    assert code == 0 and out["symbols"]["NVDA"]["stored"] > 0
    code, out = call(bars_tool, ["NVDA", "--status"], capsys)
    assert code == 0 and out["symbols"]["NVDA"]["stored"] > 0


# ------------------------------------------------------------------ tools/run_strategy.py

def _momentum_thesis(tmp_path, capsys, **kw):
    out = create(tmp_path, capsys, spec={**MOM, **kw})
    assert "rank NVDA/AMD/AVGO/TSM" in out["describe"]
    bars_tool.update_bars(conn(), client_mod.BrokerClient(), days=200, now=ny("2026-10-30T18:45"))


def test_run_strategy_proposes_only_and_is_idempotent_per_period(broker, tmp_path, capsys):
    _momentum_thesis(tmp_path, capsys)
    code, out = call(run_strategy, ["--now", "2026-11-02T11:00", "--dry-run"], capsys)
    assert code == 0 and out["results"][0]["status"] == "dry run" and out["results"][0]["orders"]
    assert db.rows(conn(), "SELECT * FROM proposals") == []
    code, out = call(run_strategy, ["--now", "2026-11-02T11:00"], capsys)
    r = out["results"][0]
    assert code == 0 and r["status"] == "proposed" and r["period"] == "2026-11"
    props = db.rows(conn(), "SELECT * FROM proposals ORDER BY id")
    assert len(props) == 2 and all(p["status"] == "pending" for p in props)
    assert {json.loads(p["action"])["side"] for p in props} == {"buy"}
    assert broker.calls == []                                   # nothing ordered
    j = db.rows(conn(), "SELECT * FROM journal WHERE kind='strategy'")
    assert len(j) == 1 and json.loads(j[0]["data"])["inputs"]["ranking"]
    code, out = call(run_strategy, ["mom-semis", "--now", "2026-11-03T11:00"], capsys)
    assert out["results"][0]["status"] == "already decided for 2026-11"
    assert len(db.rows(conn(), "SELECT * FROM proposals")) == 2


def test_run_strategy_waits_on_pending_and_skips_auto_dca_halt(broker, tmp_path, capsys):
    _momentum_thesis(tmp_path, capsys)
    call(run_strategy, ["--now", "2026-11-02T11:00"], capsys)
    c = conn()
    c.execute("UPDATE proposals SET expires='2026-12-31T00:00:00+00:00'")
    code, out = call(run_strategy, ["mom-semis", "--now", "2026-12-01T11:00"], capsys)
    assert out["results"][0]["status"].startswith("waiting")
    c.execute("UPDATE theses SET mode='auto' WHERE id='mom-semis'")
    c.execute("UPDATE theses SET spec=json_set(spec, '$.mode', 'auto') WHERE id='mom-semis'")
    code, out = call(run_strategy, ["mom-semis", "--now", "2026-12-01T11:00"], capsys)
    assert out["results"][0]["status"].startswith("skipped: auto mode")
    create(tmp_path, capsys)  # a DCA thesis is not a strategy thesis
    code, out = call(run_strategy, ["semis-q4"], capsys)
    assert out["results"][0]["status"].startswith("skipped: dca")
    assert run_strategy.strategy_theses(c) == ["mom-semis"]  # listed; skipped per run because of auto mode
    config.HALT_PATH.touch()
    c.execute("UPDATE theses SET mode='confirm', spec=json_set(spec, '$.mode', 'confirm') WHERE id='mom-semis'")
    code, out = call(run_strategy, ["mom-semis", "--now", "2027-01-04T11:00"], capsys)
    assert out["results"][0]["status"] == "halted" and broker.calls == []


def test_run_strategy_refuses_stale_or_missing_bars(broker, tmp_path, capsys):
    _momentum_thesis(tmp_path, capsys)
    code, out = call(run_strategy, ["--now", "2026-11-20T11:00"], capsys)
    assert code == 1 and "last bar 2026-10-30" in out["reason"]
    conn().execute("DELETE FROM bars WHERE symbol='AMD'")
    code, out = call(run_strategy, ["--now", "2026-11-02T11:00"], capsys)
    assert code == 1 and "AMD has 0 of 64 bars" in out["reason"]
    assert db.rows(conn(), "SELECT * FROM proposals") == []
    assert db.get_setting(conn(), "strategy_done:mom-semis:2026-11") is None


# ------------------------------------------------------------------ tools/backtest.py

def test_backtest_cli_on_thesis_and_spec_file(broker, tmp_path, capsys):
    _momentum_thesis(tmp_path, capsys)
    code, out = call(backtest_tool, ["--thesis", "mom-semis"], capsys)
    assert code == 0 and out["ok"] and out["benchmark"]["symbol"] == "SPY"
    assert "trade_log" not in out and "equity_curve" not in out
    for k in ("total_return", "cagr", "max_drawdown", "turnover", "trades", "excess_return"):
        assert k in out
    p = tmp_path / "spec.json"
    p.write_text(json.dumps({**MOM, "id": "draft"}))
    code, out = call(backtest_tool, ["--spec", str(p), "--sweep", "lookback_days=21,63", "--trades"], capsys)
    assert code == 0 and out["combinations"] == 2 and "overfitting_warning" in out
    p.write_text(json.dumps({**MOM, "id": "draft", "universe": ["NVDA", "AAPL"], "strategy": {"type": "momentum",
                                                                                              "top_n": 1}}))
    code, out = call(backtest_tool, ["--spec", str(p)], capsys)
    assert code == 1 and out["missing"] == ["AAPL"] and "tools/bars.py AAPL" in out["reason"]


def test_backtest_cli_never_writes(broker, tmp_path, capsys):
    _momentum_thesis(tmp_path, capsys)
    before = config.DB_PATH.stat().st_mtime_ns, db.rows(conn(), "SELECT COUNT(*) AS n FROM journal")
    call(backtest_tool, ["--thesis", "mom-semis"], capsys)
    assert db.rows(conn(), "SELECT COUNT(*) AS n FROM journal") == before[1]
    code, out = call(backtest_tool, ["--thesis", "mom-semis", "--db", str(tmp_path / "nope.db")], capsys)
    assert code == 1 and "no database" in out["reason"] and not (tmp_path / "nope.db").exists()
