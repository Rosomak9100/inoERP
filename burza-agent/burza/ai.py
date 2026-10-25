"""Posouzení kandidátního obchodu přes Claude API (Studie B).

Claude dostane technický signál + zprávy, výsledky firem, makro a tržní přehled a vrátí
strukturované rozhodnutí (JSON schema). Při jakékoli chybě nebo odmítnutí se obchod
NEotevře (fail closed).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

from .context import MarketContext
from .models import Signal

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """Jsi risk-analytik intradenního obchodního systému pro americké akcie a ETF.
Technická strategie už vygenerovala kandidáta na LONG obchod (vstup, stop-loss, cíl). Tvým úkolem
je posoudit, zda kontext (zprávy, výsledky firem, makrodata, Fed, inflace, zaměstnanost, USD,
komodity, geopolitika, sentiment) obchod podporuje, je neutrální, nebo proti němu mluví.

Pravidla:
- Jedna zpráva sama o sobě není důvod k obchodu. Hledej soulad více nezávislých informací.
- Rozlišuj čas publikace, očekávání trhu a to, co už je v ceně. Zpráva publikovaná před hodinami,
  na kterou trh už zareagoval (vidíš to v pohybu ceny), je pravděpodobně započtená.
- U výsledků firem porovnávej skutečnost s očekáváním (EPS/tržby vs. odhad), ne absolutní čísla.
- Zvýšené riziko událostí (rozhodnutí Fedu dnes, výsledky, zveřejnění CPI/zaměstnanosti) je důvod
  obchod přeskočit nebo zmenšit, ne zvětšit.
