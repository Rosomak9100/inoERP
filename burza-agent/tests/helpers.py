import math
from datetime import date, datetime, timedelta, timezone

from burza import market
from burza.config import Config
from burza.models import Candle


def session_candles(d: date, start_price: float = 100.0, drift: float = 0.0004,
                    wave: float = 0.004, minutes: int = 5, seed: float = 0.0) -> list[Candle]:
    """Deterministická svíčková řada jedné seance (trend + vlnění)."""
    open_et, close_et = market.session_bounds(d)
    t = open_et
    out = []
    price = start_price
    i = 0
    while t < close_et:
        nxt = price * (1 + drift + wave * math.sin(i / 3.0 + seed))
        hi = max(price, nxt) * 1.001
        lo = min(price, nxt) * 0.999
        out.append(Candle(t.astimezone(timezone.utc), price, hi, lo, nxt, 1000 + 50 * (i % 7)))
        price = nxt
        t += timedelta(minutes=minutes)
        i += 1
    return out


def multi_day(days: list[date], **kw) -> list[Candle]:
    out, price = [], 100.0
    for n, d in enumerate(days):
        day = session_candles(d, start_price=price, seed=n, **kw)
        out.extend(day)
        price = day[-1].close
    return out


def config(**over) -> Config:
    cfg = Config(mode="paper", initial_capital_usd=10_000, instruments={"SPY": 3000})
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def utc(y, m, d, hh, mm) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)
