# System diagrams

This file is the visual companion to the architecture docs. The ASCII-art diagrams here are reference renderings of what the platform and the organism actually look like end-to-end. Use these alongside [`01-system-overview.md`](01-system-overview.md).

For each diagram, the corresponding deep-dive doc is linked at the bottom of that section.

---

## 1. The platform, end to end (control plane perspective)

This shows what's running in our infrastructure to host many Darwins.

```
                                  ┌────────────────────────────────────────────────┐
                                  │                  USERS (signed in via Clerk/   │
                                  │                  WorkOS, KYC'd via Persona)    │
                                  └───────────────────────┬────────────────────────┘
                                                          │
                                                          ▼
                                  ┌────────────────────────────────────────────────┐
                                  │              EDGE (Cloudflare)                 │
                                  │           - DNS, CDN, WAF, DDoS                │
                                  └───────────────────────┬────────────────────────┘
                                                          │
                          ┌───────────────────────────────┼───────────────────────────────┐
                          │                               │                               │
                          ▼                               ▼                               ▼
              ┌─────────────────────┐         ┌─────────────────────┐         ┌─────────────────────┐
              │   Web app           │         │   REST/WS API       │         │   Webhooks          │
              │   Next.js dashboard │         │   FastAPI           │         │   Stripe, MoonPay,  │
              │   (Anatomy/Timeline)│         │   (control plane)   │         │   Coinbase, KYC     │
              └──────────┬──────────┘         └──────────┬──────────┘         └──────────┬──────────┘
                         │                               │                               │
                         └───────────────────────────────┼───────────────────────────────┘
                                                         │
                                                         ▼
                          ┌──────────────────────────────────────────────────────────┐
                          │              DARWIN SUPERVISOR                            │
                          │  - lifecycle (spawn/pause/kill/reproduce)                 │
                          │  - kill-switch enforcement, quotas, anomaly detection     │
                          │  - escalation router (push/email/SMS)                     │
                          │  - reconciliation worker (LiteLLM → ledger)               │
                          │  - signing service client                                 │
                          └────┬──────────────────────┬──────────────────────┬────────┘
                               │                      │                      │
            ┌──────────────────┘                      │                      └───────────────────┐
            ▼                                         ▼                                          ▼
   ┌──────────────────┐               ┌────────────────────────────┐               ┌──────────────────────┐
   │  Signing service │               │   Kubernetes (EKS)         │               │  Notification bus    │
   │  (HSM/KMS)       │               │   - per-Darwin namespace   │               │  - push (Expo/APN)   │
   │  - DNA layers    │               │   - microVM sandboxes      │               │  - email             │
   │  - audit anchors │               │   - egress proxy           │               │  - SMS (Twilio)      │
   └──────────────────┘               └────────────┬───────────────┘               └──────────────────────┘
                                                   │
                                                   │ (each Darwin's pods run here, isolated)
                                                   │
                          ┌────────────────────────┴────────────────────────┐
                          ▼                                                 ▼
                ┌────────────────────────────────────────────────────────────────────────────────┐
                │                  SHARED PLATFORM SERVICES                                      │
                │  ┌────────────────┐  ┌────────────────┐  ┌─────────────────┐  ┌─────────────┐  │
                │  │  LiteLLM Proxy │  │ HashiCorp      │  │ Egress proxy    │  │ Temporal    │  │
                │  │  - all LLM     │  │ Vault          │  │ - per-tenant    │  │ - durable   │  │
                │  │  - embeddings  │  │ - namespace    │  │   filtering &   │  │   jobs      │  │
                │  │  - image gen   │  │   per Darwin   │  │   logging       │  │ - billing,  │  │
                │  │  - video gen   │  │ - bidirectional│  │ - blocklist     │  │   harvest,  │  │
                │  │  - TTS / STT   │  │   shared vault │  │ - quotas        │  │   reproduce │  │
                │  └────────┬───────┘  └────────┬───────┘  └────────┬────────┘  └──────┬──────┘  │
                │           │                   │                   │                  │         │
                └───────────┼───────────────────┼───────────────────┼──────────────────┼─────────┘
                            │                   │                   │                  │
                            ▼                   ▼                   ▼                  ▼
                ┌────────────────────────────────────────────────────────────────────────────────┐
                │                       SHARED STATE STORES                                      │
                │  ┌─────────────────────┐  ┌─────────────────────────┐  ┌──────────────────┐    │
                │  │ Platform Postgres   │  │ Organism Postgres       │  │ Object storage   │    │
                │  │ (Aurora)            │  │ cluster                 │  │ (S3)             │    │
                │  │ - billing           │  │ - one DB per Darwin     │  │ - per-Darwin     │    │
                │  │ - audit log         │  │ - the Darwin's business │  │   prefix         │    │
                │  │ - lineage           │  │   data (content, etc.)  │  │ - audit anchors  │    │
                │  │ - escalations       │  │ - platform UI has RO    │  │ - vault backups  │    │
                │  │ - LangGraph state   │  │   access for dashboard  │  │                  │    │
                │  └─────────────────────┘  └─────────────────────────┘  └──────────────────┘    │
                │                                                                                │
                │  ┌─────────────────────┐  ┌─────────────────────────┐                          │
                │  │ Redis (ElastiCache) │  │ LanceDB                 │                          │
                │  │ - rate limits       │  │ - per-Darwin vector     │                          │
                │  │ - prompt cache      │  │   memory (lightweight)  │                          │
                │  │ - session locks     │  │                         │                          │
                │  └─────────────────────┘  └─────────────────────────┘                          │
                └────────────────────────────────────────────────────────────────────────────────┘
                                                   │
                                                   ▼
                                  ┌────────────────────────────────┐
                                  │  Observability / metrics       │
                                  │  - OpenTelemetry collectors    │
                                  │  - Honeycomb (traces)          │
                                  │  - Prometheus (metrics)        │
                                  │  - Sentry (app errors)         │
                                  │  - PagerDuty (alerts)          │
                                  └────────────────────────────────┘
```

