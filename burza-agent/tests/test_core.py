import unittest
from datetime import date, timedelta

from burza import indicators as ind
from burza import market
from burza.broker import PaperBroker
from burza.config import CostConfig, RiskConfig
from burza.models import Account, Candle, Signal
from burza.risk import RiskEngine, RiskState
from burza.strategy import OpeningRangeBreakout, VwapPullback

from helpers import multi_day, session_candles, utc


class IndicatorTests(unittest.TestCase):
    def test_ema_constant(self):
        self.assertEqual(ind.ema([5.0] * 10, 3)[-1], 5.0)

    def test_rsi_extremes(self):
        up = [float(i) for i in range(30)]
        self.assertEqual(ind.rsi(up, 14)[-1], 100.0)
        self.assertIsNone(ind.rsi(up, 14)[13])

    def test_vwap_volume_weighted(self):
        t = utc(2026, 10, 26, 14, 0)
        cs = [Candle(t, 10, 10, 10, 10, 1), Candle(t, 20, 20, 20, 20, 3)]
        self.assertAlmostEqual(ind.vwap(cs)[-1], 17.5)

    def test_atr_constant_range(self):
        t = utc(2026, 10, 26, 14, 0)
        cs = [Candle(t, 10, 11, 9, 10) for _ in range(20)]
        self.assertAlmostEqual(ind.atr(cs, 14)[-1], 2.0)


class MarketTests(unittest.TestCase):
    def test_hours(self):
        self.assertTrue(market.is_open(utc(2026, 10, 26, 14, 0)))    # po 10:00 ET
        self.assertFalse(market.is_open(utc(2026, 10, 26, 13, 0)))   # 9:00 ET
        self.assertFalse(market.is_open(utc(2026, 10, 25, 15, 0)))   # neděle
        self.assertFalse(market.is_open(utc(2026, 11, 26, 16, 0)))   # Thanksgiving
        self.assertFalse(market.is_open(utc(2026, 11, 27, 18, 30)))  # zkrácený den po 13:00 ET

    def test_next_open(self):
        nxt = market.next_open(utc(2026, 10, 24, 12, 0))  # sobota
        self.assertEqual(market.to_et(nxt).date(), date(2026, 10, 26))


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.engine = RiskEngine(RiskConfig())
        self.st = RiskState(10_000, "2026-10-26", 10_000)
        self.acct = Account(10_000, 10_000, [])

    def sig(self, entry=100.0, stop=99.0, sym="SPY"):
        return Signal(sym, "long", entry, stop, entry + 2, "test")

    def test_size_is_one_percent_risk(self):
        d = self.engine.evaluate(self.sig(100, 95), self.st, self.acct, 0, set())
        self.assertTrue(d.accepted)
        self.assertAlmostEqual(d.risk_usd, 100.0)  # 1 % z 10 000
        self.assertAlmostEqual(d.units, 20.0)

    def test_notional_cap(self):
        d = self.engine.evaluate(self.sig(100, 99.9), self.st, self.acct, 0, set())
        self.assertTrue(d.accepted)
        self.assertLessEqual(d.amount_usd, 2_500.0 + 1e-6)  # 25 % equity
        self.assertLess(d.risk_usd, 100.0)

    def test_no_averaging_down(self):
        d = self.engine.evaluate(self.sig(), self.st, self.acct, 0, {"SPY"})
        self.assertFalse(d.accepted)

    def test_daily_limit_and_halt(self):
        self.assertEqual(self.engine.check_limits(self.st, 9_750), [])
        self.assertEqual(self.engine.check_limits(self.st, 9_690), ["daily"])
        self.assertFalse(self.engine.evaluate(self.sig(), self.st, self.acct, 0, set()).accepted)
        self.assertIn("halt", self.engine.check_limits(self.st, 8_400))
        self.engine.roll_day(self.st, date(2026, 10, 27), 8_400)
        self.assertFalse(self.st.daily_blocked)
        self.assertTrue(self.st.halted)  # celkový limit trvá do ručního schválení

    def test_open_risk_blocks_new_trade(self):
        d = self.engine.evaluate(self.sig(100, 95), self.st, self.acct, 250, set())
        self.assertFalse(d.accepted)


class PaperBrokerTests(unittest.TestCase):
    def test_round_trip_costs(self):
        b = PaperBroker(1_000, CostConfig(spread_bps=10, commission_usd=1, slippage_bps=0))
        res = b.open_long(symbol="SPY", instrument_id=1, amount_usd=500, stop=90, target=120,
                          client_ref="x", price_hint=100)
        pos = b.positions[res.position_ids[0]]
        out = b.close(pos, reason="t", price_hint=100)
        self.assertLess(out.raw["pnl"], 0)  # stejná cena → ztráta jen o náklady
        self.assertAlmostEqual(b.cash, 1_000 + out.raw["pnl"])

    def test_gap_through_stop_and_conservative_both(self):
        b = PaperBroker(1_000, CostConfig(0, 0, 0))
        b.open_long(symbol="SPY", instrument_id=1, amount_usd=100, stop=95, target=110,
                    client_ref="x", price_hint=100)
        t = utc(2026, 10, 26, 15, 0)
        fills = b.on_bar("SPY", Candle(t, 90, 91, 89, 90))  # gap pod stop → fill na open 90
        self.assertEqual(fills[0].price, 90)
        b.open_long(symbol="SPY", instrument_id=1, amount_usd=100, stop=95, target=110,
                    client_ref="y", price_hint=100)
        fills = b.on_bar("SPY", Candle(t, 100, 111, 94, 100))  # zásah obou → stop
        self.assertEqual(fills[0].reason, "stop-loss")

    def test_persistence(self):
        b = PaperBroker(1_000, CostConfig())
        b.open_long(symbol="SPY", instrument_id=1, amount_usd=100, stop=95, target=110,
                    client_ref="x", price_hint=100)
        b2 = PaperBroker.from_dict(b.to_dict(), CostConfig())
        self.assertAlmostEqual(b2.account().equity, b.account().equity)


class StrategyTests(unittest.TestCase):
    def test_no_lookahead(self):
        """Rozhodnutí v čase t nesmí záviset na svíčkách po t."""
        days = [date(2026, 10, 19) + timedelta(days=i) for i in range(5)]
        cs = multi_day(days)
        for strat in (VwapPullback(), OpeningRangeBreakout()):
            for n in range(60, len(cs), 7):
                a = strat.entry("SPY", cs[:n])
                altered = cs[:n] + [Candle(c.time, c.open * 2, c.high * 2, c.low * 2, c.close * 2)
                                    for c in cs[n:n + 20]]
                b = strat.entry("SPY", altered[:n])
                self.assertEqual(a, b)

    def test_signals_have_valid_stops(self):
        days = [date(2026, 10, 19) + timedelta(days=i) for i in range(5)]
        cs = multi_day(days)
        found = 0
        for n in range(60, len(cs)):
            s = VwapPullback().entry("SPY", cs[:n])
            if s:
                found += 1
                self.assertLess(s.stop, s.entry)
                self.assertGreater(s.target, s.entry)
        self.assertGreater(found, 0)

    def test_orb_needs_range(self):
        d = date(2026, 10, 26)
        cs = multi_day([date(2026, 10, 23)]) + session_candles(d)[:2]
        self.assertIsNone(OpeningRangeBreakout().entry("SPY", cs))


if __name__ == "__main__":
    unittest.main()
