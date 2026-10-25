"""Backtest swing strategií na denních svíčkách.

Stejné rozhodování jako živý agent: na close dne se vyhodnotí stop-lossy otevřených pozic
(podle low dne), výstupy a vstupy; obchod se plní za close dne se skluzem a poplatky eToro.
Pozice otevřená dnes se poprvé kontroluje proti stop-lossu až zítra (žádný pohled zpět).
"""

from __future__ import annotations

from datetime import date, datetime

from . import market
from .backtest import Result, TradeRecord
from .broker import PaperBroker
from .config import Config
from .models import Candle
from .risk import RiskEngine, RiskState
from .strategy import Strategy


def run_daily_backtest(cfg: Config, strategy: Strategy, data: dict[str, list[Candle]],
                       initial: float, days: list[date] | None = None) -> Result:
    broker = PaperBroker(initial, cfg.costs)
    risk = RiskEngine(cfg.risk)
    by_day: dict[date, dict[str, Candle]] = {}
    for sym, candles in data.items():
        for c in candles:
            by_day.setdefault(market.to_et(c.time).date(), {})[sym] = c
    all_days = sorted(by_day)
    active_days = set(days or all_days)
    st = RiskState(initial, "", initial)
    result = Result(initial, initial)
    history: dict[str, list[Candle]] = {s: [] for s in data}
    open_info: dict[str, tuple[datetime, float]] = {}

    def record(fills):
        for f in fills:
            t0, e0 = open_info.pop(f.position.position_id)
            result.trades.append(TradeRecord(f.position.symbol, t0, f.time, e0, f.price, f.pnl, f.reason,
                                             f.costs, f.position.invested))

    for d in all_days:
        bars = by_day[d]
        risk.roll_day(st, d, broker.account().equity)
        for sym, c in sorted(bars.items()):
            record(broker.on_bar(sym, c))       # stopy pozic z minulých dnů
            history[sym].append(c)
        if d not in active_days:
            continue
        account = broker.account()
        if "halt" in risk.check_limits(st, account.equity) or (
                st.daily_blocked and cfg.risk.flatten_on_daily_limit):
            for p in list(broker.positions.values()):
                broker.close(p, reason="limit", price_hint=broker.last_price.get(p.symbol), when=None)
                record([broker.last_closed])
        # výstupy
        for p in list(broker.positions.values()):
            if p.symbol not in bars:
                continue
            reason = strategy.exit(p, history[p.symbol][-strategy.history_bars:])
            if reason:
                c = bars[p.symbol]
                broker.close(p, reason=reason, price_hint=c.close, when=c.time)
                record([broker.last_closed])
        # vstupy (za close dne)
        for sym, c in sorted(bars.items()):
            account = broker.account()
            if any(p.symbol == sym for p in broker.positions.values()):
                continue
            sig = strategy.entry(sym, history[sym][-strategy.history_bars:])
            if sig is None:
                continue
            open_risk = sum(max(p.units * (broker.last_price.get(p.symbol, p.open_price) - p.stop), 0)
                            for p in broker.positions.values() if p.stop)
            dec = risk.evaluate(sig, st, account, open_risk, {p.symbol for p in broker.positions.values()})
            if not dec.accepted:
                result.rejected += 1
                continue
            res = broker.open_long(symbol=sym, instrument_id=0, amount_usd=dec.amount_usd, stop=sig.stop,
                                   target=sig.target, client_ref="bt", price_hint=c.close, when=c.time)
            if res.ok:
                open_info[res.position_ids[0]] = (c.time, res.fill_price)
                risk.register_trade(st, sym)
        result.daily_equity.append((d, broker.account().equity))

    # na konci testu se vše zavře (i s poplatkem), aby statistiky obchodů byly úplné
    for p in list(broker.positions.values()):
        broker.close(p, reason="konec testu", price_hint=broker.last_price.get(p.symbol), when=None)
        record([broker.last_closed])
    result.final = broker.account().equity
    if result.daily_equity:
        result.daily_equity[-1] = (result.daily_equity[-1][0], result.final)
    return result


def walk_forward_daily(cfg: Config, strategy: Strategy, data: dict[str, list[Candle]], initial: float,
                       dev_fraction: float = 0.7) -> dict[str, dict]:
    days = sorted({market.to_et(c.time).date() for cs in data.values() for c in cs})
    if len(days) < 300:
        raise ValueError("Pro swing walk-forward je potřeba aspoň 300 obchodních dnů dat")
    cut = int(len(days) * dev_fraction)
    dev = run_daily_backtest(cfg, strategy, data, initial, days[:cut])
    test = run_daily_backtest(cfg, strategy, data, initial, days[cut:])
    return {"vyvoj": {"od": str(days[0]), "do": str(days[cut - 1]), **dev.metrics()},
            "test": {"od": str(days[cut]), "do": str(days[-1]), **test.metrics()}}


def buy_and_hold(data: dict[str, list[Candle]], days: list[date]) -> dict:
    """„Kup a drž“ rovnoměrně všech instrumentů za období (bez poplatků): výnos, max. propad, Sharpe."""
    lo, hi = days[0], days[-1]
    series: dict[str, dict[date, float]] = {}
    for sym, cs in data.items():
        sel = {market.to_et(c.time).date(): c.close for c in cs if lo <= market.to_et(c.time).date() <= hi}
        if sel and min(sel) == lo:
            series[sym] = sel
    if not series:
        return {"vynos_pct": 0.0, "max_drawdown_pct": 0.0, "sharpe": 0.0}
    values, last = [], {s: v[lo] for s, v in series.items()}
    for d in days:
        for s, v in series.items():
            if d in v:
                last[s] = v[d]
        values.append(sum(last[s] / series[s][lo] for s in series) / len(series))
    peak, max_dd = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak)
    rets = [b / a - 1 for a, b in zip(values, values[1:])]
    mean = sum(rets) / len(rets) if rets else 0
    sd = (sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5 if len(rets) > 1 else 0
    return {"vynos_pct": round((values[-1] - 1) * 100, 2), "max_drawdown_pct": round(max_dd * 100, 2),
            "sharpe": round(mean / sd * 252 ** 0.5, 2) if sd else 0.0}
