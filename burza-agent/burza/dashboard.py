"""Webový dashboard (jen čtení): python -m burza dashboard --studies config_a.toml config_b.toml

Běží lokálně (výchozí 127.0.0.1:8080) a čte deníky obou studií. Do agentů nijak nezasahuje.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import OrderedDict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import market
from .config import Config

PAGE = Path(__file__).with_name("dashboard.html")
MAX_POINTS = 1500


def _connect(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _state(db, key, default=None):
    try:
        row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return default
    return json.loads(row["value"]) if row else default


def _rows(db, sql, args=()):
    try:
        return [dict(r) for r in db.execute(sql, args).fetchall()]
    except sqlite3.OperationalError:
        return []


def metrics_from(initial: float, equity: list[dict], trades: list[dict]) -> dict:
    pnls = [t["pnl"] for t in trades if t.get("pnl") is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    current = equity[-1]["equity"] if equity else initial
    peak, max_dd = initial, 0.0
    for e in equity:
        peak = max(peak, e["equity"])
        if peak:
            max_dd = max(max_dd, (peak - e["equity"]) / peak)
    daily: "OrderedDict[str, float]" = OrderedDict()
    for e in equity:
        d = market.to_et(datetime.fromisoformat(e["ts"])).date().isoformat()
        daily[d] = e["equity"]
    daily_rows, prev = [], initial
    for d, eq in daily.items():
        daily_rows.append({"date": d, "equity": round(eq, 2),
                           "ret_pct": round((eq / prev - 1) * 100, 3) if prev else 0.0,
                           "cum_pct": round((eq / initial - 1) * 100, 3) if initial else 0.0})
        prev = eq
    rets = [r["ret_pct"] / 100 for r in daily_rows]
    sharpe = None
    if len(rets) > 1:
        mean = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
        sharpe = round(mean / sd * math.sqrt(252), 2) if sd else None
    return {
        "initial": round(initial, 2),
        "current": round(current, 2),
        "return_pct": round((current / initial - 1) * 100, 3) if initial else 0.0,
        "realized": round(sum(pnls), 2),
        "unrealized": round(equity[-1]["open_pnl"], 2) if equity else 0.0,
        "max_dd_pct": round(max_dd * 100, 3),
        "trades": len(pnls),
        "win_rate_pct": round(len(wins) / len(pnls) * 100, 1) if pnls else None,
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) else None,
        "sharpe": sharpe,
        "costs": round(sum(t.get("costs") or 0 for t in trades), 2),
        "estimated_pnl_trades": sum(1 for t in trades if t.get("pnl_source") == "odhad"),
        "daily": daily_rows,
    }


def _downsample(rows: list[dict]) -> list[dict]:
    if len(rows) <= MAX_POINTS:
        return rows
    step = math.ceil(len(rows) / MAX_POINTS)
    return rows[::step] + ([rows[-1]] if (len(rows) - 1) % step else [])


def study_snapshot(cfg: Config) -> dict:
    base = {"id": cfg.study, "name": cfg.display_name, "mode": cfg.mode,
            "strategy": cfg.strategy if cfg.study == "A" else f"AI + {cfg.ai.base_strategy}",
            "methodology": methodology(cfg)}
    db = _connect(cfg.db_path)
    if db is None:
        return {**base, "running": False, "error": f"Deník {cfg.db_path} zatím neexistuje"}
    try:
        risk = _state(db, f"risk:{cfg.mode}", {}) or {}
        initial = risk.get("initial_capital") or cfg.initial_capital_usd
        equity = _rows(db, "SELECT ts, equity, cash, open_pnl, benchmark FROM equity ORDER BY ts")
        closed = _rows(db, "SELECT * FROM trades WHERE close_ts IS NOT NULL ORDER BY close_ts")
        open_tr = _rows(db, "SELECT * FROM trades WHERE close_ts IS NULL AND mode=?", (cfg.mode,))
        prices = _state(db, "last_prices", {}) or {}
        for t in open_tr:
            px = prices.get(t["symbol"])
            t["last_price"] = px
            t["unrealized"] = round((px - t["open_price"]) * t["units"], 2) if px and t["units"] else None
        hb = Path(cfg.data_dir) / "heartbeat"
        hb_age = None
        if hb.exists():
            hb_age = (datetime.now(timezone.utc) - datetime.fromisoformat(hb.read_text())).total_seconds()
        bench = next((e["benchmark"] for e in equity if e.get("benchmark")), None)
        series = [{"t": e["ts"], "equity": round(e["equity"], 2),
                   "ret_pct": round((e["equity"] / initial - 1) * 100, 4) if initial else 0,
                   "bench_pct": round((e["benchmark"] / bench - 1) * 100, 4)
                   if bench and e.get("benchmark") else None} for e in equity]
        return {
            **base,
            "running": hb_age is not None and hb_age < 300,
            "heartbeat_age_s": round(hb_age) if hb_age is not None else None,
            "kill_switch": Path(cfg.kill_file).exists(),
            "risk": risk,
            "metrics": metrics_from(initial or 0, equity, closed),
            "series": _downsample(series),
            "open_positions": open_tr,
            "trades": closed[-200:][::-1],
            "rejected": _rows(db, "SELECT ts, symbol, entry, stop, target, reason, decision FROM signals"
                                  " WHERE accepted=0 ORDER BY ts DESC LIMIT 100"),
            "rejected_count": (_rows(db, "SELECT COUNT(*) AS n FROM signals WHERE accepted=0") or [{"n": 0}])[0]["n"],
            "ai_decisions": _rows(db, "SELECT ts, symbol, take, confidence, size_factor, decision, model,"
                                      " error, input_tokens, output_tokens FROM ai_decisions"
                                      " ORDER BY ts DESC LIMIT 50"),
            "events": _rows(db, "SELECT ts, level, kind, message FROM events ORDER BY id DESC LIMIT 50"),
        }
    finally:
        db.close()


def methodology(cfg: Config) -> dict:
    return {
        "studie": cfg.display_name,
        "rezim": cfg.mode,
        "strategie": cfg.strategy if cfg.study == "A" else
        f"{cfg.ai.base_strategy} + posouzení kontextu ({cfg.ai.model}, effort {cfg.ai.effort}, "
        f"min. jistota {cfg.ai.min_confidence})",
        "data": f"eToro svíčky {cfg.interval}, jen uzavřené svíčky",
        "zpravy": "nepoužívá" if cfg.study == "A" else "Finnhub (zprávy, výsledky), FRED (makro), FOMC kalendář",
        "start": cfg.start_date or "-",
        "kapital_czk": cfg.initial_capital_czk or None,
        "kurz_usdczk": cfg.usdczk_start_rate or None,
        "kapital_usd": cfg.initial_capital_usd or None,
        "naklady": f"spread {cfg.costs.spread_bps} bps, skluz {cfg.costs.slippage_bps} bps, "
                   f"poplatek {cfg.costs.commission_usd} USD/strana" + (" (simulace)" if cfg.mode == "paper"
                                                                       else " (skutečné poplatky eToro)"),
        "ocenovani": "equity = hotovost + tržní hodnota pozic (poslední close / eToro PnL)",
        "riziko": f"{cfg.risk.risk_per_trade_pct} % na obchod, denní limit {cfg.risk.max_daily_loss_pct} %, "
                  f"celkový {cfg.risk.max_drawdown_pct} %, max {cfg.risk.max_open_positions} pozice",
        "benchmark": f"{cfg.benchmark_symbol} (S&P 500 ETF)",
    }


def comparability(cfgs: list[Config]) -> list[str]:
    """Upozornění, pokud studie nemají stejný start, kurz, oceňování a poplatky."""
    if len(cfgs) < 2:
        return []
    warn = []
    checks = [("počáteční kapitál (Kč)", lambda c: c.initial_capital_czk),
              ("kurz USD/CZK", lambda c: c.usdczk_start_rate),
              ("datum startu", lambda c: c.start_date),
              ("náklady (paper)", lambda c: (c.costs.spread_bps, c.costs.slippage_bps, c.costs.commission_usd)),
              ("režim", lambda c: c.mode),
              ("risk limity", lambda c: (c.risk.risk_per_trade_pct, c.risk.max_daily_loss_pct, c.risk.max_drawdown_pct))]
    for label, fn in checks:
        if len({json.dumps(fn(c), default=str) for c in cfgs}) > 1:
            warn.append(f"Studie se liší v: {label}")
    return warn


def build_payload(cfgs: list[Config]) -> dict:
    return {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "market_open": market.is_open(datetime.now(timezone.utc)),
            "studies": [study_snapshot(c) for c in cfgs],
            "comparability": comparability(cfgs)}


def serve(cfgs: list[Config], host: str = "127.0.0.1", port: int = 8080) -> None:
    page = PAGE.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path in ("/", "/index.html"):
                self._send(200, page, "text/html; charset=utf-8")
            elif self.path == "/api/data":
                body = json.dumps(build_payload(cfgs), ensure_ascii=False, default=str).encode()
                self._send(200, body, "application/json; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Dashboard běží na http://{host}:{port}  (Ctrl+C ukončí)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
