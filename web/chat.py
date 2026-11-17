"""Chat with Claude Code from the phone, and the locked-down `claude -p` command line shared with
tools/review.py.

Session: the session id from the `--output-format json` result is stored in
settings['chat_session'] and passed as `--resume` on later messages; if resuming fails we start
a fresh session. A resumed session re-sends its whole transcript (every earlier message and tool
result) on every model request, so the cost per message grows with the session. After
CHAT_MAX_TURNS messages, or once the last request's context passed CHAT_MAX_CONTEXT_TOKENS, the
next message starts a fresh session that carries only a short recap of the last few chat rows.
Transcript lives in `chat`.

Lockdown: the scripted sessions must not inherit the user's personal Claude Code setup (global
CLAUDE.md, auto-memory, plugins, skills, MCP servers and the claude.ai account connectors, which
include a brokerage). Only the broker daemon may talk to a brokerage. See claude_cmd()."""
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
TURNS_KEY = "chat_session_turns"      # messages sent in the current session
CONTEXT_KEY = "chat_session_context"  # prompt tokens of the last model request in it

# Rotation. A fresh session costs one system prompt (~10k tokens, cached across sessions); a long
# one re-reads its whole history on every request, tool results included (one 6-step message
# measured 145k cache-read tokens). 8 messages / 60k tokens keeps a short back-and-forth intact
# while capping the per-message cost at a few times a fresh session's.
CHAT_MAX_TURNS = 8
CHAT_MAX_CONTEXT_TOKENS = 60_000
CARRY_ROWS = 6        # chat rows recapped into a fresh session
CARRY_CHARS = 400     # per row

# Built-in tools per caller. --tools is what the model is offered at all; --allowedTools is what
# runs without a prompt (-p has no one to click "allow", and --permission-mode dontAsk denies the
# rest). Chat may run only the project's tools/*.py through the venv python and read the
# checkout, not the env files. Reviews get no tools: the pack holds the data and the answer comes
# back as structured output (tools/review.py).
TOOLS = {"chat": "Bash,Read,Grep,Glob", "review": ""}
ALLOWED = {"chat": "Bash(.venv/bin/python tools/*),Read,Grep,Glob", "review": ""}
DISALLOWED = {"chat": "Read(./config/*.env)", "review": ""}

# Isolation from the user's own Claude Code setup (semantics from `claude --help`, CLI 2.1.x):
#   --strict-mcp-config        only servers from --mcp-config, and we pass none: no MCP servers
#   --setting-sources project,local   skip ~/.claude/settings.json (user plugins, the user's own
#                              permission allow-list, model). It does NOT stop ~/.claude/CLAUDE.md
#                              from loading (measured), hence CLAUDE_CODE_DISABLE_CLAUDE_MDS below
#                              and the project CLAUDE.md passed explicitly with
#                              --append-system-prompt (chat only; reviews are self-contained).
#   --disable-slash-commands   no skills, no slash commands
#   --exclude-dynamic-system-prompt-sections   cwd/env/git status move out of the system prompt
#                              into the first message, so the system prompt stays byte-stable and
#                              cached across fresh sessions
#   --permission-mode dontAsk  anything not allow-listed is denied, never prompted
# --bare would cover most of this but refuses OAuth (subscription) auth, so it is not used.
LOCKDOWN_FLAGS = ["--strict-mcp-config", "--setting-sources", "project,local", "--disable-slash-commands",
                  "--exclude-dynamic-system-prompt-sections", "--permission-mode", "dontAsk"]
