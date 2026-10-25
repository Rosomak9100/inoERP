"""Historický report: jak by si zvolená swing strategie vedla v minulosti proti držení S&P 500 a Nasdaq 100.

Počítá se z denních dat eToro (přepočtených na splity) se skutečnými poplatky. Hodnoty jsou v Kč při
pevném startovním kurzu (bez vlivu pohybu kurzu USD/CZK). Studii B (zprávy) nejde zpětně otestovat,
protože chybí archiv historických zpráv – report ukazuje technický základ obou studií.
"""

from __future__ import annotations

import copy
import json
import math
from datetime import date
from pathlib import Path

from . import market
from .backtest_daily import run_daily_backtest
from .config import Config
from .models import Candle
from .strategy import make_strategy
from .strategy_daily import DAILY_STRATEGIES

WARMUP_DAYS = 210   # SMA200 potřebuje historii – srovnání začíná, až strategie může obchodovat


def _stats(values: list[tuple[date, float]]) -> dict:
    vals = [v for _, v in values]
    peak, max_dd = vals[0], 0.0
    for v in vals:
        peak = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak)
    rets = [b / a - 1 for a, b in zip(vals, vals[1:]) if a]
    mean = sum(rets) / len(rets) if rets else 0.0
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) if len(rets) > 1 else 0.0
    years = max((values[-1][0] - values[0][0]).days / 365.25, 1e-9)
    by_year: dict[int, list[float]] = {}
    for d, v in values:
        by_year.setdefault(d.year, []).append(v)
    yearly, prev_end = {}, vals[0]
    for y in sorted(by_year):
        yearly[y] = round((by_year[y][-1] / prev_end - 1) * 100, 2)
        prev_end = by_year[y][-1]
    return {
        "konec": round(vals[-1], 0),
        "vynos_pct": round((vals[-1] / vals[0] - 1) * 100, 2),
        "rocne_pct": round(((vals[-1] / vals[0]) ** (1 / years) - 1) * 100, 2),
        "max_dd_pct": round(max_dd * 100, 2),
        "sharpe": round(mean / sd * math.sqrt(252), 2) if sd else 0.0,
        "nejhorsi_rok_pct": min(yearly.values()) if yearly else 0.0,
        "roky": yearly,
    }


def _hold(candles: list[Candle], days: list[date], start_value: float) -> list[tuple[date, float]]:
    closes = {market.to_et(c.time).date(): c.close for c in candles}
    first = next((closes[d] for d in days if d in closes), None)
    if not first:
        return []
    out, last = [], first
    for d in days:
        last = closes.get(d, last)
        out.append((d, start_value * last / first))
    return out


def build_history(cfg: Config, data: dict[str, list[Candle]], strategy_name: str) -> dict:
    swing_cfg = copy.copy(cfg)
    swing_cfg.risk = copy.copy(cfg.risk)
    swing_cfg.risk.flatten_on_daily_limit = False
    initial_usd = cfg.initial_capital_usd or 10_000.0
    rate = cfg.usdczk_start_rate or 1.0
    start_czk = initial_usd * rate
    all_days = sorted({market.to_et(c.time).date() for cs in data.values() for c in cs})
    days = all_days[WARMUP_DAYS:]
    if len(days) < 20:
        raise ValueError(f"málo historie: {len(all_days)} obchodních dní, potřeba aspoň {WARMUP_DAYS + 20}")

    def run(name):
        res = run_daily_backtest(swing_cfg, make_strategy(name), data, initial_usd, days)
        series = [(d, v * rate) for d, v in res.daily_equity]
        return res, series

    res, strat_series = run(strategy_name)
    lines = {f"Strategie {strategy_name}": strat_series}
    for sym, label in ((cfg.benchmark_symbol, "S&P 500"), (cfg.benchmark2_symbol, "Nasdaq 100")):
        hold = _hold(data.get(sym, []), days, start_czk)
        if hold:
            lines[f"{label} ({sym}) – kup a drž"] = hold

    summary = {name: _stats(series) for name, series in lines.items()}
    summary[f"Strategie {strategy_name}"].update(
        obchodu=len(res.trades), naklady_kc=round(sum(t.costs for t in res.trades) * rate, 0))
    others = {}
    for name in DAILY_STRATEGIES:
        if name == strategy_name:
            continue
        r2, s2 = run(name)
        others[name] = {**_stats(s2), "obchodu": len(r2.trades)}
    return {
        "strategie": strategy_name,
        "od": str(days[0]), "do": str(days[-1]),
        "start_kc": round(start_czk, 0), "kurz": rate,
        "instrumenty": list(data),
        "lines": {name: [[str(d), round(v, 2)] for d, v in series] for name, series in lines.items()},
        "summary": summary,
        "others": others,
    }


