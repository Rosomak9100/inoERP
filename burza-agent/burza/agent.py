"""Hlavní smyčka agenta: data → strategie → risk → broker → deník."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import market
from .broker import Broker, EtoroBroker, PaperBroker
from .config import Config
from .etoro import EtoroClient, EtoroError
from .journal import Journal
from .models import Account, Candle, Position
from .risk import RiskEngine, RiskState
from .strategy import Strategy

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LiveData:
    """Uzavřené svíčky z eToro. Stahuje znovu, až když se uzavřela další svíčka."""

    def __init__(self, client: EtoroClient, interval: str, interval_minutes: int, count: int = 1000):
        self.client = client
        self.interval = interval
        self.step = timedelta(minutes=interval_minutes)
        self.count = count
        self._cache: dict[int, list[Candle]] = {}

    def closed_candles(self, instrument_id: int, now: datetime) -> list[Candle]:
        cached = self._cache.get(instrument_id)
        if cached and cached[-1].time + 2 * self.step > now:
            return cached  # další svíčka ještě není uzavřená
        candles = self.client.live_candles(instrument_id, self.interval, self.count)
        # Rozpracovanou svíčku zahodit – strategie vidí jen uzavřené (žádný look-ahead).
        closed = [c for c in candles if c.time + self.step <= now]
        self._cache[instrument_id] = closed
        return closed


class Agent:
    def __init__(self, cfg: Config, journal: Journal, broker: Broker, data: LiveData,
                 strategy: Strategy, clock=utcnow):
        self.cfg = cfg
        self.journal = journal
        self.broker = broker
        self.data = data
        self.strategy = strategy
        self.risk = RiskEngine(cfg.risk)
        self.clock = clock
        self.kill_file = Path(cfg.kill_file)
        self.heartbeat_file = Path(cfg.data_dir) / "heartbeat"
        if hasattr(strategy, "bind"):  # Studie B potřebuje deník (evidence zpráv a rozhodnutí)
            strategy.bind(journal, clock)

    # --- stav ---------------------------------------------------------------------
    def _load_risk_state(self, account: Account, now: datetime) -> RiskState:
        raw = self.journal.get_state(f"risk:{self.broker.mode}")
        if raw:
            return RiskState.from_dict(raw)
        initial = self.cfg.initial_capital_usd or account.equity
        st = RiskState(initial_capital=initial, day=market.trading_date(now).isoformat(),
                       day_start_equity=account.equity)
        self.journal.event("start", f"Počáteční kapitál pro limity: {initial:.2f} USD",
                           data={"mode": self.broker.mode})
        return st

    def _save(self, st: RiskState) -> None:
        self.journal.set_state(f"risk:{self.broker.mode}", st.to_dict())
        if isinstance(self.broker, PaperBroker):
            self.journal.set_state("paper_broker", self.broker.to_dict())

    def _own_positions(self, account: Account) -> list[Position]:
        if isinstance(self.broker, PaperBroker):
            return account.positions
        own = {r["position_id"] for r in self.journal.open_trades(self.broker.mode)}
        return [p for p in account.positions if p.position_id in own]

    # --- akce ---------------------------------------------------------------------
    def recover_pending(self) -> None:
        for row in self.journal.pending_orders():
            if isinstance(self.broker, EtoroBroker):
                res = self.broker.recover(row["client_ref"])
                if res.status == "unknown":
                    continue  # pořád se zpracovává, zkusí se v dalším cyklu
                self.journal.order_update(row["client_ref"], res.status, raw=res.raw)
                if res.ok:
                    for pid in res.position_ids:
                        self.journal.trade_open(pid, row["client_ref"], row["symbol"], row["instrument_id"],
                                                self.broker.mode, res.fill_price or 0, res.units or 0,
                                                row["stop"], row["target"])
                self.journal.event("recover", f"Dohledána objednávka {row['client_ref']}: {res.status}",
                                   level="WARNING")
            else:
                self.journal.order_update(row["client_ref"], "lost")

    def _close(self, p: Position, reason: str, price: float | None) -> None:
        res = self.broker.close(p, reason=reason, price_hint=price)
        if not res.ok:
            self.journal.event("close_error", f"Nepodařilo se zavřít {p.symbol} {p.position_id}: {res.error}",
                               level="ERROR")
            return
        if res.status == "filled":  # paper: známý přesný výsledek
            raw = res.raw or {}
            self.journal.trade_close(p.position_id, res.fill_price, raw.get("pnl"), "simulace", reason,
                                     costs=raw.get("costs"))
        else:
            reasons = self.journal.get_state("close_reasons", {})
            reasons[p.position_id] = reason
            self.journal.set_state("close_reasons", reasons)
        self.journal.event("close", f"Zavírám {p.symbol} ({reason})", data={"position": p.position_id})

    def flatten(self, account: Account, reason: str, prices: dict[str, float]) -> None:
        for p in self._own_positions(account):
            self._close(p, reason, prices.get(p.symbol))

    def _sync_closed(self, account: Account, prices: dict[str, float], now: datetime) -> None:
        """Pozice, které z eToro zmizely (stop/target/naše zavření) → uzavřít v deníku.
        P/L se bere přesně z historie obchodů eToro; odhad z poslední ceny jen když historie
        ani po 15 minutách pozici neobsahuje (a později se opraví v _reconcile)."""
        if isinstance(self.broker, PaperBroker):
            return
        live = {p.position_id for p in account.positions}
        gone = [r for r in self.journal.open_trades(self.broker.mode) if r["position_id"] not in live]
        if not gone:
            return
        reasons = self.journal.get_state("close_reasons", {})
        missing = self.journal.get_state("missing_since", {})
        exact = {}
        try:
            exact = self.broker.closed_history(min(r["open_ts"][:10] for r in gone))
        except EtoroError as exc:
            self.journal.event("history_error", f"Historie obchodů nedostupná: {exc}", level="WARNING")
        for row in gone:
            pid = row["position_id"]
            reason = reasons.get(pid, "uzavřeno na eToro (stop/target)")
            ex = exact.get(pid)
            if ex:
                self.journal.trade_close(pid, ex.close_price, ex.pnl, "eToro", reason, ex.close_time or None, ex.fees)
            else:
                first = datetime.fromisoformat(missing.setdefault(pid, now.isoformat()))
                if now - first < timedelta(minutes=15):
                    continue
                px = prices.get(row["symbol"])
                pnl = (px - row["open_price"]) * row["units"] if px and row["units"] else None
                self.journal.trade_close(pid, px, pnl, "odhad", reason)
            reasons.pop(pid, None)
            missing.pop(pid, None)
        self.journal.set_state("close_reasons", reasons)
        self.journal.set_state("missing_since", missing)

    def _reconcile(self, now: datetime) -> None:
        """Jednou za hodinu nahradí odhadnuté P/L přesnými čísly z historie eToro."""
        if isinstance(self.broker, PaperBroker):
            return
        last = self.journal.get_state("last_reconcile")
        if last and now - datetime.fromisoformat(last) < timedelta(hours=1):
            return
        self.journal.set_state("last_reconcile", now.isoformat())
        rows = self.journal.trades_needing_exact(self.broker.mode)
        if not rows:
            return
        try:
            exact = self.broker.closed_history(min(r["open_ts"][:10] for r in rows))
        except EtoroError:
            return
        for r in rows:
            ex = exact.get(r["position_id"])
            if ex:
                self.journal.event("reconcile", f"{r['symbol']}: P/L opraveno z eToro "
                                                f"({r['pnl']} → {ex.pnl:.2f})", data={"position": r["position_id"]})
                self.journal.trade_set_exact(r["position_id"], ex.close_price, ex.pnl,
                                             ex.close_time or r["close_ts"], ex.fees)

    def _place(self, sig, decision, iid: int, st: RiskState) -> None:
        client_ref = str(uuid.uuid4())
        self.journal.order_intent(client_ref, sig.symbol, iid, "open", decision.amount_usd, sig.stop, sig.target)
        res = self.broker.open_long(symbol=sig.symbol, instrument_id=iid, amount_usd=decision.amount_usd,
                                    stop=sig.stop, target=sig.target, client_ref=client_ref,
                                    price_hint=sig.entry)
        self.journal.order_update(client_ref, res.status, raw=res.raw if res.raw else {"error": res.error})
        if res.status == "unknown":
            # Nevíme, jestli proběhla – pozice se dohledá přes recover_pending, nic dalšího neposílat.
            self.journal.event("order_unknown", f"{sig.symbol}: stav objednávky nejasný", level="ERROR")
            self.journal.order_update(client_ref, "pending")
            return
        if not res.ok:
            self.journal.event("order_rejected", f"{sig.symbol}: {res.error}", level="WARNING")
            return
        for pid in res.position_ids:
            self.journal.trade_open(pid, client_ref, sig.symbol, iid, self.broker.mode,
                                    res.fill_price or sig.entry, res.units or 0, sig.stop, sig.target)
        self.risk.register_trade(st, sig.symbol)
        self.journal.event("open", f"Otevřeno {sig.symbol} {decision.amount_usd:.2f} USD "
                                   f"stop {sig.stop:.2f} cíl {sig.target:.2f}", data={"ref": client_ref})

    # --- jeden cyklus -------------------------------------------------------------
    def step(self) -> str:
        now = self.clock()
        self.heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
        self.heartbeat_file.write_text(now.isoformat())

        if isinstance(self.broker, EtoroBroker):
            self.recover_pending()
        account = self.broker.account()
        st = self._load_risk_state(account, now)
        prices: dict[str, float] = self.journal.get_state("last_prices", {})

        if self.kill_file.exists():
            self.flatten(account, "kill switch", prices)
            self.journal.event("kill", "Kill switch aktivní – vše zavřeno, nové obchody zakázány",
                               level="CRITICAL")
            self._save(st)
            return "killed"

        today = market.trading_date(now)
        if market.is_trading_day(today) and self.risk.roll_day(st, today, account.equity):
            self.journal.event("new_day", f"Nový obchodní den {today}, equity {account.equity:.2f}")

        self._sync_closed(account, prices, now)
        self._reconcile(now)
        open_pnl = sum(p.unrealized_pnl for p in account.positions)
        self.journal.equity_point(account.equity, account.cash, open_pnl,
                                  prices.get(self.cfg.benchmark_symbol))

        for action in self.risk.check_limits(st, account.equity):
            if action == "halt":
                self.journal.event("halt", st.halt_reason + " – vše zavřeno, nutné nové schválení",
                                   level="CRITICAL")
                self.flatten(account, "celkový limit ztráty", prices)
            elif action == "daily":
                self.journal.event("daily_limit", "Denní limit ztráty – žádné nové obchody dnes",
                                   level="WARNING")
                if self.cfg.risk.flatten_on_daily_limit:
                    self.flatten(account, "denní limit ztráty", prices)

        if not market.is_open(now):
            self._save(st)
            return "market_closed"

        if market.minutes_to_close(now) <= self.cfg.session.flatten_minutes_before_close:
            if self._own_positions(account):
                self.flatten(account, "konec seance", prices)
            self._save(st)
            return "session_end"

        entry_window = (market.minutes_since_open(now) >= self.cfg.session.no_entry_first_minutes and
                        market.minutes_to_close(now) >= self.cfg.session.no_entry_last_minutes)
        last_bars: dict[str, str] = self.journal.get_state("last_bar", {})

        for symbol, iid in self.cfg.instruments.items():
            try:
                candles = self.data.closed_candles(iid, now)
            except EtoroError as exc:
                self.journal.event("data_error", f"{symbol}: {exc}", level="WARNING")
                continue
            if not candles:
                continue
            last = candles[-1]
            age = (now - last.time).total_seconds() - self.cfg.interval_minutes * 60
            if age > self.cfg.session.stale_data_seconds:
                self.journal.event("stale", f"{symbol}: data stará {age:.0f}s – neobchoduji", level="WARNING")
                continue
            prices[symbol] = last.close

            seen = last_bars.get(symbol)
            new_bars = [c for c in candles if seen is None or c.time.isoformat() > seen]
            if not new_bars:
                continue
            last_bars[symbol] = last.time.isoformat()

            if isinstance(self.broker, PaperBroker):
                for c in new_bars:
                    for fill in self.broker.on_bar(symbol, c):
                        self.journal.trade_close(fill.position.position_id, fill.price, fill.pnl,
                                                 "simulace", fill.reason, fill.time.isoformat(), fill.costs)
                account = self.broker.account()

            own = [p for p in self._own_positions(account) if p.symbol == symbol]
            if own:
                reason = self.strategy.exit(own[0], candles)
                if reason:
                    for p in own:
                        self._close(p, reason, last.close)
                continue
            if not entry_window or st.halted or st.daily_blocked:
                continue
            if symbol in {r["symbol"] for r in self.journal.pending_orders()}:
                continue  # ochrana proti duplicitní objednávce
            sig = self.strategy.entry(symbol, candles)
            if sig is None:
                continue
            own_all = self._own_positions(account)
            open_risk = sum(max(p.units * (prices.get(p.symbol, p.open_price) - p.stop), 0)
                            for p in own_all if p.stop)
            decision = self.risk.evaluate(sig, st, account, open_risk, {p.symbol for p in account.positions},
                                          max_units=self.broker.max_units(iid))
            self.journal.signal(sig, self.strategy.name, decision.accepted, decision.reason)
            if decision.accepted:
                self._place(sig, decision, iid, st)
                account = self.broker.account()

        bench = self.cfg.benchmark_symbol
        if bench and bench not in self.cfg.instruments:
            try:
                bc = self.data.closed_candles(self.cfg.benchmark_instrument_id, now)
                if bc:
                    prices[bench] = bc[-1].close
            except EtoroError:
                pass
        self.journal.set_state("last_bar", last_bars)
        self.journal.set_state("last_prices", prices)
        self._save(st)
        return "ok"

    def run_forever(self) -> None:
        self.recover_pending()
        self.journal.event("start", f"Agent spuštěn v režimu {self.broker.mode}, strategie {self.strategy.name}")
        failures = 0
        while True:
            try:
                status = self.step()
                failures = 0
                if status == "killed":
                    log.critical("Kill switch – agent končí")
                    return
            except Exception as exc:  # watchdog: chyba jednoho cyklu nesmí shodit agenta
                failures += 1
                log.exception("Chyba v cyklu")
                self.journal.event("error", f"{type(exc).__name__}: {exc}", level="ERROR")
                if failures >= 10:
                    self.journal.event("error", "10 chyb za sebou – agent se zastavuje", level="CRITICAL")
                    raise
            now = self.clock()
            sleep = self.cfg.poll_seconds
            if not market.is_open(now):
                sleep = min(max((market.next_open(now) - now).total_seconds(), sleep), 600)
            time.sleep(sleep)
