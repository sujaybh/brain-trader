"""tools/news.py: RSS parsing from fixtures, de-dup/since filtering, defensive digest coercion,
Ollama fallback, storage, and news triggers. No network: feeds and Ollama are mocked."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from conftest import insert_thesis
from tools import news
from trader import config, db

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 16, 0, 0, tzinfo=timezone.utc)


def yahoo():
    return news.parse_rss((FIX / "yahoo_nvda.xml").read_bytes(), "Yahoo Finance")


def google():
    return news.parse_rss((FIX / "google_nvda.xml").read_bytes(), "Google News")


def test_parse_yahoo_fixture():
    items = yahoo()
    assert [i["headline"] for i in items] == [
        "Nvidia cuts data-center guidance for Q4",
        "Chip stocks tumble; are investors just repositioning?",
        "Ten days old: Nvidia at a conference",
    ]  # the title-less item is dropped
    assert items[0]["published"] == "2026-10-15T20:21:07+00:00"
    assert items[0]["source"] == "Yahoo Finance"
    assert items[0]["url"].startswith("https://finance.yahoo.com/")


def test_parse_google_strips_publisher_suffix():
    items = google()
    assert items[0]["headline"] == "Nvidia Cuts Data-Center Guidance for Q4!"
    assert items[0]["source"] == "Reuters"
    assert items[1]["headline"] == "Nvidia shares fall as AI trade cools"
    assert items[2]["published"] is None


def test_merge_dedups_and_filters_since():
    merged = news.merge_headlines([yahoo(), google()], timedelta(hours=24), limit=15, now=NOW)
    heads = [m["headline"] for m in merged]
    assert "Ten days old: Nvidia at a conference" not in heads           # older than --since
    assert sum("guidance" in h.lower() for h in heads) == 1               # de-duped across feeds
    assert heads[0] == "Nvidia cuts data-center guidance for Q4"          # first feed wins, newest first
    assert heads[-1] == "Undated story about NVDA"                        # undated kept, sorted last
    assert len(merged) == 4
    assert len(news.merge_headlines([yahoo(), google()], timedelta(hours=24), limit=2, now=NOW)) == 2


def test_parse_since():
    assert news.parse_since("24h") == timedelta(hours=24)
    assert news.parse_since("3d") == timedelta(days=3)
    with pytest.raises(ValueError):
        news.parse_since("yesterday")


HEADS = [{"headline": "Nvidia cuts data-center guidance for Q4", "url": "u1", "published": "2026-10-15T20:21:07+00:00", "source": "Yahoo Finance"},
         {"headline": "Chip stocks tumble", "url": "u2", "published": "2026-10-15T18:00:00+00:00", "source": "Yahoo Finance"},
         {"headline": "Nvidia shares fall as AI trade cools", "url": "u3", "published": None, "source": "Reuters"}]

GOOD = {"items": [
    {"i": 0, "headline": "Nvidia cuts data-center guidance for Q4", "sentiment": "negative", "relevance": 9,
     "summary": "Guidance cut for data center."},
    {"i": 1, "headline": "Chip stocks tumble", "sentiment": "negative", "relevance": 3, "summary": "Sector move."},
    {"i": 2, "headline": "Nvidia shares fall as AI trade cools", "sentiment": "negative", "relevance": 5, "summary": "Shares fall."}],
    "overall": {"sentiment": "negative", "summary": "Guidance cut dominates.", "flag": True, "flag_reason": "Q4 guidance cut"}}


def test_coerce_good_digest():
    d = news.coerce_digest(json.dumps(GOOD), HEADS)
    assert d["overall"] == {"sentiment": "negative", "summary": "Guidance cut dominates.", "flag": True,
                            "flag_reason": "Q4 guidance cut"}
    assert [i["relevance"] for i in d["items"]] == [9, 5, 3]  # sorted by relevance
    assert d["items"][0]["url"] == "u1"                      # our metadata, not the model's


def test_coerce_malformed_digest():
    raw = {"items": [
        "not a dict",
        {"headline": "A story the model invented", "sentiment": "positive", "relevance": 10},
        {"headline": "nvidia CUTS data center guidance for q4", "sentiment": "Bearish", "relevance": "15",
         "summary": " ".join(["word"] * 30)},
        {"i": "1", "sentiment": "ecstatic", "relevance": -3},
        {"i": 0, "sentiment": "positive", "relevance": 1},       # duplicate of item 0: ignored
    ], "overall": {"sentiment": None, "summary": " ".join(["w"] * 60), "flag": "yes"}}
    d = news.coerce_digest("<think>hmm</think>```json\n" + json.dumps(raw) + "\n```", HEADS)
    by = {i["headline"]: i for i in d["items"]}
    assert "A story the model invented" not in by
    g = by["Nvidia cuts data-center guidance for Q4"]
    assert (g["sentiment"], g["relevance"]) == ("negative", 10)
    assert g["summary"].endswith("…") and len(g["summary"].split()) == 20
    c = by["Chip stocks tumble"]
    assert (c["sentiment"], c["relevance"], c["summary"]) == ("neutral", 0, "")
    s = by["Nvidia shares fall as AI trade cools"]                # model skipped it: kept undigested
    assert s["sentiment"] is None and s["relevance"] is None
    assert d["overall"]["sentiment"] == "neutral" and d["overall"]["flag"] is True
    assert len(d["overall"]["summary"].split()) == 40


def test_coerce_rejects_non_object():
    with pytest.raises(ValueError):
        news.coerce_digest("I'm sorry, here are the results: ...", HEADS)
    with pytest.raises(ValueError):
        news.coerce_digest("[1, 2]", HEADS)
    d = news.coerce_digest("{}", HEADS)  # empty object: everything undigested, neutral overall
    assert d["overall"]["flag"] is False and len(d["items"]) == 3


class FakeResp:
    def __init__(self, content: str):
        self._c = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"message": {"role": "assistant", "content": self._c}}


@pytest.fixture
def feeds(monkeypatch):
    monkeypatch.setattr(news, "fetch_feed", lambda url, src: [dict(h) for h in HEADS] if "yahoo" in url else [])
    monkeypatch.setattr(news, "merge_headlines", lambda lists, since, limit, now=None: [h for l in lists for h in l][:limit])


def test_run_digests_stores_and_journals(feeds, monkeypatch):
    calls = []

    def post(url, json=None, timeout=None):
        calls.append((url, json, timeout))
        assert json["format"] == "json" and json["stream"] is False
        return FakeResp(__import__("json").dumps(GOOD))

    monkeypatch.setattr(news.requests, "post", post)
    out = news.run(["NVDA"], timedelta(hours=24), 15, digest=True)
    assert out["ok"] and out["model"] == config.OLLAMA_MODEL
    assert calls[0][0] == config.OLLAMA_URL.rstrip("/") + "/api/chat" and calls[0][2] == 120
    assert out["symbols"]["NVDA"]["overall"]["flag"] is True
    assert out["flagged"] == ["NVDA"]
    conn = db.connect()
    rows = db.rows(conn, "SELECT * FROM news ORDER BY relevance DESC")
    assert len(rows) == 3 and rows[0]["relevance"] == 9 and rows[0]["sentiment"] == "negative"
    j = db.rows(conn, "SELECT * FROM journal WHERE kind='news'")
    assert len(j) == 1 and "FLAGGED NVDA" in j[0]["message"]
    # re-run upserts instead of duplicating
    news.run(["NVDA"], timedelta(hours=24), 15, digest=True)
    assert db.row(conn, "SELECT COUNT(*) n FROM news")["n"] == 3


def test_run_falls_back_and_survives_malformed(feeds, monkeypatch):
    urls = []

    def post(url, json=None, timeout=None):
        urls.append(url)
        if url.startswith(config.OLLAMA_URL.rstrip("/")):
            raise requests.ConnectionError("no route to host")
        return FakeResp("this is not json at all")

    monkeypatch.setattr(news.requests, "post", post)
    out = news.run(["NVDA"], timedelta(hours=24), 15, digest=True)
    assert len(urls) == 2 and urls[1].startswith(config.OLLAMA_FALLBACK_URL.rstrip("/"))
    e = out["symbols"]["NVDA"]
    assert e["overall"] is None and any("digest failed" in x for x in e["errors"])
    conn = db.connect()
    assert db.row(conn, "SELECT COUNT(*) n FROM news WHERE sentiment IS NULL")["n"] == 3  # stored raw


def test_no_digest_writes_nothing(feeds, monkeypatch):
    monkeypatch.setattr(news.requests, "post", lambda *a, **k: pytest.fail("must not call ollama"))
    out = news.run(["NVDA"], timedelta(hours=24), 15, digest=False)
    assert len(out["symbols"]["NVDA"]["items"]) == 3
    conn = db.connect()
    assert db.row(conn, "SELECT COUNT(*) n FROM news")["n"] == 0


def test_news_trigger_pauses_thesis_once(feeds, monkeypatch):
    from trader.spec import Trigger
    insert_thesis(config.DB_PATH, triggers=[Trigger(type="news", action="pause")])
    monkeypatch.setattr(news.requests, "post", lambda url, json=None, timeout=None: FakeResp(__import__("json").dumps(GOOD)))
    out = news.run(["NVDA"], timedelta(hours=24), 15, digest=True)
    assert [f["thesis"] for f in out["triggers_fired"]] == ["semis"]
    conn = db.connect()
    t = db.row(conn, "SELECT status, spec FROM theses WHERE id='semis'")
    assert t["status"] == "paused" and json.loads(t["spec"])["status"] == "paused"
    assert db.row(conn, "SELECT COUNT(*) n FROM journal WHERE kind='trigger'")["n"] == 1
    conn.execute("UPDATE theses SET status='active'")
    assert news.run(["NVDA"], timedelta(hours=24), 15, digest=True)["triggers_fired"] == []  # debounced


def test_cli_rejects_bad_symbol(capsys):
    assert news.main(["nv da!"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
