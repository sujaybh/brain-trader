"""Shared helpers for tools/*.py: JSON output, error handling, broker client, thesis loading.

Every tool prints exactly one JSON document on stdout. Exit codes: 0 ok, 1 broker refusal or
validation failure (JSON has "ok": false and "reason"), 2 unexpected error."""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _reexec_in_venv() -> None:
    """`#!/usr/bin/env python3` may resolve to a python without our deps. When a tool or
    scheduler script is run directly by some other interpreter, re-exec it under .venv."""
    import os
    venv = ROOT / ".venv"
    main_file = getattr(sys.modules.get("__main__"), "__file__", None)
    if not main_file or not (venv / "bin" / "python").exists():
        return
    if Path(main_file).resolve().parent not in (ROOT / "tools", ROOT / "scheduler"):
        return
    if Path(sys.prefix).resolve() == venv.resolve() or os.environ.get("TRADER_REEXEC"):
        return
    os.environ["TRADER_REEXEC"] = "1"  # guard against loops
    py = str(venv / "bin" / "python")
    os.execv(py, [py, str(Path(main_file).resolve()), *sys.argv[1:]])


_reexec_in_venv()

from trader import client as _client  # noqa: E402
from trader import config, db  # noqa: E402
from trader.client import BrokerError  # noqa: E402
from trader.spec import ThesisSpec, manual_spec  # noqa: E402

TERMINAL_STATUSES = {"filled", "canceled", "cancelled", "rejected", "refused", "expired", "replaced"}


class ToolFailure(Exception):
    """A validation failure or refusal: exit 1 with {"ok": false, "reason": ...}."""

    def __init__(self, reason: str, **extra: Any):
        super().__init__(reason)
        self.reason = reason
        self.extra = extra


def log(*a: Any) -> None:
    print(*a, file=sys.stderr)


