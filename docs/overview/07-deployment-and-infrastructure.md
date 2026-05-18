# Deployment and infrastructure

The infrastructure plan, phase by phase. Concrete enough to act on; opinionated enough to start from.

## Phase 0 — Local + dev cloud

**Posture:** boring, single-region, smallest possible footprint. We're proving the loop, not scaling.

### Local development

Every engineer can run the platform locally with `docker-compose up`:

- Postgres 16 (single container)
- Redis 7 (single container)
- Vault OSS dev mode (no namespaces yet; paths-based isolation)
- LiteLLM Proxy (single container, real provider keys from `.env.local`)
- The FastAPI control plane
- The Next.js dashboard

E2B is *not* mocked locally — we use real E2B sandboxes pointed at our dev project. The cost is trivial (a few dollars a month per engineer) and avoids the "works locally, breaks in cloud" trap.

Coinbase Agentic Wallets in **testnet (Base Sepolia)** with mock USDC. No real money in phase 0.

### Dev cloud

A single dev environment in AWS, region us-east-1:
- One EKS cluster, 2-3 nodes (`t3.medium`), single AZ
- One Aurora Postgres serverless instance (small, auto-scaling)
- One ElastiCache Redis instance
- One HCP Vault dev-tier or self-hosted Vault on EKS
- One LiteLLM proxy deployment (single replica)
- E2B in their cloud
- S3 buckets for event log Merkle anchors, object storage
- Coinbase CDP project on Base Sepolia (testnet)

All defined in Terraform. Total monthly cost: estimated $300–500 (most of it Aurora minimum).

### CI/CD

GitHub Actions:
- On PR: lint, unit tests, type-check, build
- On merge to `main`: build container images, push to ECR, deploy to dev
- Manual approval for promotion to staging (when we have it)

No production environment yet in phase 0.

## Phase 1 — Single-region staging + small prod

**Posture:** real money, real users (us). Survives one node failure.

### Staging

Identical infrastructure shape to dev, but:
- 3 EKS nodes across 3 AZs
- Aurora Postgres regional with read replicas
- LiteLLM Proxy with 2 replicas
- HCP Vault Dedicated (single cluster, not enterprise namespaces yet)
- Coinbase mainnet wallets but funded with our own test capital (a few hundred USDC)

We use staging to run actual Darwins with real-but-small money for days at a time. This is where we discover what production will hit.

### Production (small)

A separate AWS account, separate region (us-west-2 to avoid co-locating failure modes), but otherwise identical to staging shape. Initially has zero users. The first user is one of us, intentionally, to feel the production experience.

### Treasury infra

- Stripe in test mode → Stripe live (after KYC of our company)
- Coinbase CDP project in live mode
- MoonPay sandbox → MoonPay production
- Persona or Onfido (one chosen after sandbox eval)
- Chainalysis sanctions screening (or TRM Labs)

### Observability

- OpenTelemetry collectors deployed to all environments
- Honeycomb account, with dataset per environment
- Sentry for app errors
- PagerDuty for on-call (single rotation across the founding team)

## Phase 2 — Multi-tenant alpha

**Posture:** real users beyond us. Survives an AZ failure. Compliance auditable.

### Tenant isolation goes hard

This is the biggest infra change in phase 2:

- **Vault Enterprise (or HCP Vault Plus)** with one namespace per Darwin. No more shared paths.
- **EKS namespaces per Darwin** with NetworkPolicies. Every Darwin's pods are in their own namespace; cross-namespace traffic is denied except to the supervisor-mediated egress proxy and the LangGraph checkpoint store.
- **The egress proxy** deployed cluster-wide. All outbound traffic from Darwin sandboxes flows through it; it logs, blocks the platform blocklist, enforces per-Darwin bandwidth quotas.
- **Per-Darwin databases** for organism data. Either separate Aurora Postgres serverless instances per Darwin (clean but expensive at scale) or per-Darwin databases on a shared cluster with RDS' per-database authentication (cheaper, less clean). We start with shared cluster + per-Darwin DBs and migrate to separate clusters at scale if we hit any tenant-isolation incidents.

### Platform DB vs organism DBs

