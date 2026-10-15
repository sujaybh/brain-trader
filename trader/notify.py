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
        journal(conn, "notify", text, data={"title": title, "actions": actions})
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


def _fmt_action(a: dict) -> str:
    """ntfy header action format: `http, Label, url, method=POST, clear=true`."""
    parts = [a.get("action", "http"), a["label"], a["url"]]
    if a.get("method"):
        parts.append(f"method={a['method']}")
    if a.get("body"):
        parts.append("body=" + json.dumps(a["body"]).replace(",", "\\,"))
    if a.get("clear"):
        parts.append("clear=true")
    return ", ".join(parts)
