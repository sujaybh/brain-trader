"""scheduler/cron_loop.py: pure schedule math (DST, weekends, catch-up, no double run), state
keys, the loop's job runner and --once/--dry-run. No sleeping, no real jobs."""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scheduler"))

import cron_loop as cl  # noqa: E402
from trader import db  # noqa: E402

NY = ZoneInfo("America/New_York")
J = {j.name: j for j in cl.JOBS}


def ny(y, m, d, hh=0, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=NY)


def names(now, last):
    return [j.name for j in cl.due_jobs(now, last)]


def test_tick_slots_weekday():
    # Mon 2026-11-16
    assert cl.next_run(J["tick"], ny(2026, 11, 16, 9, 0)) == ny(2026, 11, 16, 9, 30)
    assert cl.next_run(J["tick"], ny(2026, 11, 16, 9, 30)) == ny(2026, 11, 16, 9, 45)
    assert cl.next_run(J["tick"], ny(2026, 11, 16, 9, 46)) == ny(2026, 11, 16, 10, 0)
    assert cl.next_run(J["tick"], ny(2026, 11, 16, 15, 46)) == ny(2026, 11, 16, 16, 0)
    assert cl.next_run(J["tick"], ny(2026, 11, 16, 16, 0)) == ny(2026, 11, 17, 9, 30)
    # the full day: 09:30, 09:45, 10:00..15:45 (24 slots) and 16:00
    t, n = ny(2026, 11, 16, 0, 0), 0
    while (t := cl.next_run(J["tick"], t)).day == 16:
        n += 1
    assert n == 2 + 24 + 1


def test_previous_run_is_inclusive_of_now():
    assert cl.previous_run(J["tick"], ny(2026, 11, 16, 10, 0)) == ny(2026, 11, 16, 10, 0)
    assert cl.previous_run(J["tick"], ny(2026, 11, 16, 10, 0, 59)) == ny(2026, 11, 16, 10, 0)


def test_weekend_and_review():
    sat = ny(2026, 11, 21, 9, 0)
    assert cl.next_run(J["tick"], sat) == ny(2026, 11, 23, 9, 30)
    assert cl.next_run(J["nightly"], sat) == ny(2026, 11, 23, 18, 30)
    assert cl.next_run(J["review"], sat) == ny(2026, 11, 21, 10, 0)
    assert cl.next_run(J["review"], ny(2026, 11, 21, 10, 0)) == ny(2026, 11, 28, 10, 0)
    # Friday evening: next tick is Monday morning
    assert cl.next_run(J["tick"], ny(2026, 11, 20, 16, 1)) == ny(2026, 11, 23, 9, 30)


def test_due_when_slot_passed_and_no_double_run():
    last = {"tick": ny(2026, 11, 16, 9, 50), "nightly": ny(2026, 11, 13, 18, 31), "review": ny(2026, 11, 14, 10, 1)}
    assert names(ny(2026, 11, 16, 9, 59), last) == []
    assert names(ny(2026, 11, 16, 10, 0), last) == ["tick"]
    last["tick"] = ny(2026, 11, 16, 10, 0, 2)         # ran
    assert names(ny(2026, 11, 16, 10, 0, 30), last) == []
    assert names(ny(2026, 11, 16, 10, 14, 59), last) == []
    assert names(ny(2026, 11, 16, 10, 15), last) == ["tick"]


def test_catch_up_after_downtime_runs_each_job_once():
    last = {"tick": ny(2026, 11, 16, 12, 5), "nightly": ny(2026, 11, 16, 18, 31), "review": ny(2026, 11, 14, 10, 1)}
    # Down from Mon 12:05 to Wed 11:00: many tick slots, one nightly (Tue), review not yet due
    now = ny(2026, 11, 18, 11, 0)
    assert names(now, last) == ["tick", "nightly"]
    last["tick"] = last["nightly"] = now
    assert names(now + timedelta(seconds=5), last) == []
    # Weekend: the weekday jobs ran Fri evening, so only the Sat 10:00 review slot is missed
    last["tick"], last["nightly"] = ny(2026, 11, 20, 18, 31), ny(2026, 11, 20, 18, 31)
    assert names(ny(2026, 11, 21, 12, 0), last) == ["review"]


