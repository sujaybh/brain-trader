#!/usr/bin/env python3
"""Turn the signals of strategy theses (spec `strategy.type` other than dca) into proposals.

Confirm mode only: every trade becomes a proposal with Approve/Reject buttons; nothing is
ordered directly, and a thesis in `auto` mode is skipped rather than auto-executed.
Idempotent per rebalance period: once a thesis has been decided for a period (even with no
trades), re-running is a no-op until the next period. Sells are proposed before buys; the
buys are sized as if the sells went through, so approve the sells first.

Data: stored daily bars (tools/bars.py) dated before today, plus live broker quotes for
sizing. Refuses to run on missing or stale bars. Not wired into any timer; see
docs/thesis-spec.md ("Strategy theses") for how to run or schedule it."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402
import json  # noqa: E402
from datetime import datetime, time, timedelta, timezone  # noqa: E402

from tools import _common as C  # noqa: E402
from trader import config, db  # noqa: E402

import strategies  # noqa: E402
from strategies import MarketData, ThesisState  # noqa: E402
from strategies.caps import apply_caps  # noqa: E402

STALE_DAYS = 5          # newest bar older than this (calendar days) = stale data, refuse
NON_STRATEGY = ("dca", "none")


def _done_key(tid: str, period: str) -> str:
    return f"strategy_done:{tid}:{period}"


def strategy_theses(conn) -> list[str]:
    out = []
    for r in C.list_theses(conn, ("active",)):
        try:
            _, spec = C.load_thesis(conn, r["id"])
        except Exception:  # noqa: BLE001  an unreadable spec is reported by thesis_show, not here
            continue
        if spec.strategy_type() not in NON_STRATEGY:
            out.append(r["id"])
    return out


def _expires_in(sig_expires: str | None, now: datetime) -> timedelta:
    if not sig_expires:
        return timedelta(hours=24)
    d = datetime.fromisoformat(sig_expires)
    if d.tzinfo is None:
        d = datetime.combine(d.date(), time(16, 0), config.MARKET_TZ) if len(sig_expires) == 10 else \
            d.replace(tzinfo=config.MARKET_TZ)
    return max(d - now, timedelta(hours=1))


def run_strategy(thesis_id: str, *, now: datetime | None = None, dry_run: bool = False,
                 conn=None, client=None) -> dict:
    conn = conn or C.connect()
    now = now or datetime.now(timezone.utc)
    limits = config.load_limits()
    row, spec = C.load_thesis(conn, thesis_id)
    st = spec.strategy_type()
    res = {"thesis": thesis_id, "strategy": st, "dry_run": dry_run, "status": None, "period": None,
           "signals": [], "orders": [], "adjustments": [], "proposals": []}
    if row["status"] != "active":
        res["status"] = f"skipped: thesis is {row['status']}"
        return res
    if st in NON_STRATEGY:
        res["status"] = f"skipped: {st} thesis (DCA runs on the scheduler tick)"
        return res
    if spec.mode != "confirm":
        res["status"] = "skipped: auto mode is not enabled for strategies yet (set mode to confirm)"
        return res
    if config.HALT_PATH.exists():
        res["status"] = "halted"
        return res

    impl = strategies.get(st)
    today = now.astimezone(config.MARKET_TZ).date()
    period = impl.period(spec, today)
    res["period"] = period
    done = db.get_setting(conn, _done_key(thesis_id, period))
    if done is not None:
        res["status"] = f"already decided for {period}"
        res["proposals"] = json.loads(done).get("proposals", []) if done.startswith("{") else []
        return res
    pending = db.rows(conn, "SELECT id FROM proposals WHERE thesis_id=? AND status='pending' AND expires>?",
                      (thesis_id, now.astimezone(timezone.utc).isoformat(timespec="seconds")))
    if pending:
        res["status"] = "waiting: earlier proposals for this thesis are still pending; decide those first"
        res["proposals"] = [p["id"] for p in pending]
        return res

    need = impl.warmup(spec)
    since = (today - timedelta(days=int(need * 1.6) + 30)).isoformat()
    bars = db.load_bars(conn, spec.universe, start=since)
    short = {s: len(b) for s, b in bars.items() if len([r for r in b if r["date"] < today.isoformat()]) < need}
    stale_before = (today - timedelta(days=STALE_DAYS)).isoformat()
    stale = {s: b[-1]["date"] for s, b in bars.items() if b and b[-1]["date"] < stale_before}
    if short or stale:
        res["status"] = "not enough data"
        res["reason"] = ("run tools/bars.py first: " +
                         "; ".join([f"{s} has {n} of {need} bars" for s, n in short.items()] +
                                   [f"{s} last bar {d}" for s, d in stale.items()]))
        return res

    client = client or C.broker()
    qty = {s: q for s, q in db.qty_by_thesis(conn).get(thesis_id, {}).items() if q > 1e-9}
    syms = sorted(set(spec.universe) | set(qty))
    quotes = client.quote(syms) or {}
    prices = {s: p for s in syms if (p := C.quote_price(quotes.get(s))) is not None}
    deployed_all = db.deployed_by_thesis(conn)
    state = ThesisState(
        qty=qty, deployed=C.committed_for(conn, thesis_id),
        last_period=db.get_setting(conn, f"strategy_period:{thesis_id}"),
        other_deployed=sum(max(v, 0.0) for t, per in deployed_all.items() if t != thesis_id for v in per.values()))
    data = MarketData.view({s: bars.get(s, []) for s in syms}, today, prices=prices)
    sigs = impl.signals(spec, data, state)
    orders, notes = apply_caps(sigs, spec, state, limits, prices)
    inputs = {"bars_through": {s: data.last_date(s) for s in syms}, "prices": prices,
              "qty": state.qty, "deployed": state.deployed}
    if st == "momentum":
        from strategies.momentum import ranking
        inputs["ranking"] = [[s, round(r, 6)] for s, r in ranking(spec, data)]
    res.update(signals=[s.as_dict() for s in sigs], orders=[o.as_dict() for o in orders], adjustments=notes,
               inputs=inputs)
    if dry_run:
        res["status"] = "dry run"
        return res

    from web import proposals as P  # imported late: pulls in the web package
    db.set_setting(conn, _done_key(thesis_id, period), json.dumps({"status": "started", "at": db.now_iso()}))
    db.set_setting(conn, f"strategy_period:{thesis_id}", period)
    for o in orders:
        mode = db.row(conn, "SELECT mode FROM theses WHERE id=?", (thesis_id,))["mode"]
        if mode != "confirm":  # never let a mode flip turn a proposal into a direct order
            res["adjustments"].append({"signal": o.as_dict(), "action": "dropped", "why": "thesis left confirm mode"})
            continue
        try:
            p = P.create(conn, thesis_id, o.action(), o.reason, _expires_in(o.expires, now))
            res["proposals"].append(p["id"])
        except P.ProposalError as e:
            res["adjustments"].append({"signal": o.as_dict(), "action": "dropped", "why": e.reason})
    db.set_setting(conn, _done_key(thesis_id, period),
                   json.dumps({"status": "done", "at": db.now_iso(), "proposals": res["proposals"]}))
    msg = (f"{st} {period}: {len(res['proposals'])} proposal(s)" if res["proposals"]
           else f"{st} {period}: no trades (holding)")
    db.journal(conn, "strategy", msg, thesis_id=thesis_id,
               data={"period": period, "signals": res["signals"], "orders": res["orders"],
                     "adjustments": res["adjustments"], "proposals": res["proposals"], "inputs": inputs})
    res["status"] = "proposed" if res["proposals"] else "no trades"
    return res


def _main(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="*", help="thesis ids (default: every active thesis with a strategy block)")
    ap.add_argument("--now", help="pretend it is this ISO time (naive = America/New_York)")
    ap.add_argument("--dry-run", action="store_true", help="show signals and capped orders; write nothing")
    a = ap.parse_args(argv)
    now = C.parse_dt(a.now) if a.now else None
    conn = C.connect()
    ids = a.ids or strategy_theses(conn)
    results = [run_strategy(i, now=now, dry_run=a.dry_run, conn=conn) for i in ids]
    bad = [r for r in results if r["status"] == "not enough data"]
    out = {"ok": not bad, "results": results}
    if bad:
        out["reason"] = "; ".join(f"{r['thesis']}: {r['reason']}" for r in bad)
    return out


def main(argv=None) -> int:
    return C.run(_main, argv)


if __name__ == "__main__":
    sys.exit(main())
