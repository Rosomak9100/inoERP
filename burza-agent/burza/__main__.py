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
from .agent import Agent, DailyLiveData, LiveData, utcnow
from .backtest import load_csv, run_backtest, save_csv, walk_forward
from .broker import EtoroBroker, PaperBroker
from .config import Config, ConfigError, load_config
from .etoro import EtoroClient, EtoroError
from .journal import Journal
from .risk import RiskState
from .strategy import make_strategy
from .study_b import make_study_strategy


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
            raise ConfigError("Chybí počáteční kapitál: spusť python -m burza init-start --configs config_a.toml config_b.toml (nebo nastav initial_capital_usd)")
        return PaperBroker(cfg.initial_capital_usd, cfg.costs)
    broker = EtoroBroker(client, demo=cfg.mode == "demo", instruments=cfg.instruments)
    problems = broker.prepare()
    for sym, msg in problems.items():
        journal.event("eligibility", f"{sym}: {msg}", level="WARNING")
        print(f"VAROVÁNÍ {sym}: {msg}")
    return broker


def cmd_check(cfg: Config, args) -> int:
    """Srozumitelná kontrola krok za krokem – každý krok vypíše OK nebo co je špatně."""
    ok = True
    client = _client(cfg)
    print("1) Přihlášení k eToro …", end=" ")
    me = client.me()
    print("OK")
    scopes = me.get("scopes") or (me.get("profile") or {}).get("scopes") or []
    if scopes:
        if any(("real" in sc) and ("write" in sc) for sc in scopes) and cfg.mode != "real":
            print("   POZOR: tento klíč umí obchodovat se SKUTEČNÝMI penězi. Bezpečnější je klíč jen s demo právy.")

    print("2) Tržní data …")
    for sym, iid in cfg.instruments.items():
        try:
            candles = client.live_candles(iid, cfg.interval, 5)
        except EtoroError as exc:
            print(f"   {sym:6} CHYBA: {exc}")
            ok = False
            continue
        if candles:
            age = (utcnow() - candles[-1].time).total_seconds() / 3600
            print(f"   {sym:6} OK – poslední cena {candles[-1].close} (před {age:.1f} h)")
        else:
            print(f"   {sym:6} žádná data")
            ok = False

    if cfg.mode in ("demo", "real"):
        print(f"3) Účet eToro ({cfg.mode}) …")
        broker = EtoroBroker(client, demo=cfg.mode == "demo", instruments=cfg.instruments)
        try:
            for sym, msg in broker.prepare().items():
                print(f"   {sym}: {msg}")
            acct = broker.account()
            print(f"   OK – equity {acct.equity:.2f} USD, volné {acct.cash:.2f} USD, pozic {len(acct.positions)}")
        except EtoroError as exc:
            print(f"   CHYBA: {exc}")
            ok = False
    else:
        print("3) Režim paper – obchody se simulují, eToro účet se nepoužívá. OK")
    print("Trh je teď", "OTEVŘENÝ" if market.is_open(utcnow()) else "zavřený (obchoduje se Po–Pá 15:30–22:00 SEČ)")
    print("VŠE V POŘÁDKU" if ok else "NĚCO NENÍ V POŘÁDKU – viz řádky s CHYBA")
    return 0 if ok else 1


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
    name = args.strategy or (cfg.ai.base_strategy if cfg.study == "B" else cfg.strategy)
    if cfg.study == "B":
        print("Pozn.: Studie B se nedá poctivě backtestovat bez historického archivu zpráv (look-ahead),"
              " testuje se jen její technický základ:", name)
    strategy = make_strategy(name)
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


def cmd_compare(cfg: Config, args) -> int:
    """Porovná strategie: intradenní (5min, 1 rok) a/nebo swing (denní, několik let)."""
    rc = 0
    if args.horizon in ("intraday", "all"):
        rc = cmd_compare_intraday(cfg, args) or rc
    if args.horizon in ("swing", "all"):
        if args.horizon == "all":
            print()
            print("=" * 100)
            print()
        rc = cmd_compare_swing(cfg, args) or rc
    return rc


