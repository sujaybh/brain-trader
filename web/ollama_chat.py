"""Dashboard chat on Ollama (LLM_BACKEND_CHAT=ollama): an in-process tool loop instead of `claude -p`.

The model gets exactly one function tool, run_tool(name, args, stdin), which runs
`<venv python> tools/<name>.py <args>` (no shell, cwd = project root). A small local model will
make mistakes, so the limits live here in code, not in the prompt (see check_call):
  - an allowlist of tools (CHAT_TOOLS_OLLAMA overrides it) minus a hard deny list that no setting
    can lift: order.py (trades go through propose.py and a phone tap), halt.py, thesis_resume
    (resume only as a proposal), and every job/maintenance script;
  - propose.py always runs with --force-confirm, so even a thesis in mode `auto` waits for the tap;
  - thesis_create: a real create needs a --dry-run of the identical spec made in an EARLIER user
    turn (the user saw the read-back and replied);
  - every invocation, allowed or refused, is journaled as kind `chat_tool`.
History is the last HISTORY_ROWS chat rows; there are no session ids."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime

from pydantic import ValidationError

from trader import config, llm
from trader.db import get_setting, journal, now_iso, rows, set_setting
from trader.spec import ThesisSpec

HISTORY_ROWS = 12
MAX_STEPS = 8
TOOL_TIMEOUT_S = 60
OUTPUT_CHARS = 6000
LLM_TIMEOUT_S = 240
DRYRUN_KEY = "chat:dryrun"

DEFAULT_TOOLS = ("positions", "journal", "quote", "thesis_list", "thesis_show", "usage", "news", "backtest",
                 "propose", "thesis_create", "thesis_pause")
# Refused whatever CHAT_TOOLS_OLLAMA says.
HARD_DENY = {
    "order": "order.py is not available in this chat; place the trade with propose.py so it needs a phone tap",
    "halt": "halt.py (the kill switch) belongs to the user; tell them to run it",
    "thesis_resume": "resume only through a proposal: propose.py with action {\"type\":\"resume\"}",
    "run_thesis": "scheduled job; not available in chat", "run_strategy": "scheduled job; not available in chat",
    "bars": "maintenance job; not available in chat", "snapshot": "maintenance job; not available in chat",
    "review": "reviews run on their own schedule; not available in chat",
    "live_check": "not available in chat", "notify": "not available in chat",
    "check_triggers": "scheduled job; not available in chat",
}
NAME_RE = re.compile(r"^[a-z_]+$")

TOOL_SPEC = [{
    "type": "function",
    "function": {
        "name": "run_tool",
        "description": ("Run one of the project's tools/<name>.py scripts and get its JSON output. "
                        "name is the script name without .py (positions, journal, quote, thesis_list, thesis_show, "
                        "usage, news, backtest, propose, thesis_create, thesis_pause). args is the list of "
                        "command-line arguments. stdin is text piped to the script (thesis_create reads the spec "
                        "JSON from stdin with args [\"-\"]) or null."),
        "parameters": {"type": "object", "required": ["name"], "properties": {
            "name": {"type": "string"},
            "args": {"type": "array", "items": {"type": "string"}},
            "stdin": {"type": ["string", "null"]}}},
    },
}]

TOOL_GUIDE = """\
# How to use your tool

You have one function, run_tool(name, args, stdin). Scripts print JSON. Start a conversation about
holdings with positions. Available scripts and their arguments:
- positions            reconciled positions and account (no args)
- thesis_list          all theses; thesis_show <id>: one thesis, its orders and journal
- journal [--since 7d] [--thesis ID] [--kind KIND] [--limit N]
- quote SYM [SYM...]   latest prices
- news SYM [SYM...] --no-digest [--since 24h]   raw headlines only
- backtest --thesis ID | --spec -  (spec JSON on stdin)   how a strategy would have done
- usage                LLM usage and budget
- propose --thesis ID --action '<json>' --reason '<text>'   proposes a trade/pause/resume/spec_change; it only
  creates a request, the user approves on the phone. Actions: {"type":"order","side":"buy|sell","symbol":"NVDA",
  "notional":50} (or "qty"), {"type":"pause"}, {"type":"resume"}, {"type":"spec_change","patch":{"capital_cap":400}}.
  Use thesis "manual" for a trade with no thesis.