- Když kontext chybí nebo je nejasný, technický signál smí projít jen se sníženou velikostí (0.5).
- Velikost pozice nesmíš zvýšit: size_factor je 0.5 nebo 1.0.
- Neurčuj vstup, stop ani cíl – ty jsou dané. Rozhoduješ jen take / skip a size_factor.
- V key_items uváděj jen zprávy, které jsou v kontextu, s jejich zdrojem a časem publikace.
Odpovídej česky, stručně a věcně."""

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "take": {"type": "boolean", "description": "true = obchod otevřít, false = přeskočit"},
        "confidence": {"type": "number", "description": "0 až 1"},
        "size_factor": {"type": "number", "enum": [0.5, 1.0]},
        "context_bias": {"type": "string", "enum": ["podporuje", "neutralni", "proti", "nejasny"]},
        "priced_in": {"type": "string", "description": "co z informací je už v ceně a proč"},
        "event_risk": {"type": "string", "enum": ["nizke", "stredni", "vysoke"]},
        "key_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "published_at": {"type": "string"},
                    "headline": {"type": "string"},
                    "impact": {"type": "string", "enum": ["pozitivni", "negativni", "neutralni"]},
                },
                "required": ["source", "published_at", "headline", "impact"],
                "additionalProperties": False,
            },
        },
        "rationale": {"type": "string", "description": "2–4 věty zdůvodnění"},
    },
    "required": ["take", "confidence", "size_factor", "context_bias", "priced_in", "event_risk",
                 "key_items", "rationale"],
    "additionalProperties": False,
}


@dataclass
class AiDecision:
    take: bool
    confidence: float = 0.0
    size_factor: float = 0.0
    data: dict = field(default_factory=dict)
    error: str | None = None
    request_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


def _fmt_time(ts: datetime | None, now: datetime) -> str:
    if ts is None:
        return "čas neznámý"
    mins = int((now - ts).total_seconds() // 60)
    return f"{ts:%Y-%m-%d %H:%M} UTC (před {mins // 60} h {mins % 60} min)"


def build_prompt(sig: Signal, ctx: MarketContext, now: datetime, intraday_change_pct: float | None) -> str:
    lines = [f"Čas teď: {now:%Y-%m-%d %H:%M} UTC",
             "",
             f"## Kandidát ({sig.symbol}, LONG)",
             f"Vstup {sig.entry:.2f}, stop {sig.stop:.2f}, cíl {sig.target:.2f}. Technický důvod: {sig.reason}."]
    if intraday_change_pct is not None:
        lines.append(f"Dnešní změna ceny {sig.symbol} od otevření: {intraday_change_pct:+.2f} %")
    lines += ["", f"## Zprávy k {sig.symbol}"]
    lines += [f"- [{n.publisher or n.source}, {_fmt_time(n.published_at, now)}] {n.headline}"
              + (f" — {n.summary[:300]}" if n.summary else "") for n in ctx.news] or ["(žádné)"]
    lines += ["", "## Obecné tržní zprávy"]
    lines += [f"- [{n.publisher or n.source}, {_fmt_time(n.published_at, now)}] {n.headline}"
              for n in ctx.market_news] or ["(žádné)"]
    lines += ["", "## Výsledky firmy (kalendář)"]
    lines += [json.dumps(e, ensure_ascii=False) for e in ctx.earnings] or ["(žádné v okně −3 až +7 dní)"]
    lines += ["", "## Makrodata (FRED, poslední pozorování)"]
    lines += [f"- {v['popis']}: {v['hodnota']} (předchozí {v['predchozi']}, k {v['datum_pozorovani']})"
              for v in ctx.macro.values()] or ["(nedostupné)"]
    lines += ["", "## Tržní přehled (změna dnes, %)"]
    lines += [f"- {k}: {v:+.2f}" for k, v in ctx.market_moves.items()] or ["(nedostupné)"]
    lines += ["", "## Plánované události"] + (ctx.events or ["(žádné známé)"])
    if ctx.errors:
        lines += ["", "## Nedostupné zdroje", *ctx.errors]
    lines += ["", "Rozhodni podle pravidel a vrať JSON."]
    return "\n".join(lines)


class ClaudeJudge:
    """Placené Claude API (účtuje se zvlášť od předplatného Claude). Zapíná se ai.provider = "api"."""

    costs_money = True

    def __init__(self, model: str = "claude-opus-5-5", effort: str = "medium", client=None):
        self.model = model
        self.effort = effort
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import anthropic  # klíč z ANTHROPIC_API_KEY nebo `ant auth login` profilu
            self._client = anthropic.Anthropic(max_retries=3, timeout=120)
        return self._client

    def decide(self, sig: Signal, ctx: MarketContext, now: datetime,
               intraday_change_pct: float | None) -> AiDecision:
        return self.judge(build_prompt(sig, ctx, now, intraday_change_pct))

    def judge(self, prompt: str) -> AiDecision:
        import anthropic
        try:
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": self.effort,
                               "format": {"type": "json_schema", "schema": DECISION_SCHEMA}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.RateLimitError as exc:
            return AiDecision(False, error=f"rate limit: {exc.message}")
        except anthropic.APIStatusError as exc:
            return AiDecision(False, error=f"API {exc.status_code}: {exc.message}")
        except anthropic.APIConnectionError as exc:
            return AiDecision(False, error=f"spojení: {exc}")
        except Exception as exc:  # např. chybějící přihlašovací údaje
            return AiDecision(False, error=f"{type(exc).__name__}: {exc}")

        usage = getattr(resp, "usage", None)
        meta = dict(request_id=getattr(resp, "_request_id", None),
                    input_tokens=getattr(usage, "input_tokens", None),
                    output_tokens=getattr(usage, "output_tokens", None))
        if resp.stop_reason == "refusal":
            return AiDecision(False, error="model odmítl odpovědět", **meta)
        if resp.stop_reason == "max_tokens":
            return AiDecision(False, error="odpověď uříznuta (max_tokens)", **meta)
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return AiDecision(False, error="neplatný JSON v odpovědi", **meta)
        size = 1.0 if data.get("size_factor") == 1.0 else 0.5
        return AiDecision(bool(data.get("take")), float(data.get("confidence") or 0), size, data, **meta)
