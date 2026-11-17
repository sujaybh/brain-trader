#!/usr/bin/env python3
"""Fetch recent headlines for symbols from free RSS feeds (Yahoo Finance, Google News) and
digest them with Qwen via Ollama: per-headline sentiment/relevance/summary plus an overall
per-symbol view with a `flag` for material company-specific developments.

Stores digested items in the `news` table and journals one `kind=news` entry per run. Then
evaluates thesis `news` triggers (a negative material flag on a universe symbol): journaled as
kind=trigger, pushed, and acted on (pause / propose_exit / review), debounced 24h.
`--no-digest` skips the model and only returns raw headlines (writes nothing).
Prints one JSON document. Exit 0 ok, 1 invalid input, 2 unexpected error.
"""
from __future__ import annotations

import argparse
import json
import re
import os
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_VENV = ROOT / ".venv"
if __name__ == "__main__" and (_VENV / "bin" / "python").exists() and Path(sys.prefix).resolve() != _VENV.resolve() \
        and not os.environ.get("TRADER_REEXEC"):
    # run directly via the shebang by a system python: re-exec under the project venv
    os.environ["TRADER_REEXEC"] = "1"
    os.execv(str(_VENV / "bin" / "python"), [str(_VENV / "bin" / "python"), __file__, *sys.argv[1:]])

import requests  # noqa: E402

from trader import config  # noqa: E402
from trader.db import connect, journal, now_iso  # noqa: E402

YAHOO = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US"
GOOGLE = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) thesis-trader/1.0"}
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
OLLAMA_TIMEOUT = 120
SENTIMENTS = ("positive", "negative", "neutral")


def log(*a) -> None:
    print("[news]", *a, file=sys.stderr)


# ---------------------------------------------------------------- fetching / parsing

def parse_since(s: str) -> timedelta:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([mhdw]?)\s*", str(s).lower())
    if not m:
        raise ValueError(f"bad --since {s!r}; use e.g. 12h, 24h, 3d")
    n, unit = float(m.group(1)), m.group(2) or "h"
    return {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]


def normalize_title(t: str) -> str:
    t = re.sub(r"[^a-z0-9 ]+", " ", (t or "").lower())
    return re.sub(r"\s+", " ", t).strip()


def _text(el, tag: str) -> str | None:
    c = el.find(tag)
    return c.text.strip() if c is not None and c.text else None


def parse_rss(xml_text: str | bytes, default_source: str = "") -> list[dict]:
    """RSS 2.0 -> [{headline, url, published (iso UTC or None), source}]. Tolerates junk items."""
    root = ET.fromstring(xml_text)
    out = []
    for it in root.iter("item"):
        title = _text(it, "title")
        if not title:
            continue
        source = _text(it, "source") or default_source
        # Google News appends " - Publisher" to every title; strip it so de-dup works across feeds
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].rstrip()
        published = None
        pd = _text(it, "pubDate")
        if pd:
            try:
                d = parsedate_to_datetime(pd)
                d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
                published = d.astimezone(timezone.utc).isoformat(timespec="seconds")
            except (TypeError, ValueError):
                pass
        out.append({"headline": re.sub(r"\s+", " ", title), "url": _text(it, "link"),
                    "published": published, "source": source})
    return out


def fetch_feed(url: str, source: str) -> list[dict]:
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    return parse_rss(r.content, source)


