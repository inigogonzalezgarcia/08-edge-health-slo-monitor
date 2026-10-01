# Incident review: public API unreachable by name after a DNS zone migration

> **Fictional incident** used to show how the monitor supports an incident review. It matches the first incident in the demo data (`python -m slo_monitor demo`, nine days before the time you run it). Company, names and systems are invented.

| | |
|---|---|
| Date | 2026-09-22 |
| Duration | 38 minutes (00:59–01:37 UTC) |
| Severity | SEV2 |
| Services affected | `api.example.com` (public API) |
| SLO impact | `public-api-dns` missed its 99.95 % SLO for the window; `public-api` used about 88 % of its monthly budget in this incident alone |
| Incident lead | On-call platform engineer |
| Status | Reviewed, actions in progress |

## Summary

During a planned migration of the `example.com` zone to a new DNS provider, the record for `api.example.com` was not created in the new zone. When the nameserver delegation switched, clients could no longer resolve the API. The `page-fast` alert fired five minutes after the first failure; restoring the record fixed resolution 38 minutes after it started.

## Impact

- Clients that had no cached answer could not reach the public API. Clients with a cached record kept working until their cache expired, so the impact grew over the first minutes.
- `public-api-dns`: 38 minutes of failures against a monthly budget of about 22 minutes (99.95 %): **SLO missed** for the window.
- `public-api`: about 38 of its 43 bad minutes allowed per month (99.9 %), roughly **88 % of the budget** spent in one night. Later small issues pushed it just below the SLO (see the report).

## Timeline (UTC)

| Time | Event |
|---|---|
| 00:45 | Migration step "switch delegation to the new provider" applied in the change window. |
| 00:59 | First failed DNS probe for `api.example.com`; the API probe fails with "name or service not known". |
| 01:04 | `page-fast` fires for `public-api-dns` (burn rate 167×) and `public-api` (83×). `page-slow` fires at the same time. |
| 01:07 | On-call acknowledges. Both probes fail with a resolution error, not a timeout or an HTTP error: the problem is the name, not the service. |
| 01:20 | Diff of old and new zone shows the missing record. It was created by hand years ago and was not in the export used for the migration. |
| 01:31 | Record added in the new zone. |
| 01:37 | DNS and API probes succeed again. |
| 01:44 | Fast alerts resolve as the short windows clear. The `ticket` alerts stay open for about six hours, as designed, and are closed by this review. |

**Time to detect:** 5 min · **Time to mitigate:** 32 min · **Time to recover:** 38 min

## Root cause and contributing factors

1. **Two sources of truth.** Most records were managed as code; a few older ones existed only in the provider's console. The migration copied the code, not the console.
2. **No pre-switch comparison.** Nobody compared the answers of the old and new nameservers for every live name before changing the delegation.
3. **High TTL on the delegation, low TTL on the record.** Rolling back the delegation would have taken longer than fixing the record, which limited the options.
4. **The probe used the system resolver only.** It detected the outage quickly, but did not show which nameserver was answering, which cost a few minutes in the diagnosis.

## What went well

- Separate DNS and HTTP probes pointed at the DNS layer within two minutes of acknowledging.
- Burn-rate alerting paged quickly without paging for the isolated slow probes earlier in the month.

## What was hard or lucky

- Lucky: the change happened at night with low traffic.
- Hard: the console-only record had no owner and no history.

## Action items

| # | Action | Type | Owner (role) | Due |
|---|---|---|---|---|
| 1 | Import every live record into code and block console edits | Prevent | Platform lead | 2 weeks |
| 2 | Before any delegation change, query old and new nameservers for every record and require an empty diff | Prevent | Platform engineer | Next migration |
| 3 | Add an authoritative-nameserver DNS probe next to the resolver probe | Detect | Platform engineer | 4 weeks |
| 4 | Lower TTLs 48 hours before migrations; add it to the change template | Mitigate | Change manager | Done |
| 5 | Add "check all live names" to the DNS change checklist | Process | Change manager | Done |

## Lessons for other teams

A migration copies what is written down. Anything created by hand outside version control is invisible to it, and DNS is where such leftovers hurt most, because nothing fails until the delegation moves.
