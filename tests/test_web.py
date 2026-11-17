"""web/ (dashboard, auth, proposal API, chat), web/proposals.py, tools/propose.py, tools/review.py.
The broker client is replaced by a stub; claude is never invoked (subprocess is mocked)."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from conftest import insert_thesis
from trader import client as tclient
from trader import config, db
from web import app as webapp
from web import chat, proposals

T = "t"


class StubBroker:
    placed: list[dict] = []
    down = False
    refuse = None

    def __init__(self, *a, **k):
        if StubBroker.down:
            raise tclient.BrokerError(0, "broker daemon unreachable at http://127.0.0.1:8787: refused")

    def health(self):
        return {"ok": True, "mode": "paper"}

    def account(self):
        return {"equity": "1234.5", "cash": "900", "buying_power": "900"}

    def positions(self):
        return [{"symbol": "NVDA", "qty": "0.5", "avg_entry_price": "100", "current_price": "120",
                 "market_value": "60", "cost_basis": "50", "unrealized_pl": "10", "unrealized_plpc": "0.2"}]

    def reconcile(self):
        return {"updated": 0, "positions": self.positions()}

    def place_order(self, **kw):
        if StubBroker.refuse:
            raise tclient.BrokerError(403, StubBroker.refuse)
        StubBroker.placed.append(kw)
        return {"client_order_id": kw["client_order_id"], "status": "filled", "filled_qty": 0.4,
                "filled_avg_price": 125.0, "symbol": kw["symbol"], "side": kw["side"]}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr(config, "WEB_TOKEN", T)
    monkeypatch.setattr(config, "WEB_URL", "http://100.64.0.19:8010")
    monkeypatch.setattr(tclient, "BrokerClient", StubBroker)
    StubBroker.placed, StubBroker.down, StubBroker.refuse = [], False, None
    yield


@pytest.fixture
def conn():
    c = db.connect()
    yield c
    c.close()


@pytest.fixture
def web():
    return TestClient(webapp.create_app())


def add_filled(conn, thesis="semis", symbol="NVDA", side="buy", notional=50.0, qty=0.5, when=None):
    now = when or db.now_iso()
    conn.execute("INSERT INTO orders(client_order_id, thesis_id, symbol, side, notional, qty, status, filled_qty, "
                 "filled_avg_price, filled_notional, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (f"{thesis}-{symbol}-{side}-{now}", thesis, symbol, side, notional, None, "filled", qty,
                  notional / qty, notional, now, now))


def order_action(**kw):
    return {"type": "order", "side": "buy", "symbol": "NVDA", "notional": 25, **kw}


# ---------------------------------------------------------------- auth

def test_refuses_to_start_without_token(monkeypatch):
    monkeypatch.setattr(config, "WEB_TOKEN", "")
    with pytest.raises(RuntimeError):
        webapp.create_app()


def test_401_without_or_with_wrong_token(web):
    assert web.get("/").status_code == 401
    assert web.get("/?token=nope").status_code == 401
    assert web.get("/api/state").status_code == 401
    r = web.post("/api/proposals/1/approve")
    assert r.status_code == 401 and r.json()["ok"] is False
    assert web.get("/static/manifest.webmanifest").status_code == 401


def test_query_token_sets_cookie(web):
    r = web.get(f"/journal?token={T}")
    assert r.status_code == 200 and r.cookies.get("tt_token") == T
    assert web.get("/journal").status_code == 200  # cookie now carries auth
    assert web.get("/static/manifest.webmanifest").status_code == 200


# ---------------------------------------------------------------- pages

def test_dashboard_renders_thesis_position_proposal(web, conn):
    insert_thesis(config.DB_PATH)
    add_filled(conn)
    p = proposals.create(conn, "semis", order_action(), "AMD looks cheap after the dip")
    r = web.get(f"/?token={T}")
    assert r.status_code == 200, r.text
    html = r.text
    assert "Semis" in html and "$1,234.50" in html and "$50.00 / $1,000.00" in html
    assert f"/api/proposals/{p['id']}/approve" in html and "AMD looks cheap" in html
    assert "NVDA" in html and "+$10.00" in html and 'class="num pos"' in html  # 0.5 * 120 - 50
    assert "apple-mobile-web-app-capable" in html and "prefers-color-scheme" in html
    assert "next " in html  # next DCA slot shown
    st = web.get("/api/state").json()
    assert st["theses"][0]["id"] == "semis" and st["proposals"][0]["id"] == p["id"]
    assert st["positions"][0]["rows"][0]["pl"] == 10.0 and st["deployed"] == 50.0


def test_dashboard_survives_broker_down(web, conn):
    insert_thesis(config.DB_PATH)
    StubBroker.down = True
    r = web.get(f"/?token={T}")
    assert r.status_code == 200 and "broker unreachable" in r.text
    assert web.get("/api/state").json()["broker_error"].startswith("broker unreachable")
    assert web.get("/thesis/semis").status_code == 200


def test_thesis_journal_chat_pages(web, conn):
    insert_thesis(config.DB_PATH, source_text="big chips, 300 max")
    db.journal(conn, "thesis", "created semis", thesis_id="semis")
    r = web.get(f"/thesis/semis?token={T}")
    assert r.status_code == 200 and "never more than $300 total" in r.text and "big chips, 300 max" in r.text
    assert "/api/theses/semis/pause" in r.text
    assert web.get("/thesis/nope").status_code == 404
    j = web.get("/journal?since=1d")
    assert j.status_code == 200 and "created semis" in j.text
    assert web.get("/chat").status_code == 200


def test_pause_resume_buttons(web, conn):
    insert_thesis(config.DB_PATH)
    r = web.post(f"/api/theses/semis/pause?token={T}", data={"x": ""}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/thesis/semis")
    t = db.row(conn, "SELECT status, spec FROM theses WHERE id='semis'")
    assert t["status"] == "paused" and json.loads(t["spec"])["status"] == "paused"
    r = web.post("/api/theses/semis/resume")
    assert r.json()["ok"] and db.row(conn, "SELECT status FROM theses")["status"] == "active"
    pushes = [json.loads(j["data"]) for j in db.rows(conn, "SELECT data FROM journal WHERE kind='notify'")]
    assert len(pushes) == 2 and all(p["actions"] is None for p in pushes)  # outcome only, no buttons


# ---------------------------------------------------------------- proposals

def test_confirm_mode_pushes_buttons(conn):
    insert_thesis(config.DB_PATH)
    p = proposals.create(conn, "semis", order_action(), "why not")
    assert p["status"] == "pending"
    push = db.row(conn, "SELECT data FROM journal WHERE kind='notify' ORDER BY id DESC LIMIT 1")
    acts = json.loads(push["data"])["actions"]
    assert [a["label"] for a in acts] == ["Approve", "Reject"]
    assert acts[0]["url"] == f"http://100.64.0.19:8010/api/proposals/{p['id']}/approve?token={T}"
    assert all(a["method"] == "POST" and a["clear"] for a in acts)
    assert StubBroker.placed == []


def test_approve_executes_order_via_api(web, conn):
    insert_thesis(config.DB_PATH)
    p = proposals.create(conn, "semis", order_action(), "add on weakness")
    r = web.post(f"/api/proposals/{p['id']}/approve?token={T}")  # what the ntfy button sends
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["proposal"]["status"] == "executed"
    assert len(StubBroker.placed) == 1
    o = StubBroker.placed[0]
    assert o["thesis_id"] == "semis" and o["symbol"] == "NVDA" and o["notional"] == 25 and o["qty"] is None
    assert o["client_order_id"].startswith("semis-") and o["reason"].startswith(f"proposal #{p['id']}: ")
    statuses = [json.loads(j["data"])["status"] for j in
                db.rows(conn, "SELECT data FROM journal WHERE kind='proposal' ORDER BY id")]
    assert statuses == ["pending", "approved", "executed"]
    # cannot approve twice
    r2 = web.post(f"/api/proposals/{p['id']}/approve?token={T}")
    assert r2.status_code == 409 and "executed" in r2.json()["reason"]
    assert len(StubBroker.placed) == 1


def test_approve_from_html_form_redirects(web, conn):
    insert_thesis(config.DB_PATH)
    p = proposals.create(conn, "semis", order_action(), "x")
    web.get(f"/?token={T}")
    r = web.post(f"/api/proposals/{p['id']}/approve", data={"back": "/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/?msg=")
    assert proposals.get(conn, p["id"])["status"] == "executed"


def test_refused_order_marks_failed(web, conn):
    insert_thesis(config.DB_PATH)
    p = proposals.create(conn, "semis", order_action(), "x")
    StubBroker.refuse = "per-order cap exceeded"
    r = web.post(f"/api/proposals/{p['id']}/approve?token={T}")
    assert r.status_code == 200 and r.json()["ok"] is False
    assert proposals.get(conn, p["id"])["status"] == "failed"
    assert "per-order cap" in r.json()["reason"]


def test_reject(web, conn):
    insert_thesis(config.DB_PATH)
    p = proposals.create(conn, "semis", order_action(), "x")
    r = web.post(f"/api/proposals/{p['id']}/reject?token={T}")
    assert r.status_code == 200 and r.json()["proposal"]["status"] == "rejected"
    assert web.post(f"/api/proposals/{p['id']}/approve?token={T}").status_code == 409
    assert StubBroker.placed == []


def test_expired_proposal_refuses(web, conn):
    insert_thesis(config.DB_PATH)
    p = proposals.create(conn, "semis", order_action(), "x", expires=timedelta(hours=1))
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
    conn.execute("UPDATE proposals SET expires=? WHERE id=?", (past, p["id"]))
    r = web.post(f"/api/proposals/{p['id']}/approve?token={T}")
    assert r.status_code == 409 and "expired" in r.json()["reason"]
    assert proposals.get(conn, p["id"])["status"] == "expired"
    assert StubBroker.placed == []
    assert proposals.list_pending(conn) == []


def test_spec_change_validates(conn):
    insert_thesis(config.DB_PATH)
    with pytest.raises(proposals.ProposalError, match="exceeds capital_cap"):
        proposals.create(conn, "semis", {"type": "spec_change", "patch": {"capital_cap": 40}}, "shrink")
    with pytest.raises(proposals.ProposalError, match="id"):
        proposals.create(conn, "semis", {"type": "spec_change", "patch": {"id": "other"}}, "rename")
    with pytest.raises(proposals.ProposalError):
        proposals.create(conn, "semis", {"type": "spec_change", "patch": {"mode": "yolo"}}, "x")
    assert db.row(conn, "SELECT COUNT(*) n FROM proposals")["n"] == 0
    # valid patch executes and updates row + spec
    p = proposals.create(conn, "semis", {"type": "spec_change", "patch": {"capital_cap": 400, "name": "Semis+"}}, "raise")
    p = proposals.approve(conn, p["id"])
    assert p["status"] == "executed"
    t = db.row(conn, "SELECT name, spec FROM theses")
    assert t["name"] == "Semis+" and json.loads(t["spec"])["capital_cap"] == 400


def test_spec_change_revalidated_at_execution(conn):
    insert_thesis(config.DB_PATH)
    p = proposals.create(conn, "semis", {"type": "spec_change", "patch": {"capital_cap": 60}}, "shrink")
    spec = json.loads(db.row(conn, "SELECT spec FROM theses")["spec"])
    spec["schedule"]["amount"] = 100  # spec changed after the proposal was made
    spec["capital_cap"] = 300
    conn.execute("UPDATE theses SET spec=?", (json.dumps(spec),))
    p = proposals.approve(conn, p["id"])
    assert p["status"] == "failed" and "invalid" in p["result"]["reason"]
    assert json.loads(db.row(conn, "SELECT spec FROM theses")["spec"])["capital_cap"] == 300


def test_bad_actions_refused(conn):
    insert_thesis(config.DB_PATH)
    for a in ({"type": "order", "side": "short", "symbol": "NVDA", "notional": 5},
              {"type": "order", "side": "buy", "symbol": "NVDA", "notional": 5, "qty": 1},
              {"type": "order", "side": "buy", "symbol": "nv da", "notional": 5},
              {"type": "order", "side": "buy", "symbol": "NVDA", "notional": -5},
              {"type": "launch"}):
        with pytest.raises(proposals.ProposalError):
            proposals.create(conn, "semis", a, "x")
    with pytest.raises(proposals.ProposalError, match="unknown thesis"):
        proposals.create(conn, "ghost", {"type": "pause"}, "x")


# ---------------------------------------------------------------- tools/propose.py

def test_propose_cli_auto_mode_executes(conn, capsys):
    from tools import propose
    insert_thesis(config.DB_PATH, mode="auto")
    rc = propose.main(["--thesis", "semis", "--action", json.dumps(order_action(qty=0.1, notional=None)),
                       "--reason", "auto top-up"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"] and out["proposal"]["status"] == "executed"
    assert StubBroker.placed[0]["qty"] == 0.1 and StubBroker.placed[0]["notional"] is None
    pushes = db.rows(conn, "SELECT data FROM journal WHERE kind='notify'")
    assert pushes == []  # an executed order is pushed by the broker (fill/refusal), not here


def test_propose_cli_refusals(capsys):
    from tools import propose
    insert_thesis(config.DB_PATH)
    assert propose.main(["--thesis", "semis", "--action", "{nope", "--reason", "x"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert propose.main(["--thesis", "semis", "--action", '{"type":"spec_change","patch":{"capital_cap":1}}',
                         "--reason", "x"]) == 1
    assert "invalid" in json.loads(capsys.readouterr().out)["reason"]
    assert propose.main(["--thesis", "semis", "--action", '{"type":"pause"}', "--reason", "x", "--expires", "2h"]) == 0
    assert json.loads(capsys.readouterr().out)["proposal"]["status"] == "pending"


# ---------------------------------------------------------------- tools/review.py

def test_review_pack_with_too_few_snapshots(conn):
    from tools import review
    insert_thesis(config.DB_PATH, source_text="invest in big semis, 300 max")
    add_filled(conn)
    today = datetime.now(timezone.utc).date().isoformat()
    conn.execute("INSERT INTO snapshots(date, symbol, thesis_id, market_value) VALUES (?,?,?,?)", (today, "$ACCOUNT", None, 1000))
    conn.execute("INSERT INTO news(ts, symbol, headline, sentiment, relevance, summary) VALUES (?,?,?,?,?,?)",
                 (db.now_iso(), "NVDA", "Nvidia cuts guidance", "negative", 9, "cut"))
    db.journal(conn, "order", "bought NVDA", thesis_id="semis")
    pack = review.build_pack(conn, "semis", trigger={"type": "drawdown_pct", "symbol": "NVDA"})
    assert "Only 1 snapshot day(s)" in pack and "Do not estimate" in pack
    perf = pack.split("## Performance")[1].split("##")[0]
    assert "%" not in perf and "$" not in perf  # no invented numbers
    assert "invest in big semis, 300 max" in pack and "never more than $300 total" in pack
    assert "Nvidia cuts guidance" in pack and "bought NVDA" in pack and '"drawdown_pct"' in pack
    assert "| NVDA |" in pack and "$60.00" in pack  # 0.5 sh x $120 from the stub broker


def test_review_wash_sale_warning(conn):
    from tools import review
    insert_thesis(config.DB_PATH)
    add_filled(conn, side="sell", notional=20, qty=0.2)
    pack = review.build_pack(conn, "semis")
    assert "WARNING" in pack and "NVDA" in pack.split("## Wash-sale check")[1]


def review_doc(rec="HOLD", lines=("Rationale intact.",), proposal=None, **kw):
    """A `claude -p --output-format json --json-schema` result document."""
    so = {"recommendation": rec, "lines": list(lines), "proposal": proposal}
    return dict(RESULT_DOC, result=json.dumps(so), structured_output=so, session_id="sess-r", **kw)


def fake_claude(monkeypatch, *docs):
    """Replace subprocess.run (used by web/chat.run_json) with a stub returning docs in turn.
    Returns the list of calls (cmd, kwargs)."""
    calls = []

    def run(cmd, **kw):
        calls.append((cmd, kw))
        d = docs[min(len(calls), len(docs)) - 1]
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(d), stderr="")

    monkeypatch.setattr(chat.subprocess, "run", run)
    return calls


def test_review_runs_claude_and_journals(conn, monkeypatch, capsys):
    from tools import review
    insert_thesis(config.DB_PATH)
    add_filled(conn)
    calls = fake_claude(monkeypatch, review_doc())
    assert review.main(["--thesis", "semis"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["review"] == "HOLD\nRationale intact." and out["recommendation"] == "HOLD" and out["proposal"] is None
    cmd, kw = calls[0]
    assert kw["cwd"] == str(config.ROOT) and kw["timeout"] == 600 and "--output-format" in cmd
    # single turn, no tools, structured output, not resumable
    assert cmd[cmd.index("--tools") + 1] == "" and "--allowedTools" not in cmd and "--no-session-persistence" in cmd
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == review.REVIEW_SCHEMA
    assert "--append-system-prompt" not in cmd  # the pack is self-contained
    assert kw["env"]["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    assert "tools/propose.py" not in kw["input"] and "# Review: Semis" in kw["input"]
    assert '"type":"pause"' in kw["input"]  # action formats are spelled out
    j = db.row(conn, "SELECT message, data FROM journal WHERE kind='review'")
    assert j["message"].startswith("HOLD")
    assert json.loads(j["data"])["prices"] == {"NVDA": 120.0}  # baseline for the next skip check
    assert db.get_setting(conn, "last_review:semis")
    push = json.loads(db.row(conn, "SELECT data FROM journal WHERE kind='notify'")["data"])
    assert push["title"] == "Review: semis"
    assert review.due_theses(conn) == []  # just reviewed


def test_review_exit_creates_proposal(conn, monkeypatch, capsys):
    from tools import review
    insert_thesis(config.DB_PATH)
    add_filled(conn)
    sell = {"action": {"type": "order", "side": "sell", "symbol": "nvda", "qty": 0.5}, "reason": "guidance cut"}
    fake_claude(monkeypatch, review_doc("EXIT", ["Thesis broken."], sell))
    assert review.main(["--thesis", "semis"]) == 0
    out = json.loads(capsys.readouterr().out)
    p = out["proposal"]
    assert p["status"] == "pending" and p["action"]["symbol"] == "NVDA" and p["reason"] == "review: guidance cut"
    assert out["review"].endswith(f"Proposal #{p['id']} (pending): sell 0.5 sh NVDA")
    assert StubBroker.placed == []  # confirm mode: nothing executes without a tap
    assert json.loads(db.row(conn, "SELECT data FROM journal WHERE kind='review'")["data"])["proposal_id"] == p["id"]
    titles = [json.loads(r["data"])["title"] for r in db.rows(conn, "SELECT data FROM journal WHERE kind='notify' ORDER BY id")]
    assert titles == [f"Proposal #{p['id']}", "Review: semis"]


def test_review_proposal_refused_or_ignored(conn, monkeypatch, capsys):
    from tools import review
    insert_thesis(config.DB_PATH)
    bad = {"action": {"type": "spec_change", "patch": {"capital_cap": -5}}, "reason": "x"}
    fake_claude(monkeypatch, review_doc("CHANGE", ["Shrink it."], bad))
    assert review.main(["--thesis", "semis", "--force"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["proposal"] is None and "Proposal refused: patched spec is invalid" in out["review"]
    assert db.rows(conn, "SELECT * FROM proposals") == []
    fake_claude(monkeypatch, review_doc("HOLD", ["Fine."], {"action": {"type": "pause"}, "reason": "y"}))
    assert review.main(["--thesis", "semis", "--force"]) == 0
    assert "proposal ignored" in json.loads(capsys.readouterr().out)["review"]
    assert db.rows(conn, "SELECT * FROM proposals") == []


def test_review_bad_structured_output_fails(conn, monkeypatch, capsys):
    from tools import review
    insert_thesis(config.DB_PATH)
    doc = dict(RESULT_DOC, result="HOLD, looks fine")  # no structured_output, result not JSON
    fake_claude(monkeypatch, doc)
    assert review.main(["--thesis", "semis"]) == 1
    assert "no structured output" in json.loads(capsys.readouterr().out)["reason"]
    fake_claude(monkeypatch, review_doc("MAYBE"))
    assert review.main(["--thesis", "semis"]) == 1
    assert "bad recommendation" in json.loads(capsys.readouterr().out)["reason"]
    assert db.rows(conn, "SELECT * FROM journal WHERE kind='review'") == []
    assert db.row(conn, "SELECT COUNT(*) n FROM claude_usage")["n"] == 2  # still counted


def test_review_skips_when_nothing_changed(conn, monkeypatch, capsys):
    from tools import review
    insert_thesis(config.DB_PATH)
    add_filled(conn)
    calls = fake_claude(monkeypatch, review_doc())
    assert review.main(["--thesis", "semis"]) == 0  # no earlier review: runs
    capsys.readouterr()
    assert review.main(["--thesis", "semis"]) == 0  # nothing since: skipped, no claude
    assert json.loads(capsys.readouterr().out)["skipped"] is True and len(calls) == 1
    js = db.rows(conn, "SELECT message, data FROM journal WHERE kind='review' ORDER BY id")
    assert js[-1]["message"] == "skipped: nothing changed" and json.loads(js[-1]["data"])["skipped"] is True
    assert review.main(["--thesis", "semis", "--force"]) == 0 and len(calls) == 2
    assert review.main(["--thesis", "semis", "--trigger", '{"type":"drawdown_pct"}']) == 0 and len(calls) == 3
    capsys.readouterr()

    # each kind of material change triggers a real review (the skip baseline is the last real one)
    db.journal(conn, "fill", "bought NVDA", thesis_id="semis")
    assert review.main(["--thesis", "semis"]) == 0 and len(calls) == 4
    db.journal(conn, "news", "FLAGGED AMD. ...", data={"flagged": ["AMD"]})
    assert review.material_changes(conn, review._load(conn, "semis")[2], [], None) == ["news flagged AMD"]
    db.journal(conn, "news", "FLAGGED XOM.", data={"flagged": ["XOM"]})  # not in the universe
    spec = review._load(conn, "semis")[2]
    assert review.main(["--thesis", "semis"]) == 0 and len(calls) == 5
    assert review.material_changes(conn, spec, [{"symbol": "NVDA", "qty": 0.5, "price": 125.0}]) == []  # +4.2%
    assert review.material_changes(conn, spec, [{"symbol": "NVDA", "qty": 0.5, "price": 126.0}]) == \
        ["NVDA moved +5.0% since last review"]
    assert review.material_changes(conn, spec, [{"symbol": "NVDA", "qty": 0.5, "price": None}]) == \
        ["NVDA: no price to compare"]
    assert review.material_changes(conn, spec, [{"symbol": "AMD", "qty": 0.0, "price": None}]) == []  # not held


def test_review_skip_restarts_cadence(conn, monkeypatch, capsys):
    from tools import review
    insert_thesis(config.DB_PATH)
    calls = fake_claude(monkeypatch, review_doc())
    review.main(["--thesis", "semis"])
    conn.execute("UPDATE settings SET value='2026-01-01T00:00:00+00:00' WHERE key='last_review:semis'")
    assert review.due_theses(conn) == ["semis"]
    capsys.readouterr()
    assert review.main(["--all"]) == 0 and len(calls) == 1  # due, but nothing changed: skipped
    assert json.loads(capsys.readouterr().out)["results"][0]["skipped"] is True
    assert review.due_theses(conn) == []


def test_review_all_due(conn):
    from tools import review
    insert_thesis(config.DB_PATH)
    conn.execute("UPDATE theses SET created='2026-01-01T00:00:00+00:00'")
    assert review.due_theses(conn) == ["semis"]
    db.set_setting(conn, "last_review:semis", db.now_iso())
    assert review.due_theses(conn) == []


# ---------------------------------------------------------------- chat

def test_chat_endpoint_stores_both_and_resumes(web, conn, monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        if "--resume" in cmd and cmd[cmd.index("--resume") + 1] == "stale":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="No conversation found")
        sid = "sess-2" if len(calls) > 1 else "sess-1"
        out = {"type": "result", "is_error": False, "result": f"reply {len(calls)}\nline two", "session_id": sid}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(out), stderr="")

    monkeypatch.setattr(chat.subprocess, "run", fake_run)
    r = web.post(f"/api/chat?token={T}", json={"message": "how are my semis?"})
    assert r.status_code == 200 and r.json()["reply"] == "reply 1\nline two"
    cmd, kw = calls[0]
    assert cmd[1:4] == ["-p", "--output-format", "json"] and "--resume" not in cmd
    assert kw["input"] == "how are my semis?" and kw["cwd"] == str(config.ROOT) and kw["timeout"] == 300
    msgs = db.rows(conn, "SELECT role, content FROM chat ORDER BY id")
    assert msgs == [{"role": "user", "content": "how are my semis?"}, {"role": "assistant", "content": "reply 1\nline two"}]
    assert db.get_setting(conn, "chat_session") == "sess-1"

    web.post("/api/chat", json={"message": "and now?"})
    assert calls[1][0][-2:] == ["--resume", "sess-1"]

    db.set_setting(conn, "chat_session", "stale")  # resume fails -> fresh session
    r = web.post("/api/chat", json={"message": "again"})
    assert r.json()["ok"] and r.json()["resumed"] is False and "--resume" not in calls[-1][0]
    page = web.get("/chat").text
    assert "how are my semis?" in page and "reply 1\nline two" in page
    assert web.post("/api/chat", json={"nope": 1}).status_code == 422


def test_chat_form_posts_and_redirects(web, conn, monkeypatch):
    monkeypatch.setattr(chat.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, stdout=json.dumps({"result": "ok!", "session_id": "s"}), stderr=""))
    web.get(f"/chat?token={T}")
    r = web.post("/chat", data={"message": "hi"}, follow_redirects=False)
    assert r.status_code == 303
    assert [m["role"] for m in chat.history(conn)] == ["user", "assistant"]


def test_chat_timeout_recorded_as_error(conn, monkeypatch):
    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 300)
    monkeypatch.setattr(chat.subprocess, "run", boom)
    res = chat.send(conn, "slow question")
    assert res["ok"] is False and "5 minutes" in res["reply"]
    assert [m["role"] for m in chat.history(conn)] == ["user", "error"]


# ---------------------------------------------------------------- claude model pin + usage tracking

RESULT_DOC = {
    "type": "result", "is_error": False, "result": "we hold NVDA", "session_id": "sess-u", "num_turns": 3,
    "duration_ms": 4200, "total_cost_usd": 0.0123,
    "usage": {"input_tokens": 10, "cache_creation_input_tokens": 500, "cache_read_input_tokens": 9000, "output_tokens": 80},
    "modelUsage": {"claude-sonnet-5-5": {"costUSD": 0.0123}},
}


def test_claude_cmd_pins_model(monkeypatch):
    monkeypatch.setattr(config, "CLAUDE_MODEL", "sonnet")
    cmd = chat.claude_cmd("json", resume="s1")
    assert cmd[1:4] == ["-p", "--output-format", "json"]
    assert cmd[cmd.index("--model") + 1] == "sonnet" and cmd[-2:] == ["--resume", "s1"]
    monkeypatch.setattr(config, "CLAUDE_MODEL", "")  # inherit the CLI default
    assert "--model" not in chat.claude_cmd("json")


def test_claude_model_per_caller(monkeypatch):
    monkeypatch.setattr(config, "CLAUDE_MODEL", "sonnet")
    monkeypatch.setattr(config, "CLAUDE_MODEL_REVIEW", "haiku")
    monkeypatch.setattr(config, "CLAUDE_MODEL_CHAT", None)
    model = lambda c: c[c.index("--model") + 1]  # noqa: E731
    assert model(chat.claude_cmd("json", caller="review")) == "haiku"
    assert model(chat.claude_cmd("json")) == "sonnet"  # unset -> CLAUDE_MODEL
    monkeypatch.setattr(config, "CLAUDE_MODEL_CHAT", "")  # explicit "" -> CLI default
    assert "--model" not in chat.claude_cmd("json")


def test_claude_cmd_lockdown(monkeypatch):
    for k in ("CLAUDE_ALLOWED_TOOLS", "CLAUDE_TOOLS_CHAT", "CLAUDE_LOCKDOWN", "CLAUDE_DISALLOWED_TOOLS_CHAT"):
        monkeypatch.delenv(k, raising=False)
    cmd = chat.claude_cmd("json")
    for flag in ("--strict-mcp-config", "--disable-slash-commands", "--exclude-dynamic-system-prompt-sections"):
        assert flag in cmd
    assert "--mcp-config" not in cmd and "--bare" not in cmd  # --bare would break subscription auth
    assert cmd[cmd.index("--setting-sources") + 1] == "project,local"
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert cmd[cmd.index("--tools") + 1] == "Bash,Read,Grep,Glob"
    allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
    assert [t for t in allowed if t.startswith("Bash")] == ["Bash(.venv/bin/python tools/*)"]
    assert cmd[cmd.index("--disallowedTools") + 1] == "Read(./config/*.env)"
    # user CLAUDE.md discovery is off, so the project CLAUDE.md is passed explicitly
    sp = cmd[cmd.index("--append-system-prompt") + 1]
    assert "You are the broker for my personal thesis-trading system" in sp
    env = chat.claude_env()
    assert env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false" and env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"
    assert "--no-session-persistence" not in cmd  # chat sessions are resumed
    monkeypatch.setenv("CLAUDE_TOOLS_CHAT", "Bash")
    monkeypatch.setenv("CLAUDE_ALLOWED_TOOLS", "Bash(.venv/bin/python tools/positions.py)")
    cmd = chat.claude_cmd("json")
    assert cmd[cmd.index("--tools") + 1] == "Bash"
    assert cmd[cmd.index("--allowedTools") + 1] == "Bash(.venv/bin/python tools/positions.py)"
    monkeypatch.setenv("CLAUDE_LOCKDOWN", "0")
    cmd = chat.claude_cmd("json")
    assert "--strict-mcp-config" not in cmd and "--tools" not in cmd and "--append-system-prompt" not in cmd
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_CLAUDE_MDS", raising=False)
    assert "CLAUDE_CODE_DISABLE_CLAUDE_MDS" not in chat.claude_env()


def test_chat_rotates_session(conn, monkeypatch):
    sids = iter(f"s{i}" for i in range(1, 50))
    calls = []

    def run(cmd, **kw):
        calls.append((cmd, kw))
        doc = dict(RESULT_DOC, result=f"answer {len(calls)}", session_id=next(sids) if "--resume" not in cmd
                   else cmd[cmd.index("--resume") + 1])
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(doc), stderr="")

    monkeypatch.setattr(chat.subprocess, "run", run)
    monkeypatch.setattr(chat, "CHAT_MAX_TURNS", 3)
    for i in range(3):
        r = chat.send(conn, f"q{i}")
        assert r["ok"] and r["rotated"] is None
    assert ["--resume" in c for c, _ in calls] == [False, True, True]
    assert calls[0][1]["input"] == "q0"  # nothing to recap on the very first message
    assert db.get_setting(conn, "chat_session_turns") == "3"
    r = chat.send(conn, "q3")
    assert r["rotated"] == "3 messages in session" and not r["resumed"] and "--resume" not in calls[-1][0]
    recap = calls[-1][1]["input"]
    assert recap.startswith("[Fresh session") and "me: q2" in recap and "you: answer 3" in recap
    assert recap.endswith("[End of recap]\n\nq3") and recap.count("q3") == 1
    assert db.get_setting(conn, "chat_session") == "s2" and db.get_setting(conn, "chat_session_turns") == "1"

    # context threshold: RESULT_DOC averages 9510 tokens over 3 requests -> 3170
    assert db.get_setting(conn, "chat_session_context") == "3170"
    monkeypatch.setattr(chat, "CHAT_MAX_CONTEXT_TOKENS", 3000)
    assert chat.send(conn, "q4")["rotated"] == "context reached 3170 tokens"


def test_context_tokens_uses_last_request():
    doc = {"num_turns": 2, "usage": {"input_tokens": 4, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 50,
                                     "iterations": [{"input_tokens": 2, "cache_read_input_tokens": 40},
                                                    {"input_tokens": 2, "cache_read_input_tokens": 60,
                                                     "cache_creation_input_tokens": 50}]}}
    assert chat.context_tokens(doc) == 112
    assert chat.context_tokens(None) == 0


def test_chat_records_usage(web, conn, monkeypatch):
    monkeypatch.setattr(chat.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, stdout=json.dumps(RESULT_DOC), stderr=""))
    r = web.post(f"/api/chat?token={T}", json={"message": "what do we hold?"})
    assert r.json()["ok"] and r.json()["cost_usd"] == 0.0123
    u = db.rows(conn, "SELECT * FROM claude_usage")
    assert len(u) == 1
    assert u[0]["caller"] == "chat" and u[0]["model"] == "claude-sonnet-5-5" and u[0]["ok"] == 1
    assert (u[0]["input_tokens"], u[0]["cache_write"], u[0]["cache_read"], u[0]["output_tokens"]) == (10, 500, 9000, 80)
    assert u[0]["cost_usd"] == 0.0123 and u[0]["turns"] == 3 and u[0]["session_id"] == "sess-u"
    # a mocked reply without usage fields records nothing
    monkeypatch.setattr(chat.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, stdout=json.dumps({"result": "ok", "session_id": "sess-u"}), stderr=""))
    web.post("/api/chat", json={"message": "again"})
    assert len(db.rows(conn, "SELECT * FROM claude_usage")) == 1


def test_usage_cost_is_delta_over_resumed_session(conn):
    """total_cost_usd and modelUsage are cumulative when a session is resumed; usage tokens are per call."""
    first = dict(RESULT_DOC, total_cost_usd=0.40, modelUsage={"claude-fable-5-1": {"costUSD": 0.40}})
    r1 = chat.record_usage(conn, "chat", first)
    assert r1["cost_usd"] == 0.40 and r1["model"] == "claude-fable-5-1"
    second = dict(RESULT_DOC, total_cost_usd=0.43, modelUsage={"claude-fable-5-1": {"costUSD": 0.40},
                                                               "claude-sonnet-5-5": {"costUSD": 0.03}})
    r2 = chat.record_usage(conn, "chat", second)
    assert r2["cost_usd"] == 0.03 and r2["model"] == "claude-sonnet-5-5"  # only the model that moved
    assert r2["output_tokens"] == 80 and r2["session_cost_usd"] == 0.43
    other = dict(RESULT_DOC, session_id="fresh", total_cost_usd=0.02)
    assert chat.record_usage(conn, "review", other)["cost_usd"] == 0.02  # new session: no baseline to subtract
    assert db.row(conn, "SELECT ROUND(SUM(cost_usd),4) s FROM claude_usage")["s"] == 0.45


def test_review_records_usage_from_json(conn, monkeypatch, capsys):
    from tools import review
    insert_thesis(config.DB_PATH)
    fake_claude(monkeypatch, review_doc("HOLD", ["Fine."]))
    assert review.main(["--thesis", "semis"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["review"].startswith("HOLD") and out["cost_usd"] == 0.0123
    u = db.row(conn, "SELECT * FROM claude_usage")
    assert u["caller"] == "review" and u["output_tokens"] == 80
    data = json.loads(db.row(conn, "SELECT data FROM journal WHERE kind='review'")["data"])
    assert data["cost_usd"] == 0.0123 and data["model"] == "claude-sonnet-5-5"


def test_review_json_error_is_failure(conn, monkeypatch, capsys):
    from tools import review
    insert_thesis(config.DB_PATH)
    doc = dict(RESULT_DOC, is_error=True, result="rate limited")
    monkeypatch.setattr(chat.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 1, stdout=json.dumps(doc), stderr=""))
    assert review.main(["--thesis", "semis"]) == 1
    assert "rate limited" in json.loads(capsys.readouterr().out)["reason"]
    assert db.row(conn, "SELECT ok FROM claude_usage")["ok"] == 0  # failed call still counted
    assert db.row(conn, "SELECT * FROM journal WHERE kind='error'")


def test_usage_tool_and_dashboard(web, conn, monkeypatch, capsys):
    from tools import usage
    chat.record_usage(conn, "chat", RESULT_DOC)
    chat.record_usage(conn, "review", dict(RESULT_DOC, session_id="sess-r", total_cost_usd=0.5, modelUsage={"claude-opus-5-5": {"costUSD": 0.5}}))
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(timespec="seconds")
    conn.execute("INSERT INTO claude_usage(ts, caller, cost_usd, output_tokens) VALUES (?,?,?,?)", (old, "chat", 9.0, 1))
    conn.commit()
    monkeypatch.setattr(config, "CLAUDE_BUDGET_USD_WEEK", 2.0)
    assert usage.main([]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["windows"]["7d"]["calls"] == 2 and out["windows"]["7d"]["cost_usd"] == 0.5123
    assert out["windows"]["30d"]["calls"] == 3 and out["windows"]["30d"]["cost_usd"] == 9.5123
    assert out["week_pct_of_budget"] == 25.6 and out["over_budget"] is False
    assert {r["key"]: r["cost_usd"] for r in out["by_caller_7d"]} == {"review": 0.5, "chat": 0.0123}
    assert [r["key"] for r in out["by_model_7d"]] == ["claude-opus-5-5", "claude-sonnet-5-5"]
    page = web.get(f"/?token={T}").text
    assert "Claude (" in page and "2 calls" in page and "25.6% of $2.00/wk" in page
    monkeypatch.setattr(config, "CLAUDE_BUDGET_USD_WEEK", 0.0)
    st = web.get("/api/state").json()["claude"]
    assert st["budget_usd_week"] is None and st["week_pct_of_budget"] is None


def test_review_ignores_routine_dca_fills(conn, monkeypatch, capsys):
    from tools import review
    from trader.schedule import client_order_id
    insert_thesis(config.DB_PATH)
    add_filled(conn)
    calls = fake_claude(monkeypatch, review_doc())
    assert review.main(["--thesis", "semis"]) == 0 and len(calls) == 1
    spec = review._load(conn, "semis")[2]
    slot = "2026-10-26T10:00"
    for sym in spec.universe:
        coid = client_order_id("semis", sym, slot)
        db.journal(conn, "order", f"placed {sym}", thesis_id="semis", data={"client_order_id": coid})
        db.journal(conn, "fill", f"filled {sym}", thesis_id="semis", data={"client_order_id": coid})
    db.journal(conn, "dca", f"slot {slot}: bought", thesis_id="semis", data={"slot": slot})
    assert review.material_changes(conn, spec, []) == []  # routine slot: not material
    db.journal(conn, "refused", "NVDA refused", thesis_id="semis")
    db.journal(conn, "fill", "manual NVDA", thesis_id="semis",
               data={"client_order_id": client_order_id("semis", "NVDA", "2026-10-27T11:02:03")})
    assert review.material_changes(conn, spec, []) == ["1 new fill entry", "1 new refused entry"]
    capsys.readouterr()


def test_review_proposal_on_auto_thesis_waits_for_tap(conn, monkeypatch, capsys):
    from tools import review
    insert_thesis(config.DB_PATH, mode="auto")
    approved = []
    monkeypatch.setattr(review.proposals, "approve", lambda *a, **k: approved.append(a) or {})
    fake_claude(monkeypatch, review_doc("CHANGE", proposal={"action": {"type": "pause"}, "reason": "thesis broke"}))
    assert review.main(["--thesis", "semis", "--force"]) == 0
    p = db.rows(conn, "SELECT status FROM proposals ORDER BY id DESC LIMIT 1")
    assert approved == [] and p and p[0]["status"] == "pending"
    capsys.readouterr()
