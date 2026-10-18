"""Chat with Claude Code from the phone. One long-lived session: the session id from the
`--output-format json` result is stored in settings['chat_session'] and passed as `--resume`
on later messages; if resuming fails we start a fresh session. Transcript lives in `chat`."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from trader import config
from trader.db import get_setting, now_iso, rows, set_setting

TIMEOUT_S = 300
SESSION_KEY = "chat_session"


def claude_bin() -> str:
    """CLAUDE_BIN env, else ~/.local/bin/claude, else whatever is on PATH."""
    if os.environ.get("CLAUDE_BIN"):
        return os.environ["CLAUDE_BIN"]
    local = Path.home() / ".local" / "bin" / "claude"
    if local.exists():
        return str(local)
    return shutil.which("claude") or "claude"


def allowed_tools() -> list[str]:
    """Tools the non-interactive session may use without a permission prompt. -p mode has no
    one to click "allow", so without this every tool call is denied. Override with
    CLAUDE_ALLOWED_TOOLS (comma-separated); set it to "" to pass nothing."""
    v = os.environ.get("CLAUDE_ALLOWED_TOOLS")
    if v is None:
        v = "Read,Grep,Glob,Bash(.venv/bin/python tools/*),Bash(tools/*)"
    return [t.strip() for t in v.split(",") if t.strip()]


def claude_cmd(output_format: str, resume: str | None = None) -> list[str]:
    """`claude -p` with the prompt fed on stdin (no argv length limit, no flag-parsing surprises
    if the message starts with '-'). The model is pinned to config.CLAUDE_MODEL (default sonnet)
    so scripted calls do not inherit the CLI's default, which may be the most expensive tier."""
    cmd = [claude_bin(), "-p", "--output-format", output_format]
    if config.CLAUDE_MODEL:
        cmd += ["--model", config.CLAUDE_MODEL]
    tools = allowed_tools()
    if tools:
        cmd += ["--allowedTools", *tools]
    if resume:
        cmd += ["--resume", resume]
    return cmd


def record_usage(conn, caller: str, out: dict | None, ok: bool = True) -> dict | None:
    """Store what one `claude -p --output-format json` call consumed. `out` is the parsed result
    document (None or non-dict -> nothing recorded). Returns the row written.

    The CLI's `usage` block is for this call only, but `total_cost_usd` and `modelUsage` are
    cumulative over the whole session when `--resume` is used, so cost is taken as the delta
    from the previous row recorded for the same session_id."""
    if not isinstance(out, dict):
        return None
    u = out.get("usage") or {}
    sid = out.get("session_id")
    session_cost = float(out.get("total_cost_usd") or 0)
    model_usage = out.get("modelUsage") or {}
    prev = None
    if sid:
        prev = rows(conn, "SELECT session_cost_usd, models FROM claude_usage WHERE session_id=? ORDER BY id DESC LIMIT 1", (sid,))
    prev_cost, prev_models = 0.0, {}
    if prev:
        prev_cost = float(prev[0]["session_cost_usd"] or 0)
        try:
            prev_models = json.loads(prev[0]["models"] or "{}")
        except ValueError:
            prev_models = {}
    cost = round(max(session_cost - prev_cost, 0.0), 6) if session_cost >= prev_cost else session_cost
    # models that did work in this call: their cumulative costUSD moved (or all with cost, first time)
    moved = [m for m, v in model_usage.items()
             if float((v or {}).get("costUSD") or 0) > float((prev_models.get(m) or {}).get("costUSD") or 0)]
    if not moved:
        moved = [m for m, v in model_usage.items() if float((v or {}).get("costUSD") or 0) > 0] or list(model_usage)
    row = {
        "ts": now_iso(), "caller": caller, "model": ",".join(sorted(moved)) or None,
        "session_id": sid, "turns": out.get("num_turns"),
        "input_tokens": int(u.get("input_tokens") or 0),
        "cache_read": int(u.get("cache_read_input_tokens") or 0),
        "cache_write": int(u.get("cache_creation_input_tokens") or 0),
        "output_tokens": int(u.get("output_tokens") or 0),
        "cost_usd": cost, "session_cost_usd": session_cost,
        "models": json.dumps(model_usage) if model_usage else None,
        "duration_ms": out.get("duration_ms"), "ok": 1 if ok else 0,
    }
    if not any((row["input_tokens"], row["cache_read"], row["cache_write"], row["output_tokens"], session_cost)):
        return None  # nothing reported (e.g. a mocked or failed call)
    cols = ",".join(row)
    conn.execute(f"INSERT INTO claude_usage({cols}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))
    return row


def _add(conn, role: str, content: str) -> None:
    conn.execute("INSERT INTO chat(ts, role, content) VALUES (?,?,?)", (now_iso(), role, content))


def history(conn, n: int = 30) -> list[dict]:
    return list(reversed(rows(conn, "SELECT * FROM chat ORDER BY id DESC LIMIT ?", (n,))))


def run_json(prompt: str, resume: str | None = None, timeout: int = TIMEOUT_S) -> tuple[bool, str, dict | None]:
    """Run `claude -p --output-format json`. -> (ok, text, result document or None).
    Shared by chat and tools/review.py so both record usage the same way."""
    try:
        cp = subprocess.run(claude_cmd("json", resume), input=prompt, cwd=str(config.ROOT),
                            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"Claude did not answer within {timeout // 60} minutes.", None
    except OSError as e:
        return False, f"could not start claude: {e}", None
    try:
        out = json.loads(cp.stdout)
    except ValueError:
        out = None
    if isinstance(out, dict):
        text = str(out.get("result") or "")
        ok = cp.returncode == 0 and not out.get("is_error") and bool(text)
        return ok, text or (cp.stderr.strip() or "empty reply"), out
    return False, (cp.stderr.strip() or cp.stdout.strip() or f"claude exited {cp.returncode}")[-2000:], None


def _run(prompt: str, resume: str | None) -> tuple[bool, str, str | None, dict | None]:
    """-> (ok, text, session_id, result document)."""
    ok, text, out = run_json(prompt, resume)
    return ok, text, (out or {}).get("session_id"), out


def send(conn, message: str) -> dict:
    """Store the user message, ask Claude (resuming the stored session), store the reply."""
    message = (message or "").strip()
    if not message:
        return {"ok": False, "reason": "empty message"}
    _add(conn, "user", message)
    session = get_setting(conn, SESSION_KEY)
    ok, text, sid, out = _run(message, session)
    resumed = bool(session)
    record_usage(conn, "chat", out, ok)
    if not ok and session:  # stale/broken session: start over rather than leave the user stuck
        ok, text, sid, out = _run(message, None)
        resumed = False
        record_usage(conn, "chat", out, ok)
    if sid:
        set_setting(conn, SESSION_KEY, sid)
    _add(conn, "assistant" if ok else "error", text)
    return {"ok": ok, "reply": text, "session_id": sid, "resumed": resumed,
            "cost_usd": (out or {}).get("total_cost_usd")}
