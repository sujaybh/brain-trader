"""HTTP client for the broker daemon. The only way tools talk to Alpaca.

Retries: a *connection* error (refused, reset, connect timeout; i.e. the daemon is restarting
or not up yet) is retried a couple of times with a short backoff, but only for requests that
are safe to repeat:

- every GET, and POST /reconcile (it only pulls state from Alpaca);
- POST /orders only when the caller supplied a client_order_id. The broker dedups on that id
  under its order lock (an existing row comes back as `"duplicate": true`, and a failed upstream
  submit deletes the row so the id is free again), so a repeat can never place a second order.
  Without an id the broker derives one from the current second, so a retry a second later
  could be a *new* order: those are never retried.
- DELETE /orders/<id> is not retried (a second cancel answers 409 and would mislead).

Read timeouts are never retried (the request may still be running). Total extra wait is
~2 s, so interactive tools do not hang. Scheduled jobs use `wait_for_broker()` for the long,
bounded wait at boot."""
from __future__ import annotations

import time
from typing import Any

import requests

from . import config


class BrokerError(Exception):
    """Raised for any non-2xx from the broker. `refused` is True when the broker's limit
    chain rejected the request (HTTP 403); `reason` is the human-readable explanation."""

    def __init__(self, status: int, reason: str, detail: Any = None):
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.detail = detail
        self.refused = status == 403


class BrokerClient:
    RETRY_DELAYS: tuple[float, ...] = (0.5, 1.5)

    def __init__(self, url: str | None = None, token: str | None = None, timeout: float = 20,
                 retry_delays: tuple[float, ...] | None = None):
        self.url = (url or config.BROKER_URL).rstrip("/")
        self.token = token if token is not None else config.BROKER_TOKEN
        self.timeout = timeout
        if retry_delays is not None:
            self.RETRY_DELAYS = tuple(retry_delays)

    def _req(self, method: str, path: str, *, retry: bool | None = None, **kw) -> Any:
        headers = {"X-Broker-Token": self.token}
        delays = self.RETRY_DELAYS if (method == "GET" if retry is None else retry) else ()
        for attempt in range(len(delays) + 1):
            try:
                r = requests.request(method, f"{self.url}{path}", headers=headers, timeout=self.timeout, **kw)
                break
            except requests.ReadTimeout as e:
                raise BrokerError(0, f"broker daemon at {self.url} did not answer within {self.timeout:g}s") from e
            except requests.ConnectionError as e:
                if attempt < len(delays):
                    time.sleep(delays[attempt])
                    continue
                raise BrokerError(0, f"broker daemon unreachable at {self.url}: {e}") from e
        if r.status_code >= 400:
            try:
                body = r.json()
            except ValueError:
                body = {"reason": r.text}
            reason = body.get("reason") or body.get("detail") or r.text
            raise BrokerError(r.status_code, str(reason), body)
        return r.json()

    def health(self) -> dict:
        return self._req("GET", "/health")

    def wait_ready(self, timeout: float, interval: float = 2.0) -> tuple[bool, str | None]:
        """Poll GET /health until it answers ok or `timeout` seconds pass. -> (ready, last error)."""
        deadline = time.monotonic() + max(timeout, 0)
        last = None
        while True:
            try:
                h = self._req("GET", "/health", retry=False)
                if isinstance(h, dict) and h.get("ok"):
                    return True, None
                last = f"/health answered {h!r}"
            except BrokerError as e:
                last = e.reason
            left = deadline - time.monotonic()
            if left <= 0:
                return False, last
            time.sleep(min(interval, left))

    def clock(self) -> dict:
        return self._req("GET", "/clock")

    def account(self) -> dict:
        return self._req("GET", "/account")

    def positions(self) -> list[dict]:
        return self._req("GET", "/positions")

    def quote(self, symbols: list[str]) -> dict[str, dict]:
        return self._req("GET", "/quote", params={"symbols": ",".join(symbols)})

    def assets(self, symbols: list[str]) -> dict[str, dict]:
        return self._req("GET", "/assets", params={"symbols": ",".join(symbols)})

    def bars(self, symbols: list[str], start: str | None = None, end: str | None = None,
             days: int | None = None) -> dict:
        """Daily bars: {"source", "start", "end", "bars": {symbol: [{"date", "open", ...}]}}."""
        params: dict[str, Any] = {"symbols": ",".join(symbols)}
        for k, v in (("start", start), ("end", end), ("days", days)):
            if v is not None:
                params[k] = v
        return self._req("GET", "/bars", params=params)

    def place_order(self, *, thesis_id: str, side: str, symbol: str, notional: float | None = None,
                    qty: float | None = None, client_order_id: str | None = None, reason: str = "") -> dict:
        body = {"thesis_id": thesis_id, "side": side, "symbol": symbol, "notional": notional,
                "qty": qty, "client_order_id": client_order_id, "reason": reason}
        # retried on connection errors only with an explicit id: see the module docstring
        return self._req("POST", "/orders", retry=client_order_id is not None, json=body)

    def orders(self, status: str | None = None, limit: int = 100) -> list[dict]:
        params = {"limit": limit}
        if status:
            params["status"] = status
        return self._req("GET", "/orders", params=params)

    def order(self, order_id: str) -> dict:
        return self._req("GET", f"/orders/{order_id}")

    def cancel(self, order_id: str) -> dict:
        return self._req("DELETE", f"/orders/{order_id}")

    def reconcile(self) -> dict:
        return self._req("POST", "/reconcile", retry=True)


def wait_for_broker(job: str, *, conn=None, client=None, timeout: float | None = None) -> tuple[bool, str | None]:
    """Bounded wait for the broker at the start of a scheduled job. On timeout journals ONE
    kind=error row naming the job and returns (False, reason); the caller decides what to skip.
    `timeout` defaults to config.BROKER_WAIT_S (env BROKER_WAIT_S, 120 s)."""
    timeout = config.BROKER_WAIT_S if timeout is None else timeout
    c = client or BrokerClient(timeout=5)
    waiter = getattr(c, "wait_ready", None)
    if waiter is None:  # a stub without wait_ready: one plain health call
        try:
            c.health()
            return True, None
        except BrokerError as e:
            ok, last = False, e.reason
    else:
        ok, last = waiter(timeout)
    if ok:
        return True, None
    reason = f"{job}: broker not reachable after {timeout:g}s ({last})"
    from . import db
    own = conn is None
    try:
        conn = conn or db.connect()
        db.journal(conn, "error", reason, data={"job": job, "wait_s": timeout, "last_error": last})
    except Exception:  # noqa: BLE001  (never let journaling hide the real problem)
        pass
    finally:
        if own and conn is not None:
            conn.close()
    return False, reason
