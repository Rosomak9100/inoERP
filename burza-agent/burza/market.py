"""Obchodní hodiny NYSE/Nasdaq (9:30–16:00 ET) včetně svátků a zkrácených dnů."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
OPEN = time(9, 30)
CLOSE = time(16, 0)

# Zdroj: NYSE holiday calendar. Při přidání dalšího roku doplnit.
HOLIDAYS = {
    date(2025, 1, 1), date(2025, 1, 9), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
    date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1), date(2025, 11, 27),
    date(2025, 12, 25),
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
}
EARLY_CLOSE = {date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24),
               date(2026, 11, 27), date(2026, 12, 24), date(2027, 11, 26)}  # 13:00 ET


@lru_cache(maxsize=500_000)  # backtest volá převod pro stejné svíčky mnohokrát
def to_et(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(ET)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


@lru_cache(maxsize=8192)
def session_bounds(d: date) -> tuple[datetime, datetime]:
    close = time(13, 0) if d in EARLY_CLOSE else CLOSE
    return datetime.combine(d, OPEN, ET), datetime.combine(d, close, ET)


def is_open(now: datetime) -> bool:
    et = to_et(now)
    if not is_trading_day(et.date()):
        return False
    start, end = session_bounds(et.date())
    return start <= et < end


def minutes_since_open(now: datetime) -> float:
    et = to_et(now)
    start, _ = session_bounds(et.date())
    return (et - start).total_seconds() / 60


def minutes_to_close(now: datetime) -> float:
    et = to_et(now)
    _, end = session_bounds(et.date())
    return (end - et).total_seconds() / 60


def trading_date(now: datetime) -> date:
    """Obchodní den, ke kterému patří daný okamžik (podle ET kalendáře)."""
    return to_et(now).date()


def next_open(now: datetime) -> datetime:
    et = to_et(now)
    d = et.date()
    for _ in range(10):
        if is_trading_day(d):
            start, end = session_bounds(d)
            if et < end:
                return max(start, et)
        d += timedelta(days=1)
        et = datetime.combine(d, time(0), ET)
    raise RuntimeError("Nenalezen další obchodní den")


def in_session(ts: datetime) -> bool:
    """Svíčka patří do řádných obchodních hodin (bez pre-market / after-hours)."""
    et = to_et(ts)
    start, end = session_bounds(et.date())
    return is_trading_day(et.date()) and start <= et < end
