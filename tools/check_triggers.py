#!/usr/bin/env python3
"""Deterministic trigger check (no LLM). For each active thesis evaluates drawdown_pct,
price_below/price_above and exit.stop_loss_pct/take_profit_pct; each firing is journaled
(kind=trigger), pushed, and acted on per its action (pause / propose_exit / review). The same
(thesis, trigger type, symbol) does not re-fire within 24h. `news` triggers are evaluated by
tools/news.py, not here."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402
import json  # noqa: E402
import subprocess  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

from tools import _common as C  # noqa: E402
from trader import db  # noqa: E402
from trader import notify as N  # noqa: E402

DEBOUNCE = timedelta(hours=24)
TOOLS = Path(__file__).resolve().parent


def call_tool(name: str, args: list[str], timeout: float = 900) -> dict:
    """Run a sibling tool; tolerate it not existing yet. Returns a small result dict."""
    path = TOOLS / name
    if not path.exists():
        C.log(f"[check_triggers] {name} not present; skipping")
        return {"ran": False, "reason": f"tools/{name} not present"}
    try:
        p = subprocess.run([sys.executable, str(path), *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ran": False, "reason": f"{type(e).__name__}: {e}"}
    if p.stderr:
        C.log(p.stderr.rstrip())
    try:
        out = json.loads(p.stdout) if p.stdout.strip() else None
    except ValueError:
        out = p.stdout[-2000:]
    return {"ran": True, "exit": p.returncode, "output": out}


def _plpc_pct(pos: dict | None) -> float | None:
    """Alpaca's unrealized_plpc is a fraction (-0.15 = -15%)."""
    v = C.fnum((pos or {}).get("unrealized_plpc"))
    return None if v is None else round(v * 100, 3)


def evaluate(spec, positions: dict, quotes: dict, held: dict) -> list[dict]:
    """Pure: list of firings for one thesis."""
    fired = []
    held_syms = [s for s in spec.universe if held.get(s, 0) > 1e-9 and s in positions]
    for t in spec.triggers:
        if t.type == "drawdown_pct":
            for s in held_syms:
                pc = _plpc_pct(positions[s])
                if pc is not None and pc <= -abs(t.value):
                    fired.append({"type": t.type, "symbol": s, "threshold": t.value, "observed_pct": pc,
                                  "action": t.action, "why": f"{s} is {pc:.1f}% from avg entry (limit -{abs(t.value):g}%)"})
        elif t.type in ("price_below", "price_above"):
            for s in ([t.symbol.upper()] if t.symbol else spec.universe):
                px = C.quote_price(quotes.get(s))
                if px is None:
                    continue
                if (t.type == "price_below" and px < t.value) or (t.type == "price_above" and px > t.value):
                    word = "below" if t.type == "price_below" else "above"
                    fired.append({"type": t.type, "symbol": s, "threshold": t.value, "observed_price": px,
                                  "action": t.action, "why": f"{s} at ${px:.2f}, {word} ${t.value:g}"})
    for kind, val in (("stop_loss", spec.exit.stop_loss_pct), ("take_profit", spec.exit.take_profit_pct)):
        if not val:
            continue
        for s in held_syms:
            pc = _plpc_pct(positions[s])
            if pc is None:
                continue
            if (kind == "stop_loss" and pc <= -val) or (kind == "take_profit" and pc >= val):
                # review_only=true: a proposal, never an automatic sell (see open issues for false)
                fired.append({"type": kind, "symbol": s, "threshold": val, "observed_pct": pc,
                              "action": "propose_exit", "review_only": spec.exit.review_only,
                              "why": f"{s} at {pc:+.1f}% ({kind.replace('_', ' ')} at {'-' if kind == 'stop_loss' else '+'}{val:g}%)"})
    return fired


def check_triggers(thesis_id: str | None = None, *, now: datetime | None = None, conn=None, client=None) -> dict:
    conn = conn or C.connect()
    client = client or C.broker()
    now = now or datetime.now(timezone.utc)
    rows = [r for r in C.list_theses(conn, ("active",)) if thesis_id is None or r["id"] == thesis_id]
    if thesis_id and not rows:
        r, _ = C.load_thesis(conn, thesis_id)
        return {"checked": [], "fired": [], "debounced": [], "note": f"thesis {thesis_id} is {r['status']}"}
    positions = C.positions_by_symbol(client.positions())
    qty = db.qty_by_thesis(conn)
    specs = {r["id"]: C.load_thesis(conn, r["id"])[1] for r in rows}
    want_quotes = sorted({(t.symbol.upper() if t.symbol else s) for sp in specs.values() for t in sp.triggers
                          if t.type in ("price_below", "price_above") for s in sp.universe})
    quotes = client.quote(want_quotes) if want_quotes else {}

    out = {"checked": list(specs), "fired": [], "debounced": []}
    for tid, spec in specs.items():
        held = qty.get(tid, {})
        for f in evaluate(spec, positions, quotes, held):
            key = f"trigger:{tid}:{f['type']}:{f['symbol']}"
            last = db.get_setting(conn, key)
            if last and now - C.parse_dt(last) < DEBOUNCE:
                out["debounced"].append({"thesis": tid, **f, "last_fired": last})
                continue
            db.set_setting(conn, key, now.astimezone(timezone.utc).isoformat(timespec="seconds"))
            f = {"thesis": tid, **f}
            db.journal(conn, "trigger", f"{f['type']} fired: {f['why']} -> {f['action']}", thesis_id=tid,
                       symbol=f["symbol"], data=f)
            N.push(f"{f['why']} ({tid}). Action: {f['action']}.", title=f"Trigger: {f['type']} {f['symbol']}",
                   priority=4, tags=["warning"], conn=conn)
            f["result"] = _act(conn, tid, f, held)
            out["fired"].append(f)
    return out


def _act(conn, tid: str, f: dict, held: dict) -> dict:
    if f["action"] == "pause":
        r, _ = C.load_thesis(conn, tid)
        if r["status"] != "active":
            return {"paused": False, "reason": f"already {r['status']}"}
        with db.tx(conn):
            C.set_thesis_status(conn, tid, "paused")
            db.journal(conn, "thesis", f"paused by trigger: {f['why']}", thesis_id=tid, data={"status": "paused"})
        return {"paused": True}
    if f["action"] == "propose_exit":
        q = round(held.get(f["symbol"], 0.0), 6)
        if q <= 0:
            return {"ran": False, "reason": "thesis holds no shares of " + f["symbol"]}
        action = {"type": "order", "side": "sell", "symbol": f["symbol"], "qty": q}
        return call_tool("propose.py", ["--thesis", tid, "--action", json.dumps(action),
                                        "--reason", f"{f['type']} trigger: {f['why']}"])
    if f["action"] == "review":
        return call_tool("review.py", ["--thesis", tid, "--trigger", json.dumps(f)])
    return {"ran": False, "reason": f"unknown action {f['action']}"}


def _main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--thesis")
    ap.add_argument("--now", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    return {"ok": True, **check_triggers(a.thesis, now=C.parse_dt(a.now) if a.now else None)}


def main(argv=None) -> int:
    return C.run(_main, argv)


if __name__ == "__main__":
    sys.exit(main())
