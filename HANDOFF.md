# Handoff — updated 2026-11-16 (Mon) night

Read `README.md` for the layout and `docs/brief.md` for the design. This file is the state of
play, what to do next, and the feature backlog. Delete sections when they stop being true.

## State right now

- **Code:** everything is committed and pushed to `git@github.com:sujaybh/brain-trader.git`
  (`master`). 353 tests, fake backend, no keys needed: `.venv/bin/python -m pytest -q`.
- **Running on the laptop:** the *old* user systemd units (broker, web, tick timer, nightly
  timer). The broker and web processes are still the pre-Nov-15 code; tick/nightly already run
  the new code from the checkout. **Nothing has been restarted or reinstalled yet.**
- **Paper account:** ~$104.4k cash. `semis-q4` has $107.80 of $300 deployed (two slots, Oct 19
  and Oct 26), unrealized about −$8.50. `manual` is flat with −$0.08 realized.
- **Laptop was off Oct 30 – Nov 15.** The Nov 2 and Nov 9 slots expired (older than the 48h
  catch-up). The Nov 16 10:00 slot is still pending (laptop slept through the session) and runs
  on the first tick with the market open, if that is before Wed Nov 18 10:00 ET.
- **Needs your decision tonight/tomorrow:** proposals **#6** (from the review) and **#7** (from
  the stop-loss trigger) both sell the same 0.1152 NVDA (−26.4%, past the 20% stop). Approve at
  most one; reject the other.

## Do next, in order

1. **Pick one way to run it on this machine** (never both; `deploy/switch.sh status` shows which):
   - systemd: `scheduler/install-user.sh` (reinstalls units, restarts broker + web, adds the
     weekly review timer), or
   - Docker: `deploy/switch.sh docker` (stops the systemd units, builds, `compose up`).
   Then `.venv/bin/python scheduler/doctor.py` until there is no FAIL.
2. **First real backtest** (needs the restarted broker for `/bars`):
   `tools/bars.py` → `tools/backtest.py --thesis semis-q4`.
3. **One market-hours pass of `tools/live_check.py`** (phases `open`, `idle`, `close`; see
   `--phase all --dry-run`) on the new code.
4. **Chat check, both backends:** ask "what do we hold?"; in Claude mode also ask it to run
   `ls /` and confirm it cannot (the Bash allow-rule was never verified live).
5. **Move to the server** (below), then turn the laptop's units/containers off.
6. Paper for 2–4 clean weeks before anything else.

## Two ways to run it

| | systemd (`scheduler/`) | Docker Compose (`deploy/`) |
|---|---|---|
| start | `scheduler/install-user.sh` | `deploy/switch.sh docker` |
| jobs | timers: tick, nightly, weekly review | `scheduler/cron_loop.py` in the `scheduler` container, same schedule, catch-up on start |
| broker | 127.0.0.1:8787 | compose-internal only, **no published port** |
| web | `WEB_HOST:8010` | `WEB_BIND` (default 127.0.0.1) : 8010, or Traefik |
| LLM | Claude CLI or Ollama | Ollama only (no Claude CLI in the image) |
| health | `scheduler/doctor.py` (auto-detects the mode; FAILs if both are active) | same |

**Server with Traefik (Tailscale only)** (step by step in README.md, "Moving to a server"): copy `config/alpaca.env`, `config/trader.env`,
`config/limits.json` and `data/trader.db`; `cp deploy/.env.example deploy/.env` and set
`TRADER_HOST`, `TRAEFIK_NETWORK`, `TRAEFIK_ENTRYPOINT`, `WEB_URL=https://<host>` (what the
phone opens; ntfy buttons are built from it) and `WEB_BASIC_AUTH=user:password`; then
`COMPOSE_FILES="deploy/docker-compose.yml deploy/docker-compose.traefik.yml" deploy/switch.sh docker`.
The overlay only adds labels and the Traefik network to `web`. If Traefik should issue the
certificate, uncomment the certresolver label in `docker-compose.traefik.yml`. Details and
rollback: `deploy/README.md`. **Not tested against a real Traefik** (none on the laptop); the
overlay validates with `docker compose config` and the stack itself was smoke-tested here.

## LLM switch

`LLM_BACKEND=claude|ollama` in `config/trader.env`, with `LLM_BACKEND_CHAT` /
`LLM_BACKEND_REVIEW` to split them.

- **claude** (default outside Docker): `claude -p` on the subscription login, pinned to sonnet,
  isolated from the personal setup (no MCP connectors, global CLAUDE.md, memory, skills). Chat
  ~7.7k tokens of overhead per call (was ~25k); reviews are one tool-free structured call.
- **ollama:** in-process. Reviews use structured output. Chat gets exactly one tool
  (`run_tool`) behind a guard enforced in code: no `order.py` (trades only via proposals, always
  with a phone tap), no halt/resume, and a real `thesis_create` only for the exact spec that was
  dry-run in an earlier turn. Every call is journaled as `chat_tool`.
