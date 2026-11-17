"""Boot/wake robustness: broker readiness waits, client retries (idempotent calls only),
Ollama wait + total-failure reporting, the weekly review job, expired-slot pushes, unit
rendering, and scheduler/doctor.py. No daemon, no network, no systemd changes."""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from test_tools import SEMIS, StubBroker, call, conn, create, journal_kinds
from tools import run_thesis
from trader import client as client_mod
from trader import config, db
from trader.client import BrokerClient, BrokerError, wait_for_broker

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def broker(monkeypatch):
    stub = StubBroker()
    stub.health_calls = 0
    stub.healthy = True

    def health():
        stub.health_calls += 1
        if not stub.healthy:
            raise BrokerError(0, "broker daemon unreachable at http://127.0.0.1:8787: refused")
        return {"ok": True}
    stub.health = health
    monkeypatch.setattr(client_mod, "BrokerClient", lambda *a, **k: stub)
    return stub


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._b = status, body if body is not None else {"ok": True}

    def json(self):
        return self._b


def fake_requests(monkeypatch, outcomes):
    """requests.request replaced by a script: each item is an exception to raise or a Resp."""
    calls = []

    def req(method, url, **kw):
        calls.append((method, url))
        o = outcomes.pop(0) if outcomes else Resp()
        if isinstance(o, Exception):
            raise o
        return o
    monkeypatch.setattr(client_mod.requests, "request", req)
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)
    return calls


# ------------------------------------------------------------------ BrokerClient retries

def real_client():
    return BrokerClient(url="http://x", token="t")


def test_get_is_retried_on_connection_error(monkeypatch):
    calls = fake_requests(monkeypatch, [requests.ConnectionError("refused"), requests.ConnectionError("refused"),
                                        Resp(body={"is_open": True})])
    assert real_client().clock() == {"is_open": True}
    assert len(calls) == 3


def test_get_gives_up_after_short_retries(monkeypatch):
    calls = fake_requests(monkeypatch, [requests.ConnectionError("refused")] * 5)
    with pytest.raises(BrokerError) as e:
        real_client().positions()
    assert e.value.status == 0 and "unreachable" in e.value.reason and len(calls) == 3


def test_order_without_client_order_id_is_never_retried(monkeypatch):
    calls = fake_requests(monkeypatch, [requests.ConnectionError("reset"), Resp(201, {"status": "filled"})])
    with pytest.raises(BrokerError):
        real_client().place_order(thesis_id="manual", side="buy", symbol="SPY", notional=5)
    assert len(calls) == 1  # a retry could get a new derived id = a second order


def test_order_with_client_order_id_is_retried(monkeypatch):
    calls = fake_requests(monkeypatch, [requests.ConnectionError("refused"), Resp(200, {"duplicate": True})])
    out = real_client().place_order(thesis_id="semis", side="buy", symbol="NVDA", notional=5, client_order_id="semis-x")
    assert out["duplicate"] and [c[0] for c in calls] == ["POST", "POST"]


def test_reconcile_retried_cancel_not(monkeypatch):
    calls = fake_requests(monkeypatch, [requests.ConnectionError("refused"), Resp(body={"updated": 0})])
    assert real_client().reconcile() == {"updated": 0} and len(calls) == 2
    calls = fake_requests(monkeypatch, [requests.ConnectionError("refused"), Resp()])
    with pytest.raises(BrokerError):
        real_client().cancel("abc")
    assert len(calls) == 1


def test_read_timeout_not_retried(monkeypatch):
    calls = fake_requests(monkeypatch, [requests.ReadTimeout("slow"), Resp()])
    with pytest.raises(BrokerError) as e:
        real_client().account()
    assert len(calls) == 1 and "did not answer" in e.value.reason


def test_wait_ready_polls_until_health(monkeypatch):
    calls = fake_requests(monkeypatch, [requests.ConnectionError("refused")] * 3 + [Resp(body={"ok": True})])
    ok, why = real_client().wait_ready(timeout=60, interval=0)
    assert ok and why is None and len(calls) == 4  # wait_ready itself does not use the inner retries


def test_wait_ready_times_out(monkeypatch):
    fake_requests(monkeypatch, [requests.ConnectionError("refused")] * 100)
    t = iter(range(0, 1000, 10))
    monkeypatch.setattr(client_mod.time, "monotonic", lambda: next(t))
    ok, why = real_client().wait_ready(timeout=30, interval=1)
    assert not ok and "unreachable" in why


def test_wait_for_broker_journals_one_error(broker):
    broker.healthy = False
    c = conn()
    ok, why = wait_for_broker("tick", conn=c, client=broker, timeout=0)
    assert not ok and "tick: broker not reachable after 0s" in why
    errs = journal_kinds("error")
    assert len(errs) == 1 and json.loads(errs[0]["data"])["job"] == "tick"


