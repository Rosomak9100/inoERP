import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from burza.agent import Agent
from burza.ai import AiDecision, build_prompt
from burza.broker import Broker, ExactClose
from burza.context import MarketContext, NewsItem, earnings_blackout, upcoming_events
from burza.dashboard import build_payload, comparability
from burza.journal import Journal
from burza.models import Account, Candle, OrderResult, Signal
from burza.rules import RuleJudge
from burza.study_b import AiContextStrategy, make_study_strategy
from burza.strategy import Strategy

from helpers import config, utc


class FixedStrategy(Strategy):
    name = "fixed"

    def entry(self, symbol, candles):
        return Signal(symbol, "long", 100.0, 98.0, 104.0, "test", candles[-1].time if candles else None)


class FakeProvider:
    def __init__(self, ctx):
        self.ctx = ctx

    def get(self, symbol, now):
        return self.ctx


class FakeJudge:
    model = "fake"

    def __init__(self, decision):
        self.decision = decision
        self.prompts = []

    def decide(self, sig, ctx, now, change):
        self.prompts.append(build_prompt(sig, ctx, now, change))
        return self.decision


def ctx_with(**kw):
    now = utc(2026, 10, 27, 15, 0)
    ctx = MarketContext("AAPL", now, **kw)
    return ctx, now


class StudyBTests(unittest.TestCase):
    def setUp(self):
        self.journal = Journal(":memory:")
        self.candles = [Candle(utc(2026, 10, 27, 13, 30) + timedelta(minutes=5 * i), 100, 101, 99, 100.5)
                        for i in range(10)]

    def strat(self, decision, ctx=None, now=None):
        ctx = ctx or MarketContext("AAPL", utc(2026, 10, 27, 15, 0))
        s = AiContextStrategy(FixedStrategy(), FakeProvider(ctx), FakeJudge(decision), 0.6, 40)
        s.bind(self.journal, lambda: now or utc(2026, 10, 27, 15, 0))
        return s

    def test_take_reduces_size(self):
        d = AiDecision(True, 0.8, 0.5, {"context_bias": "neutralni", "rationale": "ok"})
        sig = self.strat(d).entry("AAPL", self.candles)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.size_factor, 0.5)
        row = self.journal.db.execute("SELECT * FROM ai_decisions").fetchone()
        self.assertEqual(row["take"], 1)

    def test_skip_and_low_confidence_are_logged(self):
        self.assertIsNone(self.strat(AiDecision(False, 0.9, 1.0, {"rationale": "proti"})).entry("AAPL", self.candles))
        self.assertIsNone(self.strat(AiDecision(True, 0.3, 1.0, {"rationale": "x"})).entry("AAPL", self.candles))
        rejected = self.journal.db.execute("SELECT decision FROM signals WHERE accepted=0").fetchall()
        self.assertEqual(len(rejected), 2)

    def test_api_error_fails_closed(self):
        sig = self.strat(AiDecision(False, error="API 500")).entry("AAPL", self.candles)
        self.assertIsNone(sig)

    def test_earnings_blackout_skips_ai_call(self):
        ctx, now = ctx_with(earnings=[{"date": "2026-10-27", "hour": "amc"}])
        judge = FakeJudge(AiDecision(True, 1.0, 1.0, {}))
        s = AiContextStrategy(FixedStrategy(), FakeProvider(ctx), judge)
        s.bind(self.journal, lambda: now)
        self.assertIsNone(s.entry("AAPL", self.candles))
        self.assertEqual(judge.prompts, [])

    def test_blackout_rules(self):
        ctx, now = ctx_with(earnings=[{"date": "2026-10-28", "hour": "bmo"}])
        self.assertIsNotNone(earnings_blackout(ctx, now))
        ctx, now = ctx_with(earnings=[{"date": "2026-10-28", "hour": "amc"}])
        self.assertIsNone(earnings_blackout(ctx, now))

    def test_fomc_event_listed(self):
        self.assertTrue(any("DNES" in e for e in upcoming_events(utc(2026, 10, 28, 15, 0))))

    def test_prompt_contains_sources_and_times(self):
        news = NewsItem("finnhub", "Reuters", "Apple beats estimates", "EPS 1.9 vs 1.8", "http://x",
                        utc(2026, 10, 27, 12, 0), "AAPL")
        ctx, now = ctx_with(news=[news], macro={"CPIAUCSL": {"popis": "CPI", "hodnota": 2.9,
                                                             "predchozi": 3.0, "datum_pozorovani": "2026-09-01"}})
        p = build_prompt(Signal("AAPL", "long", 100, 98, 104, "VWAP"), ctx, now, 1.2)
        self.assertIn("Reuters", p)
        self.assertIn("před 3 h", p)
        self.assertIn("CPI", p)

    def test_daily_ai_call_limit(self):
        s = self.strat(AiDecision(True, 0.9, 1.0, {}), now=datetime.now(timezone.utc))
        s.max_calls_per_day = 1
        self.assertIsNotNone(s.entry("AAPL", self.candles))
        self.assertIsNone(s.entry("AAPL", self.candles))


