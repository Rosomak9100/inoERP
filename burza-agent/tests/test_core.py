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
        b = PaperBroker(1_000, CostConfig.zero())
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


class NoiseMomentumTests(unittest.TestCase):
    def days(self, n):
        out, d = [], date(2026, 9, 1)
        while len(out) < n:
            if market.is_trading_day(d):
                out.append(d)
            d += timedelta(days=1)
        return out

    def test_bands_from_same_time_of_day(self):
        from burza.strategy import NoiseAreaMomentum
        cs = multi_day(self.days(8))
        b = NoiseAreaMomentum(lookback_days=5).bands(cs)
        self.assertIsNotNone(b)
        upper, lower, today = b
        self.assertGreater(upper, today[0].open)
        self.assertLess(lower, today[0].open)

    def test_needs_enough_days(self):
        from burza.strategy import NoiseAreaMomentum
        self.assertIsNone(NoiseAreaMomentum().bands(multi_day(self.days(3))))

    def test_no_lookahead_and_valid_stops(self):
        from burza.strategy import NoiseAreaMomentum
        cs = multi_day(self.days(12), drift=0.0012)
        strat = NoiseAreaMomentum()
        found = 0
        for n in range(500, len(cs)):
            a = strat.entry("SPY", cs[:n])
            altered = cs[:n] + [Candle(c.time, c.open * 3, c.high * 3, c.low * 3, c.close * 3) for c in cs[n:n + 10]]
            self.assertEqual(a, strat.entry("SPY", altered[:n]))
            if a:
                found += 1
                self.assertLess(a.stop, a.entry)
                # vstup jen v kontrolních časech (každých 30 min od otevření)
                end = market.to_et(a.time + timedelta(minutes=5))
                self.assertEqual((end.hour * 60 + end.minute - 570) % 30, 0)
        self.assertGreater(found, 0)


class LastHalfHourTests(unittest.TestCase):
    def test_entry_only_at_1530_after_positive_morning(self):
        from burza.strategy import LastHalfHourMomentum
        days = [date(2026, 10, 19) + timedelta(days=i) for i in range(5)]
        cs = multi_day(days, drift=0.001, wave=0.0005)
        strat = LastHalfHourMomentum()
        hits = []
        for n in range(40, len(cs) + 1):
            sig = strat.entry("SPY", cs[:n])
            if sig:
                end = market.to_et(sig.time + timedelta(minutes=5))
                hits.append((end.hour, end.minute))
                self.assertLess(sig.stop, sig.entry)
        self.assertTrue(hits)
        self.assertTrue(all(h == (15, 30) for h in hits))

    def test_no_entry_after_negative_morning(self):
        from burza.strategy import LastHalfHourMomentum
        days = [date(2026, 10, 19) + timedelta(days=i) for i in range(5)]
        cs = multi_day(days, drift=-0.001, wave=0.0005)
        self.assertFalse(any(LastHalfHourMomentum().entry("SPY", cs[:n]) for n in range(40, len(cs) + 1)))

    def test_in_session_filter(self):
        self.assertTrue(market.in_session(utc(2026, 10, 26, 14, 0)))
        self.assertFalse(market.in_session(utc(2026, 10, 26, 12, 0)))   # pre-market
        self.assertFalse(market.in_session(utc(2026, 10, 26, 21, 0)))   # after-hours


class EtoroCostTests(unittest.TestCase):
    def test_real_fees_per_symbol(self):
        b = PaperBroker(10_000, CostConfig())
        for sym, fee in (("SPY", 3.0), ("AAPL", 1.0)):
            res = b.open_long(symbol=sym, instrument_id=1, amount_usd=2_000, stop=1, target=1e9,
                              client_ref=sym, price_hint=100)
            out = b.close(b.positions[res.position_ids[0]], reason="t", price_hint=100)
            # stejná cena tam i zpět → ztráta = 2× poplatek + spread + skluz
            self.assertLess(out.raw["pnl"], -2 * fee)
            self.assertGreater(out.raw["pnl"], -2 * fee - 2)
            self.assertAlmostEqual(out.raw["costs"], -out.raw["pnl"], places=3)

    def test_position_smaller_than_fees_rejected(self):
        b = PaperBroker(10_000, CostConfig())
        res = b.open_long(symbol="SPY", instrument_id=1, amount_usd=5, stop=1, target=1e9, client_ref="x", price_hint=100)
        self.assertFalse(res.ok)
