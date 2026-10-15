"""Tests for tools/ and scheduler/ against an in-memory broker stub (no daemon, no network).
The temp DB / HALT / NTFY isolation comes from the autouse fixture in conftest.py."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from trader import client as client_mod
from trader import config, db
from trader.client import BrokerError
from trader.schedule import client_order_id

from tools import check_triggers, halt, order, run_thesis, thesis_create, thesis_pause, thesis_resume
from scheduler import tick as tick_mod

SEMIS = {
    "id": "semis-q4", "name": "Big semiconductors", "rationale": "AI capex",
    "universe": ["NVDA", "AMD", "AVGO", "TSM"], "capital_cap": 300, "max_position_pct": 60,
    "schedule": {"type": "dca", "amount": 50, "cron": "0 10 * * 1", "tz": "America/New_York"},
    "exit": {"stop_loss_pct": None, "review_only": True},
    "triggers": [{"type": "drawdown_pct", "value": 15, "action": "review"}],
    "source_text": "invest in big semis, 300 max",
}
PRICES = {"NVDA": 183.20, "AMD": 150.00, "AVGO": 330.00, "TSM": 190.00, "SPY": 580.00}
MON_SLOT = "2026-10-19T10:00"


class StubBroker:
    """Mimics the broker daemon: writes the orders table like the real one (filled at once)."""

    def __init__(self):
        self.is_open = True
        self.calls: list[dict] = []
        self.refuse: dict[str, tuple[str, str]] = {}   # symbol -> (check, reason)
        self.assets_map: dict = {s: {"tradable": True, "fractionable": True, "asset_class": "us_equity",
                                     "name": s, "exchange": "NASDAQ"} for s in PRICES}
        self.positions_list: list[dict] = []
        self.clock_calls = 0

    def clock(self):
        self.clock_calls += 1
        return {"is_open": self.is_open, "next_open": "2026-10-20T09:30:00-04:00"}

    def quote(self, symbols):
        return {s: {"bid": PRICES[s] - 0.05, "ask": PRICES[s] + 0.05, "last": PRICES[s]} for s in symbols if s in PRICES}

    def assets(self, symbols):
        return {s: self.assets_map.get(s) for s in symbols}

    def account(self):
        return {"cash": 900.0, "equity": 1000.0, "portfolio_value": 1000.0, "buying_power": 900.0}

    def positions(self):
        return self.positions_list

    def reconcile(self):
        return {"updated": 0, "positions": self.positions_list}

    def place_order(self, *, thesis_id, side, symbol, notional=None, qty=None, client_order_id=None, reason=""):
        self.calls.append(dict(thesis_id=thesis_id, side=side, symbol=symbol, notional=notional, qty=qty,
                               client_order_id=client_order_id, reason=reason))
        if client_order_id is None:  # the real broker derives one for manual orders
            from datetime import datetime, timezone
            from trader.schedule import manual_order_id
            client_order_id = manual_order_id(thesis_id, symbol, side, datetime.now(timezone.utc))
        conn = db.connect()
        try:
            ex = db.row(conn, "SELECT * FROM orders WHERE client_order_id=?", (client_order_id,))
            if ex:
                return {**ex, "duplicate": True}
            if symbol in self.refuse:
                check, why = self.refuse[symbol]
                raise BrokerError(403, why, {"refused": True, "reason": why, "check": check})
            px = PRICES[symbol]
            q = qty if qty is not None else round(notional / px, 4)
            n = notional if notional is not None else round(q * px, 2)
            now = db.now_iso()
            conn.execute("INSERT INTO orders(client_order_id, thesis_id, symbol, side, notional, qty, limit_price, "
                         "status, filled_qty, filled_avg_price, filled_notional, reason, created, updated) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (client_order_id, thesis_id, symbol, side, n, q, px, "filled", q, px, n, reason, now, now))
            return {"client_order_id": client_order_id, "symbol": symbol, "side": side, "type": "limit",
                    "limit_price": px, "qty": q, "notional": n, "status": "filled", "filled_qty": q,
                    "filled_avg_price": px}
        finally:
            conn.close()


@pytest.fixture
def broker(monkeypatch):
    stub = StubBroker()
    monkeypatch.setattr(client_mod, "BrokerClient", lambda *a, **k: stub)
    return stub


def call(mod, argv, capsys):
    code = mod.main(argv)
    out = capsys.readouterr().out
    return code, json.loads(out)


def conn():
    return db.connect()


def create(tmp_path, capsys, spec=SEMIS, backdate=True):
    p = tmp_path / f"{spec['id']}.json"
    p.write_text(json.dumps(spec))
    code, out = call(thesis_create, [str(p)], capsys)
    assert code == 0, out
    if backdate:  # make the thesis older than every slot the tests use
        conn().execute("UPDATE theses SET created='2026-01-01T00:00:00+00:00' WHERE id=?", (spec["id"],))
    return out


def ny(s):
    return datetime.fromisoformat(s).replace(tzinfo=config.MARKET_TZ)


def journal_kinds(kind, thesis=None):
    q = "SELECT * FROM journal WHERE kind=?" + (" AND thesis_id=?" if thesis else "")
    return db.rows(conn(), q, (kind, thesis) if thesis else (kind,))


# ------------------------------------------------------------------ thesis_create

def test_create_refuses_bad_symbols_naming_all(broker, tmp_path, capsys):
    broker.assets_map["FOO"] = None
    broker.assets_map["BRKX"] = {"tradable": False, "fractionable": True, "asset_class": "us_equity"}
    broker.assets_map["ODD"] = {"tradable": True, "fractionable": False, "asset_class": "us_equity"}
    spec = {**SEMIS, "universe": ["NVDA", "FOO", "BRKX", "ODD"]}
    p = tmp_path / "s.json"
    p.write_text(json.dumps(spec))
    code, out = call(thesis_create, [str(p)], capsys)
    assert code == 1 and out["ok"] is False
    for s in ("FOO", "BRKX", "ODD"):
        assert s in out["reason"]
    assert "NVDA" not in out["reason"]
    assert {b["symbol"] for b in out["bad_symbols"]} == {"FOO", "BRKX", "ODD"}
    assert db.rows(conn(), "SELECT * FROM theses") == []


def test_create_rejects_invalid_spec(broker, tmp_path, capsys):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({**SEMIS, "capital_cap": 10}))  # amount 50 > cap
    code, out = call(thesis_create, [str(p)], capsys)
    assert code == 1 and "invalid" in out["reason"]


def test_dry_run_stores_nothing(broker, tmp_path, capsys):
    p = tmp_path / "s.json"
    p.write_text(json.dumps(SEMIS))
    code, out = call(thesis_create, [str(p), "--dry-run"], capsys)
    assert code == 0 and out["dry_run"] is True
    assert out["describe"].startswith("Every Monday at 10:00am, buy $50")
    assert out["spec"]["universe"] == SEMIS["universe"]
    c = conn()
    assert db.rows(c, "SELECT * FROM theses") == []
    assert db.rows(c, "SELECT * FROM journal") == []


def test_create_stores_journals_and_ensures_manual(broker, tmp_path, capsys):
    create(tmp_path, capsys, backdate=False)
    ids = {r["id"] for r in db.rows(conn(), "SELECT id FROM theses")}
    assert ids == {"semis-q4", "manual"}
    assert any("created" in j["message"] for j in journal_kinds("thesis", "semis-q4"))
    assert journal_kinds("notify")  # the read-back was pushed (journaled; NTFY_TOPIC unset)
    p = tmp_path / "semis-q4.json"
    code, out = call(thesis_create, [str(p)], capsys)
    assert code == 1 and "already exists" in out["reason"]


# ------------------------------------------------------------------ run_thesis

def test_run_places_one_order_per_symbol_with_ids_and_is_idempotent(broker, tmp_path, capsys):
    create(tmp_path, capsys)
    code, out = call(run_thesis, ["semis-q4", "--now", "2026-10-19T10:05"], capsys)
    assert code == 0, out
    assert out["placed"] == 4
    assert [c["client_order_id"] for c in broker.calls] == [
        client_order_id("semis-q4", s, MON_SLOT) for s in SEMIS["universe"]]
    assert all(c["notional"] == 12.5 and c["reason"] == f"dca slot {MON_SLOT}" for c in broker.calls)
    assert len(journal_kinds("dca", "semis-q4")) == 1
    # fills are pushed by the broker; the tool journals one slot summary and does not push
    summary = journal_kinds("dca", "semis-q4")[0]["message"]
    assert "$12.50 NVDA @ 183.20" in summary and "$50/$300" in summary
    assert not [j for j in journal_kinds("notify") if "Bought" in j["message"]]
    for _ in range(3):
        code, out = call(run_thesis, ["semis-q4", "--now", "2026-10-19T10:20"], capsys)
        assert code == 0 and out["placed"] == 0
    assert len(broker.calls) == 4


def test_run_dry_run_places_nothing(broker, tmp_path, capsys):
    create(tmp_path, capsys)
    code, out = call(run_thesis, ["semis-q4", "--now", "2026-10-19T10:05", "--dry-run"], capsys)
    assert code == 0 and broker.calls == []
    assert len(out["slots"][0]["orders"]) == 4
    code, out = call(run_thesis, ["semis-q4", "--now", "2026-10-19T10:05"], capsys)
    assert out["placed"] == 4


def test_run_continues_past_refusal_and_does_not_retry_it(broker, tmp_path, capsys):
    create(tmp_path, capsys)
    broker.refuse["AMD"] = ("cash", "not enough cash")
    code, out = call(run_thesis, ["semis-q4", "--now", "2026-10-19T10:05"], capsys)
    assert code == 1 and out["ok"] is False
    assert out["placed"] == 3 and out["refused"] == 1
    assert {c["symbol"] for c in broker.calls} == {"NVDA", "AMD", "AVGO", "TSM"}
    r = db.row(conn(), "SELECT status FROM orders WHERE client_order_id=?",
               (client_order_id("semis-q4", "AMD", MON_SLOT),))
    assert r["status"] == "refused"
    n = len(broker.calls)
    code, out = call(run_thesis, ["semis-q4", "--now", "2026-10-19T10:20"], capsys)
    assert code == 0 and len(broker.calls) == n


def test_run_does_nothing_when_market_closed(broker, tmp_path, capsys):
    create(tmp_path, capsys)
    broker.is_open = False
    code, out = call(run_thesis, ["semis-q4", "--now", "2026-10-19T10:05"], capsys)
    assert code == 0 and out["status"] == "market closed" and out["pending"] == [MON_SLOT]
    assert broker.calls == []
    broker.is_open = True  # next tick during hours picks the slot up
    code, out = call(run_thesis, ["semis-q4", "--now", "2026-10-20T09:45"], capsys)
    assert out["placed"] == 4 and broker.calls[0]["client_order_id"].startswith("semis-q4-2026-10-19T1000")


def test_run_honors_catchup_window_and_journals_expired_once(broker, tmp_path, capsys):
    create(tmp_path, capsys)
    # 47h59m late: still caught up
    code, out = call(run_thesis, ["semis-q4", "--now", "2026-10-21T09:59"], capsys)
    assert out["placed"] == 4


def test_run_never_attempts_expired_slot(broker, tmp_path, capsys):
    create(tmp_path, capsys)
    call(run_thesis, ["semis-q4", "--now", "2026-10-19T09:00"], capsys)   # before the slot
    code, out = call(run_thesis, ["semis-q4", "--now", "2026-10-21T10:01"], capsys)  # 48h01m later
    assert code == 0 and broker.calls == [] and out["expired"] == [MON_SLOT]
    call(run_thesis, ["semis-q4", "--now", "2026-10-21T10:30"], capsys)
    call(run_thesis, ["semis-q4", "--now", "2026-10-23T10:30"], capsys)
    assert broker.calls == []
    assert len(journal_kinds("expired", "semis-q4")) == 1


def test_run_skips_when_fully_deployed(broker, tmp_path, capsys):
    create(tmp_path, capsys)
    c = conn()
    for s in SEMIS["universe"]:
        c.execute("INSERT INTO orders(client_order_id, thesis_id, symbol, side, notional, status, filled_qty, "
                  "filled_notional, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (f"pre-{s}", "semis-q4", s, "buy", 75, "filled", 0.4, 75, db.now_iso(), db.now_iso()))
    code, out = call(run_thesis, ["semis-q4", "--now", "2026-10-19T10:05"], capsys)
    assert code == 0 and broker.calls == []
    assert out["slots"][0]["status"].startswith("skipped")
    call(run_thesis, ["semis-q4", "--now", "2026-10-19T10:20"], capsys)
    sk = journal_kinds("skipped", "semis-q4")
    assert len(sk) == 1 and "fully deployed" in sk[0]["message"]


def test_paused_thesis_skips_and_does_not_catch_up_on_resume(broker, tmp_path, capsys):
    create(tmp_path, capsys)
    code, _ = call(thesis_pause, ["semis-q4"], capsys)
    assert code == 0
    spec = json.loads(db.row(conn(), "SELECT spec FROM theses WHERE id='semis-q4'")["spec"])
    assert spec["status"] == "paused"
    code, out = call(run_thesis, ["semis-q4", "--now", "2026-10-19T10:05"], capsys)
    assert broker.calls == [] and "paused" in out["status"]
    call(thesis_resume, ["semis-q4"], capsys)
    # resumed "now" (real time) - slots before the resume are not caught up
    conn().execute("UPDATE settings SET value='2026-10-19T15:00:00+00:00' WHERE key='resumed:semis-q4'")
    code, out = call(run_thesis, ["semis-q4", "--now", "2026-10-19T12:00"], capsys)
    assert broker.calls == [] and out["pending"] == []


# ------------------------------------------------------------------ order.py

def test_order_defaults_to_manual_thesis(broker, capsys):
    code, out = call(order, ["--side", "buy", "--symbol", "spy", "--notional", "20"], capsys)
    assert code == 0 and out["ok"] and out["thesis"] == "manual"
    assert broker.calls[0]["thesis_id"] == "manual" and broker.calls[0]["symbol"] == "SPY"
    assert db.row(conn(), "SELECT id FROM theses WHERE id='manual'")
    assert broker.calls[0]["client_order_id"] is None  # the broker derives the id and pushes the fill
    assert not any("Bought" in j["message"] for j in journal_kinds("notify"))


def test_order_refusal_exits_1(broker, capsys):
    broker.refuse["SPY"] = ("per_order_cap", "order exceeds per-order cap of $200")
    code, out = call(order, ["--side", "buy", "--symbol", "SPY", "--notional", "500"], capsys)
    assert code == 1 and out["ok"] is False and out["refused"] is True
    assert "per-order cap" in out["reason"] and out["check"] == "per_order_cap"


def test_order_unknown_thesis(broker, capsys):
    code, out = call(order, ["--side", "buy", "--symbol", "SPY", "--qty", "1", "--thesis", "nope"], capsys)
    assert code == 1 and broker.calls == []


# ------------------------------------------------------------------ check_triggers

def _hold_nvda_down(broker, tmp_path, capsys, spec=SEMIS):
    create(tmp_path, capsys, spec)
    call(run_thesis, [spec["id"], "--now", "2026-10-19T10:05"], capsys)
    broker.positions_list = [{"symbol": "NVDA", "qty": "0.0682", "avg_entry_price": "183.2",
                              "current_price": "146.56", "unrealized_plpc": "-0.2"}]


def test_drawdown_fires_once_and_debounces(broker, tmp_path, capsys, monkeypatch):
    calls = []
    monkeypatch.setattr(check_triggers, "call_tool", lambda name, args, timeout=900: calls.append((name, args)) or {"ran": True})
    _hold_nvda_down(broker, tmp_path, capsys)
    t0 = "2026-10-19T18:30"
    code, out = call(check_triggers, ["--now", t0], capsys)
    assert code == 0 and len(out["fired"]) == 1
    f = out["fired"][0]
    assert (f["type"], f["symbol"], f["action"]) == ("drawdown_pct", "NVDA", "review")
    assert calls[0][0] == "review.py" and calls[0][1][:2] == ["--thesis", "semis-q4"]
    code, out = call(check_triggers, ["--now", "2026-10-20T18:00"], capsys)   # 23.5h later
    assert out["fired"] == [] and len(out["debounced"]) == 1
    assert len(journal_kinds("trigger", "semis-q4")) == 1
    code, out = call(check_triggers, ["--now", "2026-10-20T18:31"], capsys)   # >24h later
    assert len(out["fired"]) == 1


def test_trigger_pause_and_missing_subprocess_tools(broker, tmp_path, capsys, monkeypatch):
    spec = {**SEMIS, "triggers": [{"type": "drawdown_pct", "value": 15, "action": "pause"},
                                  {"type": "price_below", "value": 160, "symbol": "NVDA", "action": "propose_exit"}]}
    monkeypatch.setattr(check_triggers, "TOOLS", tmp_path)   # propose.py "does not exist"
    _hold_nvda_down(broker, tmp_path, capsys, spec)
    monkeypatch.setitem(PRICES, "NVDA", 150.0)
    code, out = call(check_triggers, ["--now", "2026-10-19T18:30"], capsys)
    assert code == 0
    by_type = {f["type"]: f for f in out["fired"]}
    assert by_type["drawdown_pct"]["result"] == {"paused": True}
    assert by_type["price_below"]["result"]["ran"] is False
    assert db.row(conn(), "SELECT status FROM theses WHERE id='semis-q4'")["status"] == "paused"


# ------------------------------------------------------------------ halt

def test_halt_on_off_status(broker, capsys):
    code, out = call(halt, ["status"], capsys)
    assert out["halted"] is False
    code, out = call(halt, ["on", "--reason", "testing"], capsys)
    assert code == 0 and out["halted"] and config.HALT_PATH.exists()
    loud = [j for j in journal_kinds("notify") if "Kill switch ON" in j["message"]]
    assert len(loud) == 1
    call(halt, ["on"], capsys)  # idempotent: no second push
    assert len([j for j in journal_kinds("notify") if "Kill switch ON" in j["message"]]) == 1
    code, out = call(halt, ["off"], capsys)
    assert out["halted"] is False and not config.HALT_PATH.exists()


def test_run_respects_halt(broker, tmp_path, capsys):
    create(tmp_path, capsys)
    config.HALT_PATH.write_text("x")
    code, out = call(run_thesis, ["semis-q4", "--now", "2026-10-19T10:05"], capsys)
    assert out["status"] == "halted" and broker.calls == []


# ------------------------------------------------------------------ scheduler tick

def test_tick_runs_all_active_and_exits_0_on_refusal(broker, tmp_path, capsys):
    create(tmp_path, capsys)
    create(tmp_path, capsys, {**SEMIS, "id": "tech", "universe": ["SPY"], "max_position_pct": 100})
    broker.refuse["SPY"] = ("global_cap", "global cap reached")
    code = tick_mod.main(["--now", "2026-10-19T10:05"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["placed"] == 4 and out["refused"] == 1
    assert broker.clock_calls == 1


def test_tick_skips_clock_when_nothing_pending(broker, tmp_path, capsys):
    create(tmp_path, capsys)
    broker.is_open = False
    code = tick_mod.main(["--now", "2026-10-22T12:00"])   # Thursday, Monday slot long gone
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and broker.clock_calls == 0 and out["placed"] == 0


# ------------------------------------------------------------------ scheduler nightly

def test_nightly_step_failure_is_journaled_and_next_steps_run(broker, tmp_path, capsys, monkeypatch):
    from scheduler import nightly as nightly_mod
    create(tmp_path, capsys)
    news_calls = []
    monkeypatch.setattr(nightly_mod, "call_tool",
                        lambda name, args, timeout=0: news_calls.append((name, args)) or {"ran": True, "exit": 0})

    def boom():
        raise BrokerError(502, "alpaca is down")
    monkeypatch.setattr(broker, "reconcile", boom)
    code = nightly_mod.main([])
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert [(s["step"], s["ok"]) for s in out["steps"]] == [
        ("reconcile", False), ("snapshot", False), ("news", True), ("check_triggers", True)]
    assert news_calls[0] == ("news.py", ["AMD", "AVGO", "NVDA", "TSM", "--since", "24h"])
    errs = journal_kinds("error")
    assert len(errs) == 2 and "alpaca is down" in errs[0]["message"]


def test_snapshot_is_idempotent(broker, tmp_path, capsys):
    from tools import snapshot
    create(tmp_path, capsys)
    call(run_thesis, ["semis-q4", "--now", "2026-10-19T10:05"], capsys)
    broker.positions_list = [{"symbol": s, "qty": str(12.5 / PRICES[s]), "current_price": str(PRICES[s])}
                             for s in SEMIS["universe"]] + [{"symbol": "AAPL", "qty": "1", "current_price": "230"}]
    for _ in range(2):
        code, out = call(snapshot, ["--date", "2026-10-19"], capsys)
        assert code == 0
    rows = db.rows(conn(), "SELECT * FROM snapshots WHERE date='2026-10-19'")
    assert len(rows) == 7  # 4 thesis rows + unattributed AAPL + $ACCOUNT + SPY benchmark
    assert any(r["symbol"] == "SPY" and r["thesis_id"] == "$BENCH" and r["price"] for r in rows)
    assert {r["symbol"] for r in rows if r["thesis_id"] is None} == {"AAPL", "$ACCOUNT"}
