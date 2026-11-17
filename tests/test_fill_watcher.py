"""The broker daemon's background fill watcher: fills pushed within ~a minute, exactly once even
when the 15-minute tick reconciles too, only during market hours, never fatal, never trading."""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from broker import app as broker_app
from broker.app import create_app, fill_poll_seconds
from broker.backends import BackendError, FakeBackend
from conftest import TEST_LIMITS, TOKEN
from scheduler import tick as tick_mod
from trader import config, db

H = {"X-Broker-Token": TOKEN}


class CountingFake(FakeBackend):
    def __init__(self, **kw):
        super().__init__(cash=10000, market_open=True, auto_fill=False, **kw)
        self.clock_calls = 0
        self.get_calls = 0
        self.submits = 0
        self.fail_get = False

    def clock(self):
        self.clock_calls += 1
        return super().clock()

    def get_order(self, broker_order_id):
        self.get_calls += 1
        if self.fail_get:
            raise BackendError("Fake: upstream blew up")
        return super().get_order(broker_order_id)

    def submit_limit_order(self, **kw):
        self.submits += 1
        return super().submit_limit_order(**kw)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.delenv("BROKER_FILL_POLL_SECONDS", raising=False)
    backend = CountingFake()

    def make(poll=None):
        if poll is not None:
            monkeypatch.setenv("BROKER_FILL_POLL_SECONDS", str(poll))
        return create_app(backend, db_path=config.DB_PATH, halt_path=config.HALT_PATH, token=TOKEN,
                          limits=dict(TEST_LIMITS))

    return backend, make


def place(client, symbol="NVDA", notional=50):
    r = client.post("/orders", json={"thesis_id": "manual", "side": "buy", "symbol": symbol, "notional": notional},
                    headers=H)
    assert r.status_code == 201, r.text
    return r.json()


def counts():
    conn = db.connect()
    try:
        fills = db.rows(conn, "SELECT * FROM journal WHERE kind='fill'")
        pushes = db.rows(conn, "SELECT * FROM journal WHERE kind='notify' AND message LIKE 'Bought%'")
        return len(fills), len(pushes)
    finally:
        conn.close()


def only_open_order(backend):
    (oid,) = [k for k, o in backend.orders.items() if o["status"] == "submitted"]
    return oid


def test_interval_env(monkeypatch):
    monkeypatch.delenv("BROKER_FILL_POLL_SECONDS", raising=False)
    assert fill_poll_seconds() == 60
    for raw, want in (("15", 15), ("0", 0), ("off", 0), ("no", 0), ("junk", 60)):
        monkeypatch.setenv("BROKER_FILL_POLL_SECONDS", raw)
        assert fill_poll_seconds() == want


def test_off_switch(env):
    _, make = env
    app = make(poll="off")
    assert app.state.fill_watcher is None
    with TestClient(app) as c:  # lifespan still works without a watcher
        assert c.get("/health").json()["ok"]


def test_poll_pushes_fill_once_and_tick_does_not_repeat_it(env):
    backend, make = env
    app = make()
    c = TestClient(app)  # no `with`: the thread is not running; we drive poll_once by hand
    w = app.state.fill_watcher
    place(c)
    assert counts() == (0, 0)
    backend.fill_remaining(only_open_order(backend))

    assert w.poll_once() == 1
    assert counts() == (1, 1)
    assert w.poll_once() == 0  # nothing open any more

    # the scheduler tick reconciles through POST /reconcile: no second journal row or push
    class ViaHttp:
        def reconcile(self):
            return c.post("/reconcile", headers=H).json()
    out, code = tick_mod.tick(conn=db.connect(), client=ViaHttp())
    assert code == 0 and "reconcile" not in out  # tick saw no open orders
    assert c.post("/reconcile", headers=H).json()["updated"] == 0
    assert counts() == (1, 1)
    assert backend.submits == 1  # the watcher never placed anything


