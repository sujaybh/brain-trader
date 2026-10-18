"""Phone-facing dashboard + chat over Tailscale. FastAPI + Jinja2, no JS framework.

Auth: every route needs `token` == config.WEB_TOKEN, as a query parameter (ntfy action URLs,
home-screen bookmark) or the `tt_token` cookie set on the first successful visit.
"""
from __future__ import annotations

import hmac
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote
from zoneinfo import ZoneInfo

from croniter import croniter
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from trader import client as tclient
from trader import config
from trader.db import connect, deployed_by_thesis, qty_by_thesis, row, rows
from trader.spec import ThesisSpec, describe
from tools.usage import summary as claude_usage_summary

from . import chat as chatmod
from . import proposals

HERE = Path(__file__).resolve().parent
COOKIE = "tt_token"
templates = Jinja2Templates(directory=str(HERE / "templates"))


# ---------------------------------------------------------------- formatting

def money(v, signed: bool = False) -> str:
    if v is None:
        return "—"
    s = f"{abs(v):,.2f}"
    sign = "-" if v < 0 else ("+" if signed and v > 0 else "")
    return f"{sign}${s}"


def pct(v) -> str:
    return "—" if v is None else f"{v:+.2f}%"


def ago(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        d = datetime.fromisoformat(ts)
    except ValueError:
        return ts
    d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    s = (datetime.now(timezone.utc) - d).total_seconds()
    if s < 0:
        s = -s
        return "in " + (f"{int(s // 60)}m" if s < 3600 else f"{int(s // 3600)}h" if s < 172800 else f"{int(s // 86400)}d")
    if s < 90:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)}m ago"
    if s < 172800:
        return f"{int(s // 3600)}h ago"
    return f"{int(s // 86400)}d ago"


