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
             news, review, journal, notify, halt, snapshot, check_triggers
scheduler/   tick.py (every 15 min, market hours) and nightly.py, plus systemd units + install.sh
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
scheduler/install-user.sh        # user systemd units: broker, web, tick timer, nightly timer (no sudo)
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
- Every 15 minutes during market hours the tick timer executes any due DCA slot. If the
  laptop was asleep, the slot catches up within 48 hours and then expires.
- Nightly: reconcile, snapshot, news digest, trigger check. Triggers produce proposals or a
  Claude review session, never a silent trade.
- Proposals arrive as a notification with Approve / Reject buttons. `mode: auto` theses
  skip the buttons and just tell you.
- `touch data/HALT` stops everything. Only you remove it.

## Limits (`config/limits.json`)

global cap, per-order cap, manual-thesis cap, limit slippage, PDT guard (3 day trades per
5 business days), catch-up window. Per-thesis and per-name caps live in each thesis spec.
