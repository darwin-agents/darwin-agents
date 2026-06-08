# Phase 2 — multi-tenant alpha build plan

Phase 1 proved one Darwin can evolve, reproduce, and earn real money. Phase 2 opens the platform to real users — a handful of internal and friendly alpha testers — each running their own fully isolated Darwin. This is the biggest infrastructure step in the project: from "one operator running a test organism" to "multiple principals trusting us with money and data."

This doc is the concrete build plan. It maps from the roadmap (`docs/overview/06-roadmap-and-phases.md` Phase 2) and the infrastructure design (`docs/overview/07-deployment-and-infrastructure.md` Phase 2) to specific deliverables, module changes, and acceptance criteria.

## What Phase 2 proves

1. **Tenant isolation is real.** No Darwin can see, touch, or influence another tenant's data, treasury, vault, or running soma — verified by internal pen-test and an external third-party audit.
2. **Real users can fund and observe.** A non-engineer can sign up, pass KYC, deposit fiat, spawn a Darwin, watch it run, resolve escalations, and harvest profits.
3. **The unit economics work.** Platform infrastructure cost per active Darwin is < $40/month.
4. **The product is coherent.** Alpha users don't universally find it confusing, useless, or distrustful.
5. **Operational maturity is sufficient.** Incidents are detected, paged, and resolved per runbook. Data survives AZ failures.

## Multi-tenancy hardening

### Per-Darwin Kubernetes namespaces

Phase 1 runs all Darwins as asyncio tasks inside a single supervisor process. Phase 2 separates each Darwin's compute into its own Kubernetes namespace.

```
platform-system/          Supervisor, API, dashboard, background workers
platform-data/            Egress proxy, LiteLLM, Vault Agent, OTel collectors
darwin-{darwin_id}/       Per-Darwin: orchestrator pod, neuron microVMs, LanceDB sidecar
```

Each `darwin-{id}` namespace gets a NetworkPolicy:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: darwin-default-deny
  namespace: darwin-{{ darwin_id }}
spec:
  podSelector: {}
  policyTypes: [Ingress, Egress]
  egress:
    - to:
        - namespaceSelector:
            matchLabels:
              role: platform-data
      ports:
        - port: 443 # egress proxy
        - port: 5432 # LangGraph checkpoint store
  ingress: [] # no inbound — Darwins are clients, not servers
```

The supervisor provisions the namespace at spawn and tears it down at death.

### Vault Enterprise namespaces

Phase 0–1 uses path-based isolation on a single Vault OSS/dev instance. Phase 2 moves to **HCP Vault Plus** (or Vault Enterprise self-hosted) with one namespace per Darwin:

```
vault/
  darwin-{id}/           Per-Darwin namespace
    kv/                  KV v2 secrets: credentials, API keys, account tokens
    pki/                 Per-Darwin short-lived certs (future, for mTLS within namespace)
    transit/             Per-Darwin encryption-as-a-service (for organism data at rest)
```

Each Darwin authenticates via AppRole; each neuron invocation gets a child token with ACL policies matching the neuron's capability tags (`tier=public`, `tier=scoped`, `tier=sensitive`). Cross-namespace access is syntactically impossible in Vault Enterprise.

### Per-Darwin databases (organism data)

Two levels of Postgres in Phase 2:

| Layer        | Cluster                          | Isolation                                 | Purpose                                                            |
| ------------ | -------------------------------- | ----------------------------------------- | ------------------------------------------------------------------ |
| **Platform** | Shared Aurora (multi-AZ)         | Per-Darwin schemas + RLS                  | Billing, audit, lifecycle, lineage, checkpoints, escalation        |
| **Organism** | Shared Aurora (separate cluster) | Per-Darwin databases + per-DB credentials | Whatever the Darwin's business is: content, customers, subscribers |

The `infrastructure.provisioner` neuron requests organism databases via the platform. The platform creates a database on the shared organism cluster, writes the connection string into the Darwin's vault namespace (`tier=scoped`, tagged to the requesting neuron), and exposes a read-only role for the dashboard's Data browser.

### The egress proxy

All outbound traffic from Darwin namespaces flows through a cluster-wide egress proxy in `platform-data/`:

- **Logs** every connection: URL, response code, payload size → audit trail.
- **Blocks** destinations on the platform blocklist (OFAC-sanctioned IPs, known phishing, internal cluster CIDRs).
- **Enforces** per-Darwin bandwidth quotas (default 10 GB/day, 2× burst).
- **Strips** internal headers before forwarding to the external world.

Implementation: Envoy proxy with a dynamic blocklist sidecar reading from the supervisor's configuration. The blocklist is updated by operator action (`POST /platform/blocklist`).

### E2B sandbox lifecycle

Phase 2 E2B posture:

- **Sandbox rotation**: each Darwin's orchestrator sandbox is replaced every 24 hours (or on soma mutation, whichever comes first). No sandbox accumulates long-lived state beyond what's checkpointed.
- **Per-dispatch neuron sandboxes**: fresh microVM per neuron invocation; torn down on completion.
- **Quotas**: max 16 alive sandboxes per Darwin (queued beyond that). Global fleet cap configurable per the E2B account tier.
- **Fallback**: if E2B is unavailable, Darwins enter a degraded mode (orchestrator runs but neurons cannot dispatch; the supervisor pauses the fleet and alerts).

## The full payment stack

### Fiat on-ramp (Stripe)

Users deposit via card, ACH, or SEPA through Stripe:

```
User card/bank  →  Stripe Checkout  →  webhook (payment_intent.succeeded)
                                          │
                                          ▼
                                    Platform receives fiat
                                          │ convert to USDC
                                          ▼
                                    Platform omnibus wallet (USDC on Base)
                                          │ fund Darwin wallet
                                          ▼
                                    Per-Darwin Coinbase Agentic Wallet
