"""Load and validate the targets file (TOML)."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

KINDS = {"dns", "tls", "http", "tcp"}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


@dataclass(frozen=True)
class Target:
    name: str
    kind: str
    host: str = ""
    port: int = 0
    url: str = ""
    expect_status: int = 200
    timeout_s: float = 5.0
    slo: float = 99.9                 # percent of good probes over the SLO window
    latency_ms: int | None = None     # a probe slower than this counts as bad
    tls_critical_days: int = 7        # TLS probe fails when the certificate expires sooner
    tls_warn_days: int = 21           # reported as a warning, does not burn budget
    tags: dict = field(default_factory=dict)

    @property
    def error_budget(self) -> float:
        """Fraction of probes allowed to be bad, e.g. 0.001 for 99.9 %."""
        return 1 - self.slo / 100


@dataclass(frozen=True)
class Settings:
    database: str = "edge-probes.db"
    slo_window_days: int = 30
    interval_s: int = 60
    elasticsearch_url: str = ""
    elasticsearch_index: str = "edge-probes"
    webhook_url: str = ""


def _target(raw: dict) -> Target:
    name, kind = raw.get("name", ""), raw.get("kind", "")
    if not NAME_RE.match(name):
        raise ValueError(f"invalid target name {name!r}: use lowercase letters, digits and '-'")
    if kind not in KINDS:
        raise ValueError(f"{name}: kind must be one of {sorted(KINDS)}")
    if kind == "http" and not raw.get("url", "").startswith(("http://", "https://")):
        raise ValueError(f"{name}: http targets need a url starting with http:// or https://")
    if kind in {"dns", "tls", "tcp"} and not raw.get("host"):
        raise ValueError(f"{name}: {kind} targets need a host")
    if kind == "tcp" and not raw.get("port"):
        raise ValueError(f"{name}: tcp targets need a port")
    slo = float(raw.get("slo", 99.9))
    if not 50 <= slo < 100:
        raise ValueError(f"{name}: slo must be between 50 and 100 (exclusive), got {slo}")
    known = set(Target.__dataclass_fields__)
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"{name}: unknown keys {sorted(unknown)}")
    if kind == "tls" and not raw.get("port"):
        raw = {**raw, "port": 443}
    return Target(**{**raw, "slo": slo})


def load(path: str | Path) -> tuple[Settings, list[Target]]:
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    settings = Settings(**data.get("settings", {}))
    targets = [_target(dict(t)) for t in data.get("targets", [])]
    names = [t.name for t in targets]
    if len(names) != len(set(names)):
        raise ValueError("target names must be unique")
    if not targets:
        raise ValueError("no targets defined")
    return settings, targets