def test_tick_first_then_poll_pushes_once(env):
    backend, make = env
    app = make()
    c = TestClient(app)
    place(c)
    backend.fill_remaining(only_open_order(backend))

    class ViaHttp:
        def reconcile(self):
            return c.post("/reconcile", headers=H).json()
    out, _ = tick_mod.tick(conn=db.connect(), client=ViaHttp())
    assert out["reconcile"] == 1
    assert app.state.fill_watcher.poll_once() == 0
    assert counts() == (1, 1)


def test_partial_then_full_fill_two_pushes_not_more(env):
    backend, make = env
    app = make()
    c = TestClient(app)
    w = app.state.fill_watcher
    place(c)
    oid = only_open_order(backend)
    backend._fill(oid, round(backend.orders[oid]["qty"] / 2, 9))
    assert w.poll_once() == 1
    c.post("/reconcile", headers=H)
    backend.fill_remaining(oid)
    c.post("/reconcile", headers=H)
    w.poll_once()
    assert counts() == (2, 2)


def test_concurrent_tick_and_loop_push_once(env):
    backend, make = env
    app = make()
    c = TestClient(app)
    w = app.state.fill_watcher
    for sym in ("NVDA", "AMD", "AVGO"):
        place(c, sym, 20)
    for oid in [k for k, o in backend.orders.items() if o["status"] == "submitted"]:
        backend.fill_remaining(oid)

    errors = []

    def hammer(fn):
        try:
            for _ in range(5):
                fn()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=hammer, args=(w.poll_once,)) for _ in range(3)] + \
         [threading.Thread(target=hammer, args=(lambda: c.post("/reconcile", headers=H),)) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors
    assert counts() == (3, 3)


def test_market_closed_no_reconcile_and_clock_cached(env):
    backend, make = env
    app = make()
    c = TestClient(app)
    w = app.state.fill_watcher
    place(c)
    backend.fill_remaining(only_open_order(backend))
    backend.market_open = False
    gets, clocks = backend.get_calls, backend.clock_calls
    assert w.poll_once() == 0 and w.poll_once() == 0
    assert backend.get_calls == gets          # did not touch the order
    assert backend.clock_calls == clocks + 1  # one clock call, then the cache
    assert counts() == (0, 0)


def test_no_open_orders_no_backend_calls(env):
    backend, make = env
    w = make().state.fill_watcher
    assert w.poll_once() == 0
    assert backend.clock_calls == 0 and backend.get_calls == 0


def test_clock_cache_never_outlives_next_transition(env, monkeypatch):
    backend, make = env
    w = make().state.fill_watcher
    soon = time.time() + 2  # close in 2 s: cache must not say "open" for 5 minutes
    from datetime import datetime, timezone
    monkeypatch.setattr(backend, "clock", lambda: {"is_open": True, "next_close":
                                                   datetime.fromtimestamp(soon, timezone.utc).isoformat()})
    assert w.market_open()
    assert w._clock[1] - time.monotonic() <= 2.5


def test_thread_runs_survives_errors_and_stops(env, caplog):
    backend, make = env
    app = make(poll=0.02)
    w = app.state.fill_watcher
    with TestClient(app) as c:
        assert w._thread is not None and w._thread.is_alive()
        place(c)
        backend.fail_get = True
        deadline = time.time() + 5
        while backend.get_calls < 3 and time.time() < deadline:
            time.sleep(0.01)
        assert backend.get_calls >= 3                  # kept polling after the first failure
        assert "fill watcher poll failed" in caplog.text
        backend.fail_get = False
        backend.fill_remaining(only_open_order(backend))
        deadline = time.time() + 5
        while counts()[0] == 0 and time.time() < deadline:
            time.sleep(0.01)
        assert counts() == (1, 1)                      # pushed by the loop, no tick involved
        assert c.get("/health").json()["ok"]
        thread = w._thread
    assert w._thread is None and not thread.is_alive()  # clean shutdown with the app
    assert counts() == (1, 1)
    assert backend.submits == 1


def test_watcher_does_not_start_without_lifespan(env):
    _, make = env
    app = make()
    TestClient(app).get("/health")
    assert app.state.fill_watcher._thread is None
    assert broker_app.DEFAULT_FILL_POLL_SECONDS == 60
