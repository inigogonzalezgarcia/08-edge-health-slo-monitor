"""Outputs: Elasticsearch (bulk API), Prometheus text format and signed webhooks.

Secrets come from environment variables, never from the targets file:
  ES_API_KEY       Elasticsearch API key (base64 "id:key" as shown by Kibana)
  WEBHOOK_SECRET   shared secret used to sign webhook payloads (HMAC-SHA256)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.request
from datetime import datetime, timezone

from .config import Target
from .probes import ProbeResult


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def to_ecs(r: ProbeResult, t: Target) -> dict:
    """One probe as an Elastic Common Schema (ECS) style document."""
    doc = {
        "@timestamp": _iso(r.ts),
        "event": {"kind": "metric", "category": ["network"], "dataset": "edge.probe",
                  "outcome": "success" if r.ok else "failure",
                  "duration": int((r.latency_ms or 0) * 1_000_000)},   # ECS duration is in ns
        "service": {"name": t.name},
        "message": r.detail,
        "edge": {"probe": {"kind": r.kind, "good": r.is_good(t), "latency_ms": r.latency_ms,
                           "slo_target": t.slo}},
        "labels": {k: str(v) for k, v in t.tags.items()},
    }
    if t.url:
        doc["url"] = {"full": t.url}
    if t.host:
        doc["destination"] = {"domain": t.host, "port": t.port or None}
    if "days_left" in r.extra:
        doc["tls"] = {"server": {"not_after_days": r.extra["days_left"]}, "version": r.extra.get("tls_version")}
    if "status" in r.extra:
        doc["http"] = {"response": {"status_code": r.extra["status"]}}
    if not r.ok:
        doc["error"] = {"message": r.detail}
    return doc


def bulk_body(index: str, docs: list[dict]) -> bytes:
    lines = []
    for doc in docs:
        lines.append(json.dumps({"create": {"_index": index}}))   # "create" works for data streams too
        lines.append(json.dumps(doc))
    return ("\n".join(lines) + "\n").encode()


def send_to_elasticsearch(url: str, index: str, docs: list[dict], timeout: float = 10) -> dict:
    headers = {"Content-Type": "application/x-ndjson"}
    api_key = os.environ.get("ES_API_KEY")
    if api_key:
        headers["Authorization"] = f"ApiKey {api_key}"
    request = urllib.request.Request(url.rstrip("/") + "/_bulk", data=bulk_body(index, docs),
                                     headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read())
    if result.get("errors"):
        first = next(i for i in result["items"] if "error" in next(iter(i.values())))
        raise RuntimeError(f"Elasticsearch rejected documents: {json.dumps(first)[:300]}")
    return result


def prometheus(statuses: list, latest: dict) -> str:
    """Render current state in the Prometheus text exposition format."""
    out = [
        "# HELP edge_probe_up 1 if the last probe succeeded.",
        "# TYPE edge_probe_up gauge",
    ]
    for name, row in latest.items():
        out.append(f'edge_probe_up{{target="{name}",kind="{row["kind"]}"}} {row["ok"]}')
    out += ["# HELP edge_probe_latency_ms Latency of the last probe in milliseconds.",
            "# TYPE edge_probe_latency_ms gauge"]
    for name, row in latest.items():
        if row["latency_ms"] is not None:
            out.append(f'edge_probe_latency_ms{{target="{name}"}} {row["latency_ms"]}')
    out += ["# HELP edge_slo_error_budget_remaining Fraction of the error budget left in the SLO window.",
            "# TYPE edge_slo_error_budget_remaining gauge"]
    for s in statuses:
        if s.budget_remaining is not None:
            out.append(f'edge_slo_error_budget_remaining{{target="{s.target.name}"}} {s.budget_remaining:.4f}')
    out += ["# HELP edge_slo_burn_rate Error budget burn rate over a window.",
            "# TYPE edge_slo_burn_rate gauge"]
    for s in statuses:
        for window, value in s.burn.items():
            if value is not None:
                out.append(f'edge_slo_burn_rate{{target="{s.target.name}",window="{window}"}} {value:.3f}')
    return "\n".join(out) + "\n"


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def send_webhook(url: str, event: dict, timeout: float = 10) -> None:
    body = json.dumps(event).encode()
    headers = {"Content-Type": "application/json"}
    secret = os.environ.get("WEBHOOK_SECRET")
    if secret:
        headers["X-Signature-256"] = sign(body, secret)
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout):
        pass
