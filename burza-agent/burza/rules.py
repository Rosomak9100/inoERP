"""Bezplatné posouzení kontextu pro Studii B – pevná pravidla, žádné placené API.

Pracuje se stejným kontextem jako AI (zprávy, výsledky, makro, trh, události) a vrací stejný
typ rozhodnutí, takže evidence i dashboard fungují stejně. Pravidla jsou záměrně jednoduchá
a průhledná – každé rozhodnutí obsahuje seznam faktorů, které k němu vedly.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from .ai import AiDecision
from .context import MarketContext, NewsItem
from .models import Signal

POSITIVE = [
    "beat", "beats", "tops estimates", "above estimates", "raises guidance", "raised guidance",
    "raises outlook", "upgrade", "upgraded", "outperform", "record revenue", "record profit",
    "strong demand", "surge", "soars", "jumps", "rallies", "buyback", "repurchase", "dividend hike",
    "approval", "approved", "wins contract", "partnership", "expands", "better-than-expected",
]
NEGATIVE = [
    "miss", "misses", "below estimates", "cuts guidance", "cut guidance", "lowers guidance",
    "lowers outlook", "warns", "warning", "downgrade", "downgraded", "underperform", "lawsuit",
    "sued", "probe", "investigation", "subpoena", "recall", "plunge", "plunges", "tumbles", "sinks",
    "slumps", "layoffs", "job cuts", "fraud", "bankruptcy", "default", "delay", "delayed", "halt",
    "tariff", "sanction", "ban", "antitrust", "worse-than-expected", "weak demand", "sell-off",
]
MARKET_NEGATIVE = ["recession", "sell-off", "crash", "war", "invasion", "attack", "default",
                   "shutdown", "hawkish", "rate hike", "inflation surge", "bank failure"]

FRESH = timedelta(hours=3)   # starší zprávy mají poloviční váhu (pravděpodobně už v ceně)


def _hits(text: str, words: list[str]) -> list[str]:
    t = text.lower()
    return [w for w in words if re.search(r"\b" + re.escape(w) + r"\b", t)]


def score_news(items: list[NewsItem], now: datetime, words_pos=POSITIVE, words_neg=NEGATIVE):
    """Vrátí (skóre, seznam položek s dopadem). Zpráva bez klíčových slov je neutrální."""
    total = 0.0
    used = []
    for n in items:
        text = f"{n.headline}. {n.summary}"
        pos, neg = _hits(text, words_pos), _hits(text, words_neg)
        if not pos and not neg:
            continue
        s = (1 if pos else 0) - (1 if neg else 0)
        if s == 0:
            continue
        weight = 1.0 if n.published_at and now - n.published_at <= FRESH else 0.5
        total += s * weight
        used.append({"source": n.publisher or n.source,
                     "published_at": n.published_at.strftime("%Y-%m-%d %H:%M UTC") if n.published_at else "?",
                     "headline": n.headline[:200], "impact": "pozitivni" if s > 0 else "negativni",
                     "slova": ", ".join(pos + neg)})
    return total, used


class RuleJudge:
    """Bezplatná alternativa k ClaudeJudge se stejným rozhraním decide()."""

    model = "pravidla"
    costs_money = False

    def decide(self, sig: Signal, ctx: MarketContext, now: datetime,
               intraday_change_pct: float | None) -> AiDecision:
        factors: list[str] = []
        size = 1.0
        event_risk = "nizke"

        stock_score, stock_items = score_news(ctx.news, now)
        mkt_score, mkt_items = score_news(ctx.market_news, now, [], MARKET_NEGATIVE)

        # 1) zprávy k firmě
        if stock_score <= -1.5:
            return self._skip(f"převažují negativní zprávy k {sig.symbol} (skóre {stock_score:+.1f})",
                              stock_items, "proti", event_risk)
        if stock_score >= 1:
            factors.append(f"pozitivní zprávy k {sig.symbol} (skóre {stock_score:+.1f})")
        elif not ctx.news:
            factors.append("žádné aktuální zprávy k firmě – rozhoduje technika")
        else:
            factors.append(f"zprávy k {sig.symbol} bez jasného směru (skóre {stock_score:+.1f})")

        # 2) už v ceně: silný pohyb dnes + staré pozitivní zprávy
        priced_in = "nelze určit"
        if intraday_change_pct is not None:
            if intraday_change_pct > 3 and stock_score > 0:
                size = min(size, 0.5)
                priced_in = f"cena dnes už +{intraday_change_pct:.1f} %, pozitivní zprávy jsou pravděpodobně v ceně"
                factors.append("pozitivní zprávy už nejspíš v ceně → poloviční velikost")
            else:
                priced_in = f"dnešní pohyb {intraday_change_pct:+.1f} %"

        # 3) celkový trh
        spy = ctx.market_moves.get("SPY")
        if spy is not None and spy <= -1.5:
            return self._skip(f"trh dnes výrazně klesá (SPY {spy:+.1f} %) – long proti trhu se nebere",
                              stock_items, "proti", "stredni")
        if mkt_score <= -2:
            size = min(size, 0.5)
            event_risk = "stredni"
            factors.append("negativní tón obecných tržních zpráv → poloviční velikost")

        # 4) makro a volatilita
        vix = (ctx.macro.get("VIXCLS") or {}).get("hodnota")
        if vix is not None:
            if vix >= 35:
                return self._skip(f"extrémní volatilita (VIX {vix:.0f})", stock_items, "proti", "vysoke")
            if vix >= 25:
                size = min(size, 0.5)
                event_risk = "stredni"
                factors.append(f"zvýšená volatilita (VIX {vix:.0f}) → poloviční velikost")

        # 5) plánované události (Fed)
        if any("DNES" in e for e in ctx.events):
            size = min(size, 0.5)
            event_risk = "vysoke"
            factors.append("dnes rozhoduje Fed → poloviční velikost")

        if not ctx.news and not ctx.macro:
            size = min(size, 0.5)
            factors.append("kontext nedostupný → poloviční velikost")

        bias = "podporuje" if stock_score >= 1 else "neutralni"
        confidence = 0.75 if bias == "podporuje" else 0.65
        return AiDecision(True, confidence, size, {
            "take": True, "confidence": confidence, "size_factor": size, "context_bias": bias,
            "priced_in": priced_in, "event_risk": event_risk,
            "key_items": stock_items + mkt_items,
            "rationale": "; ".join(factors) + ".",
        })

    @staticmethod
    def _skip(reason: str, items: list[dict], bias: str, event_risk: str) -> AiDecision:
        return AiDecision(False, 0.8, 0.0, {
            "take": False, "confidence": 0.8, "size_factor": 0.0, "context_bias": bias,
            "priced_in": "-", "event_risk": event_risk, "key_items": items, "rationale": reason + ".",
        })
