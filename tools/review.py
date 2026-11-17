#!/usr/bin/env python3
"""Build a review context pack for a thesis and hand it to Claude Code for a judgment call.

The pack (markdown): spec + plain-English read-back + the user's original words; current
positions (reconciled from Alpaca) attributed to the thesis; P&L since creation vs SPY from
the snapshots table; journal since the last review; 7 days of news for the universe; the
trigger payload; wash-sale warnings.

Without --pack-only, runs one single-turn `claude -p` with no tools (the pack already holds the
data) and --json-schema structured output:
  {"recommendation": "HOLD|CHANGE|EXIT", "lines": ["..."], "proposal": null | {"action": {...}, "reason": "..."}}
The answer is validated here, journaled as kind=review and pushed. A proposal (CHANGE/EXIT only)
is created by this script through web/proposals.create, the same path tools/propose.py uses, so
the phone gets Approve/Reject (or, for a thesis in mode auto, it executes as propose.py would).
Claude cannot run anything during a review: it can only propose.

Skipping: if nothing material happened since the last real review (see material_changes and the
SKIP_* constants), no Claude call is made; a kind=review "skipped: nothing changed" entry is
journaled and the cadence clock restarts. --force always runs Claude.

--all reviews every active thesis whose review cadence is due (settings key last_review:<id>).
Prints one JSON document (--raw prints the bare markdown pack instead, with --pack-only).
Exit 0 ok (reviewed or skipped), 1 refusal (unknown thesis, claude failed), 2 unexpected error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_VENV = ROOT / ".venv"
if __name__ == "__main__" and (_VENV / "bin" / "python").exists() and Path(sys.prefix).resolve() != _VENV.resolve() \
        and not os.environ.get("TRADER_REEXEC"):
    # run directly via the shebang by a system python: re-exec under the project venv
    os.environ["TRADER_REEXEC"] = "1"
    os.execv(str(_VENV / "bin" / "python"), [str(_VENV / "bin" / "python"), __file__, *sys.argv[1:]])

from croniter import croniter  # noqa: E402

from trader import client as tclient  # noqa: E402
from trader import config, llm, notify  # noqa: E402
from trader.db import connect, deployed_by_thesis, get_setting, journal, qty_by_thesis, row, rows, set_setting  # noqa: E402
from trader.schedule import client_order_id  # noqa: E402
from trader.spec import ThesisSpec, describe  # noqa: E402
from web import proposals  # noqa: E402
from web.chat import record_usage, run_json  # noqa: E402

TIMEOUT_S = 600
CADENCE_DAYS = {"weekly": 7, "monthly": 30, "quarterly": 91}
BENCHMARK = "SPY"
PROPOSAL_EXPIRES = timedelta(hours=24)  # same default as tools/propose.py

# Skip rule: a scheduled review is skipped (no Claude call) unless, since the last review that
# actually ran, at least one of these happened for the thesis:
#   - a journal entry of a SKIP_MATERIAL_KINDS kind (orders, fills, refusals, fired triggers,
#     proposals, spec/status changes). Orders and fills of the thesis's own scheduled DCA slots
#     are routine and do not count (a refusal during a slot still does);
#   - a news digest flagged a universe symbol;
#   - a held symbol's price moved SKIP_PRICE_MOVE_PCT or more from the price recorded at that
#     review (a held symbol with no recorded or no current price counts as moved);
#   - the review was started by a trigger (--trigger), or there was no earlier review.
SKIP_MATERIAL_KINDS = ("order", "fill", "refused", "trigger", "proposal", "thesis")
SKIP_PRICE_MOVE_PCT = 5.0
MAX_LINES = 12

REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["recommendation", "lines", "proposal"],
    "properties": {
        "recommendation": {"type": "string", "enum": ["HOLD", "CHANGE", "EXIT"]},
        "lines": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": MAX_LINES},
        "proposal": {"anyOf": [
            {"type": "null"},
            {"type": "object", "additionalProperties": False, "required": ["action", "reason"],
             "properties": {
                 "action": {"type": "object", "required": ["type"], "properties": {
                     "type": {"type": "string", "enum": list(proposals.ACTION_TYPES)},
                     "side": {"type": "string", "enum": ["buy", "sell"]},
                     "symbol": {"type": "string"},
                     "notional": {"type": "number"},
                     "qty": {"type": "number"},
                     "patch": {"type": "object"}}},
                 "reason": {"type": "string"}}},
        ]},
    },
}

INSTRUCTIONS = """\
---
## Your task

