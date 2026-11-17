# Handoff — updated 2026-10-17 (Sat) night

Read `README.md` for the layout and `docs/brief.md` for the design. This file is the state
of play and what to do next. Delete it when it stops being true.

## State right now

- **v1 scaffold is built, tested (165 tests) and committed.** Repo is portable: `setup.sh`,
  templated units, tokens file scrubbed from history. Safe to push to the server.
- **Running as user systemd units** (installed by `scheduler/install-user.sh`, lingering on):
  - `thesis-broker.service` — Alpaca **paper**, keys in `config/alpaca.env` (mode 600), localhost:8787
  - `thesis-web.service` — dashboard + chat, http://100.64.0.19:8010/?token=<WEB_TOKEN from config/trader.env>
  - `thesis-tick.timer` — every 15 min, 09:30–16:00 ET weekdays, runs due DCA slots
  - `thesis-nightly.timer` — 18:30 ET weekdays: reconcile, snapshot, news (Qwen), triggers
  - `thesis-first-order.timer` — one-shot: $5 SPY at **Fri 2026-10-16 09:35 ET** (step-0 proof)
- **Live thesis:** `semis-q4` — $50 every Monday 10:00 ET across NVDA/AMD/AVGO/TSM, cap $300,
  max $180/name, `mode: confirm`. First slot fires **Mon 2026-10-19 10:00 ET**.
- **ntfy topic:** in `config/trader.env` (`NTFY_TOPIC`). Pushes verified delivered to ntfy.sh.
- **Step 0 done (Fri Oct 16):** $5 SPY bought at 9:35 ET, filled @ 772.19. The fill was only
  pushed Saturday because nothing reconciled mid-day; fixed, every tick now reconciles open orders.
- **Paper account:** ~$104.5k cash, SPY 0.0064 sh in `manual`, PDT count 0.
- **Not done:** the sudo `scheduler/install.sh` (dedicated `thesis` user owning the keys).
- **Claude cost (Oct 15 evening):** chat and `tools/review.py` shell out to `claude -p` on the
  subscription login, no API key. They now pin `CLAUDE_MODEL` (default `sonnet`; the CLI's own
  default is fable) and log every call's tokens + `total_cost_usd` to `claude_usage`.
  `tools/usage.py` sums today/7d/30d; the dashboard shows the 7d figure. Set
  `CLAUDE_BUDGET_USD_WEEK` in `config/trader.env` to get a % of budget. The first review
  (Oct 15, 21:22 UTC, on fable, before the pin) is not in the table.
- **Review sessions can act:** the first real `review.py` run noticed the thesis was paused by the
  button test and created a resume proposal on its own (allowed by CLAUDE.md, but decide whether
  reviews should be read-only).

## Useful commands

```sh
systemctl --user list-timers 'thesis-*'                # what fires next
journalctl --user -u thesis-broker -n 50               # broker log (also thesis-tick, thesis-nightly, thesis-web)
.venv/bin/python tools/positions.py                    # reconciled account + positions by thesis
.venv/bin/python tools/journal.py --since 24h          # what happened and why
.venv/bin/python scheduler/tick.py                     # run a tick by hand (safe, idempotent)
.venv/bin/python scheduler/nightly.py                  # run the nightly job by hand
.venv/bin/python tools/halt.py on "reason"             # kill switch; `off` to resume
systemctl --user restart thesis-broker thesis-web      # after editing code or config
.venv/bin/python tools/usage.py --calls 5              # Claude spend by chat/review, vs CLAUDE_BUDGET_USD_WEEK
.venv/bin/python -m pytest -q                          # 147 tests, fake backend, no keys needed
```

## Monday 2026-10-19: autonomous live test

`tools/live_check.py` exercises the whole system against the paper account with $3 orders and
prints PASS/FAIL per check (exit 0 = all passed). Read the plan first:
`.venv/bin/python tools/live_check.py --phase all --dry-run`. Run the phases at these times (ET),
from the project root; a session can drive this with `/loop` or by hand:

| time | command | checks |
|---|---|---|
| 09:35 | `tools/live_check.py --phase open` | health, clock open, IEX quotes during hours, $3 AAPL buy fills within 120s, duplicate id, refusals (per_order_cap, crypto, short sell, HALT on/off) |
| 10:05 | `tools/live_check.py --phase slot --slot 2026-10-19T10:00` | the first DCA slot placed exactly 4 orders, idempotent re-run, ~$50 deployed, one `dca` journal row |
| ~11:00 | `tools/live_check.py --phase idle` | proposal → web approve → $2 SPY sell, `/api/state`, news digest, snapshot with `$BENCH`, triggers, review pack |
| 15:50 | `tools/live_check.py --phase close` | no stale open orders, PDT count consistent, every order matched by fill or refusal, HALT absent |

Expect on the phone: 4 refusal pushes, HALT on/off, one proposal push, and the fills. If a
phase fails, `tools/journal.py --since 1d --kind system` has the `live_check` rows with every
`client_order_id` the script placed. Also watch the 10:15 tick do nothing, and the 18:30
nightly run (news for 4 symbols, snapshot, triggers). Tuesday morning: `tools/usage.py` for
Claude spend, `tools/review.py --thesis semis-q4` for the first real review with positions.