def merge_headlines(lists: list[list[dict]], since: timedelta | None, limit: int,
                    now: datetime | None = None) -> list[dict]:
    """De-dup by normalized title (first feed wins), drop items older than `since`, newest first."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - since if since else None
    seen, out = set(), []
    for lst in lists:
        for h in lst:
            key = normalize_title(h["headline"])
            if not key or key in seen:
                continue
            if cutoff and h.get("published") and datetime.fromisoformat(h["published"]) < cutoff:
                continue
            seen.add(key)
            out.append(h)
    out.sort(key=lambda h: h.get("published") or "", reverse=True)
    return out[:limit]


def fetch_symbol(sym: str, since: timedelta, limit: int) -> tuple[list[dict], list[str]]:
    lists, errors = [], []
    for name, url in (("Yahoo Finance", YAHOO.format(sym=sym)), ("Google News", GOOGLE.format(q=quote_plus(f"{sym} stock")))):
        try:
            lists.append(fetch_feed(url, name))
        except (requests.RequestException, ET.ParseError) as e:
            errors.append(f"{name}: {e}")
            log(f"{sym}: {name} failed: {e}")
    return merge_headlines(lists, since, limit), errors


# ---------------------------------------------------------------- digest

SYSTEM = (
    "You are a terse financial news analyst. You read headlines about one stock and reply with "
    "a single JSON object only, no prose."
)


def build_prompt(sym: str, headlines: list[dict]) -> str:
    lines = "\n".join(f"{i}. {h['headline']} ({h.get('source') or '?'}, {h.get('published') or 'undated'})"
                      for i, h in enumerate(headlines))
    return f"""Stock: {sym}

Headlines:
{lines}

