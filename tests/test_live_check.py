"""tools/live_check.py against an in-memory broker stub. Sibling tools run in-process through a
fake `run_tool`; the web API calls are served by calling web/proposals.py and web/app.py directly."""
from __future__ import annotations

import contextlib
import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from test_tools import SEMIS, StubBroker, ny  # noqa: F401  (same stub style as the tool tests)
from tools import (check_triggers, halt, journal as journal_tool, live_check as lc, order, positions, propose,
                   review, run_thesis, snapshot)
from trader import client as client_mod
from trader import config, db
from trader.client import BrokerError
from trader.schedule import client_order_id

SLOT = "2026-10-19T10:00"


class LiveStub(StubBroker):
    """StubBroker plus the endpoints live_check uses, and the refusal checks it provokes."""

    def __init__(self):
        super().__init__()
        self.wrong_check: str | None = None   # report every refusal with this check name
        self.open_status = "filled"          # what order() reports for a polled order
        self.halted_seen = False

    def health(self):
        return {"ok": True, "backend": "fake", "paper": True, "halted": config.HALT_PATH.exists()}

    def clock(self):
        c = super().clock()
        return {**c, "next_close": "2026-10-19T16:00:00-04:00"}

    def account(self):
        return {**super().account(), "daytrade_count": 0}

    def order(self, coid):
        conn = db.connect()
        try:
            r = db.row(conn, "SELECT * FROM orders WHERE client_order_id=?", (coid,))
        finally:
            conn.close()
        if r is None:
            raise BrokerError(404, f"no order {coid}")
        return {**r, "status": r["status"] if self.open_status == "filled" else self.open_status}

    def cancel(self, coid):
        return {"status": "canceled"}

    def _refuse(self, check, why, **req):
        check = self.wrong_check or check
        conn = db.connect()  # the real broker journals every refusal (kind=refused)
        try:
            db.journal(conn, "refused", f"Refused: {why}", symbol=req.get("symbol"),
                       data={"check": check, "reason": why, "request": req})
        finally:
            conn.close()
        raise BrokerError(403, why, {"refused": True, "reason": why, "check": check})

    def place_order(self, *, thesis_id, side, symbol, notional=None, qty=None, client_order_id=None, reason=""):
        if client_order_id:
            conn = db.connect()
            try:
                if db.row(conn, "SELECT 1 FROM orders WHERE client_order_id=?", (client_order_id,)):
                    return super().place_order(thesis_id=thesis_id, side=side, symbol=symbol, notional=notional,
                                               qty=qty, client_order_id=client_order_id, reason=reason)
            finally:
                conn.close()
        req = dict(thesis_id=thesis_id, side=side, symbol=symbol, notional=notional, qty=qty,
                   client_order_id=client_order_id)
        if config.HALT_PATH.exists():
            self.halted_seen = True
            self._refuse("halt", "Kill switch is on", **req)
        if not self.assets_map.get(symbol):
            self._refuse("asset", f"{symbol} is not a known Alpaca asset.", **req)
        if notional and notional > 200:
            self._refuse("per_order_cap", "over $200", **req)
        if side == "sell":
            conn = db.connect()
            try:
                held = db.qty_by_thesis(conn).get(thesis_id, {}).get(symbol, 0.0)
            finally:
                conn.close()
            q = qty if qty is not None else notional / 580.0
            if q > held + 1e-6:
                self._refuse("side", "shorting is not allowed", **req)
        return super().place_order(thesis_id=thesis_id, side=side, symbol=symbol, notional=notional, qty=qty,
                                   client_order_id=client_order_id, reason=reason)


@pytest.fixture
def stub(monkeypatch):
    s = LiveStub()
    monkeypatch.setattr(client_mod, "BrokerClient", lambda *a, **k: s)
    monkeypatch.setattr(lc, "POLL_INTERVAL_S", 0)
    monkeypatch.setattr(lc.time, "sleep", lambda s: None)
    return s


MODS = {"order.py": order, "halt.py": halt, "run_thesis.py": run_thesis, "positions.py": positions,
        "propose.py": propose, "snapshot.py": snapshot, "check_triggers.py": check_triggers,
        "review.py": review, "journal.py": journal_tool}