```

Stripe is the source of truth for the user's fiat deposit history. The platform reconciles Stripe charges against the omnibus wallet balance daily.

### KYC integration

Before a user can fund a Darwin (or before a Darwin can perform identity-sensitive actions), the user passes KYC. Provider: **Persona** (or Onfido — final pick by Phase 2 kickoff after sandbox evaluation of both).

- KYC result (pass/fail + tier) is stored in the platform DB.
- KYC documents (ID scan, selfie, utility bill) are written to the user's vault under `tier=sensitive` — readable only when a neuron presents an owner-approved escalation token.
- The platform never stores KYC docs in plaintext outside the vault.

**Provider adapters + verdict ingestion.** `security/kyc.py` ships `PersonaKYCProvider` and `OnfidoKYCProvider` behind the `KYCProvider` protocol (`start_inquiry` / `poll_inquiry` / `verify_webhook` / `revoke`); `KYCService` caches verdicts, ages them at the retention TTL, and exposes the spawn/fiat gate. Verdicts arrive two ways:

- **Operator/manual poll** — `POST /platform/kyc/webhook` (bearer-authenticated) re-polls a named inquiry.
- **Provider callback** — `POST /webhooks/kyc` (unauthenticated at the bearer layer; the provider's signature is the auth). The configured adapter verifies the signature over the raw body — Persona's `Persona-Signature` (`t=…,v1=…` HMAC-SHA256 of `"{t}.{body}"`, hex, with secret-rotation support) or Onfido's `X-SHA2-Signature` (HMAC-SHA256 of the raw body keyed by the webhook token) — then the service re-polls for the authoritative verdict and caches it. Secrets are wired via `--kyc-persona-webhook-secret` / `--kyc-onfido-webhook-token` (or their env vars) and are never logged.

### Real wallet provisioning (mainnet)

Each spawned Darwin gets a **Coinbase Agentic Wallet** on Base mainnet:

- Wallet key material lives in Coinbase's TEE — the platform never holds private keys.
- The supervisor calls the Coinbase CDP API to provision the wallet at spawn time.
- Spend policy is installed at the wallet level (matching the DNA's hard caps): per-tx max, daily max, approved-recipients allowlist.
- On Darwin death, any residual balance is swept back to the platform omnibus wallet, then credited to the user's account for withdrawal.

### x402 infrastructure billing

The platform's own services (LiteLLM, E2B, Browserbase) are invoiced to each Darwin via x402:

- LiteLLM proxy returns 402 on each completion request; the Darwin's wallet auto-settles.
- E2B sandbox costs are metered and billed per minute of usage.
- Browser sessions are metered per session-second.

This means the platform's revenue model is **pass-through markup** on infrastructure the Darwin consumes, plus a **harvest fee** on gross profit withdrawals.

### Sanctions screening (production)

Phase 1 uses `LocalDenylistScreener`. Phase 2 integrates a real provider:

- **Chainalysis KYT** (Know Your Transaction) for on-chain address screening.
- OR **TRM Labs** for the same.
- Every outbound wallet transfer is screened before chain submission.
- Blocked transfers are logged, the Darwin is paused, and an alert fires.

### Harvest rules

The harvest scheduler (`harvest_scheduler.py`) already exists. Phase 2 wires it to real withdrawals:

- User configures rules via the dashboard ("move balance above $X to my wallet on schedule Y").
- Harvest triggers a withdrawal from the Darwin's wallet to the user's verified external wallet/bank.
- The platform deducts a harvest fee (configurable; default 10% of gross profit harvested) before transfer.
- The withdrawal is subject to the same sanctions screening as any outbound payment.

## The user-facing dashboard

### Architecture

```
Next.js app (Vercel or EKS)  →  Darwin Platform API (FastAPI :8000)
                              →  WebSocket feed for real-time updates