def emit(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def run(main: Callable[[list[str] | None], tuple[dict, int] | dict], argv: list[str] | None = None) -> int:
    """Run a tool's body, turning exceptions into the JSON/exit-code convention."""
    try:
        res = main(argv)
        if isinstance(res, tuple):
            out, code = res
        else:
            out, code = res, 0 if res.get("ok", True) else 1
        emit(out)
        return code
    except ToolFailure as e:
        emit({"ok": False, "reason": e.reason, **e.extra})
        return 1
    except BrokerError as e:
        if e.refused:
            detail = e.detail if isinstance(e.detail, dict) else {}
            emit({"ok": False, "refused": True, "reason": e.reason, "check": detail.get("check")})
            return 1
        traceback.print_exc()
        emit({"ok": False, "reason": e.reason, "broker_status": e.status})
        return 2
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        emit({"ok": False, "reason": f"unexpected error: {type(e).__name__}: {e}"})
        return 2


def broker() -> "_client.BrokerClient":
    # Looked up at call time so tests can monkeypatch trader.client.BrokerClient.
    return _client.BrokerClient()


def connect() -> sqlite3.Connection:
    return db.connect()


def load_thesis(conn: sqlite3.Connection, thesis_id: str) -> tuple[dict, ThesisSpec]:
    r = db.row(conn, "SELECT * FROM theses WHERE id=?", (thesis_id,))
    if not r:
        if thesis_id == config.MANUAL_THESIS_ID:
            ensure_manual_thesis(conn)
            return load_thesis(conn, thesis_id)
        raise ToolFailure(f"no thesis with id {thesis_id!r}")
    spec = ThesisSpec(**json.loads(r["spec"]))
    return r, spec


def list_theses(conn: sqlite3.Connection, statuses: tuple[str, ...] | None = ("active", "paused")) -> list[dict]:
    if statuses is None:
        return db.rows(conn, "SELECT * FROM theses ORDER BY created, id")
    q = ",".join("?" * len(statuses))
    return db.rows(conn, f"SELECT * FROM theses WHERE status IN ({q}) ORDER BY created, id", statuses)


def insert_thesis(conn: sqlite3.Connection, spec: ThesisSpec) -> None:
    now = db.now_iso()
    conn.execute(
        "INSERT INTO theses(id, name, status, mode, spec, source_text, created, updated) VALUES (?,?,?,?,?,?,?,?)",
        (spec.id, spec.name, spec.status, spec.mode, json.dumps(spec.model_dump()), spec.source_text, now, now),
    )


def ensure_manual_thesis(conn: sqlite3.Connection) -> bool:
    """Create the implicit `manual` thesis row if missing. Returns True if it was created."""
    if db.row(conn, "SELECT id FROM theses WHERE id=?", (config.MANUAL_THESIS_ID,)):
        return False
    spec = manual_spec(config.load_limits()["manual_thesis_cap"])
    try:
        insert_thesis(conn, spec)
    except sqlite3.IntegrityError:
        return False  # created concurrently
    db.journal(conn, "thesis", "created implicit manual thesis", thesis_id=spec.id,
               data={"capital_cap": spec.capital_cap})
    return True


def set_thesis_status(conn: sqlite3.Connection, thesis_id: str, status: str) -> tuple[str, ThesisSpec]:
    """Update status in both the column and the stored spec JSON. Returns (old_status, new_spec)."""
    r, spec = load_thesis(conn, thesis_id)
    old = r["status"]
    spec.status = status  # type: ignore[assignment]
    conn.execute("UPDATE theses SET status=?, spec=?, updated=? WHERE id=?",
                 (status, json.dumps(spec.model_dump()), db.now_iso(), thesis_id))
    if status == "active" and old != "active":
        # Paused theses skip slots entirely; slots missed while paused are not caught up.
        db.set_setting(conn, f"resumed:{thesis_id}", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return old, spec


def parse_dt(s: str) -> datetime:
    """ISO datetime; a naive value is taken as America/New_York (market time)."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=config.MARKET_TZ)
    return dt


def parse_since(s: str, now: datetime | None = None) -> datetime:
    """`7d`, `24h`, `30m`, `2026-10-01`, or a full ISO datetime -> aware datetime."""
    now = now or datetime.now(timezone.utc)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([dhmw])", s.strip())
    if m:
        n, unit = float(m.group(1)), m.group(2)
        return now - {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n),
                      "w": timedelta(weeks=n)}[unit]
    try:
        return parse_dt(s)
    except ValueError:
        raise ToolFailure(f"cannot parse --since {s!r}; use 7d, 24h, 30m, YYYY-MM-DD or ISO")


def quote_price(q: dict | None) -> float | None:
    """Best price from a broker quote: last trade, else bid/ask midpoint, else either side."""
    if not q:
        return None
    last = q.get("last")
    if last:
        return float(last)
    bid, ask = q.get("bid") or 0, q.get("ask") or 0
    if bid and ask:
        return round((float(bid) + float(ask)) / 2, 4)
    return float(ask or bid) if (ask or bid) else None


def inflight_by_symbol(conn: sqlite3.Connection, thesis_id: str) -> dict[str, float]:
    """Unfilled buy notional of our non-terminal orders for this thesis. Counted as committed
    when sizing new buys so a second catch-up slot does not over-allocate before fills land."""
    out: dict[str, float] = {}
    q = ",".join("?" * len(TERMINAL_STATUSES))
    for r in conn.execute(
        f"SELECT symbol, notional, filled_notional FROM orders WHERE thesis_id=? AND side='buy' "
        f"AND status NOT IN ({q}) AND notional IS NOT NULL", (thesis_id, *TERMINAL_STATUSES)):
        rem = max((r["notional"] or 0) - (r["filled_notional"] or 0), 0.0)
        if rem:
            out[r["symbol"]] = round(out.get(r["symbol"], 0.0) + rem, 2)
    return out


def committed_for(conn: sqlite3.Connection, thesis_id: str) -> dict[str, float]:
    """deployed_by_thesis for one thesis plus in-flight buys."""
    out = dict(db.deployed_by_thesis(conn).get(thesis_id, {}))
    for s, v in inflight_by_symbol(conn, thesis_id).items():
        out[s] = round(out.get(s, 0.0) + v, 2)
    return out


def positions_by_symbol(positions: list[dict]) -> dict[str, dict]:
    return {p["symbol"]: p for p in positions or []}


def fnum(x: Any) -> float | None:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def change_status_tool(argv, *, target: str, allowed_from: str, verb: str) -> dict:
    """Body shared by thesis_pause.py / thesis_resume.py."""
    import argparse
    from trader import notify
    past = {"pause": "paused", "resume": "resumed"}[verb]
    ap = argparse.ArgumentParser(description=f"{verb.capitalize()} a thesis: set status to {target} "
                                             "(column and stored spec JSON), journal, push.")
    ap.add_argument("id")
    ap.add_argument("--reason", default="")
    a = ap.parse_args(argv)
    conn = connect()
    r, _ = load_thesis(conn, a.id)
    if r["status"] == target:
        return {"ok": True, "id": a.id, "status": target, "changed": False}
    if r["status"] != allowed_from:
        raise ToolFailure(f"thesis {a.id} is {r['status']}; only a {allowed_from} thesis can be {past}")
    with db.tx(conn):
        set_thesis_status(conn, a.id, target)
        db.journal(conn, "thesis", f"{past} {a.reason}".strip(), thesis_id=a.id, data={"status": target})
    notify.push(f"{past.capitalize()} thesis {a.id}. {a.reason}".strip(), title=f"Thesis {past}", conn=conn)
    return {"ok": True, "id": a.id, "status": target, "changed": True}
