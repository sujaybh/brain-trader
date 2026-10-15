#!/usr/bin/env python3
"""Execute the due DCA slots of one thesis, exactly per docs/thesis-spec.md ("How a DCA slot
executes"). Safe to re-run any number of times: every order carries a deterministic
client_order_id and finished slots are remembered."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

from tools import _common as C  # noqa: E402
from trader import config, db  # noqa: E402
from trader import notify as N  # noqa: E402
from trader.client import BrokerError  # noqa: E402
from trader.schedule import client_order_id, dca_allocation, due_slots, slot_key  # noqa: E402

# Refusals that mean "not now" rather than "not this order": the slot stays pending.
TRANSIENT_CHECKS = {"market_hours", "halt"}


def _done_key(tid: str, key: str) -> str:
    return f"slot_done:{tid}:{key}"


def _slot_floor(conn, row: dict) -> datetime | None:
    """Slots at or before this instant are never executed: before the thesis existed, or
    before its last resume (paused theses do not catch up)."""
    cands = []
    for s in (row.get("created"), db.get_setting(conn, f"resumed:{row['id']}")):
        if s:
            try:
                cands.append(C.parse_dt(s))
            except ValueError:
                pass
    return max(cands) if cands else None


def _record_refusal(conn, coid: str, tid: str, sym: str, usd: float, reason: str) -> None:
    """Remember the refusal so the slot is not retried (and re-pushed) every tick. If the broker
    already wrote a row for this id, this is a no-op."""
    now = db.now_iso()
    conn.execute(
        "INSERT OR IGNORE INTO orders(client_order_id, thesis_id, symbol, side, notional, status, reason, created, updated) "
        "VALUES (?,?,?,?,?,?,?,?,?)", (coid, tid, sym, "buy", usd, "refused", reason, now, now))


def _clock(client, cache: dict | None) -> dict:
    if cache is not None and "clock" in cache:
        return cache["clock"]
    c = client.clock()
    if cache is not None:
        cache["clock"] = c
    return c


def run_thesis(thesis_id: str, *, now: datetime | None = None, dry_run: bool = False,
               lookback_hours: float | None = None, conn=None, client=None,
               clock_cache: dict | None = None) -> dict:
    conn = conn or C.connect()
    limits = config.load_limits()
    catchup = float(lookback_hours if lookback_hours is not None else limits["catchup_hours"])
    min_n = float(limits.get("min_order_notional", 1.0))
    now = now or datetime.now(timezone.utc)
    row, spec = C.load_thesis(conn, thesis_id)
    res = {"thesis": thesis_id, "now": now.isoformat(), "dry_run": dry_run, "status": None,
           "slots": [], "pending": [], "expired": [], "placed": 0, "refused": 0, "errors": []}

    if row["status"] != "active":
        res["status"] = f"skipped: thesis is {row['status']}"
        return res
    if spec.schedule.type != "dca":
        res["status"] = "no schedule"
        return res

    floor = _slot_floor(conn, row)
    after_floor = (lambda s: floor is None or s > floor)
    done = (lambda key: db.get_setting(conn, _done_key(thesis_id, key)) is not None)

    # 1. Slots that fell out of the catch-up window since the last tick: journal `expired` once.
    tick_key = f"last_tick:{thesis_id}"
    prev_s = db.get_setting(conn, tick_key)
    window_start = now - timedelta(hours=catchup)
    # First run (no last_tick): look back at most one extra window, never to the dawn of time.
    prev = C.parse_dt(prev_s) if prev_s else max(floor or window_start, window_start)
    exp_from = prev - timedelta(hours=catchup)
    if exp_from < window_start:
        span = (window_start - exp_from).total_seconds() / 3600
        for s in due_slots(spec, window_start, span):
            key = slot_key(s)
            if after_floor(s) and not done(key):
                res["expired"].append(key)
                if not dry_run:
                    db.journal(conn, "expired", f"slot {key} expired: not executed within {catchup:g}h",
                               thesis_id=thesis_id, data={"slot": key, "catchup_hours": catchup})
                    db.set_setting(conn, _done_key(thesis_id, key), "expired")
    if not dry_run and (prev_s is None or now > prev):
        db.set_setting(conn, tick_key, now.astimezone(timezone.utc).isoformat(timespec="seconds"))

    # 2. Due slots inside the window that are not finished yet.
    pending = [s for s in due_slots(spec, now, catchup) if after_floor(s) and not done(slot_key(s))]
    res["pending"] = [slot_key(s) for s in pending]
    if not pending:
        res["status"] = "nothing due"
        return res
    if config.HALT_PATH.exists():
        res["status"] = "halted"
        return res
    client = client or C.broker()
    clk = _clock(client, clock_cache)
    if not clk.get("is_open"):
        res["status"] = "market closed"
        res["next_open"] = clk.get("next_open")
        return res

    # 3. Execute, oldest slot first.
    stop = False
    for s in pending:
        key = slot_key(s)
        committed = C.committed_for(conn, thesis_id)
        alloc = dca_allocation(spec, committed, min_notional=min_n)
        entry = {"slot": key, "allocation": alloc, "orders": [], "refused": [], "already": [], "status": "done"}
        res["slots"].append(entry)
        if not alloc:
            entry["status"] = "skipped: thesis fully deployed"
            if not dry_run:
                db.journal(conn, "skipped", f"slot {key} skipped: thesis fully deployed", thesis_id=thesis_id,
                           data={"slot": key, "committed": round(sum(committed.values()), 2),
                                 "capital_cap": spec.capital_cap})
                db.set_setting(conn, _done_key(thesis_id, key), "skipped")
            continue
        for sym, usd in alloc.items():
            coid = client_order_id(thesis_id, sym, key)
            if db.row(conn, "SELECT 1 AS x FROM orders WHERE client_order_id=?", (coid,)):
                entry["already"].append({"symbol": sym, "client_order_id": coid})
                continue
            if dry_run:
                entry["orders"].append({"symbol": sym, "notional": usd, "client_order_id": coid, "would_place": True})
                continue
            try:
                o = client.place_order(thesis_id=thesis_id, side="buy", symbol=sym, notional=usd,
                                       client_order_id=coid, reason=f"dca slot {key}")
            except BrokerError as e:
                check = e.detail.get("check") if isinstance(e.detail, dict) else None
                if e.refused and check not in TRANSIENT_CHECKS:
                    _record_refusal(conn, coid, thesis_id, sym, usd, e.reason)
                    entry["refused"].append({"symbol": sym, "notional": usd, "client_order_id": coid,
                                             "reason": e.reason, "check": check})
                    res["refused"] += 1
                    continue  # the other symbols in the slot still execute
                # market closed under us, halted, broker down: leave the slot pending
                entry["status"] = "interrupted"
                res["errors"].append({"slot": key, "symbol": sym, "status": e.status, "reason": e.reason,
                                      "check": check})
                stop = True
                break
            if o.get("duplicate"):
                entry["already"].append({"symbol": sym, "client_order_id": coid})
                continue
            entry["orders"].append(o)
            res["placed"] += 1
        if stop:
            break
        if dry_run:
            continue
        db.set_setting(conn, _done_key(thesis_id, key), "done")
        if entry["orders"] or entry["refused"]:
            _summarize(conn, thesis_id, spec, key, entry)

    res["status"] = "interrupted" if stop else ("dry run" if dry_run else "ran")
    return res


def _summarize(conn, thesis_id, spec, key, entry) -> None:
    bought = []
    for o in entry["orders"]:
        px = o.get("filled_avg_price") or o.get("limit_price")
        amt = o.get("notional") or 0
        bought.append(f"${float(amt):.2f} {o['symbol']}" + (f" @ {float(px):.2f}" if px else ""))
    total = sum(C.committed_for(conn, thesis_id).values())
    text = ""
    if bought:
        text = f"Bought {', '.join(bought)} ({thesis_id}). "
    if entry["refused"]:
        text += "Refused: " + "; ".join(f"{r['symbol']} ({r['reason']})" for r in entry["refused"]) + ". "
    text += f"Thesis at ${total:,.0f}/${spec.capital_cap:,.0f} deployed."
    db.journal(conn, "dca", f"slot {key}: {text}", thesis_id=thesis_id,
               data={"slot": key, "orders": [o.get("client_order_id") for o in entry["orders"]],
                     "refused": entry["refused"]})
    # Fills and refusals are pushed by the broker as they happen; a second push here would
    # double-buzz the phone. The slot summary lives in the journal.


def _main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("id")
    ap.add_argument("--now", help="pretend it is this ISO time (naive = America/New_York)")
    ap.add_argument("--dry-run", action="store_true", help="show what would be placed; write nothing")
    ap.add_argument("--lookback-hours", type=float, help="override catchup_hours from limits.json")
    a = ap.parse_args(argv)
    now = C.parse_dt(a.now) if a.now else None
    res = run_thesis(a.id, now=now, dry_run=a.dry_run, lookback_hours=a.lookback_hours)
    if res["errors"]:
        return {"ok": False, "reason": res["errors"][0]["reason"], **res}, 2 if any(
            e["status"] != 403 for e in res["errors"]) else 1
    if res["refused"]:
        return {"ok": False, "reason": f"{res['refused']} order(s) refused by the broker", **res}, 1
    return {"ok": True, **res}


def main(argv=None) -> int:
    return C.run(_main, argv)


if __name__ == "__main__":
    sys.exit(main())