- **Platform Postgres** (one Aurora cluster): billing, audit log, control-plane state, escalation tickets, lineage metadata, supervisor state, LangGraph checkpoints (per-Darwin schemas with RLS).
- **Organism Postgres** (per Darwin, on a shared Aurora cluster with per-DB credentials): whatever the Darwin's business is — content drafts, customer records, subscriber lists, etc.

The platform UI reads from organism DBs via a read-only role to populate the dashboard's Data browser.

### High availability

- Aurora Postgres multi-AZ
- EKS across 3 AZs with cluster autoscaler
- Multiple LiteLLM proxy replicas behind a target group, with health checks
- Multi-AZ ElastiCache Redis with replica
- Vault HA mode with integrated storage (Raft) across 3 nodes

### Backups & DR

- Aurora point-in-time recovery (7-day retention initially)
- Vault snapshot to S3 every 6 hours, cross-region replicated
- S3 versioning + lifecycle (90 days → Glacier)
- DR runbook: failover Aurora to read-replica region, restore Vault from snapshot, repoint DNS. Target RTO 4 hours, RPO 15 minutes (Aurora replication lag).

### Compliance hardening

- SOC 2 Type I readiness assessment by phase-2 mid
- KYC/AML procedures documented and audited
- Money transmitter assessment (legal partner) — we may operate as a non-custodial platform if the structure works, or via a partnership with a chartered MTL
- Data residency for EU users: keep their data in a EU region (eu-west-1) when we expand

### Cost target

Phase 2 target: < $40/month per *active* Darwin in all-in infrastructure cost (compute, storage, networking, third-party services). We hit this by:
- Sharing the Aurora cluster across organism DBs
- Aggressively rotating sandboxes (no idle compute)
- LiteLLM caching to reduce duplicate LLM spend
- Right-sizing E2B sandbox tiers per neuron class

If we can't hit $40 in phase 2, the unit economics are broken and the phase-2 kill criterion fires.

## Phase 3 — Public beta

**Posture:** publicly accessible, multi-region, capable of handling user growth bursts.

### Multi-region

Two regions at minimum:
- us-east-1 (primary for Americas)
- eu-west-1 (primary for EU users; data residency)

Adds:
- DNS-based geo routing (Route 53 latency or geo policy)
- Per-region EKS, Aurora, Vault, LiteLLM
- Cross-region Aurora replication for failover (not for active-active)
- Platform-level state still has a primary region; we live with the cross-region latency for control-plane operations

Asia-Pacific is a phase-3+ decision, not a launch requirement.

### Autoscaling

- EKS cluster autoscaler + Karpenter for fast node provisioning
- E2B usage scales with sandbox demand (E2B handles their own scaling)
- LiteLLM Proxy HPA on CPU + request rate
- Aurora serverless v2 for both platform and organism DB clusters

### CDN and edge

- Cloudflare in front of the Next.js dashboard (cache static, proxy dynamic)
- Cloudflare Workers for low-latency endpoints (status pages, healthchecks)
- Cloudflare AI Gateway in front of LiteLLM as an optional caching layer (evaluate during phase 3; not required)

### Security

- WAF on Cloudflare + AWS Shield Standard
- Audited pen-test before public launch
- Bug bounty program (small at first)
- Hardware-backed root signing keys (CloudHSM or equivalent) for DNA signature signing
- Vault auto-unseal via KMS with split-key custody

## Kubernetes shape

Walking through what runs in EKS, by namespace:

### `platform-system` namespace
- The FastAPI control plane (3+ replicas)
- The Next.js dashboard (3+ replicas)
- The supervisor workers (replicated, leader-elected via Postgres advisory locks)
- The escalation router worker
- The reconciliation worker (LiteLLM spend → ledger)
- Background job runners (Temporal Cloud workers)

### `platform-data` namespace
- The egress proxy (3+ replicas)
- LiteLLM Proxy (3+ replicas)
- Vault Agent injector
- OpenTelemetry collectors

### `darwin-{darwin_id}` namespaces (one per active Darwin)
- The orchestrator microVM (one, long-lived)
- Neuron microVMs (ephemeral, spawned per dispatch)
- Per-Darwin LanceDB sidecar (lightweight)
- NetworkPolicy: deny all egress except to `platform-data/egress-proxy`

