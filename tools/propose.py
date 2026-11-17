#!/usr/bin/env python3
"""Create a proposal for the user to approve (or, for a thesis in `auto` mode, execute it now).

Actions (JSON):
  {"type":"order","side":"buy|sell","symbol":"NVDA","notional":50}   (or "qty": 0.5 instead of notional)
  {"type":"pause"}   {"type":"resume"}
  {"type":"spec_change","patch":{"capital_cap":400}}   (shallow merge over the spec, re-validated)

In confirm mode the phone gets Approve/Reject buttons; nothing executes until a tap.
Prints one JSON document. Exit 0 ok, 1 refused/invalid, 2 unexpected error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_VENV = ROOT / ".venv"
if __name__ == "__main__" and (_VENV / "bin" / "python").exists() and Path(sys.prefix).resolve() != _VENV.resolve() \
        and not os.environ.get("TRADER_REEXEC"):
    # run directly via the shebang by a system python: re-exec under the project venv
    os.environ["TRADER_REEXEC"] = "1"
    os.execv(str(_VENV / "bin" / "python"), [str(_VENV / "bin" / "python"), __file__, *sys.argv[1:]])

from trader.db import connect  # noqa: E402
from web import proposals  # noqa: E402


def _out(doc: dict, code: int) -> int:
    print(json.dumps(doc, indent=2, default=str))
    return code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--thesis", required=True, help="thesis id (use 'manual' for an order outside any thesis)")
    ap.add_argument("--action", required=True, help="action JSON, see above")
    ap.add_argument("--reason", required=True, help="why; shown on the phone")
    ap.add_argument("--expires", default="24h", help="how long the proposal stays open (e.g. 4h, 2d); default 24h")
    ap.add_argument("--force-confirm", action="store_true",
                    help="always wait for the phone tap, even if the thesis is in mode auto (used by the Ollama chat)")
    args = ap.parse_args(argv)
    try:
        try:
            action = json.loads(args.action)
            expires = proposals.parse_duration(args.expires)
        except ValueError as e:
            return _out({"ok": False, "reason": f"bad argument: {e}"}, 1)
        conn = connect()
        try:
            p = proposals.create(conn, args.thesis, action, args.reason, expires, force_confirm=args.force_confirm)
        except proposals.ProposalError as e:
            return _out({"ok": False, "reason": e.reason}, 1)
        finally:
            conn.close()
        ok = p["status"] != "failed"
        doc = {"ok": ok, "proposal": p}
        if p["status"] == "pending":
            doc["note"] = "waiting for the user to approve on the phone or dashboard"
        if not ok:
            doc["reason"] = (p.get("result") or {}).get("reason", "execution failed")
        return _out(doc, 0 if ok else 1)
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        return _out({"ok": False, "reason": f"unexpected error: {type(e).__name__}: {e}"}, 2)


if __name__ == "__main__":
    sys.exit(main())
