"""Proposal lifecycle shared by tools/propose.py (auto mode) and the web approve/reject API.

Status transitions:
    pending -> approved -> executed | failed
    pending -> rejected
    pending -> expired            (checked lazily: expire_stale() runs on every listing/decision)

Every transition is journaled (kind=proposal). Pushes: the creation (with Approve/Reject
buttons in confirm mode), rejection, expiry, and the final outcome (executed/failed, which
also reports the approval) so an Approve tap buzzes the phone once, not twice.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError

from trader import client as tclient
from trader import config, notify
from trader.db import journal, now_iso, row, rows, tx
from trader.schedule import manual_order_id
from trader.spec import ThesisSpec

SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
ACTION_TYPES = ("order", "pause", "resume", "spec_change")
TERMINAL = ("executed", "failed", "rejected", "expired")


class ProposalError(Exception):
    """A refusal: bad action, unknown thesis, wrong status, expired. Carries a user-facing reason."""

    def __init__(self, reason: str, proposal: dict | None = None):
        super().__init__(reason)
        self.reason = reason
        self.proposal = proposal


# ---------------------------------------------------------------- helpers

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_duration(s: str) -> timedelta:
    """'24h', '7d', '90m', '2w' -> timedelta. Bare numbers are hours."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([mhdw]?)\s*", str(s).lower())
    if not m:
        raise ValueError(f"bad duration {s!r}; use e.g. 90m, 24h, 7d, 2w")
    n, unit = float(m.group(1)), m.group(2) or "h"
    return {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]


def _web_link(path: str) -> str:
    return f"{config.WEB_URL.rstrip('/')}{path}?token={config.WEB_TOKEN}"


def _push(conn, text: str, **kw) -> None:
    try:
        notify.push(text, conn=conn, **kw)
    except Exception as e:  # a failed push must never break a state transition
        journal(conn, "error", f"push failed: {e}")


def load_thesis(conn, thesis_id: str) -> dict | None:
    return row(conn, "SELECT * FROM theses WHERE id=?", (thesis_id,))


def describe_action(action: dict) -> str:
    t = action.get("type")
    if t == "order":
        size = f"${action['notional']:g}" if action.get("notional") is not None else f"{action['qty']:g} sh"
        return f"{action['side']} {size} {action['symbol']}"
    if t == "spec_change":
        return "change spec: " + ", ".join(f"{k}={json.dumps(v)}" for k, v in action.get("patch", {}).items())
    return str(t)


def decode(p: dict | None) -> dict | None:
    """Proposal row with action/result JSON decoded and an English summary."""
    if p is None:
        return None
    p = dict(p)
    for k in ("action", "result"):
        if isinstance(p.get(k), str):
            try:
                p[k] = json.loads(p[k])
            except ValueError:
                pass
    p["summary"] = describe_action(p["action"]) if isinstance(p.get("action"), dict) else str(p.get("action"))
    return p


# ---------------------------------------------------------------- validation

def _num(v: Any, name: str) -> float:
    if isinstance(v, bool):
        raise ProposalError(f"{name} must be a number")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ProposalError(f"{name} must be a number, got {v!r}")
    if not f > 0:
        raise ProposalError(f"{name} must be > 0")
    return f


def apply_patch(spec_json: dict, patch: dict) -> ThesisSpec:
    """Shallow-merge `patch` over the stored spec and re-validate. Raises ProposalError."""
    if not isinstance(patch, dict) or not patch:
        raise ProposalError("spec_change needs a non-empty 'patch' object")
    if "id" in patch and patch["id"] != spec_json.get("id"):
        raise ProposalError("spec_change may not change the thesis id")
    merged = {**spec_json, **patch}
    try:
        return ThesisSpec.model_validate(merged)
    except ValidationError as e:
        errs = "; ".join(f"{'.'.join(str(x) for x in err['loc']) or 'spec'}: {err['msg']}" for err in e.errors())
        raise ProposalError(f"patched spec is invalid: {errs}")


def validate_action(conn, thesis_id: str, action: Any) -> dict:
    """Normalize and check an action against the thesis. Returns the cleaned action."""
    if not isinstance(action, dict):
        raise ProposalError("action must be a JSON object")
    t = action.get("type")
    if t not in ACTION_TYPES:
        raise ProposalError(f"action.type must be one of {', '.join(ACTION_TYPES)}")
    thesis = load_thesis(conn, thesis_id)
    if thesis is None and not (t == "order" and thesis_id == config.MANUAL_THESIS_ID):
        raise ProposalError(f"unknown thesis {thesis_id!r}")
    if t == "order":
        side = str(action.get("side", "")).lower()
        if side not in ("buy", "sell"):
            raise ProposalError("order side must be buy or sell")
        sym = str(action.get("symbol", "")).strip().upper()
        if not SYMBOL_RE.match(sym):
            raise ProposalError(f"bad symbol {action.get('symbol')!r}")
        has_n, has_q = action.get("notional") is not None, action.get("qty") is not None
        if has_n == has_q:
            raise ProposalError("order needs exactly one of notional (USD) or qty (shares)")
        out = {"type": "order", "side": side, "symbol": sym, "notional": None, "qty": None}
        if has_n:
            out["notional"] = round(_num(action["notional"], "notional"), 2)
        else:
            out["qty"] = _num(action["qty"], "qty")
        return out
    if t in ("pause", "resume"):
        return {"type": t}
    # spec_change: validate now so a bad patch never reaches the phone
    apply_patch(json.loads(thesis["spec"]), action.get("patch"))
    return {"type": "spec_change", "patch": action["patch"]}