def cmd_compare_swing(cfg: Config, args) -> int:
    """Swing strategie na denních svíčkách za `years` let, walk-forward 70/30, srovnání s „kup a drž“."""
    import copy
    import time as _time
    from .backtest_daily import buy_and_hold, run_daily_backtest, walk_forward_daily
    from .config import CostConfig
    from .strategy_daily import DAILY_STRATEGIES, normalize_daily

    def path(sym):
        return Path(cfg.data_dir) / "history" / f"{sym}_1d.csv"

    stale = [s for s in cfg.instruments
             if not path(s).exists() or _time.time() - path(s).stat().st_mtime > 20 * 3600]
    if stale or args.refresh:
        print(f"Stahuji denní historii {args.years} let z eToro …")
        client = _client(cfg)
        end = utcnow()
        for sym, iid in cfg.instruments.items():
            candles = client.history_candles(iid, "1d", end - timedelta(days=365 * args.years), end)
            save_csv(path(sym), candles)
            print(f"{sym}: {len(candles)} dní")
    data = {s: normalize_daily(load_csv(path(s))) for s in cfg.instruments}
    data = {s: c for s, c in data.items() if len(c) > 260}
    swing_cfg = copy.copy(cfg)
    swing_cfg.risk = copy.copy(cfg.risk)
    swing_cfg.risk.flatten_on_daily_limit = False   # swing při denním limitu jen nenakupuje
    initial = cfg.initial_capital_usd or 10_000.0
    days = sorted({market.to_et(c.time).date() for cs in data.values() for c in cs})
    test_days = days[int(len(days) * 0.7):]
    gross_cfg = copy.copy(swing_cfg)
    gross_cfg.costs = CostConfig.zero()

    rows = []
    for name, cls in DAILY_STRATEGIES.items():
        print(f"Testuji {name} …")
        wf = walk_forward_daily(swing_cfg, cls(), data, initial)
        gross = run_daily_backtest(gross_cfg, cls(), data, initial, test_days).metrics()
        rows.append((name, wf, gross))
        with (Path(cfg.data_dir) / "backtests.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": utcnow().isoformat(), "horizont": "swing", "strategie": name,
                                "risk": vars(swing_cfg.risk), **wf, "test_bez_nakladu": gross},
                               ensure_ascii=False, default=str) + "\n")
    bh_dev = buy_and_hold(data, days[:int(len(days) * 0.7)])
    bh_test = buy_and_hold(data, test_days)
    print()
    print(f"SWING (držení dny až týdny), {len(data)} instrumentů, {days[0]} až {days[-1]}, skutečné náklady eToro.")
    print(f"Vývoj: {rows[0][1]['vyvoj']['od']} – {rows[0][1]['vyvoj']['do']}, "
          f"TEST: {rows[0][1]['test']['od']} – {rows[0][1]['test']['do']}")
    head = (f"{'strategie':18} {'část':6} {'výnos %':>8} {'obchodů':>8} {'win %':>6} {'PF':>5} "
            f"{'max DD %':>8} {'Sharpe':>7} {'t (dny)':>7} {'nákl. %':>8} {'bez nákl.%':>10}")
    print(head)
    print("-" * len(head))
    for name, wf, gross in rows:
        for part in ("vyvoj", "test"):
            m = wf[part]
            extra = f"{gross['vynos_pct']:>10.2f}" if part == "test" else f"{'':>10}"
            print(f"{name:18} {part:6} {m['vynos_pct']:>8.2f} {m['obchodu']:>8} {m['win_rate_pct']:>6} "
                  f"{str(m['profit_factor']):>5} {m['max_drawdown_pct']:>8.2f} {m['sharpe']:>7.2f} "
                  f"{str(m['t_stat_dni']):>7} {m['naklady_pct']:>8.2f} {extra}")
    for part, bh in (("vyvoj", bh_dev), ("test", bh_test)):
        print(f"{'kup a drž (vše)':18} {part:6} {bh['vynos_pct']:>8.2f} {'–':>8} {'–':>6} {'–':>5} "
              f"{bh['max_drawdown_pct']:>8.2f} {bh['sharpe']:>7.2f}   (rovnoměrně všechny instrumenty, bez poplatků)")
    print()
    print("Jak číst: strategie dává smysl, když má v TEST kladný výnos, PF > 1.2 a lepší poměr výnosu a rizika")
    print("než 'kup a drž' (vyšší Sharpe nebo výrazně menší max DD). 't (dny)' > 2 = výsledek nejspíš není náhoda.")
    print("Strategie drží max. 3 pozice najednou (limit rizika), 'kup a drž' drží všechno – výnosy proto nejsou")
    print("přímo srovnatelné, srovnávej hlavně Sharpe a max DD. Pozor: dnešní velké firmy (NVDA, META…)")
    print("v minulosti hodně rostly – výsledky z minulosti to nadhodnocuje (survivorship bias).")
    return 0


