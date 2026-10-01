"""The four probes. Each returns a ProbeResult and never raises for network errors."""
from __future__ import annotations

import concurrent.futures
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import Target

USER_AGENT = "edge-health-slo-monitor/0.1"
_DNS_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="dns")


@dataclass
class ProbeResult:
    target: str
    kind: str
    ts: float                      # unix time of the probe
    ok: bool                       # the probe itself succeeded
    latency_ms: float | None
    detail: str = ""
    extra: dict = field(default_factory=dict)

    def is_good(self, target: Target) -> bool:
        """A good event for the SLI: success and, if a latency objective is set, fast enough."""
        if not self.ok:
            return False
        if target.latency_ms is not None and self.latency_ms is not None:
            return self.latency_ms <= target.latency_ms
        return True


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


def probe_dns(t: Target) -> ProbeResult:
    """Resolve the host with the system resolver (what applications actually use)."""
    start, now = time.perf_counter(), time.time()
    # getaddrinfo has no timeout of its own, so it runs in a worker thread we can stop waiting for.
    future = _DNS_POOL.submit(socket.getaddrinfo, t.host, None, proto=socket.IPPROTO_TCP)
    try:
        infos = future.result(timeout=t.timeout_s)
    except concurrent.futures.TimeoutError:
        return ProbeResult(t.name, "dns", now, False, _elapsed_ms(start), f"resolution timed out after {t.timeout_s}s")
    except OSError as exc:
        return ProbeResult(t.name, "dns", now, False, _elapsed_ms(start), f"resolution failed: {exc}")
    addresses = sorted({info[4][0] for info in infos})
    return ProbeResult(t.name, "dns", now, True, _elapsed_ms(start),
                       f"{len(addresses)} address(es)", {"addresses": addresses})


def days_until(not_after: str, now: datetime | None = None) -> float:
    """Days from now to a certificate's notAfter value (format used by ssl.getpeercert)."""
    expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(not_after), tz=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return round((expires - now).total_seconds() / 86400, 1)


def probe_tls(t: Target) -> ProbeResult:
    """Full TLS handshake with certificate and hostname verification, then check expiry."""
    start, now = time.perf_counter(), time.time()
    context = ssl.create_default_context()
    try:
        with socket.create_connection((t.host, t.port or 443), timeout=t.timeout_s) as sock:
            with context.wrap_socket(sock, server_hostname=t.host) as tls:
                cert = tls.getpeercert()
                version = tls.version()
    except (OSError, ssl.SSLError) as exc:
        return ProbeResult(t.name, "tls", now, False, _elapsed_ms(start), f"handshake failed: {exc}")
    latency = _elapsed_ms(start)
    left = days_until(cert["notAfter"])
    issuer = dict(item[0] for item in cert.get("issuer", ())).get("organizationName", "")
    extra = {"days_left": left, "tls_version": version, "issuer": issuer}
    if left < t.tls_critical_days:
        return ProbeResult(t.name, "tls", now, False, latency, f"certificate expires in {left} days", extra)
    note = " (renew soon)" if left < t.tls_warn_days else ""
    return ProbeResult(t.name, "tls", now, True, latency, f"{version}, {left} days left{note}", extra)


def probe_http(t: Target) -> ProbeResult:
    """GET the URL and compare the status code with the expected one."""
    start, now = time.perf_counter(), time.time()
    request = urllib.request.Request(t.url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=t.timeout_s) as response:
            status = response.status
            response.read(64 * 1024)          # read part of the body so the timing is realistic
    except urllib.error.HTTPError as exc:     # 4xx/5xx still give us a status code
        status = exc.code
    except (OSError, ValueError) as exc:
        return ProbeResult(t.name, "http", now, False, _elapsed_ms(start), f"request failed: {exc}")
    latency = _elapsed_ms(start)
    ok = status == t.expect_status
    return ProbeResult(t.name, "http", now, ok, latency, f"HTTP {status}", {"status": status})


def probe_tcp(t: Target) -> ProbeResult:
    """Open a TCP connection: the simplest check that a private endpoint is reachable."""
    start, now = time.perf_counter(), time.time()
    try:
        with socket.create_connection((t.host, t.port), timeout=t.timeout_s):
            pass
    except OSError as exc:
        return ProbeResult(t.name, "tcp", now, False, _elapsed_ms(start), f"connect failed: {exc}")
    return ProbeResult(t.name, "tcp", now, True, _elapsed_ms(start), f"connected to port {t.port}")


PROBES = {"dns": probe_dns, "tls": probe_tls, "http": probe_http, "tcp": probe_tcp}


def run(t: Target) -> ProbeResult:
    return PROBES[t.kind](t)
