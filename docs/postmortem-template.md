# Incident review: <short title>

> Blameless: describe what the system and the process allowed, not who made a mistake.
> Write it within five working days, while people still remember the details.

| | |
|---|---|
| Date | YYYY-MM-DD |
| Duration | from first impact to full recovery |
| Severity | SEV1 / SEV2 / SEV3 |
| Services affected | |
| SLO impact | budget consumed, SLO met or missed for the window |
| Incident lead | role |
| Status | draft / reviewed / actions complete |

## Summary

Two or three sentences a manager can read on a phone: what broke, who noticed, how long, what fixed it.

## Impact

- Users or systems affected, and how (errors, slowness, data delayed).
- Error budget consumed per SLO (take the numbers from the report).
- Anything still degraded.

## Timeline (UTC)

| Time | Event |
|---|---|
| HH:MM | Change / trigger |
| HH:MM | First alert (which rule, which burn rate) |
| HH:MM | Acknowledged |
| HH:MM | Cause identified |
| HH:MM | Mitigation applied |
| HH:MM | Recovery confirmed by probes |

**Time to detect:** · **Time to mitigate:** · **Time to recover:**

## Root cause and contributing factors

What made this possible, as a chain. Usually more than one factor: a change, a missing check, a gap in monitoring, an unclear owner.

## What went well

## What was hard or lucky

## Action items

| # | Action | Type (prevent / detect / mitigate / process) | Owner (role) | Due | Ticket |
|---|---|---|---|---|---|
| 1 | | | | | |

## Lessons for other teams
