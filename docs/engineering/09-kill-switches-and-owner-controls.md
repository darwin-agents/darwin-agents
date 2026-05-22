# Kill switches and owner controls

Most of this work is _operational_, not architectural. The architecture has been designed for these controls; this doc says how they work in practice and how they get exercised.

## Three kill triggers, three latencies

A Darwin can be stopped by three parties, with three different latencies and different invariants. They all converge on the same final state.

### 1. Self-kill (the death sequence)

The Darwin notices that its treasury is below the minimum viable balance with no revenue inbound, and triggers its own death. This is the most common kill — most Darwins will die this way.

- **Latency:** up to one heartbeat (typically ≤ 30 seconds).
- **Trigger source:** the Darwin's own `check_death` node in the orchestrator.
- **Path:** orchestrator stops at safe checkpoint → settles in-flight commitments where it's cheaper to do so than abandon → writes final ledger entries → snapshots soma for forensics → notifies owner → exits.
- **What survives:** event log, ledger, the Darwin's databases, the vault namespace, children (if any).

### 2. Owner kill

The owner clicks "Terminate" in the dashboard. Confirmation modal because this is destructive.

- **Latency:** ≤ 5 seconds from confirmation to halt (one heartbeat plus a few seconds for the supervisor to flip the flag).
- **Trigger source:** dashboard → platform API → supervisor → flag flip in the Darwin's namespace.
- **Path:** supervisor sets `kill_flag=true` AND cuts the Darwin's egress network access at the proxy in parallel → orchestrator polls the flag at the top of its main loop and on every neuron dispatch → halts → writes the death sequence's final entries.
- **What survives:** same as self-kill, plus the kill is annotated in the event log as "owner-initiated".

### 3. Platform kill