```

Auth: **Clerk** (or WorkOS) for user identity → the platform issues a scoped bearer token (scope=owner, owned_darwins=[...]) that the dashboard passes to the API.

### Screens (Phase 2 MVP)

| Screen               | What it shows                                                                           | API endpoints                                       |
| -------------------- | --------------------------------------------------------------------------------------- | --------------------------------------------------- |
| **Darwin list**      | All user's Darwins: status, balance, age, generation, fitness                           | `GET /darwins`                                      |
| **Anatomy**          | Live organism visualization: DNA layers, active strategy, neuron registry, current plan | `GET /darwins/{id}`, WebSocket state feed           |
| **Timeline**         | Chronological event feed: dispatches, payments, escalations, mutations, reproductions   | `GET /darwins/{id}/events` (paginated)              |
| **Vault**            | Bidirectional: user entries + Darwin entries, with provenance and revoke actions        | Vault admin API                                     |
| **Data browser**     | Read-only window into the Darwin's organism databases                                   | New: `GET /darwins/{id}/data/{table}`               |
| **Escalation inbox** | Open tickets, deadlines, approve/deny/provide-data actions                              | `GET /darwins/{id}/escalations`, `POST .../respond` |
| **Lineage tree**     | Visual tree of generations with fitness scores                                          | `GET /darwins/{id}/lineage/tree`                    |
| **Settings**         | Budget caps, harvest rules, neuron enable/disable, pause/kill                           | Various `POST` endpoints                            |
| **Billing**          | Deposit history (Stripe), current balance, cost breakdown, harvest history              | Billing API                                         |

### Mobile (escalation push)

A minimal Expo / React Native app (or progressive web app) for:

- Push notifications on new escalations (FCM / APNs).
- Approve/deny/provide-data inline from the notification.
- Quick-glance Darwin status (alive, balance, open escalations).

The mobile app hits the same API; the push delivery is wired through the `NotificationSink` protocol in `escalation.py` (currently `LogNotificationSink`; Phase 2 swaps in an `FCMNotificationSink`).

## The supervisor in earnest

### Resource quota enforcement

The monitoring module (`monitoring.py`) already computes signals and fires alerts. Phase 2 closes the loop:

| Signal           | Threshold → WARN     | Threshold → CRITICAL | Response                          |
| ---------------- | -------------------- | -------------------- | --------------------------------- |
| Spend velocity   | > $30/hour sustained | > $50/hour sustained | Throttle LLM tier → auto-pause    |
| Failure rate     | > 40% over 1h        | > 60% over 1h        | Throttle coding tier → auto-pause |
| Heartbeat gap    | > 120s               | > 300s               | Alert → quarantine investigation  |
| Escalation rate  | > 10/hour            | > 20/hour            | Alert → auto-pause                |
| Egress bandwidth | > 20 GB/day          | > 50 GB/day          | Throttle egress → pause           |

### Anomaly detection (behavior drift)

Beyond threshold-based alerts, Phase 2 adds a simple drift detector:

- The supervisor maintains a 7-day rolling profile of each Darwin's behavior (neuron dispatch distribution, spend category distribution, escalation pattern).
- If a day's behavior deviates by > 2σ from the rolling mean, an INFO alert fires.
- If deviation persists for 3+ consecutive days, a WARN fires and the owner is notified ("your Darwin's behavior has changed significantly").

This catches scenarios the threshold detectors miss — e.g., a Darwin that suddenly stops doing what it was doing and starts doing something entirely different (possibly a soma mutation gone wrong).

### Escalation classification

The `TicketClass` enum already classifies escalations (captcha, KYC, approval, info, data). Phase 2 enriches the router to:

- Route `captcha` and `KYC` tickets with higher urgency (shorter deadline, louder notification).
- Group `info` tickets into a daily digest (no push per ticket).
- Track per-class response rates and time-to-respond for the fitness function.

### Billing reconciliation

The billing service (`billing.py`) already meters wallet debits. Phase 2 adds:

- Daily reconciliation: platform omnibus wallet balance vs. sum of all Darwin wallet balances + platform revenue.
- Discrepancy alert if mismatch > $1 (fires immediately; investigation required).
- Monthly invoicing: per-user statement of charges (infrastructure markup) and harvest fees.

## Operational maturity

### Observability stack

| Layer    | Tool                   | Phase 2 change                                                             |
| -------- | ---------------------- | -------------------------------------------------------------------------- |
| Traces   | Honeycomb (via OTLP)   | Replace Jaeger with Honeycomb for production; Jaeger remains for local dev |
| Metrics  | Prometheus + Grafana   | Grafana Cloud for dashboards; custom Darwin fleet dashboard                |
| Logs     | CloudWatch + structlog | Structured JSON; per-Darwin log group; 90-day retention                    |
| Alerting | PagerDuty              | On-call rotation across founding team; escalation policy                   |

### Audit log Merkle anchoring

The audit log is already hash-chained per-Darwin. Phase 2 adds daily Merkle root anchoring:

- Every 24 hours, the platform computes a Merkle tree over all audit entries written that day.
- The root hash is published to a public chain (Base mainnet; a single transaction per day, cost < $0.01).
- Anyone (user, auditor, regulator) can verify that the platform hasn't tampered with the audit log by checking the anchor.

### Runbooks (top 10)

| #   | Incident                            | Runbook                                                                              |
| --- | ----------------------------------- | ------------------------------------------------------------------------------------ |
| 1   | Darwin stuck (no heartbeat > 5 min) | Check namespace pod status; restart pod; if persists, kill and post-mortem           |
| 2   | Wallet balance mismatch             | Halt Darwin; compare ledger vs. chain; manual reconciliation; resume or kill         |
| 3   | Audit chain break                   | Halt Darwin; identify gap; backfill from checkpoint; human review before resume      |
| 4   | E2B fleet down                      | All Darwins enter degraded mode; notify owners; await E2B recovery                   |
| 5   | LiteLLM unhealthy                   | Auto-failover to backup replica; if all down, Darwins degrade to cheaper tiers       |
| 6   | Escalation delivery failure         | FCM/SES retry; if persistent, fall back to email; notify owner via secondary channel |
| 7   | Tenant isolation suspected          | Immediate kill of suspect Darwins; lock affected namespaces; third-party forensics   |
| 8   | KYC provider outage                 | New spawns blocked; existing Darwins unaffected; queue until recovery                |
| 9   | Coinbase wallet API outage          | Darwins pause all payments; escalate spending decisions to owners                    |
| 10  | Region partial failure              | Pods reschedule to healthy AZs; monitor; manual region-failover if full AZ loss      |

### Backup and disaster recovery

| Component                      | Backup                                | Retention     | RPO         | RTO                     |
| ------------------------------ | ------------------------------------- | ------------- | ----------- | ----------------------- |
| Aurora Platform DB             | Point-in-time recovery                | 7 days        | < 5 min     | < 2 min (auto-failover) |
| Aurora Organism DBs            | Point-in-time recovery                | 7 days        | < 5 min     | < 2 min                 |
| Vault                          | Raft snapshots → S3 (cross-region)    | 90 days       | < 6 hours   | < 30 min                |
| S3 (event log anchors, assets) | Versioning + lifecycle                | 90d → Glacier | 0 (durable) | < 5 min                 |
| EKS state                      | GitOps (Flux/ArgoCD) from Helm charts | Git history   | 0           | < 10 min                |

## Security gate

Phase 2 cannot launch to external users until:

- [ ] Internal pen-test of tenant isolation passes (no cross-tenant data leakage, no namespace escape, no vault cross-read).
- [ ] Third-party security audit commissioned and completed (scope: tenant isolation, auth, wallet security, egress controls).
- [ ] KYC/AML procedures reviewed by legal counsel.
- [ ] SOC 2 Type I readiness assessment completed (full certification is a Phase 3 deliverable).

## Module map (new + modified)

```
src/
  darwin_platform/
    security/kyc.py                       NEW  — KYC provider integration (Persona/Onfido)
    payments/stripe_ramp.py               NEW  — Stripe fiat on-ramp: checkout sessions, webhooks
    payments/omnibus_wallet.py            NEW  — Platform omnibus wallet: fund, sweep, reconcile
    infra/egress_proxy.py                 MOD  — adds Phase 2 BandwidthQuota + /bandwidth endpoint
    tenancy/namespace_provisioner.py      NEW  — K8s + Vault namespace + organism DB lifecycle
    observability/drift_detector.py       NEW  — Behavior drift anomaly detection (rolling 7d profile)
    observability/merkle_anchor.py        NEW  — Daily Merkle root → public chain anchoring
    escalations/notification_fcm.py       NEW  — FCM push notification sink
    runtime/supervisor.py                 MOD  — Phase 2 services injection + helper accessors
    api.py                                MOD  — data browser endpoint, billing statements, KYC status
    security/auth.py                      MOD  — Clerk/WorkOS integration (replace in-memory tokens)
    payments/billing.py                   MOD  — daily reconciliation, monthly invoicing
    observability/monitoring.py           MOD  — drift detector integration, tighter quotas
    escalations/escalation.py             MOD  — FCM sink wiring, ticket classification enrichment
    adapters/wallet.py                    MOD  — Coinbase CDP API for wallet provisioning
    adapters/vault_backend.py             MOD  — Vault Enterprise namespace creation

  web/                            NEW  — Next.js dashboard
    app/
      dashboard/                  Darwin list, spawn, settings
      anatomy/                    Live organism view
      timeline/                   Event feed
      vault/                      Bidirectional vault browser
      data/                       Organism data browser
      escalations/                Inbox + respond
      lineage/                    Tree visualization
      billing/                    Deposits, costs, harvests
    lib/
      api.ts                      Type-safe API client (from OpenAPI schema)
      ws.ts                       WebSocket real-time feed
      auth.ts                     Clerk integration

  mobile/                         NEW  — Expo/RN escalation app (minimal)
    src/
      PushHandler.tsx
      EscalationCard.tsx
      StatusWidget.tsx

  infra/                          NEW  — Terraform IaC
    modules/
      eks/
      aurora-platform/
      aurora-organism-cluster/
      vault/
      litellm-proxy/
      egress-proxy/
      coinbase-cdp/
      stripe-account/
      observability/
    envs/
      dev/
      staging/
      prod-us-east/
