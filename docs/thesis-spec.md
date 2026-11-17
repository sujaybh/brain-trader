# Thesis spec

A thesis is compiled once from prose into this JSON, read back to the user as one paragraph
(`trader.spec.describe`), confirmed, and stored by `tools/thesis_create.py`. After that the
scheduler executes it deterministically. The spec is validated by `trader/spec.py`.

```json
{
  "id": "semis-q4",
  "name": "Big semiconductors",
  "rationale": "AI capex keeps flowing to the handful of companies that design or fab the chips.",
  "universe": ["NVDA", "AMD", "AVGO", "TSM"],
  "capital_cap": 300,
  "max_position_pct": 60,
  "schedule": { "type": "dca", "amount": 50, "cron": "0 10 * * 1", "tz": "America/New_York" },
  "entry": { "type": "schedule_only" },
  "exit": { "stop_loss_pct": 20, "take_profit_pct": null, "review_only": true },
  "triggers": [ { "type": "drawdown_pct", "value": 15, "action": "review" } ],
  "mode": "confirm",
  "review": "monthly",
  "status": "active",
  "created": "2026-10-15",
  "source_text": "invest in big semiconductor companies, use 300 dollars max, don't diversify too much"
}
```

| field | meaning |
|---|---|
| `id` | slug, 2–40 chars `[a-z0-9-]`; used in order ids and journal |
| `universe` | 1–25 tickers; every one must be tradable on Alpaca or `thesis_create` refuses and says which |
| `capital_cap` | USD this thesis may ever have deployed (net of sells) |
| `max_position_pct` | % of `capital_cap` allowed in a single name |
| `schedule.type` | `dca` (buy `amount` on `cron`, split equally across the universe, skipping names at their cap) or `none` (`amount` may be omitted) |
| `schedule.cron` | 5-field cron, interpreted in `schedule.tz` (always `America/New_York` unless the user insists) |
| `entry.type` | `schedule_only`: only the scheduler buys. `manual`: only chat/`order.py` buys |
| `exit.stop_loss_pct` / `take_profit_pct` | thresholds checked by the nightly job. With `review_only: true` (default) they produce a proposal, never an automatic sell |
| `triggers[]` | `drawdown_pct` (position down X% from avg entry), `news` (Qwen flags a high-relevance negative story), `price_below`/`price_above` (needs `symbol` or applies to any). `action`: `review` (start a Claude review session), `pause` (pause the thesis and notify), `propose_exit` (create a sell proposal) |
| `mode` | `confirm`: proposals need an Approve tap. `auto`: act and notify. Scheduled DCA buys are not proposals; they just run |
| `review` | cadence of the Claude review: `weekly` / `monthly` / `quarterly` |
| `status` | `active` / `paused` / `closed`. Paused theses skip scheduled slots entirely (missed slots are not caught up on resume) |
| `strategy` | optional; omit it for v1 behaviour (driven by `schedule`). See "Strategy theses" below |
| `source_text` | the user's words, verbatim, so a review can check the rules still match intent |

## How a DCA slot executes

1. The scheduler tick (every 15 min) lists cron occurrences in the last `catchup_hours`.
2. For each occurrence (slot) and each symbol in the allocation, it derives
   `client_order_id(thesis, symbol, slot)`. If that id is already in `orders`, skip.
3. If the market is closed, stop; the slot stays pending until the next tick during hours.
   A Monday 10:00 slot missed because the laptop was asleep executes on the next open tick
   within 48 hours, then expires with a journal entry.
4. Allocation: `schedule.amount` split equally across the universe; names at
   `max_position_pct` are skipped and their share redistributed; the total is clipped to
   the remaining room under `capital_cap`. Positions are attributed to the thesis from our
   own filled orders. If nothing fits, the slot is journaled as `skipped: thesis fully deployed`.
5. Each order goes through the broker's full limit chain. A refusal is journaled and pushed;
   the other symbols in the slot still execute.

## The `manual` thesis

Quick trades from chat with no thesis attached land in the implicit thesis `manual`
(cap `manual_thesis_cap` from `config/limits.json`). It has no schedule.

## Strategy theses

The optional `strategy` block picks how signals are produced (`strategies/<type>.py`).
Without it a spec behaves exactly as before; `{"type": "dca"}` is the same as no block.

```json
"schedule": { "type": "none" },
"strategy": { "type": "momentum", "lookback_days": 126, "top_n": 3, "rebalance": "monthly" }
```

| field | meaning |
|---|---|
| `strategy.type` | `dca` (v1, needs `schedule.type: dca`) or `momentum` (needs `schedule.type: none` and `entry.type: schedule_only`) |
| `lookback_days` | momentum: trading days of close-to-close return to rank by, 5–504 (default 126 ≈ 6 months) |
| `top_n` | momentum: how many of the best-ranked names to hold, equal weight, ≤ universe size (default 3) |
| `rebalance` | momentum: `monthly` (default) or `weekly`; one decision per period |

Momentum target per name is `min(capital_cap / top_n, capital_cap * max_position_pct)`, valued at
the current price. Names that fall out of the top `top_n` are sold in full; held names more than
10% off target are trimmed or topped up. Every proposed order then goes through
`strategies/caps.py` (capital cap, per-name cap, global cap, minimum size, split above
`per_order_cap`) and, when approved, the broker's full limit chain.

Read-back example: *"Once a month, rank NVDA/AMD/AVGO/TSM by their 126-trading-day return and
hold the top 3 in equal weight (about $100 each), selling names that drop out; never more than
$300 total; ...; propose every trade for my approval; review monthly."*

### Running a strategy thesis

Data comes from the `bars` table (daily bars, filled by `tools/bars.py` and the nightly job).

```sh
.venv/bin/python tools/bars.py                    # backfill/update universes + SPY (nightly does this too)
.venv/bin/python tools/backtest.py --spec s.json  # before creating it: how would it have done?
.venv/bin/python tools/run_strategy.py --dry-run  # signals and capped orders, writes nothing
.venv/bin/python tools/run_strategy.py            # proposals (Approve/Reject on the phone)
```

`run_strategy.py` only ever creates `confirm` proposals: a thesis in `auto` mode is skipped, not
auto-executed. It is idempotent per period (setting `strategy_done:<id>:<period>`), waits while
earlier proposals of the thesis are still pending, and refuses to run on missing or stale bars.
Approve sells before buys: buys are sized as if the sells went through, and the broker refuses a
buy that would exceed the cap before they do.

It is **not on a timer**. To enable it, once a backtest and a dry run look right, add a user
timer that runs after the nightly bars step, e.g. weekdays 09:45 New York:
`ExecStart=__ROOT__/.venv/bin/python __ROOT__/tools/run_strategy.py` in a
`thesis-strategy.service` + `.timer` pair modelled on `thesis-tick.*`, then
`systemctl --user enable --now thesis-strategy.timer`. Running it daily is safe: each thesis
acts once per rebalance period.
