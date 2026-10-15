#!/usr/bin/env python3
"""Validate and store a compiled thesis spec. --dry-run prints the normalized spec and the
plain-English read-back without storing anything."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402
import json  # noqa: E402
import sqlite3  # noqa: E402

from pydantic import ValidationError  # noqa: E402

from tools import _common as C  # noqa: E402
from trader import config, db  # noqa: E402
from trader import notify as N  # noqa: E402
from trader.spec import ThesisSpec, describe  # noqa: E402


def check_assets(client, symbols: list[str]) -> list[dict]:
    """Every universe symbol must exist, be a tradable US equity, and be fractionable
    (DCA orders are dollar-sized). Returns one problem per bad symbol (all of them)."""
    info = client.assets(symbols) or {}
    problems = []
    for s in symbols:
        a = info.get(s)
        if not a:
            problems.append({"symbol": s, "problem": "not found on Alpaca"})
            continue
        why = []
        if not a.get("tradable"):
            why.append("not tradable")
        if not a.get("fractionable"):
            why.append("not fractionable")
        if a.get("asset_class") not in (None, "us_equity"):
            why.append(f"asset class {a.get('asset_class')} (only US equities)")
        if why:
            problems.append({"symbol": s, "problem": ", ".join(why)})
    return problems


def _main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("spec", help="path to spec JSON ('-' for stdin)")
    ap.add_argument("--dry-run", action="store_true", help="validate and read back; store nothing")
    a = ap.parse_args(argv)

    try:
        raw = sys.stdin.read() if a.spec == "-" else Path(a.spec).read_text()
        data = json.loads(raw)
    except (OSError, ValueError) as e:
        raise C.ToolFailure(f"cannot read spec: {e}")
    try:
        spec = ThesisSpec(**data)
    except ValidationError as e:
        errs = [{"field": ".".join(str(x) for x in err["loc"]), "error": err["msg"]} for err in e.errors()]
        raise C.ToolFailure("spec is invalid: " + "; ".join(f"{x['field']}: {x['error']}" for x in errs),
                            errors=errs)
    if spec.id == config.MANUAL_THESIS_ID:
        raise C.ToolFailure("'manual' is reserved for the implicit manual thesis")

    conn = C.connect()
    if db.row(conn, "SELECT id FROM theses WHERE id=?", (spec.id,)):
        raise C.ToolFailure(f"a thesis with id {spec.id!r} already exists; pick another id")

    problems = check_assets(C.broker(), spec.universe)
    if problems:
        raise C.ToolFailure("not tradable here: " + "; ".join(f"{p['symbol']} ({p['problem']})" for p in problems),
                            bad_symbols=problems)

    text = describe(spec)
    out = {"ok": True, "dry_run": a.dry_run, "id": spec.id, "describe": text, "spec": spec.model_dump()}
    if a.dry_run:
        return out

    C.ensure_manual_thesis(conn)
    try:
        with db.tx(conn):
            C.insert_thesis(conn, spec)
            db.journal(conn, "thesis", f"created: {text}", thesis_id=spec.id,
                       data={"spec": spec.model_dump()})
    except sqlite3.IntegrityError:
        raise C.ToolFailure(f"a thesis with id {spec.id!r} already exists; pick another id")
    N.push(text, title=f"New thesis: {spec.name} ({spec.id})", conn=conn)
    out["stored"] = True
    return out


def main(argv=None) -> int:
    return C.run(_main, argv)


if __name__ == "__main__":
    sys.exit(main())
