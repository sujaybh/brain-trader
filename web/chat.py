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
    if the message starts with '-')."""
    cmd = [claude_bin(), "-p", "--output-format", output_format]
    tools = allowed_tools()
    if tools:
        cmd += ["--allowedTools", *tools]
    if resume:
        cmd += ["--resume", resume]
    return cmd


def _add(conn, role: str, content: str) -> None:
    conn.execute("INSERT INTO chat(ts, role, content) VALUES (?,?,?)", (now_iso(), role, content))


def history(conn, n: int = 30) -> list[dict]:
    return list(reversed(rows(conn, "SELECT * FROM chat ORDER BY id DESC LIMIT ?", (n,))))


def _run(prompt: str, resume: str | None) -> tuple[bool, str, str | None]:
    """-> (ok, text, session_id)."""
    try:
        cp = subprocess.run(claude_cmd("json", resume), input=prompt, cwd=str(config.ROOT),
                            capture_output=True, text=True, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return False, f"Claude did not answer within {TIMEOUT_S // 60} minutes.", None
    except OSError as e:
        return False, f"could not start claude: {e}", None
    try:
        out = json.loads(cp.stdout)
    except ValueError:
        out = None
    if isinstance(out, dict):
        text = str(out.get("result") or "")
        ok = cp.returncode == 0 and not out.get("is_error") and bool(text)
        return ok, text or (cp.stderr.strip() or "empty reply"), out.get("session_id")
    return False, (cp.stderr.strip() or cp.stdout.strip() or f"claude exited {cp.returncode}")[-2000:], None


def send(conn, message: str) -> dict:
    """Store the user message, ask Claude (resuming the stored session), store the reply."""
    message = (message or "").strip()
    if not message:
        return {"ok": False, "reason": "empty message"}
    _add(conn, "user", message)
    session = get_setting(conn, SESSION_KEY)
    ok, text, sid = _run(message, session)
    resumed = bool(session)
    if not ok and session:  # stale/broken session: start over rather than leave the user stuck
        ok, text, sid = _run(message, None)
        resumed = False
    if sid:
        set_setting(conn, SESSION_KEY, sid)
    _add(conn, "assistant" if ok else "error", text)
    return {"ok": ok, "reply": text, "session_id": sid, "resumed": resumed}