## What to test, in order (older list)

1. ~~**Phone push**~~ done Oct 15. Approve button (pause proposal #1), a real `review.py` run,
   and Reject (proposal #3) were also exercised Oct 15 evening.
2. ~~**Fri 09:35 ET: first real paper order**~~ done; filled, see above.
3. **Proposal buttons.** During market hours run
   `tools/propose.py --thesis manual --action '{"type":"order","side":"sell","symbol":"SPY","notional":2}' --reason "button test"`
   and tap Approve on the phone. Expect a sell fill push. Reject works the same way.
4. **Chat from the dashboard.** Ask "what do we hold?". It runs Claude Code with CLAUDE.md
   in the project dir. First real exercise of `web/chat.py`; expect rough edges.
5. **Mon 10:00 ET: first DCA slot** — four fills of $12.50 pushed one by one. Then Mon 10:15
   tick should do nothing (idempotent). Mon 2026-10-26 is the second run = step 1 done.
6. **Catch-up.** Close the lid across a Monday 10:00 slot; it should execute on the next
   tick after wake (within 48h) and the journal should say so.
7. **Nightly** (18:30 ET): `journal.py --kind news` should show a Qwen digest per symbol;
   `snapshots` table gets a row per position plus `$ACCOUNT` and a SPY `$BENCH` row.
8. **Review**: `tools/review.py --thesis semis-q4 --pack-only --raw` to see the pack; drop
   `--pack-only` to run a real Claude review (writes `kind=review` to the journal, pushes).

## Next steps (build order)

- **Step 1 is in flight** (two unattended DCA buys: Oct 19 and Oct 26). Run paper 2–4 weeks; watch for wrong
  refusals, double pushes, missed slots after sleep.
- **Harden**: run `sudo scheduler/install.sh` so only the `thesis` user can read the keys,
  then `systemctl --user disable --now` the user units.
- **Step 2 polish**: chat UX, dashboard P&L vs SPY, pause/resume from the thesis page.
- **Step 3**: news triggers end-to-end (a `news` trigger in a thesis + a flagged story →
  review session → proposal), monthly review on the `review` cadence (`review.py --all`
  is wired but not scheduled yet; add a weekly timer that runs it).
- **Multi-strategy expansion:** see `docs/roadmap.md` (strategy block in the spec, `strategies/`
  plugins, backtester first; quant momentum → news → insider-following).
- **Open questions from the brief** still unanswered: taxable vs IRA (wash-sale tracking
  is warn-only today); `auto` for pure DCA (recommended) vs `confirm` everywhere.

## Test day — Thu 2026-10-22: PASSED

All four steps worked end to end on paper, every fill within 0.3% of the limit (new anchoring):

| ET | Step | Result |
|---|---|---|
| 09:42 | you in chat: "Buy 5 dollars of spy" | `order.py` placed 0.0065 SPY limit 768.93, filled 766.66, journaled by the 09:45 tick |
| 11:00 | scripted chat → proposal #4 | buy $10 AMD with a two-sentence thesis reason; approved in 31 s; filled 512.48 (limit 513.88); journaled 11:15 |
| 13:30 | scripted chat → proposal #5 | sell all 0.0129 SPY (both lots); approved in 13 s; filled 763.18 above the 760.94 floor; `pdt_log` row 1 |
| 15:30 | `review.py --thesis semis-q4` | HOLD, pushed |

Reminder pushes at 09:15/09:40/10:58/13:28/15:28/16:10 all fired (laptop woke 09:11). Claude spend
for the day: $0.40 on sonnet (3 chat calls + 1 review). `data/testday/` can be deleted or kept as
the template for the next scripted day.

Small things seen:
- Fill pushes arrive at the next 15-min tick, so 2–15 min after the fill. Fine for DCA; a
  faster path would be the broker polling its own open orders every minute.
- `positions.py` shows the closed manual SPY position as qty 0 with cost $0.08 and P&L −$0.08:
  that is the realized loss on the round trip leaking into the "unrealized" column. Closed
  positions should drop out and realized P&L be reported separately (nightly snapshot too).
- Alpaca's `daytrade_count` stayed 0 while our `pdt_log` has 1; our counter is the one the
  broker enforces, so this is harmless, but worth re-checking tomorrow after Alpaca's rollover.

## Known gaps / gotchas

- Alpaca IEX quotes are thin: on Mon 10-19 at 10:00 ET the ask was 6% above the tape on AMD
  and TSM, so "ask + 0.3%" limits were 6% above the fills. Since Tue 10-20 the buy limit is
  (1+s) × min(ask, last) and the sell limit (1−s) × max(bid, last) (`broker/limits.py`). After
  hours `ask: 0` with a last trade is now priced off the last trade; the market-closed check
  still refuses the order.
- `per_order_cap` ($200) applies to sells too, so a position above $200 needs two sell orders.
- A refused DCA symbol is not retried that slot (by design, to avoid a push every 15 min).
- Stop-loss / take-profit always produce proposals, never automatic sells, even with
  `review_only: false` (nightly runs after close anyway).
- Test pushes from this afternoon's build (a $20 SPY buy, kill-switch toggles) went to the
  real topic; they were against the fake backend. Real messages start 17:08 ET on Oct 15.
