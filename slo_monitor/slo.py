"""SLIs, error budgets and multi-window, multi-burn-rate alerting.

Burn rate = observed error rate / error rate allowed by the SLO.
A burn rate of 1 spends exactly the whole budget over the SLO window;
14.4 spends 2 % of a 30-day budget in one hour.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import store
from .config import Target

HOUR = 3600


@dataclass(frozen=True)
class Rule:
    name: str
    severity: str        # page | ticket
    long_s: int
    short_s: int
    burn: float
    min_bad: int         # minimum bad probes in the long window before the rule can fire
    meaning: str


# Thresholds from the multi-window, multi-burn-rate approach in Google's SRE Workbook
# ("Alerting on SLOs"), tuned for a 30-day window. The short window makes an alert
# stop firing soon after the problem is fixed instead of waiting for the long window to drain.
#
# min_bad: with one probe per minute, a single failed probe in the last hour already means a
# burn rate of ~17 for a 99.9 % SLO. Requiring a few bad probes stops one lost packet from
# paging someone at night (see docs/decisions.md).
RULES = (
    Rule("page-fast", "page", 1 * HOUR, 5 * 60, 14.4, 3, "2% of the monthly budget spent in 1 hour"),
    Rule("page-slow", "page", 6 * HOUR, 30 * 60, 6.0, 5, "5% of the monthly budget spent in 6 hours"),
    Rule("ticket", "ticket", 72 * HOUR, 6 * HOUR, 1.0, 10, "10% of the monthly budget spent in 3 days"),
)


def burn_rate(con: sqlite3.Connection, t: Target, now: float, seconds: int) -> float | None:
    total, bad = store.counts(con, t.name, now - seconds, now + 1)
    if total == 0:
        return None
    return (bad / total) / t.error_budget


def fires(long_burn, short_burn, long_bad: int, rule: Rule) -> bool:
    """The rule fires when both windows burn faster than the threshold and enough probes failed."""
    return (long_burn is not None and short_burn is not None and long_bad >= rule.min_bad
            and long_burn >= rule.burn and short_burn >= rule.burn)


@dataclass
class Status:
    target: Target
    total: int
    bad: int
    sli: float | None              # percent good over the window
    budget_remaining: float | None # 1.0 = untouched, 0 = spent, negative = SLO missed
    burn: dict                     # window label -> burn rate
    firing: list                   # rules currently firing

    @property
    def level(self) -> str:
        """good / warning / serious / critical, used for the status badge."""
        if self.budget_remaining is None:
            return "unknown"
        if self.budget_remaining < 0:
            return "critical"
        if self.firing or self.budget_remaining < 0.25:
            return "serious"
        if self.budget_remaining < 0.5:
            return "warning"
        return "good"


def evaluate(con: sqlite3.Connection, t: Target, now: float, window_days: int) -> Status:
    total, bad = store.counts(con, t.name, now - window_days * 86400, now + 1)
    sli = remaining = None
    if total:
        error_rate = bad / total
        sli = 100 * (1 - error_rate)
        remaining = 1 - error_rate / t.error_budget
    burn = {label: burn_rate(con, t, now, seconds)
            for label, seconds in (("5m", 300), ("1h", HOUR), ("6h", 6 * HOUR), ("3d", 72 * HOUR))}
    firing = [rule for rule in RULES if _fires(con, t, now, rule)]
    return Status(t, total, bad, sli, remaining, burn, firing)


def _fires(con: sqlite3.Connection, t: Target, now: float, rule: Rule) -> bool:
    _, long_bad = store.counts(con, t.name, now - rule.long_s, now + 1)
    return fires(burn_rate(con, t, now, rule.long_s), burn_rate(con, t, now, rule.short_s), long_bad, rule)


def update_alerts(con: sqlite3.Connection, t: Target, now: float) -> list[dict]:
    """Open or close alerts for one target. Returns the transitions as events."""
    events = []
    for rule in RULES:
        long_burn = burn_rate(con, t, now, rule.long_s)
        short_burn = burn_rate(con, t, now, rule.short_s)
        _, long_bad = store.counts(con, t.name, now - rule.long_s, now + 1)
        firing = fires(long_burn, short_burn, long_bad, rule)
        current = store.open_alert(con, t.name, rule.name)
        if firing and current is None:
            with con:
                con.execute("INSERT INTO alerts (target, rule, severity, opened_ts, burn_long, burn_short) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (t.name, rule.name, rule.severity, now, long_burn, short_burn))
            events.append(_event("alert.opened", t, rule, now, long_burn, short_burn))
        elif not firing and current is not None:
            with con:
                con.execute("UPDATE alerts SET closed_ts = ? WHERE id = ?", (now, current["id"]))
            events.append(_event("alert.resolved", t, rule, now, long_burn, short_burn))
    return events


def _event(kind: str, t: Target, rule: Rule, now: float, long_burn, short_burn) -> dict:
    return {"event": kind, "target": t.name, "rule": rule.name, "severity": rule.severity,
            "slo": t.slo, "ts": now, "burn_long": _round(long_burn), "burn_short": _round(short_burn),
            "meaning": rule.meaning}


def _round(value):
    return None if value is None else round(value, 2)
