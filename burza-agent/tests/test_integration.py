import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from burza.agent import Agent
from burza.backtest import run_backtest, walk_forward
from burza.broker import EtoroBroker, PaperBroker
from burza.models import Candle
from burza.etoro import EtoroClient
from burza.journal import Journal
from burza.strategy import VwapPullback

from helpers import config, multi_day, utc


class FakeResp:
    def __init__(self, status, body, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.content = json.dumps(body).encode()
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.responses.pop(0)


class ClientTests(unittest.TestCase):
    def test_headers_and_429_retry(self):
        s = FakeSession([FakeResp(429, {}, {"Retry-After": "0"}), FakeResp(200, {"ok": 1})])
        c = EtoroClient("app", "user", session=s)
        self.assertEqual(c.request("GET", "/api/v1/me"), {"ok": 1})
        headers = s.calls[0][2]["headers"]
        self.assertEqual(headers["x-api-key"], "app")
        self.assertEqual(headers["x-user-key"], "user")
        self.assertIn("x-request-id", headers)
        self.assertNotIn("Authorization", headers)

    def test_order_retry_keeps_request_id(self):
        s = FakeSession([FakeResp(503, {}), FakeResp(202, {"orderId": 1})])
        c = EtoroClient("a", "u", session=s)
        with mock.patch("time.sleep"):
            c.open_market_order(demo=True, instrument_id=3000, amount_usd=20, settlement_type="cfd",
                                stop_loss=1, take_profit=2, request_id="ref-1")
        ids = {call[2]["headers"]["x-request-id"] for call in s.calls}
        self.assertEqual(ids, {"ref-1"})
        self.assertIn("/api/v3/trading/execution/demo/orders", s.calls[0][1])

    def test_close_is_never_retried(self):
        s = FakeSession([FakeResp(503, {})])
        c = EtoroClient("a", "u", session=s)
        with self.assertRaises(Exception):
            c.close_position(demo=True, position_id="5", instrument_id=3000)
        self.assertEqual(len(s.calls), 1)

    def test_live_candles_parse(self):
        body = {"interval": "FiveMinutes", "candles": [{"instrumentId": 3000, "candles": [
            {"fromDate": "2026-10-23T19:55:00Z", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10},
            {"fromDate": "2026-10-23T19:50:00Z", "open": 1, "high": 2, "low": 0.5, "close": 1.2, "volume": 0}]}]}
        c = EtoroClient("a", "u", session=FakeSession([FakeResp(200, body)]))
        cs = c.live_candles(3000, "5m", 2)
        self.assertEqual([x.close for x in cs], [1.2, 1.5])

    def test_lookup_result_parsing(self):
        filled = {"status": {"id": 3}, "positionExecutions": [
            {"positionId": 9001, "openingData": {"units": 0.5, "avgPrice": 100.0}}]}
        r = EtoroBroker.result_from_lookup("ref", filled)
        self.assertTrue(r.ok)
        self.assertEqual(r.position_ids, ["9001"])
        rej = EtoroBroker.result_from_lookup("ref", {"status": {"id": 4, "errorCode": 7, "errorMessage": "x"}})
        self.assertFalse(rej.ok)

    def test_settlement_choice(self):
        elig = {"eligibilities": [
            {"instrumentId": 1001, "allowOpenPosition": True, "maxUnitsPerOrder": 100, "leverageConfigs": [
                {"settlementType": "real", "direction": "long", "leverageValues": [1]},
                {"settlementType": "cfd", "direction": "long", "leverageValues": [2, 5]}]},
            {"instrumentId": 3000, "allowOpenPosition": True, "leverageConfigs": [
                {"settlementType": "cfd", "direction": "long", "leverageValues": [1]}]}]}
        c = EtoroClient("a", "u", session=FakeSession([FakeResp(200, elig)]))
        b = EtoroBroker(c, demo=True, instruments={"AAPL": 1001, "SPY": 3000})
        self.assertEqual(b.prepare(), {})
        self.assertEqual(b.settlement, {1001: "real", 3000: "cfd"})


class FakeData:
    def __init__(self, candles):
        self.candles = candles

    def closed_candles(self, iid, now):
        return [c for c in self.candles if c.time.timestamp() + 300 <= now.timestamp()]


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.cfg = config(data_dir=str(d), kill_file=str(d / "STOP"))
        self.journal = Journal(self.cfg.db_path)
        days = [date(2026, 10, 19) + timedelta(days=i) for i in range(5)] + [date(2026, 10, 26)]
        self.data = FakeData(multi_day(days))
        self.broker = PaperBroker(10_000, self.cfg.costs)

    def tearDown(self):
        self.journal.close()
        self.tmp.cleanup()

    def agent(self, now):
        return Agent(self.cfg, self.journal, self.broker, self.data, VwapPullback(), clock=lambda: now)

    def test_market_closed(self):
        self.assertEqual(self.agent(utc(2026, 10, 25, 15, 0)).step(), "market_closed")

    def test_trading_day_runs_and_limits_hold(self):
        t = utc(2026, 10, 26, 14, 0)
        while t < utc(2026, 10, 26, 20, 0):
            self.agent(t).step()
            t = t + timedelta(minutes=5)
        rows = self.journal.db.execute("SELECT * FROM trades").fetchall()
        for r in rows:  # riziko obchodu nikdy nad 1 % kapitálu
            self.assertLessEqual(r["units"] * (r["open_price"] - r["stop"]), 100.0 * 1.01)
        self.assertEqual(self.journal.open_trades("paper"), [])  # na konci seance vše zavřeno
        self.assertGreater(len(self.journal.equity_series()), 0)

    def test_kill_switch_flattens(self):
        self.broker.open_long(symbol="SPY", instrument_id=3000, amount_usd=1_000, stop=1, target=1_000,
                              client_ref="x", price_hint=100)
        Path(self.cfg.kill_file).write_text("x")
        self.assertEqual(self.agent(utc(2026, 10, 26, 15, 0)).step(), "killed")
        self.assertEqual(self.broker.positions, {})


class BacktestTests(unittest.TestCase):
    def test_flatten_all_uses_each_symbols_price(self):
        """Regrese: pozice SPY za 19 USD se při limitu „prodala“ za 2354 USD (cena NVDA)."""
        from burza.backtest import flatten_all
        from burza.config import CostConfig
        b = PaperBroker(100_000, CostConfig.zero())
        b.open_long(symbol="SPY", instrument_id=1, amount_usd=1_000, stop=1, target=1e9, client_ref="a", price_hint=19.0)
        b.open_long(symbol="NVDA", instrument_id=2, amount_usd=1_000, stop=1, target=1e9, client_ref="b", price_hint=2354.0)
        t = utc(2026, 10, 26, 15, 0)
        b.on_bar("SPY", Candle(t, 19.0, 19.2, 18.9, 19.1))
        b.on_bar("NVDA", Candle(t, 2354, 2360, 2350, 2355))
        fills = {f.position.symbol: f for f in flatten_all(b, "limit", t)}
        self.assertAlmostEqual(fills["SPY"].price, 19.1)
        self.assertAlmostEqual(fills["NVDA"].price, 2355)
        self.assertLess(abs(fills["SPY"].pnl), 20)

    def test_exit_prices_belong_to_own_symbol(self):
        """Regrese: při limitu se pozice zavíraly cenou jiného symbolu (SPY 19 → 2354 USD)."""
        from burza.strategy import STRATEGIES
        cfg = config()
        cfg.risk.max_daily_loss_pct = 0.5  # ať se limit spouští často
        days = [date(2026, 9, 1) + timedelta(days=i) for i in range(40)]
        days = [d for d in days if d.weekday() < 5 and d != date(2026, 9, 7)]
        cheap = multi_day(days, drift=-0.0004)
        rich = [type(c)(c.time, c.open * 50, c.high * 50, c.low * 50, c.close * 50, c.volume)
                for c in multi_day(days, drift=0.0006)]
        for name, cls in STRATEGIES.items():
            res = run_backtest(cfg, cls(), {"CHEAP": cheap, "RICH": rich}, 10_000)
            for t in res.trades:
                self.assertLess(abs(t.exit / t.entry - 1), 0.2, f"{name}: {t}")
            self.assertLess(res.final, 20_000, name)

    def test_backtest_and_walk_forward(self):
        cfg = config()
        days = [date(2026, 9, 1) + timedelta(days=i) for i in range(40)]
        data = {"SPY": multi_day([d for d in days if d.weekday() < 5 and d != date(2026, 9, 7)])}
        res = run_backtest(cfg, VwapPullback(), data, 10_000)
        m = res.metrics()
        self.assertGreater(m["obchodu"], 0)
        for t in res.trades:
            self.assertLess(t.open_time, t.close_time)
        wf = walk_forward(cfg, VwapPullback(), data, 10_000)
        self.assertLess(wf["vyvoj"]["do"], wf["test"]["od"])


if __name__ == "__main__":
    unittest.main()
