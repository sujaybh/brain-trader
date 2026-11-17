"""Push notifications to the phone via ntfy. Journals every push. Safe no-op if unconfigured."""
from __future__ import annotations

import json
import sys
from typing import Iterable

import requests

from . import config
from .db import connect, journal


def push(text: str, *, title: str | None = None, priority: int | None = None,
         tags: Iterable[str] | None = None, actions: list[dict] | None = None,
         click: str | None = None, conn=None) -> bool:
    """Send one notification. `actions` is a list of ntfy action dicts, e.g.
    {"action": "http", "label": "Approve", "url": "...", "method": "POST", "clear": True}."""
    own = conn is None
    conn = conn or connect()
    try:
        journal(conn, "notify", text, data={"title": title, "actions": _redact(actions)})
    finally:
        if own:
            conn.close()
    if not config.NTFY_TOPIC:
        print(f"[notify] NTFY_TOPIC unset; would have sent: {title or ''} {text}", file=sys.stderr)
        return False
    headers = {}
    if title:
        headers["Title"] = title
    if priority:
        headers["Priority"] = str(priority)
    if tags:
        headers["Tags"] = ",".join(tags)
    if click:
        headers["Click"] = click
    if actions:
        headers["Actions"] = "; ".join(_fmt_action(a) for a in actions)
    try:
        r = requests.post(f"{config.NTFY_URL.rstrip('/')}/{config.NTFY_TOPIC}", data=text.encode(),
                          headers=headers, timeout=10)
        return r.ok
    except requests.RequestException as e:
        print(f"[notify] failed: {e}", file=sys.stderr)
        return False


def _redact(actions: list[dict] | None) -> list[dict] | None:
    """Copy of `actions` with header values hidden: the journal must not store credentials."""
    if not actions:
        return actions
    return [{**a, "headers": {k: "***" for k in a["headers"]}} if a.get("headers") else a for a in actions]


def _q(v: str) -> str:
    """ntfy action values are comma/semicolon separated; quote a value that contains either
    (single quotes, or double quotes when the value itself holds a single quote)."""
    if not any(c in v for c in ",;") or v[:1] in "'\"":
        return v
    return f'"{v}"' if "'" in v else f"'{v}'"


def _fmt_action(a: dict) -> str:
    """ntfy header action format (https://docs.ntfy.sh/publish/#send-http-request):
    `http, Label, url, method=POST, headers.Authorization=Basic xxx, body='{"a":1}', clear=true`."""
    parts = [a.get("action", "http"), _q(a["label"]), _q(a["url"])]
    if a.get("method"):
        parts.append(f"method={a['method']}")
    for k, v in (a.get("headers") or {}).items():
        parts.append(f"headers.{k}={_q(str(v))}")
    if a.get("body"):
        body = a["body"] if isinstance(a["body"], str) else json.dumps(a["body"])
        parts.append("body=" + _q(body))
    if a.get("clear"):
        parts.append("clear=true")
    return ", ".join(parts)
