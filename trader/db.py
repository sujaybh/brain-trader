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
