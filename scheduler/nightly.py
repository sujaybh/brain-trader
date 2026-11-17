#!/usr/bin/env python3
"""Nightly job (weekdays 18:30 America/New_York): reconcile -> snapshot -> news digest ->
trigger check -> daily bars. Each step's failure is journaled (kind=error) and the next step still runs."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402
import json  # noqa: E402
import traceback  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

from tools import _common as C  # noqa: E402
from tools.check_triggers import call_tool, check_triggers  # noqa: E402
from tools.snapshot import take_snapshot  # noqa: E402
from trader import db  # noqa: E402
from trader.client import wait_for_broker  # noqa: E402


def nightly(conn=None, client=None, wait_s: float | None = None) -> dict:
    conn = conn or C.connect()
    client = client or C.broker()
    steps = []
    # At boot the Persistent timer can fire before the broker listens: wait (bounded) once.
    # If it never answers, ONE error is journaled and the broker steps are skipped; news
    # (no broker needed) still runs.
    broker_ok, broker_why = wait_for_broker("nightly", conn=conn, client=client, timeout=wait_s)

    def step(name, fn):
        try:
            res = fn()
            steps.append({"step": name, "ok": True, "result": res})
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            msg = getattr(e, "reason", None) or f"{type(e).__name__}: {e}"
            steps.append({"step": name, "ok": False, "error": msg})
            try:
                db.journal(conn, "error", f"nightly step {name} failed: {msg}", data={"step": name})
            except Exception:  # noqa: BLE001
                pass

    def bstep(name, fn):
        """A step that needs the broker: skipped (not re-journaled) when it never came up."""
        if broker_ok:
            step(name, fn)
        else:
            steps.append({"step": name, "ok": False, "error": f"skipped: {broker_why}"})

    def news():
        syms = sorted({s for r in C.list_theses(conn, ("active", "paused")) if r["id"] != "manual"
                       for s in C.load_thesis(conn, r["id"])[1].universe})
        if not syms:
            return {"ran": False, "reason": "no symbols"}
        res = call_tool("news.py", [*syms, "--since", "24h"], timeout=1800)
        if res.get("ran") and res.get("exit") not in (0, None):
            o = res.get("output")
            why = o.get("reason") if isinstance(o, dict) and o.get("reason") else str(o)
            raise RuntimeError(f"news.py exited {res['exit']}: {why[:300]}")
        return res

    bstep("reconcile", lambda: {"updated": (client.reconcile() or {}).get("updated")})
    bstep("snapshot", lambda: take_snapshot(conn, client))
    step("news", news)
    bstep("check_triggers", lambda: check_triggers(conn=conn, client=client))
    bstep("bars", lambda: _bars_step(conn, client))
    ok = all(s["ok"] for s in steps)
    _record_run(conn, ok, steps)
    return {"ok": ok, "steps": steps}


def _record_run(conn, ok: bool, steps: list[dict]) -> None:
    """One kind=system row per run, and job_ok:nightly on success (scheduler/doctor.py reads both)."""
    try:
        failed = [s["step"] for s in steps if not s["ok"]]
        db.journal(conn, "system", "nightly ok" if ok else f"nightly finished; failed: {', '.join(failed)}",
                   data={"job": "nightly", "ok": ok, "failed": failed})
        if ok:
            db.set_setting(conn, "job_ok:nightly", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    except Exception:  # noqa: BLE001
        pass


def _bars_step(conn, client) -> dict:
    """Daily bars for every thesis universe + SPY (tools/bars.py), for strategies and backtests."""
    from tools.bars import update_bars
    res = update_bars(conn, client)
    if res["errors"]:
        raise RuntimeError("; ".join(f"{e['symbol']}: {e['reason']}" for e in res["errors"]))
    return {s: v.get("last") for s, v in res["symbols"].items()}


def main(argv=None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    try:
        out = nightly()
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        print(json.dumps({"ok": False, "reason": f"{type(e).__name__}: {e}"}, indent=2))
        return 2
    print(json.dumps(out, indent=2, default=str))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