You are reviewing the thesis above for me, using only the data in this pack. You have no tools
and cannot change anything; your answer is a structured reply (recommendation, lines, proposal).

1. Does the original rationale (and my words in source_text) still hold? Cite the specific news or
   numbers that support or undermine it. Separate company-specific developments from market noise.
2. How has it done versus the {bench} benchmark over the same period? If the pack says there is not
   enough snapshot history, say so; do not estimate.
3. recommendation: exactly one of HOLD, CHANGE (say what), or EXIT. Say what would change your mind.
4. proposal: null for HOLD. For CHANGE or EXIT, either null (if you only want me to think about it)
   or one concrete action with a one-line reason; it goes to my phone for approval. Actions:
     {{"type":"order","side":"sell","symbol":"NVDA","notional":50}}   (or "qty": 0.5 instead of notional)
     {{"type":"pause"}}   {{"type":"resume"}}
     {{"type":"spec_change","patch":{{"capital_cap":400}}}}   (shallow merge over the spec JSON above)
5. Mention any wash-sale warning from the pack if it applies.

lines: at most {max_lines} short lines, plain text, no markdown headers. I'm reading this on a phone.
"""


class ReviewError(Exception):
    pass


def log(*a) -> None:
    print("[review]", *a, file=sys.stderr)


def _money(v) -> str:
    return "n/a" if v is None else f"${v:,.2f}"


def _pct(v) -> str:
    return "n/a" if v is None else f"{v:+.2f}%"


def _md_table(headers: list[str], data: list[list]) -> str:
    if not data:
        return "_none_"
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in data]
    return "\n".join(out)


# ---------------------------------------------------------------- pack sections

def positions_section(conn, spec: ThesisSpec) -> tuple[str, list[dict]]:
    broker_note = ""
    live: dict[str, dict] = {}
    try:
        bc = tclient.BrokerClient()
        bc.reconcile()
        live = {p["symbol"]: p for p in bc.positions()}
    except tclient.BrokerError as e:
        broker_note = f"\n_Broker unreachable ({e.reason}); prices below are missing, attribution is from our order history only._\n"
    dep = deployed_by_thesis(conn).get(spec.id, {})
    qty = qty_by_thesis(conn).get(spec.id, {})
    out, data = [], []
    for sym in sorted(set(dep) | set(qty)):
        q = qty.get(sym, 0.0)
        if abs(q) < 1e-4:  # closed (below qty_decimals): its result is realized, not a position
            continue
        lp = live.get(sym)
        price = float(lp["current_price"]) if lp and lp.get("current_price") is not None else None
        mv = round(q * price, 2) if price is not None else None
        cost = dep.get(sym, 0.0)
        pl = round(mv - cost, 2) if mv is not None else None
        plpc = round(pl / cost * 100, 2) if pl is not None and cost > 0 else None
        acct_q = float(lp["qty"]) if lp else None
        out.append({"symbol": sym, "qty": q, "cost": cost, "price": price, "market_value": mv, "pl": pl, "plpc": plpc,
                    "account_qty": acct_q})
        data.append([sym, f"{q:g}", _money(cost), _money(price), _money(mv), f"{_money(pl)} ({_pct(plpc)})",
                     "n/a" if acct_q is None else f"{acct_q:g}"])
    total_cost = sum(max(v, 0) for v in dep.values())
    md = (f"Deployed (net filled notional): {_money(total_cost)} of {_money(spec.capital_cap)} cap; "
          f"max per name {_money(spec.max_position_usd())}.\n{broker_note}\n"
          + _md_table(["symbol", "thesis qty", "net cost", "price", "value", "P&L", "acct qty (all theses)"], data))
    return md, out


def performance_section(conn, spec: ThesisSpec, created: str) -> str:
    """P&L since creation and vs SPY from snapshots. Refuses to invent numbers."""
    start = created[:10]
    dates = [r["date"] for r in rows(conn, "SELECT DISTINCT date FROM snapshots WHERE date>=? ORDER BY date", (start,))]
    if len(dates) < 2:
        return (f"Only {len(dates)} snapshot day(s) since {start}: not enough history to compute P&L over time "
                f"or compare with {BENCHMARK}. Do not estimate these numbers.")
    first, last = dates[0], dates[-1]
    lines = [f"Period: {first} → {last} ({len(dates)} snapshot days)."]

    def thesis_at(d):
        r = row(conn, "SELECT SUM(market_value) mv, SUM(cost_basis) cb, COUNT(*) n FROM snapshots "
                      "WHERE date=? AND thesis_id=? AND symbol!='$ACCOUNT'", (d, spec.id))
        return r if r and r["n"] else None

    t1 = thesis_at(last)
    if t1 and t1["mv"] is not None and t1["cb"]:
        pl = t1["mv"] - t1["cb"]
        lines.append(f"- Thesis on {last}: value {_money(t1['mv'])}, cost {_money(t1['cb'])}, "
                     f"P&L {_money(pl)} ({_pct(pl / t1['cb'] * 100)}).")
        t0 = thesis_at(first)
        if t0 and t0["mv"] is not None and t0["cb"]:
            lines.append(f"- Thesis on {first}: value {_money(t0['mv'])}, cost {_money(t0['cb'])} "
                         f"(P&L {_pct((t0['mv'] - t0['cb']) / t0['cb'] * 100)}).")
    else:
        lines.append("- No per-thesis snapshot rows for this thesis yet.")

    def acct(d):
        return row(conn, "SELECT market_value FROM snapshots WHERE date=? AND symbol='$ACCOUNT' ORDER BY id LIMIT 1", (d,))

    a0, a1 = acct(first), acct(last)
    if a0 and a1 and a0["market_value"]:
        lines.append(f"- Account equity {_money(a0['market_value'])} → {_money(a1['market_value'])} "
                     f"({_pct((a1['market_value'] / a0['market_value'] - 1) * 100)}; includes deposits and other theses).")
    else:
        lines.append("- No $ACCOUNT snapshot on both ends of the period.")

    def spy(d, order):
        return row(conn, f"SELECT date, price FROM snapshots WHERE symbol=? AND price IS NOT NULL AND date>=? AND date<=? "
                         f"ORDER BY date {order} LIMIT 1", (BENCHMARK, first, last))

    s0, s1 = spy(first, "ASC"), spy(last, "DESC")
    if s0 and s1 and s0["date"] != s1["date"] and s0["price"]:
        lines.append(f"- {BENCHMARK} {s0['date']} {_money(s0['price'])} → {s1['date']} {_money(s1['price'])} "
                     f"({_pct((s1['price'] / s0['price'] - 1) * 100)}).")
        lines.append("  Note: the thesis return above is on net cost of DCA buys, not time-weighted; compare loosely.")
    else:
        lines.append(f"- No {BENCHMARK} price in snapshots on two different days; benchmark comparison unavailable.")
    return "\n".join(lines)


def last_real_review(conn, thesis_id: str) -> dict | None:
    """The newest kind=review journal row that was an actual Claude review (not a skip)."""
    return row(conn, "SELECT id, ts, data FROM journal WHERE kind='review' AND thesis_id=? "
                     "AND COALESCE(json_extract(data, '$.skipped'), 0) = 0 ORDER BY id DESC LIMIT 1", (thesis_id,))


def journal_section(conn, thesis_id: str) -> str:
    last = last_real_review(conn, thesis_id)
    since_id = last["id"] if last else 0
    js = rows(conn, "SELECT ts, kind, symbol, message FROM journal WHERE thesis_id=? AND id>? AND kind!='notify' "
                    "ORDER BY id DESC LIMIT 150", (thesis_id, since_id))
    head = f"Since last review ({last['ts']}):" if last else "No previous review; all entries (newest first, max 150):"
    if not js:
        return head + "\n_none_"
    return head + "\n" + "\n".join(f"- {j['ts'][:16]} [{j['kind']}]{' ' + j['symbol'] if j['symbol'] else ''} "
                                   f"{j['message'][:400]}" for j in js)


def news_section(conn, universe: list[str]) -> str:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    q = ",".join("?" * len(universe))
    ns = rows(conn, f"SELECT symbol, published, ts, headline, source, sentiment, relevance, summary FROM news "
                    f"WHERE symbol IN ({q}) AND COALESCE(published, ts)>=? "
                    f"ORDER BY COALESCE(relevance, -1) DESC, COALESCE(published, ts) DESC LIMIT 40", (*universe, cutoff))
    if not ns:
        return "_No news stored for the universe in the last 7 days._"
    return "\n".join(f"- {n['symbol']} {(n['published'] or n['ts'])[:10]} [{n['sentiment'] or '?'} r{n['relevance'] if n['relevance'] is not None else '?'}] "
                     f"{n['headline']}{' — ' + n['summary'] if n['summary'] else ''}" for n in ns)


def wash_sale_section(conn, spec: ThesisSpec, status: str) -> str:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
    q = ",".join("?" * len(spec.universe))
    sells = rows(conn, f"SELECT symbol, thesis_id, MAX(updated) last_sell, SUM(filled_qty) q FROM orders "
                       f"WHERE side='sell' AND status IN ('filled','partially_filled') AND updated>=? AND symbol IN ({q}) "
                       f"GROUP BY symbol, thesis_id", (cutoff, *spec.universe))
    if not sells:
        return "No sells of universe symbols in the last 30 days."
    rebuy = spec.schedule.type == "dca" and status == "active"
    nxt = ""
    if rebuy:
        nxt_dt = croniter(spec.schedule.cron, datetime.now(ZoneInfo(spec.schedule.tz))).get_next(datetime)
        nxt = f" (next scheduled buy {nxt_dt:%Y-%m-%d %H:%M %Z})"
    out = []
    for s in sells:
        msg = f"- {s['symbol']}: sold {s['q']:g} sh (thesis {s['thesis_id']}) on {s['last_sell'][:10]}."
        if rebuy:
            msg += f" **WARNING:** the DCA schedule would rebuy it within 30 days{nxt}; if that sale was at a loss it is a wash sale."
        out.append(msg)
    return "\n".join(out)


def build_pack(conn, thesis_id: str, trigger=None) -> str:
    return _build(conn, thesis_id, trigger)[0]


def _load(conn, thesis_id: str) -> tuple[dict, dict, ThesisSpec]:
    t = row(conn, "SELECT * FROM theses WHERE id=?", (thesis_id,))
    if t is None:
        raise LookupError(f"unknown thesis {thesis_id!r}")
    spec_json = json.loads(t["spec"])
    return t, spec_json, ThesisSpec.model_validate(spec_json)


def _build(conn, thesis_id: str, trigger=None) -> tuple[str, list[dict], ThesisSpec]:
    """-> (markdown pack, positions rows, spec)."""
    t, spec_json, spec = _load(conn, thesis_id)
    pos_md, positions = positions_section(conn, spec)
    parts = [
        f"# Review: {spec.name} (`{spec.id}`)",
        f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. Status **{t['status']}**, mode **{t['mode']}**, "
        f"created {t['created'][:10]}, review cadence {spec.review}.",
        "## Rationale\n" + (spec.rationale or "_none given_"),
        "## Rules (plain English)\n" + describe(spec),
        "## User's original words (source_text)\n> " + ((t["source_text"] or spec.source_text or "_none_").replace("\n", "\n> ")),
        "## Spec JSON\n```json\n" + json.dumps(spec_json, indent=2) + "\n```",
        "## Positions\n" + pos_md,
        "## Performance (from daily snapshots)\n" + performance_section(conn, spec, spec.created or t["created"]),
        "## Journal\n" + journal_section(conn, thesis_id),
        "## News, last 7 days (most relevant first)\n" + news_section(conn, spec.universe),
    ]
    if trigger is not None:
        parts.append("## Trigger that started this review\n```json\n" + json.dumps(trigger, indent=2, default=str) + "\n```")
    parts.append("## Wash-sale check\n" + wash_sale_section(conn, spec, t["status"]))
    return "\n\n".join(parts) + "\n", positions, spec


# ---------------------------------------------------------------- running claude

def _held_prices(positions: list[dict]) -> dict[str, float | None]:
    return {p["symbol"]: p["price"] for p in positions if abs(p.get("qty") or 0) > 1e-9}


def _coid(data) -> str | None:
    try:
        return (json.loads(data or "{}") or {}).get("client_order_id")
    except (ValueError, AttributeError, TypeError):
        return None


def _dca_order_ids(conn, spec: ThesisSpec, after_id: int) -> set[str]:
    """client_order_ids of the thesis's scheduled DCA slots journaled after `after_id` (the
    executor's `dca` rows carry the slot; ids are deterministic per thesis/slot/symbol)."""
    out: set[str] = set()
    for r in rows(conn, "SELECT data FROM journal WHERE thesis_id=? AND id>? AND kind='dca'", (spec.id, after_id)):
        try:
            slot = (json.loads(r["data"] or "{}") or {}).get("slot")
        except (ValueError, AttributeError, TypeError):
            continue
        if slot:
            out |= {client_order_id(spec.id, sym, slot) for sym in spec.universe}
    return out