def test_none_means_never_ran():
    assert names(ny(2026, 11, 16, 12, 0), {}) == ["tick", "nightly", "review"]


def test_utc_input():
    last = {"tick": datetime(2026, 11, 16, 14, 50, tzinfo=timezone.utc), "nightly": ny(2026, 11, 16, 18, 31),
            "review": ny(2026, 11, 14, 10, 1)}
    now_utc = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)   # 10:00 EST
    assert names(now_utc, last) == ["tick"]


def test_dst_spring_forward():
    # 2026-03-08 is a Sunday: 02:00 EST -> 03:00 EDT. Weekday times keep their wall-clock hour.
    nxt = cl.next_run(J["tick"], ny(2026, 3, 6, 19, 0))
    assert nxt == ny(2026, 3, 9, 9, 30) and nxt.utcoffset() == timedelta(hours=-4)
    # Friday 18:30 EST nightly vs Monday 18:30 EDT: 71 hours apart in UTC, not 72
    a = cl.next_run(J["nightly"], ny(2026, 3, 6, 18, 31))
    assert a == ny(2026, 3, 9, 18, 30)
    assert a.astimezone(timezone.utc) - ny(2026, 3, 6, 18, 30).astimezone(timezone.utc) == timedelta(hours=71)
    last = {"tick": ny(2026, 3, 6, 16, 0, 5), "nightly": ny(2026, 3, 6, 18, 30, 5), "review": ny(2026, 3, 7, 10, 0, 5)}
    assert names(ny(2026, 3, 8, 12, 0), last) == []
    assert names(ny(2026, 3, 9, 9, 30), last) == ["tick"]


def test_dst_fall_back():
    # 2026-11-01 (Sunday) 02:00 EDT -> 01:00 EST. Sat review stays 10:00 local on both sides.
    a = cl.next_run(J["review"], ny(2026, 10, 24, 10, 1))
    b = cl.next_run(J["review"], ny(2026, 10, 31, 10, 1))
    assert a == ny(2026, 10, 31, 10, 0) and a.utcoffset() == timedelta(hours=-4)
    assert b == ny(2026, 11, 7, 10, 0) and b.utcoffset() == timedelta(hours=-5)
    assert b.astimezone(timezone.utc) - a.astimezone(timezone.utc) == timedelta(days=7, hours=1)
    t = cl.next_run(J["tick"], ny(2026, 11, 2, 15, 50))   # 16:00 EST Monday = 21:00 UTC
    assert t.astimezone(timezone.utc).hour == 21


def test_next_wakeup_is_earliest():
    assert cl.next_wakeup(ny(2026, 11, 16, 17, 0)) == ny(2026, 11, 16, 18, 30)
    assert cl.next_wakeup(ny(2026, 11, 20, 19, 0)) == ny(2026, 11, 21, 10, 0)


# ---------------------------------------------------------------- state + loop

def test_load_last_runs_takes_newest_of_cron_last_and_job_ok():
    conn = db.connect()
    assert cl.load_last_runs(conn) == {"tick": None, "nightly": None, "review": None}
    db.set_setting(conn, "job_ok:tick", "2026-11-16T14:00:00+00:00")
    db.set_setting(conn, "cron_last:tick", "2026-11-16T13:00:00+00:00")
    db.set_setting(conn, "cron_last:nightly", "2026-11-13T23:31:00+00:00")
    got = cl.load_last_runs(conn)
    assert got["tick"] == datetime(2026, 11, 16, 14, 0, tzinfo=timezone.utc)
    assert got["nightly"] == datetime(2026, 11, 13, 23, 31, tzinfo=timezone.utc)
    conn.close()


