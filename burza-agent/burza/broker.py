"""Brokery: PaperBroker (vlastní simulace) a EtoroBroker (demo / real přes API)."""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from .config import CostConfig
from .etoro import ORDER_FILLED, ORDER_REJECTED, EtoroClient, EtoroError, ci_get, parse_time
from .models import Account, Candle, OrderResult, Position

log = logging.getLogger(__name__)


@dataclass
class ClosedFill:
    position: Position
    price: float
    pnl: float
    reason: str
    time: datetime
    costs: float = 0.0


@dataclass
class ExactClose:
    position_id: str
    close_price: float
    pnl: float
    close_time: str
    fees: float | None


class Broker:
    mode = "base"

    def account(self) -> Account:
        raise NotImplementedError

    def open_long(self, *, symbol: str, instrument_id: int, amount_usd: float, stop: float,
                  target: float, client_ref: str, price_hint: float) -> OrderResult:
        raise NotImplementedError

    def close(self, position: Position, *, reason: str, price_hint: float | None) -> OrderResult:
        raise NotImplementedError

    def max_units(self, instrument_id: int) -> float | None:
        return None

    def closed_history(self, min_date: str) -> dict[str, ExactClose]:
        return {}


# ---------------------------------------------------------------------------------------
class PaperBroker(Broker):
    """Simulace s náklady (spread, skluz, poplatek). Stop/target se kontrolují na svíčkách;
    když svíčka zasáhne stop i target, počítá se konzervativně stop. Gap přes stop = fill na open."""

    mode = "paper"

    def __init__(self, cash: float, costs: CostConfig):
        self.cash = cash
        self.costs = costs
        self.positions: dict[str, Position] = {}
        self.last_price: dict[str, float] = {}
        self.entry_costs: dict[str, float] = {}
        self.next_id = 1

    # persistence mezi restarty
    def to_dict(self) -> dict:
        return {"cash": self.cash, "next_id": self.next_id, "last_price": self.last_price,
                "entry_costs": self.entry_costs,
                "positions": [{**asdict(p), "open_time": p.open_time.isoformat() if p.open_time else None}
                              for p in self.positions.values()]}

    @classmethod
    def from_dict(cls, d: dict, costs: CostConfig) -> "PaperBroker":
        b = cls(d["cash"], costs)
        b.next_id = d["next_id"]
        b.last_price = d.get("last_price", {})
        b.entry_costs = d.get("entry_costs", {})
        for p in d["positions"]:
            p["open_time"] = datetime.fromisoformat(p["open_time"]) if p["open_time"] else None
            b.positions[p["position_id"]] = Position(**p)
        return b

    def _half_cost(self, symbol: str) -> float:
        return (self.costs.spread(symbol) / 2 + self.costs.slippage_bps) / 10_000

    def account(self) -> Account:
        positions = []
        value = 0.0
        for p in self.positions.values():
            px = self.last_price.get(p.symbol, p.open_price)
            p.unrealized_pnl = p.units * (px - p.open_price)
            value += p.units * px
            positions.append(p)
        return Account(self.cash + value, self.cash, positions)

    def open_long(self, *, symbol, instrument_id, amount_usd, stop, target, client_ref,
                  price_hint, when: datetime | None = None) -> OrderResult:
        fill = price_hint * (1 + self._half_cost(symbol))
        fee = self.costs.fee(symbol)
        spend = amount_usd
        if spend > self.cash:
            return OrderResult(False, client_ref, "rejected", error="nedostatek hotovosti")
        if spend <= fee * 2:
            return OrderResult(False, client_ref, "rejected", error="pozice menší než poplatky")
        units = (spend - fee) / fill
        self.cash -= spend
        pid = f"P{self.next_id}"
        self.next_id += 1
        self.positions[pid] = Position(pid, symbol, instrument_id, units, fill, stop, target,
                                       when or datetime.now(timezone.utc), 0.0, spend)
        # náklady vstupu: commission + rozdíl mezi fill a midem
        self.entry_costs[pid] = fee + units * (fill - price_hint)
        self.last_price.setdefault(symbol, price_hint)
        return OrderResult(True, client_ref, "filled", [pid], fill, units)

    def _close_at(self, p: Position, price: float, reason: str, when: datetime) -> ClosedFill:
        exit_px = price * (1 - self._half_cost(p.symbol))
        fee = self.costs.fee(p.symbol)
        proceeds = p.units * exit_px - fee
        self.cash += proceeds
        del self.positions[p.position_id]
        costs = self.entry_costs.pop(p.position_id, 0.0) + fee + p.units * (price - exit_px)
        return ClosedFill(p, exit_px, proceeds - p.invested, reason, when, round(costs, 4))

    def close(self, position: Position, *, reason: str, price_hint: float | None,
              when: datetime | None = None) -> OrderResult:
        p = self.positions.get(position.position_id)
        if p is None:
            return OrderResult(False, "", "missing", error="pozice neexistuje")
        px = price_hint or self.last_price.get(p.symbol, p.open_price)
        fill = self._close_at(p, px, reason, when or datetime.now(timezone.utc))
        self.last_closed = fill
        return OrderResult(True, "", "filled", [p.position_id], fill.price, p.units,
                           raw={"pnl": fill.pnl, "costs": fill.costs})

    def on_bar(self, symbol: str, c: Candle) -> list[ClosedFill]:
        """Zpracuje novou svíčku: aktualizuje cenu a vyhodnotí stop/target."""
        closed = []
        for p in [p for p in self.positions.values() if p.symbol == symbol]:
            if p.stop is not None and c.low <= p.stop:
                closed.append(self._close_at(p, min(p.stop, c.open), "stop-loss", c.time))
            elif p.target is not None and c.high >= p.target:
                closed.append(self._close_at(p, max(p.target, c.open), "take-profit", c.time))
        self.last_price[symbol] = c.close
        return closed


