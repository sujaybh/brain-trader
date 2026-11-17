"""SQLite access and schema. Alpaca is the source of truth for positions; this DB is the
cache plus the system's own memory (theses, orders we placed, proposals, journal)."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS theses (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',   -- active | paused | closed
    mode        TEXT NOT NULL DEFAULT 'confirm',  -- confirm | auto
    spec        TEXT NOT NULL,                    -- full JSON spec
    source_text TEXT NOT NULL DEFAULT '',
    created     TEXT NOT NULL,
    updated     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id               INTEGER PRIMARY KEY,
    client_order_id  TEXT NOT NULL UNIQUE,
    thesis_id        TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    side             TEXT NOT NULL,               -- buy | sell
    notional         REAL,                        -- requested USD
    qty              REAL,                        -- requested shares
    limit_price      REAL,
    status           TEXT NOT NULL,               -- submitted | filled | partially_filled | canceled | rejected | refused | expired
    broker_order_id  TEXT,
    filled_qty       REAL NOT NULL DEFAULT 0,
    filled_avg_price REAL,
    filled_notional  REAL NOT NULL DEFAULT 0,
    reason           TEXT,                        -- why placed, or why refused
    created          TEXT NOT NULL,
    updated          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_thesis ON orders(thesis_id, symbol);
CREATE INDEX IF NOT EXISTS orders_status ON orders(status);
CREATE TABLE IF NOT EXISTS proposals (
    id          INTEGER PRIMARY KEY,
    thesis_id   TEXT,
    action      TEXT NOT NULL,                    -- JSON: {"type": "order", "side":..., "symbol":..., "notional":...} | {"type":"pause"} | {"type":"spec_change", ...}
    reason      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected | executed | failed | expired
    result      TEXT,                             -- JSON outcome
    created     TEXT NOT NULL,
    decided     TEXT,
    expires     TEXT
);
CREATE TABLE IF NOT EXISTS journal (
    id        INTEGER PRIMARY KEY,
    ts        TEXT NOT NULL,
    kind      TEXT NOT NULL,                      -- order | refused | fill | thesis | proposal | trigger | news | review | notify | system | error
    thesis_id TEXT,
    symbol    TEXT,
    message   TEXT NOT NULL,
    data      TEXT                                -- JSON
);
CREATE INDEX IF NOT EXISTS journal_ts ON journal(ts);
CREATE TABLE IF NOT EXISTS snapshots (
    id             INTEGER PRIMARY KEY,
    date           TEXT NOT NULL,                 -- YYYY-MM-DD (market tz)
    symbol         TEXT NOT NULL,                 -- '$ACCOUNT' for the account row
    thesis_id      TEXT,
    qty            REAL,
    price          REAL,
    market_value   REAL,
    cost_basis     REAL,
    unrealized_pl  REAL,
    data           TEXT,
    UNIQUE(date, symbol, thesis_id)
);
CREATE TABLE IF NOT EXISTS pdt_log (
    id            INTEGER PRIMARY KEY,
    date          TEXT NOT NULL,                  -- YYYY-MM-DD business day of the round trip
    symbol        TEXT NOT NULL,
    buy_order_id  TEXT,
    sell_order_id TEXT,
    created       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS news (
    id        INTEGER PRIMARY KEY,
    ts        TEXT NOT NULL,                      -- when digested
    published TEXT,
    symbol    TEXT NOT NULL,
    headline  TEXT NOT NULL,
    source    TEXT,
    url       TEXT,
    sentiment TEXT,                               -- positive | negative | neutral
    relevance INTEGER,                            -- 0..10
    summary   TEXT,
    data      TEXT,
    UNIQUE(symbol, headline)
);
CREATE TABLE IF NOT EXISTS chat (
    id      INTEGER PRIMARY KEY,
    ts      TEXT NOT NULL,
    role    TEXT NOT NULL,                        -- user | assistant | error
    content TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS claude_usage (
    id            INTEGER PRIMARY KEY,
    ts            TEXT NOT NULL,
    caller        TEXT NOT NULL,                  -- chat | review
    model         TEXT,                           -- canonical model id(s) reported by claude -p
    session_id    TEXT,
    turns         INTEGER,
    input_tokens  INTEGER NOT NULL DEFAULT 0,     -- uncached input
    cache_read    INTEGER NOT NULL DEFAULT 0,
    cache_write   INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0,        -- this call only, API list-price equivalent
    session_cost_usd REAL,                        -- total_cost_usd as reported: cumulative over a resumed session
    models        TEXT,                           -- modelUsage JSON as reported (cumulative), for the next delta
    duration_ms   INTEGER,
    ok            INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS claude_usage_ts ON claude_usage(ts);
CREATE TABLE IF NOT EXISTS bars (
    symbol  TEXT NOT NULL,
    date    TEXT NOT NULL,                        -- YYYY-MM-DD trading day (market tz)
    open    REAL NOT NULL,
    high    REAL NOT NULL,
    low     REAL NOT NULL,
    close   REAL NOT NULL,
    volume  REAL,
    vwap    REAL,
    source  TEXT,                                 -- alpaca-iex | fake
    fetched TEXT NOT NULL,
    PRIMARY KEY (symbol, date)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


# Columns added after a table first shipped: (table, column, type). CREATE TABLE IF NOT EXISTS
# never alters an existing table, so connect() adds whatever is missing. Additive only, so an
# older reader of the same DB keeps working.
COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("snapshots", "realized_pl", "REAL"),  # cumulative realized P&L (average cost) for that thesis x symbol
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, col, typ in COLUMN_MIGRATIONS:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in have:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError as e:  # another process added it first
                if "duplicate column" not in str(e).lower():
                    raise


def connect_ro(path: Path | None = None) -> sqlite3.Connection:
    """Read-only connection (backtests, reports): no schema creation, no writes possible."""
    path = path or config.DB_PATH
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def journal(conn: sqlite3.Connection, kind: str, message: str, *, thesis_id: str | None = None,
            symbol: str | None = None, data: Any = None) -> int:
    cur = conn.execute(
        "INSERT INTO journal(ts, kind, thesis_id, symbol, message, data) VALUES (?,?,?,?,?,?)",
        (now_iso(), kind, thesis_id, symbol, message, json.dumps(data) if data is not None else None),
    )
    return int(cur.lastrowid)


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def row(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> dict | None:
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def deployed_by_thesis(conn: sqlite3.Connection) -> dict[str, dict[str, float]]:
    """Net filled notional per thesis per symbol from our own order history: buys minus sells.
    This is how positions are attributed to theses (Alpaca does not tag positions)."""
    out: dict[str, dict[str, float]] = {}
    for r in conn.execute(
        "SELECT thesis_id, symbol, side, SUM(filled_notional) AS n, SUM(filled_qty) AS q FROM orders "
        "WHERE filled_qty > 0 GROUP BY thesis_id, symbol, side"  # incl. canceled/expired after a partial fill
    ):
        sign = 1 if r["side"] == "buy" else -1
        d = out.setdefault(r["thesis_id"], {})
        d[r["symbol"]] = round(d.get(r["symbol"], 0.0) + sign * (r["n"] or 0.0), 2)
    return out


def qty_by_thesis(conn: sqlite3.Connection) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for r in conn.execute(
        "SELECT thesis_id, symbol, side, SUM(filled_qty) AS q FROM orders "
        "WHERE filled_qty > 0 GROUP BY thesis_id, symbol, side"  # incl. canceled/expired after a partial fill
    ):
        sign = 1 if r["side"] == "buy" else -1
        d = out.setdefault(r["thesis_id"], {})
        d[r["symbol"]] = round(d.get(r["symbol"], 0.0) + sign * (r["q"] or 0.0), 6)
    return out


BAR_FIELDS = ("open", "high", "low", "close", "volume", "vwap")


def upsert_bars(conn: sqlite3.Connection, symbol: str, bars: list[dict], source: str) -> int:
    """Insert or replace daily bars ({"date", "open", "high", "low", "close", ...}). Returns count."""
    now = now_iso()
    conn.executemany(
        "INSERT INTO bars(symbol, date, open, high, low, close, volume, vwap, source, fetched) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol, date) DO UPDATE SET open=excluded.open, "
        "high=excluded.high, low=excluded.low, close=excluded.close, volume=excluded.volume, "
        "vwap=excluded.vwap, source=excluded.source, fetched=excluded.fetched",
        [(symbol, b["date"], *(b.get(k) for k in BAR_FIELDS), source, now) for b in bars])
    return len(bars)


def load_bars(conn: sqlite3.Connection, symbols: list[str], start: str | None = None,
              end: str | None = None) -> dict[str, list[dict]]:
    """symbol -> bars sorted by date, each {"date", "open", "high", "low", "close", "volume", "vwap"}.
    `start`/`end` are inclusive YYYY-MM-DD bounds."""
    out: dict[str, list[dict]] = {s: [] for s in symbols}
    if not symbols:
        return out
    sql = f"SELECT symbol, date, {', '.join(BAR_FIELDS)} FROM bars WHERE symbol IN ({','.join('?' * len(symbols))})"
    params: list = list(symbols)
    if start:
        sql += " AND date>=?"
        params.append(start)
    if end:
        sql += " AND date<=?"
        params.append(end)
    for r in conn.execute(sql + " ORDER BY symbol, date", params):
        d = dict(r)
        out[d.pop("symbol")].append(d)
    return out
