"""Technické indikátory nad seznamy čísel / svíček. Bez externích závislostí."""

from __future__ import annotations

import math

from .models import Candle


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def sma(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = []
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= period:
            total -= values[i - period]
        out.append(total / period if i >= period - 1 else None)
    return out


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    """RSI s Wilderovým vyhlazením."""
    out: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / period, losses / period
    out[period] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0)) / period
        out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def atr(candles: list[Candle], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(candles)
    if len(candles) <= period:
        return out
    trs = [candles[0].high - candles[0].low]
    for prev, c in zip(candles, candles[1:]):
        trs.append(max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close)))
    value = sum(trs[1:period + 1]) / period
    out[period] = value
    for i in range(period + 1, len(candles)):
        value = (value * (period - 1) + trs[i]) / period
        out[i] = value
    return out


def vwap(candles: list[Candle]) -> list[float]:
    """Kumulativní VWAP od první předané svíčky (předávej svíčky jedné seance).
    Pokud objem chybí, použije se rovnoměrná váha."""
    out: list[float] = []
    pv = vol = 0.0
    for c in candles:
        typical = (c.high + c.low + c.close) / 3
        w = c.volume if c.volume > 0 else 1.0
        pv += typical * w
        vol += w
        out.append(pv / vol)
    return out


def bollinger(values: list[float], period: int = 20, mult: float = 2.0):
    mid = sma(values, period)
    upper: list[float | None] = []
    lower: list[float | None] = []
    for i, m in enumerate(mid):
        if m is None:
            upper.append(None)
            lower.append(None)
            continue
        window = values[i - period + 1:i + 1]
        sd = math.sqrt(sum((x - m) ** 2 for x in window) / period)
        upper.append(m + mult * sd)
        lower.append(m - mult * sd)
    return mid, upper, lower
