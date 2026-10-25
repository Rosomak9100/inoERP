"""Backtest se stejnou strategií, risk enginem a PaperBrokerem jako živý agent.

Signál na close svíčky t se plní na OPEN svíčky t+1 (žádný look-ahead). Stop/target se
vyhodnocují uvnitř svíčky; zásah obou = konzervativně stop. Na konci seance se vše zavře.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from . import market
from .broker import PaperBroker
from .config import Config
from .models import Candle
from .risk import RiskEngine, RiskState
from .strategy import Strategy


@dataclass
class TradeRecord:
    symbol: str
    open_time: datetime
    close_time: datetime
    entry: float
    exit: float
    pnl: float
    reason: str


@dataclass
class Result:
    initial: float
    final: float
    trades: list[TradeRecord] = field(default_factory=list)
    daily_equity: list[tuple[date, float]] = field(default_factory=list)
    rejected: int = 0

    def metrics(self) -> dict:
        pnls = [t.pnl for t in self.trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        eq = [self.initial] + [e for _, e in self.daily_equity]
        peak, max_dd = eq[0], 0.0
        for e in eq:
            peak = max(peak, e)
            max_dd = max(max_dd, (peak - e) / peak if peak else 0)
        rets = [(b - a) / a for a, b in zip(eq, eq[1:]) if a]
        sharpe = 0.0
        if len(rets) > 1:
            mean = sum(rets) / len(rets)
            sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
            sharpe = mean / sd * math.sqrt(252) if sd else 0.0
        t_stat = None  # průměrný obchod / jeho směrodatná chyba; > 2 slušné, > 3 přesvědčivé
        if len(pnls) > 2:
            mp = sum(pnls) / len(pnls)
            sdp = math.sqrt(sum((p - mp) ** 2 for p in pnls) / (len(pnls) - 1))
            t_stat = mp / (sdp / math.sqrt(len(pnls))) if sdp else None
        return {
            "pocatecni_kapital": round(self.initial, 2),
            "konecny_kapital": round(self.final, 2),
            "vynos_pct": round((self.final / self.initial - 1) * 100, 2) if self.initial else 0,
            "obchodu": len(pnls),
            "win_rate_pct": round(len(wins) / len(pnls) * 100, 1) if pnls else 0,
            "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) else None,
            "max_drawdown_pct": round(max_dd * 100, 2),
            "sharpe": round(sharpe, 2),
            "t_stat": round(t_stat, 2) if t_stat is not None else None,
            "nejlepsi_obchod": round(max(pnls), 2) if pnls else 0,
            "nejhorsi_obchod": round(min(pnls), 2) if pnls else 0,
            "odmitnute_signaly": self.rejected,
        }


def group_by_day(candles: list[Candle]) -> dict[date, list[Candle]]:
    out: dict[date, list[Candle]] = {}
    for c in candles:
        d = market.to_et(c.time).date()
        start, end = market.session_bounds(d)
        if start <= market.to_et(c.time) < end:
            out.setdefault(d, []).append(c)
    return out


def flatten_all(broker: PaperBroker, reason: str, when: datetime | None) -> list:
    """Zavře všechny pozice, každou za SVOU poslední cenu (ne za cenu právě zpracovávané svíčky)."""
    fills = []
    for p in list(broker.positions.values()):
        broker.close(p, reason=reason, price_hint=broker.last_price.get(p.symbol), when=when)
        fills.append(broker.last_closed)
    return fills


def run_backtest(cfg: Config, strategy: Strategy, data: dict[str, list[Candle]],
                 initial: float, days: list[date] | None = None) -> Result:
    """data: symbol → svíčky vzestupně (pro indikátory můžou obsahovat i dny před `days`)."""
    broker = PaperBroker(initial, cfg.costs)
    risk = RiskEngine(cfg.risk)
    by_sym_day = {s: group_by_day(c) for s, c in data.items()}
    all_days = sorted({d for m in by_sym_day.values() for d in m})
    days = days or all_days
    st = RiskState(initial, "", initial)
    result = Result(initial, initial)
    sess_cfg = cfg.session
    history: dict[str, list[Candle]] = {s: [] for s in data}
    open_info: dict[str, tuple[datetime, float]] = {}

    def record(fills):
        for f in fills:
            t0, e0 = open_info.pop(f.position.position_id)
            result.trades.append(TradeRecord(f.position.symbol, t0, f.time, e0, f.price, f.pnl, f.reason))

    for d in all_days:
        active = d in days
        account = broker.account()
        risk.roll_day(st, d, account.equity)
        # časová osa dne přes všechny symboly
        bars = sorted(((c.time, s, c) for s in data for c in by_sym_day[s].get(d, [])),
                      key=lambda x: (x[0], x[1]))
        pending: dict[str, tuple] = {}  # symbol → (signal, decision) čeká na open další svíčky
        for ts, sym, c in bars:
            if sym in pending:
                sig, dec = pending.pop(sym)
                res = broker.open_long(symbol=sym, instrument_id=0, amount_usd=dec.amount_usd,
                                       stop=sig.stop, target=sig.target, client_ref="bt",
                                       price_hint=c.open, when=c.time)
                if res.ok:
                    open_info[res.position_ids[0]] = (c.time, res.fill_price)
                    risk.register_trade(st, sym)
            record(broker.on_bar(sym, c))
            history[sym].append(c)
            if not active:
                continue
            account = broker.account()
            if "halt" in risk.check_limits(st, account.equity) or (
                    st.daily_blocked and cfg.risk.flatten_on_daily_limit):
                record(flatten_all(broker, "limit", c.time))
            bar_end = c.time + timedelta(minutes=cfg.interval_minutes)
            if market.minutes_to_close(bar_end) <= sess_cfg.flatten_minutes_before_close:
                for p in [p for p in broker.positions.values() if p.symbol == sym]:
                    broker.close(p, reason="konec seance", price_hint=c.close, when=c.time)
                    record([broker.last_closed])
                continue
            hist = history[sym][-strategy.history_bars:]
            own = [p for p in broker.positions.values() if p.symbol == sym]
            if own:
                reason = strategy.exit(own[0], hist)
                if reason:
                    broker.close(own[0], reason=reason, price_hint=c.close, when=c.time)
                    record([broker.last_closed])
                continue
            last_minutes = getattr(strategy, "no_entry_last_minutes", None) or sess_cfg.no_entry_last_minutes
            if (market.minutes_since_open(bar_end) < sess_cfg.no_entry_first_minutes or
                    market.minutes_to_close(bar_end) < last_minutes):
                continue
            sig = strategy.entry(sym, hist)
            if sig is None:
                continue
            open_risk = sum(max(p.units * (broker.last_price.get(p.symbol, p.open_price) - p.stop), 0)
                            for p in broker.positions.values() if p.stop)
            held = {p.symbol for p in broker.positions.values()} | set(pending)
            dec = risk.evaluate(sig, st, account, open_risk, held)
            if dec.accepted:
                pending[sym] = (sig, dec)
            else:
                result.rejected += 1
        # pojistka: nic nepřenášet přes noc
        record(flatten_all(broker, "konec dne", None))
        if active:
            result.daily_equity.append((d, broker.account().equity))
    result.final = broker.account().equity
    return result


def walk_forward(cfg: Config, strategy: Strategy, data: dict[str, list[Candle]], initial: float,
                 dev_fraction: float = 0.7) -> dict[str, dict]:
    """Rozdělí dny na vývojovou a nezávislou testovací část a vrátí metriky obou."""
    days = sorted({d for c in data.values() for d in group_by_day(c)})
    if len(days) < 5:
        raise ValueError("Pro walk-forward je potřeba aspoň 5 obchodních dnů dat")
    cut = int(len(days) * dev_fraction)
    dev = run_backtest(cfg, strategy, data, initial, days[:cut])
    test = run_backtest(cfg, strategy, data, initial, days[cut:])
    return {"vyvoj": {"od": str(days[0]), "do": str(days[cut - 1]), **dev.metrics()},
            "test": {"od": str(days[cut]), "do": str(days[-1]), **test.metrics()}}


# --- cache dat ---------------------------------------------------------------------
def save_csv(path: Path, candles: list[Candle]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        for c in candles:
            w.writerow([c.time.isoformat(), c.open, c.high, c.low, c.close, c.volume])


def load_csv(path: Path) -> list[Candle]:
    with path.open() as f:
        return [Candle(datetime.fromisoformat(r["time"]), float(r["open"]), float(r["high"]),
                       float(r["low"]), float(r["close"]), float(r["volume"] or 0))
                for r in csv.DictReader(f)]