def render_html(report: dict) -> str:
    chart_js = Path(__file__).with_name("chart.js").read_text(encoding="utf-8")
    return TEMPLATE.replace("/*CHART_JS*/", chart_js).replace(
        "/*DATA*/null", json.dumps(report, ensure_ascii=False).replace("</", "<\\/"))


TEMPLATE = """<!doctype html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Historické srovnání</title>
<style>
:root { color-scheme: light; --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,.10); --series-1:#2a78d6; --series-2:#eb6834;
  --series-3:#1baf7a; --series-4:#eda100; --good:#006300; --critical:#d03b3b; }
@media (prefers-color-scheme: dark) { :root:where(:not([data-theme="light"])) { color-scheme: dark;
  --page:#0d0d0d; --surface:#1a1a19; --ink:#f0efec; --ink-2:#c3c2b7; --grid:#2c2c2a; --axis:#383835;
  --border:rgba(255,255,255,.10); --series-1:#3987e5; --series-2:#d95926; --series-3:#199e70; --series-4:#c98500; --good:#0ca30c; } }
:root[data-theme="dark"] { color-scheme: dark; --page:#0d0d0d; --surface:#1a1a19; --ink:#f0efec; --ink-2:#c3c2b7;
  --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10); --series-1:#3987e5; --series-2:#d95926;
  --series-3:#199e70; --series-4:#c98500; --good:#0ca30c; }
* { box-sizing: border-box; }
body { margin:0; background:var(--page); color:var(--ink); font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; }
main { max-width:1180px; margin:0 auto; padding:16px; }
h1 { font-size:20px; margin:4px 0; }
.meta { color:var(--ink-2); font-size:13px; margin-bottom:16px; }
.card { background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:16px; margin-bottom:16px; }
.card h2 { font-size:15px; margin:0 0 12px; }
.table-wrap { overflow-x:auto; }
table { border-collapse:collapse; width:100%; font-size:13px; }
th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--grid); white-space:nowrap; }
th { color:var(--ink-2); font-weight:600; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
.pos::before { content:"▲ "; color:var(--good); font-size:10px; }
.neg::before { content:"▼ "; color:var(--critical); font-size:10px; }
.chart { position:relative; }
.chart svg { width:100%; height:280px; display:block; }
.chart .tip { position:absolute; pointer-events:none; background:var(--surface); border:1px solid var(--border);
  border-radius:8px; padding:8px 10px; font-size:12px; box-shadow:0 4px 12px rgba(0,0,0,.12); display:none; white-space:nowrap; }
.legend { display:flex; flex-wrap:wrap; gap:16px; font-size:13px; color:var(--ink-2); margin-bottom:8px; }
.legend span::before { content:""; display:inline-block; width:14px; height:2px; margin-right:6px; vertical-align:middle; background:var(--c); }
.note { color:var(--ink-2); font-size:13px; }
.note li { margin-bottom:4px; }
.empty { color:var(--muted); }
</style>
</head>
<body>
<main>
  <h1>Jak by si strategie vedla v minulosti</h1>
  <div class="meta" id="meta"></div>
  <div class="card"><h2>Hodnota 200 000 Kč v čase</h2><div class="legend" id="lg"></div><div class="chart" id="ch"></div></div>
  <div class="card"><h2>Souhrn</h2><div id="sum"></div></div>
  <div class="card"><h2>Výnos po jednotlivých letech</h2><div id="years"></div></div>
  <div class="card"><h2>Ostatní swing strategie (pro srovnání)</h2><div id="others"></div></div>
  <div class="card note"><h2>Jak to číst</h2><ul>
    <li>Strategie i „kup a drž“ začínají stejný den se stejnou částkou. Strategie platí skutečné poplatky eToro, „kup a drž“ je bez poplatků.</li>
    <li>Strategie drží nejvýš 3 pozice a riskuje 1 % kapitálu na obchod – často je část peněz v hotovosti. Proto obvykle vydělá méně než index v rostoucím trhu, ale má menší propady.</li>
    <li>Hodnoty v Kč jsou při pevném startovním kurzu; skutečný pohyb kurzu USD/CZK by výsledky posunul.</li>
    <li>Studii B (filtr zpráv) zpětně otestovat nejde – chybí archiv historických zpráv. Report ukazuje technický základ obou studií.</li>
    <li>Velké firmy v seznamu jsou vybrané zpětně (dnes úspěšné) – minulé výsledky to nadhodnocuje. Minulost nezaručuje budoucnost.</li>
  </ul></div>
</main>
<script>
/*CHART_JS*/
const R = /*DATA*/null;
const kc = v => v == null ? "–" : fmt(v, 0) + " Kč";
$("#meta").textContent = `Strategie ${R.strategie} · ${R.od} až ${R.do} · start ${kc(R.start_kc)} (kurz ${fmt(R.kurz, 4)} Kč/USD) · instrumenty: ${R.instrumenty.join(", ")}`;
const names = Object.keys(R.lines);
const colors = [SERIES[0], SERIES[2], SERIES[3]];
const series = names.map((n, i) => ({name: n, short: n.split(" (")[0].replace(/^Strategie .*/, "Strategie"), color: colors[i % 3], points: R.lines[n].map(([d, v]) => ({t: Date.parse(d), v}))}));
$("#lg").innerHTML = series.map(s => `<span style="--c:${s.color}">${esc(s.name)}</span>`).join("");
lineChart($("#ch"), series, "Kč");
const row = (n, m) => `<tr><td>${esc(n)}</td><td class="num">${kc(m.konec)}</td>
  <td class="num ${signCls(m.vynos_pct)}">${pct(m.vynos_pct)}</td><td class="num">${pct(m.rocne_pct)}</td>
  <td class="num">${pct(-m.max_dd_pct)}</td><td class="num">${fmt(m.sharpe)}</td>
  <td class="num ${signCls(m.nejhorsi_rok_pct)}">${pct(m.nejhorsi_rok_pct)}</td>
  <td class="num">${m.obchodu ?? "–"}</td></tr>`;
const head = `<tr><th></th><th class="num">Konečná hodnota</th><th class="num">Celkem</th><th class="num">Ročně</th>
  <th class="num">Max. propad</th><th class="num">Sharpe</th><th class="num">Nejhorší rok</th><th class="num">Obchodů</th></tr>`;
$("#sum").innerHTML = `<div class="table-wrap"><table>${head}${names.map(n => row(n, R.summary[n])).join("")}</table></div>`;
const yrs = [...new Set(names.flatMap(n => Object.keys(R.summary[n].roky)))].sort();
$("#years").innerHTML = `<div class="table-wrap"><table><tr><th>Rok</th>${names.map(n => `<th class="num">${esc(n)}</th>`).join("")}</tr>
  ${yrs.map(y => `<tr><td>${y}${y == yrs[0] ? " (od " + R.od + ")" : ""}</td>${names.map(n => {
    const v = R.summary[n].roky[y]; return `<td class="num ${signCls(v)}">${pct(v)}</td>`;}).join("")}</tr>`).join("")}</table></div>`;
const on = Object.keys(R.others);
$("#others").innerHTML = on.length ? `<div class="table-wrap"><table>${head}${on.map(n => row(n, R.others[n])).join("")}</table></div>`
  : `<div class="empty">–</div>`;
</script>
</body>
</html>
"""
