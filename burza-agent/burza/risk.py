"""Společný risk engine: velikost pozice, denní limit, celkový propad, kill switch."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date

from .config import RiskConfig
from .models import Account, Signal


@dataclass
class RiskState:
    initial_capital: float
    day: str  # ISO datum obchodního dne
    day_start_equity: float
    halted: bool = False          # celkový limit – čeká na ruční schválení
    halt_reason: str = ""
    daily_blocked: bool = False   # denní limit – do dalšího obchodního dne
    trades_today: int = 0
    trades_by_symbol: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RiskState":
        return cls(**d)


@dataclass
class Decision:
    accepted: bool
    reason: str
    amount_usd: float = 0.0
    units: float = 0.0
    risk_usd: float = 0.0


class RiskEngine:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg

    def daily_limit_usd(self, st: RiskState) -> float:
        return st.initial_capital * self.cfg.max_daily_loss_pct / 100

    def drawdown_limit_usd(self, st: RiskState) -> float:
        return st.initial_capital * self.cfg.max_drawdown_pct / 100

    def roll_day(self, st: RiskState, today: date, equity: float) -> bool:
        """Nový obchodní den: reset denních čítačů. Celkový halt zůstává."""
        if st.day == today.isoformat():
            return False
        st.day = today.isoformat()
        st.day_start_equity = equity
        st.daily_blocked = False
        st.trades_today = 0
        st.trades_by_symbol = {}
        return True

    def check_limits(self, st: RiskState, equity: float) -> list[str]:
        """Vyhodnotí limity podle aktuální equity (vč. nerealizovaného P/L).
        Vrací nově spuštěné akce: 'halt' nebo 'daily'."""
        actions = []
        if not st.halted and st.initial_capital - equity >= self.drawdown_limit_usd(st):
            st.halted = True
            st.halt_reason = (f"celkový propad {st.initial_capital - equity:.2f} USD "
                              f">= {self.drawdown_limit_usd(st):.2f} USD")
            actions.append("halt")
        if not st.daily_blocked and st.day_start_equity - equity >= self.daily_limit_usd(st):
            st.daily_blocked = True
            actions.append("daily")
        return actions

    def evaluate(self, sig: Signal, st: RiskState, account: Account, open_risk_usd: float,
                 held_symbols: set[str], max_units: float | None = None,
                 min_amount: float = 10.0) -> Decision:
        c = self.cfg
        if st.halted:
            return Decision(False, f"zastaveno: {st.halt_reason}")
        if st.daily_blocked:
            return Decision(False, "denní limit ztráty dosažen")
        if sig.side != "long":
            return Decision(False, "short zatím není povolen")
        if sig.symbol in held_symbols:
            return Decision(False, "pozice v symbolu už existuje (žádné přikupování)")
        if len(account.positions) >= c.max_open_positions:
            return Decision(False, "max. počet otevřených pozic")
        if st.trades_today >= c.max_trades_per_day:
            return Decision(False, "max. počet obchodů za den")
        if st.trades_by_symbol.get(sig.symbol, 0) >= 2:
            return Decision(False, "max. 2 obchody na symbol za den")
        rpu = sig.risk_per_unit
        if rpu <= 0 or sig.stop <= 0:
            return Decision(False, "neplatný stop-loss")

        risk_budget = st.initial_capital * c.risk_per_trade_pct / 100 * min(max(sig.size_factor, 0.0), 1.0)
        units = risk_budget / rpu
        max_notional = min(account.equity * c.max_position_pct / 100, account.cash * 0.98)
        if units * sig.entry > max_notional:
            units = max_notional / sig.entry
        if max_units is not None:
            units = min(units, max_units)
        amount = units * sig.entry
        if amount < min_amount:
            return Decision(False, f"pozice příliš malá ({amount:.2f} USD)")
        risk_usd = units * rpu

        # Kdyby se zastavily všechny otevřené pozice i tato, nesmí se překročit denní limit.
        lost_today = max(st.day_start_equity - account.equity, 0.0)
        if lost_today + open_risk_usd + risk_usd > self.daily_limit_usd(st):
            return Decision(False, "obchod by mohl překročit denní limit ztráty")
        return Decision(True, "ok", round(amount, 2), units, risk_usd)

    def register_trade(self, st: RiskState, symbol: str) -> None:
        st.trades_today += 1
        st.trades_by_symbol[symbol] = st.trades_by_symbol.get(symbol, 0) + 1
