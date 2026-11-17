# Thesis Trader web app

Phone dashboard + chat for Thesis Trader, served over Tailscale. FastAPI + Jinja2, no JS framework.

## Run

```sh
.venv/bin/python -m web          # binds WEB_HOST:WEB_PORT from config/trader.env (default 0.0.0.0:8010)
```

In production it runs as `thesis-web.service` (see `scheduler/`). It refuses to start if
`WEB_TOKEN` is empty. It needs the broker daemon for account and positions. If the broker is
down, the pages still render and show "broker unreachable".

## The token

Every route, including `/static`, requires `WEB_TOKEN` from `config/trader.env`. You can pass it two ways:

- as a query parameter, `?token=...`. ntfy Approve/Reject buttons and the home-screen bookmark use this.
- as the `tt_token` cookie. The app sets it on the first request that carries a valid `?token=`,
  so later links inside the app don't need it.

A wrong or missing token gets a `401`. To revoke access, change `WEB_TOKEN` and restart the service.
Old bookmarks and any Approve buttons still pending in ntfy stop working.

The token travels inside ntfy notifications (Approve/Reject/Click URLs). Use an unguessable
`NTFY_TOPIC`, or a self-hosted ntfy server, and treat the token like a password.

## iPhone home screen

1. Put the iPhone on the tailnet: Tailscale app installed and connected.
2. In **Safari** open `http://100.64.0.19:8010/?token=YOUR_WEB_TOKEN`. Use the `WEB_URL` from
   `config/trader.env`; the MagicDNS name works too.
3. Tap **Share → Add to Home Screen**. It opens full-screen like an app. The saved URL includes
   `?token=`, so it authenticates even though home-screen apps keep their own cookie jar.

Dark mode follows the system setting.

## Pages and API

| route | what |
|---|---|
| `GET /` | account line (equity, cash, deployed vs global cap), pending proposals with Approve/Reject, theses (status, deployed/cap, next scheduled buy, last action), positions grouped by thesis with P&L, last 20 journal entries |
| `GET /thesis/{id}` | plain-English read-back, rationale, your original words, positions, pending proposals, journal, Pause/Resume, raw spec |
| `GET /journal?since=7d&kind=` | journal (notify entries hidden unless `kind=notify`) |
| `GET /chat`, `POST /chat` | conversation with Claude Code (last 30 messages) |
| `GET /api/state` | JSON of everything the dashboard shows |
| `POST /api/proposals/{id}/approve` / `reject` | JSON from ntfy/curl. From an HTML form it does a 303 back to the page |
| `POST /api/theses/{id}/pause` / `resume` | Pause/Resume button. Goes through a proposal that is approved at once, so it leaves the same audit trail |
| `POST /api/chat` `{"message": "..."}` | runs `claude -p` (cwd = project root, project `CLAUDE.md` appended, see Isolation) and returns `{"ok", "reply", "session_id", "resumed", "rotated"}` |

## Approve buttons over Tailscale

`tools/propose.py` pushes a notification with two ntfy **http actions**:
`POST {WEB_URL}/api/proposals/{id}/approve?token=...` and `.../reject?token=...`.
The **ntfy app on the phone** sends that POST, not the ntfy server. So the buttons only work
while the phone is connected to the tailnet and can reach `WEB_URL`. If Tailscale is off, the
tap fails and the proposal stays pending until it expires (24h by default). You can still
approve it from the dashboard once you are back on the tailnet. Approve on an expired, rejected
or already-executed proposal returns `409` and does nothing.

## Chat

A Claude Code session whose id is stored in `settings.chat_session` and passed as `--resume`.
If resuming fails, a fresh session starts. Replies are not streamed: the page waits up to 5
minutes. The transcript is in the `chat` table.

A resumed session re-sends its whole history (messages and tool output) on every model request,
so each message costs more than the last. After `CHAT_MAX_TURNS` (8) messages, or once the last
request's context reached `CHAT_MAX_CONTEXT_TOKENS` (60k), the next message opens a fresh session
that starts with a short recap of the last 6 chat rows. The constants are in `web/chat.py`; the
counters are `settings.chat_session_turns` / `chat_session_context`.

### Isolation

The scripted sessions (chat and `tools/review.py`) must not inherit the user's own Claude Code
setup: the global `~/.claude/CLAUDE.md`, auto-memory, plugins, skills, MCP servers and the
claude.ai account connectors (which include a brokerage; only the broker daemon may talk to one).
`web/chat.py: claude_cmd()` therefore passes `--strict-mcp-config` (no `--mcp-config`, so no MCP
servers), `--setting-sources project,local`, `--disable-slash-commands`,
`--exclude-dynamic-system-prompt-sections`, `--permission-mode dontAsk` and `--tools`, and sets
`ENABLE_CLAUDEAI_MCP_SERVERS=false`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` and
`CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`. The last one is needed because `--setting-sources` alone
still loads `~/.claude/CLAUDE.md`; the project `CLAUDE.md` is passed explicitly with
`--append-system-prompt` instead (chat only). `--bare` is not used: it refuses subscription login.

Chat gets the tools `Bash,Read,Grep,Glob`; only `Bash(.venv/bin/python tools/*)` and the read
tools run, everything else is denied, and `Read(./config/*.env)` is denied outright. Overrides
(comma-separated, in the service environment): `CLAUDE_TOOLS_CHAT`, `CLAUDE_ALLOWED_TOOLS`,
`CLAUDE_DISALLOWED_TOOLS_CHAT`; `CLAUDE_LOCKDOWN=0` drops the isolation for debugging. The binary
is `CLAUDE_BIN`, else `~/.local/bin/claude`. Money limits are enforced by the broker regardless
of what chat asks for.

Reviews get no tools at all: one turn, `--json-schema` structured output, and `tools/review.py`
creates any proposal itself (see its docstring), so a review can only propose.

### Model and usage

`claude -p` runs on the Claude subscription login, not an API key, and would otherwise inherit
the CLI's default model (which may be the most expensive one). The scripts pin `CLAUDE_MODEL`
from `config/trader.env` (default `sonnet`; set it to `haiku`, `opus`, or `""` to inherit).
`CLAUDE_MODEL_CHAT` and `CLAUDE_MODEL_REVIEW` override it per caller.

Every call's tokens and `total_cost_usd` (API list-price equivalent, as reported by the CLI) are
written to the `claude_usage` table. `tools/usage.py` sums them for today / 7d / 30d, by caller
and by model; the dashboard shows the 7-day figure. Set `CLAUDE_BUDGET_USD_WEEK` to get a
percentage against a budget. The plan's real remaining allowance is only visible with `/usage`
in the CLI or on claude.ai; this table tells you what the scripts have been spending.
