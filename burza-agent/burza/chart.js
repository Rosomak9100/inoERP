// Sdílené pomocné funkce a čárový graf (dashboard i historický report).
const $ = (s, el = document) => el.querySelector(s);
const fmt = (v, d = 2) => v == null || Number.isNaN(v) ? "–" : Number(v).toLocaleString("cs-CZ", {minimumFractionDigits: d, maximumFractionDigits: d});
const usd = v => v == null ? "–" : fmt(v) + " USD";
const pct = v => v == null ? "–" : (v > 0 ? "+" : "") + fmt(v) + " %";
const signCls = v => v > 0 ? "pos" : v < 0 ? "neg" : "";
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const dt = s => s ? new Date(s).toLocaleString("cs-CZ", {dateStyle: "short", timeStyle: "short"}) : "–";
const SERIES = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)"];

// Čárový graf v SVG s křížem a tooltipem. series: [{name, short?, color, points:[{t: Date, v}], band?:[{t, lo, hi}]}]
// band = rozpětí kolem čáry (např. 10.–90. percentil simulace), kreslí se jako průhledná plocha.
function lineChart(el, series, unit, opts = {}) {
  const W = el.clientWidth || 800, H = 280, m = {l: 64, r: W < 500 ? 84 : 128, t: 12, b: 28};
  const all = series.flatMap(s => s.points);
  if (!all.length) { el.innerHTML = `<div class="empty">Zatím žádná data</div>`; return; }
  const t0 = Math.min(...all.map(p => p.t)), t1 = Math.max(...all.map(p => p.t)) || t0 + 1;
  const bands = series.flatMap(s => s.band || []);
  let v0 = Math.min(...all.map(p => p.v), ...bands.map(b => b.lo)), v1 = Math.max(...all.map(p => p.v), ...bands.map(b => b.hi));
  if (unit === "%") { v0 = Math.min(v0, 0); v1 = Math.max(v1, 0); }
  if (v0 === v1) { v0 -= 1; v1 += 1; }
  const pad = (v1 - v0) * 0.08; v0 -= pad; v1 += pad;
  const x = t => m.l + (t - t0) / ((t1 - t0) || 1) * (W - m.l - m.r);
  const y = v => m.t + (1 - (v - v0) / (v1 - v0)) * (H - m.t - m.b);
  const ticks = 5, yt = [...Array(ticks)].map((_, i) => v0 + (v1 - v0) * i / (ticks - 1));
  const fy = v => unit === "%" ? fmt(v, 1) + " %" : fmt(v, 0);
  let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(series.map(s => s.name).join(", "))}">`;
  yt.forEach(v => svg += `<line x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)" stroke-width="1"/>
    <text x="${m.l - 8}" y="${y(v) + 4}" text-anchor="end" font-size="11" fill="var(--muted)">${fy(v)}</text>`);
  if (unit === "%") svg += `<line x1="${m.l}" x2="${W - m.r}" y1="${y(0)}" y2="${y(0)}" stroke="var(--axis)" stroke-width="1"/>`;
  const xt = W - m.l - m.r < 260 ? [[t0, "start"], [t1, "end"]] : [[t0, "start"], [(t0 + t1) / 2, "middle"], [t1, "end"]];
  xt.forEach(([t, a]) => svg += `<text x="${x(t)}" y="${H - 8}" font-size="11" fill="var(--muted)" text-anchor="${a}">${new Date(t).toLocaleDateString("cs-CZ")}</text>`);
  series.forEach(s => {
    if (!s.band || !s.band.length) return;
    const up = s.band.map((b, i) => `${i ? "L" : "M"}${x(b.t).toFixed(1)},${y(b.hi).toFixed(1)}`).join("");
    const down = [...s.band].reverse().map(b => `L${x(b.t).toFixed(1)},${y(b.lo).toFixed(1)}`).join("");
    svg += `<path d="${up}${down}Z" fill="${s.color}" fill-opacity="0.12" stroke="none"/>`;
  });
  const labels = [];
  series.forEach(s => {
    if (!s.points.length) return;
    const d = s.points.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join("");
    svg += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
    const last = s.points[s.points.length - 1];
    labels.push({x: x(last.t) + 6, y: y(last.v) + 4, text: s.short || s.name});
  });
  // popisky na konci čar od sebe odsunout, ať se nepřekrývají
  labels.sort((a, b) => a.y - b.y).forEach((l, i, arr) => { if (i && l.y < arr[i - 1].y + 13) l.y = arr[i - 1].y + 13; });
  if (series.length > 1) labels.forEach(l => svg += `<text x="${l.x}" y="${l.y}" font-size="11" fill="var(--ink-2)">${esc(l.text)}</text>`);
  svg += `<line id="xh" y1="${m.t}" y2="${H - m.b}" stroke="var(--axis)" stroke-width="1" visibility="hidden"/>`;
  series.forEach((s, i) => svg += `<circle id="pt${i}" r="4" fill="${s.color}" stroke="var(--surface)" stroke-width="2" visibility="hidden"/>`);
  svg += `<rect x="${m.l}" y="${m.t}" width="${W - m.l - m.r}" height="${H - m.t - m.b}" fill="transparent" id="hit"/></svg><div class="tip"></div>`;
  el.innerHTML = svg;
  const tip = $(".tip", el), hit = $("#hit", el), xh = $("#xh", el);
  hit.addEventListener("pointermove", ev => {
    const r = el.querySelector("svg").getBoundingClientRect();
    const px = (ev.clientX - r.left) * W / r.width;
    const t = t0 + (px - m.l) / (W - m.l - m.r) * (t1 - t0);
    let html = `<div>${new Date(t).toLocaleString("cs-CZ", opts.dateOnly ? {dateStyle: "short"} : {dateStyle: "short", timeStyle: "short"})}</div>`;
    series.forEach((s, i) => {
      const pt = s.points.reduce((a, b) => Math.abs(b.t - t) < Math.abs(a.t - t) ? b : a, s.points[0]);
      const c = $("#pt" + i, el);
      if (!pt) return;
      c.setAttribute("cx", x(pt.t)); c.setAttribute("cy", y(pt.v)); c.setAttribute("visibility", "visible");
      const fv = v => unit === "%" ? pct(v) : fmt(v, unit === "Kč" ? 0 : 2) + " " + unit;
      const b = s.band && s.band.find(b => b.t === pt.t);
      html += `<div><span style="display:inline-block;width:10px;height:2px;background:${s.color};vertical-align:middle;margin-right:6px"></span>${esc(s.name)}: <b>${fv(pt.v)}</b>${b ? ` <span style="color:var(--muted)">(${fv(b.lo)} až ${fv(b.hi)})</span>` : ""}</div>`;
    });
    xh.setAttribute("x1", px); xh.setAttribute("x2", px); xh.setAttribute("visibility", "visible");
    tip.innerHTML = html; tip.style.display = "block";
    const left = (ev.clientX - r.left) + 14;
    tip.style.left = Math.min(left, r.width - tip.offsetWidth - 4) + "px"; tip.style.top = "12px";
  });
  hit.addEventListener("pointerleave", () => {
    tip.style.display = "none"; xh.setAttribute("visibility", "hidden");
    series.forEach((_, i) => $("#pt" + i, el).setAttribute("visibility", "hidden"));
  });
}
