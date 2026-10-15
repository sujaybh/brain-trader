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
| `schedule.type` | `dca` (buy `amount` on `cron`, split equally across the universe, skipping names at their cap) or `none` |
| `schedule.cron` | 5-field cron, interpreted in `schedule.tz` (always `America/New_York` unless the user insists) |
| `entry.type` | `schedule_only`: only the scheduler buys. `manual`: only chat/`order.py` buys |
| `exit.stop_loss_pct` / `take_profit_pct` | thresholds checked by the nightly job. With `review_only: true` (default) they produce a proposal, never an automatic sell |
| `triggers[]` | `drawdown_pct` (position down X% from avg entry), `news` (Qwen flags a high-relevance negative story), `price_below`/`price_above` (needs `symbol` or applies to any). `action`: `review` (start a Claude review session), `pause` (pause the thesis and notify), `propose_exit` (create a sell proposal) |
| `mode` | `confirm`: proposals need an Approve tap. `auto`: act and notify. Scheduled DCA buys are not proposals; they just run |
| `review` | cadence of the Claude review: `weekly` / `monthly` / `quarterly` |
| `status` | `active` / `paused` / `closed`. Paused theses skip scheduled slots entirely (missed slots are not caught up on resume) |
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