# ---------------------------------------------------------------- lifecycle

def get(conn, proposal_id: int) -> dict | None:
    return decode(row(conn, "SELECT * FROM proposals WHERE id=?", (proposal_id,)))


def expire_stale(conn) -> list[int]:
    """Lazily move overdue pending proposals to expired. Returns the ids expired."""
    now = _now()
    expired = []
    for p in rows(conn, "SELECT * FROM proposals WHERE status='pending' AND expires IS NOT NULL"):
        exp = _parse_ts(p["expires"])
        if exp and exp <= now:
            n = conn.execute("UPDATE proposals SET status='expired', decided=? WHERE id=? AND status='pending'",
                             (now_iso(), p["id"])).rowcount
            if n:
                expired.append(p["id"])
                summary = decode(p)["summary"]
                journal(conn, "proposal", f"proposal #{p['id']} expired: {summary}", thesis_id=p["thesis_id"],
                        data={"proposal_id": p["id"], "status": "expired"})
                _push(conn, f"Proposal #{p['id']} expired unanswered: {summary}", title="Proposal expired",
                      tags=["hourglass"])
    return expired


def list_pending(conn) -> list[dict]:
    expire_stale(conn)
    return [decode(p) for p in rows(conn, "SELECT * FROM proposals WHERE status='pending' ORDER BY id")]


def create(conn, thesis_id: str, action: Any, reason: str, expires: timedelta = timedelta(hours=24),
           approve_now: str | None = None) -> dict:
    """Insert a pending proposal. In confirm mode push Approve/Reject buttons; in auto mode
    approve and execute immediately (the same path a phone tap takes) and push the result.
    `approve_now="web"` is for actions the user initiated themselves (dashboard buttons): the
    tap is the approval, so skip the buttons and execute."""
    reason = (reason or "").strip()
    if not reason:
        raise ProposalError("a reason is required")
    clean = validate_action(conn, thesis_id, action)
    thesis = load_thesis(conn, thesis_id)
    mode = thesis["mode"] if thesis else "confirm"
    exp = (_now() + expires).isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO proposals(thesis_id, action, reason, status, created, expires) VALUES (?,?,?,?,?,?)",
        (thesis_id, json.dumps(clean), reason, "pending", now_iso(), exp))
    pid = int(cur.lastrowid)
    summary = describe_action(clean)
    journal(conn, "proposal", f"proposal #{pid} created ({mode}): {summary} — {reason}", thesis_id=thesis_id,
            symbol=clean.get("symbol"), data={"proposal_id": pid, "status": "pending", "action": clean,
                                              "mode": mode, "expires": exp})
    if mode == "auto" or approve_now:
        return approve(conn, pid, by=approve_now or "auto")
    actions = [
        {"action": "http", "label": "Approve", "url": _web_link(f"/api/proposals/{pid}/approve"),
         "method": "POST", "clear": True},
        {"action": "http", "label": "Reject", "url": _web_link(f"/api/proposals/{pid}/reject"),
         "method": "POST", "clear": True},
    ]
    _push(conn, f"[{thesis_id}] {summary}\n{reason}", title=f"Proposal #{pid}", tags=["question"],
          actions=actions, click=_web_link("/"))
    return get(conn, pid)


def _claim(conn, proposal_id: int, new_status: str) -> dict:
    """Atomically move a pending, unexpired proposal to new_status; refuse otherwise."""
    expire_stale(conn)
    with tx(conn):
        p = row(conn, "SELECT * FROM proposals WHERE id=?", (proposal_id,))
        if p is None:
            raise ProposalError(f"no proposal #{proposal_id}")
        if p["status"] != "pending":
            raise ProposalError(f"proposal #{proposal_id} is {p['status']}, not pending", decode(p))
        conn.execute("UPDATE proposals SET status=?, decided=? WHERE id=?", (new_status, now_iso(), proposal_id))
    return get(conn, proposal_id)


def reject(conn, proposal_id: int, by: str = "phone") -> dict:
    p = _claim(conn, proposal_id, "rejected")
    journal(conn, "proposal", f"proposal #{proposal_id} rejected ({by}): {p['summary']}", thesis_id=p["thesis_id"],
            data={"proposal_id": proposal_id, "status": "rejected", "by": by})
    _push(conn, f"Rejected #{proposal_id}: {p['summary']}", title=f"Proposal #{proposal_id} rejected", tags=["x"])
    return p


