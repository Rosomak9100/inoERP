import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from burza import market
from burza.agent import Agent
from burza.backtest_daily import buy_and_hold, run_daily_backtest
from burza.broker import PaperBroker
from burza.config import CostConfig
from burza.journal import Journal
from burza.models import Candle
from burza.strategy_daily import (DAILY_STRATEGIES, DonchianBreakout, TrendSMA, daily_from_intraday,
                                  normalize_daily)

from helpers import config, session_candles, utc


def etoro_daily(days: list[date], closes: list[float]) -> list[Candle]:
    """Denní svíčky jako z eToro: začátek 17:00 ET předchozího dne (21:00/22:00 UTC)."""
    out = []
    for d, c in zip(days, closes):
        start = datetime.combine(d - timedelta(days=1), datetime.min.time(), market.ET).replace(hour=17)
        out.append(Candle(start.astimezone(timezone.utc), c * 0.995, c * 1.01, c * 0.99, c, 1e6))
    return out


def trading_days(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if market.is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


class NormalizeTests(unittest.TestCase):
    def test_summer_winter_and_monday(self):
        raw = [Candle(utc(2026, 10, 22, 21, 0), 1, 1, 1, 759.82),   # = pátek 23. 10. (EDT)
               Candle(utc(2026, 3, 3, 22, 0), 1, 1, 1, 685.56),     # = středa 4. 3. (EST)
               Candle(utc(2026, 10, 18, 21, 0), 1, 1, 1, 766.9)]    # neděle 21:00Z = pondělí 19. 10.
        got = {market.to_et(c.time).date(): c.close for c in normalize_daily(raw)}
        self.assertEqual(got, {date(2026, 10, 23): 759.82, date(2026, 3, 4): 685.56, date(2026, 10, 19): 766.9})

    def test_today_from_intraday(self):
        bars = session_candles(date(2026, 10, 26))
        day = daily_from_intraday(bars)
        self.assertEqual(len(day), 1)
        self.assertEqual(day[0].open, bars[0].open)
        self.assertEqual(day[0].close, bars[-1].close)
        self.assertEqual(day[0].high, max(b.high for b in bars))


class DailyStrategyTests(unittest.TestCase):
    def test_trend_sma_entry_and_exit(self):
        days = trading_days(date(2025, 1, 2), 260)
        up = etoro_daily(days, [100 + i * 0.5 for i in range(260)])
        candles = normalize_daily(up)
        s = TrendSMA()
        self.assertIsNotNone(s.entry("SPY", candles))
        down = candles + normalize_daily(etoro_daily(trading_days(days[-1] + timedelta(days=1), 60),
                                                     [200 - i * 3 for i in range(60)]))
        self.assertIsNotNone(s.exit(None, down))

    def test_all_daily_strategies_no_lookahead(self):
        days = trading_days(date(2024, 1, 2), 400)
        import math
        candles = normalize_daily(etoro_daily(days, [100 * (1 + 0.1 * math.sin(i / 15)) + i * 0.1 for i in range(400)]))
        for name, cls in DAILY_STRATEGIES.items():
            s = cls()
            for n in range(260, 400, 5):
                a = s.entry("X", candles[:n])
                altered = candles[:n] + [Candle(c.time, c.open * 5, c.high * 5, c.low * 5, c.close * 5) for c in candles[n:]]
                self.assertEqual(a, s.entry("X", altered[:n]), name)
                if a:
                    self.assertLess(a.stop, a.entry)


class DailyBacktestTests(unittest.TestCase):
    def test_runs_and_closes_everything(self):
        days = trading_days(date(2023, 1, 3), 500)
        import math
        data = {s: normalize_daily(etoro_daily(days, [100 * (1 + 0.15 * math.sin(i / (20 + k))) + i * 0.05
                                                      for i in range(500)]))
                for k, s in enumerate(["SPY", "AAPL"])}
        cfg = config()
        for cls in DAILY_STRATEGIES.values():
            res = run_daily_backtest(cfg, cls(), data, 10_000)
            self.assertAlmostEqual(res.final, 10_000 + sum(t.pnl for t in res.trades), places=4)
            for t in res.trades:
                self.assertLessEqual(t.open_time, t.close_time)
        bh = buy_and_hold(data, sorted({market.to_et(c.time).date() for c in data["SPY"]}))
        self.assertIn("max_drawdown_pct", bh)

    def test_entry_day_low_does_not_trigger_stop(self):
        """Pozice otevřená na close dne se nesmí zastavit podle low téhož dne (to bylo před vstupem)."""
        days = trading_days(date(2025, 1, 2), 110)
        closes = [100.0] * 100 + [120.0] + [121.0] * 9
        candles = normalize_daily(etoro_daily(days, closes))
        crash_day = candles[100]
        candles[100] = Candle(crash_day.time, 100, 120.5, 50, 120.0, 1e6)  # hluboké low PŘED vstupem
        res = run_daily_backtest(config(), DonchianBreakout(), {"SPY": candles}, 10_000)
        self.assertTrue(res.trades)
        self.assertNotEqual(res.trades[0].reason, "stop-loss")


class FakeDailyData:
    def __init__(self, candles):
        self.candles = candles

    def daily_candles(self, iid, now):
        today = market.trading_date(now)
        return [c for c in self.candles if market.to_et(c.time).date() <= today]


class SwingAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.cfg = config(data_dir=str(d), kill_file=str(d / "STOP"), horizon="swing")
        self.cfg.costs = CostConfig.zero()
        self.journal = Journal(self.cfg.db_path)
        days = trading_days(date(2025, 9, 1), 300)
        self.days = days
        self.candles = normalize_daily(etoro_daily(days, [100 + i * 0.4 for i in range(300)]))
        self.broker = PaperBroker(10_000, self.cfg.costs)

    def tearDown(self):
        self.journal.close()
        self.tmp.cleanup()

    def step(self, now):
        return Agent(self.cfg, self.journal, self.broker, FakeDailyData(self.candles), TrendSMA(),
                     clock=lambda: now).step()

    def at(self, d, h, m):
        return datetime.combine(d, datetime.min.time(), market.ET).replace(hour=h, minute=m).astimezone(timezone.utc)

    def test_decides_once_near_close_and_holds_overnight(self):
        d1, d2 = self.days[-2], self.days[-1]
        self.assertEqual(self.step(self.at(d1, 11, 0)), "swing_wait")
        self.assertEqual(self.step(self.at(d1, 15, 46)), "swing_ok")
        self.assertEqual(len(self.broker.positions), 1)
        self.assertEqual(self.step(self.at(d1, 15, 50)), "swing_done")
        self.assertEqual(self.step(self.at(d1, 15, 55)), "swing_done")    # žádné zavírání před koncem dne
        self.assertEqual(len(self.broker.positions), 1)
        self.assertEqual(self.step(self.at(d2, 15, 46)), "swing_ok")
        self.assertEqual(len(self.broker.positions), 1)                    # drží přes noc


if __name__ == "__main__":
    unittest.main()


class SplitTests(unittest.TestCase):
    def test_detect_real_amzn_split_and_not_crash(self):
        from burza.splits import detect_split
        self.assertEqual(detect_split(2443.7986, 125.5969), 20.0)   # skutečná data eToro, AMZN 2022
        self.assertEqual(detect_split(100.0, 1000.0), 0.1)           # zpětný split 1:10
        self.assertIsNone(detect_split(100.0, 70.0))                 # propad o 30 % není split
        self.assertIsNone(detect_split(100.0, 30.0))                 # propad o 70 % neodpovídá poměru

    def test_adjust_makes_series_continuous(self):
        from burza.splits import adjust_splits
        days = trading_days(date(2022, 5, 2), 30)
        closes = [2400.0 + i for i in range(15)] + [(2415.0 + i) / 20 for i in range(15)]
        candles = normalize_daily(etoro_daily(days, closes))
        for a, b in zip(candles, candles[1:]):
            self.assertLess(abs(b.close / a.close - 1), 0.05)
        self.assertAlmostEqual(candles[-1].close, (2415.0 + 14) / 20)    # dnešní ceny beze změny
        self.assertEqual(adjust_splits(candles), candles)                # podruhé už nic

    def test_paper_position_split(self):
        b = PaperBroker(10_000, CostConfig.zero())
        res = b.open_long(symbol="NVDA", instrument_id=1, amount_usd=2_000, stop=900, target=2_000,
                          client_ref="x", price_hint=1_000)
        p = b.positions[res.position_ids[0]]
        value = p.units * 1_000
        b.apply_split("NVDA", 10)
        self.assertAlmostEqual(p.units * 100, value)
        self.assertAlmostEqual(p.stop, 90)
        fills = b.on_bar("NVDA", Candle(utc(2024, 6, 10, 13, 30), 100, 101, 95, 100))
        self.assertEqual(fills, [])                                       # žádný falešný stop-loss


class HistoryReportTests(unittest.TestCase):
    def test_strategy_vs_indices(self):
        import math
        from burza.history_report import WARMUP_DAYS, build_history, render_html
        days = trading_days(date(2022, 1, 3), 600)
        data = {s: normalize_daily(etoro_daily(days, [100 * (1 + 0.15 * math.sin(i / (15 + k))) + i * (0.05 + k / 100)
                                                      for i in range(600)]))
                for k, s in enumerate(["SPY", "QQQ", "AAPL"])}
        cfg = config(instruments={"SPY": 3000, "QQQ": 3006, "AAPL": 1001}, usdczk_start_rate=20.0)
        rep = build_history(cfg, data, "rsi2_pullback")
        names = list(rep["lines"])
        self.assertEqual(names, ["Strategie rsi2_pullback", "S&P 500 (SPY) – kup a drž", "Nasdaq 100 (QQQ) – kup a drž"])
        self.assertEqual(rep["od"], str(days[WARMUP_DAYS]))
        for n in names:   # všechny začínají stejnou částkou v Kč ve stejný den
            self.assertEqual(rep["lines"][n][0][0], rep["od"])
            self.assertAlmostEqual(rep["lines"][n][0][1], 200_000, delta=200)
        spy = data["SPY"]
        self.assertAlmostEqual(rep["summary"][names[1]]["vynos_pct"],
                               (spy[-1].close / spy[WARMUP_DAYS].close - 1) * 100, places=1)
        self.assertEqual(set(rep["others"]), set(DAILY_STRATEGIES) - {"rsi2_pullback"})
        html = render_html(rep)
        self.assertIn("lineChart", html)
        rep["strategie"] = "</script><b>"
        self.assertNotIn("</script><b>", render_html(rep))

    def test_too_short_history(self):
        from burza.history_report import build_history
        days = trading_days(date(2026, 1, 5), 100)
        data = {"SPY": normalize_daily(etoro_daily(days, [100 + i for i in range(100)]))}
        with self.assertRaises(ValueError):
            build_history(config(), data, "rsi2_pullback")