def test_wait_broker_script(monkeypatch, capsys):
    from scheduler import wait_broker
    monkeypatch.setattr(client_mod.BrokerClient, "wait_ready", lambda self, t, i=2: (False, "refused"))
    assert wait_broker.main(["--timeout", "1"]) == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "url": config.BROKER_URL, "reason": "refused"}
    monkeypatch.setattr(client_mod.BrokerClient, "wait_ready", lambda self, t, i=2: (True, None))
    assert wait_broker.main([]) == 0


# ------------------------------------------------------------------ tick / nightly / review wait

def test_tick_broker_never_up_one_error_no_orders(broker, tmp_path, capsys):
    from scheduler import tick as tick_mod
    create(tmp_path, capsys)
    create(tmp_path, capsys, {**SEMIS, "id": "tech", "universe": ["SPY"], "max_position_pct": 100})
    broker.healthy = False
    code = tick_mod.main(["--now", "2026-10-19T10:05"])
    out = json.loads(capsys.readouterr().out)
    assert code == 2 and "broker not reachable" in out["broker_unready"]
    assert broker.calls == [] and broker.clock_calls == 0 and broker.health_calls == 1  # waited once only
    errs = journal_kinds("error")
    assert len(errs) == 1 and errs[0]["message"].startswith("tick: broker not reachable")
    assert db.get_setting(conn(), "job_ok:tick") is None
    # slot stays pending; the next tick with the broker up places it
    broker.healthy = True
    assert tick_mod.main(["--now", "2026-10-19T10:20"]) == 0
    assert json.loads(capsys.readouterr().out)["placed"] == 5
    assert db.get_setting(conn(), "job_ok:tick")


def test_tick_without_work_does_not_touch_broker(broker, tmp_path, capsys):
    from scheduler import tick as tick_mod
    create(tmp_path, capsys)
    broker.healthy = False
    assert tick_mod.main(["--now", "2026-10-22T12:00"]) == 0
    capsys.readouterr()
    assert broker.health_calls == 0 and journal_kinds("error") == []


def test_nightly_broker_never_up(broker, tmp_path, capsys, monkeypatch):
    from scheduler import nightly as nightly_mod
    create(tmp_path, capsys)
    news_calls = []
    monkeypatch.setattr(nightly_mod, "call_tool",
                        lambda name, args, timeout=0: news_calls.append(name) or {"ran": True, "exit": 0})
    broker.healthy = False
    code = nightly_mod.main([])
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert [(s["step"], s["ok"]) for s in out["steps"]] == [
        ("reconcile", False), ("snapshot", False), ("news", True), ("check_triggers", False), ("bars", False)]
    assert out["steps"][0]["error"].startswith("skipped: nightly: broker not reachable")
    assert news_calls == ["news.py"]  # news needs no broker
    errs = journal_kinds("error")
    assert len(errs) == 1 and errs[0]["message"].startswith("nightly: broker not reachable")
    assert "failed: reconcile, snapshot, check_triggers, bars" in journal_kinds("system")[-1]["message"]
    assert db.get_setting(conn(), "job_ok:nightly") is None


def test_nightly_ok_records_success(broker, tmp_path, capsys, monkeypatch):
    from scheduler import nightly as nightly_mod
    create(tmp_path, capsys)
    monkeypatch.setattr(nightly_mod, "call_tool", lambda name, args, timeout=0: {"ran": True, "exit": 0})
    assert nightly_mod.main([]) == 0
    capsys.readouterr()
    assert db.get_setting(conn(), "job_ok:nightly") and journal_kinds("system")[-1]["message"] == "nightly ok"


def test_nightly_news_total_failure_is_an_error(broker, tmp_path, capsys, monkeypatch):
    from scheduler import nightly as nightly_mod
    create(tmp_path, capsys)
    monkeypatch.setattr(nightly_mod, "call_tool", lambda name, args, timeout=0: {
        "ran": True, "exit": 1, "output": {"ok": False, "reason": "every digest failed (4 symbol(s))"}})
    assert nightly_mod.main([]) == 1
    out = json.loads(capsys.readouterr().out)
    news = [s for s in out["steps"] if s["step"] == "news"][0]
    assert not news["ok"] and "every digest failed" in news["error"]
    assert any("every digest failed" in e["message"] for e in journal_kinds("error"))


