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
    history_bars = 300  # kolik posledních svíček strategie potřebuje (backtest je jí předá)

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
    history_bars = 150  # EMA21/RSI14/ATR14 jsou po 150 svíčkách ustálené

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
    history_bars = 150

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


def sessions(candles: list[Candle]) -> list[list[Candle]]:
    """Rozdělí svíčky na seance (jen řádné obchodní hodiny), vzestupně."""
    out: list[list[Candle]] = []
    current_day = None
    for c in candles:
        et = market.to_et(c.time)
        start, end = market.session_bounds(et.date())
        if not (start <= et < end):
            continue
        if et.date() != current_day:
            out.append([])
            current_day = et.date()
        out[-1].append(c)
    return out


class NoiseAreaMomentum(Strategy):
    """Intradenní momentum podle Zarattini, Aziz & Barbon (2024), „Beat the Market“ (SPY).

    Pásmo šumu = max(open dne, včerejší close) × (1 + σ), kde σ je průměrný absolutní pohyb
    od otevření do stejné minuty dne za posledních `lookback_days` dní. Cena nad pásmem a nad
    VWAP v kontrolním čase (každých 30 min) = nerovnováha poptávky → long. Výstup, když cena
    spadne pod max(pásmo, VWAP), jinak na konci dne. Systém je zatím jen long, článek obchoduje
    i short; výsledky proto nejsou přímo srovnatelné s článkem.
    """

    name = "noise_momentum"
    history_bars = 1300  # ~16 seancí 5min svíček

    def __init__(self, lookback_days: int = 14, min_days: int = 5, check_minutes: int = 30,
                 reward_risk: float = 4.0):
        self.lookback_days = lookback_days
        self.min_days = min_days
        self.check_minutes = check_minutes
        self.reward_risk = reward_risk

    def bands(self, candles: list[Candle]):
        days = sessions(candles)
        if len(days) < self.min_days + 1:
            return None
        today, prev = days[-1], days[-2]
        k = len(today) - 1
        moves = [abs(d[k].close / d[0].open - 1) for d in days[-1 - self.lookback_days:-1]
                 if len(d) > k and d[0].open]
        if len(moves) < self.min_days:
            return None
        sigma = sum(moves) / len(moves)
        o, pc = today[0].open, prev[-1].close
        return max(o, pc) * (1 + sigma), min(o, pc) * (1 - sigma), today

    def _check_time(self, candles: list[Candle]) -> bool:
        step = candles[-1].time - candles[-2].time
        end = market.to_et(candles[-1].time + step)
        start, _ = market.session_bounds(end.date())
        return int((end - start).total_seconds() // 60) % self.check_minutes == 0

    def entry(self, symbol: str, candles: list[Candle]) -> Signal | None:
        if len(candles) < MIN_HISTORY or not self._check_time(candles):
            return None
        b = self.bands(candles)
        a = ind.atr(candles[-100:], 14)[-1]
        if b is None or a is None or a <= 0:
            return None
        upper, _, today = b
        vw = ind.vwap(today)[-1]
        last = candles[-1]
        if not (last.close > upper and last.close > vw):
            return None
        entry = last.close
        stop = _bounded_stop(entry, max(upper, vw), a)
        return Signal(symbol, "long", entry, stop, entry + self.reward_risk * (entry - stop),
                      f"nad pásmem šumu {upper:.2f} a VWAP {vw:.2f}", last.time)

    def exit(self, position: Position, candles: list[Candle]) -> str | None:
        b = self.bands(candles)
        if b is None:
            return None
        upper, _, today = b
        if candles[-1].close < max(upper, ind.vwap(today)[-1]):
            return "návrat do pásma šumu / pod VWAP"
        return None


class LastHalfHourMomentum(Strategy):
    """Intradenní momentum podle Gao, Han, Li & Zhou (2018, JFE): výnos od včerejšího close
    do 10:00 ET předpovídá směr poslední půlhodiny. Long v 15:30 ET, když byl ranní výnos kladný
    (nad `min_move`); drží se do konce seance (agent zavírá 10 min před koncem)."""

    name = "intraday_momentum"
    history_bars = 200
    no_entry_last_minutes = 25  # vstup přesně v 15:30 ET musí projít oknem pro vstupy

    def __init__(self, min_move_pct: float = 0.1, entry_minute: int = 360, reward_risk: float = 2.0):
        self.min_move = min_move_pct / 100
        self.entry_minute = entry_minute  # minut od otevření (360 = 15:30 ET)

    def entry(self, symbol: str, candles: list[Candle]) -> Signal | None:
        if len(candles) < 30:
            return None
        step = candles[-1].time - candles[-2].time
        end = market.to_et(candles[-1].time + step)
        start, _ = market.session_bounds(end.date())
        if int((end - start).total_seconds() // 60) != self.entry_minute:
            return None
        days = sessions(candles)
        if len(days) < 2:
            return None
        today, prev = days[-1], days[-2]
        first = [c for c in today if market.to_et(c.time + step) <= start + timedelta(minutes=30)]
        if not first:
            return None
        r1 = first[-1].close / prev[-1].close - 1
        if r1 < self.min_move:
            return None
        a = ind.atr(candles[-100:], 14)[-1]
        if a is None or a <= 0:
            return None
        entry = candles[-1].close
        stop = _bounded_stop(entry, entry - 1.5 * a, a)
        return Signal(symbol, "long", entry, stop, entry + 2 * (entry - stop),
                      f"ranní výnos {r1 * 100:+.2f} % → momentum poslední půlhodiny", candles[-1].time)


STRATEGIES = {VwapPullback.name: VwapPullback, OpeningRangeBreakout.name: OpeningRangeBreakout,
              NoiseAreaMomentum.name: NoiseAreaMomentum, LastHalfHourMomentum.name: LastHalfHourMomentum}


def make_strategy(name: str) -> Strategy:
    try:
        return STRATEGIES[name]()
    except KeyError:
        raise ValueError(f"Neznámá strategie {name}; dostupné: {', '.join(STRATEGIES)}") from None
