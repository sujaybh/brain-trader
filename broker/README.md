# broker/

The only process that holds the Alpaca keys and the only thing that can place an order.
Contract: `docs/broker-api.md`. Bound to `127.0.0.1:8787` by default.

## Run

```sh
# simulated broker (no keys needed): in-memory quotes, fills immediately at the limit
BROKER_TOKEN=dev BROKER_BACKEND=fake .venv/bin/python -m broker

# Alpaca (paper by default)
.venv/bin/python -m broker
```

Settings come from `config/trader.env` (`BROKER_TOKEN`, `BROKER_HOST`, `BROKER_PORT`,
`BROKER_BACKEND`, `NTFY_*`). The daemon will not start without `BROKER_TOKEN`. Every endpoint
except `/health` needs the `X-Broker-Token` header.

Fake backend knobs: `FAKE_CASH` (default 10000), `FAKE_MARKET_OPEN` (default true),
`FAKE_PARTIAL_FILL` (e.g. `0.5`), `FAKE_AUTO_FILL` (default true; false leaves orders open).

## Keys

Put the Alpaca keys in `config/alpaca.env` (see `config/alpaca.env.example`):
`APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`, `APCA_PAPER=true`. Only the broker user should be
able to read it (`chmod 600`). Nothing else in the system reads this file.

## The limit chain (`broker/limits.py`)

Every `POST /orders` is checked in this order, and the first failure stops the chain. A
failure returns `403 {"refused": true, "reason", "check"}`, is journaled (`kind=refused`)
and pushed to the phone.

1. `halt`: `data/HALT` exists
2. token/schema: FastAPI (401 / 422) before the chain
3. `market_hours`: the clock must be open (regular hours only)
4. `asset`: must be known, `us_equity`, tradable, and fractionable if the order is fractional
5. `side`: buy/sell only; a sell may not exceed what the thesis holds (our fills, net of
   open sells) or what the account holds, so nothing can go short
6. `order_type`: needs a usable quote; the marketable limit must size to more than 0 shares
   and at least `min_order_notional`
7. `per_order_cap`
8. `thesis_cap`: unknown thesis refused; `manual` uses `manual_thesis_cap`
9. `position_cap`: `capital_cap * max_position_pct / 100` per symbol
10. `global_cap`: summed over all theses
11. `cash`: account cash minus open buy orders (never buying power or margin)
12. `pdt`: a same-day round trip refused once day trades in 5 business days reach
    `pdt_max_day_trades` (the larger of `pdt_log` and Alpaca's `daytrade_count`)

Caps 8 to 11 apply to buys. "Deployed" is net filled notional from our own orders plus the
unfilled part of open buys. The daemon reconciles open orders from the backend before
every order, so the caps use fresh fills.

Orders are always `limit`, `day`, never extended hours. Buy limit = ask × (1 + slippage%),
sell limit = bid × (1 − slippage%), rounded to cents. A notional request becomes
`qty = floor(notional / limit, qty_decimals)`.

## Tests

```sh
.venv/bin/python -m pytest tests/test_limits.py tests/test_broker_fake.py -q
```
