"""Backends: the thin layer that talks to a broker. Everything else (limits, sizing,
idempotency, journaling, reconciliation) lives above this so the fake and Alpaca behave
identically from the daemon's point of view.

All methods return plain dicts in the shapes of docs/broker-api.md. Orders come back as
    {"broker_order_id", "client_order_id", "status", "filled_qty", "filled_avg_price", "submitted_at"}
with `status` already mapped to our vocabulary:
    submitted | partially_filled | filled | canceled | expired | rejected
"""
from __future__ import annotations

import os
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional, Protocol

from trader import config


class BackendError(Exception):
    """An upstream broker error. The daemon turns it into HTTP 502 with the message."""


class OrderNotFound(BackendError):
    pass


class Backend(Protocol):
    name: str
    paper: bool

    def clock(self) -> dict: ...
    def account(self) -> dict: ...
    def positions(self) -> list[dict]: ...
    def quotes(self, symbols: list[str]) -> dict[str, Optional[dict]]: ...
    def assets(self, symbols: list[str]) -> dict[str, Optional[dict]]: ...
    def submit_limit_order(self, *, symbol: str, side: str, qty: float, limit_price: float,
                           client_order_id: str) -> dict: ...
    def get_order(self, broker_order_id: str) -> dict: ...
    def cancel_order(self, broker_order_id: str) -> None: ...


# Alpaca order status -> ours. Anything not terminal maps to `submitted` so reconcile keeps
# polling it (pending_cancel included: it can still fill).
_ALPACA_STATUS = {
    "filled": "filled",
    "partially_filled": "partially_filled",
    "canceled": "canceled",
    "replaced": "canceled",
    "expired": "expired",
    "rejected": "rejected",
    "suspended": "rejected",
}


def map_alpaca_status(status: str, filled_qty: float) -> str:
    mapped = _ALPACA_STATUS.get(status, "submitted")
    if mapped == "submitted" and filled_qty > 0:
        return "partially_filled"
    return mapped


def _iso(v: Any) -> Optional[str]:
    if v is None:
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


def _f(v: Any) -> Optional[float]:
    return None if v is None else float(v)


def _val(v: Any) -> Any:
    """Enum -> its value; anything else unchanged."""
    return getattr(v, "value", v)


# ---------------------------------------------------------------------------- Alpaca