def cmd_compare_intraday(cfg: Config, args) -> int:
    """Porovná intradenní strategie walk-forward testem + diagnostika (bez nákladů, po instrumentech)."""
    import copy
    import time as _time
    from .backtest import group_by_day
    from .config import CostConfig
    from .strategy import STRATEGIES

    def path(sym):
        return Path(cfg.data_dir) / "history" / f"{sym}_{cfg.interval}.csv"

    stale = [s for s in cfg.instruments
             if not path(s).exists() or _time.time() - path(s).stat().st_mtime > 20 * 3600]
    if stale or args.refresh:
        print(f"Stahuji historii {args.days} dní z eToro (chvíli to trvá) …")
        cmd_fetch(cfg, args)
    from .splits import adjust_splits
    data = {s: adjust_splits(load_csv(path(s))) for s in cfg.instruments}
    initial = cfg.initial_capital_usd or 10_000.0
    days = sorted({d for c in data.values() for d in group_by_day(c)})
    test_days = days[int(len(days) * 0.7):]
    gross_cfg = copy.copy(cfg)
    gross_cfg.costs = CostConfig.zero()

    rows = []
    for name in STRATEGIES:
        print(f"Testuji {name} …")
        wf = walk_forward(cfg, make_strategy(name), data, initial)
        gross = run_backtest(gross_cfg, make_strategy(name), data, initial, test_days).metrics()
        per_sym = {sym: run_backtest(cfg, make_strategy(name), {sym: data[sym]}, initial, test_days).metrics()
                   for sym in data}
        rows.append((name, wf, gross, per_sym))
        with (Path(cfg.data_dir) / "backtests.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": utcnow().isoformat(), "strategie": name, "interval": cfg.interval,
                                "naklady": vars(cfg.costs), "risk": vars(cfg.risk), **wf,
                                "test_bez_nakladu": gross, "test_po_instrumentech": per_sym},
                               ensure_ascii=False) + "\n")
    print()
    fees = ", ".join(f"{s} {cfg.costs.fee(s):g}" for s in data)
    print(f"1) Výsledky se skutečnými náklady eToro (poplatek USD za nákup i prodej: {fees}; spread podle")
    print(f"   instrumentu, skluz {cfg.costs.slippage_bps} bps). Rozhoduje část TEST – data, na kterých se nic neladilo.")
    head = (f"{'strategie':18} {'část':6} {'výnos %':>8} {'obchodů':>8} {'win %':>6} {'PF':>5} "
            f"{'max DD %':>8} {'Sharpe':>7} {'t-stat':>7} {'bez nákl.%':>10}")
    print(head)
    print("-" * len(head))
    for name, wf, gross, _ in rows:
        for part in ("vyvoj", "test"):
            m = wf[part]
            extra = f"{gross['vynos_pct']:>10.2f}" if part == "test" else f"{'':>10}"
            print(f"{name:18} {part:6} {m['vynos_pct']:>8.2f} {m['obchodu']:>8} {m['win_rate_pct']:>6} "
                  f"{str(m['profit_factor']):>5} {m['max_drawdown_pct']:>8.2f} {m['sharpe']:>7.2f} "
                  f"{str(m['t_stat']):>7} {extra}")
    print()
    print("2) Kolik stojí poplatky (část TEST, se skutečnými náklady):")
    h2 = f"{'strategie':18} {'prům. pozice':>13} {'zisk/obchod před nákl.':>23} {'náklad/obchod':>14} {'náklady celkem':>15}"
    print(h2)
    print("-" * len(h2))
    for name, wf, _, _ in rows:
        m = wf["test"]
        print(f"{name:18} {m['prum_pozice_usd']:>9.0f} USD {m['prum_zisk_pred_naklady_usd']:>19.2f} USD "
              f"{m['prum_naklad_usd']:>10.2f} USD {m['naklady_usd']:>8.0f} USD ({m['naklady_pct']:.1f} %)")
    print("   Strategie vydělá jen tehdy, když 'zisk/obchod před nákl.' je výrazně větší než 'náklad/obchod'.")
    print()
    syms = list(data)
    print("3) Část TEST po instrumentech – výnos % (počet obchodů):")
    print(f"{'strategie':18} " + " ".join(f"{s:>13}" for s in syms))
    for name, _, _, per_sym in rows:
        cells = [f"{per_sym[s]['vynos_pct']:>+7.2f} ({per_sym[s]['obchodu']:>3})" for s in syms]
        print(f"{name:18} " + " ".join(f"{c:>13}" for c in cells))
    print()
    print("Jak číst: strategie je použitelná, jen když má v části TEST kladný výnos, PF > 1.2, aspoň 30 obchodů")
    print("a t-stat > 2. 'bez nákl.' ukazuje, zda strategie má vůbec nějakou výhodu, kterou pak sežerou náklady.")
    print("Minulost nezaručuje budoucnost.")
    return 0