Cross-references: [`08-platform-control-plane.md`](08-platform-control-plane.md), [`../overview/07-deployment-and-infrastructure.md`](../overview/07-deployment-and-infrastructure.md)

---

## 2. A single Darwin organism (organism perspective)

Zooming into one tenant — what runs inside one Darwin's Kubernetes namespace.

```
                          ┌─────────────────────────────────────────────────────────┐
                          │   K8S NAMESPACE: darwin-{darwin_id}                     │
                          │   NetworkPolicy: deny all except → egress proxy         │
                          ├─────────────────────────────────────────────────────────┤
                          │                                                         │
                          │   ┌─────────────────────────────────────────────────┐   │
                          │   │       ORCHESTRATOR microVM (E2B)                │   │
                          │   │       (long-lived, rotated every 12h)           │   │
                          │   │                                                 │   │
                          │   │   ┌─────────── DNA (read-only, signed) ────┐    │   │
                          │   │   │  Layer N  (sealed lessons)             │    │   │
                          │   │   │  ...                                   │    │   │
                          │   │   │  Layer 1  (sealed lessons)             │    │   │
                          │   │   │  Layer 0  (founding genome)            │    │   │
                          │   │   │  Constitution, fitness, kill switch,   │    │   │
                          │   │   │  audit logger, treasury policy         │    │   │
                          │   │   └────────────────────────────────────────┘    │   │
                          │   │                                                 │   │
                          │   │   ┌─────────── SOMA (read-write) ──────────┐    │   │
                          │   │   │  LangGraph orchestrator (running)      │    │   │
                          │   │   │  ┌──────────────────────────────┐      │    │   │
                          │   │   │  │ perceive → plan → dispatch   │      │    │   │
                          │   │   │  │     ↓        ↓        ↓      │      │    │   │
                          │   │   │  │  reflect ← propose_mutation  │      │    │   │
                          │   │   │  │            evaluate → apply  │      │    │   │
                          │   │   │  │     ↓                        │      │    │   │
                          │   │   │  │  check_death / reproduction  │      │    │   │
                          │   │   │  └──────────────────────────────┘      │    │   │
                          │   │   │                                        │    │   │
                          │   │   │  Strategies, neuron clients, memory    │    │   │
                          │   │   └────────────────────────────────────────┘    │   │
                          │   └─────────────────────┬───────────────────────────┘   │
                          │                         │                               │
                          │                         │ dispatches neuron tasks       │
                          │                         ▼                               │
                          │   ┌───── NEURON microVMs (ephemeral, per-dispatch) ──┐  │
                          │   │                                                  │  │
                          │   │   content.writer    browser.operator             │  │
                          │   │   accounts.mgr      analytics                    │  │
                          │   │   ads.manager       support.responder            │  │
                          │   │   research.scout    payments.facilitator         │  │
                          │   │   infrastructure.provisioner                     │  │
                          │   │   vault.writer                                   │  │
                          │   │                                                  │  │
                          │   │   Each gets: scoped vault creds, scratch FS,     │  │
                          │   │   egress through proxy, no DNA write access      │  │
                          │   └──────────────────────────────────────────────────┘  │
                          │                                                         │
                          └──────────────────────────────┬──────────────────────────┘
                                                         │
                                  ┌──────────────────────┼─────────────────────────────────┐
                                  │  All outbound through the platform egress proxy        │
                                  ▼                                                        ▼
                          ┌──────────────────────────┐                       ┌─────────────────────────┐
                          │   THIS DARWIN'S SLICE    │                       │   EXTERNAL WORLD        │
                          │   OF PLATFORM SERVICES   │                       │                         │
                          │                          │                       │  - LLM providers        │
                          │  - Vault namespace       │                       │    (via LiteLLM)        │
                          │    (bidirectional)       │                       │  - Browsers (via        │
                          │  - LiteLLM virtual key   │                       │    Browserbase)         │
                          │  - Coinbase Agentic      │                       │  - Affiliate networks   │
                          │    Wallet (USDC, TEE)    │                       │  - Content platforms    │
                          │  - Postgres DB(s) for    │                       │  - Ad networks          │
                          │    organism business     │                       │  - x402-enabled APIs    │
                          │  - S3 bucket prefix      │                       │  - Counterparties       │
                          │  - LanceDB instance      │                       │                         │
                          │  - LangGraph checkpoints │                       │                         │
                          │    (in platform DB)      │                       │                         │
                          │  - Event log slice       │                       │                         │
                          └──────────────────────────┘                       └─────────────────────────┘
```