def material_changes(conn, spec: ThesisSpec, positions: list[dict], trigger=None) -> list[str]:
    """Why a review is worth a Claude call (see the SKIP_* constants). Empty list = nothing changed."""
    if trigger is not None:
        return ["started by a trigger"]
    last = last_real_review(conn, spec.id)
    if last is None:
        return ["no earlier review"]
    reasons = []
    q = ",".join("?" * len(SKIP_MATERIAL_KINDS))
    routine = _dca_order_ids(conn, spec, last["id"])
    counts: dict[str, int] = {}
    for r in rows(conn, f"SELECT kind, data FROM journal WHERE thesis_id=? AND id>? AND kind IN ({q}) ORDER BY id",
                  (spec.id, last["id"], *SKIP_MATERIAL_KINDS)):
        if r["kind"] in ("order", "fill") and _coid(r["data"]) in routine:
            continue
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
    for kind, n in sorted(counts.items()):
        reasons.append(f"{n} new {kind} entr{'y' if n == 1 else 'ies'}")
    flagged = set()
    for n in rows(conn, "SELECT data FROM journal WHERE kind='news' AND id>?", (last["id"],)):
        try:
            flagged |= set((json.loads(n["data"] or "{}") or {}).get("flagged") or [])
        except (ValueError, AttributeError, TypeError):
            continue
    if flagged & set(spec.universe):
        reasons.append("news flagged " + ",".join(sorted(flagged & set(spec.universe))))
    try:
        before = (json.loads(last["data"] or "{}") or {}).get("prices") or {}
    except ValueError:
        before = {}
    for sym, now in sorted(_held_prices(positions).items()):
        was = before.get(sym)
        if now is None or not was:
            reasons.append(f"{sym}: no price to compare")
        elif abs(now / was - 1) * 100 >= SKIP_PRICE_MOVE_PCT:
            reasons.append(f"{sym} moved {(now / was - 1) * 100:+.1f}% since last review")
    return reasons


