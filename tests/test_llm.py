"""LLM backend switch: trader/llm.py, tools/review.py on Ollama, web/ollama_chat.py (in-process chat
tool loop and its guard). HTTP (requests.post) and subprocess are mocked; no network, no claude."""
from __future__ import annotations

import json
import subprocess

import pytest
import requests

from conftest import insert_thesis
from trader import client as tclient
from trader import config, db, llm
from web import chat, ollama_chat

SPEC = {"id": "aapl-msft", "name": "AAPL and MSFT", "rationale": "steady", "universe": ["AAPL", "MSFT"],
        "capital_cap": 100, "max_position_pct": 60,
        "schedule": {"type": "dca", "amount": 20, "cron": "0 10 * * 1"}, "source_text": "$20 a week"}


class StubBroker:
    def __init__(self, *a, **k):
        pass

    def positions(self):
        return [{"symbol": "NVDA", "qty": "0.5", "avg_entry_price": "100", "current_price": "120",
                 "market_value": "60", "cost_basis": "50", "unrealized_pl": "10", "unrealized_plpc": "0.2"}]

    def reconcile(self):
        return {"updated": 0, "positions": self.positions()}

    def account(self):
        return {"equity": "1000", "cash": "900", "buying_power": "900"}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr(tclient, "BrokerClient", StubBroker)
    monkeypatch.setattr(config, "LLM_BACKEND", "claude")
    monkeypatch.setattr(config, "LLM_BACKEND_CHAT", None)
    monkeypatch.setattr(config, "LLM_BACKEND_REVIEW", None)
    monkeypatch.setattr(config, "OLLAMA_URL", "http://primary:11434")
    monkeypatch.setattr(config, "OLLAMA_FALLBACK_URL", "http://fallback:11434")
    monkeypatch.delenv("CHAT_TOOLS_OLLAMA", raising=False)


@pytest.fixture
def conn():
    c = db.connect()
    yield c
    c.close()


class FakeOllama:
    """requests.post replacement: replies are dicts (message payload) or exceptions, in order."""

    def __init__(self, monkeypatch, *replies, down=()):
        self.calls, self.replies, self.down = [], list(replies), set(down)
        monkeypatch.setattr(llm.requests, "post", self.post)

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "body": json, "timeout": timeout})
        host = url.split("/api")[0]
        if host in self.down:
            raise requests.ConnectionError("refused")
        r = self.replies[min(len(self.calls), len(self.replies)) - 1]
        if isinstance(r, Exception):
            raise r
        resp = requests.Response()
        resp.status_code = r.get("status", 200)
        resp._content = __import__("json").dumps(r.get("body", {})).encode()
        return resp


def reply(content="", tool_calls=None, p=100, e=10, **kw):
    msg = {"role": "assistant", "content": content, **kw}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return {"body": {"message": msg, "prompt_eval_count": p, "eval_count": e}}


def call(name, args=None, stdin=None, raw=None):
    a = raw if raw is not None else {"name": name, "args": args or [], "stdin": stdin}
    return [{"function": {"name": "run_tool", "arguments": a}}]


class FakeScripts:
    def __init__(self, monkeypatch, out='{"ok": true}', code=0):
        self.runs = []
        self.out, self.code = out, code

        def run(cmd, **kw):
            if isinstance(cmd, list) and "tools" in str(cmd[1]):
                self.runs.append((cmd, kw))
                return subprocess.CompletedProcess(cmd, self.code, stdout=self.out, stderr="")
            raise AssertionError(f"unexpected subprocess {cmd}")

        monkeypatch.setattr(ollama_chat.subprocess, "run", run)


def send(conn, text):
    return chat.send(conn, text)


# ---------------------------------------------------------------- config / llm.py

def test_backend_selection_and_overrides(monkeypatch):
    assert config.llm_backend("chat") == "claude" and config.llm_backend("review") == "claude"
    monkeypatch.setattr(config, "LLM_BACKEND", "ollama")
    assert config.llm_backend("chat") == config.llm_backend("review") == "ollama"
    monkeypatch.setattr(config, "LLM_BACKEND_REVIEW", "Claude")
    assert config.llm_backend("review") == "claude" and config.llm_backend("chat") == "ollama"
    monkeypatch.setattr(config, "LLM_BACKEND_CHAT", "gpt")
    with pytest.raises(ValueError, match="unknown LLM backend 'gpt'"):
        config.llm_backend("chat")


