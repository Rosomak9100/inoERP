"""Studie B: technický kandidát + posouzení kontextu (zprávy, makro) přes Claude."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone

from . import market
from .ai import ClaudeJudge
from .context import ContextProvider, earnings_blackout
from .models import Candle, Position, Signal
from .strategy import Strategy, make_strategy, session_candles


class AiContextStrategy(Strategy):
    def __init__(self, base: Strategy, provider: ContextProvider, judge: ClaudeJudge,
                 min_confidence: float = 0.6, max_calls_per_day: int = 40):
        self.base = base
        self.provider = provider
        self.judge = judge
        self.min_confidence = min_confidence
        self.max_calls_per_day = max_calls_per_day
        self.name = f"kontext+{base.name}"
        self.journal = None
        self.clock = lambda: datetime.now(timezone.utc)

    def bind(self, journal, clock) -> None:
        self.journal = journal
        self.clock = clock

    def exit(self, position: Position, candles: list[Candle]) -> str | None:
        return self.base.exit(position, candles)

    def _reject(self, sig: Signal, reason: str) -> None:
        if self.journal:
            self.journal.signal(sig, self.name, False, f"filtr zpráv/makra: {reason}")

    def entry(self, symbol: str, candles: list[Candle]) -> Signal | None:
        sig = self.base.entry(symbol, candles)
        if sig is None:
            return None
        now = self.clock()
        ctx = self.provider.get(symbol, now)
        for n in ctx.all_news():
            if self.journal:
                self.journal.news_item(n)
        candidate = {k: v for k, v in asdict(sig).items() if k != "time"}
        news_ids = [n.id for n in ctx.all_news()]

        blackout = earnings_blackout(ctx, now)
        if blackout:
            if self.journal:
                self.journal.ai_decision(symbol, candidate, False, None, None, {"pravidlo": blackout},
                                         news_ids, ctx.macro, "pravidlo", None, None, None, None)
            self._reject(sig, blackout)
            return None

        # začátek obchodního dne (ET) převedený na UTC – deník ukládá časy v UTC
        since = datetime.combine(market.trading_date(now), datetime.min.time(), market.ET) \
            .astimezone(timezone.utc).isoformat(timespec="seconds")
        paid = getattr(self.judge, "costs_money", True)
        if paid and self.journal and self.journal.ai_calls_since(since) >= self.max_calls_per_day:
            self._reject(sig, "denní limit volání AI vyčerpán")
            return None

        sess = session_candles(candles)
        change = (sess[-1].close / sess[0].open - 1) * 100 if sess else None
        dec = self.judge.decide(sig, ctx, now, change)
        if self.journal:
            self.journal.ai_decision(symbol, candidate, dec.take and dec.error is None, dec.confidence,
                                     dec.size_factor, dec.data or None, news_ids, ctx.macro,
                                     self.judge.model, dec.request_id, dec.input_tokens,
                                     dec.output_tokens, dec.error)
        if dec.error:
            self._reject(sig, f"chyba posouzení ({dec.error}) – obchod se neotevírá")
            return None
        if not dec.take:
            self._reject(sig, dec.data.get("rationale", "AI: přeskočit"))
            return None
        if dec.confidence < self.min_confidence:
            self._reject(sig, f"nízká jistota {dec.confidence:.2f}")
            return None
        sig.size_factor = dec.size_factor
        sig.reason = (f"{sig.reason} | {self.judge.model}: {dec.data.get('context_bias')} "
                      f"({dec.confidence:.2f}, ×{dec.size_factor})")
        return sig


def make_study_strategy(cfg, client=None) -> Strategy:
    """Studie A = čistě technická strategie, Studie B = technika + AI kontext."""
    if cfg.study == "A":
        return make_strategy(cfg.strategy)
    from .context import FinnhubSource, FredSource

    moves = None
    if client is not None:
        watch = {"SPY": 3000, "QQQ": 3006, "GLD": 3025, "USO": 3007}

        def daily_moves():
            out = {}
            for sym, iid in watch.items():
                daily = client.live_candles(iid, "1d", 2)
                if len(daily) >= 2 and daily[-2].close:
                    out[sym] = round((daily[-1].close / daily[-2].close - 1) * 100, 2)
            return out

        moves = daily_moves

    provider = ContextProvider(
        FinnhubSource(cfg.finnhub_api_key) if cfg.finnhub_api_key else None,
        FredSource(cfg.fred_api_key) if cfg.fred_api_key else None,
        cfg.ai.news_lookback_hours, cfg.ai.context_cache_minutes, moves)
    if cfg.ai.provider == "api":  # placené Claude API – jen když si ho uživatel výslovně zapne
        judge = ClaudeJudge(cfg.ai.model, cfg.ai.effort)
    else:                        # výchozí: bezplatná pravidla
        from .rules import RuleJudge
        judge = RuleJudge()
    return AiContextStrategy(make_strategy(cfg.ai.base_strategy), provider, judge,
                             cfg.ai.min_confidence, cfg.ai.max_calls_per_day)