def _agent(cfg: Config) -> tuple[Agent, Journal]:
    journal = Journal(cfg.db_path)
    client = _client(cfg)
    broker = _broker(cfg, journal, client)
    data = DailyLiveData(client) if cfg.horizon == "swing" else LiveData(client, cfg.interval, cfg.interval_minutes)
    return Agent(cfg, journal, broker, data, make_study_strategy(cfg, client)), journal


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


USDCZK_INSTRUMENT_ID = 82


def cmd_init_start(cfg: Config, args) -> int:
    """Zapíše do všech configů stejný kurz USD/CZK a datum startu (jen pokud ještě nejsou)."""
    import re
    client = _client(cfg)
    daily = client.live_candles(USDCZK_INSTRUMENT_ID, "1d", 2)
    if not daily:
        print("Kurz USD/CZK se nepodařilo načíst.")
        return 1
    rate = round(daily[-1].close, 4)
    today = market.trading_date(utcnow()).isoformat()
    for path in args.configs:
        text = Path(path).read_text(encoding="utf-8")
        if re.search(r"^usdczk_start_rate\s*=\s*[1-9]", text, re.M) and not args.force:
            print(f"{path}: kurz už je nastavený, nic neměním (--force přepíše)")
            continue
        text = re.sub(r"^usdczk_start_rate\s*=.*$", f"usdczk_start_rate = {rate}", text, flags=re.M)
        text = re.sub(r"^start_date\s*=.*$", f'start_date = "{today}"', text, flags=re.M)
        Path(path).write_text(text, encoding="utf-8")
        print(f"{path}: kurz USD/CZK {rate}, start {today}")
    return 0


def cmd_sync_config(cfg: Config, args) -> int:
    """Doplní do configů instrumenty, které přibyly ve vzorových configech (nic jiného nemění)."""
    import re
    import tomllib
    for path in args.configs:
        target = Path(path)
        example = target.with_name(target.stem + ".example.toml")
        if not target.exists() or not example.exists():
            continue
        text = target.read_text(encoding="utf-8-sig")
        have = {k.upper() for k in tomllib.loads(text).get("instruments", {})}
        want = tomllib.loads(example.read_text(encoding="utf-8")).get("instruments", {})
        missing = {k: v for k, v in want.items() if k.upper() not in have}
        if not missing:
            print(f"{path}: instrumenty aktuální")
            continue
        lines = "".join(f"{k} = {v}\n" for k, v in missing.items())
        text, n = re.subn(r"^\[instruments\][^\n]*\n", lambda m: m.group(0) + lines, text, count=1, flags=re.M)
        if n:
            target.write_text(text, encoding="utf-8")
            print(f"{path}: přidáno {', '.join(missing)}")
    return 0


def cmd_set_strategy(cfg: Config, args) -> int:
    """Přepne studie na zvolenou strategii a horizont (Studie B ji použije jako technický základ)."""
    import re
    from .strategy import make_strategy
    make_strategy(args.strategy)  # ověří, že strategie existuje
    for path in args.configs:
        target = Path(path)
        text = target.read_text(encoding="utf-8-sig")

        def put(key, value, text):
            line = f"{key} = {value}"
            if re.search(rf"^{key}\s*=", text, re.M):
                return re.sub(rf"^{key}\s*=.*$", line, text, count=1, flags=re.M)
            return re.sub(r"^(mode\s*=.*)$", rf"\1\n{line}", text, count=1, flags=re.M)

        text = put("horizon", f'"{args.horizon}"', text)
        text = put("strategy", f'"{args.strategy}"', text)
        text = re.sub(r"^base_strategy\s*=.*$", f'base_strategy = "{args.strategy}"', text, flags=re.M)
        flatten = "false" if args.horizon == "swing" else "true"
        text = re.sub(r"^flatten_on_daily_limit\s*=.*$", f"flatten_on_daily_limit = {flatten}", text, flags=re.M)
        target.write_text(text, encoding="utf-8")
        print(f"{path}: horizon = {args.horizon}, strategie = {args.strategy}")
    print("Hotovo. Zastav agenty (Burza - ZASTAVIT) a spusť je znovu (Burza - SPUSTIT).")
    return 0