```

## Definition of done (Phase 2)

Per `docs/overview/06-roadmap-and-phases.md`:

- [ ] 5+ alpha users have each funded and run a Darwin for ≥ 2 weeks.
- [ ] ≥ 50% of alpha-user Darwins survive their first 7 days.
- [ ] ≥ 1 alpha-user Darwin reaches generation 2 (successful reproduction by a paying user's Darwin).
- [ ] ≥ 1 escalation per alpha-user successfully resolved end-to-end via the mobile push flow.
- [ ] No tenant-isolation incidents (verified via pen-test + external audit).
- [ ] Platform infra cost per active Darwin < $40/month.

### Kill criteria (Phase 2)

- Alpha users universally find the product confusing, useless, or distrustful.
- A tenant-isolation incident occurs that we can't fix structurally.
- Unit economics don't close: Darwin metabolism costs more than the platform's pass-through margin can cover, and no obvious optimization path.

## Timeline and sequencing

| Track                 | Weeks | Deliverable                                                                                                    |
| --------------------- | ----- | -------------------------------------------------------------------------------------------------------------- |
| **A — Isolation**     | 16–20 | K8s namespace provisioner, Vault Enterprise migration, egress proxy, per-Darwin DBs, internal pen-test         |
| **B — Payments**      | 17–22 | Stripe integration, KYC (Persona), real wallet provisioning (CDP API), sanctions provider, harvest withdrawals |
| **C — Dashboard**     | 18–24 | Next.js app (all screens), Clerk auth, WebSocket feed, mobile push (Expo)                                      |
| **D — Ops maturity**  | 20–26 | Honeycomb migration, Merkle anchoring, runbooks, on-call, backup/DR drill, drift detector                      |
| **E — Security gate** | 24–28 | External audit, legal review of KYC/AML, SOC 2 readiness                                                       |

Tracks A and B are the critical path — without isolation and payments, no external user can be onboarded. Track C can lag slightly (internal users can use curl for a few weeks). Track D runs in parallel throughout. Track E is the final gate before any external invite.

## After Phase 2

When the definition of done is met, the next doc is **Phase 3 — public beta** (`23-phase-three-public-beta-build-plan.md`): public waitlist, expanded neuron catalog, cross-lineage learning, multi-region, and the long-term architecture experiments.