- thesis_pause <id>
- thesis_create - [--dry-run]   spec JSON on stdin. ALWAYS run it with --dry-run first, show the user the
  "describe" paragraph, and wait for their reply. Only after they say yes, in their NEXT message, call it again
  without --dry-run with the SAME spec. Do not change anything between the two.
This section overrides the project instructions above wherever they differ: trades from chat are NOT placed with
order.py, always propose them with propose.py.
Not available here: order.py (use propose), halt, resume (use propose), scheduled jobs. If a call is refused,
tell the user why in plain words; do not try to work around it.
Never invent a ticker. Keep answers short; the user reads on a phone.
"""

SPEC_GUIDE = """\
# Thesis spec (JSON for thesis_create)
{"id":"semis-q4","name":"Big semiconductors","rationale":"why","universe":["NVDA","AMD"],"capital_cap":300,
 "max_position_pct":60,"schedule":{"type":"dca","amount":50,"cron":"0 10 * * 1","tz":"America/New_York"},
 "entry":{"type":"schedule_only"},"exit":{"stop_loss_pct":20,"take_profit_pct":null,"review_only":true},
 "triggers":[{"type":"drawdown_pct","value":15,"action":"review"}],"mode":"confirm","review":"monthly",
 "status":"active","source_text":"the user's words, verbatim"}
id: slug [a-z0-9-], 2-40 chars. universe: 1-25 real tickers. capital_cap: USD ever deployed. max_position_pct:
% of the cap in one name. schedule.type: dca (amount split equally over the universe on the 5-field cron, in
America/New_York) or none. entry.type: schedule_only or manual. triggers: drawdown_pct, news, price_below,
price_above; action review|pause|propose_exit. mode: confirm (default) or auto. review: weekly|monthly|quarterly.
source_text: the user's words verbatim. Weekly Monday 10:00 = "0 10 * * 1".
"""


def pending_dryrun(conn, user_row_id: int) -> dict | None:
    """The dry-run recorded in an earlier user turn, if any (the model cannot see its own earlier
    tool calls, so the spec is handed back to it verbatim)."""
    try:
        rec = json.loads(get_setting(conn, DRYRUN_KEY) or "null")
    except ValueError:
        return None
    if isinstance(rec, dict) and rec.get("spec") and int(rec.get("user_row") or 0) < user_row_id:
        return rec
    return None


def system_prompt(conn=None, user_row_id: int = 0, today: datetime | None = None) -> str:
    from web.chat import project_instructions
    today = today or datetime.now(config.MARKET_TZ)
    parts = [project_instructions(), TOOL_GUIDE, SPEC_GUIDE, f"Today is {today:%A %Y-%m-%d}."]
    rec = pending_dryrun(conn, user_row_id) if conn is not None else None
    if rec:
        parts.append("A thesis_create --dry-run of this spec was shown to the user earlier. If the user's new "
                     "message confirms it, call run_tool with name thesis_create, args [\"-\"] and exactly this "
                     "text as stdin, unchanged. If they want changes, dry-run the changed spec instead; if they "
                     "moved on, ignore this.\n" + json.dumps(rec["spec"], separators=(",", ":")))
    return "\n\n".join(parts)


# ---------------------------------------------------------------- guard

def allowed_names() -> list[str]:
    import os
    raw = os.environ.get("CHAT_TOOLS_OLLAMA")
    names = DEFAULT_TOOLS if raw is None else tuple(t.strip() for t in raw.split(",") if t.strip())
    return [n for n in names if n not in HARD_DENY]


def canon_spec(raw: str) -> dict:
    """The spec as ThesisSpec normalizes it (defaults filled in), so the dry-run's echoed spec and
    the model's own text compare equal. Raises ValueError if invalid."""
    try:
        return ThesisSpec(**json.loads(raw)).model_dump(mode="json")
    except (ValueError, TypeError, ValidationError) as e:
        raise ValueError(f"the spec is not valid: {e}") from None


