"""Swing strategie na denních svíčkách – málo obchodů, držení dny až týdny.

Rozhoduje se jednou denně těsně před koncem seance (agent v 15:45 ET, backtest na close dne).
Strategie dostávají denní svíčky normalizované tak, že `market.to_et(c.time).date()` je obchodní den.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import indicators as ind
from . import market
from .models import Candle, Signal
from .strategy import Strategy


def trading_date_of(c: Candle):
    """Obchodní den denní svíčky eToro (začíná ~17:00 ET předchozího dne nebo o půlnoci UTC)."""
    return market.to_et(c.time + timedelta(hours=9)).date()


def normalize_daily(candles: list[Candle]) -> list[Candle]:
    """Převede denní svíčky z eToro na jednu svíčku na obchodní den s časem = otevření seance (UTC)."""
    by_day: dict = {}
    for c in candles:
        d = trading_date_of(c)
        if not market.is_trading_day(d):
            continue
        start, _ = market.session_bounds(d)
        by_day[d] = Candle(start.astimezone(timezone.utc), c.open, c.high, c.low, c.close, c.volume)
    return [by_day[d] for d in sorted(by_day)]


def daily_from_intraday(candles: list[Candle]) -> list[Candle]:
    """Sestaví denní svíčky z intradenních (jen řádná seance) – pro dnešní, ještě neuzavřený den."""
    out: dict = {}
    for c in candles:
        if not market.in_session(c.time):
            continue
        d = market.to_et(c.time).date()
        if d not in out:
            start, _ = market.session_bounds(d)
            out[d] = [start.astimezone(timezone.utc), c.open, c.high, c.low, c.close, c.volume]
        else:
            row = out[d]
            row[2] = max(row[2], c.high)
            row[3] = min(row[3], c.low)
            row[4] = c.close
            row[5] += c.volume
    return [Candle(*out[d]) for d in sorted(out)]


class DailyStrategy(Strategy):
    horizon = "swing"
    history_bars = 300
    atr_stop = 3.0        # katastrofický stop na eToro (strategie vystupuje sama dřív)

    def _signal(self, symbol: str, candles: list[Candle], reason: str) -> Signal | None:
        a = ind.atr(candles[-60:], 20)[-1]
        if a is None or a <= 0:
            return None
        entry = candles[-1].close
        stop = entry - self.atr_stop * a
        if stop <= 0:
            return None
        return Signal(symbol, "long", entry, stop, entry * 3, reason, candles[-1].time)


class TrendSMA(DailyStrategy):
    """Trend nad 200denním průměrem (Faber 2007, denní verze s pásmem proti přeskakování)."""

    name = "trend_sma200"

    def __init__(self, period: int = 200, band: float = 0.01):
        self.period = period
        self.band = band

    def _sma(self, candles):
        if len(candles) < self.period:
            return None
        return sum(c.close for c in candles[-self.period:]) / self.period

    def entry(self, symbol, candles):
        sma = self._sma(candles)
        if sma is None or candles[-1].close <= sma * (1 + self.band):
            return None
        return self._signal(symbol, candles, f"close nad SMA{self.period} ({sma:.2f})")

    def exit(self, position, candles):
        sma = self._sma(candles)
        if sma is not None and candles[-1].close < sma * (1 - self.band):
            return f"pokles pod SMA{self.period}"
        return None


class TimeSeriesMomentum(DailyStrategy):
    """12měsíční momentum (Moskowitz, Ooi & Pedersen 2012), kontrola jednou měsíčně."""

    name = "momentum_12m"

    def __init__(self, lookback: int = 252):
        self.lookback = lookback

    @staticmethod
    def _month_start(candles) -> bool:
        if len(candles) < 2:
            return False
        a, b = market.to_et(candles[-2].time).date(), market.to_et(candles[-1].time).date()
        return a.month != b.month

    def _ret(self, candles):
        if len(candles) <= self.lookback:
            return None
        return candles[-1].close / candles[-1 - self.lookback].close - 1

    def entry(self, symbol, candles):
        r = self._ret(candles)
        if r is None or r <= 0 or not self._month_start(candles):
            return None
        return self._signal(symbol, candles, f"12měs. výnos {r * 100:+.1f} %")

    def exit(self, position, candles):
        r = self._ret(candles)
        if r is not None and r < 0 and self._month_start(candles):
            return "12měsíční výnos záporný"
        return None


class DonchianBreakout(DailyStrategy):
    """Průraz 55denního maxima, výstup pod 20denním minimem („Turtles“)."""

    name = "donchian_55_20"
    atr_stop = 2.0

    def __init__(self, entry_days: int = 55, exit_days: int = 20):
        self.entry_days = entry_days
        self.exit_days = exit_days

    def entry(self, symbol, candles):
        if len(candles) <= self.entry_days + 20:
            return None
        hi = max(c.high for c in candles[-1 - self.entry_days:-1])
        if candles[-1].close <= hi:
            return None
        return self._signal(symbol, candles, f"nové {self.entry_days}denní maximum nad {hi:.2f}")

    def exit(self, position, candles):
        if len(candles) <= self.exit_days:
            return None
        lo = min(c.low for c in candles[-1 - self.exit_days:-1])
        if candles[-1].close < lo:
            return f"pod {self.exit_days}denním minimem"
        return None


class Rsi2Pullback(DailyStrategy):
    """Krátký pokles v dlouhodobém trendu: RSI(2) < 10 nad SMA200, výstup nad SMA5 (Connors)."""

    name = "rsi2_pullback"

    def __init__(self, max_days: int = 10):
        self.max_days = max_days

    def entry(self, symbol, candles):
        if len(candles) < 200:
            return None
        closes = [c.close for c in candles]
        sma200 = sum(closes[-200:]) / 200
        r = ind.rsi(closes[-50:], 2)[-1]
        if r is None or closes[-1] <= sma200 or r >= 10:
            return None
        return self._signal(symbol, candles, f"RSI(2) {r:.0f} v trendu nad SMA200")

    def exit(self, position, candles):
        closes = [c.close for c in candles]
        if len(closes) >= 5 and closes[-1] > sum(closes[-5:]) / 5:
            return "návrat nad SMA5"
        if position.open_time and candles[-1].time - position.open_time >= timedelta(days=self.max_days * 1.5):
            return f"max. doba držení {self.max_days} dní"
        return None


DAILY_STRATEGIES = {s.name: s for s in (TrendSMA, TimeSeriesMomentum, DonchianBreakout, Rsi2Pullback)}


def today_et(now: datetime):
    return market.to_et(now).date()