Cross-references: [`01-system-overview.md`](01-system-overview.md), [`03-soma-and-orchestrator.md`](03-soma-and-orchestrator.md), [`07-isolation-and-multi-tenancy.md`](07-isolation-and-multi-tenancy.md)

---

## 3. The LLM gateway (LiteLLM) data flow

How a single LLM call from a Darwin neuron reaches a provider and comes back, with cost attribution.

```
   ┌─────────────────────┐
   │   Neuron in Darwin  │ (e.g. content.writer in a sandbox)
   │                     │
   │   tier=balanced     │
   │   prompt="..."      │
   └──────────┬──────────┘
              │
              │ HTTPS, virtual-key auth
              │ (key minted at Darwin spawn,
              │  stored in this Darwin's vault)
              ▼
   ┌─────────────────────────────────────────────────┐
   │   LiteLLM Proxy (platform-operated)             │
   │   ┌─────────────────────────────────────────┐   │
   │   │ 1. Auth virtual key                     │   │
   │   │ 2. Check per-key budget (block if over) │   │
   │   │ 3. Resolve tier → model_list[tier]      │   │
   │   │ 4. Pick model (cost-based routing)      │   │
   │   │ 5. Check prompt cache (Redis)           │   │
   │   │ 6. If miss → call provider              │   │
   │   │ 7. Record cost + tokens in PG audit     │   │
   │   │ 8. Return response with usage.cost      │   │
   │   └─────────────────────────────────────────┘   │
   └────────┬────────────────────────────┬───────────┘
            │                            │
            │ (cache hit)                │ (cache miss)
            ▼                            ▼
   ┌──────────────────────┐   ┌─────────────────────────┐
   │   Redis prompt cache │   │   Provider (Anthropic,  │
   │   (per-tenant keyed) │   │   OpenAI, Google, ...)  │
   └──────────────────────┘   └─────────────────────────┘
                                         │
                                         ▼
                              ┌─────────────────────────┐
                              │   Response, usage data  │
                              └─────────────────────────┘

                              ┌───────────────────────────────────────────┐
   Background:                │   Reconciliation worker (every minute)    │
   ──────────                 │   - reads LiteLLM PG per-key spend table  │
                              │   - writes ledger entries to platform PG  │
                              │   - tags with darwin_id, category, tier   │
                              │   - charges Darwin's USDC wallet via x402 │
                              │     at hourly settlement window           │
                              └───────────────────────────────────────────┘
```