def cmd_dashboard(cfg: Config, args) -> int:
    from .dashboard import serve
    cfgs = [load_config(path, validate=False) for path in args.studies] if args.studies else [cfg]
    serve(cfgs, args.host, args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="burza", description="AI obchodní agent pro eToro")
    p.add_argument("--config", default="config_a.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="ověří klíče, oprávnění, instrumenty a data")
    f = sub.add_parser("fetch", help="stáhne historická data pro backtest")
    f.add_argument("--days", type=int, default=60)
    b = sub.add_parser("backtest", help="backtest nad staženými daty")
    b.add_argument("--days", type=int, default=60)
    b.add_argument("--strategy")
    b.add_argument("--walk-forward", action="store_true", help="vývojová 70 % / testovací 30 % dnů")
    c = sub.add_parser("compare", help="porovná všechny strategie (walk-forward)")
    c.add_argument("--days", type=int, default=365, help="intradenní: kolik dní 5min dat")
    c.add_argument("--years", type=int, default=8, help="swing: kolik let denních dat")
    c.add_argument("--horizon", choices=["intraday", "swing", "all"], default="all")
    c.add_argument("--refresh", action="store_true", help="znovu stáhnout historii")
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
    i = sub.add_parser("init-start", help="společný kurz USD/CZK a datum startu do configů")
    i.add_argument("--configs", nargs="+", required=True)
    i.add_argument("--force", action="store_true")
    sc = sub.add_parser("sync-config", help="doplní nové instrumenty ze vzorových configů")
    sc.add_argument("--configs", nargs="+", required=True)
    ss = sub.add_parser("set-strategy", help="přepne studie na strategii a horizont")
    ss.add_argument("--configs", nargs="+", required=True)
    ss.add_argument("--horizon", choices=["intraday", "swing"], required=True)
    ss.add_argument("--strategy", required=True)
    d = sub.add_parser("dashboard", help="webový dashboard obou studií")
    d.add_argument("--studies", nargs="+", help="configy studií, např. config_a.toml config_b.toml")
    d.add_argument("--host", default="127.0.0.1")
    d.add_argument("--port", type=int, default=8080)
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = load_config(args.config, validate=args.cmd not in ("dashboard", "init-start", "sync-config", "set-strategy"))
    except ConfigError as exc:
        print("Chyba konfigurace:", exc)
        return 2
    handlers = {"check": cmd_check, "fetch": cmd_fetch, "backtest": cmd_backtest, "run": cmd_run,
                "kill": cmd_kill, "resume": cmd_resume, "reset-halt": cmd_reset_halt,
                "status": cmd_status, "report": cmd_report, "test-order": cmd_test_order,
                "dashboard": cmd_dashboard, "init-start": cmd_init_start, "compare": cmd_compare,
                "sync-config": cmd_sync_config, "set-strategy": cmd_set_strategy}
    try:
        return handlers[args.cmd](cfg, args)
    except ConfigError as exc:
        print("Chyba:", exc)
        return 2
    except EtoroError as exc:
        print(friendly_etoro_error(exc))
        return 3


def friendly_etoro_error(exc: EtoroError) -> str:
    if exc.status in (401, 403):
        return ("CHYBA: eToro klíče nefungují (odmítnuté přihlášení). Zkontroluj v souboru .env, že "
                "ETORO_API_KEY a ETORO_USER_KEY jsou správně a celé, bez mezer a uvozovek.")
    if exc.status == 0:
        return "CHYBA: nepodařilo se spojit s eToro – zkontroluj připojení k internetu."
    if exc.status == 429:
        return "CHYBA: eToro dočasně omezilo počet dotazů, zkus to za minutu znovu."
    return f"CHYBA eToro: {exc}"


if __name__ == "__main__":
    sys.exit(main())