def parse_answer(out: dict | None, text: str) -> dict:
    """Validate the structured reply. Raises ReviewError. The CLI validates against REVIEW_SCHEMA
    too; this is the check that counts."""
    a = (out or {}).get("structured_output")
    if a is None:
        try:
            a = json.loads(text)
        except (TypeError, ValueError):
            raise ReviewError("no structured output in the model's reply")
    if not isinstance(a, dict):
        raise ReviewError("structured output is not an object")
    rec = str(a.get("recommendation", "")).strip().upper()
    if rec not in ("HOLD", "CHANGE", "EXIT"):
        raise ReviewError(f"bad recommendation {a.get('recommendation')!r}")
    lines = a.get("lines")
    if not isinstance(lines, list) or not all(isinstance(x, str) for x in lines):
        raise ReviewError("lines must be a list of strings")
    lines = [x.strip() for x in lines if x.strip()][:MAX_LINES]
    prop = a.get("proposal")
    if prop is not None:
        if not isinstance(prop, dict) or not isinstance(prop.get("action"), dict) or not str(prop.get("reason") or "").strip():
            raise ReviewError("proposal must be null or {action: {...}, reason: '...'}")
        prop = {"action": prop["action"], "reason": str(prop["reason"]).strip()}
    return {"recommendation": rec, "lines": lines, "proposal": prop}


