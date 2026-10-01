"""SQLite storage for probe results and alert state."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Iterable

from .config import Target
from .probes import ProbeResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS probes (
    ts          REAL    NOT NULL,
    target      TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    ok          INTEGER NOT NULL,
    good        INTEGER NOT NULL,   -- counts for the SLI (ok and within the latency objective)
    latency_ms  REAL,
    detail      TEXT,
    extra       TEXT
);
CREATE INDEX IF NOT EXISTS probes_target_ts ON probes (target, ts);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY,
    target      TEXT NOT NULL,
    rule        TEXT NOT NULL,      -- e.g. page-fast
    severity    TEXT NOT NULL,      -- page | ticket
    opened_ts   REAL NOT NULL,
    closed_ts   REAL,
    burn_long   REAL,
    burn_short  REAL
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def save(con: sqlite3.Connection, results: Iterable[tuple[ProbeResult, Target]]) -> int:
    rows = [(r.ts, r.target, r.kind, int(r.ok), int(r.is_good(t)), r.latency_ms, r.detail,
             json.dumps(r.extra) if r.extra else None) for r, t in results]
    with con:
        con.executemany("INSERT INTO probes VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def counts(con: sqlite3.Connection, target: str, since: float, until: float) -> tuple[int, int]:
    """(total, bad) probes for a target in [since, until)."""
    row = con.execute(
        "SELECT COUNT(*) AS total, COALESCE(SUM(1 - good), 0) AS bad FROM probes "
        "WHERE target = ? AND ts >= ? AND ts < ?", (target, since, until)).fetchone()
    return row["total"], row["bad"]


def daily(con: sqlite3.Connection, target: str, since: float) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT date(ts, 'unixepoch') AS day, COUNT(*) AS total, SUM(1 - good) AS bad, "
        "AVG(latency_ms) AS avg_latency FROM probes WHERE target = ? AND ts >= ? "
        "GROUP BY day ORDER BY day", (target, since)).fetchall()


def latest(con: sqlite3.Connection, target: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM probes WHERE target = ? ORDER BY ts DESC LIMIT 1",
                       (target,)).fetchone()


def latency_percentile(con: sqlite3.Connection, target: str, since: float, pct: float) -> float | None:
    values = [r[0] for r in con.execute(
        "SELECT latency_ms FROM probes WHERE target = ? AND ts >= ? AND ok = 1 AND latency_ms IS NOT NULL "
        "ORDER BY latency_ms", (target, since))]
    if not values:
        return None
    index = min(len(values) - 1, max(0, round(pct / 100 * len(values)) - 1))
    return values[index]


def open_alert(con: sqlite3.Connection, target: str, rule: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM alerts WHERE target = ? AND rule = ? AND closed_ts IS NULL",
                       (target, rule)).fetchone()


def alerts(con: sqlite3.Connection, since: float) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM alerts WHERE opened_ts >= ? ORDER BY opened_ts DESC",
                       (since,)).fetchall()


def prune(con: sqlite3.Connection, before: float) -> int:
    with closing(con.cursor()) as cur, con:
        cur.execute("DELETE FROM probes WHERE ts < ?", (before,))
        return cur.rowcount
