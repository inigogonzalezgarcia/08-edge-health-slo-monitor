"""Command line: demo, probe, run, report, metrics."""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config, exporters, probes, report, simulate, slo, store


def _probe_all(targets):
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(targets))) as pool:
        return list(zip(pool.map(probes.run, targets), targets))


def cmd_probe(args) -> int:
    settings, targets = config.load(args.config)
    con = store.connect(settings.database)
    results = _probe_all(targets)
    store.save(con, results)
    now = time.time()
    for r, t in results:
        mark = "ok " if r.is_good(t) else "BAD"
        latency = "" if r.latency_ms is None else f"{r.latency_ms:>8.1f} ms"
        print(f"[{mark}] {t.name:<24} {t.kind:<4} {latency}  {r.detail}")
    events = [e for t in targets for e in slo.update_alerts(con, t, now)]
    for event in events:
        print(json.dumps(event))
        if settings.webhook_url:
            try:
                exporters.send_webhook(settings.webhook_url, event)
            except OSError as exc:
                print(f"webhook failed: {exc}", file=sys.stderr)
    if settings.elasticsearch_url:
        try:
            exporters.send_to_elasticsearch(settings.elasticsearch_url, settings.elasticsearch_index,
                                            [exporters.to_ecs(r, t) for r, t in results])
        except (OSError, RuntimeError) as exc:
            # Losing a shipment must not stop probing: the data is still in SQLite.
            print(f"elasticsearch export failed: {exc}", file=sys.stderr)
    return 0


def cmd_run(args) -> int:
    settings, _ = config.load(args.config)
    interval = args.interval or settings.interval_s
    print(f"probing every {interval}s; Ctrl+C to stop")
    while True:
        started = time.monotonic()
        cmd_probe(args)
        con = store.connect(settings.database)
        store.prune(con, time.time() - (settings.slo_window_days + 3) * 86400)
        time.sleep(max(1.0, interval - (time.monotonic() - started)))


def cmd_report(args) -> int:
    settings, targets = config.load(args.config)
    con = store.connect(settings.database)
    Path(args.out).write_text(report.render(con, targets, time.time(), settings.slo_window_days), encoding="utf-8")
    print(f"report written to {args.out}")
    return 0


def cmd_demo(args) -> int:
    db = Path(args.db)
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    con = store.connect(db)
    now = time.time()
    count = simulate.generate(con, now, days=30)
    Path(args.out).write_text(report.render(con, simulate.DEMO_TARGETS, now, 30), encoding="utf-8")
    print(f"generated {count:,} fictional probes for {len(simulate.DEMO_TARGETS)} targets")
    for t in simulate.DEMO_TARGETS:
        s = slo.evaluate(con, t, now, 30)
        print(f"  {t.name:<20} SLI {s.sli:.3f}% (SLO {t.slo}%)  budget left {s.budget_remaining * 100:6.1f}%  {s.level}")
    print(f"report written to {args.out}")
    return 0


def cmd_metrics(args) -> int:
    settings, targets = config.load(args.config)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/metrics":
                self.send_error(404)
                return
            con = store.connect(settings.database)
            now = time.time()
            statuses = [slo.evaluate(con, t, now, settings.slo_window_days) for t in targets]
            latest = {t.name: row for t in targets if (row := store.latest(con, t.name))}
            body = exporters.prometheus(statuses, latest).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"serving http://{args.bind}:{args.port}/metrics")
    server.serve_forever()
    return 0


def cmd_check(args) -> int:
    _, targets = config.load(args.config)
    for t in targets:
        print(f"{t.name:<24} {t.kind:<4} SLO {t.slo}%  budget {t.error_budget:.4%} of probes")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m slo_monitor", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="generate 30 days of fictional data and an HTML report")
    demo.add_argument("--db", default="demo.db")
    demo.add_argument("--out", default="report.html")
    demo.set_defaults(func=cmd_demo)

    for name, func, text in (("probe", cmd_probe, "run every probe once"),
                             ("run", cmd_run, "probe in a loop"),
                             ("report", cmd_report, "write the HTML report"),
                             ("metrics", cmd_metrics, "serve Prometheus metrics"),
                             ("check-config", cmd_check, "validate the targets file")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--config", default="targets.toml")
        p.set_defaults(func=func)
        if name == "run":
            p.add_argument("--interval", type=int, default=0, help="seconds between rounds")
        if name == "report":
            p.add_argument("--out", default="report.html")
        if name == "metrics":
            p.add_argument("--bind", default="127.0.0.1")
            p.add_argument("--port", type=int, default=9108)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