def test_review_all_waits_for_broker(monkeypatch, capsys):
    from conftest import insert_thesis
    from tools import review
    insert_thesis(config.DB_PATH)
    conn().execute("UPDATE theses SET created='2026-01-01T00:00:00+00:00'")
    monkeypatch.setattr(review.tclient, "wait_for_broker",
                        lambda job, conn=None, **k: (False, f"{job}: broker not reachable after 120s (refused)"))
    monkeypatch.setattr(review, "run_json", lambda *a, **k: pytest.fail("claude must not run"))
    assert review.main(["--all"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["due"] == ["semis"] and "review: broker not reachable" in out["reason"]
    assert db.get_setting(conn(), "last_review:semis") is None  # still due next run


# ------------------------------------------------------------------ news / Ollama

def test_wait_for_ollama_retries_then_falls_back(monkeypatch):
    from tools import news
    seen = []

    def get(url, timeout=None):
        seen.append(url)
        if len(seen) < 3 or url.startswith(config.OLLAMA_URL.rstrip("/")):
            raise requests.ConnectionError("down")

        class R:
            def raise_for_status(self):
                pass
        return R()
    monkeypatch.setattr(news.requests, "get", get)
    monkeypatch.setattr(news.time, "sleep", lambda s: None)
    assert news.wait_for_ollama(timeout=60) == config.OLLAMA_FALLBACK_URL
    t = iter(range(0, 10000, 30))
    monkeypatch.setattr(news.time, "monotonic", lambda: next(t))
    monkeypatch.setattr(news.requests, "get", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("x")))
    assert news.wait_for_ollama(timeout=60) is None


def test_news_all_digests_fail_exits_1_partial_ok(monkeypatch, capsys):
    from tools import news
    heads = [{"headline": "Nvidia beats", "url": None, "published": None, "source": "x"}]
    monkeypatch.setattr(news, "fetch_symbol", lambda sym, since, limit: (list(heads) if sym != "AMD" else [], []))
    monkeypatch.setattr(news, "wait_for_ollama", lambda timeout=None: None)
    monkeypatch.setattr(news, "ask_model", lambda p: (_ for _ in ()).throw(requests.ConnectionError("refused")))
    assert news.main(["NVDA", "AMD"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and "every digest failed" in out["reason"] and "refused" in out["reason"]
    assert journal_kinds("news") == []
    # one symbol digested, one failed: a normal run with a news row
    good = json.dumps({"items": [], "overall": {"sentiment": "positive", "summary": "fine", "flag": False}})
    monkeypatch.setattr(news, "ask_model", lambda p: ("m", good) if "NVDA" in p else (_ for _ in ()).throw(ValueError("junk")))
    monkeypatch.setattr(news, "fetch_symbol", lambda sym, since, limit: (list(heads), []))
    assert news.main(["NVDA", "TSM", "--no-triggers"]) == 0
    capsys.readouterr()
    assert len(journal_kinds("news")) == 1


def test_news_every_feed_failed(monkeypatch, capsys):
    from tools import news
    monkeypatch.setattr(news, "fetch_symbol", lambda sym, since, limit: ([], ["Yahoo: down", "Google: down"]))
    monkeypatch.setattr(news, "wait_for_ollama", lambda timeout=None: pytest.fail("no headlines, no wait"))
    assert news.main(["NVDA"]) == 1
    assert "every news feed failed" in json.loads(capsys.readouterr().out)["reason"]


# ------------------------------------------------------------------ expired slot push

def test_expired_slot_pushes_once(broker, tmp_path, capsys):
    create(tmp_path, capsys)
    call(run_thesis, ["semis-q4", "--now", "2026-10-19T09:00"], capsys)
    for now in ("2026-10-21T10:01", "2026-10-21T10:30", "2026-10-23T10:30"):
        call(run_thesis, ["semis-q4", "--now", now], capsys)
    pushes = [json.loads(r["data"])["title"] for r in journal_kinds("notify")]
    assert pushes.count("Slot expired: semis-q4") == 1


# ------------------------------------------------------------------ claude binary

def test_claude_bin_resolution(monkeypatch, tmp_path):
    from web import chat
    monkeypatch.setenv("CLAUDE_BIN", "~/bin/claude")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert chat.claude_bin() == str(tmp_path / "bin" / "claude")
    monkeypatch.delenv("CLAUDE_BIN")
    monkeypatch.setenv("PATH", "/nonexistent")
    assert chat.claude_bin() in ("claude", "/usr/local/bin/claude")
    b = tmp_path / ".local" / "bin" / "claude"
    b.parent.mkdir(parents=True)
    b.write_text("#!/bin/sh\n")
    b.chmod(0o755)
    assert chat.claude_bin() == str(b)  # found without PATH, as under systemd


# ------------------------------------------------------------------ unit rendering

def render(flavor, dest):
    subprocess.run(["bash", str(ROOT / "scheduler" / "render-units.sh"), flavor, str(dest)], check=True)
    return {p.name: p.read_text() for p in dest.iterdir()}


def test_render_user_units_portable(tmp_path):
    units = render("user", tmp_path)
    assert {"thesis-review.service", "thesis-review.timer", "thesis-broker.service"} <= set(units)
    for name, text in units.items():
        assert "network-online.target" not in text and "__ROOT__" not in text and "__HOME__" not in text, name
        assert "\nUser=" not in text and "multi-user.target" not in text
    assert "ExecStartPost=" in units["thesis-broker.service"] and "wait_broker.py" in units["thesis-broker.service"]
    assert "tools/review.py --all" in units["thesis-review.service"]
    assert "After=thesis-broker.service\n" in units["thesis-web.service"]


def test_render_system_units(tmp_path):
    units = render("system", tmp_path)
    owner = (ROOT).stat().st_uid
    import pwd
    assert f"User={pwd.getpwuid(owner).pw_name}" in units["thesis-review.service"]
    assert "Wants=thesis-broker.service network-online.target" in units["thesis-nightly.service"]
    assert "User=thesis" in units["thesis-broker.service"]
    assert "Persistent=true" in units["thesis-review.timer"] and "Sat *-*-* 10:00:00 America/New_York" in units["thesis-review.timer"]


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="needs systemd-analyze")
@pytest.mark.parametrize("flavor", ["user", "system"])
def test_rendered_units_verify(flavor):
    from scheduler import doctor
    d = doctor.Doctor(mode="user")
    d.check_verify(flavor)
    assert d.checks[-1]["status"] == "PASS", d.checks[-1]


# ------------------------------------------------------------------ doctor

def make_db(path: Path, now: datetime):
    c = db.connect(path)
    db.set_setting(c, "job_ok:tick", (now - timedelta(hours=1)).isoformat())
    db.journal(c, "news", "NVDA: neutral")
    db.journal(c, "expired", "slot 2026-11-09T10:00 expired", thesis_id="semis")
    db.journal(c, "error", "nightly: broker not reachable")
    c.close()


def test_doctor_db_checks_read_only(tmp_path):
    from scheduler import doctor
    now = datetime.now(timezone.utc)
    p = tmp_path / "t.db"
    make_db(p, now)
    p.chmod(0o444)  # a read-only connection must suffice
    d = doctor.Doctor(mode="user", db_path=p, now=now)
    d.check_db()
    by = {c["check"]: c for c in d.checks}
    assert by["last_tick"]["status"] == "PASS"
    assert by["last_nightly"]["status"] == "INFO"  # no job_ok:nightly, fallback to the news row
    assert by["expired_slots_14d"]["status"] == "WARN" and len(by["expired_slots_14d"]["slots"]) == 1
    assert by["errors_7d"]["status"] == "WARN"
    with pytest.raises(sqlite3.OperationalError):
        sqlite3.connect(f"file:{p}?mode=ro", uri=True).execute("DELETE FROM journal")


def test_doctor_fails_and_exits_1(monkeypatch, tmp_path, capsys):
    from scheduler import doctor
    real_run = doctor.Doctor._run

    def fake_run(cmd, timeout=30):
        if cmd[:1] == ["bash"]:
            return real_run(cmd, timeout)
        if "calendar" in cmd:
            return 0, "  Next elapse: Mon 2026-11-16 09:30:00 EST"
        if "verify" in cmd:
            return 0, ""
        if "is-enabled" in cmd:
            return 1, "disabled"
        if "is-active" in cmd:
            return 3, "inactive"
        if "show" in cmd:
            return 0, "ActiveState=inactive\nNextElapseUSecRealtime=\nResult=success"
        return 1, "Linger=no"
    monkeypatch.setattr(doctor.Doctor, "_run", staticmethod(fake_run))
    monkeypatch.setattr(doctor, "USER_DIR", tmp_path / "none")
    monkeypatch.setattr(doctor, "SYSTEM_DIR", tmp_path / "none2")
    monkeypatch.setattr(doctor.BrokerClient, "health", lambda self: (_ for _ in ()).throw(BrokerError(0, "refused")))
    monkeypatch.setattr(doctor.requests, "get", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("x")))
    code = doctor.main(["--db", str(tmp_path / "missing.db")])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and not out["ok"]
    st = {c["check"]: c["status"] for c in out["checks"]}
    assert st["install_mode"] == "FAIL" and st["broker_health"] == "FAIL" and st["ollama"] == "FAIL"
    assert st["db"] == "FAIL" and st["linger"] == "FAIL" and st["second_machine"] == "INFO"
    assert st["calendar thesis-review.timer"] == "PASS"
