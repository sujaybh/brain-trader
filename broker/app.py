"""FastAPI app for the broker daemon. Implements docs/broker-api.md.

Everything that matters for safety happens here or in limits.py, above the backend:
idempotency, the limit chain, sizing, journaling, reconciliation, pdt_log."""
from __future__ import annotations

import hmac
import logging
import sqlite3
import threading
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from trader import config, db, notify
from trader.schedule import manual_order_id
from trader.spec import ThesisSpec, manual_spec

from .backends import Backend, BackendError, make_backend
from .limits import LazyContext, OrderReq, run_chain, sizing

log = logging.getLogger("broker")

NON_TERMINAL = ("submitted", "partially_filled")


# ------------------------------------------------------------------------------ models

class OrderIn(BaseModel):
    thesis_id: str = Field(default=config.MANUAL_THESIS_ID, min_length=1, max_length=40)
    side: str = Field(min_length=1, max_length=10)
    symbol: str = Field(min_length=1, max_length=15)
    notional: Optional[float] = Field(default=None, gt=0)
    qty: Optional[float] = Field(default=None, gt=0)
    client_order_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    reason: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _one_size(self):
        if (self.notional is None) == (self.qty is None):
            raise ValueError("exactly one of notional or qty is required")
        return self

    def normalized(self) -> OrderReq:
        return OrderReq(thesis_id=self.thesis_id.strip(), side=self.side.strip().lower(),
                        symbol=self.symbol.strip().upper(), notional=self.notional, qty=self.qty,
                        client_order_id=self.client_order_id, reason=self.reason)


# ------------------------------------------------------------------------------ helpers

def _now_market() -> datetime:
    return datetime.now(config.MARKET_TZ)


def _market_date(iso: str):
    return datetime.fromisoformat(iso).astimezone(config.MARKET_TZ).date()


def _business_days_back(d, n: int):
    while n > 0:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def order_out(r: dict) -> dict:
    return {"client_order_id": r["client_order_id"], "broker_order_id": r["broker_order_id"],
            "thesis_id": r["thesis_id"], "symbol": r["symbol"], "side": r["side"], "type": "limit",
            "limit_price": r["limit_price"], "qty": r["qty"], "notional": r["notional"],
            "status": r["status"], "filled_qty": r["filled_qty"], "filled_avg_price": r["filled_avg_price"],
            "filled_notional": r["filled_notional"], "reason": r["reason"],
            "submitted_at": r["created"], "updated": r["updated"]}


