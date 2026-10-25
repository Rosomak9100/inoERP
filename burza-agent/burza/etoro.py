"""Tenký klient eToro Public API (autentizace x-api-key + x-user-key)."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone

import requests

from .models import Candle

log = logging.getLogger(__name__)

BASE_URL = "https://public-api.etoro.com"

LIVE_INTERVALS = {"1m": "OneMinute", "5m": "FiveMinutes", "15m": "FifteenMinutes", "1d": "OneDay"}

# Stavy objednávky z /orders:lookup
ORDER_FILLED = {3, 5}
ORDER_REJECTED = {4, 7, 8, 9, 10}


class EtoroError(Exception):
    def __init__(self, status: int, message: str, body: object = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.body = body


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def ci_get(d: dict, *names, default=None):
    """Case-insensitive čtení klíče (API vrací positionID i positionId)."""
    lowered = {k.lower(): v for k, v in d.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return default


class EtoroClient:
    def __init__(self, api_key: str, user_key: str, base_url: str = BASE_URL,
                 session: requests.Session | None = None, max_retries: int = 4):
        self.api_key = api_key
        self.user_key = user_key
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.max_retries = max_retries

    def request(self, method: str, path: str, *, params: dict | None = None,
                body: dict | None = None, request_id: str | None = None,
                retry: bool | None = None) -> dict:
        """Provede požadavek. GET se opakuje při 429/5xx/výpadku sítě.
        Zápisy se opakují jen se stejným request_id (idempotence u v3 objednávek)."""
        request_id = request_id or str(uuid.uuid4())
        if retry is None:
            retry = method == "GET"
        headers = {
            "x-api-key": self.api_key,
            "x-user-key": self.user_key,
            "x-request-id": request_id,
            "Accept": "application/json",
        }
        attempts = self.max_retries if retry else 1
        delay = 2.0
        for attempt in range(1, attempts + 1):
            try:
                resp = self.session.request(method, self.base_url + path, params=params,
                                            json=body, headers=headers, timeout=20)
            except requests.RequestException as exc:
                if attempt == attempts:
                    raise EtoroError(0, f"síťová chyba: {exc}") from exc
                time.sleep(delay)
                delay *= 2
                continue
            if resp.status_code == 429 and attempt < attempts:
                wait = float(resp.headers.get("Retry-After", delay))
                log.warning("429 rate limit na %s, čekám %.0fs", path, wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 500 and attempt < attempts:
                time.sleep(delay)
                delay *= 2
                continue
            if resp.status_code >= 400:
                try:
                    payload = resp.json()
                except ValueError:
                    payload = resp.text
                raise EtoroError(resp.status_code, str(payload)[:500], payload)
            if not resp.content:
                return {}
            return resp.json()
        raise EtoroError(0, "vyčerpány pokusy")

    # --- identita -----------------------------------------------------------------
    def me(self) -> dict:
        return self.request("GET", "/api/v1/me")

    # --- tržní data ---------------------------------------------------------------
    def live_candles(self, instrument_id: int, interval: str, count: int = 200) -> list[Candle]:
        """Aktuální svíčky (endpoint grafů). Vrací vzestupně podle času."""
        name = LIVE_INTERVALS[interval]
        data = self.request(
            "GET", f"/api/v1/market-data/instruments/{instrument_id}/history/candles/asc/{name}/{count}")
        out: list[Candle] = []
        for group in data.get("candles", []):
            for c in group.get("candles", []):
                out.append(Candle(parse_time(c["fromDate"]), float(c["open"]), float(c["high"]),
                                  float(c["low"]), float(c["close"]), float(c.get("volume") or 0)))
        out.sort(key=lambda c: c.time)
        return out

    def history_candles(self, instrument_id: int, interval: str, start: datetime,
                        end: datetime) -> list[Candle]:
        """Historické svíčky pro backtest (Data endpoint, stránkuje se kurzorem)."""
        params = {"interval": interval, "from": start.isoformat(), "to": end.isoformat(),
                  "limit": "2000", "side": "bid"}
        out: list[Candle] = []
        while True:
            data = self.request("GET", f"/api/v1/data/instruments/{instrument_id}/candles", params=params)
            for c in data.get("results", []):
                if c.get("close") is None:
                    continue
                out.append(Candle(parse_time(c["time"]), float(c["open"]), float(c["high"]),
                                  float(c["low"]), float(c["close"]), float(c.get("volume") or 0)))
            pag = data.get("pagination") or {}
            if not pag.get("hasNext"):
                break
            params = {**params, "cursor": pag["nextCursor"]}
        uniq = {c.time: c for c in out}
        return [uniq[t] for t in sorted(uniq)]

    # --- obchodování --------------------------------------------------------------
    @staticmethod
    def _env(demo: bool) -> str:
        return "/demo" if demo else ""

    def portfolio_pnl(self, demo: bool) -> dict:
        path = "/api/v1/trading/info/demo/pnl" if demo else "/api/v1/trading/info/real/pnl"
        return self.request("GET", path)

    def eligibility(self, instrument_ids: list[int], demo: bool) -> dict:
        path = f"/api/v2/trading/info{self._env(demo)}/eligibility"
        return self.request("POST", path, body={"instrumentIds": instrument_ids, "currency": "USD"},
                            retry=True)

    def open_market_order(self, *, demo: bool, instrument_id: int, amount_usd: float,
                          settlement_type: str, stop_loss: float | None,
                          take_profit: float | None, request_id: str) -> dict:
        body = {
            "action": "open",
            "transaction": "buy",
            "instrumentId": instrument_id,
            "settlementType": settlement_type,
            "orderType": "mkt",
            "leverage": 1,
            "amount": round(amount_usd, 2),
            "orderCurrency": "usd",
            "stopLossRate": stop_loss,
            "takeProfitRate": take_profit,
            "stopLossType": "fixed",
        }
        path = f"/api/v3/trading/execution{self._env(demo)}/orders"
        # Stejné x-request-id = idempotentní opakování, proto lze retry povolit.
        return self.request("POST", path, body=body, request_id=request_id, retry=True)

    def lookup_order(self, *, demo: bool, reference_id: str | None = None,
                     order_id: int | None = None) -> dict:
        params = {"referenceId": reference_id} if reference_id else {"orderId": str(order_id)}
        return self.request("GET", f"/api/v2/trading/info{self._env(demo)}/orders:lookup", params=params)

    def trade_history(self, *, demo: bool, min_date: str, page_size: int = 100) -> list[dict]:
        """Uzavřené obchody od min_date (YYYY-MM-DD) – přesné netProfit, closeRate, poplatky."""
        path = "/api/v1/trading/info/trade/demo/history" if demo else "/api/v1/trading/info/trade/history"
        out: list[dict] = []
        page = 1
        while True:
            data = self.request("GET", path, params={"minDate": min_date, "page": str(page),
                                                     "pageSize": str(page_size)})
            rows = data if isinstance(data, list) else data.get("trades") or data.get("items") or []
            out.extend(rows)
            if len(rows) < page_size or page >= 50:
                return out
            page += 1

    def close_position(self, *, demo: bool, position_id: str, instrument_id: int) -> dict:
        path = f"/api/v1/trading/execution{self._env(demo)}/market-close-orders/positions/{position_id}"
        # Bez automatického opakování – stav se ověří v dalším cyklu podle portfolia.
        return self.request("POST", path, body={"InstrumentID": instrument_id}, retry=False)
