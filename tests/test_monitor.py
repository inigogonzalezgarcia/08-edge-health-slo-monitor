"""Run with `python -m pytest` or `python -m unittest discover tests`."""
import hashlib
import hmac
import http.server
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from slo_monitor import config, exporters, probes, report, simulate, slo, store  # noqa: E402
from slo_monitor.config import Target  # noqa: E402
from slo_monitor.probes import ProbeResult  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/health" else 503)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_):
        pass


class ProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_http_ok_and_wrong_status(self):
        good = probes.probe_http(Target(name="h", kind="http", url=f"http://127.0.0.1:{self.port}/health"))
        bad = probes.probe_http(Target(name="h", kind="http", url=f"http://127.0.0.1:{self.port}/broken"))
        self.assertTrue(good.ok)
        self.assertEqual(good.extra["status"], 200)
        self.assertFalse(bad.ok)
        self.assertEqual(bad.detail, "HTTP 503")

    def test_tcp_open_and_closed_port(self):
        self.assertTrue(probes.probe_tcp(Target(name="t", kind="tcp", host="127.0.0.1", port=self.port)).ok)
        with socket.socket() as s:            # find a port nobody listens on
            s.bind(("127.0.0.1", 0))
            closed = s.getsockname()[1]
        result = probes.probe_tcp(Target(name="t", kind="tcp", host="127.0.0.1", port=closed, timeout_s=1))
        self.assertFalse(result.ok)

    def test_dns_localhost_and_invalid_name(self):
        self.assertTrue(probes.probe_dns(Target(name="d", kind="dns", host="localhost")).ok)
        self.assertFalse(probes.probe_dns(Target(name="d", kind="dns", host="does-not-exist.invalid")).ok)

    def test_certificate_days_left(self):
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.assertEqual(probes.days_until("Oct 31 00:00:00 2026 GMT", now), 30.0)

    def test_latency_objective_makes_a_successful_probe_bad(self):
        t = Target(name="h", kind="http", url="https://x.example", latency_ms=100)
        self.assertTrue(ProbeResult("h", "http", 0, True, 80).is_good(t))
        self.assertFalse(ProbeResult("h", "http", 0, True, 250).is_good(t))
        self.assertFalse(ProbeResult("h", "http", 0, False, 10).is_good(t))


class ConfigTests(unittest.TestCase):
    def test_example_file_is_valid(self):
        settings, targets = config.load(ROOT / "targets.example.toml")
        self.assertGreaterEqual(len(targets), 4)
        self.assertEqual(settings.slo_window_days, 30)

    def test_rejects_bad_targets(self):
        cases = [
            '[[targets]]\nname = "Bad Name"\nkind = "dns"\nhost = "a"\n',
            '[[targets]]\nname = "x"\nkind = "ping"\nhost = "a"\n',
            '[[targets]]\nname = "x"\nkind = "tcp"\nhost = "a"\n',
            '[[targets]]\nname = "x"\nkind = "http"\nurl = "ftp://a"\n',
            '[[targets]]\nname = "x"\nkind = "dns"\nhost = "a"\nslo = 100\n',
            '[[targets]]\nname = "x"\nkind = "dns"\nhost = "a"\npassword = "nope"\n',
        ]
        for text in cases:
            with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
                f.write(text)
            with self.subTest(text=text), self.assertRaises(ValueError):
                config.load(f.name)


class SloTests(unittest.TestCase):
    def setUp(self):
        self.con = store.connect(":memory:")
        self.t = Target(name="api", kind="http", url="https://api.example.com", slo=99.9)
        self.now = 1_000_000_000.0

    def _fill(self, minutes, bad_last=0):
        rows = []
        for i in range(minutes):
            ok = i < minutes - bad_last                  # the last `bad_last` probes fail
            rows.append((ProbeResult("api", "http", self.now - (minutes - i) * 60, ok, 100), self.t))
        store.save(self.con, rows)

    def test_error_budget_and_sli(self):
        self._fill(10_000, bad_last=5)                   # 0.05 % bad against a 0.1 % budget
        s = slo.evaluate(self.con, self.t, self.now, 30)
        self.assertAlmostEqual(s.sli, 99.95, places=3)
        self.assertAlmostEqual(s.budget_remaining, 0.5, places=3)

    def test_burn_rate(self):
        self._fill(60, bad_last=6)                       # 10 % bad in the last hour
        self.assertAlmostEqual(slo.burn_rate(self.con, self.t, self.now, 3600), 100.0, places=3)

    def test_alert_opens_and_resolves(self):
        self._fill(120, bad_last=10)
        events = slo.update_alerts(self.con, self.t, self.now)
        self.assertIn(("alert.opened", "page-fast"), {(e["event"], e["rule"]) for e in events})
        later = self.now + 10 * 60                        # ten good minutes: the short window clears
        store.save(self.con, [(ProbeResult("api", "http", self.now + i * 60, True, 100), self.t) for i in range(10)])
        events = slo.update_alerts(self.con, self.t, later)
        self.assertIn(("alert.resolved", "page-fast"), {(e["event"], e["rule"]) for e in events})

    def test_a_single_failed_probe_does_not_page(self):
        self._fill(120, bad_last=1)
        self.assertEqual(slo.update_alerts(self.con, self.t, self.now), [])


class OutputTests(unittest.TestCase):
    def test_ecs_document_and_bulk_body(self):
        t = Target(name="api", kind="http", url="https://api.example.com/health", tags={"tier": "edge"})
        r = ProbeResult("api", "http", 1_700_000_000, False, 1234.5, "HTTP 503", {"status": 503})
        doc = exporters.to_ecs(r, t)
        self.assertEqual(doc["event"]["outcome"], "failure")
        self.assertEqual(doc["event"]["duration"], 1_234_500_000)
        self.assertEqual(doc["http"]["response"]["status_code"], 503)
        lines = exporters.bulk_body("edge-probes", [doc]).decode().splitlines()
        self.assertEqual(json.loads(lines[0]), {"create": {"_index": "edge-probes"}})

    def test_webhook_signature(self):
        expected = "sha256=" + hmac.new(b"s3cret", b"{}", hashlib.sha256).hexdigest()
        self.assertEqual(exporters.sign(b"{}", "s3cret"), expected)
        self.assertTrue(exporters.sign(b"{}", "s3cret").startswith("sha256="))
        self.assertNotEqual(exporters.sign(b"{}", "a"), exporters.sign(b"{}", "b"))

    def test_demo_and_report(self):
        con = store.connect(":memory:")
        now = time.time()
        simulate.generate(con, now, days=30)
        statuses = {t.name: slo.evaluate(con, t, now, 30) for t in simulate.DEMO_TARGETS}
        self.assertEqual(statuses["public-api-dns"].level, "critical")    # the DNS outage broke its SLO
        self.assertEqual(statuses["portal-tls"].level, "good")
        opened = {(a["target"], a["rule"]) for a in store.alerts(con, now - 30 * 86400)}
        self.assertIn(("public-api-dns", "page-fast"), opened)
        page = report.render(con, simulate.DEMO_TARGETS, now, 30)
        self.assertIn("Renew soon", page)
        self.assertIn("orders-db-private", page)
        latest = {t.name: store.latest(con, t.name) for t in simulate.DEMO_TARGETS}
        metrics = exporters.prometheus(list(statuses.values()), latest)
        self.assertIn('edge_slo_burn_rate{target="public-api",window="1h"}', metrics)


if __name__ == "__main__":
    unittest.main()