def local(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        d = datetime.fromisoformat(ts)
    except ValueError:
        return ts
    d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return d.astimezone(config.MARKET_TZ).strftime("%b %d %H:%M")


templates.env.filters.update(money=money, pct=pct, ago=ago, local=local)


def parse_since(s: str | None, default: str = "7d") -> timedelta:
    try:
        return proposals.parse_duration(s or default)
    except ValueError:
        return proposals.parse_duration(default)


# ---------------------------------------------------------------- data

def _broker():
    """(account, positions, error) — never raises; the dashboard shows 'broker unreachable'."""
    try:
        bc = tclient.BrokerClient(timeout=8)
        return bc.account(), bc.positions(), None
    except tclient.BrokerError as e:
        reason = e.reason.split(": HTTPConnectionPool")[0]  # drop urllib3's wall of text
        return None, [], f"broker unreachable: {reason[:140]}"
    except Exception as e:  # anything else from the client is also "unreachable" to the phone
        return None, [], f"broker unreachable: {type(e).__name__}: {e}"


def next_slot(spec: dict, status: str) -> str | None:
    sch = spec.get("schedule") or {}
    if status != "active" or sch.get("type") != "dca":
        return None
    try:
        tz = ZoneInfo(sch.get("tz") or "America/New_York")
        nxt = croniter(sch["cron"], datetime.now(tz)).get_next(datetime)
        return nxt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return None


def positions_by_thesis(conn, live: list[dict]) -> list[dict]:
    """Alpaca positions don't carry a thesis; attribute them from our filled orders. Each group:
    {thesis_id, rows: [{symbol, qty, cost, price, market_value, pl, plpc}], totals}."""
    live_by = {p["symbol"]: p for p in live}
    dep, qty = deployed_by_thesis(conn), qty_by_thesis(conn)
    groups, attributed = [], {}
    for tid in sorted(set(dep) | set(qty)):
        rs = []
        for sym in sorted(set(dep.get(tid, {})) | set(qty.get(tid, {}))):
            q = qty.get(tid, {}).get(sym, 0.0)
            if q <= 1e-9:
                continue
            attributed[sym] = attributed.get(sym, 0.0) + q
            lp = live_by.get(sym)
            price = _f(lp.get("current_price")) if lp else None
            cost = dep.get(tid, {}).get(sym, 0.0)
            mv = round(q * price, 2) if price is not None else None
            pl = round(mv - cost, 2) if mv is not None else None
            rs.append({"symbol": sym, "qty": q, "cost": cost, "price": price, "market_value": mv, "pl": pl,
                       "plpc": round(pl / cost * 100, 2) if pl is not None and cost > 0 else None})
        if rs:
            groups.append({"thesis_id": tid, "rows": rs, **_totals(rs)})
    # anything Alpaca holds that our orders don't explain (placed outside the system, corporate actions)
    extra = []
    for sym, lp in sorted(live_by.items()):
        rest = (_f(lp.get("qty")) or 0) - attributed.get(sym, 0.0)
        if rest > 1e-6:
            price = _f(lp.get("current_price"))
            avg = _f(lp.get("avg_entry_price"))
            mv = round(rest * price, 2) if price is not None else None
            cost = round(rest * avg, 2) if avg is not None else None
            pl = round(mv - cost, 2) if mv is not None and cost is not None else None
            extra.append({"symbol": sym, "qty": round(rest, 6), "cost": cost, "price": price, "market_value": mv,
                          "pl": pl, "plpc": round(pl / cost * 100, 2) if pl is not None and cost else None})
    if extra:
        groups.append({"thesis_id": "(unattributed)", "rows": extra, **_totals(extra)})
    return groups


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _totals(rs: list[dict]) -> dict:
    cost = sum(r["cost"] or 0 for r in rs)
    mvs = [r["market_value"] for r in rs]
    mv = round(sum(mvs), 2) if all(m is not None for m in mvs) else None
    pl = round(mv - cost, 2) if mv is not None else None
    return {"cost": round(cost, 2), "market_value": mv, "pl": pl,
            "plpc": round(pl / cost * 100, 2) if pl is not None and cost > 0 else None}


def thesis_rows(conn) -> list[dict]:
    dep = deployed_by_thesis(conn)
    out = []
    for t in rows(conn, "SELECT * FROM theses ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END, id"):
        spec = json.loads(t["spec"])
        last = row(conn, "SELECT ts, kind, message FROM journal WHERE thesis_id=? AND kind!='notify' "
                         "ORDER BY id DESC LIMIT 1", (t["id"],))
        out.append({"id": t["id"], "name": t["name"], "status": t["status"], "mode": t["mode"],
                    "deployed": round(sum(max(v, 0) for v in dep.get(t["id"], {}).values()), 2),
                    "cap": spec.get("capital_cap"), "next_slot": next_slot(spec, t["status"]), "last": last})
    return out


def state(conn) -> dict:
    """Everything the dashboard shows, JSON-serializable (also served at /api/state)."""
    account, live, err = _broker()
    limits = config.load_limits()
    dep = deployed_by_thesis(conn)
    deployed = round(sum(max(v, 0) for d in dep.values() for v in d.values()), 2)
    acct = None
    if account:
        acct = {"equity": _f(account.get("equity")), "cash": _f(account.get("cash")),
                "buying_power": _f(account.get("buying_power"))}
    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "broker_error": err,
        "account": acct,
        "deployed": deployed,
        "global_cap": limits.get("global_cap"),
        "halted": config.HALT_PATH.exists(),
        "proposals": proposals.list_pending(conn),
        "theses": thesis_rows(conn),
        "positions": positions_by_thesis(conn, live),
        "journal": rows(conn, "SELECT * FROM journal WHERE kind!='notify' ORDER BY id DESC LIMIT 20"),
        "claude": claude_usage_summary(conn),
    }


# ---------------------------------------------------------------- app

def _token_ok(given: str | None) -> bool:
    return bool(given) and hmac.compare_digest(given.encode(), config.WEB_TOKEN.encode())


