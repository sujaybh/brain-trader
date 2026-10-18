# scheduler/

Deterministic jobs that run without an LLM. All units are **system-level** (this laptop has
lingering off, so user units would die at logout) and the timers use `Persistent=true` so a
run missed during lid-close suspend fires on wake.

## What runs when

| unit | when | runs as | what |
|---|---|---|---|
| `thesis-broker.service` | always (Restart=always) | `thesis` | `python -m broker` on 127.0.0.1:8787. Only process with the Alpaca keys (`/etc/thesis-trader/alpaca.env`, mode 600). |
| `thesis-tick.timer` → `tick.py` | Mon–Fri 09:30, 09:45, 10:00…15:45 every 15 min, 16:00 (America/New_York) | `anomander` | Every active thesis: execute due DCA slots (`tools/run_thesis.py` logic). |
| `thesis-nightly.timer` → `nightly.py` | Mon–Fri 18:30 America/New_York | `anomander` | `/reconcile` → snapshot → `tools/news.py <universe> --since 24h` → `check_triggers`. A failing step is journaled (`kind=error`) and the next step still runs. |
| `thesis-web.service` | always | `anomander` | `python -m web` (dashboard + chat over Tailscale). |

Timer expressions (validated with `systemd-analyze calendar` on systemd 255; one range from
09:30 to 16:00 can't be written as a single expression):

```
OnCalendar=Mon..Fri *-*-* 09:30,45:00 America/New_York
OnCalendar=Mon..Fri *-*-* 10..15:00/15:00 America/New_York
OnCalendar=Mon..Fri *-*-* 16:00:00 America/New_York
OnCalendar=Mon..Fri *-*-* 18:30:00 America/New_York      # nightly
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
sudo systemctl start thesis-tick.service              # same as the timer would
journalctl -u thesis-tick -n 50
systemctl list-timers 'thesis-*'
```

## After the laptop was asleep

Nothing to do in most cases. On wake, systemd fires each timer once if it missed a run
(`Persistent=true`), and the tick picks up any slot from the last 48h:

- Woke during market hours → the missed slot executes on that tick.
- Woke outside market hours → the tick sees the market closed and does nothing; the slot runs
  on the first tick after the next open (the timer fires 09:30 on the next weekday).
- Asleep more than 48h past a slot → the slot is journaled `expired`; check with
  `tools/journal.py --kind expired`. Place it by hand with `tools/order.py` if you want it.
- The nightly job missed → it runs once on wake; snapshots are idempotent per date.

Check: `systemctl list-timers 'thesis-*'` (LAST/NEXT columns) and
`tools/journal.py --since 24h`.

## Install / uninstall

```bash
sudo scheduler/install.sh     # idempotent; read its header for exactly what it changes
sudo scheduler/uninstall.sh   # removes units; keeps data/ and keys (add --purge-user to drop the user)
```
