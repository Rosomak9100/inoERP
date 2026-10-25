"""Příkazová řádka: python -m burza <příkaz>"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from . import market
from .agent import Agent, LiveData, utcnow
from .backtest import load_csv, run_backtest, save_csv, walk_forward
from .broker import EtoroBroker, PaperBroker
from .config import Config, ConfigError, load_config
from .etoro import EtoroClient
from .journal import Journal
from .risk import RiskState
from .strategy import make_strategy


def _client(cfg: Config) -> EtoroClient:
    if not (cfg.api_key and cfg.user_key):
        raise ConfigError("Chybí ETORO_API_KEY / ETORO_USER_KEY (tržní data se berou z eToro)")
    return EtoroClient(cfg.api_key, cfg.user_key)


def _broker(cfg: Config, journal: Journal, client: EtoroClient):
    if cfg.mode == "paper":
        saved = journal.get_state("paper_broker")
        if saved:
            return PaperBroker.from_dict(saved, cfg.costs)
        if cfg.initial_capital_usd <= 0:
            raise ConfigError("Pro paper režim nastav initial_capital_usd > 0")
        return PaperBroker(cfg.initial_capital_usd, cfg.costs)
    broker = EtoroBroker(client, demo=cfg.mode == "demo", instruments=cfg.instruments)
    problems = broker.prepare()
    for sym, msg in problems.items():
        journal.event("eligibility", f"{sym}: {msg}", level="WARNING")
        print(f"VAROVÁNÍ {sym}: {msg}")
    return broker


def cmd_check(cfg: Config, args) -> int:
    client = _client(cfg)
    me = client.me()
    print("Připojení OK.")
    scopes = me.get("scopes") or me.get("profile", {}).get("scopes") or []
    if scopes:
        print("Oprávnění klíče:", ", ".join(scopes))
        if any("real" in s and "write" in s for s in scopes) and cfg.mode != "real":
            print("POZOR: klíč umí obchodovat se skutečnými penězi. Pro demo/paper použij klíč jen s demo právy.")
    broker = EtoroBroker(client, demo=cfg.mode != "real", instruments=cfg.instruments)
    problems = broker.prepare()
    for sym, iid in cfg.instruments.items():
        st = broker.settlement.get(iid)
        print(f"  {sym:6} id={iid:<6} settlement={st or '-'} {problems.get(sym, '')}")
        candles = client.live_candles(iid, cfg.interval, 5)
        if candles:
            age = (utcnow() - candles[-1].time).total_seconds() / 60
            print(f"         poslední svíčka {candles[-1].time:%Y-%m-%d %H:%M} UTC (před {age:.0f} min), "
                  f"close {candles[-1].close}, objem {candles[-1].volume:.0f}")
    acct = broker.account()
    print(f"Účet ({broker.mode}): equity {acct.equity:.2f} USD, volné {acct.cash:.2f} USD, "
          f"pozic {len(acct.positions)}")
    print("Trh je", "OTEVŘENÝ" if market.is_open(utcnow()) else "zavřený")
    return 0


def cmd_fetch(cfg: Config, args) -> int:
    client = _client(cfg)
    end = utcnow()
    start = end - timedelta(days=args.days)
    for sym, iid in cfg.instruments.items():
        candles = client.history_candles(iid, cfg.interval, start, end)
        path = Path(cfg.data_dir) / "history" / f"{sym}_{cfg.interval}.csv"
        save_csv(path, candles)
        print(f"{sym}: {len(candles)} svíček → {path}")
    return 0


def cmd_backtest(cfg: Config, args) -> int:
    data = {}
    for sym in cfg.instruments:
        path = Path(cfg.data_dir) / "history" / f"{sym}_{cfg.interval}.csv"
        if not path.exists():
            print(f"Chybí {path} – nejdřív spusť: python -m burza fetch --days {args.days}")
            return 1
        data[sym] = load_csv(path)
    strategy = make_strategy(args.strategy or cfg.strategy)
    initial = cfg.initial_capital_usd or 10_000.0
    if args.walk_forward:
        out = walk_forward(cfg, strategy, data, initial)
    else:
        out = run_backtest(cfg, strategy, data, initial).metrics()
    out = {"strategie": strategy.name, "interval": cfg.interval, "naklady": vars(cfg.costs), **out}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    log_path = Path(cfg.data_dir) / "backtests.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:  # výsledky se jen přidávají, nepřepisují
        f.write(json.dumps({"ts": utcnow().isoformat(), "risk": vars(cfg.risk), **out},
                           ensure_ascii=False) + "\n")
    return 0


def _agent(cfg: Config) -> tuple[Agent, Journal]:
    journal = Journal(cfg.db_path)
    client = _client(cfg)
    broker = _broker(cfg, journal, client)
    data = LiveData(client, cfg.interval, cfg.interval_minutes)
    return Agent(cfg, journal, broker, data, make_strategy(cfg.strategy)), journal


def cmd_run(cfg: Config, args) -> int:
    if Path(cfg.kill_file).exists():
        print(f"Existuje {cfg.kill_file} (kill switch). Smaž ho příkazem: python -m burza resume")
        return 1
    agent, journal = _agent(cfg)
    st = journal.get_state(f"risk:{agent.broker.mode}")
    if st and st.get("halted"):
        print("Agent je zastavený celkovým limitem ztráty:", st.get("halt_reason"))
        print("Po kontrole a novém schválení: python -m burza reset-halt")
        return 1
    if args.once:
        agent.recover_pending()
        print(agent.step())
        return 0
    agent.run_forever()
    return 0


def cmd_kill(cfg: Config, args) -> int:
    Path(cfg.kill_file).write_text(utcnow().isoformat())
    print(f"Kill switch aktivní ({cfg.kill_file}). Běžící agent v příštím cyklu zavře své pozice a skončí.")
    if args.now:
        agent, _ = _agent(cfg)
        print(agent.step())
    return 0


def cmd_resume(cfg: Config, args) -> int:
    Path(cfg.kill_file).unlink(missing_ok=True)
    print("Kill switch zrušen.")
    return 0


def cmd_reset_halt(cfg: Config, args) -> int:
    journal = Journal(cfg.db_path)
    key = f"risk:{cfg.mode}"
    raw = journal.get_state(key)
    if not raw:
        print("Žádný uložený stav.")
        return 0
    st = RiskState.from_dict(raw)
    if args.new_capital:
        st.initial_capital = args.new_capital
    st.halted, st.halt_reason = False, ""
    journal.set_state(key, st.to_dict())
    journal.event("approval", f"Ručně schváleno pokračování, kapitál pro limity {st.initial_capital:.2f}",
                  level="WARNING")
    print("Zastavení zrušeno.")
    return 0


def cmd_status(cfg: Config, args) -> int:
    journal = Journal(cfg.db_path)
    hb = Path(cfg.data_dir) / "heartbeat"
    if hb.exists():
        age = (utcnow() - datetime.fromisoformat(hb.read_text())).total_seconds()
        print(f"Poslední cyklus agenta před {age:.0f} s" + ("  ⚠ agent možná neběží" if age > 300 else ""))
    else:
        print("Agent ještě neběžel.")
    print("Kill switch:", "AKTIVNÍ" if Path(cfg.kill_file).exists() else "ne")
    st = journal.get_state(f"risk:{cfg.mode}")
    if st:
        print(json.dumps(st, ensure_ascii=False, indent=2))
    for r in journal.open_trades(cfg.mode):
        print(f"  otevřeno {r['symbol']} {r['units']:.4f} ks @ {r['open_price']:.2f} "
              f"stop {r['stop']:.2f} cíl {r['target']:.2f}")
    return 0


def cmd_report(cfg: Config, args) -> int:
    journal = Journal(cfg.db_path)
    since = (utcnow() - timedelta(days=args.days)).isoformat(timespec="seconds")
    trades = journal.closed_trades(since)
    eq = journal.equity_series()
    st = journal.get_state(f"risk:{cfg.mode}") or {}
    initial = st.get("initial_capital")
    print(f"=== Report za posledních {args.days} dní ({cfg.mode}, {cfg.strategy}) ===")
    if eq:
        cur = eq[-1]["equity"]
        print(f"Kapitál: {cur:.2f} USD" + (f" (start {initial:.2f}, {((cur / initial) - 1) * 100:+.2f} %)"
                                           if initial else ""))
        peak, max_dd = eq[0]["equity"], 0.0
        for row in eq:
            peak = max(peak, row["equity"])
            max_dd = max(max_dd, (peak - row["equity"]) / peak)
        print(f"Max. drawdown: {max_dd * 100:.2f} %")
    pnls = [t["pnl"] for t in trades if t["pnl"] is not None]
    print(f"Uzavřených obchodů: {len(trades)}, P/L: {sum(pnls):+.2f} USD")
    if pnls:
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        print(f"Win rate: {len(wins) / len(pnls) * 100:.1f} %, profit factor: "
              f"{(sum(wins) / -sum(losses)) if losses and sum(losses) else float('inf'):.2f}")
        best = max(trades, key=lambda t: t["pnl"] or 0)
        worst = min(trades, key=lambda t: t["pnl"] or 0)
        print(f"Nejlepší: {best['symbol']} {best['pnl']:+.2f}, nejhorší: {worst['symbol']} {worst['pnl']:+.2f}")
        if any(t["pnl_source"] == "odhad" for t in trades):
            print("(P/L části obchodů je odhad z poslední ceny – přesná čísla jsou v historii eToro)")
    print(f"Otevřené pozice: {len(journal.open_trades(cfg.mode))}")
    print(f"Odmítnuté signály: {len(journal.rejected_signals(since))}")
    if st.get("halted"):
        print("STAV: ZASTAVENO –", st.get("halt_reason"))
    elif st.get("daily_blocked"):
        print("STAV: denní limit dosažen")
    return 0


def cmd_test_order(cfg: Config, args) -> int:
    """Akceptační test: otevře minimální pozici na demo účtu a hned ji zavře."""
    if cfg.mode != "demo":
        print("Testovací objednávka je povolena jen v režimu demo.")
        return 1
    if not market.is_open(utcnow()):
        print("Trh je zavřený – testovací objednávku spusť během obchodních hodin (15:30–22:00 SEČ).")
        return 1
    journal = Journal(cfg.db_path)
    client = _client(cfg)
    broker = EtoroBroker(client, demo=True, instruments=cfg.instruments)
    broker.prepare()
    sym = args.symbol.upper()
    iid = cfg.instruments[sym]
    price = client.live_candles(iid, "1m", 3)[-1].close
    ref = str(uuid.uuid4())
    journal.order_intent(ref, sym, iid, "open", 20.0, price * 0.95, price * 1.05)
    res = broker.open_long(symbol=sym, instrument_id=iid, amount_usd=20.0, stop=price * 0.95,
                           target=price * 1.05, client_ref=ref, price_hint=price)
    journal.order_update(ref, res.status, raw=res.raw)
    print("Otevření:", res.status, res.error or "", res.position_ids, res.fill_price)
    if not res.ok:
        return 1
    acct = broker.account()
    for p in acct.positions:
        if p.position_id in res.position_ids:
            out = broker.close(p, reason="test", price_hint=price)
            print("Zavření:", out.status, out.error or "")
    journal.event("test_order", f"Testovací objednávka {sym}: {res.status}", data={"ref": ref})
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="burza", description="AI obchodní agent pro eToro")
    p.add_argument("--config", default="config.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="ověří klíče, oprávnění, instrumenty a data")
    f = sub.add_parser("fetch", help="stáhne historická data pro backtest")
    f.add_argument("--days", type=int, default=60)
    b = sub.add_parser("backtest", help="backtest nad staženými daty")
    b.add_argument("--days", type=int, default=60)
    b.add_argument("--strategy")
    b.add_argument("--walk-forward", action="store_true", help="vývojová 70 % / testovací 30 % dnů")
    r = sub.add_parser("run", help="spustí agenta")
    r.add_argument("--once", action="store_true", help="jen jeden cyklus")
    k = sub.add_parser("kill", help="nouzové zastavení")
    k.add_argument("--now", action="store_true", help="hned zavřít pozice (i když agent neběží)")
    sub.add_parser("resume", help="zruší kill switch")
    h = sub.add_parser("reset-halt", help="ruční schválení po celkovém limitu")
    h.add_argument("--new-capital", type=float)
    sub.add_parser("status", help="stav agenta")
    rp = sub.add_parser("report", help="report (denní = --days 1, týdenní = --days 7)")
    rp.add_argument("--days", type=int, default=1)
    t = sub.add_parser("test-order", help="testovací demo objednávka (otevřít + zavřít)")
    t.add_argument("--symbol", default="SPY")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print("Chyba konfigurace:", exc)
        return 2
    handlers = {"check": cmd_check, "fetch": cmd_fetch, "backtest": cmd_backtest, "run": cmd_run,
                "kill": cmd_kill, "resume": cmd_resume, "reset-halt": cmd_reset_halt,
                "status": cmd_status, "report": cmd_report, "test-order": cmd_test_order}
    try:
        return handlers[args.cmd](cfg, args)
    except ConfigError as exc:
        print("Chyba:", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
