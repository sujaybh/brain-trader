# Thesis Trader

Personal thesis-driven trading agent on Alpaca paper. Claude Code is the brain, the scripts
in `tools/` are the hands, the `broker/` daemon is the only thing that can place an order,
and Qwen does the cheap text work. Full design: `docs/brief.md`.

**The rule:** the model proposes, the code disposes. Every money limit lives in
`broker/limits.py` and `config/limits.json`, never in a prompt.

## Layout

```
broker/      localhost daemon; holds the Alpaca keys; enforces every limit; journals everything
tools/       CLI scripts Claude Code runs: quote, positions, thesis_*, run_thesis, order, propose,
             news, review, journal, notify, halt, snapshot, check_triggers,
             bars (daily bars), run_strategy (strategy signals -> proposals), backtest
strategies/  strategy families: pure signals() per spec strategy.type (dca, momentum), shared caps,
             and the backtester (backtest.py)
scheduler/   tick.py (every 15 min, market hours), nightly.py (incl. the bars update), weekly
             review timer, systemd units, install scripts, doctor.py health check
web/         phone dashboard + chat over Tailscale, approve/reject endpoints for ntfy buttons
trader/      shared library: config, db schema, thesis spec, scheduling math, broker client, notify
data/        trader.db (SQLite) and the HALT kill-switch file
config/      trader.env (non-secret), alpaca.env (keys, broker user only), limits.json
docs/        brief.md, thesis-spec.md, broker-api.md
tests/       pytest; the broker has a fake backend so the whole chain runs without keys
```

## Setup on a fresh machine (paper)

```sh
git clone <repo> tracker && cd tracker
./setup.sh                       # venv, requirements, config/trader.env with fresh tokens, runs the tests
$EDITOR config/alpaca.env        # paste paper keys from the Alpaca dashboard (Paper Trading -> API keys)
scheduler/install-user.sh        # user systemd units: broker, web, tick/nightly/weekly-review timers (no sudo)
.venv/bin/python scheduler/doctor.py   # read-only health check of units, broker, Ollama, claude, DB
.venv/bin/python tools/positions.py
```

Then subscribe the ntfy app to the topic `setup.sh` printed, and open the dashboard URL it
printed in Safari (Share -> Add to Home Screen). The phone must be on the same tailnet.

Hardened variant: `sudo scheduler/install.sh` creates a `thesis` system user that alone can
read the keys (moved to `/etc/thesis-trader/alpaca.env`) and runs the units system-wide.
Nothing in the repo hard-codes a path or a host: the unit files use `__ROOT__`, rendered by
the installers; `WEB_URL` and `OLLAMA_URL` live in `config/trader.env`, which is gitignored.

Step 0 is done when one `tools/order.py --side buy --symbol SPY --notional 5` fills during
market hours and the phone buzzes.

Without keys you can still exercise everything:

```sh
BROKER_BACKEND=fake .venv/bin/python -m broker &      # fake Alpaca, market always open
.venv/bin/python tools/order.py --side buy --symbol SPY --notional 5
.venv/bin/python tools/positions.py
.venv/bin/python -m pytest -q
```

## Day to day

- Describe a thesis in chat. Claude compiles it, reads back one paragraph, you say yes.
- Chat and reviews can run on Ollama instead of Claude (`LLM_BACKEND=ollama`, per caller
  `LLM_BACKEND_CHAT` / `LLM_BACKEND_REVIEW`), so a Docker deployment needs no Claude login. Chat
  then has a stricter code-level guard (no `order.py`, trades only as phone-tap proposals); see
  `web/README.md`.
- Every 15 minutes during market hours the tick timer executes any due DCA slot. If the
  laptop was asleep, the slot catches up within 48 hours and then expires.
- Fills reach the phone within about a minute: the broker daemon checks its open orders every
  `BROKER_FILL_POLL_SECONDS` (60) while the market is open; the tick reconciles too, and a fill
  is pushed once either way.
- P&L: cost basis is average cost per thesis per symbol (`trader/pnl.py`). Closed positions
  leave the position list; their result is `realized_pl` (positions.py, snapshots, dashboard).
  The dashboard and thesis page show each thesis's return since its first fill vs the same
  dollars put into SPY on the same days, from the nightly snapshots ("not enough history"
  until there is a snapshot after the first fill day).
- Nightly: reconcile, snapshot, news digest, trigger check. Triggers produce proposals or a
  Claude review, never a silent trade.
- Reviews (`tools/review.py`, on each thesis's cadence or a trigger) are one tool-free Claude
  call with structured output: HOLD / CHANGE / EXIT, a few lines, and at most one proposal,
  which the script files like `propose.py` does. If nothing material changed since the last
  review (no orders, fills, refusals, triggers, proposals or flagged news, held prices within
  5%), it journals "skipped: nothing changed" and does not call Claude; `--force` overrides.
- Chat and reviews run `claude -p` isolated from your personal Claude Code setup (no MCP
  servers or claude.ai connectors, no global CLAUDE.md, memory, plugins or skills); see
  `web/README.md`.
- Saturdays 10:00 ET: `review.py --all` reviews every thesis whose review cadence is due.
- Moving to an always-on server: see `scheduler/README.md`.
- Proposals arrive as a notification with Approve / Reject buttons. `mode: auto` theses
  skip the buttons and just tell you.
- `touch data/HALT` stops everything. Only you remove it.
- Strategy theses (`strategy` block, e.g. momentum): backtest first with `tools/backtest.py`, then
  `tools/run_strategy.py` turns signals into proposals. Not on a timer yet; see docs/thesis-spec.md.

## Limits (`config/limits.json`)

global cap, per-order cap, manual-thesis cap, limit slippage, PDT guard (3 day trades per
5 business days), catch-up window. Per-thesis and per-name caps live in each thesis spec.
