# scheduler/

Deterministic jobs that run without an LLM (plus the weekly Claude review). The same unit
templates in `systemd/` install either as **user units** (`install-user.sh`, no sudo; needs
lingering so they run at boot without a login) or as **system units** (`sudo install.sh`;
a `thesis` user alone holds the keys). Timers use `Persistent=true`, so a run missed while
the machine was off or asleep fires once on boot/wake.

## What runs when

| unit | when | runs as (system / user flavor) | what |
|---|---|---|---|
| `thesis-broker.service` | always (Restart=always) | `thesis` / you | `python -m broker` on 127.0.0.1:8787. Only process with the Alpaca keys. Counts as started only once `/health` answers (`ExecStartPost=wait_broker.py`). |
| `thesis-tick.timer` → `tick.py` | Mon–Fri 09:30, 09:45, 10:00…15:45 every 15 min, 16:00 (America/New_York) | owner / you | Every active thesis: execute due DCA slots (`tools/run_thesis.py` logic). |
| `thesis-nightly.timer` → `nightly.py` | Mon–Fri 18:30 America/New_York | owner / you | `/reconcile` → snapshot → `tools/news.py <universe> --since 24h` → `check_triggers`. A failing step is journaled (`kind=error`) and the next step still runs. |
| `thesis-review.timer` → `tools/review.py --all` | Sat 10:00 America/New_York | owner / you | Claude review of every active thesis whose `review` cadence (weekly/monthly/quarterly) is due. |
| `thesis-web.service` | always | owner / you | `python -m web` (dashboard + chat over Tailscale). |

