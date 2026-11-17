"""The limit chain. Every check is a pure function `(req, ctx) -> None | (check_name, reason)`.

`ctx` is a mapping with these keys (the daemon fills it lazily so cheap checks never touch
the network; tests pass a plain dict):

    halted        bool
    clock         {"is_open": bool, ...}
    assets        {symbol: {"tradable", "fractionable", "asset_class", ...} | None}
    quotes        {symbol: {"bid", "ask", "last"} | None}
    account       {"cash": float, "daytrade_count": int, ...}
    positions     [{"symbol", "qty", ...}]            straight from the backend
    held_qty      {thesis_id: {symbol: qty}}           our fills, net of open sells
    deployed      {thesis_id: {symbol: usd}}           net filled notional + open buys
    pending_buys  float                                 unfilled notional of open buys
    thesis        {"id", "capital_cap", "max_position_usd"} | None   (None = unknown)
    pdt           {"logged": int, "bought_today": set, "sold_today": set}
    limits        effective limits (run_chain sets it)

The order matters: cheap and absolute first. The first refusal stops the chain.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any, Callable, Mapping, Optional

Refusal = tuple[str, str]
EPS = 1e-6


@dataclass(frozen=True)
class OrderReq:
    """A normalized order request (symbol upper-case, side lower-case)."""
    thesis_id: str
    side: str
    symbol: str
    notional: Optional[float] = None
    qty: Optional[float] = None
    client_order_id: Optional[str] = None
    reason: str = ""


@dataclass(frozen=True)
class Sizing:
    limit_price: float
    qty: float
    notional: float      # USD the order is sized for: requested notional, or qty * limit
    fractional: bool


def floor_to(x: float, decimals: int) -> float:
    q = Decimal(1).scaleb(-decimals)
    return float(Decimal(str(x)).quantize(q, rounding=ROUND_DOWN))


def limit_price(side: str, quote: Mapping[str, Any], slippage_pct: float) -> Optional[float]:
    """Marketable limit, rounded to cents.

    Buy: (1 + s) x the LOWER of the ask and the last trade. Sell: (1 - s) x the HIGHER of the
    bid and the last trade. On a tight market the quote and the last trade agree, so this is
    the plain ask/bid + slippage. On the free IEX feed the quoted side can sit several percent
    away from the national market (seen 6% on AMD and TSM at 10:00 ET), which would turn a
    0.3% slippage allowance into 6%; anchoring on the last trade keeps the allowance honest.
    Alpaca still fills at the national best price, so a tighter limit costs nothing when the
    market is normal and simply does not fill when it is not. No positive reference -> None."""
    quoted = quote.get("ask") if side == "buy" else quote.get("bid")
    refs = [float(x) for x in (quoted, quote.get("last")) if x and float(x) > 0]
    if not refs:
        return None
    ref = min(refs) if side == "buy" else max(refs)
    factor = 1 + slippage_pct / 100 if side == "buy" else 1 - slippage_pct / 100
    return round(ref * factor, 2)


def sizing(req: OrderReq, ctx: Mapping[str, Any]) -> Optional[Sizing]:
    """Limit price and share quantity for the request, or None if there is no usable quote."""
    limits = ctx["limits"]
    quote = ctx["quotes"].get(req.symbol)
    if not quote or req.side not in ("buy", "sell"):
        return None
    lp = limit_price(req.side, quote, float(limits["limit_slippage_pct"]))
    if lp is None:
        return None
    decimals = int(limits["qty_decimals"])
    if req.notional is not None:
        qty = floor_to(req.notional / lp, decimals)
        notional = float(req.notional)
    else:
        qty = floor_to(float(req.qty), decimals)
        notional = round(qty * lp, 2)
    return Sizing(limit_price=lp, qty=qty, notional=notional, fractional=qty != int(qty))


# ---------------------------------------------------------------- checks, in chain order

def check_halt(req: OrderReq, ctx: Mapping[str, Any]) -> Optional[Refusal]:
    if ctx["halted"]:
        return ("halt", "Kill switch is on (data/HALT exists); all orders are refused.")
    return None


def check_market_hours(req: OrderReq, ctx: Mapping[str, Any]) -> Optional[Refusal]:
    if not ctx["clock"].get("is_open"):
        return ("market_hours", "Market is closed; orders are only placed during regular hours.")
    return None


def check_asset(req: OrderReq, ctx: Mapping[str, Any]) -> Optional[Refusal]:
    asset = ctx["assets"].get(req.symbol)
    if not asset:
        return ("asset", f"{req.symbol} is not a known Alpaca asset.")
    if asset.get("asset_class") != "us_equity":
        return ("asset", f"{req.symbol} is {asset.get('asset_class')}, only US equities are allowed.")
    if not asset.get("tradable"):
        return ("asset", f"{req.symbol} is not tradable on Alpaca.")
    s = sizing(req, ctx)
    fractional = s.fractional if s else req.notional is not None or float(req.qty or 0) % 1 != 0
    if fractional and not asset.get("fractionable"):
        return ("asset", f"{req.symbol} is not fractionable; order whole shares by qty.")
    return None


def check_side(req: OrderReq, ctx: Mapping[str, Any]) -> Optional[Refusal]:
    if req.side not in ("buy", "sell"):
        return ("side", f"Side must be buy or sell, not {req.side!r}.")
    if req.side == "buy":
        return None
    s = sizing(req, ctx)
    if s is None:
        return None  # no quote; order_type refuses it next
    held = ctx["held_qty"].get(req.thesis_id, {}).get(req.symbol, 0.0)
    pos = next((p for p in ctx["positions"] if p.get("symbol") == req.symbol), None)
    account_qty = float(pos["qty"]) if pos else 0.0
    can_sell = min(held, account_qty)
    if s.qty > can_sell + EPS:
        return ("side", f"Sell of {s.qty:g} {req.symbol} exceeds the {max(can_sell, 0):g} shares "
                        f"thesis {req.thesis_id} holds; shorting is not allowed.")
    return None


def check_order_type(req: OrderReq, ctx: Mapping[str, Any]) -> Optional[Refusal]:
    s = sizing(req, ctx)
    if s is None:
        return ("order_type", f"No usable quote for {req.symbol}; cannot price a marketable limit order.")
    if s.qty <= 0:
        return ("order_type", f"Order rounds to zero shares at limit {s.limit_price}.")
    min_notional = float(ctx["limits"].get("min_order_notional", 1.0))
    if s.qty * s.limit_price < min_notional - EPS:
        return ("order_type", f"Order value ${s.qty * s.limit_price:.2f} is below the ${min_notional:g} minimum.")
    return None


def check_per_order_cap(req: OrderReq, ctx: Mapping[str, Any]) -> Optional[Refusal]:
    s = sizing(req, ctx)
    cap = float(ctx["limits"]["per_order_cap"])
    if s.notional > cap + EPS:
        return ("per_order_cap", f"Order of ${s.notional:.2f} exceeds the ${cap:g} per-order cap.")
    return None


def _thesis_deployed(ctx: Mapping[str, Any], thesis_id: str) -> dict[str, float]:
    return ctx["deployed"].get(thesis_id, {})


def check_thesis_cap(req: OrderReq, ctx: Mapping[str, Any]) -> Optional[Refusal]:
    thesis = ctx["thesis"]
    if thesis is None:
        return ("thesis_cap", f"Unknown thesis {req.thesis_id!r}.")
    if req.side != "buy":
        return None
    s = sizing(req, ctx)
    deployed = sum(max(v, 0.0) for v in _thesis_deployed(ctx, req.thesis_id).values())
    cap = float(thesis["capital_cap"])
    if deployed + s.notional > cap + EPS:
        return ("thesis_cap", f"Thesis {req.thesis_id} has ${deployed:.2f} deployed; ${s.notional:.2f} more "
                              f"would exceed its ${cap:g} cap.")
    return None


def check_position_cap(req: OrderReq, ctx: Mapping[str, Any]) -> Optional[Refusal]:
    if req.side != "buy":
        return None
    s = sizing(req, ctx)
    in_name = max(_thesis_deployed(ctx, req.thesis_id).get(req.symbol, 0.0), 0.0)
    cap = float(ctx["thesis"]["max_position_usd"])
    if in_name + s.notional > cap + EPS:
        return ("position_cap", f"Thesis {req.thesis_id} has ${in_name:.2f} in {req.symbol}; ${s.notional:.2f} "
                                f"more would exceed the ${cap:g} per-name cap.")
    return None


def check_global_cap(req: OrderReq, ctx: Mapping[str, Any]) -> Optional[Refusal]:
    if req.side != "buy":
        return None
    s = sizing(req, ctx)
    total = sum(max(v, 0.0) for per in ctx["deployed"].values() for v in per.values())
    cap = float(ctx["limits"]["global_cap"])
    if total + s.notional > cap + EPS:
        return ("global_cap", f"${total:.2f} is deployed across all theses; ${s.notional:.2f} more would "
                              f"exceed the ${cap:g} global cap.")
    return None


def check_cash(req: OrderReq, ctx: Mapping[str, Any]) -> Optional[Refusal]:
    if req.side != "buy":
        return None
    s = sizing(req, ctx)
    cash = float(ctx["account"]["cash"]) - float(ctx.get("pending_buys", 0.0) or 0.0)
    if s.notional > cash + EPS:
        return ("cash", f"Order of ${s.notional:.2f} exceeds available cash ${cash:.2f}; margin is never used.")
    return None


def check_pdt(req: OrderReq, ctx: Mapping[str, Any]) -> Optional[Refusal]:
    pdt = ctx["pdt"]
    if req.side == "sell":
        is_day_trade = req.symbol in pdt["bought_today"]
    else:
        is_day_trade = req.symbol in pdt["sold_today"]
    if not is_day_trade:
        return None
    count = max(int(pdt["logged"]), int(ctx["account"].get("daytrade_count") or 0))
    limit = int(ctx["limits"]["pdt_max_day_trades"])
    if count >= limit:
        return ("pdt", f"This {req.side} of {req.symbol} would be a day trade and {count} day trades "
                       f"already happened in the last 5 business days (max {limit}).")
    return None


CHAIN: list[tuple[str, Callable[[OrderReq, Mapping[str, Any]], Optional[Refusal]]]] = [
    ("halt", check_halt),
    # token/schema: enforced by FastAPI before the chain runs
    ("market_hours", check_market_hours),
    ("asset", check_asset),
    ("side", check_side),
    ("order_type", check_order_type),
    ("per_order_cap", check_per_order_cap),
    ("thesis_cap", check_thesis_cap),
    ("position_cap", check_position_cap),
    ("global_cap", check_global_cap),
    ("cash", check_cash),
    ("pdt", check_pdt),
]


def run_chain(req: OrderReq, ctx: Any, limits: Mapping[str, Any]) -> Optional[Refusal]:
    """Run every check in order; return the first refusal or None if the order may go."""
    ctx["limits"] = limits
    for _name, check in CHAIN:
        refusal = check(req, ctx)
        if refusal is not None:
            return refusal
    return None


class LazyContext(dict):
    """A dict whose missing keys are computed on first access by `loaders[key](ctx)`, so
    the halt and clock checks never pay for quotes, positions or DB queries."""

    def __init__(self, loaders: Mapping[str, Callable[["LazyContext"], Any]]):
        super().__init__()
        self._loaders = loaders

    def __missing__(self, key: str) -> Any:
        if key not in self._loaders:
            raise KeyError(key)
        value = self._loaders[key](self)
        self[key] = value
        return value

    def get(self, key: str, default: Any = None) -> Any:  # dict.get bypasses __missing__
        try:
            return self[key]
        except KeyError:
            return default
