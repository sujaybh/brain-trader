# Thesis Trader

Personal thesis-driven trading agent on Alpaca paper. Claude Code (or a local Ollama model,
`LLM_BACKEND`) is the brain, the scripts
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
             review timer, systemd units, install scripts, cron_loop.py (the same schedule
             without systemd), doctor.py health check
deploy/      Docker Compose: one image, broker/web/scheduler services, optional Traefik overlay,
             switch.sh (systemd <-> docker)
web/         phone dashboard + chat over Tailscale, approve/reject endpoints for ntfy buttons
trader/      shared library: config, db schema, thesis spec, scheduling math, broker client, notify
data/        trader.db (SQLite) and the HALT kill-switch file
config/      trader.env (non-secret), alpaca.env (keys, broker user only), limits.json
docs/        brief.md, thesis-spec.md, broker-api.md
tests/       pytest; the broker has a fake backend so the whole chain runs without keys
```

## Setup on a fresh machine (paper)

```sh
git clone git@github.com:sujaybh/brain-trader.git tracker && cd tracker
./setup.sh                       # venv, requirements, config files with fresh tokens, runs the tests
$EDITOR config/alpaca.env        # paste paper keys from the Alpaca dashboard (Paper Trading -> API keys)
```

Then start it **one** of two ways (never both on a machine; `deploy/switch.sh status` says which is live):

| | systemd | Docker Compose |
|---|---|---|
| start | `scheduler/install-user.sh` | `deploy/switch.sh docker` |
| jobs | timers: tick, nightly, weekly review | `scheduler/cron_loop.py` in the `scheduler` container |
| broker | 127.0.0.1:8787 | internal network only, no published port |
| web | `WEB_HOST:8010` | `WEB_BIND:8010` (default 127.0.0.1), or behind Traefik |
| chat / reviews | Claude CLI or Ollama (`LLM_BACKEND`) | Ollama (no Claude CLI in the image) |
| run a tool | `.venv/bin/python tools/positions.py` | `docker compose -f deploy/docker-compose.yml exec scheduler python tools/positions.py` |
| docs | `scheduler/README.md` | `deploy/README.md` |

`.venv/bin/python scheduler/doctor.py` is the read-only health check for either mode (units or
containers, broker, Ollama, DB, last runs) and fails if both modes are active.

`setup.sh` prints the ntfy topic to subscribe to and the dashboard URL (Safari: Share -> Add to
Home Screen). The phone must be on the same tailnet. `WEB_BASIC_AUTH=user:password` adds HTTP
Basic auth on top of the token.

Hardened systemd variant: `sudo scheduler/install.sh` creates a `thesis` system user that alone
can read the keys (moved to `/etc/thesis-trader/alpaca.env`) and runs the units system-wide.
Nothing in the repo hard-codes a path or a host: the unit files use `__ROOT__`, rendered by
the installers; `WEB_URL` and `OLLAMA_URL` live in `config/trader.env`, which is gitignored.

## Moving to a server (Docker + Traefik, Tailscale only)

Only one machine may run against the same Alpaca keys, so the old one is stopped first.

```sh
# 1. old machine, market closed: stop everything, flush the DB, copy state across
deploy/switch.sh status
systemctl --user disable --now thesis-tick.timer thesis-nightly.timer thesis-review.timer thesis-broker thesis-web
.venv/bin/python -c "import sqlite3; sqlite3.connect('data/trader.db').execute('PRAGMA wal_checkpoint(TRUNCATE)')"
rsync -av config/alpaca.env config/trader.env SERVER:tracker/config/      # after cloning there
rsync -av data/trader.db SERVER:tracker/data/

# 2. server
git clone git@github.com:sujaybh/brain-trader.git tracker && cd tracker && mkdir -p data
$EDITOR config/trader.env        # WEB_URL=https://<host the phone opens>, WEB_BASIC_AUTH=user:password,
                                 # OLLAMA_URL / OLLAMA_FALLBACK_* reachable from a container
cp deploy/.env.example deploy/.env && $EDITOR deploy/.env
                                 # TRADER_HOST, TRAEFIK_NETWORK, TRAEFIK_ENTRYPOINT, TRAEFIK_TLS, and
                                 # COMPOSE_FILES="deploy/docker-compose.yml deploy/docker-compose.traefik.yml"
deploy/switch.sh docker          # builds and starts broker, web, scheduler
docker compose -f deploy/docker-compose.yml ps                                    # three healthy
docker compose -f deploy/docker-compose.yml exec scheduler python tools/positions.py
```

Without Traefik, leave `COMPOSE_FILES` unset and set `WEB_BIND` to the server's tailnet IP.
Old notifications keep pointing at the old machine; approve or reject pending proposals from the
new dashboard. Rollback: `docker compose -p thesis down` on the server, copy `data/trader.db`
back, `scheduler/install-user.sh` on the old machine. Full detail: `deploy/README.md`.

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