Cross-references: [`13-llm-routing-litellm.md`](13-llm-routing-litellm.md), [`17-payments-providers.md`](17-payments-providers.md)

---

## 4. The shared vault and Darwin-owned databases

How the bidirectional vault works between user, Darwin, and platform UI. This is one of the more subtle pieces of the architecture.

```
                       ┌─────────────────────────────────────────┐
                       │  User (via Dashboard, mobile app)       │
                       └────────────────┬────────────────────────┘
                                        │
                                        │ writes:
                                        │ - KYC docs
                                        │ - existing account creds
                                        │ - payment methods
                                        │ - revoke/grant per item
                                        │
                                        │ reads (RO):
                                        │ - everything Darwin
                                        │   has written
                                        ▼
                       ┌──────────────────────────────────────────┐
                       │ Vault namespace: darwins/{darwin_id}/    │
                       │                                          │
                       │   public/                                │
                       │   scoped/                                │
                       │     content_publisher/    ← user writes  │
                       │       wordpress_creds                    │
                       │     content_publisher/    ← Darwin       │
                       │       medium_token           writes      │
                       │     infrastructure/       ← platform     │
                       │       postgres_main_dsn     writes (at   │
                       │                             provision)   │
                       │   sensitive/   ← user writes, escalation │
                       │     kyc/                    -gated reads │
                       │   platform_managed/   ← platform only    │
                       │     litellm_virtual_key                  │
                       └────────────┬──────────────────────┬──────┘
                                    ▲                      ▲
                                    │                      │
                       writes/reads │                      │ reads (RO, for UI)
                       (via AppRole │                      │ via platform AppRole
                        per neuron) │                      │
                                    │                      │
                       ┌────────────┴───────────┐  ┌───────┴─────────────────┐
                       │   The Darwin           │  │   Platform API          │
                       │   (orchestrator +      │  │   (FastAPI control      │
                       │    neurons in their    │  │    plane)               │
                       │    own sandboxes)      │  │                         │
                       │                        │  │   - serves Vault view   │
                       │   Each neuron gets a   │  │     in dashboard        │
                       │   child token scoped   │  │   - reads provenance,   │
                       │   to its capability    │  │     metadata, values    │
                       │   tag at dispatch.     │  │     where user permits  │
                       └────────────────────────┘  └─────────────────────────┘


                            DARWIN-OWNED DATABASES (separate from Vault)
                            ────────────────────────────────────────────

                       ┌──────────────────────────────────────────────────────┐
                       │  infrastructure.provisioner neuron                   │
                       │  ─────────────────────────────────                   │
                       │  Darwin requests: "I need a Postgres DB for X"       │
                       └──────────────────────┬───────────────────────────────┘
                                              │
                                              ▼
                       ┌──────────────────────────────────────────────────────┐
                       │  Platform supervisor                                 │
                       │  - creates DB in organism Postgres cluster           │
                       │  - writes DSN to Vault under scoped/infrastructure/  │
                       │  - grants Darwin's neuron role on the DB             │
                       │  - grants platform's read-only role on the DB        │
                       └──────────────────────┬───────────────────────────────┘
                                              │
                                              ▼
                       ┌──────────────────────────────────────────────────────┐
                       │  Organism Postgres cluster                           │
                       │  ┌───────────────────┐ ┌───────────────────┐         │
                       │  │ darwin_{id}_main  │ │ darwin_{id}_aux   │         │
                       │  │ - the Darwin's    │ │ - additional DBs  │         │
                       │  │   working DB      │ │   if requested    │         │
                       │  └─────────┬─────────┘ └─────────┬─────────┘         │
                       │            │                     │                   │
                       │            │ Darwin writes       │                   │
                       │            │ (full access via    │                   │
                       │            │  vault credentials) │                   │
                       │            │                     │                   │
                       │            │ Platform UI reads   │                   │
                       │            │ (RO, to render in   │                   │
                       │            │  Data browser tab)  │                   │
                       └────────────┴─────────────────────┴───────────────────┘
                                             │
                                             ▼
                       ┌──────────────────────────────────────────────────────┐
                       │  User Dashboard → Data browser tab                   │
                       │  Browse what your Darwin built: content drafts,      │
                       │  subscribers, customer records, etc.                 │
                       └──────────────────────────────────────────────────────┘
```

