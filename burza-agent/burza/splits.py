"""Rozpoznání a přepočet splitů akcií.

Historie z eToro NENÍ přepočtená na splity (ověřeno: AMZN 3. 6. 2022 close 2443,80 → 6. 6. 2022
open 125,60 po splitu 20:1). Bez úpravy by backtest viděl pád o 95 %. Skok mezi close a dalším
open se považuje za split, jen když odpovídá běžnému poměru s tolerancí 5 % – skutečný pád velké
firmy o 50 % a víc přes noc se prakticky nevyskytuje, a i kdyby, poměr by sotva přesně seděl.
"""

from __future__ import annotations

from .models import Candle

SPLIT_RATIOS = (2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 40, 50)
TOLERANCE = 0.05


def detect_split(prev_close: float, next_open: float) -> float | None:
    """Vrátí poměr splitu k (> 1 dopředný, < 1 zpětný), jinak None."""
    if prev_close <= 0 or next_open <= 0:
        return None
    r = prev_close / next_open
    if 0.55 < r < 1.8:
        return None
    for k in SPLIT_RATIOS:
        if abs(r / k - 1) <= TOLERANCE:
            return float(k)
        if abs(r * k - 1) <= TOLERANCE:
            return 1.0 / k
    return None


def adjust_splits(candles: list[Candle]) -> list[Candle]:
    """Přepočte starší svíčky tak, aby navazovaly na dnešní ceny (jako „adjusted“ data)."""
    if len(candles) < 2:
        return list(candles)
    factors = [1.0] * len(candles)
    cum = 1.0
    for i in range(len(candles) - 1, 0, -1):
        k = detect_split(candles[i - 1].close, candles[i].open)
        if k:
            cum *= k
        factors[i - 1] = cum
    if cum == 1.0 and all(f == 1.0 for f in factors):
        return list(candles)
    return [Candle(c.time, c.open / f, c.high / f, c.low / f, c.close / f, c.volume * f) if f != 1.0 else c
            for c, f in zip(candles, factors)]