@pytest.fixture
def tools(monkeypatch):
    """Run sibling tools in-process; news.py is faked (it needs the network and Qwen)."""
    calls = []

    def fake_run_tool(name, args, timeout=120):
        calls.append((name, list(args)))
        if name == "news.py":
            return 0, {"ok": True, "model": "qwen-test", "symbols": {"SPY": {
                "overall": {"sentiment": "neutral", "summary": "quiet day", "flag": False, "flag_reason": ""},
                "items": [{"headline": "x"}]}}}, ""
        if name == "run_thesis.py":  # pin the clock so a real-world Monday never makes a slot due
            args = [*args, "--now", "2026-10-19T10:15"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = MODS[name].main(args)
        return code, json.loads(buf.getvalue()), ""

    monkeypatch.setattr(lc, "run_tool", fake_run_tool)
    return calls


class Resp:
    def __init__(self, body, code=200):
        self._b, self.status_code, self.text = body, code, json.dumps(body, default=str)

    def json(self):
        return json.loads(self.text)


@pytest.fixture
def web(monkeypatch):
    from web import app as webapp
    from web import proposals

    def post(url, params=None, timeout=None):
        assert url.startswith(f"http://127.0.0.1:{config.WEB_PORT}/api/proposals/")
        pid = int(url.rstrip("/").split("/")[-2])
        conn = db.connect()
        try:
            p = proposals.approve(conn, pid, by="web")
        finally:
            conn.close()
        return Resp({"ok": p["status"] == "executed", "proposal": p})

    def get(url, params=None, timeout=None):
        assert url.endswith("/api/state")
        conn = db.connect()
        try:
            return Resp(json.loads(json.dumps(webapp.state(conn), default=str)))
        finally:
            conn.close()

    monkeypatch.setattr(lc.requests, "post", post)
    monkeypatch.setattr(lc.requests, "get", get)


def run(argv, capsys):
    code = lc.main(argv)
    cap = capsys.readouterr()
    return code, json.loads(cap.out), cap.err


def by_name(out):
    return {c["name"]: c for c in out["checks"]}


def conn():
    return db.connect()


def make_semis(tmp_path, capsys):
    from tools import thesis_create
    p = tmp_path / "semis.json"
    p.write_text(json.dumps(SEMIS))
    assert thesis_create.main([str(p)]) == 0
    capsys.readouterr()
    conn().execute("UPDATE theses SET created='2026-01-01T00:00:00+00:00' WHERE id='semis-q4'")


# ------------------------------------------------------------------ dry run

def test_dry_run_lists_checks_and_calls_nothing(monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("dry run must not touch the broker")
    monkeypatch.setattr(client_mod, "BrokerClient", boom)
    code, out, err = run(["--phase", "all", "--dry-run"], capsys)
    assert code == 0 and out["dry_run"] is True
    assert list(out["plan"]) == ["open", "idle", "close"]
    names = [c["name"] for c in out["plan"]["open"]]
    assert "duplicate_id" in names and "refuse_halt" in names
    assert "refuse_halt" in err
    assert db.rows(conn(), "SELECT * FROM journal") == []


def test_dry_run_slot_phase(capsys):
    code, out, _ = run(["--phase", "slot", "--dry-run"], capsys)
    assert code == 0 and list(out["plan"]) == ["slot"]


# ------------------------------------------------------------------ open

def test_open_phase_passes(stub, tools, capsys):
    code, out, err = run(["--phase", "open", "--symbol", "NVDA"], capsys)
    checks = by_name(out)
    assert code == 0, [c for c in out["checks"] if not c["ok"]]
    assert out["passed"] == out["total"] == 12
    assert len(out["orders_placed"]) == 2   # the order.py buy and the duplicate-id buy
    assert "filled" in checks["order_fill"]["detail"]
    assert {r["check"] for r in out["expected_refusals"]} == {"per_order_cap", "asset", "side", "halt"}
    assert stub.halted_seen and not config.HALT_PATH.exists()
    assert "PASS" in err
    js = db.rows(conn(), "SELECT * FROM journal WHERE kind='system' AND message LIKE 'live_check%'")
    assert len(js) == 1 and js[0]["message"] == "live_check open: 12/12 passed"
    assert json.loads(js[0]["data"])["orders_placed"] == out["orders_placed"]


def test_open_phase_fails_cleanly_on_wrong_check(stub, tools, capsys):
    stub.wrong_check = "market_hours"
    code, out, _ = run(["--phase", "open", "--symbol", "NVDA"], capsys)
    checks = by_name(out)
    assert code == 1
    for n in ("refuse_per_order_cap", "refuse_asset", "refuse_side", "refuse_halt"):
        assert not checks[n]["ok"] and "expected refusal" in checks[n]["detail"], checks[n]
    assert checks["health"]["ok"] and checks["order_buy"]["ok"] and checks["duplicate_id"]["ok"]
    assert not config.HALT_PATH.exists()


def test_open_phase_skips_orders_when_market_closed(stub, tools, capsys):
    stub.is_open = False
    code, out, _ = run(["--phase", "open", "--symbol", "NVDA"], capsys)
    checks = by_name(out)
    assert code == 1 and out["orders_placed"] == [] and stub.calls == []
    assert checks["order_buy"]["detail"].startswith("skipped: market closed")


def test_unfilled_order_reports_limit_and_ask(stub, tools, capsys, monkeypatch):
    monkeypatch.setattr(lc, "POLL_TIMEOUT_S", 0)
    stub.open_status = "submitted"
    ctx = lc.Ctx(lc_args(symbol="NVDA"))
    ctx.state["buy"] = {"client_order_id": "x", "status": "submitted", "limit_price": 183.75}
    r = lc.check("order_fill", lc.c_order_fill, ctx)
    assert not r["ok"] and r["detail"].startswith("unfilled: ask may have moved; limit was 183.75, current ask 183.25")


def lc_args(**kw):
    import argparse
    d = dict(phase="open", symbol="AAPL", size=3.0, thesis="semis-q4", slot=None, expect_deployed=None, dry_run=False)
    d.update(kw)
    return argparse.Namespace(**d)


def test_duplicate_check_detects_missing_dedupe(stub, monkeypatch):
    ctx = lc.Ctx(lc_args(symbol="NVDA"))
    assert lc.check("duplicate_id", lc.c_duplicate, ctx)["ok"]
    assert len(ctx.orders_placed) == 1
    # a broker that does not dedupe must fail the check
    orig = stub.place_order
    monkeypatch.setattr(stub, "place_order", lambda **kw: {**orig(**kw), "duplicate": False})
    r = lc.check("duplicate_id", lc.c_duplicate, lc.Ctx(lc_args(symbol="NVDA")))
    assert not r["ok"] and "second duplicate=False" in r["detail"]


def test_halt_check_leaves_no_halt_and_respects_foreign_halt(stub, tools):
    ctx = lc.Ctx(lc_args(symbol="NVDA"))
    r = lc.check("refuse_halt", lc.c_refuse_halt, ctx)
    assert r["ok"] and "HALT removed" in r["detail"]
    assert not config.HALT_PATH.exists()
    # the user's own HALT is never removed
    config.HALT_PATH.write_text("halted by user: going on holiday\n")
    r = lc.check("refuse_halt", lc.c_refuse_halt, lc.Ctx(lc_args(symbol="NVDA")))
    assert not r["ok"] and "not ours" in r["detail"]
    assert config.HALT_PATH.exists()


def test_halt_removed_even_if_order_call_crashes(stub, tools, monkeypatch):
    def crash(**kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(stub, "place_order", crash)
    r = lc.check("refuse_halt", lc.c_refuse_halt, lc.Ctx(lc_args(symbol="NVDA")))
    assert not r["ok"] and "boom" in r["detail"]
    assert not config.HALT_PATH.exists()


# ------------------------------------------------------------------ slot

def _run_slot():
    res = run_thesis.run_thesis("semis-q4", now=ny("2026-10-19T10:05"))
    assert res["placed"] == 4, res


def test_slot_phase_passes(stub, tools, tmp_path, capsys):
    make_semis(tmp_path, capsys)
    _run_slot()
    code, out, _ = run(["--phase", "slot", "--slot", SLOT], capsys)
    assert code == 0, [c for c in out["checks"] if not c["ok"]]
    assert out["slot"] == SLOT and out["passed"] == 5
    assert "4/4" in by_name(out)["slot_orders"]["detail"]


def test_slot_phase_fails_when_an_order_is_missing(stub, tools, tmp_path, capsys):
    make_semis(tmp_path, capsys)
    _run_slot()
    conn().execute("DELETE FROM orders WHERE client_order_id=?", (client_order_id("semis-q4", "AVGO", SLOT),))
    code, out, _ = run(["--phase", "slot", "--slot", SLOT], capsys)
    checks = by_name(out)
    assert code == 1
    assert not checks["slot_orders"]["ok"] and "missing AVGO" in checks["slot_orders"]["detail"]
    assert not checks["slot_positions"]["ok"]   # $37.50 deployed, not $50


def test_default_slot_is_most_recent_cron_occurrence():
    from trader.spec import ThesisSpec
    spec = ThesisSpec(**SEMIS)
    assert lc.default_slot(spec, ny("2026-10-19T10:05")) == SLOT
    assert lc.default_slot(spec, ny("2026-10-19T09:59")) == "2026-10-12T10:00"
    assert lc.default_slot(spec, ny("2026-10-21T15:00")) == SLOT


# ------------------------------------------------------------------ idle

def test_idle_phase_passes(stub, tools, web, tmp_path, capsys):
    make_semis(tmp_path, capsys)
    # SPY bought "Friday" in manual so the $2 sell has shares to sell and is not a day trade
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
    conn().execute("INSERT INTO orders(client_order_id, thesis_id, symbol, side, notional, qty, limit_price, status, "
                   "filled_qty, filled_avg_price, filled_notional, created, updated) "
                   "VALUES ('fri-spy','manual','SPY','buy',5,0.0086,580,'filled',0.0086,580,5,?,?)", (old, old))
    code, out, _ = run(["--phase", "idle"], capsys)
    checks = by_name(out)
    assert code == 0, [c for c in out["checks"] if not c["ok"]]
    assert "no day trade" in checks["proposal_approve"]["detail"]
    assert len(out["orders_placed"]) == 1
    sell = db.row(conn(), "SELECT * FROM orders WHERE client_order_id=?", (out["orders_placed"][0],))
    assert sell["side"] == "sell" and sell["symbol"] == "SPY"


def test_idle_proposal_skipped_if_spy_bought_today(stub, tools, web, capsys):
    now = db.now_iso()
    conn().execute("INSERT INTO orders(client_order_id, thesis_id, symbol, side, notional, status, filled_qty, "
                   "filled_notional, created, updated) VALUES ('t','manual','SPY','buy',5,'filled',0.01,5,?,?)",
                   (now, now))
    r = lc.check("proposal_approve", lc.c_proposal, lc.Ctx(lc_args()))
    assert not r["ok"] and "day trade" in r["detail"]
    assert db.rows(conn(), "SELECT * FROM proposals") == []


# ------------------------------------------------------------------ close

def test_close_phase_passes_after_open(stub, tools, capsys):
    run(["--phase", "open", "--symbol", "NVDA"], capsys)
    code, out, _ = run(["--phase", "close"], capsys)
    checks = by_name(out)
    assert code == 0, [c for c in out["checks"] if not c["ok"]]
    assert "2 order(s), 4 expected refusal(s)" in checks["script_orders"]["detail"]


def test_close_phase_flags_stale_submitted_order(stub, tools, capsys, monkeypatch):
    created = datetime.now(timezone.utc) - timedelta(minutes=45)
    # "today" is the stale order's market date, even if the test runs just after midnight ET
    monkeypatch.setattr(lc, "now_et", lambda: created.astimezone(config.MARKET_TZ))
    iso = created.isoformat(timespec="seconds")
    conn().execute("INSERT INTO orders(client_order_id, thesis_id, symbol, side, notional, qty, limit_price, status, "
                   "created, updated) VALUES ('stale-1','manual','NVDA','buy',3,0.016,183.7,'submitted',?,?)",
                   (iso, iso))
    code, out, _ = run(["--phase", "close"], capsys)
    checks = by_name(out)
    assert code == 1
    assert not checks["no_stale_orders"]["ok"] and "stale-1" in checks["no_stale_orders"]["detail"]
    assert checks["no_halt"]["ok"] and checks["pdt"]["ok"]


def test_close_flags_script_order_never_filled(stub, tools, capsys):
    db.journal(conn(), "system", "live_check open: 12/12 passed", data={"orders_placed": ["ghost-1"],
                                                                       "expected_refusals": []})
    code, out, _ = run(["--phase", "close"], capsys)
    c = by_name(out)["script_orders"]
    assert code == 1 and not c["ok"] and "ghost-1:no row" in c["detail"]