Cross-references: [`07-isolation-and-multi-tenancy.md`](07-isolation-and-multi-tenancy.md), [`16-secrets-and-vault.md`](16-secrets-and-vault.md)

---

## 5. The dashboard's two canvases — data flow

How the **Anatomy** and **Timeline** views are powered.

```
                                                  ┌───────────────────────────┐
                                                  │  USER                     │
                                                  │  (browser / mobile app)   │
                                                  └─────────────┬─────────────┘
                                                                │
                                                                │ websocket + HTTP
                                                                ▼
                                                  ┌───────────────────────────┐
                                                  │  Next.js dashboard        │
                                                  │  ┌─────────────────────┐  │
                                                  │  │  Anatomy canvas     │  │
                                                  │  │  (react-flow / D3,  │  │
                                                  │  │   force layout)     │  │
                                                  │  ├─────────────────────┤  │
                                                  │  │  Timeline canvas    │  │
                                                  │  │  (virtualized       │  │
                                                  │  │   horizontal feed)  │  │
                                                  │  └─────────────────────┘  │
                                                  └─────────────┬─────────────┘
                                                                │
                                                                ▼
                                                  ┌───────────────────────────┐
                                                  │  Platform API             │
                                                  │  ┌─────────────────────┐  │
                                                  │  │  /anatomy/stream    │  │
                                                  │  │  /timeline/stream   │  │
                                                  │  └──────────┬──────────┘  │
                                                  └─────────────┼─────────────┘
                                                                │
                                ┌───────────────────────────────┼───────────────────────────────┐
                                │                               │                               │
                                ▼                               ▼                               ▼
                  ┌──────────────────────────┐    ┌───────────────────────────┐    ┌───────────────────────────┐
                  │  ANATOMY DATA            │    │  TIMELINE DATA            │    │  CONTEXT DATA             │
                  │                          │    │                           │    │                           │
                  │  - Neuron registry       │    │  - Event log (PG, sliced  │    │  - Treasury balance       │
                  │    (which neurons exist  │    │    by darwin_id, indexed  │    │    (Coinbase wallet API)  │
                  │    in this Darwin's      │    │    by timestamp)          │    │  - Current strategy       │
                  │    soma)                 │    │  - LiteLLM call log       │    │    (from soma state)      │
                  │  - In-flight task graph  │    │  - Payment ledger         │    │  - Open escalations       │
                  │    (LangGraph state)     │    │  - Escalation history     │    │    (escalation tickets)   │
                  │  - Recent activity       │    │  - Mutation history       │    │  - Lineage info           │
                  │    heatmap (Honeycomb    │    │  - Reproduction events    │    │  - DNA layer manifest     │
                  │    or our own metrics)   │    │                           │    │                           │
                  └──────────┬───────────────┘    └────────────┬──────────────┘    └────────────┬──────────────┘
                             │                                 │                                │
                             └─────────────────────────────────┼────────────────────────────────┘
                                                               │
                                                               ▼
                                              ┌─────────────────────────────────────┐
                                              │  Platform Postgres + Honeycomb      │
                                              │  + per-Darwin LangGraph state       │
                                              └─────────────────────────────────────┘

Interaction:
  - User clicks a neuron in Anatomy → Timeline filters to that neuron's events
  - User clicks an event in Timeline → Anatomy highlights the responsible neuron + connection
  - Live updates: new events stream via websocket; Anatomy re-renders smoothly
  - Pause-the-stream toggle for users who want to study a moment without it moving
```

Cross-references: [`08-platform-control-plane.md`](08-platform-control-plane.md)

---

## 6. The reproduction event — sequence

What happens, in order, when a Darwin crosses the reproduction threshold.