def spec_hash(raw: str) -> str:
    """sha256 of the canonical JSON of canon_spec(raw)."""
    return hashlib.sha256(json.dumps(canon_spec(raw), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _spec_arg(args: list[str]) -> str | None:
    for i, a in enumerate(args):
        if a == "--spec" and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--spec="):
            return a[7:]
    return None


def check_call(conn, name, args, stdin, user_row_id: int) -> tuple[str | None, list[str]]:
    """Decide whether run_tool(name, args, stdin) may run. -> (refusal message or None, final args).
    Pure of side effects except reading settings."""
    if not isinstance(name, str) or not NAME_RE.match(name) or name.startswith("_"):
        return "refused: tool name must be lowercase letters and underscores, like positions or thesis_list", []
    if name in HARD_DENY:
        return f"refused: {HARD_DENY[name]}", []
    if name not in allowed_names():
        return f"refused: {name} is not an available tool. Available: {', '.join(allowed_names())}", []
    if not (config.ROOT / "tools" / f"{name}.py").is_file():
        return f"refused: there is no tool {name}", []
    if args is None:
        args = []
    if not isinstance(args, list) or not all(isinstance(a, (str, int, float)) and not isinstance(a, bool) for a in args):
        return "refused: args must be a list of strings", []
    args = [str(a) for a in args]
    if any("\x00" in a for a in args):
        return "refused: bad characters in args", []
    if stdin is not None and not isinstance(stdin, str):
        return "refused: stdin must be a string or null", []
    if name == "news" and "--no-digest" not in args:
        return "refused: news may only be run with --no-digest here (raw headlines)", []
    if name == "backtest":
        okflags = {"--spec", "--thesis", "--start", "--end", "--sweep", "--slippage-pct", "--trades", "--curve"}
        if any(a.startswith("--") and a.split("=")[0] not in okflags for a in args) or _spec_arg(args) not in (None, "-"):
            return "refused: backtest here takes --thesis ID or --spec - (spec on stdin); no --db or file paths", []
    if name == "propose":
        if "--force-confirm" not in args:
            args = [*args, "--force-confirm"]
    if name == "thesis_create":
        return _check_create(conn, args, stdin, user_row_id)
    return None, args


def _check_create(conn, args: list[str], stdin: str | None, user_row_id: int) -> tuple[str | None, list[str]]:
    dry = "--dry-run" in args
    if any(a not in ("-", "--dry-run") for a in args):
        return "refused: call thesis_create with args [\"-\"] (or [\"-\", \"--dry-run\"]) and the spec JSON in stdin", []
    args = ["-", *(["--dry-run"] if dry else [])]  # small models forget the "-"; the spec only ever comes from stdin
    if not stdin or not stdin.strip():
        return "refused: put the spec JSON in stdin", []
    try:
        h = spec_hash(stdin)
    except ValueError as e:
        return f"refused: {e}", []
    if dry:
        return None, args
    rec = None
    try:
        rec = json.loads(get_setting(conn, DRYRUN_KEY) or "null")
    except ValueError:
        pass
    if not isinstance(rec, dict) or not rec.get("hash"):
        return ("refused: run thesis_create with --dry-run first, show the user the read-back, and ask for "
                "their confirmation"), []
    if rec["hash"] != h:
        old, new = rec.get("spec") or {}, canon_spec(stdin)
        keys = sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))
        return (f"refused: this spec differs from the one that was dry-run (fields: {', '.join(keys) or '?'}). "
                "Send exactly the dry-run spec, unchanged. If the user wants changes, dry-run the new spec again, "
                "show the read-back, and wait for their yes"), []
    if int(rec.get("user_row") or 0) >= user_row_id:
        return ("refused: the dry-run was done in this same turn. Show the user the read-back now and wait for "
                "their reply; only create after they answer in a new message"), []
    return None, args


# ---------------------------------------------------------------- running

def _python() -> str:
    py = config.ROOT / ".venv" / "bin" / "python"
    return str(py) if py.exists() else sys.executable


def run_script(name: str, args: list[str], stdin: str | None) -> tuple[int | None, str]:
    """-> (exit code or None on timeout/spawn failure, text for the model)."""
    import os
    env = dict(os.environ, TRADER_DATA_DIR=str(config.DATA_DIR), TRADER_REEXEC="1")
    try:
        cp = subprocess.run([_python(), str(config.ROOT / "tools" / f"{name}.py"), *args], input=stdin or "",
                            cwd=str(config.ROOT), env=env, capture_output=True, text=True, timeout=TOOL_TIMEOUT_S,
                            shell=False)
    except subprocess.TimeoutExpired:
        return None, f"error: {name} did not finish within {TOOL_TIMEOUT_S}s"
    except OSError as e:
        return None, f"error: could not run {name}: {e}"
    text = (cp.stdout or "").strip()
    if cp.returncode != 0 and not text:
        text = (cp.stderr or "").strip()[-1500:] or f"exit {cp.returncode}"
    if len(text) > OUTPUT_CHARS:
        text = text[:OUTPUT_CHARS] + f"\n...[truncated, {len(text) - OUTPUT_CHARS} more chars]"
    return cp.returncode, f"exit code {cp.returncode}\n{text}"