def _propose(conn, thesis_id: str, answer: dict) -> tuple[dict | None, str | None]:
    """Create the review's proposal, if any. -> (proposal, note for the review text)."""
    prop = answer["proposal"]
    if prop is None:
        return None, None
    if answer["recommendation"] == "HOLD":
        return None, "(proposal ignored: recommendation is HOLD)"
    try:
        p = proposals.create(conn, thesis_id, prop["action"], f"review: {prop['reason']}", PROPOSAL_EXPIRES,
                             force_confirm=True)  # a review never acts on its own, even on `mode: auto`
    except proposals.ProposalError as e:
        journal(conn, "error", f"review proposal for {thesis_id} refused: {e.reason}", thesis_id=thesis_id,
                data={"proposal": prop})
        return None, f"Proposal refused: {e.reason}"
    return p, f"Proposal #{p['id']} ({p['status']}): {p['summary']}"


def run_ollama(pack: str, instructions: str) -> tuple[bool, str, dict | None]:
    """The review on Ollama: one structured-output call (REVIEW_SCHEMA as `format`), the review
    instructions as the system message and the pack as the user message. Same return shape as
    run_json, so the answer goes through the same parse_answer / usage / proposal path."""
    model = config.ollama_model("review")
    messages = [{"role": "system", "content": instructions + "\nReply with the JSON object only."},
                {"role": "user", "content": pack}]
    tokens = [0, 0, 0]
    text, name = "", model
    for attempt in range(2):
        try:
            r = llm.ollama_chat(messages, model=model, format=REVIEW_SCHEMA, timeout=TIMEOUT_S)
        except llm.LLMError as e:
            return False, str(e), None
        text, name = r["message"].get("content") or "", r["model"]
        tokens = [tokens[0] + r["prompt_tokens"], tokens[1] + r["eval_tokens"], tokens[2] + r["duration_ms"]]
        try:
            answer = json.loads(text)
        except ValueError:
            answer = None
        if isinstance(answer, dict) or attempt:
            break
        # some models (qwen3.5 measured) ignore `format` and answer in prose: ask once more
        messages += [{"role": "assistant", "content": text},
                     {"role": "user", "content": "That was not a JSON object. Reply again with only the JSON object "
                      "described above (recommendation, lines, proposal), no other text."}]
    out = llm.usage_doc(name, *tokens[:2], tokens[2])
    if isinstance(answer, dict):
        out["structured_output"] = answer
    return bool(text), text or "empty reply from Ollama", out