```
     PARENT DARWIN                  PLATFORM                       NEW CHILD DARWIN
     ─────────────                  ────────                       ─────────────────

   [orchestrator]
       │
       │ (1) check_reproduction
       │     fires; fitness ≥ threshold;
       │     treasury surplus ≥ child seed
       │
       │ (2) PAUSE at safe checkpoint
       │
       │ (3) DISTILL lessons
       │     - inspect recent successes
       │     - inspect rejected mutations
       │     - generate candidate layer
       │     (structured data + small hints)
       │
       │ (4) VALIDATE candidate
       │     - syntactic checks
       │     - constitution checks
       │     - empirical sandbox check
       │
       │     POST candidate to platform ────────►
       │                                      │
       │                                      │ (5) SIGN candidate as layer N+1
       │                                      │     (signing service, HSM-backed)
       │                                      │
       │                                      │ (6) PROVISION child:
       │                                      │     - new darwin_id
       │                                      │     - new wallet (Coinbase
       │                                      │       Agentic, USDC, TEE)
       │                                      │     - new Vault namespace
       │                                      │     - new K8s namespace
       │                                      │     - fresh organism DB(s)
       │                                      │     - empty event log slice
       │                                      │
       │                                      │ (7) TRANSFER seed funds
       │     ◄──── debit parent's treasury ───┤     (PROFIT_TRANSFER_FRACTION
       │           credit child's treasury    │      × surplus above min-viable)
       │                                      │
       │                                      │ (8) WRITE lineage edge in
       │                                      │     platform DB:
       │                                      │     parent_id → child_id
       │                                      │
       │                                      │ (9) SPAWN child:
       │                                      │                                  ─► [orchestrator boots]
       │                                      │                                     │
       │                                      │                                     │ (10) load DNA
       │                                      │                                     │      - verify all N+1 signatures
       │                                      │                                     │      - load constitution
       │                                      │                                     │      - reconstruct soma from
       │                                      │                                     │        Layer 0 + lessons
       │                                      │                                     │
       │                                      │                                     │ (11) initial heartbeat
       │                                      │                                     │      perceive → plan → ...
       │                                      │                                     │
       │ (12) RESUME parent                   │ (12) AUDIT log entry: REPRODUCED    │
       │      orchestrator continues          │      (parent_id, child_id, gen,     │
       │                                      │       transferred_usdc, layer_hash) │
       │                                      │                                     │
       │ (13) NOTIFY owner                    │                                     │
       │      "Your Darwin just reproduced!"  │                                     │
```

Cross-references: [`06-evolution-and-reproduction.md`](06-evolution-and-reproduction.md)

---

## 7. The death sequence — sequence

The mirror of reproduction: what happens, in order, when a Darwin runs out.

```
   [orchestrator]
       │
       │ (1) check_death fires
       │     - treasury < minimum_viable
       │     - no revenue inbound within grace_window
       │     OR external kill flag is set
       │
       │ (2) STOP at next safe checkpoint
       │     - finish current node, halt before next
       │     - audit-log entry "DEATH_INITIATED" with reason
       │
       │ (3) REFUSE new neuron dispatches
       │     - dispatch node short-circuits with "darwin dying"
       │
       │ (4) SETTLE in-flight commitments
       │     - if abandoning costs more than completing,
       │       complete it (e.g. a small remaining LLM call)
       │     - otherwise abandon cleanly
       │
       │ (5) FINAL ledger entries
       │     - reconcile against wallet
       │     - write all pending entries
       │     - mark ledger as closed
       │
       │ (6) SNAPSHOT soma to S3
       │     - for forensic review
       │     - allows post-mortem reproduction within 30d
       │
       │ (7) WRITE final audit entry
       │     "DEATH_COMPLETE" with full state summary
       │
       │ (8) NOTIFY platform supervisor ───────────►  [supervisor]
       │                                                  │
       │                                                  │ (9) NOTIFY owner:
       │                                                  │     "Your Darwin has died"
       │                                                  │     with link to ledger replay
       │                                                  │
       │                                                  │ (10) RELEASE resources:
       │                                                  │      - terminate K8s namespace
       │                                                  │      - terminate sandboxes
       │                                                  │      - mark wallet as closed
       │                                                  │      - keep DBs, vault, event log
       │                                                  │        (organism survives for
       │                                                  │         90-day inheritance window)
       │                                                  │
       │ (11) ORCHESTRATOR exits                          │ (11) lineage entry updated:
       │                                                  │      darwin_id.status = DEAD
       │                                                  │      darwin_id.death_at = NOW
       │                                                  │      darwin_id.death_reason = ...
       │                                                  │
       │                                                  │      children (if any) continue
       │                                                  │      unaffected.
```