def approve(conn, proposal_id: int, by: str = "phone") -> dict:
    p = _claim(conn, proposal_id, "approved")
    journal(conn, "proposal", f"proposal #{proposal_id} approved ({by}): {p['summary']}", thesis_id=p["thesis_id"],
            data={"proposal_id": proposal_id, "status": "approved", "by": by})
    return execute_proposal(conn, proposal_id)


def _finish(conn, p: dict, status: str, result: dict) -> dict:
    conn.execute("UPDATE proposals SET status=?, result=? WHERE id=?", (status, json.dumps(result), p["id"]))
    msg = result.get("message") or result.get("reason") or ""
    journal(conn, "proposal", f"proposal #{p['id']} {status}: {p['summary']} — {msg}", thesis_id=p["thesis_id"],
            symbol=(p["action"] or {}).get("symbol"), data={"proposal_id": p["id"], "status": status, "result": result})
    if not (status == "executed" and (p["action"] or {}).get("type") == "order"):
        # an executed order is pushed by the broker (fill or refusal); don't double-buzz
        _push(conn, f"#{p['id']} {p['summary']}: {msg}", title=f"Proposal #{p['id']} {status}",
              tags=["white_check_mark"] if status == "executed" else ["warning"], priority=None if status == "executed" else 4)
    return get(conn, p["id"])


def _set_thesis_status(conn, thesis: dict, status: str) -> dict:
    spec = json.loads(thesis["spec"])
    spec["status"] = status
    conn.execute("UPDATE theses SET status=?, spec=?, updated=? WHERE id=?",
                 (status, json.dumps(spec), now_iso(), thesis["id"]))
    journal(conn, "thesis", f"thesis {thesis['id']} {status}", thesis_id=thesis["id"], data={"status": status})
    return spec


def execute_proposal(conn: sqlite3.Connection, proposal_id: int) -> dict:
    """Carry out an approved proposal. Returns the updated proposal (status executed|failed)."""
    p = get(conn, proposal_id)
    if p is None:
        raise ProposalError(f"no proposal #{proposal_id}")
    if p["status"] != "approved":
        raise ProposalError(f"proposal #{proposal_id} is {p['status']}; only approved proposals execute", p)
    a, tid = p["action"], p["thesis_id"]
    try:
        if a["type"] == "order":
            decided = _parse_ts(p["decided"]) or _now()
            coid = manual_order_id(tid, a["symbol"], a["side"], decided)
            res = tclient.BrokerClient().place_order(
                thesis_id=tid, side=a["side"], symbol=a["symbol"], notional=a.get("notional"), qty=a.get("qty"),
                client_order_id=coid, reason=f"proposal #{proposal_id}: {p['reason']}"[:500])
            fill = f", filled {res.get('filled_qty')} @ {res.get('filled_avg_price')}" if res.get("filled_qty") else ""
            return _finish(conn, p, "executed", {"order": res, "client_order_id": coid,
                                                 "message": f"order {res.get('status', 'submitted')}{fill}"})
        thesis = load_thesis(conn, tid)
        if thesis is None:
            return _finish(conn, p, "failed", {"reason": f"thesis {tid!r} no longer exists"})
        if a["type"] in ("pause", "resume"):
            target = "paused" if a["type"] == "pause" else "active"
            if thesis["status"] == "closed":
                return _finish(conn, p, "failed", {"reason": "thesis is closed"})
            if thesis["status"] == target:
                return _finish(conn, p, "executed", {"message": f"already {target}"})
            _set_thesis_status(conn, thesis, target)
            return _finish(conn, p, "executed", {"message": f"thesis {target}"})
        # spec_change: re-validate against the spec as it is *now*
        try:
            new = apply_patch(json.loads(thesis["spec"]), a.get("patch"))
        except ProposalError as e:
            return _finish(conn, p, "failed", {"reason": e.reason})
        spec = new.model_dump()
        conn.execute("UPDATE theses SET name=?, status=?, mode=?, spec=?, source_text=?, updated=? WHERE id=?",
                     (new.name, new.status, new.mode, json.dumps(spec), new.source_text, now_iso(), tid))
        journal(conn, "thesis", f"thesis {tid} spec changed by proposal #{proposal_id}: {sorted(a['patch'])}",
                thesis_id=tid, data={"patch": a["patch"], "spec": spec})
        return _finish(conn, p, "executed", {"message": "spec updated", "spec": spec})
    except tclient.BrokerError as e:
        return _finish(conn, p, "failed", {"reason": e.reason, "refused": e.refused, "http": e.status})
    except Exception as e:  # never leave a proposal stuck in 'approved'
        return _finish(conn, p, "failed", {"reason": f"{type(e).__name__}: {e}"})