The platform decides a Darwin must die. Reasons range from confirmed policy violations to compromised infrastructure (a Darwin's sandbox got popped, say) to regulator demand.

- **Latency:** ≤ 5 seconds, same path as owner kill.
- **Trigger source:** platform supervisor, either auto-fired by anomaly detection or manually fired by a platform operator.
- **Path:** identical to owner kill but with a `reason` field in the event log entry and a different notification template to the owner.
- **What survives:** same as the others. The owner can read the platform's stated reason.

In all three cases, the **graceful shutdown invariant** holds: the Darwin halts before its next side-effecting action. There is no scenario where a kill arrives mid-payment and we're left with a half-completed transaction.

## The five-level graded response

Before reaching kill, the supervisor escalates through five graded levels. Most Darwin issues never reach kill.

| Level                      | Trigger                                                     | Response                                                                               | User-visible?                                                            |
| -------------------------- | ----------------------------------------------------------- | -------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| 1. Log + dashboard warning | Mild anomaly (single-event spike)                           | Annotated in event log; yellow badge on dashboard                                      | Yes (passive)                                                            |
| 2. Throttle                | Sustained anomaly above threshold                           | Rate-limit the offending resource (LLM calls, sandbox spawns, egress bandwidth)        | Yes (passive)                                                            |
| 3. Pause                   | Anomaly persists or escalates                               | Set `pause_flag=true`; orchestrator halts at next checkpoint; await owner decision     | Yes (active — notification)                                              |
| 4. Quarantine              | Confirmed policy issue or compliance trigger                | Pause + lock the Darwin's wallet + lock outbound network; supervise-side review begins | Yes (active — high-priority notification, "your Darwin is under review") |
| 5. Kill                    | Unrecoverable issue or quarantine review confirms violation | Permanent halt as above                                                                | Yes (final notification with reason)                                     |

The supervisor only auto-escalates _up_ to level 4. Level 5 (kill) on the platform side always requires a human operator approval — never auto-fired, even for confirmed policy violations. This is deliberate: an auto-kill bug would be catastrophic across the fleet.

Self-kill is the exception — it doesn't go through grading; it goes directly to graceful death because the trigger is "the agent has no money left".

## The owner's available controls

The user has more granular controls than just "kill". From the dashboard or mobile app:

### Lifecycle

- **Pause / Resume.** Take a coffee break; come back. The Darwin halts at next checkpoint and resumes cleanly.
- **Terminate.** Permanent. Confirmation required.

### Budget

- **Tighten hard caps.** Lower the per-action / per-day / lifetime spend ceilings below the DNA's hard cap (cannot raise them).
- **Adjust strategy budget envelope.** Force the Darwin to spend less on a specific strategy.
- **Pause spending entirely.** A "spend lock" flag that refuses all outbound payments but lets the Darwin keep running otherwise (useful while debugging).

### Capabilities

- **Disable a neuron.** "Don't use the ads.manager neuron right now." The Darwin replans without it.
- **Disable a category.** "Don't pursue affiliate strategies for the next 24 hours." Tactical control.
- **Force escalation lowering.** "Escalate to me on every spend over $1." (Cannot raise the escalation threshold above the DNA's default — escalation is one-way relaxable, not strengthenable.)

### Reproduction

- **Block reproduction.** The Darwin can keep running and earning, but cannot spawn children. Useful if the user wants a stable single Darwin without lineage.
- **Force reproduction.** Trigger a reproduction event now, even if the fitness threshold hasn't been crossed. Useful for testing or for "harvesting" lessons before the parent dies.
- **Force compaction.** For deep lineages, manually trigger the layer compaction step.

### Vault

- **Revoke a credential.** Removes the entry; the next neuron that needs it escalates.
- **Mark sensitive.** Promote a `tier=scoped` entry to `tier=sensitive`, requiring fresh escalation on every read.

### Harvest

- **Set/change harvest rule.** "Move profits above $X to my external wallet on schedule Y."
- **Force harvest now.** Move everything above the minimum viable balance.

All of these write to the audit log with `owner_action: <kind>`. The user has a complete history of what they did to their Darwin, when, and why (with an optional note field).

## The platform operator's controls

In addition to the kill switch, platform operators (us, internally) can:

- **Suspend pending review.** Move a Darwin to level 4 (quarantine). Required when a complaint or anomaly is investigated.
- **Force a constitutional amendment.** The platform-supervisor pathway for forced reproduction with a platform-supplied amendment DNA layer. Used only for critical constitution updates.
- **Hard-block a counterparty.** Add a recipient to the platform's global blocklist. Affects all running Darwins.
- **Hard-block a category.** Add a business category to the platform's forbidden list. Affects new strategies; existing strategies in the category are quarantined.
- **Throttle the fleet.** Reduce per-Darwin LLM tier caps globally during incidents (e.g. an LLM provider has a known issue and we want to slow consumption).

Operator actions also write to the audit log, with operator identity (verified via WebAuthn-protected internal tooling).

## Recovery and the no-resurrection rule

A killed Darwin cannot be resurrected. The death is permanent.

This is deliberate. Resurrection breaks the selection-pressure model and creates a category of "almost dead" agents that are neither living nor dead. It also creates a regulatory mess (does the resurrected Darwin inherit the dead Darwin's transaction history?).

What the user _can_ do:

- **Spawn a new Darwin** with a fresh budget. Genesis 0, no inherited lessons.
- **Spawn a new Darwin from an archived snapshot,** if the dead Darwin was useful enough that we wanted to preserve its lineage. This is a _new_ Darwin (new ID, new wallet) but with the parent's DNA + lessons up to the moment of death. It's a normal post-mortem reproduction event; we just allow it for an already-dead parent within a 30-day window.
- **Inherit the dead Darwin's accounts and databases.** These are in the user's vault and survive death. The new Darwin can pick up where the old one left off — same WordPress site, same Stripe Connect, same subscriber list. The biological metaphor breaks here; we lean into it for usability.

## The platform's commitments to users

Stated plainly so we can hold ourselves to them:

1. **We honor kill commands within 5 seconds.** Even during platform incidents, the kill path is the highest-priority surface in the system.
2. **We don't auto-kill.** No anomaly detector can permanently terminate a Darwin without a human-in-the-loop at the platform side.
3. **A dead Darwin's data survives.** The user retains the event log, ledger, databases, vault contents, and any accounts the Darwin opened, for at least 90 days post-death (longer where required by law).
4. **We don't take what's left.** When a Darwin dies, the user keeps any residual treasury balance (no platform fee on residuals, only on harvests of profit).
5. **We're audit-transparent.** The user can replay any moment of any of their Darwins' lives. The audit log is theirs as much as ours.

## What we do not promise

Equally plainly:

1. **We do not guarantee profitability.** Most Darwins will die. This is by design.
2. **We do not refund losses.** A Darwin that burned through its budget is gone; the budget is gone. Stated at signup.
3. **We are not your fiduciary.** We host the platform; we do not direct what your Darwin does. The DNA defines limits, but inside those limits the Darwin makes its own decisions.
4. **We may force reproduction events.** For critical constitutional updates, your Darwin may be forced to undergo a reproduction event with a platform-supplied amendment layer. The parent dies; the child carries the amendment. We will publish a notice before doing this, with rationale.
5. **We may suspend the platform.** Force majeure, regulatory action, security incident. Suspended Darwins are paused, not killed; we resume when the issue resolves. If we have to wind down for good, we publish a wind-down timeline with at least 30 days' notice (or as soon as legally permitted).

## Operational drills

Things we will practice on a schedule:

| Drill                            | Frequency                             | What we verify                                           |
| -------------------------------- | ------------------------------------- | -------------------------------------------------------- |
| Kill-switch fire (single Darwin) | Weekly in staging, monthly in prod    | < 5s latency, clean death sequence, audit trail complete |
| Fleet-wide pause                 | Monthly in staging, quarterly in prod | Supervisor can pause all Darwins without losing state    |
| DR failover (region)             | Quarterly in staging                  | RTO 4 hours, RPO 15 minutes                              |
| Vault sealing + unsealing        | Quarterly                             | Procedure works, runbook is current                      |
| Pen-test of tenant isolation     | Annually (external)                   | No cross-tenant data leakage                             |
| Incident response                | Quarterly tabletop                    | Team knows roles, runbooks current                       |

Drills that fail block any new feature work until the underlying issue is fixed.

## Things still open

- Owner emergency override of an unresponsive Darwin (e.g. if the orchestrator is stuck in a tight loop and not polling the kill flag fast enough). Currently mitigated by the supervisor's network-level egress cut, which forces the Darwin to fail-and-halt. Worth designing an explicit "force-kill" that bypasses the orchestrator entirely. Phase 2 task.
- Per-jurisdiction kill SLOs. EU users may want faster kill commitments than US users due to GDPR. Phase 2 question for legal.
- Coordinated multi-Darwin kill for a user with many running Darwins. UX detail; phase 2 polish.
