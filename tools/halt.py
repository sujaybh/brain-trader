#!/usr/bin/env python3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402
import getpass  # noqa: E402

from tools import _common as C  # noqa: E402
from trader import config, db  # noqa: E402
from trader import notify as N  # noqa: E402

DOC = ("Kill switch. 'on' creates data/HALT (the broker then refuses every order), 'off' removes "
       "it, 'status' reports it.")


def _status() -> dict:
    p = config.HALT_PATH
    return {"halted": p.exists(), "path": str(p), "content": p.read_text().strip() if p.exists() else None}


def _main(argv):
    ap = argparse.ArgumentParser(description=DOC)
    ap.add_argument("state", choices=["on", "off", "status"])
    ap.add_argument("--reason", default="", help="why (recorded in the HALT file and journal)")
    a = ap.parse_args(argv)
    p = config.HALT_PATH
    if a.state == "status":
        return {"ok": True, **_status()}
    conn = C.connect()
    changed = False
    if a.state == "on":
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"halted {db.now_iso()} by {getpass.getuser()}: {a.reason}\n")
            changed = True
            db.journal(conn, "system", f"HALT on: {a.reason or 'no reason given'}", data={"path": str(p)})
            N.push(f"Kill switch ON. All orders will be refused. {a.reason}".strip(),
                   title="TRADING HALTED", priority=5, tags=["rotating_light"], conn=conn)
    else:
        if p.exists():
            p.unlink()
            changed = True
            db.journal(conn, "system", f"HALT off: {a.reason or 'no reason given'}", data={"path": str(p)})
            N.push("Kill switch OFF. Orders allowed again.", title="Trading resumed", conn=conn)
    return {"ok": True, "changed": changed, **_status()}


def main(argv=None) -> int:
    return C.run(_main, argv)


if __name__ == "__main__":
    sys.exit(main())