class Broker:
    """Per-app state: the backend, paths, and the lock that serializes order placement so
    two concurrent requests cannot both squeeze under the same cap."""

    def __init__(self, backend: Backend, db_path: Path, halt_path: Path, token: str,
                 limits_override: Optional[dict] = None):
        self.backend = backend
        self.db_path = db_path
        self.halt_path = halt_path
        self.token = token
        self.limits_override = limits_override or {}
        self.order_lock = threading.Lock()

    def connect(self) -> sqlite3.Connection:
        return db.connect(self.db_path)

    def limits(self) -> dict:
        return {**config.load_limits(), **self.limits_override}

    def halted(self) -> bool:
        return self.halt_path.exists()

    # -------------------------------------------------------------- fills & reconcile

    def apply_update(self, conn: sqlite3.Connection, r: dict, bo: dict) -> bool:
        """Write the backend's view of an order into our row; journal + push new fills."""
        new_fq = float(bo.get("filled_qty") or 0)
        avg = bo.get("filled_avg_price")
        status = bo["status"]
        old_fq = float(r["filled_qty"] or 0)
        if status == r["status"] and abs(new_fq - old_fq) < 1e-12 and avg == r["filled_avg_price"]:
            return False
        filled_notional = round(new_fq * float(avg), 4) if avg else 0.0
        conn.execute("UPDATE orders SET status=?, filled_qty=?, filled_avg_price=?, filled_notional=?, "
                     "broker_order_id=COALESCE(broker_order_id, ?), updated=? WHERE id=?",
                     (status, new_fq, avg, filled_notional, bo.get("broker_order_id"), db.now_iso(), r["id"]))
        updated = db.row(conn, "SELECT * FROM orders WHERE id=?", (r["id"],))
        if new_fq > old_fq + 1e-12:
            self._on_fill(conn, updated, first_fill=old_fq == 0)
        elif status != r["status"]:
            db.journal(conn, "order", f"{r['client_order_id']} is now {status}", thesis_id=r["thesis_id"],
                       symbol=r["symbol"], data=order_out(updated))
        return True

    def _on_fill(self, conn: sqlite3.Connection, r: dict, first_fill: bool) -> None:
        verb = "Bought" if r["side"] == "buy" else "Sold"
        partial = "" if r["status"] == "filled" else f" ({r['status'].replace('_', ' ')})"
        msg = (f"{verb} {r['filled_qty']:g} {r['symbol']} @ ${r['filled_avg_price']:.2f} = "
               f"${r['filled_notional']:.2f} for {r['thesis_id']}{partial}")
        db.journal(conn, "fill", msg, thesis_id=r["thesis_id"], symbol=r["symbol"], data=order_out(r))
        log.info(msg)
        notify.push(self._fill_push_text(conn, r, verb), title=f"{verb} {r['symbol']}", tags=["moneybag"], conn=conn)
        if first_fill:
            self._record_pdt(conn, r)

    def _fill_push_text(self, conn: sqlite3.Connection, r: dict, verb: str) -> str:
        """Phone-sized: 'Bought $12.49 NVDA @ 183.75 (semis-q4). Thesis at $50/$300 deployed.'"""
        text = f"{verb} ${r['filled_notional']:.2f} {r['symbol']} @ {r['filled_avg_price']:.2f} ({r['thesis_id']})"
        if r["status"] != "filled":
            text += f" [{r['status'].replace('_', ' ')}]"
        try:
            t = self._thesis(conn, r["thesis_id"], self.limits())
            deployed = sum(max(v, 0.0) for v in db.deployed_by_thesis(conn).get(r["thesis_id"], {}).values())
            if t:
                text += f". Thesis at ${deployed:,.0f}/${t['capital_cap']:,.0f} deployed."
        except Exception as e:  # never let a push detail break a fill
            log.warning("fill push context failed: %s", e)
        return text

    def _record_pdt(self, conn: sqlite3.Connection, r: dict) -> None:
        """A first fill with an opposite-side fill in the same symbol on the same market date
        (any thesis: PDT is per account) completes a round trip: one pdt_log row."""
        today = _now_market().date()
        if _market_date(r["created"]) != today:
            return
        if db.row(conn, "SELECT id FROM pdt_log WHERE buy_order_id=? OR sell_order_id=?",
                  (r["client_order_id"], r["client_order_id"])):
            return
        opposite = "sell" if r["side"] == "buy" else "buy"
        since = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds")
        others = [o for o in db.rows(conn, "SELECT * FROM orders WHERE symbol=? AND side=? AND filled_qty>0 "
                                           "AND id<>? AND created>=? ORDER BY id DESC",
                                     (r["symbol"], opposite, r["id"], since))
                  if _market_date(o["created"]) == today]
        if not others:
            return
        other = others[0]["client_order_id"]
        buy_id, sell_id = (r["client_order_id"], other) if r["side"] == "buy" else (other, r["client_order_id"])
        conn.execute("INSERT INTO pdt_log(date, symbol, buy_order_id, sell_order_id, created) VALUES (?,?,?,?,?)",
                     (today.isoformat(), r["symbol"], buy_id, sell_id, db.now_iso()))
        db.journal(conn, "system", f"Day trade recorded in {r['symbol']}", symbol=r["symbol"],
                   data={"buy_order_id": buy_id, "sell_order_id": sell_id})

    def reconcile(self, conn: sqlite3.Connection, only: Optional[str] = None) -> int:
        """Refresh every non-terminal order (or just `only`) from the backend."""
        marks = ",".join("?" * len(NON_TERMINAL))
        sql = f"SELECT * FROM orders WHERE status IN ({marks}) AND broker_order_id IS NOT NULL"
        params: tuple = NON_TERMINAL
        if only:
            sql += " AND client_order_id=?"
            params += (only,)
        n = 0
        for r in db.rows(conn, sql, params):
            if self.apply_update(conn, r, self.backend.get_order(r["broker_order_id"])):
                n += 1
        return n

    # -------------------------------------------------------------- limit-chain context

    def build_ctx(self, conn: sqlite3.Connection, req: OrderReq, limits: dict) -> LazyContext:
        b = self.backend

        def reconciled(ctx):
            return self.reconcile(conn)

        def open_orders(ctx):
            ctx["reconciled"]
            marks = ",".join("?" * len(NON_TERMINAL))
            return db.rows(conn, f"SELECT * FROM orders WHERE status IN ({marks})", NON_TERMINAL)

        def remaining(o: dict) -> float:
            return max(float(o["qty"] or 0) - float(o["filled_qty"] or 0), 0.0)

        def deployed(ctx):
            d = db.deployed_by_thesis(conn)
            for o in ctx["open_orders"]:
                if o["side"] == "buy":
                    per = d.setdefault(o["thesis_id"], {})
                    per[o["symbol"]] = per.get(o["symbol"], 0.0) + remaining(o) * float(o["limit_price"] or 0)
            return d

        def held_qty(ctx):
            q = db.qty_by_thesis(conn)
            for o in ctx["open_orders"]:
                if o["side"] == "sell":
                    per = q.setdefault(o["thesis_id"], {})
                    per[o["symbol"]] = per.get(o["symbol"], 0.0) - remaining(o)
            return q

        def pending_buys(ctx):
            return sum(remaining(o) * float(o["limit_price"] or 0) for o in ctx["open_orders"] if o["side"] == "buy")

        def thesis(ctx):
            return self._thesis(conn, req.thesis_id, limits)

        def pdt(ctx):
            ctx["reconciled"]
            today = _now_market().date()
            start = _business_days_back(today, 4).isoformat()
            logged = conn.execute("SELECT COUNT(*) FROM pdt_log WHERE date>=?", (start,)).fetchone()[0]
            since = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds")
            marks = ",".join("?" * len(NON_TERMINAL))
            recent = db.rows(conn, f"SELECT side, symbol, created FROM orders WHERE created>=? AND "
                                   f"(filled_qty>0 OR status IN ({marks}))", (since, *NON_TERMINAL))
            today_rows = [o for o in recent if _market_date(o["created"]) == today]
            return {"logged": int(logged),
                    "bought_today": {o["symbol"] for o in today_rows if o["side"] == "buy"},
                    "sold_today": {o["symbol"] for o in today_rows if o["side"] == "sell"}}

        return LazyContext({
            "halted": lambda ctx: self.halted(),
            "clock": lambda ctx: b.clock(),
            "assets": lambda ctx: b.assets([req.symbol]),
            "quotes": lambda ctx: b.quotes([req.symbol]),
            "account": lambda ctx: b.account(),
            "positions": lambda ctx: b.positions(),
            "reconciled": reconciled,
            "open_orders": open_orders,
            "deployed": deployed,
            "held_qty": held_qty,
            "pending_buys": pending_buys,
            "thesis": thesis,
            "pdt": pdt,
        })

    @staticmethod
    def _thesis(conn: sqlite3.Connection, thesis_id: str, limits: dict) -> Optional[dict]:
        if thesis_id == config.MANUAL_THESIS_ID:
            spec = manual_spec(float(limits["manual_thesis_cap"]))
        else:
            r = db.row(conn, "SELECT spec FROM theses WHERE id=?", (thesis_id,))
            if r is None:
                return None
            try:
                spec = ThesisSpec.model_validate_json(r["spec"])
            except ValueError:
                log.error("thesis %s has an invalid spec; treating as unknown", thesis_id)
                return None
        return {"id": spec.id, "capital_cap": spec.capital_cap, "max_position_usd": spec.max_position_usd(),
                "status": spec.status}

    # -------------------------------------------------------------- placing

    def refuse(self, conn: sqlite3.Connection, req: OrderReq, check: str, reason: str) -> JSONResponse:
        size = f"${req.notional:g}" if req.notional is not None else f"{req.qty:g} sh"
        msg = f"Refused {req.side} {req.symbol} {size} for {req.thesis_id}: {reason}"
        db.journal(conn, "refused", msg, thesis_id=req.thesis_id, symbol=req.symbol,
                   data={"check": check, "reason": reason, "request": asdict(req)})
        log.warning(msg)
        notify.push(msg, title=f"Order refused ({check})", tags=["no_entry"], priority=4, conn=conn)
        return JSONResponse(status_code=403, content={"refused": True, "reason": reason, "check": check})

    def place(self, body: OrderIn) -> JSONResponse:
        req = body.normalized()
        limits = self.limits()
        with self.order_lock:
            conn = self.connect()
            try:
                if req.client_order_id is None:
                    req, existing = self._derive_id(conn, req)
                else:
                    existing = db.row(conn, "SELECT * FROM orders WHERE client_order_id=?", (req.client_order_id,))
                if existing:
                    return JSONResponse(status_code=200, content={**order_out(existing), "duplicate": True})
                ctx = self.build_ctx(conn, req, limits)
                refusal = run_chain(req, ctx, limits)
                if refusal:
                    return self.refuse(conn, req, *refusal)
                return self._submit(conn, req, sizing(req, ctx))
            finally:
                conn.close()

    @staticmethod
    def _derive_id(conn: sqlite3.Connection, req: OrderReq) -> tuple[OrderReq, Optional[dict]]:
        """Manual orders get `manual_order_id(thesis, symbol, side, now)` (1-second resolution).
        An identical request in the same second is a double-submit and returns the existing row
        as a duplicate; a different size in the same second gets the next free second."""
        now = _now_market().replace(microsecond=0)
        for bump in range(60):
            coid = manual_order_id(req.thesis_id, req.symbol, req.side, now + timedelta(seconds=bump))
            existing = db.row(conn, "SELECT * FROM orders WHERE client_order_id=?", (coid,))
            if existing is None:
                return replace(req, client_order_id=coid), None
            same_size = (req.notional is not None and existing["notional"] == req.notional) or \
                        (req.qty is not None and existing["qty"] == req.qty)
            if bump == 0 and same_size:
                return replace(req, client_order_id=coid), existing
        raise HTTPException(429, "too many manual orders for this symbol in the last minute")

    def _submit(self, conn: sqlite3.Connection, req: OrderReq, s) -> JSONResponse:
        now = db.now_iso()
        cur = conn.execute(
            "INSERT INTO orders(client_order_id, thesis_id, symbol, side, notional, qty, limit_price, status, "
            "reason, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (req.client_order_id, req.thesis_id, req.symbol, req.side, s.notional, s.qty, s.limit_price,
             "submitted", req.reason, now, now))
        row_id = int(cur.lastrowid)
        try:
            bo = self.backend.submit_limit_order(symbol=req.symbol, side=req.side, qty=s.qty,
                                                 limit_price=s.limit_price, client_order_id=req.client_order_id)
        except BackendError as e:
            # Nothing was placed (or the broker will reject a retry as a duplicate id), so free
            # the idempotency key for a retry.
            conn.execute("DELETE FROM orders WHERE id=?", (row_id,))
            db.journal(conn, "error", f"Order {req.client_order_id} failed upstream: {e}",
                       thesis_id=req.thesis_id, symbol=req.symbol, data={"request": asdict(req)})
            raise
        conn.execute("UPDATE orders SET broker_order_id=?, updated=? WHERE id=?",
                     (bo["broker_order_id"], db.now_iso(), row_id))
        r = db.row(conn, "SELECT * FROM orders WHERE id=?", (row_id,))
        msg = (f"Placed {req.side} {s.qty:g} {req.symbol} limit ${s.limit_price:.2f} (~${s.notional:.2f}) "
               f"for {req.thesis_id}")
        db.journal(conn, "order", msg, thesis_id=req.thesis_id, symbol=req.symbol,
                   data={**order_out(r), "request_reason": req.reason})
        log.info(msg)
        self.apply_update(conn, r, bo)
        r = db.row(conn, "SELECT * FROM orders WHERE id=?", (row_id,))
        out = order_out(r)
        if req.notional is not None:
            out["notional"] = req.notional
        return JSONResponse(status_code=201, content=out)

    def cancel(self, client_order_id: str) -> dict:
        conn = self.connect()
        try:
            with self.order_lock:
                return self._cancel(conn, client_order_id)
        finally:
            conn.close()

    def _cancel(self, conn: sqlite3.Connection, client_order_id: str) -> dict:
        r = db.row(conn, "SELECT * FROM orders WHERE client_order_id=?", (client_order_id,))
        if r is None:
            raise HTTPException(404, f"no order {client_order_id}")
        if r["status"] not in NON_TERMINAL:
            raise HTTPException(409, f"order {client_order_id} is already {r['status']}")
        self.backend.cancel_order(r["broker_order_id"])
        self.apply_update(conn, r, self.backend.get_order(r["broker_order_id"]))
        r = db.row(conn, "SELECT * FROM orders WHERE id=?", (r["id"],))
        status = r["status"] if r["status"] not in NON_TERMINAL else "pending_cancel"
        log.info("cancel %s -> %s", client_order_id, status)
        return {"status": status, "client_order_id": client_order_id, "order": order_out(r)}


