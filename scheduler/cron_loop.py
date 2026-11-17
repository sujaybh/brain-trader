#!/usr/bin/env python3
"""Long-running scheduler for the Docker deployment: the systemd timers, without systemd.

Reproduces (America/New_York): tick Mon-Fri 09:30-16:00 every 15 min, nightly Mon-Fri 18:30,
weekly review Sat 10:00. Each job is the same command its systemd unit runs, as a subprocess
with output on stdout. Jobs run one at a time in a single loop, so a job never overlaps
itself; a failing job (non-zero exit, timeout, crash) is logged and the loop carries on.

Catch-up (like `Persistent=true`): the last run of every job is kept in settings as
`cron_last:<job>`. Whenever a job's most recent scheduled time is later than its last run,
it runs once, so a wake from sleep or a restart after downtime runs each missed job once, not
once per missed slot. The first ever start has no record: it is baselined to "now" and that is
recorded (nothing is replayed). A heartbeat (`cron_alive`) is written every minute for scheduler/doctor.py.

  .venv/bin/python scheduler/cron_loop.py             # run forever (SIGTERM = clean stop)
  .venv/bin/python scheduler/cron_loop.py --dry-run   # print schedule + what is due, run nothing
  .venv/bin/python scheduler/cron_loop.py --healthcheck  # exit 0 if the heartbeat is < 5 min old
  .venv/bin/python scheduler/cron_loop.py --once --job tick   # force one job now
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import argparse  # noqa: E402
import signal  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from croniter import croniter  # noqa: E402

TZ = ZoneInfo("America/New_York")
PY = sys.executable
HEARTBEAT_KEY = "cron_alive"
HEARTBEAT_EVERY_S = 60
HEARTBEAT_STALE_S = 300


@dataclass(frozen=True)
class Job:
    name: str
    crons: tuple[str, ...]      # all evaluated in America/New_York
    cmd: tuple[str, ...]
    timeout_s: int


# The systemd timers' OnCalendar expressions, as cron (see scheduler/README.md).
JOBS: tuple[Job, ...] = (
    Job("tick", ("30,45 9 * * 1-5", "*/15 10-15 * * 1-5", "0 16 * * 1-5"),
        (PY, str(ROOT / "scheduler" / "tick.py")), 10 * 60),
    Job("nightly", ("30 18 * * 1-5",), (PY, str(ROOT / "scheduler" / "nightly.py")), 2 * 3600),
    Job("review", ("0 10 * * 6",), (PY, str(ROOT / "tools" / "review.py"), "--all"), 2 * 3600),
)


# ---------------------------------------------------------------- pure schedule math

def _aware(now: datetime) -> datetime:
    return (now if now.tzinfo else now.replace(tzinfo=timezone.utc)).astimezone(TZ)


def previous_run(job: Job, now: datetime) -> datetime:
    """Most recent scheduled time <= now (tz-aware, New York)."""
    base = _aware(now).replace(microsecond=0) + timedelta(seconds=1)  # get_prev is strict
    return max(croniter(c, base).get_prev(datetime) for c in job.crons)


def next_run(job: Job, now: datetime) -> datetime:
    """First scheduled time strictly after now."""
    base = _aware(now).replace(microsecond=0)
    return min(croniter(c, base).get_next(datetime) for c in job.crons)


def due_jobs(now: datetime, last_run: dict[str, datetime | None], jobs=JOBS) -> list[Job]:
    """Jobs to run at `now`: those whose latest scheduled time is later than their last run
    (None = never ran = due). Running a job and recording its start time as the new last run
    clears it, so nothing runs twice and any number of missed slots collapse to one run."""
    out = []
    for j in jobs:
        last = last_run.get(j.name)
        if last is None or previous_run(j, now) > _aware(last):
            out.append(j)
    return out


def next_wakeup(now: datetime, jobs=JOBS) -> datetime:
    return min(next_run(j, now) for j in jobs)


# ---------------------------------------------------------------- state (settings table)

def _parse(iso: str | None) -> datetime | None:
    try:
        d = datetime.fromisoformat(iso) if iso else None
    except ValueError:
        return None
    return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)


def load_last_runs(conn, jobs=JOBS) -> dict[str, datetime | None]:
    """cron_last:<job>, or the older systemd-era job_ok:<job> if that is newer (a switch from
    systemd must not replay what the timers already ran)."""
    from trader import db
    out = {}
    for j in jobs:
        cands = [_parse(db.get_setting(conn, f"cron_last:{j.name}")), _parse(db.get_setting(conn, f"job_ok:{j.name}"))]
        cands = [c for c in cands if c]
        out[j.name] = max(cands) if cands else None
    return out


def record(job_name: str, when: datetime, log=print) -> None:
    from trader import db
    try:
        conn = db.connect()
        try:
            db.set_setting(conn, f"cron_last:{job_name}", when.astimezone(timezone.utc).isoformat(timespec="seconds"))
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        log(f"WARN could not record cron_last:{job_name}: {type(e).__name__}: {e}")


def heartbeat(log=print) -> None:
    from trader import db
    try:
        conn = db.connect()
        try:
            db.set_setting(conn, HEARTBEAT_KEY, datetime.now(timezone.utc).isoformat(timespec="seconds"))
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        log(f"WARN heartbeat failed: {type(e).__name__}: {e}")


def heartbeat_age_s() -> float | None:
    """Seconds since the loop last wrote its heartbeat (None: never / DB unreadable)."""
    from trader import db
    try:
        conn = db.connect_ro()
        try:
            hb = _parse(db.get_setting(conn, HEARTBEAT_KEY))
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None
    return (datetime.now(timezone.utc) - hb).total_seconds() if hb else None


# ---------------------------------------------------------------- loop

def log(msg: str) -> None:
    print(f"{datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S %Z')} cron_loop: {msg}", flush=True)


class Runner:
    def __init__(self):
        self.stop = threading.Event()
        self.proc: subprocess.Popen | None = None

    def request_stop(self, *_):
        self.stop.set()
        p = self.proc
        if p and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass

    def run_job(self, job: Job) -> int | None:
        """Run one job to completion (or timeout/stop). Never raises."""
        log(f"start {job.name}: {' '.join(Path(c).name if i == 0 else c for i, c in enumerate(job.cmd))}")
        t0 = datetime.now(timezone.utc)
        try:
            self.proc = subprocess.Popen(job.cmd, cwd=str(ROOT))
            try:
                rc = self.proc.wait(timeout=job.timeout_s)
            except subprocess.TimeoutExpired:
                log(f"ERROR {job.name} timed out after {job.timeout_s}s; killing")
                self.proc.kill()
                rc = self.proc.wait()
        except Exception as e:  # noqa: BLE001
            log(f"ERROR {job.name} could not run: {type(e).__name__}: {e}")
            return None
        finally:
            self.proc = None
        secs = (datetime.now(timezone.utc) - t0).total_seconds()
        log(f"{'ok' if rc == 0 else 'ERROR exit ' + str(rc)}: {job.name} finished in {secs:.0f}s")
        return rc


def wait_for_broker(runner: Runner, total_s: float) -> bool:
    from trader.client import BrokerClient
    c = BrokerClient(timeout=3)
    deadline = datetime.now().timestamp() + total_s
    while not runner.stop.is_set():
        ok, why = c.wait_ready(min(5, max(total_s, 0)), 1.0)
        if ok:
            log(f"broker ready at {c.url}")
            return True
        if datetime.now().timestamp() >= deadline:
            log(f"WARN broker not ready after {total_s:g}s ({why}); continuing, jobs wait for it themselves")
            return False
    return False


def describe_schedule(now: datetime, last: dict, jobs=JOBS) -> None:
    for j in jobs:
        lr = last.get(j.name)
        seen = f"{_aware(lr):%Y-%m-%d %H:%M %Z}" if lr else "unknown"
        log(f"{j.name}: next {next_run(j, now):%a %Y-%m-%d %H:%M %Z}; last run {seen}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="run what is due now, then exit")
    ap.add_argument("--dry-run", action="store_true", help="print the schedule and what is due; run nothing")
    ap.add_argument("--job", choices=[j.name for j in JOBS], help="with --once: run this job regardless of schedule")
    ap.add_argument("--healthcheck", action="store_true", help="exit 0 if the loop's heartbeat is fresh (container healthcheck)")
    ap.add_argument("--no-wait-broker", action="store_true", help="do not wait for the broker at start")
    a = ap.parse_args(argv)

    from trader import config, db
    if a.healthcheck:
        return 0 if heartbeat_age_s() is not None and heartbeat_age_s() < HEARTBEAT_STALE_S else 1
    runner = Runner()
    signal.signal(signal.SIGTERM, runner.request_stop)
    signal.signal(signal.SIGINT, runner.request_stop)

    started = datetime.now(timezone.utc)
    try:
        conn = db.connect()
        try:
            last = load_last_runs(conn)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        log(f"WARN cannot read the DB ({type(e).__name__}: {e}); starting with no history")
        last = {j.name: None for j in JOBS}
    # No record at all (first start): nothing to catch up on, count from now.
    seeded = [n for n, v in last.items() if v is None]
    for n in seeded:
        last[n] = started
        if not a.dry_run:
            record(n, started, log)   # persisted, so a restart before the first run still catches up
    log(f"started (TZ {TZ.key}); jobs: {', '.join(j.name for j in JOBS)}"
        + (f"; no history for {', '.join(seeded)}, counting from now" if seeded else ""))
    describe_schedule(started, last)

    if a.dry_run:
        now = datetime.now(timezone.utc)
        due = [j.name for j in due_jobs(now, last)] if not a.job else [a.job]
        log(f"dry-run: due now: {', '.join(due) or 'nothing'}; next wake {next_wakeup(now):%a %Y-%m-%d %H:%M %Z}")
        return 0

    if not a.no_wait_broker:
        wait_for_broker(runner, config.BROKER_WAIT_S)

    def run_due(forced: str | None = None) -> None:
        now = datetime.now(timezone.utc)
        todo = [j for j in JOBS if j.name == forced] if forced else due_jobs(now, last)
        for j in todo:
            if runner.stop.is_set():
                return
            when = datetime.now(timezone.utc)
            last[j.name] = when          # recorded at start: a slow job is not re-run for its own slot
            record(j.name, when, log)
            runner.run_job(j)

    if a.once:
        run_due(a.job)
        return 0

    next_beat = 0.0
    while not runner.stop.is_set():
        try:
            run_due()
            now = datetime.now(timezone.utc)
            if now.timestamp() >= next_beat:
                heartbeat(log)
                next_beat = now.timestamp() + HEARTBEAT_EVERY_S
            wake = next_wakeup(now)
            sleep_s = max(1.0, min((wake - now).total_seconds(), HEARTBEAT_EVERY_S))
        except Exception as e:  # noqa: BLE001  (the loop must never die)
            log(f"ERROR loop iteration failed: {type(e).__name__}: {e}")
            sleep_s = 30.0
        runner.stop.wait(sleep_s)
    log("stopping (signal received)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