def review_one(conn, thesis_id: str, trigger=None, pack_only: bool = False, force: bool = False) -> dict:
    pack, positions, spec = _build(conn, thesis_id, trigger)
    if pack_only:
        return {"ok": True, "thesis": thesis_id, "pack": pack}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    changes = material_changes(conn, spec, positions, trigger)
    if not changes and not force:
        last = last_real_review(conn, thesis_id)
        journal(conn, "review", "skipped: nothing changed", thesis_id=thesis_id,
                data={"skipped": True, "since": last["ts"] if last else None})
        set_setting(conn, f"last_review:{thesis_id}", now)
        log(f"{thesis_id}: nothing changed since {last['ts'] if last else '?'}; skipped")
        return {"ok": True, "thesis": thesis_id, "skipped": True, "review": "skipped: nothing changed"}
    instructions = INSTRUCTIONS.format(bench=BENCHMARK, max_lines=MAX_LINES)
    try:
        backend = config.llm_backend("review")
    except ValueError as e:
        return {"ok": False, "thesis": thesis_id, "reason": str(e)}
    log(f"running {backend} for {thesis_id} ({len(pack)} chars of context; {'; '.join(changes) or 'forced'})")
    if backend == "ollama":
        ok, text, out = run_ollama(pack, instructions)
    else:
        ok, text, out = run_json(pack + "\n" + instructions, timeout=TIMEOUT_S, caller="review",
                                 json_schema=REVIEW_SCHEMA)
    usage = record_usage(conn, "review", out, ok)
    answer = None
    if ok:
        try:
            answer = parse_answer(out, text)
        except ReviewError as e:
            ok, text = False, f"{e}: {text[:300]}"
    if not ok:
        journal(conn, "error", f"review of {thesis_id} failed: {text}", thesis_id=thesis_id, data={"trigger": trigger})
        notify.push(f"Review of {thesis_id} failed: {text[:200]}", title="Review failed", tags=["warning"], conn=conn)
        return {"ok": False, "thesis": thesis_id, "reason": text}
    proposal, note = _propose(conn, thesis_id, answer)
    review = "\n".join([answer["recommendation"], *answer["lines"], *([note] if note else [])])
    journal(conn, "review", review, thesis_id=thesis_id,
            data={"trigger": trigger, "changes": changes, "forced": bool(force and not changes),
                  "recommendation": answer["recommendation"], "proposal": answer["proposal"],
                  "proposal_id": (proposal or {}).get("id"), "prices": _held_prices(positions),
                  "pack_chars": len(pack), "cost_usd": (usage or {}).get("cost_usd"),
                  "model": (usage or {}).get("model")})
    set_setting(conn, f"last_review:{thesis_id}", now)
    short = review if len(review) <= 300 else review[:297].rstrip() + "…"
    link = f"{config.WEB_URL.rstrip('/')}/thesis/{thesis_id}?token={config.WEB_TOKEN}"
    notify.push(short, title=f"Review: {thesis_id}", tags=["mag"], click=link, conn=conn)
    return {"ok": True, "thesis": thesis_id, "review": review, "recommendation": answer["recommendation"],
            "proposal": proposal, "cost_usd": (usage or {}).get("cost_usd")}