# ---------------------------------------------------------------------------------------
class EtoroBroker(Broker):
    """Obchoduje přes eToro API. demo=True → virtuální účet, demo=False → skutečné peníze
    (typicky Agent Portfolio). Spravuje jen pozice uvedené v `own_position_ids`."""

    def __init__(self, client: EtoroClient, demo: bool, instruments: dict[str, int],
                 poll_timeout: float = 20.0):
        self.client = client
        self.demo = demo
        self.mode = "demo" if demo else "real"
        self.instruments = instruments
        self.symbol_by_id = {v: k for k, v in instruments.items()}
        self.poll_timeout = poll_timeout
        self.settlement: dict[int, str] = {}
        self.limits: dict[int, dict] = {}

    def prepare(self) -> dict[str, str]:
        """Načte eligibility a zvolí settlementType pro long bez páky. Vrací problémy."""
        data = self.client.eligibility(list(self.instruments.values()), self.demo)
        problems: dict[str, str] = {}
        for e in data.get("eligibilities", []):
            iid = e["instrumentId"]
            sym = self.symbol_by_id.get(iid, str(iid))
            if not e.get("allowOpenPosition", False):
                problems[sym] = "otevírání pozic není povoleno"
                continue
            options = [lc["settlementType"].lower() for lc in e.get("leverageConfigs", [])
                       if lc.get("direction", "").lower() == "long" and 1 in lc.get("leverageValues", [])
                       and not lc.get("isPotential", False)]
            if not options:
                problems[sym] = "chybí long bez páky"
                continue
            self.settlement[iid] = "real" if "real" in options else options[0]
            self.limits[iid] = {"max_units": e.get("maxUnitsPerOrder"),
                                "min_exposure": e.get("minPositionExposure", 10)}
        for iid in data.get("notFoundInstrumentIds", []):
            problems[self.symbol_by_id.get(iid, str(iid))] = "instrument nenalezen"
        return problems

    def max_units(self, instrument_id: int) -> float | None:
        return (self.limits.get(instrument_id) or {}).get("max_units")

    def account(self) -> Account:
        pf = self.client.portfolio_pnl(self.demo).get("clientPortfolio", {})
        cash = float(pf.get("credit") or 0)
        positions = []
        invested_total = 0.0
        for raw in pf.get("positions", []):
            iid = int(ci_get(raw, "instrumentId", "instrumentID"))
            amount = float(ci_get(raw, "amount", default=0) or 0)
            upnl = ci_get(raw, "unrealizedPnL", default=None)
            pnl = float(upnl.get("pnL", 0)) if isinstance(upnl, dict) else float(ci_get(raw, "pnL", default=0) or 0)
            opened = ci_get(raw, "openDateTime")
            positions.append(Position(
                position_id=str(ci_get(raw, "positionId", "positionID")),
                symbol=self.symbol_by_id.get(iid, str(iid)),
                instrument_id=iid,
                units=float(ci_get(raw, "units", default=0) or 0),
                open_price=float(ci_get(raw, "openRate", default=0) or 0),
                stop=ci_get(raw, "stopLossRate"),
                target=ci_get(raw, "takeProfitRate"),
                open_time=parse_time(opened) if opened else None,
                unrealized_pnl=pnl,
                invested=amount,
            ))
            invested_total += amount + pnl
        return Account(cash + invested_total, cash, positions)

    def _wait_for_order(self, client_ref: str) -> dict:
        deadline = time.monotonic() + self.poll_timeout
        info: dict = {}
        while time.monotonic() < deadline:
            try:
                info = self.client.lookup_order(demo=self.demo, reference_id=client_ref)
            except EtoroError as exc:
                if exc.status != 404:
                    raise
            status = (info.get("status") or {}).get("id")
            if status in ORDER_FILLED or status in ORDER_REJECTED:
                return info
            time.sleep(1.5)
        return info

    @staticmethod
    def result_from_lookup(client_ref: str, info: dict) -> OrderResult:
        status = info.get("status") or {}
        sid = status.get("id")
        if sid in ORDER_FILLED:
            execs = info.get("positionExecutions") or []
            pids = [str(e["positionId"]) for e in execs]
            units = sum(float((e.get("openingData") or {}).get("units") or e.get("remainingUnits") or 0)
                        for e in execs)
            prices = [float((e.get("openingData") or {}).get("avgPrice") or 0) for e in execs]
            return OrderResult(True, client_ref, "filled", pids, prices[0] if prices else None,
                               units, raw=info)
        if sid in ORDER_REJECTED:
            return OrderResult(False, client_ref, "rejected",
                               error=f"{status.get('errorCode')}: {status.get('errorMessage')}", raw=info)
        return OrderResult(False, client_ref, "unknown", error="stav objednávky nepotvrzen", raw=info)

    def open_long(self, *, symbol, instrument_id, amount_usd, stop, target, client_ref,
                  price_hint) -> OrderResult:
        settlement = self.settlement.get(instrument_id)
        if settlement is None:
            return OrderResult(False, client_ref, "rejected", error="instrument není připraven (prepare)")
        try:
            resp = self.client.open_market_order(
                demo=self.demo, instrument_id=instrument_id, amount_usd=amount_usd,
                settlement_type=settlement, stop_loss=round(stop, 4), take_profit=round(target, 4),
                request_id=client_ref)
        except EtoroError as exc:
            return OrderResult(False, client_ref, "rejected" if 400 <= exc.status < 500 else "unknown",
                               error=str(exc))
        log.info("Objednávka přijata %s: %s", symbol, resp)
        return self.result_from_lookup(client_ref, self._wait_for_order(client_ref))

    def recover(self, client_ref: str) -> OrderResult:
        """Po pádu: zjistí, jak dopadla objednávka zapsaná jako pending."""
        try:
            info = self.client.lookup_order(demo=self.demo, reference_id=client_ref)
        except EtoroError as exc:
            if exc.status == 404:
                return OrderResult(False, client_ref, "rejected", error="objednávka na eToro neexistuje")
            raise
        return self.result_from_lookup(client_ref, info)

    def closed_history(self, min_date: str) -> dict[str, ExactClose]:
        out = {}
        for row in self.client.trade_history(demo=self.demo, min_date=min_date):
            pid = ci_get(row, "positionId", "positionID")
            if pid is None or ci_get(row, "netProfit") is None:
                continue
            fees = ci_get(row, "fees")
            out[str(pid)] = ExactClose(str(pid), float(ci_get(row, "closeRate") or 0),
                                       float(ci_get(row, "netProfit")),
                                       str(ci_get(row, "closeTimestamp") or ""),
                                       float(fees) if fees is not None else None)
        return out

    def close(self, position: Position, *, reason: str, price_hint: float | None) -> OrderResult:
        try:
            resp = self.client.close_position(demo=self.demo, position_id=position.position_id,
                                              instrument_id=position.instrument_id)
        except EtoroError as exc:
            return OrderResult(False, "", "error", error=str(exc))
        order = resp.get("orderForClose") or {}
        return OrderResult(True, "", "submitted", [position.position_id], price_hint, position.units,
                           raw={"orderId": order.get("orderID"), "pnl": None})
