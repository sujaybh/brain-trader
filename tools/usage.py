#!/usr/bin/env python3
"""LLM usage by the scripted calls (dashboard chat, tools/review.py): tokens, API list-price
cost, and how that compares with CLAUDE_BUDGET_USD_WEEK in config/trader.env.

The calls run on the Claude subscription login, not an API key, so `cost_usd` is what the same
tokens would cost at API list prices, as reported by `claude -p` (total_cost_usd). The plan's
actual remaining allowance is only visible in the Claude Code CLI (`/usage`) or claude.ai, so
set a weekly budget here to get a percentage; without one you get totals only."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

from tools import _common as C  # noqa: E402
from trader import config, db  # noqa: E402

WINDOWS = {"today": timedelta(days=1), "7d": timedelta(days=7), "30d": timedelta(days=30)}


def _since(now: datetime, name: str) -> datetime:
    if name == "today":
        local = now.astimezone(config.MARKET_TZ)
        return local.replace(hour=0, minute=0, second=0, microsecond=0)
    return now - WINDOWS[name]


def totals(conn, since: datetime, group: str | None = None) -> list[dict]:
    cols = f"{group} AS key, " if group else ""
    sql = (f"SELECT {cols}COUNT(*) AS calls, SUM(ok=0) AS failed, SUM(turns) AS turns, "
           "SUM(input_tokens) AS input_tokens, SUM(cache_read) AS cache_read, SUM(cache_write) AS cache_write, "
           "SUM(output_tokens) AS output_tokens, ROUND(SUM(cost_usd), 4) AS cost_usd "
           "FROM claude_usage WHERE ts >= ?")
    if group:
        sql += f" GROUP BY {group} ORDER BY cost_usd DESC"
    out = db.rows(conn, sql, (since.astimezone(timezone.utc).isoformat(timespec="seconds"),))
    for r in out:
        for k, v in list(r.items()):
            if v is None and k != "key":
                r[k] = 0
    return out


def _backend(caller: str) -> str:
    try:
        return config.llm_backend(caller)
    except ValueError as e:
        return f"invalid ({e})"


def summary(conn, now: datetime | None = None) -> dict:
    """What the dashboard and the tool both show."""
    now = now or datetime.now(timezone.utc)
    windows = {name: totals(conn, _since(now, name))[0] for name in WINDOWS}
    week = windows["7d"]
    budget = config.CLAUDE_BUDGET_USD_WEEK or None
    pct = round(100 * week["cost_usd"] / budget, 1) if budget else None
    return {
        "model": config.CLAUDE_MODEL or "(cli default)",
        "models": {c: (f"ollama:{config.ollama_model(c)}" if _backend(c) == "ollama"
                       else config.claude_model(c) or "(cli default)") for c in ("chat", "review")},
        "backends": {c: _backend(c) for c in ("chat", "review")},
        "windows": windows,
        "by_caller_7d": totals(conn, _since(now, "7d"), "caller"),
        "by_model_7d": totals(conn, _since(now, "7d"), "model"),
        "budget_usd_week": budget,
        "week_pct_of_budget": pct,
        "over_budget": bool(budget and week["cost_usd"] > budget),
        "note": ("cost_usd is the API list-price equivalent reported by claude -p; the calls run on the "
                 "Claude subscription, whose remaining allowance is only visible via /usage in the CLI. "
                 "Set CLAUDE_BUDGET_USD_WEEK in config/trader.env to get a percentage. "
                 "Calls on the Ollama backend appear as model ollama:<name> with cost 0 (local); their "
                 "token counts are real."),
    }


def _main(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--calls", type=int, default=0, help="also list the N most recent calls")
    a = ap.parse_args(argv)
    conn = C.connect()
    out = {"ok": True, **summary(conn)}
    if a.calls:
        out["recent"] = db.rows(conn, "SELECT * FROM claude_usage ORDER BY id DESC LIMIT ?", (a.calls,))
    return out


def main(argv=None) -> int:
    return C.run(_main, argv)


if __name__ == "__main__":
    sys.exit(main())
