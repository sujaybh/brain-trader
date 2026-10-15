#!/usr/bin/env python3
"""Nightly job (weekdays 18:30 America/New_York): reconcile -> snapshot -> news digest ->
trigger check. Each step's failure is journaled (kind=error) and the next step still runs."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402
import json  # noqa: E402
import traceback  # noqa: E402

from tools import _common as C  # noqa: E402
from tools.check_triggers import call_tool, check_triggers  # noqa: E402
from tools.snapshot import take_snapshot  # noqa: E402
from trader import db  # noqa: E402


def nightly(conn=None, client=None) -> dict:
    conn = conn or C.connect()
    client = client or C.broker()
    steps = []

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

    def news():
        syms = sorted({s for r in C.list_theses(conn, ("active", "paused")) if r["id"] != "manual"
                       for s in C.load_thesis(conn, r["id"])[1].universe})
        if not syms:
            return {"ran": False, "reason": "no symbols"}
        res = call_tool("news.py", [*syms, "--since", "24h"], timeout=1800)
        if res.get("ran") and res.get("exit") not in (0, None):
            raise RuntimeError(f"news.py exited {res['exit']}: {str(res.get('output'))[:300]}")
        return res

    step("reconcile", lambda: {"updated": (client.reconcile() or {}).get("updated")})
    step("snapshot", lambda: take_snapshot(conn, client))
    step("news", news)
    step("check_triggers", lambda: check_triggers(conn=conn, client=client))
    return {"ok": all(s["ok"] for s in steps), "steps": steps}


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