Return JSON exactly in this shape:
{{"items": [{{"i": <headline number>, "headline": "<copied verbatim>", "sentiment": "positive|negative|neutral",
  "relevance": <0-10, how much this matters specifically to {sym}'s business or stock>, "summary": "<=20 words"}}],
 "overall": {{"sentiment": "positive|negative|neutral", "summary": "<=40 words", "flag": true|false, "flag_reason": "<short or empty>"}}}}

One item per headline. Set "flag" true ONLY for a material, company-specific development for {sym}:
earnings, guidance change, regulatory action, major product launch/failure, M&A, major litigation,
executive departure. Routine market chatter, sector moves, analyst price-target noise, listicles and
"should you buy" pieces are NOT flags."""


def _strip_think(s: str) -> str:
    s = re.sub(r"<think>.*?</think>", "", s or "", flags=re.S).strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-z]*\s*|\s*```$", "", s, flags=re.S)
    return s


def call_ollama(url: str, model: str, prompt: str, timeout: float = OLLAMA_TIMEOUT) -> str:
    body = {"model": model, "stream": False, "format": "json", "options": {"temperature": 0.1},
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]}
    r = requests.post(f"{url.rstrip('/')}/api/chat", json=body, timeout=timeout)
    r.raise_for_status()
    return r.json()["message"]["content"]


def ask_model(prompt: str) -> tuple[str, str]:
    """Primary model, then fallback on connection error / timeout. Returns (model, raw content)."""
    try:
        return config.OLLAMA_MODEL, call_ollama(config.OLLAMA_URL, config.OLLAMA_MODEL, prompt)
    except (requests.ConnectionError, requests.Timeout) as e:
        log(f"primary {config.OLLAMA_MODEL}@{config.OLLAMA_URL} unavailable ({type(e).__name__}); falling back")
    return config.OLLAMA_FALLBACK_MODEL, call_ollama(config.OLLAMA_FALLBACK_URL, config.OLLAMA_FALLBACK_MODEL, prompt)


def wait_for_ollama(timeout: float | None = None, interval: float = 5.0) -> str | None:
    """Poll GET /api/tags on the primary, then the fallback Ollama until one answers or
    `timeout` (config.OLLAMA_WAIT_S, 60 s) passes. Right after boot the nightly job can start
    before Ollama is serving. Returns the URL that answered, or None (digests will then fail
    and be reported as such)."""
    timeout = config.OLLAMA_WAIT_S if timeout is None else timeout
    deadline = time.monotonic() + max(timeout, 0)
    urls = [u for u in dict.fromkeys((config.OLLAMA_URL, config.OLLAMA_FALLBACK_URL)) if u]
    while True:
        for u in urls:
            try:
                requests.get(f"{u.rstrip('/')}/api/tags", timeout=3).raise_for_status()
                return u
            except requests.RequestException:
                pass
        left = deadline - time.monotonic()
        if left <= 0:
            log(f"no Ollama answered at {', '.join(urls)} within {timeout:g}s")
            return None
        time.sleep(min(interval, left))


def _words(s, n: int) -> str:
    w = str(s or "").split()
    return " ".join(w[:n]) + ("…" if len(w) > n else "")


def _sentiment(v) -> str:
    v = str(v or "").strip().lower()
    if v in SENTIMENTS:
        return v
    if v in ("bullish", "pos", "good", "+"):
        return "positive"
    if v in ("bearish", "neg", "bad", "-"):
        return "negative"
    return "neutral"


def _relevance(v) -> int:
    try:
        return max(0, min(10, int(round(float(v)))))
    except (TypeError, ValueError):
        return 0


def _bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    return str(v).strip().lower() in ("true", "yes", "y", "1")


def coerce_digest(raw, headlines: list[dict]) -> dict:
    """Defensively validate the model's output against the headlines we actually sent.
    Items the model invented (no matching headline) are dropped; fields are coerced and
    clamped. Raises ValueError if `raw` is not a JSON object at all."""
    if isinstance(raw, str):
        raw = json.loads(_strip_think(raw))
    if not isinstance(raw, dict):
        raise ValueError("model output is not a JSON object")
    by_norm = {normalize_title(h["headline"]): idx for idx, h in enumerate(headlines)}
    items, used = [], set()
    for it in raw.get("items") if isinstance(raw.get("items"), list) else []:
        if not isinstance(it, dict):
            continue
        idx = by_norm.get(normalize_title(str(it.get("headline") or "")))
        if idx is None:
            try:
                i = int(it.get("i"))
                idx = i if 0 <= i < len(headlines) else None
            except (TypeError, ValueError):
                idx = None
        if idx is None or idx in used:
            continue
        used.add(idx)
        src = headlines[idx]
        items.append({**src, "sentiment": _sentiment(it.get("sentiment")), "relevance": _relevance(it.get("relevance")),
                      "summary": _words(it.get("summary"), 20)})
    ov = raw.get("overall") if isinstance(raw.get("overall"), dict) else {}
    flag = _bool(ov.get("flag", False))
    overall = {"sentiment": _sentiment(ov.get("sentiment")), "summary": _words(ov.get("summary"), 40),
               "flag": flag, "flag_reason": _words(ov.get("flag_reason"), 30) if flag else ""}
    # headlines the model skipped are still kept, just undigested
    for idx, h in enumerate(headlines):
        if idx not in used:
            items.append({**h, "sentiment": None, "relevance": None, "summary": None})
    items.sort(key=lambda x: (x["relevance"] is None, -(x["relevance"] or 0)))
    return {"overall": overall, "items": items}


def store(conn, sym: str, items: list[dict], model: str | None) -> int:
    ts = now_iso()
    n = 0
    for it in items:
        conn.execute(
            "INSERT INTO news(ts, published, symbol, headline, source, url, sentiment, relevance, summary, data) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol, headline) DO UPDATE SET "
            "ts=excluded.ts, published=COALESCE(excluded.published, news.published), "
            "sentiment=COALESCE(excluded.sentiment, news.sentiment), relevance=COALESCE(excluded.relevance, news.relevance), "
            "summary=COALESCE(excluded.summary, news.summary), data=excluded.data",
            (ts, it.get("published"), sym, it["headline"], it.get("source"), it.get("url"), it.get("sentiment"),
             it.get("relevance"), it.get("summary"), json.dumps({"model": model})))
        n += 1
    return n


# ---------------------------------------------------------------- news triggers
# tools/check_triggers.py evaluates price/drawdown triggers; `news` triggers live here because
# this is where the digest happens. A thesis with a {"type": "news"} trigger fires when Qwen
# flags a material development for a universe symbol AND it is negative (overall sentiment
# negative, or a negative item with relevance >= 7). Debounced 24h per (thesis, symbol) with the
# same settings key scheme as check_triggers.

NEWS_DEBOUNCE = timedelta(hours=24)


def is_negative_flag(entry: dict) -> bool:
    ov = entry.get("overall") or {}
    if not ov.get("flag"):
        return False
    if ov.get("sentiment") == "negative":
        return True
    return any(i.get("sentiment") == "negative" and (i.get("relevance") or 0) >= 7 for i in entry.get("items", []))


def _spawn_review(thesis_id: str, payload: dict) -> dict:
    """Start tools/review.py detached: a Claude review can take minutes and must not hold up
    the nightly job."""
    import subprocess
    try:
        p = subprocess.Popen([sys.executable, str(ROOT / "tools" / "review.py"), "--thesis", thesis_id,
                              "--trigger", json.dumps(payload)], cwd=str(ROOT), stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        return {"review_started": True, "pid": p.pid}
    except OSError as e:
        return {"review_started": False, "reason": str(e)}


def fire_news_triggers(conn, results: dict, now: datetime | None = None) -> list[dict]:
    from trader import notify
    from trader.db import get_setting, qty_by_thesis, set_setting
    from trader.db import rows as db_rows
    from web import proposals

    now = now or datetime.now(timezone.utc)
    bad = {s: e for s, e in results.items() if is_negative_flag(e)}
    if not bad:
        return []
    fired = []
    held_all = qty_by_thesis(conn)
    for t in db_rows(conn, "SELECT id, status, spec FROM theses WHERE status='active'"):
        spec = json.loads(t["spec"])
        trigs = [x for x in spec.get("triggers", []) if x.get("type") == "news"]
        for trig in trigs:
            for sym in spec.get("universe", []):
                if sym not in bad:
                    continue
                key = f"trigger:{t['id']}:news:{sym}"
                last = get_setting(conn, key)
                if last and now - datetime.fromisoformat(last) < NEWS_DEBOUNCE:
                    continue
                set_setting(conn, key, now.isoformat(timespec="seconds"))
                ov = bad[sym]["overall"]
                why = f"{sym}: {ov.get('flag_reason') or ov.get('summary')}"
                f = {"thesis": t["id"], "type": "news", "symbol": sym, "action": trig.get("action", "review"),
                     "why": why, "overall": ov}
                journal(conn, "trigger", f"news fired: {why} -> {f['action']}", thesis_id=t["id"], symbol=sym, data=f)
                notify.push(f"{why} ({t['id']}). Action: {f['action']}.", title=f"Trigger: news {sym}",
                            priority=4, tags=["warning"], conn=conn)
                if f["action"] == "pause":
                    if t["status"] == "active":
                        spec["status"] = "paused"
                        conn.execute("UPDATE theses SET status='paused', spec=?, updated=? WHERE id=?",
                                     (json.dumps(spec), now_iso(), t["id"]))
                        journal(conn, "thesis", f"paused by news trigger: {why}", thesis_id=t["id"],
                                data={"status": "paused"})
                    f["result"] = {"paused": True}
                elif f["action"] == "propose_exit":
                    q = round(held_all.get(t["id"], {}).get(sym, 0.0), 6)
                    if q <= 0:
                        f["result"] = {"ran": False, "reason": f"thesis holds no {sym}"}
                    else:
                        try:
                            p = proposals.create(conn, t["id"], {"type": "order", "side": "sell", "symbol": sym, "qty": q},
                                                 f"news trigger: {why}")
                            f["result"] = {"proposal_id": p["id"], "status": p["status"]}
                        except proposals.ProposalError as e:
                            f["result"] = {"ran": False, "reason": e.reason}
                else:
                    f["result"] = _spawn_review(t["id"], f)
                fired.append(f)
    return fired


# ---------------------------------------------------------------- main

def run(symbols: list[str], since: timedelta, limit: int, digest: bool, triggers: bool = True) -> dict:
    result: dict = {"ok": True, "model": None, "symbols": {}}
    fetched = {}
    for sym in symbols:
        heads, errors = fetch_symbol(sym, since, limit)
        fetched[sym] = (heads, errors)
        log(f"{sym}: {len(heads)} headlines")
    if not digest:
        for sym, (heads, errors) in fetched.items():
            result["symbols"][sym] = {"overall": None, "items": heads, **({"errors": errors} if errors else {})}
        return result
    if any(h for h, _ in fetched.values()):
        result["ollama"] = wait_for_ollama()
    conn = connect()
    try:
        models = set()
        for sym, (heads, errors) in fetched.items():
            entry: dict = {"overall": None, "items": heads}
            if errors:
                entry["errors"] = list(errors)
            if heads:
                t0 = time.monotonic()
                try:
                    model, content = ask_model(build_prompt(sym, heads))
                    models.add(model)
                    d = coerce_digest(content, heads)
                    entry.update(d, model=model, latency_s=round(time.monotonic() - t0, 1))
                except (requests.RequestException, ValueError, KeyError) as e:
                    entry.setdefault("errors", []).append(f"digest failed: {type(e).__name__}: {e}")
                    entry["items"] = [{**h, "sentiment": None, "relevance": None, "summary": None} for h in heads]
                    log(f"{sym}: digest failed: {e}")
                store(conn, sym, entry["items"], entry.get("model"))
            result["symbols"][sym] = entry
        result["model"] = ", ".join(sorted(models)) or None
        summary = {s: v["overall"] for s, v in result["symbols"].items()}
        failure = _total_failure(fetched, result["symbols"])
        if failure:
            # Not a news day of "no digest" x N: report failure (nightly journals it as an error)
            result.update(ok=False, reason=failure, flagged=[])
            log(failure)
            return result
        flagged = [s for s, o in summary.items() if o and o.get("flag")]
        msg = "; ".join(f"{s}: {o['sentiment']} — {o['summary']}" if o else f"{s}: no digest"
                        for s, o in summary.items())
        journal(conn, "news", (f"FLAGGED {','.join(flagged)}. " if flagged else "") + msg,
                data={"model": result["model"], "overall": summary, "flagged": flagged,
                      "counts": {s: len(v["items"]) for s, v in result["symbols"].items()}})
        result["flagged"] = flagged
        if triggers:
            result["triggers_fired"] = fire_news_triggers(conn, result["symbols"])
    finally:
        conn.close()
    return result


def _total_failure(fetched: dict, entries: dict) -> str | None:
    """-> a reason when the run produced nothing usable: every symbol that had headlines failed
    its digest, or every feed failed for every symbol. None for a normal (or partly failed) run."""
    with_heads = [s for s, (h, _) in fetched.items() if h]
    if with_heads and not any(entries[s].get("overall") for s in with_heads):
        first = next((e for s in with_heads for e in entries[s].get("errors", []) if e.startswith("digest failed")), "")
        return (f"every digest failed ({len(with_heads)} symbol(s) with headlines; Ollama "
                f"{config.OLLAMA_URL} / fallback {config.OLLAMA_FALLBACK_URL}): {first[:200]}")
    if fetched and not with_heads and all(e for _, e in fetched.values()):
        return f"every news feed failed for {', '.join(fetched)} (network down?)"
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbols", nargs="+", metavar="SYM")
    ap.add_argument("--since", default="24h", help="only headlines newer than this (e.g. 12h, 3d); default 24h")
    ap.add_argument("--limit", type=int, default=15, help="max headlines per symbol (default 15)")
    ap.add_argument("--no-digest", action="store_true", help="skip Qwen; return raw headlines, write nothing")
    ap.add_argument("--no-triggers", action="store_true", help="digest and store, but do not fire thesis `news` triggers")
    args = ap.parse_args(argv)
    try:
        syms = []
        for s in args.symbols:
            s = s.strip().upper()
            if not SYMBOL_RE.match(s):
                print(json.dumps({"ok": False, "reason": f"bad symbol {s!r}"}, indent=2))
                return 1
            if s not in syms:
                syms.append(s)
        try:
            since = parse_since(args.since)
        except ValueError as e:
            print(json.dumps({"ok": False, "reason": str(e)}, indent=2))
            return 1
        out = run(syms, since, max(1, args.limit), not args.no_digest, not args.no_triggers)
        print(json.dumps(out, indent=2, default=str))
        return 0 if out.get("ok", True) else 1
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"ok": False, "reason": f"unexpected error: {type(e).__name__}: {e}"}, indent=2))
        return 2


if __name__ == "__main__":
    sys.exit(main())