# ------------------------------------------------------------------------------ app

def create_app(backend: Optional[Backend] = None, *, db_path: Optional[Path] = None,
               halt_path: Optional[Path] = None, token: Optional[str] = None,
               limits: Optional[dict] = None) -> FastAPI:
    broker = Broker(backend or make_backend(), Path(db_path or config.DB_PATH),
                    Path(halt_path or config.HALT_PATH),
                    config.BROKER_TOKEN if token is None else token, limits)
    app = FastAPI(title="Thesis Trader broker", version="1")
    app.state.broker = broker

    def require_token(x_broker_token: Optional[str] = Header(default=None)) -> None:
        if not broker.token or not x_broker_token or not hmac.compare_digest(x_broker_token, broker.token):
            raise HTTPException(401, "bad or missing X-Broker-Token")

    @app.exception_handler(HTTPException)
    async def _http_exc(request: Request, exc: HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"reason": exc.detail})

    @app.exception_handler(BackendError)
    async def _backend_exc(request: Request, exc: BackendError):
        log.error("upstream error: %s", exc)
        return JSONResponse(status_code=502, content={"reason": str(exc)})

    @app.exception_handler(RequestValidationError)
    async def _validation_exc(request: Request, exc: RequestValidationError):
        errors = exc.errors()
        first = errors[0] if errors else {}
        where = ".".join(str(p) for p in first.get("loc", []) if p != "body")
        reason = f"{where}: {first.get('msg', 'invalid request')}" if where else first.get("msg", "invalid request")
        return JSONResponse(status_code=422, content={"reason": reason,
                                                      "detail": [{k: e.get(k) for k in ("loc", "msg", "type")}
                                                                 for e in errors]})

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "backend": broker.backend.name, "paper": bool(broker.backend.paper),
                "halted": broker.halted()}

    api = APIRouter(dependencies=[Depends(require_token)])

    def symbols_param(symbols: str) -> list[str]:
        out = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        if not out:
            raise HTTPException(422, "symbols is empty")
        return out

    @api.get("/clock")
    def clock() -> dict:
        return broker.backend.clock()

    @api.get("/account")
    def account() -> dict:
        return broker.backend.account()

    @api.get("/positions")
    def positions() -> list[dict]:
        return broker.backend.positions()

    @api.get("/quote")
    def quote(symbols: str = Query(...)) -> dict:
        return broker.backend.quotes(symbols_param(symbols))

    @api.get("/assets")
    def assets(symbols: str = Query(...)) -> dict:
        return broker.backend.assets(symbols_param(symbols))

    @api.post("/orders")
    def place_order(body: OrderIn) -> JSONResponse:
        return broker.place(body)

    @api.get("/orders")
    def list_orders(status: Literal["open", "closed", "all"] = "all",
                    limit: int = Query(100, ge=1, le=1000)) -> list[dict]:
        conn = broker.connect()
        try:
            with broker.order_lock:
                broker.reconcile(conn)
            marks = ",".join("?" * len(NON_TERMINAL))
            where = {"open": f"WHERE status IN ({marks})", "closed": f"WHERE status NOT IN ({marks})",
                     "all": ""}[status]
            params = NON_TERMINAL if status != "all" else ()
            rs = db.rows(conn, f"SELECT * FROM orders {where} ORDER BY id DESC LIMIT ?", (*params, limit))
            return [order_out(r) for r in rs]
        finally:
            conn.close()

    @api.get("/orders/{client_order_id}")
    def get_order(client_order_id: str) -> dict:
        conn = broker.connect()
        try:
            with broker.order_lock:
                broker.reconcile(conn, only=client_order_id)
            r = db.row(conn, "SELECT * FROM orders WHERE client_order_id=?", (client_order_id,))
            if r is None:
                raise HTTPException(404, f"no order {client_order_id}")
            return order_out(r)
        finally:
            conn.close()

    @api.delete("/orders/{client_order_id}")
    def cancel_order(client_order_id: str) -> dict:
        return broker.cancel(client_order_id)

    @api.post("/reconcile")
    def reconcile() -> dict:
        conn = broker.connect()
        try:
            with broker.order_lock:
                n = broker.reconcile(conn)
            return {"updated": n, "positions": broker.backend.positions()}
        finally:
            conn.close()

    @api.get("/limits")
    def limits_view() -> dict:
        return broker.limits()

    app.include_router(api)
    return app
