"""Společné datové typy."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Candle:
    time: datetime  # čas otevření svíčky, UTC
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Signal:
    symbol: str
    side: str  # zatím jen "long"
    entry: float
    stop: float
    target: float
    reason: str
    time: datetime | None = None
    size_factor: float = 1.0  # Studie B může riziko snížit (nikdy zvýšit nad limit)

    @property
    def risk_per_unit(self) -> float:
        return self.entry - self.stop


@dataclass
class Position:
    position_id: str
    symbol: str
    instrument_id: int
    units: float
    open_price: float
    stop: float | None = None
    target: float | None = None
    open_time: datetime | None = None
    unrealized_pnl: float = 0.0
    invested: float = 0.0


@dataclass
class Account:
    equity: float
    cash: float
    positions: list[Position] = field(default_factory=list)


@dataclass
class OrderResult:
    ok: bool
    client_ref: str
    status: str
    position_ids: list[str] = field(default_factory=list)
    fill_price: float | None = None
    units: float | None = None
    error: str | None = None
    raw: dict | None = None
