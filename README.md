# Edge Health & SLO Monitor

Synthetic probes for the path every request takes before it reaches an application: **DNS resolution, TLS, HTTP and private (TCP) connectivity**. Each result counts toward a **service level objective**, the monitor tracks the **error budget**, and it alerts on **burn rate** instead of single failures. Results go to SQLite, and optionally to **Elasticsearch/Kibana** and **Prometheus**. The repository also includes an **incident review template** and a worked, fictional example.

Python 3.11+, standard library only. Runs with fictional demo data out of the box.

## Why

Most outages users notice are not "the server is down". The name stops resolving after a DNS change, a certificate expires on a Sunday, a private endpoint gets slow, a load balancer returns 503 to one request in thirty. Up/down checks either miss these or page someone for every blip.

From years of running IT operations in environments where minutes of downtime are visible immediately, the practices that made the difference were:

- **Check each layer separately**, so the first minutes of an incident are spent fixing, not guessing.
- **Agree on what "good enough" means** (an SLO) and alert when it is genuinely at risk, not on every failed probe.
- **Review every significant incident** in a blameless, structured way, and track the actions to completion.

This project puts those three things in one small tool.

## What it does

| | |
|---|---|
| Probes | `dns` (system resolver, with timeout), `tls` (handshake, chain and hostname validation, days to expiry), `http` (status code), `tcp` (private endpoint reachability). Optional latency objective per target. |
| SLOs | SLI and error budget per target over a 30-day window. |
| Alerts | Multi-window, multi-burn-rate rules (fast page, slow page, ticket), with alert state kept across restarts. |
| Outputs | Elasticsearch bulk API with ECS field names, Prometheus `/metrics`, HMAC-signed webhook for alert transitions (for example to n8n), self-contained HTML report. |
| Incidents | [Review template](docs/postmortem-template.md) and a [worked example](docs/postmortems/2026-09-22-api-dns-record-removed.md) that matches the demo data. |

## Try it

```bash
git clone https://github.com/inigogonzalezgarcia/08-edge-health-slo-monitor.git
cd 08-edge-health-slo-monitor
python -m slo_monitor demo          # 30 days of fictional probes for 4 targets
open report.html                    # Windows: start report.html
```

The demo data contains three incidents:

1. **DNS record lost in a zone migration** (9 days ago, 38 minutes): the DNS target misses its SLO and the API spends most of its monthly budget. The [incident review](docs/postmortems/2026-09-22-api-dns-record-removed.md) walks through it.
2. **Slow private endpoint** (3 days ago, 4 hours): connections succeed but miss the 40 ms latency objective, so the budget burns even though the endpoint is "up".
3. **Intermittent 503s** (right now): about 3 % of API requests fail.

The TLS target is healthy but its certificate has 14 days left, so the report flags it for renewal.

## Point it at your own endpoints

```bash
cp targets.example.toml targets.toml       # edit names, hosts, URLs and SLOs
python -m slo_monitor check-config
python -m slo_monitor probe                # one round, prints results and alert transitions
python -m slo_monitor run                  # probe every interval_s seconds
python -m slo_monitor report --out report.html
python -m slo_monitor metrics              # http://127.0.0.1:9108/metrics
```

Run the probes from where your users or applications are: a probe for a private endpoint only means something from inside the network that should reach it. Example systemd units are in [`deploy/systemd/`](deploy/systemd).

### Elasticsearch and Kibana

A local lab cluster (security off, bound to 127.0.0.1, for experiments only):

```bash
docker compose -f deploy/docker-compose.elastic.yml up -d
curl -X PUT localhost:9200/_index_template/edge-probes \
     -H 'Content-Type: application/json' -d @deploy/elasticsearch/index-template.json
```

Then set `elasticsearch_url = "http://localhost:9200"` in `targets.toml`. Against a secured cluster, put an API key in `ES_API_KEY`. In Kibana, create a data view for `edge-probes*` and filter on `edge.probe.good : false` to see every bad probe, or chart `edge.probe.latency_ms` by `service.name`.

### Alert webhook

Set `webhook_url` and `WEBHOOK_SECRET`. Each alert transition is posted once as JSON, signed in the `X-Signature-256` header (`sha256=<HMAC of the body>`):

```json
{"event": "alert.opened", "target": "public-api-dns", "rule": "page-fast", "severity": "page",
 "slo": 99.95, "burn_long": 166.7, "burn_short": 2000.0, "meaning": "2% of the monthly budget spent in 1 hour"}
```

## Alert rules

| Rule | Windows | Burn rate | Meaning |
|---|---|---|---|
| `page-fast` | 1 h and 5 min | 14.4× | 2 % of the monthly budget spent in an hour |
| `page-slow` | 6 h and 30 min | 6× | 5 % spent in six hours |
| `ticket` | 3 days and 6 h | 1× | 10 % spent in three days |

Thresholds follow the multi-window, multi-burn-rate approach in Google's *Site Reliability Workbook*. Because synthetic probes are low volume, each rule also needs a minimum number of bad probes, so one lost packet never pages anyone. Details in [ARCHITECTURE.md](ARCHITECTURE.md) and [docs/decisions.md](docs/decisions.md).

## Tests

```bash
python -m unittest discover tests      # or: pip install -r requirements-dev.txt && python -m pytest
```

The tests start a local HTTP server and use real sockets for the HTTP, TCP and DNS probes, check the SLO and burn-rate maths, the alert open/resolve cycle, the ECS documents and the demo report.

**What has not been tested yet:** shipping to a real Elasticsearch cluster (the index template and bulk format follow the documentation but have not been run against a cluster), the systemd units, and the webhook against a real n8n instance.

## Roadmap

- Authoritative-nameserver DNS probe next to the system-resolver probe.
- Retry queue for Elasticsearch shipments.
- Kibana saved objects (data view and dashboard) ready to import.
- Probes from several vantage points, aggregated per region.
- Run it inside a Kubernetes cluster against the Ingress set up in project 06.

## Customisation and contact

Want this adapted to your environment (your endpoints and SLOs, your alerting channel, Elastic or Prometheus integration, an incident review process for your team)? Get in touch:

- Email: [inigogonzalezgarcia@yahoo.es](mailto:inigogonzalezgarcia@yahoo.es)
- LinkedIn: [linkedin.com/in/igonzalez93](https://www.linkedin.com/in/igonzalez93)

## License

MIT