A Darwin namespace is provisioned by the supervisor on spawn and torn down on death.

## Terraform layout

```
infra/
├── modules/
│   ├── eks/
│   ├── aurora-platform/
│   ├── aurora-organism-cluster/
│   ├── vault/
│   ├── litellm-proxy/
│   ├── egress-proxy/
│   ├── coinbase-cdp/
│   ├── stripe-account/
│   ├── moonpay-account/
│   └── observability/         # OTel collectors, Honeycomb, etc.
├── envs/
│   ├── dev/
│   │   └── main.tf
│   ├── staging/
│   │   └── main.tf
│   ├── prod-us-east/
│   │   └── main.tf
│   └── prod-eu-west/
│       └── main.tf
└── shared/                    # cross-env: DNS, signing keys, audit log anchoring
```

Terraform state in S3 with DynamoDB locking. State files encrypted with KMS. Changes go through a Terraform Cloud workspace with required approval for prod.

## What we do NOT run

A few things we explicitly *don't* operate, by choice:

- **Our own LLM inference.** No GPUs. Everything goes through LiteLLM to providers.
- **Our own browser fleet.** Browserbase handles this.
- **Our own custodial wallet.** Coinbase Agentic Wallets do this.
- **Our own KYC infrastructure.** Persona/Onfido do this.
- **Our own fiat ramps.** Stripe and MoonPay.
- **Our own blockchain nodes.** RPC providers (Alchemy/Infura on Base) handle this.
- **Our own DNS.** Route 53 + Cloudflare.

This is deliberate per principle [P9](02-design-principles.md). The novelty in this project is the agent architecture, not the substrate.

## Disaster recovery scenarios

A short list of what we plan for and how:

| Scenario | Detection | Response | RTO |
|---|---|---|---|
| EKS node failure | Node-not-ready alert | Cluster autoscaler replaces; pods reschedule | < 5 min |
| AZ outage | Multi-AZ failures | Route through healthy AZs; quorum maintains | < 10 min |
| Region outage | Region-level alarms | Manual failover to other region; DNS swing | < 4 hours |
| Aurora primary down | RDS event alert | Auto-failover to replica | < 2 min |
| Vault outage | Vault healthcheck | Standby promotes; new tokens issue | < 5 min |
| LiteLLM outage | Healthcheck + 5xx rate | Replica replacement; Darwins gracefully degrade tier | < 5 min |
| Coinbase outage | Wallet API errors | Darwins pause payments, escalate spending decisions to owners | manual |
| E2B outage | Sandbox provision failures | All Darwins effectively paused (no neurons can run) | manual; depends on E2B |
| Provider key compromise | Anomaly alert (sudden huge spend) | Rotate via Vault dynamic credentials, force Darwin orchestrator restart | < 30 min |
| Tenant-isolation breach | Audit log anomaly OR external report | Immediate kill of suspect Darwins, post-incident review, public disclosure if user data exposed | < 1 hour |

## Monitoring SLIs / SLOs

What we measure and target. Initial targets — to be tightened as the platform matures:

| SLI | SLO |
|---|---|
| Control-plane API availability | 99.9% |
| Darwin heartbeat health (90th percentile) | < 60s between heartbeats |
| Escalation delivery time (push → user device) | < 30s at p95 |
| Ledger reconciliation lag | < 60s |
| LiteLLM success rate (excluding upstream provider 5xx) | > 99.5% |
| Sandbox provision time | < 5s at p95 |
| Dashboard load time (first contentful paint) | < 2s at p95 |
| Vault read latency | < 100ms at p95 |
| Per-Darwin infra cost / month (phase 2) | < $40 |

Anything missing these for sustained periods triggers paging and a review.

## What we're not doing in this doc

- Kubernetes operator framework for Darwin lifecycle (premature; the supervisor handles it directly)
- Service mesh (Istio/Linkerd) — premature; mTLS within cluster handled by AWS PrivateLink and SGs
- Self-hosted log aggregation — we use Honeycomb + CloudWatch
- A custom event store — Postgres handles the audit log well at our scale
- Multi-cloud — we are AWS-first; GCP at most for some Gemini-routed work

The infrastructure stays boring on purpose, until it actively hurts us.