def due_theses(conn, today: date | None = None) -> list[str]:
    today = today or datetime.now(timezone.utc).date()
    out = []
    for t in rows(conn, "SELECT id, spec, created FROM theses WHERE status='active' ORDER BY id"):
        cadence = json.loads(t["spec"]).get("review", "monthly")
        last = get_setting(conn, f"last_review:{t['id']}") or t["created"]
        try:
            last_d = datetime.fromisoformat(last[:10]).date()
        except ValueError:
            last_d = date.min
        if (today - last_d).days >= CADENCE_DAYS.get(cadence, 30):
            out.append(t["id"])
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--thesis", help="thesis id to review")
    g.add_argument("--all", action="store_true", help="review every active thesis whose cadence is due")
    ap.add_argument("--trigger", help="JSON payload describing what triggered the review")
    ap.add_argument("--pack-only", action="store_true", help="build and print the context pack; do not run claude")
    ap.add_argument("--raw", action="store_true", help="with --pack-only: print bare markdown instead of JSON")
    ap.add_argument("--force", action="store_true", help="run Claude even if nothing material changed")
    args = ap.parse_args(argv)
    try:
        trigger = None
        if args.trigger:
            try:
                trigger = json.loads(args.trigger)
            except ValueError as e:
                print(json.dumps({"ok": False, "reason": f"--trigger is not JSON: {e}"}, indent=2))
                return 1
        conn = connect()
        try:
            if args.all:
                ids = due_theses(conn)
                if ids and not args.pack_only:
                    # scheduled path (thesis-review.timer): bounded wait for the broker so a
                    # review right after boot has live positions. Not up -> one journaled error,
                    # nothing reviewed, last_review untouched so the next run picks them up.
                    ready, why = tclient.wait_for_broker("review", conn=conn)
                    if not ready:
                        print(json.dumps({"ok": False, "due": ids, "reason": why}, indent=2))
                        return 1
                results = [review_one(conn, i, trigger, args.pack_only, args.force) for i in ids]
                doc = {"ok": all(r["ok"] for r in results), "due": ids, "results": results}
            else:
                try:
                    doc = review_one(conn, args.thesis, trigger, args.pack_only, args.force)
                except LookupError as e:
                    doc = {"ok": False, "reason": str(e.args[0])}
        finally:
            conn.close()
        if args.raw and args.pack_only and not args.all and doc.get("ok"):
            print(doc["pack"])
        else:
            print(json.dumps(doc, indent=2, default=str))
        return 0 if doc.get("ok") else 1
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"ok": False, "reason": f"unexpected error: {type(e).__name__}: {e}"}, indent=2))
        return 2


if __name__ == "__main__":
    sys.exit(main())
