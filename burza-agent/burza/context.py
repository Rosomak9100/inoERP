"""Kontext pro Studii B: zprávy (Finnhub), makrodata (FRED), výsledky firem, tržní přehled.

Každá zpráva nese zdroj, vydavatele a čas publikace; vše se ukládá do deníku.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import requests

from . import market

log = logging.getLogger(__name__)

FINNHUB_URL = "https://finnhub.io/api/v1"
FRED_URL = "https://api.stlouisfed.org/fred/series/observations"

# FRED série: (id, popis, transformace)
MACRO_SERIES = [
    ("FEDFUNDS", "Fed funds rate (%)", "level"),
    ("CPIAUCSL", "Inflace CPI meziročně (%)", "yoy"),
    ("CPILFESL", "Jádrová inflace CPI meziročně (%)", "yoy"),
    ("UNRATE", "Nezaměstnanost USA (%)", "level"),
    ("PAYEMS", "Nefarmářská zaměstnanost, změna m/m (tis.)", "diff"),
    ("DGS10", "Výnos 10Y Treasury (%)", "level"),
    ("DGS2", "Výnos 2Y Treasury (%)", "level"),
    ("DTWEXBGS", "Index síly USD (broad)", "level"),
    ("DCOILWTICO", "Ropa WTI (USD/barel)", "level"),
    ("VIXCLS", "VIX", "level"),
]

# Rozhodnutí FOMC (druhý den zasedání), zdroj federalreserve.gov. Doplnit pro další roky.
FOMC_DECISIONS = {
    date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29), date(2026, 6, 17),
    date(2026, 7, 29), date(2026, 9, 16), date(2026, 10, 28), date(2026, 12, 9),
}


@dataclass
class NewsItem:
    source: str          # odkud jsme zprávu získali (finnhub)
    publisher: str       # kdo ji vydal (Reuters, Bloomberg…)
    headline: str
    summary: str
    url: str
    published_at: datetime | None
    symbol: str | None = None

    @property
    def id(self) -> str:
        key = self.url or f"{self.publisher}|{self.headline}|{self.published_at}"
        return hashlib.sha1(key.encode()).hexdigest()[:16]


@dataclass
class MarketContext:
    symbol: str
    as_of: datetime
    news: list[NewsItem] = field(default_factory=list)
    market_news: list[NewsItem] = field(default_factory=list)
    earnings: list[dict] = field(default_factory=list)
    macro: dict[str, dict] = field(default_factory=dict)
    market_moves: dict[str, float] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def all_news(self) -> list[NewsItem]:
        return self.news + self.market_news


class _Http:
    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()

    def get(self, url: str, params: dict) -> object:
        for attempt in range(3):
            try:
                r = self.session.get(url, params=params, timeout=15)
            except requests.RequestException:
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 5)))
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"{url}: nedostupné")


class FinnhubSource(_Http):
    """Zprávy firem, obecné tržní zprávy a kalendář výsledků (free tier Finnhub)."""

    def __init__(self, api_key: str, session: requests.Session | None = None):
        super().__init__(session)
        self.api_key = api_key

    def _items(self, rows, symbol: str | None) -> list[NewsItem]:
        out = []
        for r in rows or []:
            ts = r.get("datetime")
            out.append(NewsItem(
                source="finnhub", publisher=r.get("source") or "", headline=(r.get("headline") or "").strip(),
                summary=(r.get("summary") or "").strip()[:600], url=r.get("url") or "",
                published_at=datetime.fromtimestamp(ts, timezone.utc) if ts else None, symbol=symbol))
        return [i for i in out if i.headline]

    def company_news(self, symbol: str, since: datetime, now: datetime) -> list[NewsItem]:
        rows = self.get(f"{FINNHUB_URL}/company-news", {
            "symbol": symbol, "from": since.date().isoformat(), "to": now.date().isoformat(),
            "token": self.api_key})
        return [i for i in self._items(rows, symbol) if i.published_at is None or i.published_at >= since]

    def market_news(self, since: datetime) -> list[NewsItem]:
        rows = self.get(f"{FINNHUB_URL}/news", {"category": "general", "token": self.api_key})
        return [i for i in self._items(rows, None) if i.published_at and i.published_at >= since]

    def earnings(self, symbol: str, start: date, end: date) -> list[dict]:
        data = self.get(f"{FINNHUB_URL}/calendar/earnings", {
            "symbol": symbol, "from": start.isoformat(), "to": end.isoformat(), "token": self.api_key})
        return [{"date": e.get("date"), "hour": e.get("hour"), "eps_estimate": e.get("epsEstimate"),
                 "eps_actual": e.get("epsActual"), "revenue_estimate": e.get("revenueEstimate"),
                 "revenue_actual": e.get("revenueActual")}
                for e in (data or {}).get("earningsCalendar", [])]


class FredSource(_Http):
    def __init__(self, api_key: str, session: requests.Session | None = None):
        super().__init__(session)
        self.api_key = api_key

    def snapshot(self) -> dict[str, dict]:
        out = {}
        for sid, label, kind in MACRO_SERIES:
            try:
                data = self.get(FRED_URL, {"series_id": sid, "api_key": self.api_key, "file_type": "json",
                                           "sort_order": "desc", "limit": "14"})
            except Exception as exc:  # jedna série nesmí shodit celý kontext
                log.warning("FRED %s: %s", sid, exc)
                continue
            obs = [o for o in data.get("observations", []) if o.get("value") not in (".", None)]
            if not obs:
                continue
            vals = [float(o["value"]) for o in obs]
            if kind == "yoy" and len(vals) >= 13:
                value, prev = (vals[0] / vals[12] - 1) * 100, (vals[1] / vals[13] - 1) * 100 if len(vals) >= 14 else None
            elif kind == "diff" and len(vals) >= 3:
                value, prev = vals[0] - vals[1], vals[1] - vals[2]
            else:
                value, prev = vals[0], vals[1] if len(vals) > 1 else None
            out[sid] = {"popis": label, "hodnota": round(value, 3),
                        "predchozi": round(prev, 3) if prev is not None else None,
                        "datum_pozorovani": obs[0]["date"]}
        return out


class ContextProvider:
    """Sestaví a nacachuje kontext. Makro se obnovuje po 6 h, zprávy podle configu."""

    def __init__(self, finnhub: FinnhubSource | None, fred: FredSource | None,
                 lookback_hours: int = 24, cache_minutes: int = 15, market_moves=None):
        self.finnhub = finnhub
        self.fred = fred
        self.lookback = timedelta(hours=lookback_hours)
        self.cache_ttl = timedelta(minutes=cache_minutes)
        self.market_moves = market_moves  # callable() -> dict symbol -> % změna dne
        self._cache: dict[str, tuple[datetime, MarketContext]] = {}
        self._macro: tuple[datetime, dict] | None = None
        self._market_news: tuple[datetime, list[NewsItem]] | None = None

    def get(self, symbol: str, now: datetime) -> MarketContext:
        hit = self._cache.get(symbol)
        if hit and now - hit[0] < self.cache_ttl:
            return hit[1]
        ctx = MarketContext(symbol, now)
        since = now - self.lookback
        if self.finnhub:
            try:
                ctx.news = sorted(self.finnhub.company_news(symbol, since, now),
                                  key=lambda n: n.published_at or now, reverse=True)[:15]
            except Exception as exc:
                ctx.errors.append(f"zprávy {symbol}: {exc}")
            try:
                if not self._market_news or now - self._market_news[0] > self.cache_ttl:
                    self._market_news = (now, self.finnhub.market_news(since))
                ctx.market_news = sorted(self._market_news[1], key=lambda n: n.published_at or now,
                                         reverse=True)[:10]
            except Exception as exc:
                ctx.errors.append(f"tržní zprávy: {exc}")
            try:
                today = market.trading_date(now)
                ctx.earnings = self.finnhub.earnings(symbol, today - timedelta(days=3), today + timedelta(days=7))
            except Exception as exc:
                ctx.errors.append(f"kalendář výsledků: {exc}")
        if self.fred:
            if not self._macro or now - self._macro[0] > timedelta(hours=6):
                self._macro = (now, self.fred.snapshot())
            ctx.macro = self._macro[1]
        if self.market_moves:
            try:
                ctx.market_moves = self.market_moves()
            except Exception as exc:
                ctx.errors.append(f"tržní přehled: {exc}")
        ctx.events = upcoming_events(now)
        self._cache[symbol] = (now, ctx)
        return ctx


def upcoming_events(now: datetime) -> list[str]:
    today = market.trading_date(now)
    out = []
    for d in sorted(FOMC_DECISIONS):
        if today <= d <= today + timedelta(days=7):
            out.append(f"Rozhodnutí FOMC (Fed) {d.isoformat()} ve 14:00 ET"
                       + (" – DNES" if d == today else ""))
    return out


def earnings_blackout(ctx: MarketContext, now: datetime) -> str | None:
    """Tvrdé pravidlo: nevstupovat v den výsledků ani den před výsledky před otevřením (riziko gapu)."""
    today = market.trading_date(now)
    for e in ctx.earnings:
        try:
            d = date.fromisoformat(e["date"])
        except (TypeError, ValueError):
            continue
        if d == today:
            return f"výsledky {ctx.symbol} dnes ({e.get('hour') or '?'})"
        nxt = today + timedelta(days=1)
        while not market.is_trading_day(nxt):
            nxt += timedelta(days=1)
        if d == nxt and (e.get("hour") or "").lower() == "bmo":
            return f"výsledky {ctx.symbol} zítra před otevřením"
    return None
