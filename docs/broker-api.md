# Broker daemon API

Bound to `127.0.0.1:8787`. Every request carries `X-Broker-Token: <BROKER_TOKEN>`; a wrong
or missing token is `401`. JSON in, JSON out. The daemon is the only process that holds the
Alpaca keys and the only thing allowed to place an order.

## Refusals

A request that the limit chain rejects returns **`403`** with
`{"refused": true, "reason": "<one sentence>", "check": "<limit name>"}`. Every refusal is
journaled (`kind=refused`) and pushed to the phone. Upstream Alpaca errors return `502` with
`{"reason": ...}`. Validation errors are `422`.

## Endpoints

| method | path | returns |
|---|---|---|
| GET | `/health` | `{"ok": true, "backend": "alpaca"\|"fake", "paper": bool, "halted": bool}` |
| GET | `/clock` | `{"is_open": bool, "next_open": iso, "next_close": iso, "timestamp": iso}` |
| GET | `/account` | `{"cash", "buying_power", "equity", "portfolio_value", "daytrade_count", "pattern_day_trader", "currency"}` |
| GET | `/positions` | list of `{"symbol", "qty", "avg_entry_price", "current_price", "market_value", "cost_basis", "unrealized_pl", "unrealized_plpc"}` straight from Alpaca |
| GET | `/quote?symbols=NVDA,AMD` | `{"NVDA": {"bid", "ask", "last", "ts"}, ...}` |
| GET | `/assets?symbols=...` | `{"NVDA": {"tradable": bool, "fractionable": bool, "name", "exchange", "asset_class"}, ...}`; unknown symbols map to `null` |
| POST | `/orders` | place one order (below) |
| GET | `/orders?status=open\|closed\|all&limit=100` | our orders table joined with live Alpaca status |
| GET | `/orders/{client_order_id}` | one order, refreshed from Alpaca |
| DELETE | `/orders/{client_order_id}` | cancel; `{"status": "canceled"}` |
| POST | `/reconcile` | refresh fills for all non-terminal orders from Alpaca, update `orders` and `pdt_log`; returns `{"updated": n, "positions": [...]}` |
| GET | `/limits` | the effective limits (`config/limits.json` merged with defaults) |

### POST `/orders`

Request:
```json
{"thesis_id": "semis-q4", "side": "buy", "symbol": "NVDA", "notional": 50,
 "qty": null, "client_order_id": "semis-q4-2026-10-19T1000-NVDA-b-72ba49", "reason": "dca slot 2026-10-19T10:00"}
```
Exactly one of `notional` (USD) or `qty` (shares). `client_order_id` is optional for manual
orders (the daemon derives one from thesis, symbol, side, timestamp) but **required** from the
scheduler so retries cannot double-buy. If a row with that `client_order_id` already exists the
daemon returns the existing order with `200` and `"duplicate": true` and places nothing.

Response `201`:
```json
{"client_order_id": "...", "broker_order_id": "...", "symbol": "NVDA", "side": "buy",
 "type": "limit", "limit_price": 183.75, "qty": 0.272, "notional": 50, "status": "submitted",
 "filled_qty": 0, "filled_avg_price": null, "submitted_at": "..."}
```

### The limit chain, in order

Each check is a pure function in `broker/limits.py` taking the request plus a context dict
(account, positions, quotes, deployed-by-thesis, pdt counts, limits, halted, clock). The first
failing check stops the chain. The order matters: cheap and absolute first.

1. **halt** – `data/HALT` exists → refuse everything.
2. **token/schema** – handled by FastAPI before the chain.
3. **market_hours** – `clock.is_open` must be true. No extended hours.
4. **asset** – symbol must exist on Alpaca, be `tradable`, `asset_class == us_equity`, and
   `fractionable` if the order is fractional. Crypto/options never pass.
5. **side** – `buy` or `sell` only. A sell may not exceed the quantity this thesis holds
   (attributed from our own filled orders), so nothing can go short.
6. **order_type** – always a marketable limit, day, `extended_hours=false`. Buy limit =
   `ask * (1 + slippage)`, sell limit = `bid * (1 - slippage)`. Market orders cannot be
   requested; there is no field for it.
7. **per_order_cap** – `notional` (or `qty * limit_price`) ≤ `per_order_cap`.
8. **thesis_cap** – deployed(thesis) + notional ≤ thesis `capital_cap`; for the `manual`
   thesis the cap is `manual_thesis_cap`. Unknown thesis id → refuse.
9. **position_cap** – deployed(thesis, symbol) + notional ≤ `capital_cap * max_position_pct`.
10. **global_cap** – sum of deployed over all theses + notional ≤ `global_cap`.
11. **cash** – notional ≤ account cash (not buying power; cash only, never margin).
12. **pdt** – a sell of a symbol bought today, or a buy of a symbol sold today, counts as a
    day trade. If day trades in the last 5 business days (from `pdt_log` ∪ Alpaca's
    `daytrade_count`) are already ≥ `pdt_max_day_trades`, refuse.

### Fractional sizing

Alpaca accepts `notional` only on market orders, and we never send market orders. So a
`notional` request becomes `qty = floor(notional / limit_price, qty_decimals)` on a limit
order. The response reports both. Filled notional is `filled_qty * filled_avg_price`, from
the fill, never from the request.

## Backends

`BROKER_BACKEND=alpaca` uses `alpaca-py` with keys from `config/alpaca.env` (`APCA_PAPER`
chooses paper vs live). `BROKER_BACKEND=fake` is an in-memory simulator: fixed quote table,
market always open unless `FAKE_MARKET_OPEN=false`, orders fill immediately at the limit price
(or partially with `FAKE_PARTIAL_FILL=0.5`). Everything above the backend is identical, so
tests and dry runs exercise the real limit chain.
