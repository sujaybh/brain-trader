# Roadmap: from one DCA thesis to a multi-strategy lab

The v1 pipeline is `thesis spec → deterministic executor → broker limit chain → journal`.
Every strategy family below plugs into that same pipeline. Nothing bypasses the broker, and
nothing in a prompt decides on a schedule. What changes per family is only **how a signal is
produced**; what it produces is always the same object: a proposal (or, in `auto` mode, an
order) with a reason attached.

## The extension point

`trader/spec.py` gains a `strategy` block and the executor dispatches on `strategy.type`:

```json
"strategy": { "type": "dca", ... }                        // v1: schedule_only buying
"strategy": { "type": "momentum", "lookback_days": 126, "rebalance": "monthly", "top_n": 3 }
"strategy": { "type": "mean_reversion", "zscore_entry": -2.0, "zscore_exit": 0.0, "window": 20 }
"strategy": { "type": "news", "min_relevance": 8, "sentiment": "positive", "confirm": true }
"strategy": { "type": "insider", "min_purchase_usd": 250000, "roles": ["CEO", "CFO", "10%"], "cluster_days": 30 }
```

Each type is a module in `strategies/<type>.py` with one pure function:

```python
def signals(spec: ThesisSpec, data: MarketData, state: ThesisState) -> list[Signal]
# Signal = {symbol, side, notional|qty, reason, confidence, expires}
```

`data` is a snapshot the scheduler assembles (bars, quotes, news rows, insider filings,
positions) so the function is deterministic and unit-testable with fixtures. The scheduler
turns each Signal into `propose.py` (mode `confirm`) or `order.py` (mode `auto`). Caps, PDT,
market hours and the kill switch apply exactly as today.

## Families, cheapest first

| family | data needed | signal source | LLM involvement | when it fits |
|---|---|---|---|---|
| **dca** (v1) | none | calendar | none | done |
| **quant: momentum / mean reversion** | daily bars from Alpaca's data API (free on paper) | arithmetic over bars | none | next: fully deterministic, backtestable, no new vendors |
| **news-driven** | RSS already wired; Qwen digest already in the `news` table | relevance+sentiment thresholds over the digest; Claude only writes the *reason* for the review | Qwen for digest, Claude for review | after quant, since the digest quality is the risk |
| **insider-following** | SEC EDGAR Form 4 (public, free, has an API and daily index) | cluster buys by officers/10% holders above a dollar size | none for the signal; Claude for the review | legal and well studied; data plumbing is the work |
| **earnings / events** | earnings calendar (Alpaca has corporate actions; or a free calendar feed) | pause DCA into earnings, or buy the post-earnings drift | none | small add-on to any family |

"Insider trading based" here means following *disclosed* insider purchases (Form 4 filings
are public within two business days). That is a standard, legal factor. Trading on non-public
information is not something this system can or should do.

## What has to exist before a second family is worth running

1. **Bars + a backtester.** `strategies/backtest.py`: replay a spec over stored daily bars
   with the same `signals()` function, same caps, a simple fill model (next open, slippage
   = our limit offset), and output P&L, drawdown, turnover, number of trades. Without this,
   every new family is a guess. Store bars in a `bars` table filled nightly.
2. **Per-thesis benchmarking on the dashboard.** The `$BENCH` snapshot row exists; the review
   pack already compares to SPY. Add the same for every thesis on the dashboard so families
   compete on one screen.
3. **Portfolio-level caps.** Today: global, per-thesis, per-name. Add per-family and
   per-sector so three theses can't all be 100% NVDA.
4. **A paper run per family, side by side, with small caps.** Two to four weeks each before
   anything is promoted, same rule as the brief.

## Guardrails that matter more with more strategies

- **Overfitting.** A backtest that only beats SPY after tuning five parameters is noise.
  Prefer families with one or two parameters and a published rationale.
- **Data quality.** Free RSS feeds carry off-topic stories; EDGAR parsing breaks on odd
  filings. Every signal records its inputs in `data` so a bad trade can be traced to the row.
- **Costs.** Qwen is free. Claude reviews are pinned to a cheap model and metered
  (`tools/usage.py`). A strategy that needs an LLM in the signal loop is a design smell.
- **PDT.** Short-horizon mean reversion will hit the day-trade limit on a small account. The
  broker refuses the fourth; the strategy should not generate it.

## Suggested order

quant momentum (monthly rebalance, 3 names, `auto` mode after one clean paper month) →
backtester → news-driven (confirm mode) → insider-following → per-family dashboard.
