"""WEB_BASIC_AUTH: optional HTTP Basic layer in front of the web app, /healthz, and the ntfy
action headers that keep Approve/Reject working through it."""
from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from trader import client as tclient
from trader import config, db, notify
from web import app as webapp
from web import proposals

from conftest import insert_thesis
from test_web import StubBroker

TOKEN = "tok"
CREDS = "alice:s3cret,pass"          # comma on purpose: it must survive ntfy quoting


def basic(user_pass: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(user_pass.encode()).decode()}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr(config, "WEB_TOKEN", TOKEN)
    monkeypatch.setattr(config, "WEB_URL", "https://trader.example.ts.net")
    monkeypatch.setattr(tclient, "BrokerClient", StubBroker)
    StubBroker.placed, StubBroker.down, StubBroker.refuse = [], False, None


@pytest.fixture
def secured(monkeypatch):
    monkeypatch.setattr(config, "WEB_BASIC_AUTH", CREDS)
    return TestClient(webapp.create_app())


@pytest.fixture
def plain(monkeypatch):
    monkeypatch.setattr(config, "WEB_BASIC_AUTH", "")
    return TestClient(webapp.create_app())


# ---------------------------------------------------------------- off: unchanged

def test_unset_behaves_as_before(plain):
    assert plain.get("/").status_code == 401
    assert "www-authenticate" not in plain.get("/").headers
    assert plain.get("/api/state").status_code == 401
    assert plain.get(f"/?token={TOKEN}").status_code == 200


def test_healthz_open_without_basic_auth_too(plain):
    r = plain.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


# ---------------------------------------------------------------- on

def test_every_route_needs_basic(secured):
    for path in ("/", f"/?token={TOKEN}", "/journal", "/api/state", "/static/manifest.webmanifest"):
        r = secured.get(path)
        assert r.status_code == 401, path
        assert r.headers["www-authenticate"].startswith("Basic ")
    assert secured.post("/api/proposals/1/approve").status_code == 401


def test_basic_plus_token_is_200(secured):
    r = secured.get(f"/?token={TOKEN}", headers=basic(CREDS))
    assert r.status_code == 200
    assert secured.get("/journal", headers=basic(CREDS)).status_code == 200   # cookie from the first visit


def test_wrong_password_or_user_is_401(secured):
    for bad in ("alice:nope", "bob:s3cret,pass", "alice:", ":", "alice"):
        r = secured.get(f"/?token={TOKEN}", headers=basic(bad))
        assert r.status_code == 401 and "www-authenticate" in r.headers, bad
    assert secured.get(f"/?token={TOKEN}", headers={"Authorization": "Basic !!!notbase64"}).status_code == 401
    assert secured.get(f"/?token={TOKEN}", headers={"Authorization": "Bearer abc"}).status_code == 401


def test_token_still_required_with_valid_basic(secured):
    r = secured.get("/", headers=basic(CREDS))
    assert r.status_code == 401 and "www-authenticate" not in r.headers   # no Basic re-prompt loop
    assert secured.get("/?token=wrong", headers=basic(CREDS)).status_code == 401
    r = secured.post("/api/proposals/1/approve", headers=basic(CREDS))
    assert r.status_code == 401 and r.json()["reason"] == "bad or missing token"


def test_healthz_open_and_reveals_nothing(secured):
    r = secured.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert CREDS not in r.text and TOKEN not in r.text


def test_malformed_setting_refuses_to_start(monkeypatch):
    monkeypatch.setattr(config, "WEB_BASIC_AUTH", "nocolon")
    with pytest.raises(RuntimeError):
        webapp.create_app()


# ---------------------------------------------------------------- ntfy actions

def test_proposal_actions_carry_authorization_header(monkeypatch, conn):
    monkeypatch.setattr(config, "WEB_BASIC_AUTH", CREDS)
    monkeypatch.setattr(config, "NTFY_TOPIC", "topic")
    insert_thesis(config.DB_PATH)
    sent = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        sent.update(url=url, headers=headers)

        class R:
            ok = True
        return R()

    monkeypatch.setattr(notify.requests, "post", fake_post)
    p = proposals.create(conn, "semis", {"type": "order", "side": "buy", "symbol": "NVDA", "notional": 25}, "test")
    assert p["status"] == "pending"
    want = "Basic " + base64.b64encode(CREDS.encode()).decode()
    actions = sent["headers"]["Actions"]
    assert actions.count(f"headers.Authorization={want}") == 2
    assert "method=POST" in actions and "clear=true" in actions
    assert f"/api/proposals/{p['id']}/approve?token={TOKEN}" in actions
    # the credential must not be stored in the journal
    rows = conn.execute("SELECT data FROM journal WHERE kind='notify'").fetchall()
    assert rows and all(base64.b64encode(CREDS.encode()).decode() not in (r["data"] or "") for r in rows)


def test_no_header_when_basic_auth_off(monkeypatch, conn):
    monkeypatch.setattr(config, "WEB_BASIC_AUTH", "")
    monkeypatch.setattr(config, "NTFY_TOPIC", "topic")
    insert_thesis(config.DB_PATH)
    sent = {}
    monkeypatch.setattr(notify.requests, "post", lambda url, data=None, headers=None, timeout=None:
                        sent.update(headers=headers) or type("R", (), {"ok": True})())
    proposals.create(conn, "semis", {"type": "order", "side": "buy", "symbol": "NVDA", "notional": 25}, "test")
    assert "headers." not in sent["headers"]["Actions"]


def test_fmt_action_matches_ntfy_format():
    a = {"action": "http", "label": "Approve", "url": "https://x/y?token=t", "method": "POST",
         "headers": {"Authorization": "Basic YTpi"}, "clear": True}
    assert notify._fmt_action(a) == \
        "http, Approve, https://x/y?token=t, method=POST, headers.Authorization=Basic YTpi, clear=true"
    # values with a comma or semicolon are quoted so ntfy does not split them
    a2 = {"action": "http", "label": "Go, now", "url": "https://x", "headers": {"X-A": "p,q"}, "body": {"a": 1, "b": 2}}
    out = notify._fmt_action(a2)
    assert "'Go, now'" in out and "headers.X-A='p,q'" in out and """body='{"a": 1, "b": 2}'""" in out


def test_journal_redaction_helper():
    acts = [{"label": "A", "headers": {"Authorization": "Basic zzz"}}, {"label": "B"}]
    red = notify._redact(acts)
    assert red[0]["headers"] == {"Authorization": "***"} and red[1] == {"label": "B"}
    assert acts[0]["headers"]["Authorization"] == "Basic zzz"     # original untouched


@pytest.fixture
def conn():
    c = db.connect()
    yield c
    c.close()