- **Measured quality:** `qwen3-coder:30b` (100.64.0.6) answers in 8–30 s and completes thesis
  creation, but retypes specs and invents small details; the guard catches the dangerous part.
  `qwen3.5` 9.7B (local fallback) takes 2–3 min per chat message and cannot do reviews (ignores
  the schema, timed out). Suggested: `LLM_BACKEND_CHAT=ollama` if you want zero Claude usage for
  chat, keep `LLM_BACKEND_REVIEW=claude` where a login exists; reviews are the judgment call.
- Reviews are skipped (no LLM call) when nothing material changed; routine DCA fills do not
  count; a >5% move, refusal, trigger, flagged news or manual trade does. `--force` overrides.

## Useful commands

```sh
deploy/switch.sh status                                 # which mode is live
.venv/bin/python scheduler/doctor.py                    # PASS/WARN/FAIL for everything
.venv/bin/python tools/positions.py                     # reconciled account, realized + unrealized
.venv/bin/python tools/journal.py --since 24h           # what happened and why
.venv/bin/python tools/halt.py on "reason"              # kill switch; only you turn it off
.venv/bin/python tools/usage.py --calls 5               # LLM spend by caller
.venv/bin/python tools/backtest.py --thesis semis-q4    # after tools/bars.py
systemctl --user list-timers 'thesis-*'                 # systemd mode
docker compose -f deploy/docker-compose.yml logs -f scheduler   # docker mode
```

## Known gaps / gotchas

- **Duplicate proposals:** a stop-loss trigger and a review can both propose the same sell on
  the same night (#6/#7). Fix: skip creating a proposal when an identical pending one exists.
- **Losses eat the cap:** thesis capital is buys minus sells, so a realized loss keeps using
  `capital_cap`. Fine for DCA; a rotating strategy slowly shrinks. Accepted for now.
- `run_strategy.py` (momentum → proposals) is not on any timer; `auto` mode is not built for it.
  Approve its sells before its buys.
- cron_loop runs jobs one at a time (a long nightly would delay a tick; they do not overlap in
  the current schedule). On its very first start nothing is replayed.
- `config/limits.json` must exist before `compose up` (a missing file becomes a directory).
- IEX quotes/bars are thin: limits anchor on the last trade; backtest prices are approximate.
- `per_order_cap` ($200) applies to sells, so a bigger position needs two orders.
- A refused DCA symbol is not retried in that slot. Stop-loss / take-profit only ever propose.
- Not done: `sudo scheduler/install.sh` (dedicated `thesis` user owning the keys). Less relevant
  once Docker is the server setup, where only the broker container gets the keys.
- Open from the brief: taxable vs IRA (wash-sale handling is warn-only).

## Future features

Quantitative (testable now with `tools/backtest.py`; each is a `strategies/<type>.py` module):

1. **Trend filter** — pause DCA while SPY is below its 200-day average. Small; no new data.
2. **Volatility sizing** — split each buy by inverse volatility so one name does not carry the risk.
3. **Dip-scaled DCA** — buy more when a name is well below its recent average, less when stretched.
4. **Momentum rotation** — built (`strategies/momentum.py`); needs a paper thesis and a timer.
5. **Mean reversion** — z-score entry/exit on a 20-day window. Watch the PDT counter.
6. **Strategy tournament** — several strategies on paper with small caps, one leaderboard vs SPY.
7. **Walk-forward check** — tune on one period, score on the next, flag edges that vanish.
8. **Drawdown guard** — at −15% from peak, propose halving the weekly amount.

Risk and information:

9. **Exposure view + sector/family caps** — "70% semis across three theses"; correlation between theses.
10. **Insider following** — SEC Form 4 cluster buys → proposal (public filings; data plumbing is the work).
11. **Earnings awareness** — no buys the week before earnings; review the morning after.
12. **Tax lots / wash-sale tracking** — needed before a taxable live account.

Product and plumbing:

13. **Duplicate-proposal guard** and proposal expiry digest (one push for everything pending).
14. **Catch-up policy per thesis** — "if a slot expired, buy it late anyway / skip / ask me".
15. **Backtest from chat** — "how would this have done since 2024?" before the read-back.
16. **Dashboard:** equity curve per thesis vs SPY, backtest results page, LLM spend chart.
17. **Local-model quality** — a small eval set of chat tasks (compile a thesis, answer a
    holdings question) scored per model, so a model swap is a measured decision.
18. **Parallel jobs in cron_loop**, DB backups (nightly copy of `trader.db`), and an uptime
    push ("no tick for 2 trading hours").
19. **Live trading switch** — only after the paper period: separate keys, lower caps, a
    checklist in `docs/`.

Suggested order: 13 → 1 → 2 → 6 → 18, then whatever the tournament says.