async def _form(request: Request) -> dict:
    if request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
        body = (await request.body()).decode(errors="replace")
        return {k: v[-1] for k, v in parse_qs(body).items()}
    return {}


def create_app() -> FastAPI:
    if not config.WEB_TOKEN:
        raise RuntimeError("WEB_TOKEN is empty; set it in config/trader.env before starting the web app")
    app = FastAPI(title="Thesis Trader", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.middleware("http")
    async def auth(request: Request, call_next):
        q = request.query_params.get("token")
        c = request.cookies.get(COOKIE)
        ok = _token_ok(q) if q is not None else _token_ok(c)
        if not ok:
            if request.url.path.startswith("/api/"):
                return JSONResponse({"ok": False, "reason": "bad or missing token"}, status_code=401)
            return HTMLResponse("<h1>401</h1><p>Open this app with <code>?token=…</code> once.</p>", status_code=401)
        resp = await call_next(request)
        if q is not None and c != q:
            resp.set_cookie(COOKIE, q, max_age=400 * 86400, httponly=True, samesite="lax")
        return resp

    def wants_redirect(request: Request) -> bool:
        return request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded")

    # -------- pages
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        conn = connect()
        try:
            st = state(conn)
        finally:
            conn.close()
        return templates.TemplateResponse(request, "dashboard.html", {"s": st, "flash": request.query_params.get("msg")})

    @app.get("/thesis/{thesis_id}", response_class=HTMLResponse)
    def thesis_page(request: Request, thesis_id: str):
        conn = connect()
        try:
            t = row(conn, "SELECT * FROM theses WHERE id=?", (thesis_id,))
            if t is None:
                return HTMLResponse("<h1>Not found</h1>", status_code=404)
            spec = json.loads(t["spec"])
            try:
                readback = describe(ThesisSpec.model_validate(spec))
            except Exception as e:
                readback = f"(spec does not validate: {e})"
            _, live, err = _broker()
            groups = [g for g in positions_by_thesis(conn, live) if g["thesis_id"] == thesis_id]
            ctx = {
                "t": t, "spec": spec, "spec_json": json.dumps(spec, indent=2), "readback": readback,
                "broker_error": err, "group": groups[0] if groups else None,
                "deployed": round(sum(max(v, 0) for v in deployed_by_thesis(conn).get(thesis_id, {}).values()), 2),
                "next_slot": next_slot(spec, t["status"]),
                "proposals": [p for p in proposals.list_pending(conn) if p["thesis_id"] == thesis_id],
                "journal": rows(conn, "SELECT * FROM journal WHERE thesis_id=? AND kind!='notify' ORDER BY id DESC LIMIT 50",
                                (thesis_id,)),
                "flash": request.query_params.get("msg"),
            }
        finally:
            conn.close()
        return templates.TemplateResponse(request, "thesis.html", ctx)

    @app.get("/journal", response_class=HTMLResponse)
    def journal_page(request: Request, since: str = "7d", kind: str = ""):
        cutoff = (datetime.now(timezone.utc) - parse_since(since)).isoformat(timespec="seconds")
        conn = connect()
        try:
            sql, params = "SELECT * FROM journal WHERE ts>=?", [cutoff]
            if kind:
                sql += " AND kind=?"
                params.append(kind)
            else:
                sql += " AND kind!='notify'"
            js = rows(conn, sql + " ORDER BY id DESC LIMIT 500", tuple(params))
            kinds = [r["kind"] for r in rows(conn, "SELECT DISTINCT kind FROM journal ORDER BY kind")]
        finally:
            conn.close()
        return templates.TemplateResponse(request, "journal.html",
                                          {"journal": js, "since": since, "kind": kind, "kinds": kinds})

    @app.get("/chat", response_class=HTMLResponse)
    def chat_page(request: Request):
        conn = connect()
        try:
            msgs = chatmod.history(conn, 30)
        finally:
            conn.close()
        return templates.TemplateResponse(request, "chat.html", {"messages": msgs})

    @app.post("/chat")
    async def chat_form(request: Request):
        form = await _form(request)
        msg = form.get("message", "")
        await run_in_threadpool(_chat, msg)
        return RedirectResponse("/chat#bottom", status_code=303)

    # -------- API
    @app.get("/api/state")
    def api_state():
        conn = connect()
        try:
            return JSONResponse(json.loads(json.dumps(state(conn), default=str)))
        finally:
            conn.close()

    def _decide(request: Request, pid: int, fn, back: str):
        conn = connect()
        try:
            try:
                p = fn(conn, pid, by="web")
                body, code = {"ok": p["status"] in ("executed", "rejected"), "proposal": p}, 200
                if p["status"] == "failed":
                    body["reason"] = (p.get("result") or {}).get("reason", "execution failed")
                msg = f"#{pid} {p['status']}" + (f": {body['reason']}" if body.get("reason") else "")
            except proposals.ProposalError as e:
                body, code = {"ok": False, "reason": e.reason, "proposal": e.proposal}, 409
                msg = e.reason
        finally:
            conn.close()
        if wants_redirect(request):
            return RedirectResponse(f"{back}?msg={quote(msg)}", status_code=303)
        return JSONResponse(json.loads(json.dumps(body, default=str)), status_code=code)

    @app.post("/api/proposals/{pid}/approve")
    async def approve(request: Request, pid: int):
        form = await _form(request)
        return await run_in_threadpool(_decide, request, pid, proposals.approve, _safe_back(form.get("back")))

    @app.post("/api/proposals/{pid}/reject")
    async def reject(request: Request, pid: int):
        form = await _form(request)
        return await run_in_threadpool(_decide, request, pid, proposals.reject, _safe_back(form.get("back")))

    @app.post("/api/theses/{thesis_id}/{verb}")
    async def thesis_toggle(request: Request, thesis_id: str, verb: str):
        """Pause/resume from the thesis page. The user tapping the button *is* the approval, so
        this creates a proposal and approves it in one go — same audit trail as a phone tap."""
        if verb not in ("pause", "resume"):
            return JSONResponse({"ok": False, "reason": "verb must be pause or resume"}, status_code=404)

        def go():
            conn = connect()
            try:
                p = proposals.create(conn, thesis_id, {"type": verb}, f"{verb} from web dashboard",
                                     timedelta(minutes=10), approve_now="web")
                return {"ok": p["status"] == "executed", "proposal": p}, 200
            except proposals.ProposalError as e:
                return {"ok": False, "reason": e.reason}, 409
            finally:
                conn.close()

        body, code = await run_in_threadpool(go)
        if wants_redirect(request):
            msg = body.get("reason") or (body["proposal"].get("result") or {}).get("message", "")
            return RedirectResponse(f"/thesis/{thesis_id}?msg={quote(msg)}", status_code=303)
        return JSONResponse(json.loads(json.dumps(body, default=str)), status_code=code)

    @app.post("/api/chat")
    async def api_chat(request: Request):
        try:
            data = await request.json()
        except ValueError:
            data = None
        if not isinstance(data, dict) or not str(data.get("message") or "").strip():
            return JSONResponse({"ok": False, "reason": 'body must be {"message": "..."}'}, status_code=422)
        res = await run_in_threadpool(_chat, str(data["message"]))
        return JSONResponse(res, status_code=200 if res.get("ok") else 502)

    return app


def _chat(message: str) -> dict:
    conn = connect()
    try:
        return chatmod.send(conn, message)
    finally:
        conn.close()


def _safe_back(v: str | None) -> str:
    """Only allow local paths for the post-form redirect."""
    return v if v and re.fullmatch(r"/[A-Za-z0-9/_\-]*", v) else "/"
