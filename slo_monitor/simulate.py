"""Fictional 30-day history for the demo, with three incidents built in.

All names are fictional (example.com / example.internal). The incidents match the
sample postmortem in docs/postmortems/, so the report and the review tell one story.
"""
from __future__ import annotations

import random
from bisect import bisect_left
import sqlite3
from itertools import accumulate

from . import slo, store
from .config import Target
from .probes import ProbeResult

STEP = 60  # one probe per target per minute

DEMO_TARGETS = [
    Target(name="public-api", kind="http", url="https://api.example.com/health", slo=99.9, latency_ms=800,
           tags={"tier": "edge", "owner": "platform"}),
    Target(name="public-api-dns", kind="dns", host="api.example.com", slo=99.95, latency_ms=150,
           tags={"tier": "edge", "owner": "platform"}),
    Target(name="portal-tls", kind="tls", host="portal.example.com", port=443, slo=99.9,
           tags={"tier": "edge", "owner": "platform"}),
    Target(name="orders-db-private", kind="tcp", host="orders-db.example.internal", port=5432, slo=99.5,
           latency_ms=40, tags={"tier": "private-link", "owner": "data"}),
]

# Incidents, in minutes before "now": (target, start, duration, failure probability, kind of failure)
INCIDENTS = [
    # 9 days ago: a DNS change removes the record for 38 minutes; the API is unreachable by name too.
    ("public-api-dns", 9 * 1440 + 600, 38, 1.0, "resolution failed: [Errno -2] Name or service not known"),
    ("public-api", 9 * 1440 + 600, 38, 1.0, "request failed: <urlopen error [Errno -2] Name or service not known>"),
    # 3 days ago: the private endpoint gets slow for 4 hours (connects succeed but miss the latency objective).
    ("orders-db-private", 3 * 1440 + 300, 240, 0.60, "slow"),
    # Right now: about 3 % of API requests return 503.
    ("public-api", 70, 70, 0.03, "HTTP 503"),
]


def _normal_latency(rng: random.Random, t: Target) -> float:
    base = {"http": 180, "dns": 18, "tls": 95, "tcp": 6}[t.kind]
    value = rng.lognormvariate(0, 0.35) * base
    if rng.random() < 0.0003:             # rare slow outlier
        value *= rng.uniform(3, 6)
    return round(value, 1)


def generate(con: sqlite3.Connection, now: float, days: int = 30, seed: int = 7) -> int:
    rng = random.Random(seed)
    minutes = days * 1440
    start = now - minutes * STEP
    total = 0
    for t in DEMO_TARGETS:
        rows = []
        for i in range(minutes):
            ts = start + i * STEP + rng.uniform(0, 5)
            ago = minutes - i                         # minutes before now
            ok, latency, detail, extra = True, _normal_latency(rng, t), "", {}
            for name, inc_start, duration, probability, failure in INCIDENTS:
                if name == t.name and inc_start - duration < ago <= inc_start and rng.random() < probability:
                    if failure == "slow":
                        latency = round(rng.uniform(60, 400), 1)
                    elif failure.startswith("HTTP"):
                        ok, detail, extra = False, failure, {"status": 503}
                    else:
                        ok, latency, detail = False, round(rng.uniform(4000, 5000), 1), failure
            if t.kind == "tls":
                # The certificate has 44 days left at the start of the window and 14 now: renewal is due.
                days_left = round(14 + (now - ts) / 86400, 1)
                extra = {"days_left": days_left, "tls_version": "TLSv1.3", "issuer": "Example CA"}
                detail = detail or f"TLSv1.3, {days_left} days left" + (" (renew soon)" if days_left < 21 else "")
            elif ok and not detail:
                detail = {"http": "HTTP 200", "dns": "2 address(es)", "tcp": f"connected to port {t.port}"}[t.kind]
                if t.kind == "http":
                    extra = {"status": 200}
            rows.append((ProbeResult(t.name, t.kind, ts, ok, latency, detail, extra), t))
        total += store.save(con, rows)
    replay_alerts(con, now, days)
    return total


def replay_alerts(con: sqlite3.Connection, now: float, days: int, every_s: int = 300) -> None:
    """Rebuild alert history by evaluating the burn-rate rules every 5 minutes over the past.

    Uses per-minute prefix sums so 30 days replay in seconds; the live path (slo.update_alerts)
    runs the same rules against SQLite.
    """
    start = now - days * 86400
    for t in DEMO_TARGETS:
        rows = con.execute("SELECT ts, good FROM probes WHERE target = ? ORDER BY ts", (t.name,)).fetchall()
        times = [r["ts"] for r in rows]
        bad_prefix = [0, *accumulate(1 - r["good"] for r in rows)]

        def window(end: float, seconds: int) -> tuple[float | None, int]:
            lo = bisect_left(times, end - seconds)
            hi = bisect_left(times, end + 1)
            total, bad = hi - lo, bad_prefix[hi] - bad_prefix[lo]
            return (None if total == 0 else (bad / total) / t.error_budget), bad

        open_ids: dict[str, int] = {}
        moment = start + 6 * 3600
        while moment <= now:
            for rule in slo.RULES:
                (long_burn, long_bad), (short_burn, _) = window(moment, rule.long_s), window(moment, rule.short_s)
                firing = slo.fires(long_burn, short_burn, long_bad, rule)
                if firing and rule.name not in open_ids:
                    cur = con.execute(
                        "INSERT INTO alerts (target, rule, severity, opened_ts, burn_long, burn_short) "
                        "VALUES (?, ?, ?, ?, ?, ?)", (t.name, rule.name, rule.severity, moment, long_burn, short_burn))
                    open_ids[rule.name] = cur.lastrowid
                elif not firing and rule.name in open_ids:
                    con.execute("UPDATE alerts SET closed_ts = ? WHERE id = ?", (moment, open_ids.pop(rule.name)))
            moment += every_s
        con.commit()