class AlpacaBackend:
    """alpaca-py wrapper. Deliberately thin: one call per method, model -> dict."""

    name = "alpaca"

    def __init__(self, key: str, secret: str, paper: bool = True):
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        self.paper = paper
        self._trading = TradingClient(key, secret, paper=paper)
        self._data = StockHistoricalDataClient(key, secret)

    @classmethod
    def from_env(cls) -> "AlpacaBackend":
        config.load_env_file(config.CONFIG_DIR / "alpaca.env")
        key, secret = os.environ.get("APCA_API_KEY_ID", ""), os.environ.get("APCA_API_SECRET_KEY", "")
        if not key or not secret:
            raise RuntimeError("APCA_API_KEY_ID / APCA_API_SECRET_KEY missing; put them in config/alpaca.env")
        paper = os.environ.get("APCA_PAPER", "true").strip().lower() not in ("false", "0", "no")
        return cls(key, secret, paper=paper)

    def _call(self, fn, *args, **kw):
        from alpaca.common.exceptions import APIError
        import requests

        try:
            return fn(*args, **kw)
        except APIError as e:
            msg = _api_message(e)
            if e.status_code == 404:
                raise OrderNotFound(msg) from e
            raise BackendError(f"Alpaca: {msg}") from e
        except requests.RequestException as e:
            raise BackendError(f"Alpaca unreachable: {e}") from e

    def clock(self) -> dict:
        c = self._call(self._trading.get_clock)
        return {"is_open": bool(c.is_open), "next_open": _iso(c.next_open),
                "next_close": _iso(c.next_close), "timestamp": _iso(c.timestamp)}

    def account(self) -> dict:
        a = self._call(self._trading.get_account)
        return {"cash": _f(a.cash), "buying_power": _f(a.buying_power), "equity": _f(a.equity),
                "portfolio_value": _f(a.portfolio_value), "daytrade_count": int(a.daytrade_count or 0),
                "pattern_day_trader": bool(a.pattern_day_trader), "currency": a.currency or "USD"}

    def positions(self) -> list[dict]:
        out = []
        for p in self._call(self._trading.get_all_positions):
            out.append({"symbol": p.symbol, "qty": _f(p.qty), "avg_entry_price": _f(p.avg_entry_price),
                        "current_price": _f(p.current_price), "market_value": _f(p.market_value),
                        "cost_basis": _f(p.cost_basis), "unrealized_pl": _f(p.unrealized_pl),
                        "unrealized_plpc": _f(p.unrealized_plpc)})
        return out

    def quotes(self, symbols: list[str]) -> dict[str, Optional[dict]]:
        from alpaca.data.requests import StockLatestQuoteRequest, StockLatestTradeRequest

        q = self._call(self._data.get_stock_latest_quote, StockLatestQuoteRequest(symbol_or_symbols=symbols))
        t = self._call(self._data.get_stock_latest_trade, StockLatestTradeRequest(symbol_or_symbols=symbols))
        out: dict[str, Optional[dict]] = {}
        for s in symbols:
            quote = q.get(s)
            if quote is None:
                out[s] = None
                continue
            trade = t.get(s)
            bid, ask = _f(quote.bid_price), _f(quote.ask_price)
            last = _f(trade.price) if trade is not None else (bid + ask) / 2 if bid and ask else None
            out[s] = {"bid": bid, "ask": ask, "last": last, "ts": _iso(quote.timestamp)}
        return out

    def assets(self, symbols: list[str]) -> dict[str, Optional[dict]]:
        out: dict[str, Optional[dict]] = {}
        for s in symbols:
            try:
                a = self._call(self._trading.get_asset, s)
            except OrderNotFound:  # 404: unknown symbol
                out[s] = None
                continue
            out[s] = {"tradable": bool(a.tradable), "fractionable": bool(a.fractionable), "name": a.name,
                      "exchange": _val(a.exchange), "asset_class": _val(a.asset_class)}
        return out

    def submit_limit_order(self, *, symbol: str, side: str, qty: float, limit_price: float,
                           client_order_id: str) -> dict:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest

        req = LimitOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                                time_in_force=TimeInForce.DAY, limit_price=limit_price,
                                client_order_id=client_order_id)
        return self._order_dict(self._call(self._trading.submit_order, req))

    def get_order(self, broker_order_id: str) -> dict:
        return self._order_dict(self._call(self._trading.get_order_by_id, broker_order_id))

    def cancel_order(self, broker_order_id: str) -> None:
        self._call(self._trading.cancel_order_by_id, broker_order_id)

    @staticmethod
    def _order_dict(o: Any) -> dict:
        filled = float(o.filled_qty or 0)
        return {"broker_order_id": str(o.id), "client_order_id": o.client_order_id,
                "status": map_alpaca_status(str(_val(o.status)), filled), "filled_qty": filled,
                "filled_avg_price": _f(o.filled_avg_price), "submitted_at": _iso(o.submitted_at)}


def _api_message(e: Exception) -> str:
    try:
        return e.message  # type: ignore[attr-defined]
    except Exception:
        return str(e)


# ------------------------------------------------------------------------------ Fake

FAKE_QUOTES: dict[str, tuple[float, float, float]] = {  # bid, ask, last
    "SPY": (580.00, 580.06, 580.03),
    "NVDA": (183.10, 183.20, 183.15),
    "AMD": (160.40, 160.50, 160.45),
    "AVGO": (340.20, 340.40, 340.30),
    "TSM": (200.00, 200.12, 200.05),
    "AAPL": (230.00, 230.08, 230.04),
    "MSFT": (420.10, 420.30, 420.20),
    "BTCUSD": (60000.0, 60010.0, 60005.0),
}
FAKE_NAMES = {"SPY": "SPDR S&P 500 ETF", "NVDA": "NVIDIA Corp", "AMD": "Advanced Micro Devices",
              "AVGO": "Broadcom Inc", "TSM": "Taiwan Semiconductor ADR", "AAPL": "Apple Inc",
              "MSFT": "Microsoft Corp", "BTCUSD": "Bitcoin"}


