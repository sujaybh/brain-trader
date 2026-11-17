"""Paths and configuration. Non-secret settings live in config/trader.env; the broker's
Alpaca keys live in config/alpaca.env (readable only by the broker user)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("TRADER_DATA_DIR", ROOT / "data"))
CONFIG_DIR = ROOT / "config"
DB_PATH = DATA_DIR / "trader.db"
HALT_PATH = DATA_DIR / "HALT"
LIMITS_PATH = CONFIG_DIR / "limits.json"

MARKET_TZ = ZoneInfo("America/New_York")
MANUAL_THESIS_ID = "manual"


def load_env_file(path: Path) -> dict[str, str]:
    """Minimal .env parser: KEY=VALUE lines, # comments, optional quotes. Does not override
    variables already set in the process environment."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k] = v
        os.environ.setdefault(k, v)
    return out


load_env_file(CONFIG_DIR / "trader.env")


def env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


BROKER_HOST = env("BROKER_HOST", "127.0.0.1")
BROKER_PORT = int(env("BROKER_PORT", "8787"))
BROKER_URL = env("BROKER_URL", f"http://{BROKER_HOST}:{BROKER_PORT}")
BROKER_TOKEN = env("BROKER_TOKEN", "")
BROKER_BACKEND = env("BROKER_BACKEND", "alpaca")  # alpaca | fake
# Scheduled jobs (tick, nightly, weekly review) wait this long for the broker's /health before
# giving up with one journaled error; covers boot/wake when the timer fires before the broker
# is listening. Interactive tools do not wait (they only get BrokerClient's short retries).
BROKER_WAIT_S = float(env("BROKER_WAIT_S", "120") or 0)

NTFY_URL = env("NTFY_URL", "https://ntfy.sh")
NTFY_TOPIC = env("NTFY_TOPIC", "")

WEB_HOST = env("WEB_HOST", "0.0.0.0")
WEB_PORT = int(env("WEB_PORT", "8010"))
WEB_URL = env("WEB_URL", f"http://100.64.0.19:{WEB_PORT}")  # what the phone can reach
WEB_TOKEN = env("WEB_TOKEN", "")  # used in approve/reject links

OLLAMA_URL = env("OLLAMA_URL", "http://100.64.0.6:11434")
OLLAMA_MODEL = env("OLLAMA_MODEL", "qwen3-coder:30b")
OLLAMA_FALLBACK_URL = env("OLLAMA_FALLBACK_URL", "http://127.0.0.1:11434")
OLLAMA_FALLBACK_MODEL = env("OLLAMA_FALLBACK_MODEL", "qwen3.5:latest")
# tools/news.py polls Ollama (primary, then fallback) for up to this long before digesting, so a
# nightly run right after boot does not race the Ollama service.
OLLAMA_WAIT_S = float(env("OLLAMA_WAIT_S", "60") or 0)

# Claude Code (`claude -p`) is used by dashboard chat and tools/review.py. It runs on the
# user's Claude subscription login, so the model pin matters for plan usage. Set
# CLAUDE_MODEL="" to inherit the CLI's own default (currently fable). Every call's token
# counts and list-price cost go to the claude_usage table; tools/usage.py sums them and
# compares with CLAUDE_BUDGET_USD_WEEK (0 = no budget set).
CLAUDE_MODEL = env("CLAUDE_MODEL", "sonnet")
# Per-caller overrides; unset falls back to CLAUDE_MODEL ("" still means the CLI default).
CLAUDE_MODEL_CHAT = env("CLAUDE_MODEL_CHAT")
CLAUDE_MODEL_REVIEW = env("CLAUDE_MODEL_REVIEW")


def claude_model(caller: str) -> str:
    """Model for a scripted caller ('chat', 'review'): CLAUDE_MODEL_<CALLER>, else CLAUDE_MODEL."""
    v = globals().get(f"CLAUDE_MODEL_{caller.upper()}")
    return CLAUDE_MODEL if v is None else v


CLAUDE_BUDGET_USD_WEEK = float(env("CLAUDE_BUDGET_USD_WEEK", "0") or 0)

DEFAULT_LIMITS = {
    "global_cap": 1000,          # total deployed capital across all theses, USD
    "per_order_cap": 200,        # single order, USD
    "manual_thesis_cap": 200,    # cap for the implicit `manual` thesis
    "limit_slippage_pct": 0.3,   # marketable limit: ask * (1 + x/100) for buys
    "pdt_max_day_trades": 3,     # refuse the 4th in 5 business days
    "catchup_hours": 48,         # scheduler executes slots missed within this window
    "min_order_notional": 1.0,   # Alpaca minimum
    "qty_decimals": 4,
}


def load_limits() -> dict:
    limits = dict(DEFAULT_LIMITS)
    if LIMITS_PATH.exists():
        limits.update(json.loads(LIMITS_PATH.read_text()))
    return limits
