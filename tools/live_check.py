#!/usr/bin/env python3
"""Market-hours smoke test against the REAL broker daemon (paper), with tiny sizes.

Phases (run several times on a trading day):
  open   (~09:35 ET)  health/clock/account/quotes, a tiny buy via order.py and its fill,
                      duplicate client_order_id, per_order_cap / asset / side / halt refusals
  slot   (~10:05 ET)  the most recent DCA slot of --thesis placed exactly one order per name,
                      is marked done, is idempotent, shows up in positions and the journal
  idle   (any time)   proposal -> web approve -> sell, /api/state, news, snapshot, triggers,
                      review pack, journal
  close  (~15:50 ET)  no stale open orders, PDT count, every order this script placed today
                      filled or journaled, manual thesis under its cap, no HALT
  all                 open -> idle -> close (not slot)

Prints one JSON document on stdout and a table on stderr. Exit 0 if every check in the phase
passed, 1 if any failed, 2 on crash. Writes exactly one journal entry (kind=system) per run.
The only file it may remove is data/HALT, and only when this run created it (content contains
"live_check")."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402
import json  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import requests  # noqa: E402
from croniter import croniter  # noqa: E402

from tools import _common as C  # noqa: E402
from trader import config, db  # noqa: E402
from trader.client import BrokerError  # noqa: E402
from trader.schedule import client_order_id, slot_key  # noqa: E402

TOOLS = Path(__file__).resolve().parent
POLL_INTERVAL_S = 10
POLL_TIMEOUT_S = 120
STALE_MINUTES = 30
TAG = "live_check"
OPEN_STATUSES = ("submitted", "partially_filled")


# ------------------------------------------------------------------ plan (also used by --dry-run)

PLAN = {
    "open": [
        ("health", "GET /health: ok, not halted, paper"),
        ("clock_open", "GET /clock: is_open (order checks are skipped if closed)"),
        ("account_cash", "GET /account: cash > 0"),
        ("quotes", "GET /quote SPY,<symbol>: bid > 0 and ask > 0 (recorded)"),
        ("asset_unknown", "GET /assets ZZZZ: null"),
        ("order_buy", "tools/order.py buy $<size> <symbol> into manual: submitted or filled"),
        ("order_fill", "poll /reconcile + /orders/{id} every 10s up to 120s until filled"),
        ("duplicate_id", "place_order twice with one explicit client_order_id: 2nd is duplicate"),
        ("refuse_per_order_cap", "$5000 buy: refused, check per_order_cap"),
        ("refuse_asset", "buy BTCUSD: refused, check asset"),
        ("refuse_side", "sell 100 sh <symbol>: refused, check side"),
        ("refuse_halt", "halt.py on (live_check), order refused with check halt, halt.py off"),
    ],
    "slot": [
        ("slot_orders", "one order per universe name for the slot, all filled/submitted, no extras"),
        ("slot_done", "settings has slot_done:<thesis>:<slot>"),
        ("slot_idempotent", "tools/run_thesis.py <thesis> places 0 orders"),
        ("slot_positions", "tools/positions.py: thesis cost ~ expected (+-$2) and pct_deployed"),
        ("slot_journal", "exactly one kind=dca journal entry for the slot"),
    ],
    "idle": [
        ("proposal_approve", "propose.py sell $2 SPY (manual), approve via web API: executed, sell order"),
        ("api_state", "GET /api/state: JSON with theses and account"),
        ("news", "tools/news.py SPY --since 24h --limit 5: ok, model, well-formed overall (90s)"),
        ("snapshot", "tools/snapshot.py: rows written incl. $BENCH SPY for today"),
        ("check_triggers", "tools/check_triggers.py: ok"),
        ("review_pack", "tools/review.py --thesis <thesis> --pack-only: pack names the universe"),
        ("journal_recent", "tools/journal.py --since 1h: has entries"),
    ],
    "close": [
        ("no_stale_orders", f"no order from today in submitted/partially_filled for > {STALE_MINUTES} min"),
        ("pdt", "/account daytrade_count <= 1 and equals pdt_log rows in the last 5 business days"),
        ("script_orders", "every order live_check placed today filled, every expected refusal journaled"),
        ("manual_cap", "deployed in manual <= manual_thesis_cap"),
        ("no_halt", "data/HALT does not exist"),
    ],
}
PHASES = {"open": ["open"], "slot": ["slot"], "idle": ["idle"], "close": ["close"],
          "all": ["open", "idle", "close"]}


# ------------------------------------------------------------------ helpers

def now_et() -> datetime:
    return datetime.now(config.MARKET_TZ)


def run_tool(name: str, args: list[str], timeout: float = 120) -> tuple[int, dict | None, str]:
    """Run a sibling tool as a subprocess (exactly as the user/Claude would). Returns
    (exit code, parsed JSON or None, stderr tail). Tests monkeypatch this."""
    try:
        p = subprocess.run([sys.executable, str(TOOLS / name), *args], capture_output=True, text=True,
                           timeout=timeout, cwd=str(config.ROOT))
    except subprocess.TimeoutExpired:
        return -1, None, f"timed out after {timeout:g}s"
    try:
        out = json.loads(p.stdout) if p.stdout.strip() else None
    except ValueError:
        out = None
    return p.returncode, out, (p.stderr or "")[-500:]


def _refusal(e: BrokerError) -> str | None:
    return e.detail.get("check") if isinstance(e.detail, dict) else None


def _fmt(x) -> str:
    return "None" if x is None else f"{float(x):g}"


class Ctx:
    def __init__(self, args):
        self.args = args
        self.symbol = args.symbol.strip().upper()
        self.size = float(args.size)
        self.thesis = args.thesis
        self.conn = C.connect()
        self.client = C.broker()
        self.orders_placed: list[str] = []        # client_order_ids this run caused
        self.expected_refusals: list[dict] = []   # {"check", "symbol", "at"} we provoked
        self.state: dict = {}                     # values shared between checks
        self.skip_orders: str | None = None       # reason order checks are skipped

    def placed(self, coid: str | None) -> None:
        if coid and coid not in self.orders_placed:
            self.orders_placed.append(coid)


def check(name: str, fn, ctx: Ctx) -> dict:
    t0 = time.monotonic()
    try:
        r = fn(ctx)
        ok, detail = r[0], r[1]
        data = r[2] if len(r) > 2 else None
    except BrokerError as e:
        ok, detail, data = False, f"broker error {e.status}: {e.reason}", None
    except Exception as e:  # noqa: BLE001 - a crashing check is a failed check, the rest still run
        ok, detail, data = False, f"error: {type(e).__name__}: {e}", None
    res = {"name": name, "ok": bool(ok), "detail": str(detail), "duration_s": round(time.monotonic() - t0, 2)}
    if data is not None:
        res["data"] = data
    return res


def expect_refusal(ctx: Ctx, want: str, **order) -> tuple:
    if ctx.skip_orders:
        return False, f"skipped: {ctx.skip_orders}"
    try:
        o = ctx.client.place_order(thesis_id="manual", reason=f"{TAG}: expect {want} refusal", **order)
    except BrokerError as e:
        got = _refusal(e)
        if e.refused:
            ctx.expected_refusals.append({"check": got, "symbol": order["symbol"],
                                          "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        if e.refused and got == want:
            return True, f"refused ({got}): {e.reason}"
        return False, f"wrong outcome: expected refusal {want}, got HTTP {e.status} check={got}: {e.reason}"
    # Not refused at all: record and try to cancel so the main session can see what happened.
    coid = o.get("client_order_id")
    ctx.placed(coid)
    note = ""
    if o.get("status") in OPEN_STATUSES and coid:
        try:
            ctx.client.cancel(coid)
            note = " (canceled)"
        except BrokerError as e:
            note = f" (cancel failed: {e.reason})"
    return False, f"NOT refused: order {coid} {o.get('status')}{note}; expected check {want}"


# ------------------------------------------------------------------ phase: open

def c_health(ctx):
    h = ctx.client.health()
    ok = bool(h.get("ok")) and not h.get("halted") and h.get("paper") is not False
    if h.get("paper") is False:
        ctx.skip_orders = "broker is NOT paper"
    elif h.get("halted"):
        ctx.skip_orders = "broker is halted"
    return ok, f"ok={h.get('ok')} backend={h.get('backend')} paper={h.get('paper')} halted={h.get('halted')}", h


def c_clock(ctx):
    c = ctx.client.clock()
    if not c.get("is_open"):
        ctx.skip_orders = ctx.skip_orders or f"market closed (next open {c.get('next_open')})"
    return bool(c.get("is_open")), f"is_open={c.get('is_open')} next_close={c.get('next_close')}", c


def c_account(ctx):
    a = ctx.client.account()
    cash = C.fnum(a.get("cash")) or 0
    ctx.state["account"] = a
    return cash > 0, f"cash={cash:,.2f} equity={a.get('equity')} daytrade_count={a.get('daytrade_count')}"


def c_quotes(ctx):
    syms = list(dict.fromkeys(["SPY", ctx.symbol]))
    q = ctx.client.quote(syms)
    bad, parts = [], []
    for s in syms:
        x = q.get(s) or {}
        bid, ask = C.fnum(x.get("bid")) or 0, C.fnum(x.get("ask")) or 0
        parts.append(f"{s} bid={bid:g} ask={ask:g} last={x.get('last')}")
        if not (bid > 0 and ask > 0):
            bad.append(s)
    ctx.state["quotes"] = q
    return not bad, ("; ".join(parts) + (f"; BAD: {','.join(bad)}" if bad else "")), q


def c_asset_unknown(ctx):
    a = ctx.client.assets(["ZZZZ"])
    return "ZZZZ" in a and a["ZZZZ"] is None, f"ZZZZ -> {a.get('ZZZZ', '<missing key>')}"


def c_order_buy(ctx):
    if ctx.skip_orders:
        return False, f"skipped: {ctx.skip_orders}"
    code, out, err = run_tool("order.py", ["--side", "buy", "--symbol", ctx.symbol, "--notional", f"{ctx.size:g}",
                                           "--reason", f"{TAG}: test buy"])
    o = (out or {}).get("order") or {}
    coid = o.get("client_order_id")
    ctx.placed(coid)
    ctx.state["buy"] = o
    ok = code == 0 and o.get("status") in ("submitted", "filled", "partially_filled") and bool(coid)
    if not ok:
        return False, f"exit {code}: {(out or {}).get('reason') or err.strip()[-200:]} check={(out or {}).get('check')}"
    return True, f"{coid} {o.get('status')} limit={o.get('limit_price')} qty={o.get('qty')}", o


def c_order_fill(ctx):
    o = ctx.state.get("buy")
    if not o or not o.get("client_order_id"):
        return False, "skipped: no buy order to watch"
    coid, t0 = o["client_order_id"], time.monotonic()
    cur = o
    while True:
        if cur.get("status") == "filled":
            el = round(time.monotonic() - t0, 1)
            lim, px = C.fnum(cur.get("limit_price")), C.fnum(cur.get("filled_avg_price"))
            diff = f" ({(px - lim) / lim * 100:+.3f}% vs limit)" if lim and px else ""
            return True, f"filled {cur.get('filled_qty')} @ {_fmt(px)} limit {_fmt(lim)}{diff} after {el}s", \
                {"client_order_id": coid, "filled_avg_price": px, "limit_price": lim, "elapsed_s": el}
        if cur.get("status") not in OPEN_STATUSES:
            return False, f"order ended {cur.get('status')} (filled_qty {cur.get('filled_qty')})"
        if time.monotonic() - t0 >= POLL_TIMEOUT_S:
            break
        time.sleep(POLL_INTERVAL_S)
        ctx.client.reconcile()
        cur = ctx.client.order(coid)
    ask = None
    try:
        ask = (ctx.client.quote([ctx.symbol]).get(ctx.symbol) or {}).get("ask")
    except BrokerError:
        pass
    return False, (f"unfilled: ask may have moved; limit was {_fmt(cur.get('limit_price'))}, current ask {_fmt(ask)} "
                   f"(status {cur.get('status')}, left open for the broker/tick to reconcile)")


def c_duplicate(ctx):
    if ctx.skip_orders:
        return False, f"skipped: {ctx.skip_orders}"
    coid = f"live-check-{now_et().strftime('%Y%m%dT%H%M%S')}-dup"
    kw = dict(thesis_id="manual", side="buy", symbol=ctx.symbol, notional=ctx.size, client_order_id=coid,
              reason=f"{TAG}: duplicate-id test")
    first = ctx.client.place_order(**kw)
    ctx.placed(first.get("client_order_id") or coid)
    second = ctx.client.place_order(**kw)
    ok = not first.get("duplicate") and second.get("duplicate") is True \
        and second.get("client_order_id", coid) == coid
    return ok, f"{coid}: first {first.get('status')} duplicate={first.get('duplicate', False)}, " \
               f"second duplicate={second.get('duplicate')}"


def c_refuse_cap(ctx):
    return expect_refusal(ctx, "per_order_cap", side="buy", symbol=ctx.symbol, notional=5000)


def c_refuse_asset(ctx):
    return expect_refusal(ctx, "asset", side="buy", symbol="BTCUSD", notional=ctx.size)


def c_refuse_side(ctx):
    return expect_refusal(ctx, "side", side="sell", symbol=ctx.symbol, qty=100)


def _halt_content() -> str | None:
    p = config.HALT_PATH
    try:
        return p.read_text() if p.exists() else None
    except OSError:
        return None


def c_refuse_halt(ctx):
    if ctx.skip_orders:
        return False, f"skipped: {ctx.skip_orders}"
    if config.HALT_PATH.exists():
        return False, f"HALT already exists (not ours, not touching it): {(_halt_content() or '').strip()[:120]}"
    created = False
    try:
        code, out, err = run_tool("halt.py", ["on", "--reason", TAG])
        content = _halt_content() or ""
        created = TAG in content
        if code != 0 or not created:
            return False, f"halt.py on failed (exit {code}): {(out or {}).get('reason') or err.strip()[-200:]}"
        ok, detail = expect_refusal(ctx, "halt", side="buy", symbol=ctx.symbol, notional=ctx.size)
    finally:
        if created and TAG in (_halt_content() or ""):
            run_tool("halt.py", ["off", "--reason", f"{TAG} done"])
    if config.HALT_PATH.exists():
        return False, f"{detail}; HALT STILL PRESENT after halt.py off"
    return ok, f"{detail}; HALT removed"


# ------------------------------------------------------------------ phase: slot

def default_slot(spec, now: datetime | None = None) -> str:
    tz = ZoneInfo(spec.schedule.tz)
    base = (now or datetime.now(timezone.utc)).astimezone(tz)
    prev = croniter(spec.schedule.cron, base).get_prev(datetime)
    return slot_key(prev.astimezone(tz))


def _slot_setup(ctx):
    if "spec" not in ctx.state:
        _, spec = C.load_thesis(ctx.conn, ctx.thesis)
        ctx.state["spec"] = spec
        ctx.state["slot"] = ctx.args.slot or default_slot(spec)
    return ctx.state["spec"], ctx.state["slot"]


def c_slot_orders(ctx):
    spec, slot = _slot_setup(ctx)
    ctx.client.reconcile()
    want = {client_order_id(ctx.thesis, s, slot): s for s in spec.universe}
    rows = {r["client_order_id"]: r for r in db.rows(
        ctx.conn, "SELECT * FROM orders WHERE thesis_id=? AND (reason=? OR client_order_id IN (%s))"
        % ",".join("?" * len(want)), (ctx.thesis, f"dca slot {slot}", *want))}
    missing = [want[c] for c in want if c not in rows]
    extra = [c for c in rows if c not in want]
    bad = [f"{r['symbol']}={r['status']}" for c, r in rows.items() if c in want
           and r["status"] not in ("filled", "submitted", "partially_filled")]
    found = [f"{r['symbol']}:{r['status']}" for c, r in rows.items() if c in want]
    ok = not missing and not extra and not bad
    detail = f"slot {slot}: {len(found)}/{len(want)} [{', '.join(found)}]"
    if missing:
        detail += f"; missing {','.join(missing)}"
    if extra:
        detail += f"; extra {','.join(extra)}"
    if bad:
        detail += f"; bad status {','.join(bad)}"
    return ok, detail, {"slot": slot, "orders": sorted(rows)}


def c_slot_done(ctx):
    _, slot = _slot_setup(ctx)
    key = f"slot_done:{ctx.thesis}:{slot}"
    v = db.get_setting(ctx.conn, key)
    return v == "done", f"{key} = {v}"


def c_slot_idem(ctx):
    _slot_setup(ctx)
    code, out, err = run_tool("run_thesis.py", [ctx.thesis])
    out = out or {}
    placed = out.get("placed")
    return code == 0 and placed == 0, f"exit {code} placed={placed} status={out.get('status')} " \
                                      f"{out.get('reason') or ''}".strip()


def c_slot_positions(ctx):
    spec, slot = _slot_setup(ctx)
    code, out, err = run_tool("positions.py", [])
    if code != 0 or not out:
        return False, f"positions.py exit {code}: {(out or {}).get('reason') or err.strip()[-200:]}"
    t = (out.get("theses") or {}).get(ctx.thesis) or {}
    done = db.rows(ctx.conn, "SELECT key FROM settings WHERE key LIKE ? AND value='done'",
                   (f"slot_done:{ctx.thesis}:%",))
    expected = ctx.args.expect_deployed
    if expected is None:
        expected = min(len(done) * spec.schedule.amount, spec.capital_cap)
    cost, pct = C.fnum(t.get("cost")) or 0.0, C.fnum(t.get("pct_deployed"))
    exp_pct = round(expected / spec.capital_cap * 100, 1)
    tol_pct = 2 / spec.capital_cap * 100 + 0.1
    ok = abs(cost - expected) <= 2 and pct is not None and abs(pct - exp_pct) <= tol_pct
    return ok, f"cost=${cost:.2f} (expected ${expected:.2f} +-2 from {len(done)} done slot(s)), " \
               f"pct_deployed={pct} (expected ~{exp_pct})"


def c_slot_journal(ctx):
    _, slot = _slot_setup(ctx)
    js = [j for j in db.rows(ctx.conn, "SELECT * FROM journal WHERE kind='dca' AND thesis_id=?", (ctx.thesis,))
          if (json.loads(j["data"] or "{}") or {}).get("slot") == slot]
    return len(js) == 1, f"{len(js)} dca entr{'y' if len(js) == 1 else 'ies'} for {slot}" + \
        (f": {js[0]['message'][:160]}" if js else "")


# ------------------------------------------------------------------ phase: idle

def _web(path: str) -> str:
    return f"http://127.0.0.1:{config.WEB_PORT}{path}"


def _market_today(iso: str | None) -> bool:
    if not iso:
        return False
    try:
        return C.parse_dt(iso).astimezone(config.MARKET_TZ).date() == now_et().date()
    except ValueError:
        return False


def c_proposal(ctx):
    bought_today = [r for r in db.rows(ctx.conn, "SELECT * FROM orders WHERE symbol='SPY' AND side='buy' "
                                                 "AND (filled_qty>0 OR status IN ('submitted','partially_filled'))")
                    if _market_today(r["created"])]
    if bought_today:
        return False, (f"skipped: SPY was bought today ({bought_today[0]['client_order_id']}); "
                       "selling it now would be a day trade")
    action = json.dumps({"type": "order", "side": "sell", "symbol": "SPY", "notional": 2})
    code, out, err = run_tool("propose.py", ["--thesis", "manual", "--action", action, "--reason", TAG])
    p = (out or {}).get("proposal") or {}
    pid = p.get("id")
    if code != 0 or not pid:
        return False, f"propose.py exit {code}: {(out or {}).get('reason') or err.strip()[-200:]}"
    if p.get("status") == "pending":
        r = requests.post(_web(f"/api/proposals/{pid}/approve"), params={"token": config.WEB_TOKEN}, timeout=60)
        try:
            body = r.json()
        except ValueError:
            return False, f"approve #{pid}: HTTP {r.status_code}, not JSON: {r.text[:200]}"
        p = body.get("proposal") or {}
        if r.status_code != 200:
            return False, f"approve #{pid}: HTTP {r.status_code}: {body.get('reason')}"
    result = p.get("result") or {}
    if isinstance(result, str):
        result = json.loads(result)
    coid = result.get("client_order_id") or (result.get("order") or {}).get("client_order_id")
    ctx.placed(coid)
    if p.get("status") != "executed":
        return False, f"proposal #{pid} {p.get('status')}: {result.get('reason')}"
    o = db.row(ctx.conn, "SELECT * FROM orders WHERE client_order_id=?", (coid,)) if coid else None
    if not o or o["side"] != "sell":
        return False, f"proposal #{pid} executed but no sell order row for {coid}"
    pdt = db.row(ctx.conn, "SELECT * FROM pdt_log WHERE sell_order_id=?", (coid,))
    dt_note = "DAY TRADE recorded" if pdt else "no day trade"
    return True, f"#{pid} executed: sell {coid} {o['status']}; {dt_note}", {"proposal_id": pid, "client_order_id": coid}


def c_api_state(ctx):
    r = requests.get(_web("/api/state"), params={"token": config.WEB_TOKEN}, timeout=60)
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    s = r.json()
    ok = isinstance(s.get("theses"), list) and isinstance(s.get("account"), dict)
    return ok, f"{len(s.get('theses') or [])} theses, account={'yes' if s.get('account') else 'no'}, " \
               f"broker_error={s.get('broker_error')}"


def c_news(ctx):
    code, out, err = run_tool("news.py", ["SPY", "--since", "24h", "--limit", "5"], timeout=90)
    out = out or {}
    if code != 0 or not out.get("ok"):
        return False, f"exit {code}: {out.get('reason') or err.strip()[-200:]}"
    spy = (out.get("symbols") or {}).get("SPY") or {}
    ov = spy.get("overall")
    n = len(spy.get("items") or [])
    if n == 0:
        return bool(out.get("ok")), "ok but 0 headlines in 24h (no digest to check)"
    well = isinstance(ov, dict) and ov.get("sentiment") in ("positive", "negative", "neutral") \
        and isinstance(ov.get("summary"), str) and isinstance(ov.get("flag"), bool)
    ok = bool(out.get("model")) and well
    return ok, f"model={out.get('model')} headlines={n} overall={json.dumps(ov)[:160]}" + \
        ("" if ok else f" errors={spy.get('errors')}")


def c_snapshot(ctx):
    code, out, err = run_tool("snapshot.py", [])
    out = out or {}
    if code != 0:
        return False, f"exit {code}: {out.get('reason') or err.strip()[-200:]}"
    day = out.get("date") or now_et().date().isoformat()
    bench = db.row(ctx.conn, "SELECT * FROM snapshots WHERE date=? AND symbol='SPY' AND thesis_id='$BENCH'", (day,))
    ok = (out.get("rows") or 0) > 0 and bench is not None
    return ok, f"{out.get('rows')} rows for {day}; $BENCH SPY " + (f"@ {bench['price']}" if bench else "MISSING")


def c_triggers(ctx):
    code, out, err = run_tool("check_triggers.py", [], timeout=300)
    out = out or {}
    fired = out.get("fired") or []
    return code == 0 and out.get("ok") is True, f"exit {code} fired={len(fired)} " + \
        (json.dumps(fired)[:160] if fired else "")


def c_review_pack(ctx):
    _, spec = C.load_thesis(ctx.conn, ctx.thesis)
    code, out, err = run_tool("review.py", ["--thesis", ctx.thesis, "--pack-only"])
    out = out or {}
    if code != 0 or not out.get("ok"):
        return False, f"exit {code}: {out.get('reason') or err.strip()[-200:]}"
    pack = out.get("pack")
    text = pack if isinstance(pack, str) else json.dumps(pack)
    missing = [s for s in spec.universe if s not in text]
    return not missing, f"pack {len(text)} chars" + (f"; missing {','.join(missing)}" if missing else
                                                     f"; mentions {','.join(spec.universe)}")


def c_journal(ctx):
    code, out, err = run_tool("journal.py", ["--since", "1h"])
    out = out or {}
    n = out.get("count") or 0
    return code == 0 and n > 0, f"exit {code}, {n} entries in the last hour"


# ------------------------------------------------------------------ phase: close

def c_stale(ctx):
    ctx.client.reconcile()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=STALE_MINUTES)
    stale = [r for r in db.rows(ctx.conn, "SELECT * FROM orders WHERE status IN ('submitted','partially_filled')")
             if _market_today(r["created"]) and C.parse_dt(r["created"]) < cutoff]
    if stale:
        return False, "stale: " + "; ".join(f"{r['client_order_id']} {r['status']} since {r['created']}"
                                            for r in stale), [r["client_order_id"] for r in stale]
    return True, f"no order from today open > {STALE_MINUTES} min"


def _business_days_back(d, n):
    while n > 0:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def c_pdt(ctx):
    a = ctx.client.account()
    count = a.get("daytrade_count")
    start = _business_days_back(now_et().date(), 4).isoformat()
    logged = ctx.conn.execute("SELECT COUNT(*) FROM pdt_log WHERE date>=?", (start,)).fetchone()[0]
    ok = count is not None and int(count) <= 1 and int(count) == int(logged)
    return ok, f"alpaca daytrade_count={count}, pdt_log since {start}={logged}"


def _todays_runs(ctx) -> tuple[list[str], list[dict]]:
    coids, refusals = list(ctx.orders_placed), list(ctx.expected_refusals)
    for j in db.rows(ctx.conn, "SELECT * FROM journal WHERE kind='system' AND message LIKE ?", (f"{TAG} %",)):
        if not _market_today(j["ts"]):
            continue
        d = json.loads(j["data"] or "{}") or {}
        coids += [c for c in d.get("orders_placed") or [] if c not in coids]
        refusals += d.get("expected_refusals") or []
    return coids, refusals


def c_script_orders(ctx):
    coids, refusals = _todays_runs(ctx)
    ctx.client.reconcile()
    refused_j = [json.loads(j["data"] or "{}") or {} for j in db.rows(
        ctx.conn, "SELECT * FROM journal WHERE kind='refused'") if _market_today(j["ts"])]
    problems, summary = [], []
    for c in coids:
        r = db.row(ctx.conn, "SELECT * FROM orders WHERE client_order_id=?", (c,))
        journaled = any(((d.get("request") or {}).get("client_order_id") == c) for d in refused_j)
        if r and (r["status"] == "filled" or (r["filled_qty"] or 0) > 0):
            summary.append(f"{c}:filled")
        elif journaled or (r and r["status"] == "refused"):
            summary.append(f"{c}:refused")
        else:
            problems.append(f"{c}:{r['status'] if r else 'no row'}")
    for x in refusals:
        if not any(d.get("check") == x.get("check") and (d.get("request") or {}).get("symbol") == x.get("symbol")
                   for d in refused_j):
            problems.append(f"refusal {x.get('check')} {x.get('symbol')} not journaled")
    detail = f"{len(coids)} order(s), {len(refusals)} expected refusal(s) today"
    if problems:
        detail += "; PROBLEMS: " + "; ".join(problems)
    return not problems, detail, {"orders": summary}


def c_manual_cap(ctx):
    dep = db.deployed_by_thesis(ctx.conn).get("manual", {})
    total = round(sum(max(v, 0) for v in dep.values()), 2)
    cap = float(config.load_limits()["manual_thesis_cap"])
    return total <= cap, f"manual deployed ${total:.2f} of ${cap:g} " + json.dumps(dep)


def c_no_halt(ctx):
    ex = config.HALT_PATH.exists()
    return not ex, ("HALT present: " + (_halt_content() or "").strip()[:120]) if ex else "no HALT file"


FNS = {
    "health": c_health, "clock_open": c_clock, "account_cash": c_account, "quotes": c_quotes,
    "asset_unknown": c_asset_unknown, "order_buy": c_order_buy, "order_fill": c_order_fill,
    "duplicate_id": c_duplicate, "refuse_per_order_cap": c_refuse_cap, "refuse_asset": c_refuse_asset,
    "refuse_side": c_refuse_side, "refuse_halt": c_refuse_halt,
    "slot_orders": c_slot_orders, "slot_done": c_slot_done, "slot_idempotent": c_slot_idem,
    "slot_positions": c_slot_positions, "slot_journal": c_slot_journal,
    "proposal_approve": c_proposal, "api_state": c_api_state, "news": c_news, "snapshot": c_snapshot,
    "check_triggers": c_triggers, "review_pack": c_review_pack, "journal_recent": c_journal,
    "no_stale_orders": c_stale, "pdt": c_pdt, "script_orders": c_script_orders, "manual_cap": c_manual_cap,
    "no_halt": c_no_halt,
}


# ------------------------------------------------------------------ main

def table(results: list[dict]) -> str:
    w = max([len(r["name"]) for r in results] + [5])
    lines = [f"{'check':<{w}}  res   secs  detail", "-" * (w + 60)]
    for r in results:
        lines.append(f"{r['name']:<{w}}  {'PASS' if r['ok'] else 'FAIL'}  {r['duration_s']:5.1f}  {r['detail'][:150]}")
    return "\n".join(lines)


def _main(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", required=True, choices=list(PHASES))
    ap.add_argument("--symbol", default="AAPL", help="symbol for the test buy (default AAPL)")
    ap.add_argument("--size", type=float, default=3.0, help="USD per test buy (default 3)")
    ap.add_argument("--thesis", default="semis-q4", help="thesis for the slot/review checks")
    ap.add_argument("--slot", help="slot key YYYY-MM-DDTHH:MM (default: most recent cron occurrence)")
    ap.add_argument("--expect-deployed", type=float, help="slot phase: expected thesis cost in USD "
                    "(default: amount x number of done slots)")
    ap.add_argument("--dry-run", action="store_true", help="list the checks; call nothing")
    a = ap.parse_args(argv)
    phases = PHASES[a.phase]

    if a.dry_run:
        plan = {p: [{"name": n, "what": w} for n, w in PLAN[p]] for p in phases}
        for p in phases:
            C.log(f"[{p}]")
            for n, w in PLAN[p]:
                C.log(f"  {n:<22} {w}")
        return {"ok": True, "dry_run": True, "phase": a.phase, "symbol": a.symbol.upper(), "size": a.size,
                "thesis": a.thesis, "plan": plan}
    if a.size <= 0 or a.size > 20:
        raise C.ToolFailure("--size must be between 0 and 20 USD (this is a smoke test)")

    started = now_et()
    ctx = Ctx(a)
    results: list[dict] = []
    for p in phases:
        for name, _ in PLAN[p]:
            r = check(name, FNS[name], ctx)
            r["phase"] = p
            results.append(r)
    finished = now_et()
    passed = sum(r["ok"] for r in results)
    out = {
        "ok": passed == len(results), "phase": a.phase, "symbol": ctx.symbol, "size": ctx.size,
        "thesis": a.thesis, "slot": ctx.state.get("slot"),
        "started": started.isoformat(timespec="seconds"), "finished": finished.isoformat(timespec="seconds"),
        "passed": passed, "failed": len(results) - passed, "total": len(results),
        "checks": results, "orders_placed": ctx.orders_placed, "expected_refusals": ctx.expected_refusals,
    }
    db.journal(ctx.conn, "system", f"{TAG} {a.phase}: {passed}/{len(results)} passed",
               data={k: out[k] for k in ("phase", "started", "finished", "passed", "total", "orders_placed",
                                         "expected_refusals", "slot")}
               | {"checks": [{k: r[k] for k in ("name", "ok", "detail")} for r in results]})
    C.log(table(results))
    C.log(f"\n{TAG} {a.phase}: {passed}/{len(results)} passed; orders placed: {ctx.orders_placed or 'none'}")
    return out, (0 if out["ok"] else 1)


def main(argv=None) -> int:
    return C.run(_main, argv)


if __name__ == "__main__":
    sys.exit(main())
