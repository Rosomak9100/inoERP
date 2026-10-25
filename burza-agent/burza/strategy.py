"""Strategie Studie A – rozhodují jen z ceny a objemu.

Strategie dostává pouze UZAVŘENÉ svíčky (vzestupně). Signál vzniká na close poslední
svíčky; backtest ho plní až na open následující svíčky (žádný look-ahead).
"""

from __future__ import annotations

from datetime import timedelta

from . import indicators as ind
from . import market
from .models import Candle, Position, Signal

MIN_HISTORY = 40


def session_candles(candles: list[Candle]) -> list[Candle]:
    """Svíčky aktuální (poslední) seance v rámci řádných obchodních hodin."""
    if not candles:
        return []
    day = market.to_et(candles[-1].time).date()
    start, end = market.session_bounds(day)
    return [c for c in candles if start <= market.to_et(c.time) < end]


class Strategy:
    name = "base"

    def entry(self, symbol: str, candles: list[Candle]) -> Signal | None:
        raise NotImplementedError

    def exit(self, position: Position, candles: list[Candle]) -> str | None:
        return None


def _bounded_stop(entry: float, raw_stop: float, atr_value: float) -> float:
    """Stop aspoň 0,5 ATR a nejvýš 3 ATR pod vstupem."""
    distance = entry - raw_stop
    distance = min(max(distance, 0.5 * atr_value), 3 * atr_value)
    return entry - distance


class VwapPullback(Strategy):
    """Trend nad VWAP + EMA9 > EMA21, návrat k EMA21/VWAP a obnovení růstu."""

    name = "vwap_pullback"

    def __init__(self, reward_risk: float = 2.0):
        self.reward_risk = reward_risk

    def entry(self, symbol: str, candles: list[Candle]) -> Signal | None:
        if len(candles) < MIN_HISTORY:
            return None
        sess = session_candles(candles)
        if len(sess) < 4:
            return None
        closes = [c.close for c in candles]
        e9, e21 = ind.ema(closes, 9), ind.ema(closes, 21)
        r = ind.rsi(closes, 14)[-1]
        a = ind.atr(candles, 14)[-1]
        vw = ind.vwap(sess)
        last, prev = candles[-1], candles[-2]
        if a is None or r is None or a <= 0:
            return None
        trend_up = last.close > vw[-1] and e9[-1] > e21[-1]
        pulled_back = prev.low <= max(e21[-2], vw[-2]) * 1.001
        resumed = last.close > e9[-1] and last.close > last.open
        if not (trend_up and pulled_back and resumed and 45 <= r <= 70):
            return None
        entry = last.close
        raw_stop = min(c.low for c in sess[-3:]) - 0.1 * a
        stop = _bounded_stop(entry, raw_stop, a)
        target = entry + self.reward_risk * (entry - stop)
        return Signal(symbol, "long", entry, stop, target,
                      f"VWAP pullback rsi={r:.0f} atr={a:.3f}", last.time)

    def exit(self, position: Position, candles: list[Candle]) -> str | None:
        sess = session_candles(candles)
        if len(sess) < 2:
            return None
        a = ind.atr(candles, 14)[-1] or 0.0
        vw = ind.vwap(sess)[-1]
        if candles[-1].close < vw - 0.2 * a:
            return "close pod VWAP"
        return None


class OpeningRangeBreakout(Strategy):
    """Průraz 15min opening range nahoru s potvrzením VWAP a objemem."""

    name = "orb"

    def __init__(self, range_minutes: int = 15, reward_risk: float = 2.0):
        self.range_minutes = range_minutes
        self.reward_risk = reward_risk

    def _opening_range(self, sess: list[Candle]):
        if not sess:
            return None
        start, _ = market.session_bounds(market.to_et(sess[0].time).date())
        cutoff = start + timedelta(minutes=self.range_minutes)
        rng = [c for c in sess if market.to_et(c.time) < cutoff]
        rest = [c for c in sess if market.to_et(c.time) >= cutoff]
        if not rng or len(rest) < 1:
            return None
        return max(c.high for c in rng), min(c.low for c in rng), rest

    def entry(self, symbol: str, candles: list[Candle]) -> Signal | None:
        if len(candles) < MIN_HISTORY:
            return None
        sess = session_candles(candles)
        orr = self._opening_range(sess)
        if orr is None:
            return None
        hi, lo, rest = orr
        a = ind.atr(candles, 14)[-1]
        if a is None or a <= 0:
            return None
        last = candles[-1]
        prev_close = candles[-2].close
        vw = ind.vwap(sess)[-1]
        vols = [c.volume for c in sess if c.volume > 0]
        vol_ok = not vols or last.volume >= sum(vols) / len(vols)
        # jen první průraz dne: žádná dřívější svíčka po range nezavřela nad hi
        first_break = all(c.close <= hi for c in rest[:-1])
        if not (last.close > hi >= prev_close and last.close > vw and vol_ok and first_break):
            return None
        entry = last.close
        stop = _bounded_stop(entry, (hi + lo) / 2, a)
        target = entry + self.reward_risk * (entry - stop)
        return Signal(symbol, "long", entry, stop, target,
                      f"ORB hi={hi:.2f} lo={lo:.2f}", last.time)

    def exit(self, position: Position, candles: list[Candle]) -> str | None:
        sess = session_candles(candles)
        if len(sess) < 2:
            return None
        if candles[-1].close < ind.vwap(sess)[-1]:
            return "close pod VWAP"
        return None


STRATEGIES = {VwapPullback.name: VwapPullback, OpeningRangeBreakout.name: OpeningRangeBreakout}


def make_strategy(name: str) -> Strategy:
    try:
        return STRATEGIES[name]()
    except KeyError:
        raise ValueError(f"Neznámá strategie {name}; dostupné: {', '.join(STRATEGIES)}") from None
