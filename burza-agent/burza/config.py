"""Načtení konfigurace z TOML a klíčů z prostředí."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REAL_MONEY_ENV = "BURZA_I_ACCEPT_REAL_MONEY_RISK"


class ConfigError(Exception):
    pass


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 1.0
    max_daily_loss_pct: float = 3.0
    max_drawdown_pct: float = 15.0
    max_open_positions: int = 3
    max_position_pct: float = 25.0
    max_trades_per_day: int = 10
    flatten_on_daily_limit: bool = True


@dataclass
class SessionConfig:
    no_entry_first_minutes: int = 15
    no_entry_last_minutes: int = 30
    flatten_minutes_before_close: int = 10
    stale_data_seconds: int = 900


@dataclass
class CostConfig:
    spread_bps: float = 2.0
    commission_usd: float = 0.0
    slippage_bps: float = 1.0


@dataclass
class AiConfig:
    base_strategy: str = "vwap_pullback"   # technický kandidát, který AI posuzuje
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    min_confidence: float = 0.6
    max_calls_per_day: int = 40
    news_lookback_hours: int = 24
    context_cache_minutes: int = 15


@dataclass
class Config:
    study: str = "A"                       # A = technická, B = AI + zprávy/makro
    study_name: str = ""
    mode: str = "paper"
    real_trading_confirmed: bool = False
    strategy: str = "vwap_pullback"
    interval: str = "5m"
    poll_seconds: int = 30
    initial_capital_usd: float = 0.0
    initial_capital_czk: float = 0.0       # společný start obou studií (např. 200 000 Kč)
    usdczk_start_rate: float = 0.0         # pevný kurz USD/CZK ke dni startu, stejný pro obě studie
    start_date: str = ""
    benchmark_symbol: str = "SPY"
    benchmark_instrument_id: int = 3000
    data_dir: str = "data"
    kill_file: str = "STOP"
    instruments: dict[str, int] = field(default_factory=dict)
    risk: RiskConfig = field(default_factory=RiskConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    ai: AiConfig = field(default_factory=AiConfig)
    api_key: str = ""
    user_key: str = ""
    anthropic_api_key: str = ""
    finnhub_api_key: str = ""
    fred_api_key: str = ""

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "burza.db"

    @property
    def interval_minutes(self) -> int:
        return {"1m": 1, "5m": 5, "15m": 15}[self.interval]

    @property
    def display_name(self) -> str:
        return self.study_name or f"Studie {self.study}"

    def validate(self) -> None:
        if self.study not in ("A", "B"):
            raise ConfigError("study musí být A nebo B")
        if self.study == "B" and not self.finnhub_api_key and not self.fred_api_key:
            raise ConfigError("Studie B potřebuje aspoň FINNHUB_API_KEY nebo FRED_API_KEY")
        if self.mode not in ("paper", "demo", "real"):
            raise ConfigError(f"Neznámý mode: {self.mode}")
        if self.interval not in ("1m", "5m", "15m"):
            raise ConfigError(f"Nepodporovaný interval: {self.interval}")
        if not self.instruments:
            raise ConfigError("Chybí [instruments]")
        if self.mode in ("demo", "real") and not (self.api_key and self.user_key):
            raise ConfigError("Chybí ETORO_API_KEY / ETORO_USER_KEY v prostředí")
        if self.mode == "real":
            if not self.real_trading_confirmed:
                raise ConfigError("Režim real vyžaduje real_trading_confirmed = true v configu")
            if os.environ.get(REAL_MONEY_ENV) != "ano":
                raise ConfigError(f"Režim real vyžaduje proměnnou prostředí {REAL_MONEY_ENV}=ano")
        r = self.risk
        if not (0 < r.risk_per_trade_pct <= 2):
            raise ConfigError("risk_per_trade_pct musí být v (0, 2]")
        if not (0 < r.max_daily_loss_pct <= 10) or not (0 < r.max_drawdown_pct <= 30):
            raise ConfigError("Denní / celkový limit ztráty mimo povolený rozsah")


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    # utf-8-sig: Poznámkový blok / PowerShell můžou na začátek souboru přidat BOM
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config(path: str | Path = "config_a.toml", validate: bool = True) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Konfigurace {path} neexistuje – zkopíruj config_a.example.toml / config_b.example.toml")
    _load_dotenv(path.parent / ".env")
    raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    cfg = Config(
        risk=RiskConfig(**raw.pop("risk", {})),
        session=SessionConfig(**raw.pop("session", {})),
        costs=CostConfig(**raw.pop("costs", {})),
        ai=AiConfig(**raw.pop("ai", {})),
        instruments={k.upper(): int(v) for k, v in raw.pop("instruments", {}).items()},
        **raw,
    )
    cfg.api_key = os.environ.get("ETORO_API_KEY", "")
    cfg.user_key = os.environ.get("ETORO_USER_KEY", "")
    cfg.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    cfg.finnhub_api_key = os.environ.get("FINNHUB_API_KEY", "")
    cfg.fred_api_key = os.environ.get("FRED_API_KEY", "")
    if cfg.initial_capital_czk > 0 and cfg.usdczk_start_rate > 0:
        cfg.initial_capital_usd = round(cfg.initial_capital_czk / cfg.usdczk_start_rate, 2)
    if validate:
        cfg.validate()
    return cfg
