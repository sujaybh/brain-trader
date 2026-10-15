#!/usr/bin/env python3
"""Build a review context pack for a thesis and hand it to Claude Code for a judgment call.

The pack (markdown): spec + plain-English read-back + the user's original words; current
positions (reconciled from Alpaca) attributed to the thesis; P&L since creation vs SPY from
the snapshots table; journal since the last review; 7 days of news for the universe; the
trigger payload; wash-sale warnings.

Without --pack-only, runs `claude -p` (cwd = project root, so CLAUDE.md applies) with the pack
and review instructions, journals the answer as kind=review and pushes a summary. Claude may
only change things by calling tools/propose.py.

--all reviews every active thesis whose review cadence is due (settings key last_review:<id>).
Prints one JSON document (--raw prints the bare markdown pack instead, with --pack-only).
Exit 0 ok, 1 refusal (unknown thesis, claude failed), 2 unexpected error.
"""
from __future__ import annotations

import argparse
import json
import subprocess
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
from trader import config, notify  # noqa: E402
from trader.db import connect, deployed_by_thesis, get_setting, journal, qty_by_thesis, row, rows, set_setting  # noqa: E402
from trader.spec import ThesisSpec, describe  # noqa: E402
from web.chat import claude_cmd  # noqa: E402

TIMEOUT_S = 600
CADENCE_DAYS = {"weekly": 7, "monthly": 30, "quarterly": 91}
BENCHMARK = "SPY"

INSTRUCTIONS = """\
---
## Your task

You are reviewing the thesis above for me. Using only the data in this pack (run
`.venv/bin/python tools/positions.py` or other read-only tools if you need fresher numbers):

1. Does the original rationale (and my words in source_text) still hold? Cite the specific news or
   numbers that support or undermine it. Separate company-specific developments from market noise.
2. How has it done versus the {bench} benchmark over the same period? If the pack says there is not
   enough snapshot history, say so; do not estimate.
3. Recommend exactly one of: **HOLD**, **CHANGE** (say what), or **EXIT**. Say what would change your mind.
4. If (and only if) a change or exit is warranted, call `tools/propose.py` with a concrete action
   (spec_change patch, pause, or a sell order) and a one-line reason. Never edit the spec or the
   database directly and never place orders with order.py during a review; proposals go to my phone.
5. Mention any wash-sale warning from the pack if it applies.

Start your answer with the one-word recommendation on its own line, then at most ~12 short lines.
I'm reading this on a phone.
"""


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
        if abs(q) < 1e-9 and abs(dep.get(sym, 0.0)) < 0.005:
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


def journal_section(conn, thesis_id: str) -> str:
    last = row(conn, "SELECT id, ts FROM journal WHERE kind='review' AND thesis_id=? ORDER BY id DESC LIMIT 1", (thesis_id,))
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
    t = row(conn, "SELECT * FROM theses WHERE id=?", (thesis_id,))
    if t is None:
        raise LookupError(f"unknown thesis {thesis_id!r}")
    spec_json = json.loads(t["spec"])
    spec = ThesisSpec.model_validate(spec_json)
    pos_md, _ = positions_section(conn, spec)
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
    return "\n\n".join(parts) + "\n"


# ---------------------------------------------------------------- running claude

def run_claude(prompt: str) -> tuple[bool, str]:
    try:
        cp = subprocess.run(claude_cmd("text"), input=prompt, cwd=str(config.ROOT), capture_output=True,
                            text=True, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return False, f"claude timed out after {TIMEOUT_S // 60} minutes"
    except OSError as e:
        return False, f"could not start claude: {e}"
    text = (cp.stdout or "").strip()
    if cp.returncode != 0 or not text:
        return False, (cp.stderr.strip() or text or f"claude exited {cp.returncode}")[-1000:]
    return True, text


def review_one(conn, thesis_id: str, trigger=None, pack_only: bool = False) -> dict:
    pack = build_pack(conn, thesis_id, trigger)
    if pack_only:
        return {"ok": True, "thesis": thesis_id, "pack": pack}
    log(f"running claude for {thesis_id} ({len(pack)} chars of context)")
    ok, answer = run_claude(pack + "\n" + INSTRUCTIONS.format(bench=BENCHMARK))
    if not ok:
        journal(conn, "error", f"review of {thesis_id} failed: {answer}", thesis_id=thesis_id, data={"trigger": trigger})
        notify.push(f"Review of {thesis_id} failed: {answer[:200]}", title="Review failed", tags=["warning"], conn=conn)
        return {"ok": False, "thesis": thesis_id, "reason": answer}
    journal(conn, "review", answer, thesis_id=thesis_id, data={"trigger": trigger, "pack_chars": len(pack)})
    set_setting(conn, f"last_review:{thesis_id}", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    short = answer if len(answer) <= 300 else answer[:297].rstrip() + "…"
    link = f"{config.WEB_URL.rstrip('/')}/thesis/{thesis_id}?token={config.WEB_TOKEN}"
    notify.push(short, title=f"Review: {thesis_id}", tags=["mag"], click=link, conn=conn)
    return {"ok": True, "thesis": thesis_id, "review": answer}


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
                results = [review_one(conn, i, trigger, args.pack_only) for i in ids]
                doc = {"ok": all(r["ok"] for r in results), "due": ids, "results": results}
            else:
                try:
                    doc = review_one(conn, args.thesis, trigger, args.pack_only)
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