def handle_call(conn, call: dict, user_row_id: int) -> str:
    """Execute (or refuse) one tool call from the model; journal it; -> the tool message."""
    fn = (call or {}).get("function") or {}
    if fn.get("name") != "run_tool":
        return f"error: unknown function {fn.get('name')!r}; the only function is run_tool(name, args, stdin)"
    a = fn.get("arguments")
    if isinstance(a, str):
        try:
            a = json.loads(a)
        except ValueError:
            return "error: arguments were not valid JSON; send {\"name\": ..., \"args\": [...], \"stdin\": null}"
    if not isinstance(a, dict) or "name" not in a:
        return "error: run_tool needs {\"name\": ..., \"args\": [...], \"stdin\": null}"
    name, args, stdin = a.get("name"), a.get("args"), a.get("stdin")
    refusal, final_args = check_call(conn, name, args, stdin, user_row_id)
    shown = {"name": name, "args": args if isinstance(args, list) else [], "stdin_chars": len(stdin or "") if isinstance(stdin, str) else 0}
    if refusal:
        journal(conn, "chat_tool", f"refused {name}: {refusal}", data={**shown, "refused": True})
        return refusal
    code, text = run_script(name, final_args, stdin if isinstance(stdin, str) else None)
    if name == "thesis_create" and code == 0:
        if "--dry-run" in final_args:
            set_setting(conn, DRYRUN_KEY, json.dumps({"hash": spec_hash(stdin), "spec": canon_spec(stdin), "user_row": user_row_id, "ts": now_iso()}))
        else:
            set_setting(conn, DRYRUN_KEY, "null")  # single use
    journal(conn, "chat_tool", f"{name} {' '.join(final_args)[:200]} -> exit {code}", data={**shown, "exit": code})
    return text


def _history(conn) -> list[dict]:
    rs = list(reversed(rows(conn, "SELECT * FROM chat ORDER BY id DESC LIMIT ?", (HISTORY_ROWS,))))
    return [{"role": r["role"], "content": str(r["content"])} for r in rs if r["role"] in ("user", "assistant")]


def run(conn, user_row_id: int) -> tuple[bool, str, dict]:
    """The tool loop for the newest user message (chat row `user_row_id`).
    -> (ok, reply text, stats {prompt_tokens, eval_tokens, steps, model, duration_ms})."""
    messages = [{"role": "system", "content": system_prompt(conn, user_row_id)}, *_history(conn)]
    model = config.ollama_model("chat")
    stats = {"prompt_tokens": 0, "eval_tokens": 0, "steps": 0, "model": model, "duration_ms": 0}

    def ask(tools):
        r = llm.ollama_chat(messages, model=model, tools=tools, timeout=LLM_TIMEOUT_S)
        stats["prompt_tokens"] += r["prompt_tokens"]
        stats["eval_tokens"] += r["eval_tokens"]
        stats["duration_ms"] += r["duration_ms"]
        stats["model"] = r["model"]
        return r["message"]

    try:
        for _ in range(MAX_STEPS):
            stats["steps"] += 1
            msg = ask(TOOL_SPEC)
            calls = msg.get("tool_calls") or []
            if not calls:
                return bool(msg.get("content")), msg.get("content") or "(the model gave an empty answer)", stats
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for c in calls:
                messages.append({"role": "tool", "tool_name": (c.get("function") or {}).get("name") or "run_tool",
                                 "content": handle_call(conn, c, user_row_id)})
        messages.append({"role": "user", "content": "[system] Tool step limit reached. Answer now, in a few short "
                         "lines, using only what the tools returned so far; do not call more tools."})
        msg = ask(None)
        return bool(msg.get("content")), msg.get("content") or "(the model gave an empty answer)", stats
    except llm.LLMError as e:
        return False, str(e), stats
