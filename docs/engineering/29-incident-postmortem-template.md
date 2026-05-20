# Incident post-mortem template

A structured format for documenting failures found during the soak test, the 90-day simulation, or production operation. Every incident produces three outputs: a root-cause record, a regression test, and a fix. This doc defines the format; actual post-mortems live in `docs/postmortems/` (created per-incident).

## When to write a post-mortem

- Any Phase 0 soak test failure (criteria 1–6 fail).
- Any unhandled exception that halts a Darwin in a non-test environment.
- Any audit chain break.
- Any ledger-wallet reconciliation mismatch.
- Any kill-switch latency > 5s.
- Any security incident (doc 24 threat materialized).
- Any billing discrepancy > $0.01.
- Any data loss (checkpoint corruption, persistence failure).
- Any failure that required a manual intervention to resolve.

## Template

```markdown
# PM-XXXX: <one-line title>

**Date:** YYYY-MM-DD
**Severity:** P0 / P1 / P2 / P3
**Author:** <name>
**Status:** resolved / mitigated / open

## Summary

One paragraph: what happened, what was the impact, how long did it last.

## Timeline

| Time (UTC) | Event |
| ---------- | ----- |
| HH:MM | First signal (alert, log line, user report) |
| HH:MM | Investigation started |
| HH:MM | Root cause identified |
| HH:MM | Fix deployed / Darwin recovered |
| HH:MM | Verified resolved |

## Impact

- Which Darwins affected (IDs or "all").
- Duration of impact.
- Data affected (audit records lost? balance discrepancy? secret exposure?).
- User-facing impact (if applicable).

## Root cause

Technical explanation. Reference specific code files, line numbers, and the contract that was violated (cite doc 28 if applicable). Include the chain of events that led to the failure.

## Detection

How was the failure detected? Which signal fired first? Was it automated (monitor alert, soak assertion) or manual (operator noticed)?

If the failure should have been detected earlier, explain why it wasn't and what detection gap exists.

## Resolution

What was done to resolve the incident? Include:
1. Immediate mitigation (kill, pause, manual fix).
2. Permanent fix (code change, config change, architectural change).
3. PR / commit reference for the fix.

## Regression test

The test that now prevents this from happening again. Include:
- File path: `tests/test_<name>.py::<test_function>`
- What it asserts.
- Which tier it belongs to (doc 26).

If no automated test is possible, explain why and describe the manual verification procedure.

## Lessons learned

### What went well
- ...

### What went poorly
- ...

### Where we got lucky
- ...

## Action items

| # | Action | Owner | Status | Due |
| - | ------ | ----- | ------ | --- |
| 1 | <specific action> | <name> | done / todo | YYYY-MM-DD |
| 2 | ... | | | |

## Related docs

- Failure mode: [FM-X in doc 25](25-operational-runbook.md)
- Threat: [doc 24 §...](24-security-threat-model.md)
- Contract violated: [doc 28 §...](28-cross-cutting-contracts.md)
```

## Severity definitions

| Level | Meaning | Response expectation |
| ----- | ------- | -------------------- |
| **P0** | Data loss, security breach, or uncontrolled money movement | Immediate all-hands response; post-mortem within 24h |
| **P1** | Darwin death from a bug (not intentional kill); soak test hard failure | Same-day investigation; post-mortem within 48h |
| **P2** | Degraded operation (Darwin alive but impaired); soft assertion failure | Investigate within 1 week; post-mortem within 1 week |
| **P3** | Cosmetic or non-impactful anomaly; documentation gap discovered | Track in backlog; post-mortem optional |

## File naming convention

```
docs/postmortems/PM-0001-audit-chain-break-on-restart.md
docs/postmortems/PM-0002-treasury-policy-race-condition.md
docs/postmortems/PM-0003-kill-switch-latency-spike.md
```

Sequential numbering. The title in the filename is a slug of the one-line title.

## Process

1. **Detect** — automated signal or human observation.
2. **Mitigate** — stop the bleeding (kill, pause, manual fix).
3. **Investigate** — identify root cause; record timeline.
4. **Fix** — code change + regression test.
5. **Document** — fill in the template; file under `docs/postmortems/`.
6. **Review** — the team reviews within the timeline for the severity level.
7. **Close** — mark status as resolved; ensure all action items are done.

## Blameless culture

Post-mortems document *system failures*, not *human failures*. The goal is to make the system more robust, not to assign blame. If a human made a mistake, the question is "why did the system allow that mistake to have impact?" — not "who messed up?"
