# Design decisions

Short records of the choices that are not obvious from the code.

## 1. Probe what the user's path depends on, layer by layer

**Decision.** Four separate probe kinds (DNS, TLS, HTTP, TCP) instead of one HTTP check. When an HTTP check fails, the first question in an incident is *which layer*: name resolution, the certificate, the application, or the network path. Separate probes answer that before anyone opens a terminal. In the demo data, the DNS probe and the API probe fail together, which points at DNS within minutes.

## 2. DNS through the system resolver

**Decision.** `probe_dns` uses `getaddrinfo`, the same resolver path applications use, including caches and search domains. It does not query an authoritative server directly. The question it answers is "can a client here resolve this name?", which is what the user experiences.

**Trade-off.** A cached answer can hide a broken record until the TTL expires. To check a specific nameserver, add a probe that runs `dig @server` or use a DNS library; it is on the roadmap.

`getaddrinfo` has no timeout parameter, so it runs in a worker thread and the probe stops waiting after `timeout_s`.

## 3. Latency is part of "good"

**Decision.** A target can have `latency_ms`. A probe that succeeds but is slower than that counts as bad. A private endpoint that takes 400 ms to accept a connection is broken for the application using it, even though it is "up".

## 4. Multi-window, multi-burn-rate alerts with a minimum count

**Problem.** Alerting on "SLI below target" fires too late (the budget is already gone) or too often (every small dip). A single threshold on a short window pages for noise.

**Decision.** Three rules from the multi-window, multi-burn-rate approach in Google's *Site Reliability Workbook*: fast burn pages, slow burn pages, steady burn opens a ticket. Both windows must exceed the threshold.

**Addition.** Synthetic probes are low volume: one per minute is 60 events per hour. One failed probe in the last hour is already a burn rate of about 17 against a 99.9 % SLO, enough to page. Each rule therefore also needs a minimum number of bad probes in its long window (3, 5 and 10). This keeps a single lost packet from waking someone at 3 a.m. With real user traffic (thousands of events per hour) the minimum is irrelevant.

## 5. SQLite first, Elasticsearch optional

**Decision.** Every result is written to SQLite before anything is shipped. Alerting and the report only need SQLite, so they keep working when the search cluster is down or not deployed at all. Shipping to Elasticsearch is best effort: a failed bulk request is logged and probing continues.

**Trade-off.** Documents from a failed shipment are not retried. For a lab this is acceptable; a production version would keep a send cursor and replay.

## 6. ECS field names

**Decision.** Documents use Elastic Common Schema names where one exists (`event.outcome`, `event.duration`, `url.full`, `destination.domain`, `http.response.status_code`) and a custom `edge.probe.*` namespace for the SLO fields. Probe data can then be filtered and correlated with logs and traces from the same services in Kibana without renaming anything.

## 7. Secrets in the environment

**Decision.** The Elasticsearch API key and the webhook secret are read from `ES_API_KEY` and `WEBHOOK_SECRET`. The targets file can be committed and reviewed like any other configuration; unknown keys in it are rejected, so a `password = ...` line fails loudly instead of being ignored.

## 8. Webhooks instead of built-in notifications

**Decision.** Alert transitions go to one webhook, signed with HMAC-SHA256 (`X-Signature-256`). The receiver (n8n, a small function, an incident tool) decides who is told and how. Changing the on-call channel never requires changing the monitor.

## 9. Proxies

**Note.** The HTTP probe uses `urllib`, which honours `HTTP_PROXY` / `HTTPS_PROXY`. That is usually right for internet endpoints from a corporate network. For internal endpoints, set `NO_PROXY` so the probe measures the direct path instead of the proxy.