Timer expressions (validated with `systemd-analyze calendar` on systemd 255; one range from
09:30 to 16:00 can't be written as a single expression):

```
OnCalendar=Mon..Fri *-*-* 09:30,45:00 America/New_York
OnCalendar=Mon..Fri *-*-* 10..15:00/15:00 America/New_York
OnCalendar=Mon..Fri *-*-* 16:00:00 America/New_York
OnCalendar=Mon..Fri *-*-* 18:30:00 America/New_York      # nightly
OnCalendar=Sat *-*-* 10:00:00 America/New_York            # review
```

The tick and nightly jobs only talk to the broker over HTTP. They share `data/trader.db` with the
broker through the `thesis` group (`data/` is setgid `thesis`, mode 2775; services use `UMask=0002`).

## How catch-up works

A thesis slot is one cron occurrence of `schedule.cron` (in `schedule.tz`). On every tick:

1. Slots in the last `catchup_hours` (`config/limits.json`, 48h) that are not finished are
   **pending**. Slots from before the thesis was created, or from before its last resume,
   are ignored (paused theses don't catch up).
2. If any slot is pending, the tick asks the broker `/clock`. If the market is closed it stops;
   the slots stay pending for the next tick. No pending slots → no broker call at all.
3. Each pending slot is executed oldest first: allocation from the thesis's deployed capital,
   one order per symbol with `client_order_id(thesis, symbol, slot)`. Ids already in `orders`
   are skipped, so re-running is always safe. A refusal is recorded and pushed; the other symbols
   still go. A fully deployed thesis journals `skipped` for that slot.
4. A slot that leaves the 48h window without having run is journaled `expired` once
   (tracked with the `last_tick:<thesis>` setting) and never attempted.

Finished slots are remembered in `settings` (`slot_done:<thesis>:<slot>`).

## Running things by hand

```bash
cd /path/to/tracker
.venv/bin/python scheduler/tick.py --dry-run          # what would run now
.venv/bin/python scheduler/tick.py                    # run it for real
.venv/bin/python tools/run_thesis.py semis-q4 --dry-run
.venv/bin/python tools/run_thesis.py semis-q4 --now 2026-10-19T10:05   # naive = New York time
systemctl --user start thesis-tick.service           # same as the timer would (sudo systemctl ... for system units)
journalctl --user -u thesis-tick -n 50
systemctl --user list-timers 'thesis-*'
.venv/bin/python scheduler/wait_broker.py --timeout 5 # is the broker answering?
.venv/bin/python scheduler/doctor.py                  # everything above, read-only
```

## Boot and wake: what waits for what

At boot the Persistent timers fire immediately, often before the broker or Ollama listen
(Nov 15, 2026: reconcile/snapshot/triggers failed with "Connection refused" and news recorded
"no digest" for every symbol). Three layers stop that:

1. **Broker readiness.** `thesis-broker.service` is `Type=simple`, which counts as started at
   fork. Its `ExecStartPost=scheduler/wait_broker.py --timeout 60` keeps the unit "activating"
   until `GET /health` answers, so `After=thesis-broker.service` in the job units now means
   "after it listens". If it never answers the start fails and `Restart=always` retries.
   Works identically for user and system units (no `Type=notify`/sd_notify needed).
2. **Jobs wait too** (in case the broker restarts mid-run, or the units were not reinstalled):
   tick (only when it actually needs the broker), nightly and `review.py --all` poll `/health`
   for up to `BROKER_WAIT_S` (default 120 s, `config/trader.env`). If it never comes up they
   journal **one** `kind=error` row ("tick: broker not reachable after 120s ...") instead of
   one per step. Nightly still runs the news step; tick leaves slots pending (caught up later);
   review leaves `last_review` untouched so the next run reviews them.
3. **Client retries.** `trader/client.py` retries a *connection* error twice (0.5 s, 1.5 s)
   for GETs and `POST /reconcile`, and for `POST /orders` only when a `client_order_id` is
   given: the broker dedups on that id under its order lock (existing row → `"duplicate": true`;
   a failed upstream submit deletes the row), so a repeat cannot double-buy. Orders without an
   id (the broker derives one from the current second) and cancels are never retried; read
   timeouts never are. Interactive tools therefore wait at most ~2 s extra.

Ollama: `tools/news.py` polls `/api/tags` on the primary and fallback for up to
`OLLAMA_WAIT_S` (60 s). If every symbol that had headlines fails its digest (or every feed
fails), news.py exits 1 with a reason and writes no `kind=news` row; the nightly journals it
as `nightly step news failed: ...`. A partial failure is still a normal news row. System units
also order the nightly `After=ollama.service` (ignored if that unit does not exist).

Every clean tick/nightly writes `job_ok:tick` / `job_ok:nightly` in `settings`, and the
nightly journals one `kind=system` row ("nightly ok" or which steps failed).

## After the machine was off or asleep

Nothing to do in most cases. On boot/wake, systemd fires each timer once if it missed a run
(`Persistent=true`), and the tick picks up any slot from the last 48h:

- Woke during market hours → the missed slot executes on that tick.
- Woke outside market hours → the tick sees the market closed and does nothing; the slot runs
  on the first tick after the next open (the timer fires 09:30 on the next weekday).
- Off more than 48h past a slot → the slot is journaled `expired` **and pushed once**
  ("Slot expired: <thesis>"); check with `tools/journal.py --kind expired`. Place it by hand
  with `tools/order.py` if you want it.
- The nightly job missed → it runs once on wake; snapshots are idempotent per date.
- The Saturday review missed → it runs once on boot; cadence is tracked per thesis.

Check: `.venv/bin/python scheduler/doctor.py` (below), `systemctl --user list-timers 'thesis-*'`
(drop `--user` for system units) and `tools/journal.py --since 24h`.

## Health check: `doctor.py`

```bash
.venv/bin/python scheduler/doctor.py              # JSON, one PASS/WARN/FAIL/INFO per check; exit 1 on any FAIL
.venv/bin/python scheduler/doctor.py --mode system
```

Read-only. It renders the templates (both flavors) into a temp dir and runs
`systemd-analyze verify`, runs `systemd-analyze calendar` on every `OnCalendar`, checks the
units are installed (and match the current templates), enabled and active, timers' next/last
fire and last result, lingering (user flavor), broker `/health` (paper? halted?), Ollama
primary/fallback and their models, that `claude` resolves with a systemd PATH, `data/`
writable, HALT, the last successful tick and nightly, `kind=error` rows in 7 days and expired
slots in 14 days (DB opened `mode=ro`). It cannot tell whether a second machine is also
running the jobs; that is listed as INFO.

## The `claude` binary under systemd

Units get a minimal PATH (no `~/.local/bin`, no nvm). `web/chat.py:claude_bin()` resolves, in
order: `CLAUDE_BIN` (env or `config/trader.env`, `~` expanded), `~/.local/bin/claude` (native
installer), `~/.claude/local/claude`, `PATH`, `/usr/local/bin/claude`,
`~/.npm-global/bin/claude`. `thesis-review.service` also sets
`PATH=<owner home>/.local/bin:/usr/local/bin:/usr/bin:/bin` (for an npm install that needs
`node`). On a new machine set `CLAUDE_BIN` and run `claude` once interactively as the owner so
it is logged in; `doctor.py` checks the binary (it never runs it).

## Moving to an always-on server

1. **Pick the install path.** Dedicated server: `sudo scheduler/install.sh` (system units;
   only the `thesis` user can read the keys in `/etc/thesis-trader/alpaca.env`). Single-user
   box where you accept your user reading the keys: `scheduler/install-user.sh` plus
   lingering. Never both (two brokers, two schedulers); both installers warn and doctor FAILs.
2. **Lingering (user units only):** `sudo loginctl enable-linger $USER`, or user units start
   only at login and stop at logout. The installer tries it; doctor checks it.
3. **Timezone:** every `OnCalendar` names `America/New_York`, so the server clock can stay UTC.
   It needs tzdata (`/usr/share/zoneinfo/America/New_York`); doctor's calendar checks prove it.
4. **Ollama:** set `OLLAMA_URL` / `OLLAMA_FALLBACK_URL` in `config/trader.env`. If Ollama runs
   on the server (`ollama.service`), use `http://127.0.0.1:11434`; system units order the
   nightly after it. If it lives on another box over Tailscale, that box must be up at 18:30
   ET or the news step fails (as an error, not a silent "no digest").
5. **Web over Tailscale:** set `WEB_HOST` to the server's Tailscale IP (`tailscale ip -4`) so
   the dashboard is not exposed on other interfaces, and `WEB_URL=http://<that ip>:8010`
   (used in push links and Approve/Reject buttons). Until tailscaled is up the bind fails and
   `Restart=always` retries every 5 s. Re-add the dashboard to the phone home screen.
6. **Claude:** install the CLI as the owner, log in once, set `CLAUDE_BIN` (see above).
7. **Stop the laptop first**, so the two machines never trade at the same time (separate DBs
   mean separate `slot_done` memory: both would attempt every DCA slot):
   ```bash
   # on the laptop
   .venv/bin/python tools/halt.py on "moved to server"        # belt and braces for its copy
   scheduler/uninstall.sh --user                               # or: sudo scheduler/uninstall.sh
   ```
8. **Migrate the DB** (theses, orders, journal, settings incl. `slot_done:*`), after step 7:
   ```bash
   sqlite3 data/trader.db ".backup /tmp/trader.db"             # consistent copy, WAL included
   scp /tmp/trader.db server:tracker/data/trader.db
   ```
   Also copy `config/trader.env` (same `NTFY_TOPIC` / `WEB_TOKEN` keep the phone working; set
   the new `WEB_URL`/`WEB_HOST`) and `config/limits.json`; put the paper keys in
   `config/alpaca.env` or `/etc/thesis-trader/alpaca.env`. Do **not** copy `data/HALT`.
   For the system flavor re-run `sudo scheduler/install.sh` after copying so `data/` gets the
   `thesis` group again.
9. **Install and check:** run the installer, then `.venv/bin/python scheduler/doctor.py`
   until it has no FAIL, and `tools/positions.py` to see reconciled Alpaca data.

## Install / uninstall

```bash
scheduler/install-user.sh      # user units; idempotent; restarts broker/web only if their unit changed
sudo scheduler/install.sh      # system units; idempotent; read its header for exactly what it changes
scheduler/uninstall.sh --user  # remove user units
sudo scheduler/uninstall.sh    # remove system units; keeps data/ and keys (--purge-user drops the user)
```

Both installers render every template in `systemd/` with `render-units.sh` (`__ROOT__` = the
checkout, `__HOME__` = the owner's home). The user flavor drops `User=`/hardening lines and
`network-online.target` / `ollama.service`, which the user manager does not have. Re-run the
installer after pulling changes to `systemd/`.

## Without systemd: Docker

`scheduler/cron_loop.py` is a long-running replacement for the three timers (tick, nightly, weekly
review, America/New_York), used by the Docker deployment in `deploy/` (see `deploy/README.md`). It runs
the same commands as the units, one at a time, logs to stdout, records `cron_last:<job>` in `settings`
and catches up once after downtime. `--dry-run` shows the schedule, `--once [--job tick]` runs what is
due and exits. `doctor.py` auto-detects the compose project (docker mode) and FAILs if systemd units
are also active.