class RuleJudgeTests(unittest.TestCase):
    now = utc(2026, 10, 27, 15, 0)
    sig = Signal("AAPL", "long", 100, 98, 104, "VWAP")

    def news(self, headline, hours_ago=1):
        return NewsItem("finnhub", "Reuters", headline, "", "http://x/" + headline,
                        self.now - timedelta(hours=hours_ago), "AAPL")

    def test_negative_news_skips(self):
        ctx = MarketContext("AAPL", self.now, news=[self.news("Apple misses estimates, cuts guidance"),
                                                    self.news("Apple faces antitrust probe")])
        d = RuleJudge().decide(self.sig, ctx, self.now, 0.5)
        self.assertFalse(d.take)
        self.assertTrue(d.data["key_items"])

    def test_positive_news_full_size(self):
        ctx = MarketContext("AAPL", self.now, news=[self.news("Apple beats estimates on record revenue")],
                            macro={"VIXCLS": {"hodnota": 15}})
        d = RuleJudge().decide(self.sig, ctx, self.now, 0.8)
        self.assertTrue(d.take)
        self.assertEqual(d.size_factor, 1.0)

    def test_priced_in_halves_size(self):
        ctx = MarketContext("AAPL", self.now, news=[self.news("Apple beats estimates")],
                            macro={"VIXCLS": {"hodnota": 15}})
        d = RuleJudge().decide(self.sig, ctx, self.now, 4.5)
        self.assertTrue(d.take)
        self.assertEqual(d.size_factor, 0.5)

    def test_market_selloff_and_vix(self):
        ctx = MarketContext("AAPL", self.now, market_moves={"SPY": -2.0})
        self.assertFalse(RuleJudge().decide(self.sig, ctx, self.now, 0).take)
        ctx = MarketContext("AAPL", self.now, macro={"VIXCLS": {"hodnota": 40}})
        self.assertFalse(RuleJudge().decide(self.sig, ctx, self.now, 0).take)

    def test_fomc_day_halves(self):
        ctx = MarketContext("AAPL", self.now, events=["Rozhodnutí FOMC – DNES"],
                            macro={"VIXCLS": {"hodnota": 15}})
        self.assertEqual(RuleJudge().decide(self.sig, ctx, self.now, 0).size_factor, 0.5)

    def test_free_by_default(self):
        cfg = config(study="B", fred_api_key="x")
        self.assertIsInstance(make_study_strategy(cfg).judge, RuleJudge)
        cfg.ai.provider = "api"
        self.assertNotIsInstance(make_study_strategy(cfg).judge, RuleJudge)

    def test_free_judge_ignores_call_limit(self):
        j = Journal(":memory:")
        s = AiContextStrategy(FixedStrategy(), FakeProvider(MarketContext("AAPL", self.now)), RuleJudge(),
                              max_calls_per_day=0)
        s.bind(j, lambda: self.now)
        candles = [Candle(self.now, 100, 101, 99, 100.5)]
        self.assertIsNotNone(s.entry("AAPL", candles))


