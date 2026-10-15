# Thesis Trader — project brief

A personal trading agent that lives on my Tailscale server. I describe investment theses in plain English, it executes them on a schedule through Alpaca, pushes a notification to my phone whenever it buys or sells, and I can chat with it like a broker. Claude Code is the brain, small scripts are the hands, and a local Qwen model does the cheap high-volume text work.

Start on Alpaca **paper trading**. Nothing touches real money until the paper run has been clean for a few weeks.

## The one rule that shapes everything

**The model proposes, the code disposes.**

An LLM is good at three jobs here: translating ("buy 3 NVDA" → an order), analyzing ("what are we losing money on?"), and compiling a prose thesis into a structured spec. It is bad at being the thing that decides, on a schedule, whether to trade: it is non-deterministic, it rationalizes, and anything that lives in a prompt can be talked out of.

So:

- A thesis is compiled **once** into structured rules (JSON), read back to me in plain English, and confirmed.
- Deterministic scripts execute those rules on a schedule. No LLM in that loop.
- The LLM re-enters only for **judgment events**: a periodic review, a trigger (big drawdown, news), or me asking a question.
- Every hard limit (dollar caps, order types, kill switch) lives in code. `CLAUDE.md` describes the persona and the workflow; it is never the thing enforcing a limit.

## Components

**`broker/` — the only thing that can place an order.** A small daemon bound to localhost. It alone holds the Alpaca keys. It exposes `account`, `positions`, `quote`, `place_order`, `cancel`, and refuses any order that violates the limits below. Everything it does is appended to the journal.

**`tools/` — CLI scripts Claude Code calls.** Arguments in, JSON on stdout, non-zero exit on failure, no side effects beyond what the name says.

**`scheduler/`** — systemd timers. Fires scheduled thesis actions, a nightly news + snapshot job, and reconciliation. Judgment runs are Claude Code sessions.

**`web/`** — small FastAPI app over Tailscale. Dashboard plus chat box.

**`data/`** — SQLite: `theses`, `orders`, `proposals`, `journal`, `snapshots`, `pdt_log`.

**Qwen (local)** — news digest, headline tagging, journal summaries.

**Claude Code** — thesis compilation, reviews, trigger decisions, chat.

## Hard limits (enforced in `broker/`, never in a prompt)

- Global cap on total deployed capital; per-thesis cap; per-order cap.
- Cash only. No margin, options, shorts, or crypto.
- Limit orders only (marketable limit, e.g. ask + 0.3%). Never market orders.
- Regular market hours only.
- PDT guard: refuse any order that would be the 4th day trade in 5 business days.
- Kill switch: if `data/HALT` exists, refuse everything.
- Idempotency: every order carries a `client_order_id` derived from `(thesis, date, symbol, slot)`.
- Reconcile positions from Alpaca before any decision. Alpaca is the source of truth; SQLite is a cache.
- Every refused order is journaled with the reason and pushed to me.

## Gotchas

- Wash sales — track; warn in review.
- Partial fills — filled quantity is truth.
- Fractional shares — dollar-sized orders must work.
- Corporate actions — use Alpaca's position data, not stored cost basis.
- Timezones — schedule in `America/New_York` explicitly.
- Model drift — if Claude and the journal disagree about what a thesis is for, the journal wins.

## Build order

0. Paper account, broker with limits, order/positions/notify. Done when one paper order goes through and the phone buzzes.
1. Thesis spec, create, DCA run, scheduler. Done when a thesis buys on its own twice. Paper for 2–4 weeks.
2. Web dashboard and chat.
3. News with Qwen, triggers, propose/approve flow, monthly review.
4. Live, with caps at a fraction of paper. Raise slowly.

## Open questions

- Taxable account or IRA?
- `mode: confirm` for everything, or `auto` for pure DCA?
- Qwen: `qwen3-coder:30b` on 100.64.0.6 (GPU), `qwen3.5` 9.7B locally as fallback.
