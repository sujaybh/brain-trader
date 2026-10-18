# Handoff — 2026-10-15 evening

Read `README.md` for the layout and `docs/brief.md` for the design. This file is the state
of play and what to do next. Delete it when it stops being true.

## State right now

- **v1 scaffold is built, tested (147 tests) and committed.**
- **Running as user systemd units** (installed by `scheduler/install-user.sh`, lingering on):
  - `thesis-broker.service` — Alpaca **paper**, keys in `config/alpaca.env` (mode 600), localhost:8787
  - `thesis-web.service` — dashboard + chat, http://100.64.0.19:8010/?token=<WEB_TOKEN from config/trader.env>
  - `thesis-tick.timer` — every 15 min, 09:30–16:00 ET weekdays, runs due DCA slots
  - `thesis-nightly.timer` — 18:30 ET weekdays: reconcile, snapshot, news (Qwen), triggers
  - `thesis-first-order.timer` — one-shot: $5 SPY at **Fri 2026-10-16 09:35 ET** (step-0 proof)
- **Live thesis:** `semis-q4` — $50 every Monday 10:00 ET across NVDA/AMD/AVGO/TSM, cap $300,
  max $180/name, `mode: confirm`. First slot fires **Mon 2026-10-19 10:00 ET**.
- **ntfy topic:** in `config/trader.env` (`NTFY_TOPIC`). Pushes verified delivered to ntfy.sh.
- **Paper account at handoff:** ~$104.5k cash, zero positions, PDT count 0.
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

## What to test, in order

1. ~~**Phone push**~~ done Oct 15. Approve button (pause proposal #1), a real `review.py` run,
   and Reject (proposal #3) were also exercised Oct 15 evening.
2. **Fri 09:35 ET: first real paper order.** Expect a push "Bought $5.00 SPY @ ... (manual)".
   Check: `tools/positions.py` shows SPY under `manual`; `journal.py` shows order + fill;
   Alpaca's paper dashboard shows the same fill. If the order sits `submitted`, the next
   tick's reconcile picks the fill up; if it's refused, the push says which check.
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

- **Step 1 is in flight** (two unattended DCA buys). Run paper 2–4 weeks; watch for wrong
  refusals, double pushes, missed slots after sleep.
- **Harden**: run `sudo scheduler/install.sh` so only the `thesis` user can read the keys,
  then `systemctl --user disable --now` the user units.
- **Step 2 polish**: chat UX, dashboard P&L vs SPY, pause/resume from the thesis page.
- **Step 3**: news triggers end-to-end (a `news` trigger in a thesis + a flagged story →
  review session → proposal), monthly review on the `review` cadence (`review.py --all`
  is wired but not scheduled yet; add a weekly timer that runs it).
- **Open questions from the brief** still unanswered: taxable vs IRA (wash-sale tracking
  is warn-only today); `auto` for pure DCA (recommended) vs `confirm` everywhere.

## Known gaps / gotchas

- Alpaca IEX quotes after hours can carry `ask: 0`; the broker refuses those as "no usable
  quote" rather than mispricing. During hours this hasn't been observed yet.
- `per_order_cap` ($200) applies to sells too, so a position above $200 needs two sell orders.
- A refused DCA symbol is not retried that slot (by design, to avoid a push every 15 min).
- Stop-loss / take-profit always produce proposals, never automatic sells, even with
  `review_only: false` (nightly runs after close anyway).
- Test pushes from this afternoon's build (a $20 SPY buy, kill-switch toggles) went to the
  real topic; they were against the fake backend. Real messages start 17:08 ET on Oct 15.