def test_ollama_models_default_to_ollama_model(monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_MODEL", "m1")
    monkeypatch.setattr(config, "OLLAMA_CHAT_MODEL", "")
    monkeypatch.setattr(config, "OLLAMA_REVIEW_MODEL", "m2")
    assert config.ollama_model("chat") == "m1" and config.ollama_model("review") == "m2"


def test_chat_unknown_backend_is_a_clear_error(conn, monkeypatch):
    monkeypatch.setattr(config, "LLM_BACKEND_CHAT", "bogus")
    r = send(conn, "hi")
    assert r["ok"] is False and "unknown LLM backend" in r["reply"]


def test_think_stripped_and_usage_returned(monkeypatch):
    f = FakeOllama(monkeypatch, reply("<think>hmm\nlots</think>\nanswer", p=7, e=3))
    r = llm.ollama_chat([{"role": "user", "content": "x"}], model="m")
    assert r["message"]["content"] == "answer" and (r["prompt_tokens"], r["eval_tokens"]) == (7, 3)
    b = f.calls[0]["body"]
    assert b["stream"] is False and b["think"] is False and b["options"]["temperature"] <= 0.2
    assert f.calls[0]["url"] == "http://primary:11434/api/chat" and b["model"] == "m"


def test_think_flag_retried_without_on_400(monkeypatch):
    f = FakeOllama(monkeypatch, {"status": 400, "body": {"error": "does not support thinking"}}, reply("ok"))
    r = llm.ollama_chat([], model="m")
    assert r["message"]["content"] == "ok" and "think" not in f.calls[1]["body"]


def test_fallback_host_used_when_primary_down(monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_FALLBACK_MODEL", "small")
    f = FakeOllama(monkeypatch, reply("fine"), down={"http://primary:11434"})
    r = llm.ollama_chat([], model="big")
    assert r["url"] == "http://fallback:11434" and r["model"] == "small"
    assert [c["body"]["model"] for c in f.calls] == ["big", "small"]


def test_timeout_falls_back_and_all_down_raises(monkeypatch):
    FakeOllama(monkeypatch, requests.Timeout("slow"), reply("later"))
    assert llm.ollama_chat([], model="m")["message"]["content"] == "later"
    FakeOllama(monkeypatch, requests.ConnectionError("x"))
    with pytest.raises(llm.LLMError, match="not reachable"):
        llm.ollama_chat([], model="m")


# ---------------------------------------------------------------- reviews

def review_answer(rec="HOLD", lines=("Fine.",), proposal=None):
    return json.dumps({"recommendation": rec, "lines": list(lines), "proposal": proposal})


def setup_review(conn, mode="confirm"):
    insert_thesis(config.DB_PATH, mode=mode)
    conn.execute("INSERT INTO orders(client_order_id, thesis_id, symbol, side, notional, status, filled_qty, "
                 "filled_avg_price, filled_notional, created, updated) VALUES ('c1','semis','NVDA','buy',50,'filled',0.5,100,50,?,?)",
                 (db.now_iso(), db.now_iso()))


def test_review_via_ollama_happy_path(conn, monkeypatch, capsys):
    from tools import review
    monkeypatch.setattr(config, "LLM_BACKEND_REVIEW", "ollama")
    monkeypatch.setattr(config, "OLLAMA_REVIEW_MODEL", "big")
    setup_review(conn)
    f = FakeOllama(monkeypatch, reply(review_answer(), p=5000, e=80))
    # the Claude CLI must not be touched
    monkeypatch.setattr(chat.subprocess, "run", lambda *a, **k: pytest.fail("claude was run"))
    assert review.main(["--thesis", "semis", "--force"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["review"] == "HOLD\nFine." and out["cost_usd"] == 0
    b = f.calls[0]["body"]
    assert b["format"] == review.REVIEW_SCHEMA and b["model"] == "big"
    assert [m["role"] for m in b["messages"]] == ["system", "user"]
    assert "Your task" in b["messages"][0]["content"] and "# Review: Semis" in b["messages"][1]["content"]
    u = db.row(conn, "SELECT * FROM claude_usage")
    assert u["caller"] == "review" and u["model"] == "ollama:big" and u["input_tokens"] == 5000
    assert u["output_tokens"] == 80 and u["cost_usd"] == 0 and u["ok"] == 1
    j = json.loads(db.row(conn, "SELECT data FROM journal WHERE kind='review'")["data"])
    assert j["model"] == "ollama:big"


def test_review_via_ollama_invalid_json_fails(conn, monkeypatch, capsys):
    from tools import review
    monkeypatch.setattr(config, "LLM_BACKEND", "ollama")
    setup_review(conn)
    FakeOllama(monkeypatch, reply("Sure! HOLD it."))
    assert review.main(["--thesis", "semis", "--force"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and "no structured output" in out["reason"]
    assert db.row(conn, "SELECT 1 FROM journal WHERE kind='error' AND message LIKE 'review of semis failed%'")
    assert db.get_setting(conn, "last_review:semis") is None


def test_review_via_ollama_ollama_down(conn, monkeypatch, capsys):
    from tools import review
    monkeypatch.setattr(config, "LLM_BACKEND", "ollama")
    setup_review(conn)
    FakeOllama(monkeypatch, reply("x"), down={"http://primary:11434", "http://fallback:11434"})
    assert review.main(["--thesis", "semis", "--force"]) == 1
    assert "not reachable" in json.loads(capsys.readouterr().out)["reason"]


def test_review_proposal_on_auto_thesis_stays_pending(conn, monkeypatch, capsys):
    from tools import review
    monkeypatch.setattr(config, "LLM_BACKEND", "ollama")
    setup_review(conn, mode="auto")
    sell = {"action": {"type": "order", "side": "sell", "symbol": "NVDA", "qty": 0.5}, "reason": "cut"}
    FakeOllama(monkeypatch, reply(review_answer("EXIT", ["Broken."], sell)))
    assert review.main(["--thesis", "semis", "--force"]) == 0
    p = json.loads(capsys.readouterr().out)["proposal"]
    assert p["status"] == "pending"


def test_review_claude_path_unchanged_when_backend_claude(conn, monkeypatch, capsys):
    from tools import review
    setup_review(conn)
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: pytest.fail("ollama was called"))
    so = {"recommendation": "HOLD", "lines": ["ok"], "proposal": None}
    doc = {"type": "result", "is_error": False, "result": json.dumps(so), "structured_output": so,
           "usage": {"input_tokens": 10, "output_tokens": 5}, "total_cost_usd": 0.01, "session_id": "s",
           "modelUsage": {"claude-sonnet": {"costUSD": 0.01}}}
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(doc), stderr="")

    monkeypatch.setattr(chat.subprocess, "run", run)
    assert review.main(["--thesis", "semis", "--force"]) == 0
    assert calls and "--json-schema" in calls[0]
    assert db.row(conn, "SELECT model FROM claude_usage")["model"] == "claude-sonnet"


def test_review_unknown_backend_reported(conn, monkeypatch, capsys):
    from tools import review
    monkeypatch.setattr(config, "LLM_BACKEND_REVIEW", "nope")
    setup_review(conn)
    assert review.main(["--thesis", "semis", "--force"]) == 1
    assert "unknown LLM backend" in json.loads(capsys.readouterr().out)["reason"]


def test_usage_tool_shows_ollama_rows(conn, monkeypatch):
    from tools import usage
    from web.chat import record_usage
    monkeypatch.setattr(config, "LLM_BACKEND_CHAT", "ollama")
    record_usage(conn, "chat", llm.usage_doc("qwen3-coder:30b", 1000, 50, 900, 2))
    s = usage.summary(conn)
    assert s["windows"]["7d"]["input_tokens"] == 1000 and s["windows"]["7d"]["cost_usd"] == 0
    assert s["by_model_7d"][0]["key"] == "ollama:qwen3-coder:30b"
    assert s["backends"] == {"chat": "ollama", "review": "claude"}
    assert s["models"]["chat"].startswith("ollama:")


# ---------------------------------------------------------------- chat loop

@pytest.fixture
def ochat(monkeypatch):
    monkeypatch.setattr(config, "LLM_BACKEND_CHAT", "ollama")
    monkeypatch.setattr(config, "OLLAMA_CHAT_MODEL", "chatm")
    monkeypatch.setattr(chat.subprocess, "run", lambda *a, **k: pytest.fail("claude was run"))


def test_chat_tool_call_then_answer(conn, ochat, monkeypatch):
    sc = FakeScripts(monkeypatch, out='{"theses": []}')
    f = FakeOllama(monkeypatch, reply(tool_calls=call("thesis_list")), reply("No theses yet.", p=200, e=20))
    r = send(conn, "what theses do we have?")
    assert r == {"ok": True, "reply": "No theses yet.", "session_id": None, "resumed": False, "rotated": None,
                 "cost_usd": 0.0}
    cmd, kw = sc.runs[0]
    assert cmd[1].endswith("tools/thesis_list.py") and kw["cwd"] == str(config.ROOT) and kw["shell"] is False
    assert kw["timeout"] == 60
    # second request carries the tool result
    msgs = f.calls[1]["body"]["messages"]
    assert msgs[-1]["role"] == "tool" and '{"theses": []}' in msgs[-1]["content"]
    sysmsg = msgs[0]["content"]
    assert msgs[0]["role"] == "system" and "run_tool" in sysmsg and "broker" in sysmsg and "Today is" in sysmsg
    assert [t["function"]["name"] for t in f.calls[0]["body"]["tools"]] == ["run_tool"]
    assert db.rows(conn, "SELECT role FROM chat ORDER BY id") == [{"role": "user"}, {"role": "assistant"}]
    u = db.row(conn, "SELECT * FROM claude_usage")
    assert u["caller"] == "chat" and u["model"] == "ollama:chatm" and u["input_tokens"] == 300 and u["turns"] == 2
    j = db.row(conn, "SELECT message, data FROM journal WHERE kind='chat_tool'")
    assert j["message"].startswith("thesis_list") and json.loads(j["data"])["exit"] == 0


def test_chat_history_is_last_rows_only(conn, ochat, monkeypatch):
    for i in range(20):
        conn.execute("INSERT INTO chat(ts, role, content) VALUES (?,?,?)",
                     (db.now_iso(), "user" if i % 2 == 0 else "assistant", f"m{i}"))
    f = FakeOllama(monkeypatch, reply("ok"))
    send(conn, "latest")
    msgs = f.calls[0]["body"]["messages"]
    assert len(msgs) == 1 + ollama_chat.HISTORY_ROWS and msgs[-1]["content"] == "latest"


def test_chat_step_cap_forces_final_answer(conn, ochat, monkeypatch):
    FakeScripts(monkeypatch)
    f = FakeOllama(monkeypatch, *[reply(tool_calls=call("positions")) for _ in range(ollama_chat.MAX_STEPS)],
                   reply("Here is what I have."))
    r = send(conn, "loop forever")
    assert r["ok"] and r["reply"] == "Here is what I have."
    assert len(f.calls) == ollama_chat.MAX_STEPS + 1
    assert "tools" not in f.calls[-1]["body"]
    assert "step limit" in f.calls[-1]["body"]["messages"][-1]["content"]


@pytest.mark.parametrize("name,args,why", [
    ("order", ["--symbol", "NVDA"], "propose.py"),
    ("halt", [], "kill switch"),
    ("thesis_resume", ["semis"], "proposal"),
    ("run_thesis", [], "scheduled job"),
    ("review", [], "not available"),
    ("snapshot", [], "not available"),
    ("../tools/order", [], "lowercase"),
    ("tools/order", [], "lowercase"),
    ("_common", [], "lowercase"),
    ("positions.py", [], "lowercase"),
    ("nosuchtool", [], "not an available tool"),
    ("news", ["NVDA"], "--no-digest"),
    ("backtest", ["--thesis", "x", "--db", "/etc/passwd"], "no --db"),
    ("backtest", ["--spec", "/etc/passwd"], "no --db or file"),
])
def test_chat_refusals(conn, ochat, monkeypatch, name, args, why):
    sc = FakeScripts(monkeypatch)
    f = FakeOllama(monkeypatch, reply(tool_calls=call(name, args)), reply("Refused, sorry."))
    r = send(conn, "do it")
    assert r["ok"] and sc.runs == []
    tool_msg = f.calls[1]["body"]["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["content"].startswith("refused") and why in tool_msg["content"]
    j = db.row(conn, "SELECT data FROM journal WHERE kind='chat_tool'")
    assert json.loads(j["data"])["refused"] is True


def test_env_override_cannot_lift_hard_denies(conn, ochat, monkeypatch):
    monkeypatch.setenv("CHAT_TOOLS_OLLAMA", "order,halt,thesis_resume,positions")
    assert ollama_chat.allowed_names() == ["positions"]
    sc = FakeScripts(monkeypatch)
    FakeOllama(monkeypatch, reply(tool_calls=call("order")), reply("no"))
    send(conn, "buy")
    assert sc.runs == []
    monkeypatch.setenv("CHAT_TOOLS_OLLAMA", "positions")
    FakeOllama(monkeypatch, reply(tool_calls=call("thesis_pause", ["x"])), reply("no"))
    send(conn, "pause")
    assert sc.runs == []  # not in the narrowed allowlist


def test_propose_is_forced_to_confirm_and_news_nodigest_ok(conn, ochat, monkeypatch):
    sc = FakeScripts(monkeypatch)
    FakeOllama(monkeypatch, reply(tool_calls=call("propose", ["--thesis", "manual", "--action", "{}", "--reason", "r"])),
               reply(tool_calls=call("news", ["NVDA", "--no-digest"])), reply("done"))
    send(conn, "go")
    assert "--force-confirm" in sc.runs[0][0] and sc.runs[1][0][-2:] == ["NVDA", "--no-digest"]


def test_malformed_tool_calls_are_reported_and_loop_continues(conn, ochat, monkeypatch):
    sc = FakeScripts(monkeypatch)
    bad = [{"function": {"name": "shell", "arguments": {"cmd": "ls"}}}]
    f = FakeOllama(monkeypatch, reply(tool_calls=bad), reply(tool_calls=call("x", raw="{not json")),
                   reply(tool_calls=call("x", raw={"args": []})), reply(tool_calls=call("positions", raw='{"name": "positions"}')),
                   reply("recovered"))
    r = send(conn, "go")
    assert r["reply"] == "recovered" and len(sc.runs) == 1
    tools = [m["content"] for m in f.calls[-1]["body"]["messages"] if m["role"] == "tool"]
    assert tools[0].startswith("error: unknown function") and "not valid JSON" in tools[1] and "needs" in tools[2]


def test_ollama_down_gives_clear_reply(conn, ochat, monkeypatch):
    FakeOllama(monkeypatch, reply("x"), down={"http://primary:11434", "http://fallback:11434"})
    r = send(conn, "hello")
    assert r["ok"] is False and "not reachable" in r["reply"]
    assert db.rows(conn, "SELECT role FROM chat ORDER BY id") == [{"role": "user"}, {"role": "error"}]


def test_tool_output_truncated(conn, ochat, monkeypatch):
    FakeScripts(monkeypatch, out="x" * 20000)
    f = FakeOllama(monkeypatch, reply(tool_calls=call("positions")), reply("ok"))
    send(conn, "go")
    c = f.calls[1]["body"]["messages"][-1]["content"]
    assert len(c) < 6300 and "truncated" in c


# ---------------------------------------------------------------- thesis_create guard

def create_call(spec, dry):
    return call("thesis_create", ["-", "--dry-run"] if dry else ["-"], json.dumps(spec))


def test_create_real_without_dryrun_refused(conn, ochat, monkeypatch):
    sc = FakeScripts(monkeypatch)
    f = FakeOllama(monkeypatch, reply(tool_calls=create_call(SPEC, False)), reply("refused"))
    send(conn, "create it")
    assert sc.runs == [] and "--dry-run first" in f.calls[1]["body"]["messages"][-1]["content"]


def test_create_same_turn_refused(conn, ochat, monkeypatch):
    sc = FakeScripts(monkeypatch)
    f = FakeOllama(monkeypatch, reply(tool_calls=create_call(SPEC, True)), reply(tool_calls=create_call(SPEC, False)),
                   reply("waiting"))
    send(conn, "create a thesis")
    assert len(sc.runs) == 1 and "--dry-run" in sc.runs[0][0]  # only the dry-run ran
    assert "same turn" in f.calls[2]["body"]["messages"][-1]["content"]


def test_create_with_different_spec_refused(conn, ochat, monkeypatch):
    sc = FakeScripts(monkeypatch)
    FakeOllama(monkeypatch, reply(tool_calls=create_call(SPEC, True)), reply("read-back ..."))
    send(conn, "create a thesis")
    other = dict(SPEC, capital_cap=500)
    f = FakeOllama(monkeypatch, reply(tool_calls=create_call(other, False)), reply("no"))
    send(conn, "yes")
    assert len(sc.runs) == 1 and "differs" in f.calls[1]["body"]["messages"][-1]["content"]


def test_create_allowed_next_turn_same_spec_and_single_use(conn, ochat, monkeypatch):
    sc = FakeScripts(monkeypatch)
    FakeOllama(monkeypatch, reply(tool_calls=create_call(SPEC, True)), reply("read-back ..."))
    send(conn, "create a thesis")
    # same spec with an explicit default filled in still hashes the same (normalized)
    again = dict(SPEC, status="active")
    FakeOllama(monkeypatch, reply(tool_calls=create_call(again, False)), reply("created"))
    r = send(conn, "yes")
    assert r["reply"] == "created" and len(sc.runs) == 2 and "--dry-run" not in sc.runs[1][0]
    assert json.loads(sc.runs[1][1]["input"])["id"] == "aapl-msft"
    f = FakeOllama(monkeypatch, reply(tool_calls=create_call(SPEC, False)), reply("no"))
    send(conn, "again")
    assert len(sc.runs) == 2 and "dry-run first" in f.calls[1]["body"]["messages"][-1]["content"]


def test_create_dryrun_failure_does_not_unlock(conn, ochat, monkeypatch):
    sc = FakeScripts(monkeypatch, out='{"ok": false}', code=1)
    FakeOllama(monkeypatch, reply(tool_calls=create_call(SPEC, True)), reply("bad"))
    send(conn, "create")
    assert db.get_setting(conn, "chat:dryrun") is None
    FakeOllama(monkeypatch, reply(tool_calls=create_call(SPEC, False)), reply("no"))
    send(conn, "yes")
    assert len(sc.runs) == 1


def test_create_requires_stdin_dash(conn, ochat, monkeypatch):
    sc = FakeScripts(monkeypatch)
    f = FakeOllama(monkeypatch, reply(tool_calls=call("thesis_create", ["/etc/passwd", "--dry-run"])), reply("x"))
    send(conn, "go")
    assert sc.runs == [] and "args" in f.calls[1]["body"]["messages"][-1]["content"]


# ---------------------------------------------------------------- claude path untouched

def test_claude_path_untouched_when_backend_claude(conn, monkeypatch):
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: pytest.fail("ollama was called"))
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        out = {"type": "result", "is_error": False, "result": "hi", "session_id": "s1"}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(out), stderr="")

    monkeypatch.setattr(chat.subprocess, "run", run)
    r = send(conn, "hello")
    assert r["reply"] == "hi" and r["session_id"] == "s1" and calls and calls[0][1:3] == ["-p", "--output-format"]
    assert db.get_setting(conn, "chat_session") == "s1"


def test_next_turn_system_prompt_carries_the_dryrun_spec(conn, ochat, monkeypatch):
    FakeScripts(monkeypatch)
    f1 = FakeOllama(monkeypatch, reply(tool_calls=create_call(SPEC, True)), reply("read-back"))
    send(conn, "create a thesis")
    assert "dry-run of this spec" not in f1.calls[0]["body"]["messages"][0]["content"]
    f2 = FakeOllama(monkeypatch, reply("ok"))
    send(conn, "yes")
    sysmsg = f2.calls[0]["body"]["messages"][0]["content"]
    assert "dry-run of this spec" in sysmsg and '"id":"aapl-msft"' in sysmsg


def test_review_via_ollama_retries_once_when_model_answers_in_prose(conn, monkeypatch, capsys):
    from tools import review
    monkeypatch.setattr(config, "LLM_BACKEND", "ollama")
    setup_review(conn)
    f = FakeOllama(monkeypatch, reply("Prose answer.", p=100, e=5), reply(review_answer(), p=150, e=20))
    assert review.main(["--thesis", "semis", "--force"]) == 0
    assert len(f.calls) == 2 and "not a JSON object" in f.calls[1]["body"]["messages"][-1]["content"]
    u = db.row(conn, "SELECT input_tokens, output_tokens FROM claude_usage")
    assert (u["input_tokens"], u["output_tokens"]) == (250, 25)