class FakeEtoro(Broker):
    mode = "demo"

    def __init__(self):
        self.positions = []
        self.history = {}

    def account(self):
        return Account(10_000, 10_000, list(self.positions))

    def closed_history(self, min_date):
        return self.history

    def open_long(self, **kw):
        return OrderResult(False, kw["client_ref"], "rejected")

    def close(self, position, *, reason, price_hint):
        return OrderResult(True, "", "submitted", [position.position_id])


class EmptyData:
    def closed_candles(self, iid, now):
        return []


class ExactPnlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.cfg = config(mode="demo", data_dir=str(d), kill_file=str(d / "STOP"))
        self.journal = Journal(self.cfg.db_path)
        self.broker = FakeEtoro()
        self.journal.trade_open("777", "ref", "SPY", 3000, "demo", 100.0, 2.0, 98.0, 104.0,
                                "2026-10-26T14:00:00+00:00")

    def tearDown(self):
        self.journal.close()
        self.tmp.cleanup()

    def step(self, now):
        Agent(self.cfg, self.journal, self.broker, EmptyData(), FixedStrategy(), clock=lambda: now).step()

    def test_exact_pnl_from_history(self):
        self.broker.history = {"777": ExactClose("777", 103.5, 6.8, "2026-10-26T15:00:00Z", 0.2)}
        self.step(utc(2026, 10, 26, 15, 5))
        row = self.journal.db.execute("SELECT * FROM trades WHERE position_id='777'").fetchone()
        self.assertEqual(row["pnl_source"], "eToro")
        self.assertAlmostEqual(row["pnl"], 6.8)
        self.assertAlmostEqual(row["costs"], 0.2)

    def test_estimate_after_timeout_then_reconciled(self):
        self.journal.set_state("last_prices", {"SPY": 103.0})
        self.step(utc(2026, 10, 26, 15, 0))
        self.assertEqual(len(self.journal.open_trades("demo")), 1)  # čeká na historii
        self.step(utc(2026, 10, 26, 15, 20))
        row = self.journal.db.execute("SELECT * FROM trades WHERE position_id='777'").fetchone()
        self.assertEqual(row["pnl_source"], "odhad")
        self.broker.history = {"777": ExactClose("777", 102.9, 5.6, "2026-10-26T15:01:00Z", 0.1)}
        self.step(utc(2026, 10, 26, 16, 30))  # hodinový reconcile
        row = self.journal.db.execute("SELECT * FROM trades WHERE position_id='777'").fetchone()
        self.assertEqual(row["pnl_source"], "eToro")
        self.assertAlmostEqual(row["pnl"], 5.6)


class DashboardTests(unittest.TestCase):
    def test_payload_and_comparability(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = config(data_dir=str(Path(tmp) / "a"), study="A", initial_capital_czk=200_000, usdczk_start_rate=23.0)
            b = config(data_dir=str(Path(tmp) / "b"), study="B", initial_capital_czk=200_000, usdczk_start_rate=22.5)
            j = Journal(a.db_path)
            j.equity_point(10_000, 10_000, 0, 700.0)
            j.equity_point(10_100, 9_000, 20, 707.0)
            j.trade_open("P1", "r", "SPY", 3000, "paper", 100, 1, 99, 102)
            j.trade_close("P1", 101, 1.0, "simulace", "take-profit", costs=0.05)
            j.close()
            payload = build_payload([a, b])
            json.dumps(payload, default=str)
            sa, sb = payload["studies"]
            self.assertAlmostEqual(sa["metrics"]["return_pct"], 1.0)
            self.assertAlmostEqual(sa["series"][-1]["bench_pct"], 1.0)
            self.assertEqual(sa["metrics"]["costs"], 0.05)
            self.assertIn("error", sb)
            self.assertTrue(any("kurz" in w for w in comparability([a, b])))


if __name__ == "__main__":
    unittest.main()
