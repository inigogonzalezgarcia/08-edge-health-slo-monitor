"""Self-contained HTML report: SLO status per target, budget burn-down, alert history."""
from __future__ import annotations

import html
import math
import json
import sqlite3
from datetime import datetime, timezone

from . import slo, store
from .config import Target

ICONS = {"good": "✓", "warning": "!", "serious": "▲", "critical": "✕", "unknown": "?"}
LABELS = {"good": "Healthy", "warning": "Watch", "serious": "At risk", "critical": "SLO missed", "unknown": "No data"}


def _fmt_ts(ts: float | None) -> str:
    if ts is None:
        return "—"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _duration(opened: float, closed: float | None, now: float) -> str:
    minutes = int(((closed or now) - opened) // 60)
    text = f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"
    return text + ("" if closed else " (open)")


def _burn(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}×"


def burndown(con: sqlite3.Connection, t: Target, since: float) -> list[dict]:
    """Error budget remaining at the end of each day, counted from the start of the window."""
    points, total, bad = [], 0, 0
    for row in store.daily(con, t.name, since):
        total += row["total"]
        bad += row["bad"] or 0
        points.append({"day": row["day"], "remaining": round(100 * (1 - (bad / total) / t.error_budget), 1),
                       "bad": row["bad"] or 0, "total": row["total"]})
    return points


def _chart(points: list[dict], chart_id: str) -> str:
    """Single-series line, 0–100 % with room below zero when the budget is exhausted."""
    if not points:
        return "<p class='muted'>No data yet.</p>"
    w, h, left, right, top, bottom = 340, 150, 40, 8, 8, 22
    lowest = min(p["remaining"] for p in points)
    low = 0 if lowest >= 0 else max(-200, -50 * math.ceil(-lowest / 50))   # extend in 50 % steps, cap at -200 %
    span = 100 - low

    def x(i):
        return left + (w - left - right) * (i / max(1, len(points) - 1))

    def y(v):
        return top + (h - top - bottom) * (100 - max(low, min(100, v))) / span

    grid = []
    for tick in (100, 50, 0) + ((low,) if low < 0 else ()):
        grid.append(f"<line class='grid' x1='{left}' x2='{w - right}' y1='{y(tick):.1f}' y2='{y(tick):.1f}'/>"
                    f"<text class='axis' x='{left - 6}' y='{y(tick) + 4:.1f}' text-anchor='end'>{tick:.0f}%</text>")
    path = " ".join(f"{'M' if i == 0 else 'L'}{x(i):.1f},{y(p['remaining']):.1f}" for i, p in enumerate(points))
    area = f"{path} L{x(len(points) - 1):.1f},{y(0):.1f} L{x(0):.1f},{y(0):.1f} Z"   # wash down to the 0 % line
    last = points[-1]
    labels = (f"<text class='axis' x='{left}' y='{h - 6}'>{points[0]['day'][5:]}</text>"
              f"<text class='axis' x='{w - right}' y='{h - 6}' text-anchor='end'>{last['day'][5:]}</text>")
    data = html.escape(json.dumps(points))
    return (f"<svg class='chart' id='{chart_id}' viewBox='0 0 {w} {h}' role='img' "
            f"aria-label='Error budget remaining by day' data-points='{data}' "
            f"data-geom='{left},{w - right},{top},{h - bottom}'>"
            + "".join(grid) + labels
            + f"<path class='area' d='{area}'/><path class='line' d='{path}'/>"
            f"<circle class='end' cx='{x(len(points) - 1):.1f}' cy='{y(last['remaining']):.1f}' r='4'/>"
            f"<line class='cross' x1='0' x2='0' y1='{top}' y2='{h - bottom}' visibility='hidden'/></svg>")


def render(con: sqlite3.Connection, targets: list[Target], now: float, window_days: int) -> str:
    since = now - window_days * 86400
    cards, tables = [], []
    for i, t in enumerate(targets):
        s = slo.evaluate(con, t, now, window_days)
        last = store.latest(con, t.name)
        p95 = store.latency_percentile(con, t.name, since, 95)
        remaining = s.budget_remaining
        meter = 0 if remaining is None else max(0.0, min(1.0, remaining))
        extra = json.loads(last["extra"]) if last and last["extra"] else {}
        facts = [
            ("SLI (30 d)", "—" if s.sli is None else f"{s.sli:.3f}%"),
            ("Objective", f"{t.slo}%" + (f" · ≤{t.latency_ms} ms" if t.latency_ms else "")),
            ("Burn 1 h / 6 h", f"{_burn(s.burn['1h'])} / {_burn(s.burn['6h'])}"),
            ("p95 latency", "—" if p95 is None else f"{p95:.0f} ms"),
        ]
        if "days_left" in extra:
            renew = extra["days_left"] < t.tls_warn_days
            facts.append(("Certificate", f"{extra['days_left']:.0f} days left"
                          + (" <span class='badge warning'><i>!</i>Renew soon</span>" if renew else "")))
        firing = "".join(f"<li><b>{r.severity}</b> · {html.escape(r.name)}: {html.escape(r.meaning)}</li>"
                         for r in s.firing)
        points = burndown(con, t, since)
        endpoint = t.url or (f"{t.host}:{t.port}" if t.port else t.host)
        cards.append(f"""
<section class="card">
  <header><div><h2>{html.escape(t.name)}</h2><span class="muted">{t.kind.upper()} · {html.escape(endpoint)}</span></div>
  <span class="badge {s.level}"><i>{ICONS[s.level]}</i>{LABELS[s.level]}</span></header>
  <div class="budget"><div class="row"><span>Error budget left</span><b>{"—" if remaining is None else f"{remaining * 100:.0f}%"}</b></div>
  <div class="meter {s.level}"><div style="width:{meter * 100:.1f}%"></div></div></div>
  <dl>{"".join(f"<div><dt>{k}</dt><dd>{v}</dd></div>" for k, v in facts)}</dl>
  {f"<ul class='firing'>{firing}</ul>" if firing else ""}
  <h3>Budget remaining, day by day</h3>
  {_chart(points, f"c{i}")}
</section>""")
        rows = "".join(f"<tr><td>{p['day']}</td><td class='num'>{p['total']:,}</td><td class='num'>{p['bad']:,}</td>"
                       f"<td class='num'>{p['remaining']:.1f}%</td></tr>" for p in points)
        tables.append(f"<details><summary>{html.escape(t.name)}: daily data</summary><table><thead><tr><th>Day</th>"
                      f"<th class='num'>Probes</th><th class='num'>Bad</th><th class='num'>Budget left</th></tr></thead>"
                      f"<tbody>{rows}</tbody></table></details>")

    alert_rows = "".join(
        f"<tr><td>{_fmt_ts(a['opened_ts'])}</td><td>{html.escape(a['target'])}</td><td>{html.escape(a['rule'])}</td>"
        f"<td>{a['severity']}</td><td class='num'>{_burn(a['burn_long'])}</td><td>{_duration(a['opened_ts'], a['closed_ts'], now)}</td></tr>"
        for a in store.alerts(con, since)) or "<tr><td colspan='6' class='muted'>No alerts in the window.</td></tr>"

    return TEMPLATE.format(
        generated=_fmt_ts(now), window=window_days, cards="".join(cards), alerts=alert_rows,
        tables="".join(tables), rules="".join(
            f"<tr><td>{r.name}</td><td>{r.severity}</td><td>{r.long_s // 3600 if r.long_s >= 3600 else r.long_s // 60}"
            f"{' h' if r.long_s >= 3600 else ' min'} and {r.short_s // 3600 if r.short_s >= 3600 else r.short_s // 60}"
            f"{' h' if r.short_s >= 3600 else ' min'}</td><td class='num'>{r.burn}×</td><td>{r.meaning}</td></tr>"
            for r in slo.RULES))


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Edge Health & SLO Report</title>
<style>
:root {{ --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781; --grid:#e1e0d9;
  --border:rgba(11,11,11,.10); --line:#2a78d6; --track:#dce9f8;
  --good:#0ca30c; --warning:#fab219; --serious:#ec835a; --critical:#d03b3b; }}
@media (prefers-color-scheme: dark) {{ :root {{ --page:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7;
  --muted:#8f8e86; --grid:#2c2c2a; --border:rgba(255,255,255,.10); --line:#3987e5; --track:#1d3047; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--page); color:var(--ink); font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:1200px; margin:0 auto; padding:28px 16px 48px; }}
h1 {{ font-size:22px; margin:0 0 4px; }} h2 {{ font-size:16px; margin:0; }} h3 {{ font-size:13px; color:var(--ink2); font-weight:500; margin:16px 0 4px; }}
.muted {{ color:var(--muted); font-size:13px; }}
.grid2 {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(340px,1fr)); gap:16px; margin-top:20px; }}
.card {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:16px; min-width:0; }}
.card header {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }}
.badge {{ display:inline-flex; gap:6px; align-items:center; font-size:12px; white-space:nowrap; color:var(--ink); }}
.badge i {{ font-style:normal; width:18px; height:18px; border-radius:50%; display:inline-flex; align-items:center;
  justify-content:center; font-size:11px; font-weight:700; color:#fff; background:var(--muted); }}
.badge.good i {{ background:var(--good); }} .badge.warning i {{ background:var(--warning); color:#0b0b0b; }}
.badge.serious i {{ background:var(--serious); color:#0b0b0b; }} .badge.critical i {{ background:var(--critical); }}
.budget {{ margin:14px 0 8px; }} .budget .row {{ display:flex; justify-content:space-between; color:var(--ink2); font-size:13px; }}
.budget b {{ color:var(--ink); font-size:20px; font-weight:600; }}
.meter {{ height:8px; border-radius:4px; background:var(--track); overflow:hidden; margin-top:4px; }}
.meter div {{ height:100%; border-radius:4px; background:var(--good); }}
.meter.warning div {{ background:var(--warning); }} .meter.serious div {{ background:var(--serious); }} .meter.critical div {{ background:var(--critical); }}
dl {{ display:grid; grid-template-columns:1fr 1fr; gap:6px 16px; margin:12px 0 0; }}
dt {{ color:var(--ink2); font-size:12px; }} dd {{ margin:0; font-variant-numeric:tabular-nums; }}
.firing {{ margin:12px 0 0; padding:8px 12px 8px 28px; border-radius:8px; background:color-mix(in srgb, var(--critical) 10%, var(--surface)); }}
svg.chart {{ width:100%; height:auto; display:block; overflow:visible; }}
.grid {{ stroke:var(--grid); stroke-width:1; }} .axis {{ fill:var(--muted); font-size:11px; }}
.line {{ fill:none; stroke:var(--line); stroke-width:2; stroke-linejoin:round; stroke-linecap:round; }}
.area {{ fill:var(--line); opacity:.10; }} .end {{ fill:var(--line); stroke:var(--surface); stroke-width:2; }}
.cross {{ stroke:var(--muted); stroke-width:1; }}
.tip {{ position:fixed; pointer-events:none; background:var(--surface); color:var(--ink); border:1px solid var(--border);
  border-radius:8px; padding:6px 10px; font-size:12px; box-shadow:0 4px 16px rgba(0,0,0,.12); display:none; z-index:10; }}
table {{ width:100%; border-collapse:collapse; }} th, td {{ text-align:left; padding:7px 6px; border-bottom:1px solid var(--grid); white-space:nowrap; }}
th {{ color:var(--ink2); font-weight:500; font-size:13px; }} .num {{ text-align:right; font-variant-numeric:tabular-nums; }}
.wide {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:4px 16px 8px; margin-top:16px; overflow-x:auto; }}
details {{ margin:8px 0; }} summary {{ cursor:pointer; color:var(--ink2); }}
h2.section {{ margin:32px 0 0; }}
</style></head>
<body><main>
<h1>Edge Health &amp; SLO Report</h1>
<p class="muted">Generated {generated} · SLO window {window} days · fictional demo data unless you point it at your own targets</p>
<div class="grid2">{cards}</div>
<h2 class="section">Alerts in the window</h2>
<div class="wide"><table><thead><tr><th>Opened</th><th>Target</th><th>Rule</th><th>Severity</th><th class="num">Peak burn</th><th>Duration</th></tr></thead>
<tbody>{alerts}</tbody></table></div>
<h2 class="section">Alert rules</h2>
<div class="wide"><table><thead><tr><th>Rule</th><th>Severity</th><th>Windows (long and short)</th><th class="num">Burn rate</th><th>Meaning</th></tr></thead>
<tbody>{rules}</tbody></table></div>
<h2 class="section">Data tables</h2>
<div class="wide">{tables}</div>
</main>
<div class="tip" id="tip"></div>
<script>
const tip = document.getElementById('tip');
document.querySelectorAll('svg.chart').forEach(svg => {{
  const pts = JSON.parse(svg.dataset.points); const [l, r] = svg.dataset.geom.split(',').map(Number);
  const cross = svg.querySelector('.cross');
  svg.addEventListener('mousemove', e => {{
    const box = svg.getBoundingClientRect(); const vx = (e.clientX - box.left) / box.width * svg.viewBox.baseVal.width;
    const i = Math.max(0, Math.min(pts.length - 1, Math.round((vx - l) / (r - l) * (pts.length - 1))));
    const x = l + (r - l) * i / Math.max(1, pts.length - 1);
    cross.setAttribute('x1', x); cross.setAttribute('x2', x); cross.setAttribute('visibility', 'visible');
    const p = pts[i];
    tip.innerHTML = `<b>${{p.day}}</b><br>Budget left ${{p.remaining.toFixed(1)}}%<br>${{p.bad.toLocaleString()}} bad of ${{p.total.toLocaleString()}} probes`;
    tip.style.display = 'block'; tip.style.left = Math.min(e.clientX + 14, innerWidth - 220) + 'px'; tip.style.top = (e.clientY + 14) + 'px';
  }});
  svg.addEventListener('mouseleave', () => {{ tip.style.display = 'none'; cross.setAttribute('visibility', 'hidden'); }});
}});
</script>
</body></html>
"""