def _env_bool(key: str, default: bool) -> bool:
    v = os.environ.get(key)
    return default if v is None or v == "" else v.strip().lower() not in ("false", "0", "no")


def _business_days_back(d: date, n: int) -> date:
    while n > 0:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


class FakeBackend:
    """In-memory simulator. Orders fill immediately at the limit price (fully, or by the
    fraction in FAKE_PARTIAL_FILL). Test hooks: `market_open`, `partial_fill`, `auto_fill`
    attributes and `fill_remaining(broker_order_id)`."""

    name = "fake"
    paper = True

    def __init__(self, cash: Optional[float] = None, market_open: Optional[bool] = None,
                 partial_fill: Optional[float] = None, auto_fill: Optional[bool] = None):
        self.cash = float(cash if cash is not None else os.environ.get("FAKE_CASH") or 10000)
        self.market_open = market_open if market_open is not None else _env_bool("FAKE_MARKET_OPEN", True)
        pf = partial_fill if partial_fill is not None else os.environ.get("FAKE_PARTIAL_FILL")
        self.partial_fill: Optional[float] = float(pf) if pf not in (None, "") else None
        self.auto_fill = auto_fill if auto_fill is not None else _env_bool("FAKE_AUTO_FILL", True)
        self.quote_table = dict(FAKE_QUOTES)
        self.orders: dict[str, dict] = {}
        self._positions: dict[str, dict] = {}   # symbol -> {"qty", "cost"}
        self._fills: list[tuple[date, str, str, str]] = []  # (date, symbol, side, broker_order_id)
        self._day_trades: list[date] = []

    # -- market data
    def clock(self) -> dict:
        now = datetime.now(config.MARKET_TZ)
        today = now.date()
        next_open = datetime.combine(today + timedelta(days=1), time(9, 30), config.MARKET_TZ)
        next_close = datetime.combine(today, time(16, 0), config.MARKET_TZ)
        if next_close <= now:
            next_close += timedelta(days=1)
        return {"is_open": self.market_open, "next_open": next_open.isoformat(),
                "next_close": next_close.isoformat(), "timestamp": now.isoformat()}

    def quotes(self, symbols: list[str]) -> dict[str, Optional[dict]]:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        out: dict[str, Optional[dict]] = {}
        for s in symbols:
            q = self.quote_table.get(s)
            out[s] = {"bid": q[0], "ask": q[1], "last": q[2], "ts": ts} if q else None
        return out

    def assets(self, symbols: list[str]) -> dict[str, Optional[dict]]:
        out: dict[str, Optional[dict]] = {}
        for s in symbols:
            if s not in self.quote_table:
                out[s] = None
            elif s == "BTCUSD":
                out[s] = {"tradable": True, "fractionable": True, "name": FAKE_NAMES[s],
                          "exchange": "CRYPTO", "asset_class": "crypto"}
            else:
                out[s] = {"tradable": True, "fractionable": True, "name": FAKE_NAMES.get(s, s),
                          "exchange": "NASDAQ" if s != "SPY" else "ARCA", "asset_class": "us_equity"}
        return out

    # -- account
    def _mark(self, symbol: str) -> float:
        return self.quote_table[symbol][2]

    def positions(self) -> list[dict]:
        out = []
        for s, p in sorted(self._positions.items()):
            if p["qty"] <= 1e-9:
                continue
            price = self._mark(s)
            mv = round(p["qty"] * price, 2)
            cost = round(p["cost"], 2)
            out.append({"symbol": s, "qty": round(p["qty"], 9), "avg_entry_price": round(p["cost"] / p["qty"], 4),
                        "current_price": price, "market_value": mv, "cost_basis": cost,
                        "unrealized_pl": round(mv - cost, 2),
                        "unrealized_plpc": round((mv - cost) / cost, 6) if cost else 0.0})
        return out

    def daytrade_count(self) -> int:
        today = datetime.now(config.MARKET_TZ).date()
        start = _business_days_back(today, 4)
        return sum(1 for d in self._day_trades if d >= start)

    def account(self) -> dict:
        equity = round(self.cash + sum(p["market_value"] for p in self.positions()), 2)
        return {"cash": round(self.cash, 2), "buying_power": round(self.cash, 2), "equity": equity,
                "portfolio_value": equity, "daytrade_count": self.daytrade_count(),
                "pattern_day_trader": False, "currency": "USD"}

    # -- orders
    def submit_limit_order(self, *, symbol: str, side: str, qty: float, limit_price: float,
                           client_order_id: str) -> dict:
        if any(o["client_order_id"] == client_order_id for o in self.orders.values()):
            raise BackendError("Fake: client_order_id must be unique")
        if symbol not in self.quote_table:
            raise BackendError(f"Fake: unknown symbol {symbol}")
        oid = str(uuid.uuid4())
        self.orders[oid] = {"broker_order_id": oid, "client_order_id": client_order_id, "symbol": symbol,
                            "side": side, "qty": qty, "limit_price": limit_price, "status": "submitted",
                            "filled_qty": 0.0, "filled_avg_price": None,
                            "submitted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if self.auto_fill:
            frac = self.partial_fill if self.partial_fill else 1.0
            self._fill(oid, round(qty * frac, 9))
        return self._public(oid)

    def fill_remaining(self, broker_order_id: str) -> None:
        o = self.orders[broker_order_id]
        self._fill(broker_order_id, round(o["qty"] - o["filled_qty"], 9))

    def _fill(self, oid: str, qty: float) -> None:
        o = self.orders[oid]
        if qty <= 0 or o["status"] in ("filled", "canceled", "expired", "rejected"):
            return
        first_fill = o["filled_qty"] == 0
        price = o["limit_price"]
        pos = self._positions.setdefault(o["symbol"], {"qty": 0.0, "cost": 0.0})
        if o["side"] == "buy":
            self.cash -= qty * price
            pos["cost"] += qty * price
            pos["qty"] += qty
        else:
            avg = pos["cost"] / pos["qty"] if pos["qty"] else 0.0
            self.cash += qty * price
            pos["qty"] -= qty
            pos["cost"] -= qty * avg
        o["filled_qty"] = round(o["filled_qty"] + qty, 9)
        o["filled_avg_price"] = price
        o["status"] = "filled" if o["filled_qty"] >= o["qty"] - 1e-9 else "partially_filled"
        if first_fill:
            self._record_day_trade(o)

    def _record_day_trade(self, o: dict) -> None:
        """Same rule as the daemon's pdt_log: a first fill that has an opposite-side fill in
        the same symbol on the same market date is one day trade."""
        today = datetime.now(config.MARKET_TZ).date()
        opposite = "sell" if o["side"] == "buy" else "buy"
        if any(d == today and s == o["symbol"] and side == opposite for d, s, side, _ in self._fills):
            self._day_trades.append(today)
        self._fills.append((today, o["symbol"], o["side"], o["broker_order_id"]))

    def get_order(self, broker_order_id: str) -> dict:
        if broker_order_id not in self.orders:
            raise OrderNotFound(f"Fake: order {broker_order_id} not found")
        return self._public(broker_order_id)

    def cancel_order(self, broker_order_id: str) -> None:
        o = self.orders.get(broker_order_id)
        if o is None:
            raise OrderNotFound(f"Fake: order {broker_order_id} not found")
        if o["status"] in ("filled", "canceled", "expired", "rejected"):
            raise BackendError(f"Fake: order is already {o['status']}")
        o["status"] = "canceled"

    def _public(self, oid: str) -> dict:
        o = self.orders[oid]
        return {k: o[k] for k in ("broker_order_id", "client_order_id", "status", "filled_qty",
                                  "filled_avg_price", "submitted_at")}


def make_backend(kind: Optional[str] = None) -> Backend:
    kind = (kind or config.BROKER_BACKEND or "alpaca").lower()
    if kind == "fake":
        return FakeBackend()
    if kind == "alpaca":
        return AlpacaBackend.from_env()
    raise RuntimeError(f"unknown BROKER_BACKEND {kind!r} (alpaca | fake)")