def test_record_and_heartbeat_write_settings():
    cl.record("tick", datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc))
    cl.heartbeat()
    conn = db.connect()
    assert db.get_setting(conn, "cron_last:tick") == "2026-11-16T15:00:00+00:00"
    assert db.get_setting(conn, cl.HEARTBEAT_KEY)
    conn.close()


def test_run_job_survives_failures_and_timeouts(capsys):
    r = cl.Runner()
    ok = cl.Job("x", ("* * * * *",), (sys.executable, "-c", "print('hello from job')"), 30)
    bad = cl.Job("y", ("* * * * *",), (sys.executable, "-c", "import sys; sys.exit(3)"), 30)
    slow = cl.Job("z", ("* * * * *",), (sys.executable, "-c", "import time; time.sleep(30)"), 1)
    missing = cl.Job("m", ("* * * * *",), ("/nonexistent/binary",), 5)
    assert r.run_job(ok) == 0
    assert r.run_job(bad) == 3
    assert r.run_job(slow) is not None and r.proc is None
    assert r.run_job(missing) is None
    out = capsys.readouterr().out
    assert "hello from job" in out and "ERROR exit 3" in out and "timed out" in out and "could not run" in out


def test_once_runs_only_due_jobs_and_records(monkeypatch):
    now = datetime.now(timezone.utc)
    conn = db.connect()
    for j in ("tick", "nightly", "review"):
        db.set_setting(conn, f"cron_last:{j}", now.isoformat(timespec="seconds"))
    conn.close()
    ran = []
    monkeypatch.setattr(cl.Runner, "run_job", lambda self, job: ran.append(job.name) or 0)
    assert cl.main(["--once", "--no-wait-broker"]) == 0
    assert ran == []                                   # everything ran "just now"
    assert cl.main(["--once", "--no-wait-broker", "--job", "nightly"]) == 0
    assert ran == ["nightly"]                          # forced
    conn = db.connect()
    db.set_setting(conn, "cron_last:review", "2020-01-01T00:00:00+00:00")   # stale: a Saturday slot is missed
    conn.close()
    ran.clear()
    assert cl.main(["--once", "--no-wait-broker"]) == 0
    assert ran == ["review"]                           # caught up once
    assert cl.main(["--once", "--no-wait-broker"]) == 0
    assert ran == ["review"]                           # recorded: not due again


def test_dry_run_runs_nothing(monkeypatch, capsys):
    monkeypatch.setattr(cl.Runner, "run_job", lambda self, job: pytest.fail("dry-run ran a job"))
    assert cl.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "tick: next" in out and "nightly: next" in out and "review: next" in out and "dry-run" in out


def test_commands_match_the_systemd_units():
    for unit, job in (("thesis-tick.service", "tick"), ("thesis-review.service", "review")):
        text = (ROOT / "scheduler/systemd" / unit).read_text()
        script = Path(J[job].cmd[1]).relative_to(ROOT).as_posix()
        assert f"__ROOT__/{script}" in text
    assert Path(J["nightly"].cmd[1]) == ROOT / "scheduler" / "nightly.py"


def test_sigterm_stops_the_real_loop(tmp_path):
    import os
    import signal
    import time
    env = {**os.environ, "TRADER_DATA_DIR": str(tmp_path)}
    p = subprocess.Popen([sys.executable, str(ROOT / "scheduler/cron_loop.py"), "--no-wait-broker"], env=env,
                         stdout=subprocess.PIPE, text=True, cwd=ROOT)
    try:
        assert "started" in p.stdout.readline()
        time.sleep(1.0)
        p.send_signal(signal.SIGTERM)
        assert p.wait(timeout=10) == 0
        assert "stopping" in p.stdout.read()
    finally:
        if p.poll() is None:
            p.kill()
