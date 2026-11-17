"""Shared fixtures. Environment is set BEFORE trader.config is imported (it reads env at
import time), so nothing in the test run can touch the real data/ dir or push to ntfy."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["TRADER_DATA_DIR"] = tempfile.mkdtemp(prefix="trader-test-")
os.environ["BROKER_TOKEN"] = "test-token"
os.environ["BROKER_BACKEND"] = "fake"
os.environ["NTFY_TOPIC"] = ""
os.environ["BROKER_WAIT_S"] = "0"   # scheduled-job waits: never sleep in tests unless a test asks
os.environ["OLLAMA_WAIT_S"] = "0"
for k in ("FAKE_CASH", "FAKE_MARKET_OPEN", "FAKE_PARTIAL_FILL", "FAKE_AUTO_FILL"):
    os.environ.pop(k, None)

import pytest  # noqa: E402

from trader import config, db  # noqa: E402
from trader.spec import Schedule, ThesisSpec  # noqa: E402

TOKEN = "test-token"
TEST_LIMITS = {"global_cap": 1000, "per_order_cap": 200, "manual_thesis_cap": 200,
               "limit_slippage_pct": 0.3, "pdt_max_day_trades": 3, "min_order_notional": 1.0,
               "qty_decimals": 4}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "trader.db")
    monkeypatch.setattr(config, "HALT_PATH", tmp_path / "HALT")
    monkeypatch.setattr(config, "NTFY_TOPIC", "")
    monkeypatch.setattr(config, "BROKER_TOKEN", TOKEN)
    yield


def insert_thesis(db_path: Path, **kw) -> ThesisSpec:
    fields = dict(id="semis", name="Semis", universe=["NVDA", "AMD", "AVGO", "TSM"], capital_cap=300,
                  max_position_pct=60, schedule=Schedule(type="dca", amount=50, cron="0 10 * * 1"))
    fields.update(kw)
    spec = ThesisSpec(**fields)
    conn = db.connect(db_path)
    try:
        now = db.now_iso()
        conn.execute("INSERT INTO theses(id, name, status, mode, spec, source_text, created, updated) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     (spec.id, spec.name, spec.status, spec.mode, spec.model_dump_json(), spec.source_text,
                      now, now))
    finally:
        conn.close()
    return spec