# What no flag turns off: the claude.ai account connectors (Robinhood, Gmail, Drive), auto-memory,
# and CLAUDE.md discovery (which would pull in the user's global ~/.claude/CLAUDE.md).
LOCKDOWN_ENV = {"ENABLE_CLAUDEAI_MCP_SERVERS": "false", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1"}
PROJECT_INSTRUCTIONS = {"chat": True, "review": False}  # append the project CLAUDE.md?


def project_instructions() -> str:
    """The project CLAUDE.md (the chat persona and workflow), or "" if missing."""
    try:
        return (config.ROOT / "CLAUDE.md").read_text().strip()
    except OSError:
        return ""


def claude_bin() -> str:
    """CLAUDE_BIN (env or config/trader.env; `~` expanded), else the first executable of
    ~/.local/bin/claude (native installer), ~/.claude/local/claude, PATH, /usr/local/bin/claude,
    ~/.npm-global/bin/claude. systemd units get a minimal PATH, so the fixed locations matter;
    on a new machine set CLAUDE_BIN to be sure (scheduler/doctor.py checks it)."""
    if os.environ.get("CLAUDE_BIN"):
        return os.path.expanduser(os.environ["CLAUDE_BIN"])
    home = Path.home()
    for c in (home / ".local" / "bin" / "claude", home / ".claude" / "local" / "claude"):
        if os.access(c, os.X_OK):
            return str(c)
    found = shutil.which("claude")
    if found:
        return found
    for c in (Path("/usr/local/bin/claude"), home / ".npm-global" / "bin" / "claude"):
        if os.access(c, os.X_OK):
            return str(c)
    return "claude"


def _csv(v: str) -> list[str]:
    return [t.strip() for t in v.split(",") if t.strip()]


def _env_or(name: str, default: str) -> str:
    v = os.environ.get(name)
    return default if v is None else v


def lockdown() -> bool:
    """CLAUDE_LOCKDOWN=0 turns the isolation flags and env off (debugging only)."""
    return os.environ.get("CLAUDE_LOCKDOWN", "1").strip().lower() not in ("0", "false", "no", "off")


def allowed_tools(caller: str = "chat") -> list[str]:
    """Tools that run without a permission prompt. Override with CLAUDE_ALLOWED_TOOLS (chat) or
    CLAUDE_ALLOWED_TOOLS_REVIEW (comma-separated; "" = none)."""
    name = "CLAUDE_ALLOWED_TOOLS" if caller == "chat" else f"CLAUDE_ALLOWED_TOOLS_{caller.upper()}"
    return _csv(_env_or(name, ALLOWED.get(caller, "")))


def available_tools(caller: str = "chat") -> str:
    """The --tools value. Override with CLAUDE_TOOLS_CHAT / CLAUDE_TOOLS_REVIEW ("" = no tools)."""
    return _env_or(f"CLAUDE_TOOLS_{caller.upper()}", TOOLS.get(caller, ""))


def disallowed_tools(caller: str = "chat") -> list[str]:
    """Deny rules (win over allow). Override with CLAUDE_DISALLOWED_TOOLS_CHAT / _REVIEW."""
    return _csv(_env_or(f"CLAUDE_DISALLOWED_TOOLS_{caller.upper()}", DISALLOWED.get(caller, "")))


def claude_env() -> dict[str, str]:
    """Environment for the subprocess: ours plus LOCKDOWN_ENV."""
    env = dict(os.environ)
    if lockdown():
        env.update(LOCKDOWN_ENV)
    return env


def claude_cmd(output_format: str, resume: str | None = None, caller: str = "chat",
               json_schema: dict | None = None) -> list[str]:
    """`claude -p` with the prompt fed on stdin (no argv length limit, no flag-parsing surprises
    if the message starts with '-'). Run it with env=claude_env() and cwd=config.ROOT (the
    allow-listed `.venv/bin/python tools/*` paths are relative to it).

    The model is per caller (config.claude_model: CLAUDE_MODEL_CHAT / CLAUDE_MODEL_REVIEW, else
    CLAUDE_MODEL, default sonnet) so scripted calls never inherit the CLI's default tier.
    List-valued flags are passed as one comma-separated argument: they are variadic in the CLI."""
    cmd = [claude_bin(), "-p", "--output-format", output_format]
    model = config.claude_model(caller)
    if model:
        cmd += ["--model", model]
    if lockdown():
        cmd += [*LOCKDOWN_FLAGS, "--tools", available_tools(caller)]
        deny = disallowed_tools(caller)
        if deny:
            cmd += ["--disallowedTools", ",".join(deny)]
        instructions = project_instructions() if PROJECT_INSTRUCTIONS.get(caller) else ""
        if instructions:
            cmd += ["--append-system-prompt", "# Project instructions (CLAUDE.md)\n\n" + instructions]
    if json_schema is not None:
        cmd += ["--json-schema", json.dumps(json_schema, separators=(",", ":"))]
    if caller != "chat":
        cmd += ["--no-session-persistence"]  # one-shot: nothing will --resume it
    tools = allowed_tools(caller)
    if tools:
        cmd += ["--allowedTools", ",".join(tools)]
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


def run_json(prompt: str, resume: str | None = None, timeout: int = TIMEOUT_S, caller: str = "chat",
             json_schema: dict | None = None) -> tuple[bool, str, dict | None]:
    """Run `claude -p --output-format json`. -> (ok, text, result document or None).
    Shared by chat and tools/review.py so both record usage the same way. With json_schema the
    validated answer is the document's `structured_output`; `result` may then be empty."""
    try:
        cp = subprocess.run(claude_cmd("json", resume, caller=caller, json_schema=json_schema), input=prompt,
                            cwd=str(config.ROOT), env=claude_env(), capture_output=True, text=True, timeout=timeout)
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
        answered = bool(text) or (json_schema is not None and out.get("structured_output") is not None)
        ok = cp.returncode == 0 and not out.get("is_error") and answered
        return ok, text or ("" if ok else (cp.stderr.strip() or "empty reply")), out
    return False, (cp.stderr.strip() or cp.stdout.strip() or f"claude exited {cp.returncode}")[-2000:], None


def context_tokens(out: dict | None) -> int:
    """Prompt size of the last model request in a call, i.e. roughly what the next resumed
    message starts from. `usage` sums every request of a multi-step call, so use its last
    `iterations` entry when present, else the average per request."""
    u = (out or {}).get("usage") or {}
    its = [i for i in (u.get("iterations") or []) if isinstance(i, dict)]
    last = its[-1] if its else u
    total = sum(int(last.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    turns = int((out or {}).get("num_turns") or 1)
    if not its and turns > 1:
        total //= turns
    return total


def should_rotate(conn) -> str | None:
    """Why the stored session should not be resumed (too many messages, too much context), or None."""
    turns = int(get_setting(conn, TURNS_KEY) or 0)
    ctx = int(get_setting(conn, CONTEXT_KEY) or 0)
    if turns >= CHAT_MAX_TURNS:
        return f"{turns} messages in session"
    if ctx >= CHAT_MAX_CONTEXT_TOKENS:
        return f"context reached {ctx} tokens"
    return None


def carry_over(conn) -> str:
    """Recap of the last CARRY_ROWS user/assistant rows before the message being sent, to start a
    fresh session with. Empty when there is no history."""
    rs = history(conn, CARRY_ROWS + 1)[:-1]  # the newest row is the message being sent
    rs = [r for r in rs if r["role"] in ("user", "assistant")]
    if not rs:
        return ""
    lines = []
    for r in rs:
        c = " ".join(str(r["content"]).split())
        cut = c[:CARRY_CHARS] + ("…" if len(c) > CARRY_CHARS else "")
        lines.append(f"{'me' if r['role'] == 'user' else 'you'}: {cut}")
    return ("[Fresh session; the previous one was closed to keep context small. Our last few messages, "
            "oldest first, for continuity only; the journal and tools are the source of truth.]\n"
            + "\n".join(lines) + "\n[End of recap]\n\n")


def _run(prompt: str, resume: str | None) -> tuple[bool, str, str | None, dict | None]:
    """-> (ok, text, session_id, result document)."""
    ok, text, out = run_json(prompt, resume)
    return ok, text, (out or {}).get("session_id"), out


def send(conn, message: str) -> dict:
    """Store the user message, ask Claude (resuming the stored session unless it is due for
    rotation), store the reply. A fresh session gets a recap of the last few rows."""
    message = (message or "").strip()
    if not message:
        return {"ok": False, "reason": "empty message"}
    _add(conn, "user", message)
    session = get_setting(conn, SESSION_KEY)
    rotated = should_rotate(conn) if session else None
    ok, resumed = False, False
    if session and not rotated:
        ok, text, sid, out = _run(message, session)
        record_usage(conn, "chat", out, ok)
        resumed = ok
    if not resumed:  # first message, rotation, or a stale/broken session: start over
        ok, text, sid, out = _run(carry_over(conn) + message, None)
        record_usage(conn, "chat", out, ok)
    if sid:
        turns = int(get_setting(conn, TURNS_KEY) or 0) + 1 if resumed else 1
        set_setting(conn, SESSION_KEY, sid)
        set_setting(conn, TURNS_KEY, str(turns))
        set_setting(conn, CONTEXT_KEY, str(context_tokens(out)))
    _add(conn, "assistant" if ok else "error", text)
    return {"ok": ok, "reply": text, "session_id": sid, "resumed": resumed, "rotated": rotated,
            "cost_usd": (out or {}).get("total_cost_usd")}