Cross-references: [`05-treasury-and-payments.md`](05-treasury-and-payments.md), [`09-kill-switches-and-owner-controls.md`](09-kill-switches-and-owner-controls.md)

---

## 8. The escalation flow — when a Darwin needs human help

```
   NEURON IN SANDBOX                ORCHESTRATOR               PLATFORM            OWNER
   ─────────────────                ────────────               ────────            ─────

   (1) Hits a blocker
       (captcha, KYC,
        spend approval,
        info needed)
       │
       │ (2) calls escalate()
       │     with blocker info
       ▼
   [escalate method in neuron base]
       │
       │ (3) PAUSE neuron
       │     - persist neuron state to
       │       LangGraph checkpoint
       │     - return EscalationTicket
       ▼
   ◄────────────── (4) return ticket
                                    │
                                    │ (5) ticket reaches orchestrator
                                    │     reflect node
                                    │
                                    │ (6) write ticket to platform DB
                                    │     POST /escalations to platform API ─────►
                                    │                                                │
                                    │                                                │ (7) classify ticket:
                                    │                                                │     - captcha (minutes TTL)
                                    │                                                │     - KYC (72h TTL)
                                    │                                                │     - approval (24h TTL)
                                    │                                                │     - info (7d TTL)
                                    │                                                │
                                    │                                                │ (8) NOTIFY:
                                    │                                                │     - push to mobile
                                    │                                                │     - email
                                    │                                                │     - SMS if urgent ──────►
                                    │                                                │                                  │
                                    │                                                │                                  │ (9) USER opens app,
                                    │                                                │                                  │     reviews context:
                                    │                                                │                                  │     - what the Darwin
                                    │                                                │                                  │       was doing
                                    │                                                │                                  │     - what it needs
                                    │                                                │                                  │     - cost / risk
                                    │                                                │                                  │
                                    │                                                │                                  │ (10) USER responds:
                                    │                                                │                                  │      approve / deny /
                                    │                                                │                                  │      provide data
                                    │                                                │     ◄────────────────────────────┤
                                    │                                                │
                                    │                                                │ (11) write response;
                                    │                                                │      if sensitive data,
                                    │                                                │      mint one-time
                                    │                                                │      escalation token
                                    │                                                │      in vault
                                    │                                                │
                                    │     ◄────── (12) signal orchestrator ──────────┤
                                    │            (LangGraph resume token)
                                    │
                                    │ (13) RESUME paused neuron
                                    │      with the owner's response
                                    ▼
   [neuron continues from where it paused]
       │
       │ (14) e.g. retrieves KYC doc
       │      from vault using the
       │      one-time token
       │
       │ (15) completes its task or
       │      hits another blocker

   ─────────────────────────────────────────────────────────────────────────────────────
   If TTL expires before user responds: ticket auto-resolves with deterministic outcome
     - captcha: fail (Darwin re-plans)
     - KYC: fail (Darwin escalates to "find another way")
     - approval: deny (Darwin abandons that action)
     - info: deny (Darwin uses default assumption)
```

Cross-references: [`08-platform-control-plane.md`](08-platform-control-plane.md), [`09-kill-switches-and-owner-controls.md`](09-kill-switches-and-owner-controls.md)

---

## Notes on diagram fidelity

These diagrams are reference renderings, not specifications. The reality at runtime has more retries, more error paths, more observability injections. If a diagram and a deep-dive doc disagree, the deep-dive doc wins.

When we start building, we'll generate proper diagrams (Mermaid / drawio / Excalidraw) and check them into `docs/engineering/diagrams/`. The ASCII versions stay because they're readable in a terminal and they survive any doc-rendering choice we make.
