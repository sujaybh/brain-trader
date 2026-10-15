"""HTTP client for the broker daemon. The only way tools talk to Alpaca."""
from __future__ import annotations

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
    def __init__(self, url: str | None = None, token: str | None = None, timeout: float = 20):
        self.url = (url or config.BROKER_URL).rstrip("/")
        self.token = token if token is not None else config.BROKER_TOKEN
        self.timeout = timeout

    def _req(self, method: str, path: str, **kw) -> Any:
        headers = {"X-Broker-Token": self.token}
        try:
            r = requests.request(method, f"{self.url}{path}", headers=headers, timeout=self.timeout, **kw)
        except requests.ConnectionError as e:
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

    def place_order(self, *, thesis_id: str, side: str, symbol: str, notional: float | None = None,
                    qty: float | None = None, client_order_id: str | None = None, reason: str = "") -> dict:
        body = {"thesis_id": thesis_id, "side": side, "symbol": symbol, "notional": notional,
                "qty": qty, "client_order_id": client_order_id, "reason": reason}
        return self._req("POST", "/orders", json=body)

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
        return self._req("POST", "/reconcile")
