# Platform control plane

The platform is the "habitat" that hosts Darwins. It is intentionally boring infrastructure-wise so we can spend our novelty budget on the organisms.

## The pieces

```
┌─────────────────────────────────────────────────────────────────────┐
│                           Edge / CDN                                │
│                       (Cloudflare or Fastly)                        │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
        ┌──────────────────────┼─────────────────────────────┐
        ▼                      ▼                             ▼
   ┌──────────┐         ┌────────────┐               ┌──────────────┐
   │ Web app  │         │ REST/WS    │               │ Webhooks     │
   │ Next.js  │         │ FastAPI    │               │ (Stripe,     │
   │ (dash)   │         │ (control)  │               │  MoonPay,    │
   └────┬─────┘         └─────┬──────┘               │  KYC)        │
        │                     │                       └──────┬──────┘
        └──────────┬──────────┴──────────────────────────────┘
                   ▼
         ┌──────────────────────┐         ┌─────────────────────────┐
         │  Auth & Identity     │         │  Notification bus       │
         │  (Clerk)             │         │  (Web Push / webhook)   │
         └──────────┬───────────┘         └─────────────────────────┘
                    │
                    ▼
         ┌────────────────────────────────────────────────────┐
         │           Darwin Supervisor (control)              │
         │  - lifecycle:  spawn / pause / kill / reproduce    │
         │  - quotas, anomaly detection, kill-switch          │
         │  - escalation router                               │
         │  - signing service client                          │
         └──────────┬──────────────────────┬──────────────────┘
                    │                      │
                    ▼                      ▼
         ┌─────────────────┐    ┌────────────────────────┐
         │ Kubernetes      │    │ Signing service (HSM)  │
         │ (Darwin sandbox │    │ - DNA layer signatures │
         │  hosts)         │    │ - wallet provisioning  │
         └────────┬────────┘    └────────────────────────┘
                  │
                  ▼
         (Per-Darwin: namespace → microVMs → vault → wallet → DB schema)

         ┌──────────────────────────────────────────────────────────┐
         │ Shared state (per-tenant scoped via RLS / namespacing)   │
         │   Postgres   │  Object store (S3)  │  Vault  │ Audit log │
         └──────────────────────────────────────────────────────────┘
```

## The Darwin Supervisor

The supervisor is the only component that has the right to touch a Darwin's lifecycle. Its HTTP surface is a FastAPI app built by `darwin_platform.api.build_app`, split into domain routers under `src/darwin_platform/routers/`. The lifecycle core:

```
POST   /darwins                                   → spawn
GET    /darwins/{id}                              → status
POST   /darwins/{id}/pause | /resume              → pause / resume
POST   /darwins/{id}/kill                         → kill (irreversible)
POST   /darwins/{id}/reproduce/force              → force reproduction (rare)
POST   /darwins/{id}/escalations/{eid}/respond    → owner responds to an escalation
GET    /darwins/{id}/lineage | /ledger | /events  → ancestry, ledger, audit events (cursor-paged)
WS     /darwins/{id}/stream                       → live anatomy / status / timeline feed
```

Operator-only levers include `POST /platform/darwins/{id}/quarantine`, the fleet-wide `POST /platform/fleet/{pause,resume,spend_lock,block_category,block_counterparty}`, `POST /platform/wallets/{id}/policy`, and `POST /platform/signing/rotate`. New DNA layers are signed internally at reproduction — there is no public "sign a layer" endpoint.

### Full route reference

Generated from `build_app()` (one row per path; methods merged). **Scope**: `owner` requires the owner scope, `(owns)` additionally requires that the caller owns the Darwin, `operator` requires `platform_operator` (which implies every scope), `any token` accepts any valid token, `public` needs none. `/docs`, `/redoc`, and `/openapi.json` exist only with `--enable-docs`, which is the interactive, always-current version of this table.

| Method | Path | Router | Scope |
| ------ | ---- | ------ | ----- |
| GET/POST | `/darwins` | `darwins` | owner |
| GET | `/darwins/{darwin_id}` | `darwins` | owner (owns) |
| POST | `/darwins/{darwin_id}/pause` | `darwins` | owner (owns) |
| POST | `/darwins/{darwin_id}/resume` | `darwins` | owner (owns) |
| POST | `/darwins/{darwin_id}/kill` | `darwins` | owner (owns) |
| POST | `/darwins/{darwin_id}/reproduce/force` | `darwins` | owner (owns) |
| POST | `/darwins/{darwin_id}/controls/spend_lock` | `darwins` | owner (owns) |
| POST | `/darwins/{darwin_id}/controls/reproduction_block` | `darwins` | owner (owns) |
| POST | `/darwins/{darwin_id}/controls/disabled_neurons` | `darwins` | owner (owns) |
| POST | `/darwins/{darwin_id}/controls/disabled_categories` | `darwins` | owner (owns) |
| GET | `/darwins/{darwin_id}/controls` | `darwins` | owner (owns) |
| GET | `/darwins/{darwin_id}/anatomy` | `darwins` | owner (owns) |
| GET | `/darwins/{darwin_id}/lineage` | `darwins` | owner (owns) |
| GET | `/darwins/{darwin_id}/lineage/tree` | `darwins` | owner (owns) |
| GET | `/darwins/{darwin_id}/events` | `darwins` | owner (owns) |
| POST | `/darwins/{darwin_id}/escalations/{escalation_id}/respond` | `escalations` | owner (owns) |
| GET | `/escalations` | `escalations` | owner (owns) |
| GET | `/darwins/{darwin_id}/escalations` | `escalations` | owner (owns) |
| GET | `/darwins/{darwin_id}/escalations/{escalation_id}` | `escalations` | owner (owns) |
| WS | `/darwins/{darwin_id}/stream` | `stream` | owner (owns) |
| GET | `/darwins/{darwin_id}/ledger` | `ledger` | owner (owns) |
| GET | `/darwins/{darwin_id}/ledger.csv` | `ledger` | owner (owns) |
| GET | `/darwins/{darwin_id}/ledger/summary` | `ledger` | owner (owns) |
| GET | `/darwins/{darwin_id}/billing` | `ledger` | owner (owns) |
| GET | `/platform/billing` | `ledger` | operator |
| POST | `/darwins/{darwin_id}/harvest` | `harvest` | owner (owns) |
| PUT | `/darwins/{darwin_id}/payout-address` | `harvest` | owner (owns) |
| GET/POST | `/darwins/{darwin_id}/harvest_rules` | `harvest` | owner (owns) |
| DELETE | `/darwins/{darwin_id}/harvest_rules/{rule_id}` | `harvest` | owner (owns) |
| GET/POST/DELETE | `/platform/darwins/{darwin_id}/resource_throttle` | `throttle` | operator |
| GET/POST/DELETE | `/platform/darwins/{darwin_id}/throttle` | `throttle` | operator |
| POST | `/platform/fiat/deposit_quote` | `fiat` | owner (owns) |
| POST | `/platform/fiat/confirm_deposit` | `fiat` | owner (owns) |
| POST | `/platform/fiat/withdraw` | `fiat` | operator |
| POST | `/darwins/{darwin_id}/fiat_payment` | `fiat` | operator |
| POST | `/darwins/{darwin_id}/fiat_payment/refund` | `fiat` | operator |
| POST | `/platform/kyc/start` | `kyc` | owner |
| GET | `/platform/kyc/status` | `kyc` | owner |
| POST | `/platform/kyc/webhook` | `kyc` | owner |
| POST | `/webhooks/stripe` | `webhooks` | public — provider-signed |
| POST | `/webhooks/moonpay` | `webhooks` | public — provider-signed |
| POST | `/webhooks/kyc` | `webhooks` | public — provider-signed |
| POST | `/platform/freelancers` | `freelancers` | operator |
| DELETE | `/platform/freelancers/{owner_subject}/{freelancer_id}` | `freelancers` | operator |
| GET | `/platform/freelancers/{owner_subject}` | `freelancers` | operator |
| GET/POST | `/me/freelancers` | `me` | owner |
| DELETE | `/me/freelancers/{freelancer_id}` | `me` | owner |
| GET | `/me/push-vapid-public-key` | `me` | owner |
| GET/POST/DELETE | `/me/push-subscriptions` | `me` | owner |
| POST | `/darwins/{darwin_id}/omnibus/fund` | `omnibus` | operator |
| POST | `/platform/omnibus/credit` | `omnibus` | operator |
| POST | `/platform/fleet/pause` | `fleet` | operator |
| POST | `/platform/fleet/resume` | `fleet` | operator |
| POST | `/platform/fleet/spend_lock` | `fleet` | operator |
| POST | `/platform/fleet/block_category` | `fleet` | operator |
| POST | `/platform/fleet/block_counterparty` | `fleet` | operator |
| POST/DELETE | `/platform/darwins/{darwin_id}/quarantine` | `fleet` | operator |
| GET | `/darwins/{darwin_id}/proposals` | `proposals` | owner (owns) |
| POST | `/platform/darwins/{darwin_id}/proposals/{name}/admit` | `proposals` | operator |
| POST | `/platform/darwins/{darwin_id}/proposals/{name}/reject` | `proposals` | operator |
| GET/POST/DELETE | `/darwins/{darwin_id}/vault` | `vault` | owner (owns) |
| POST | `/platform/vault/sweep` | `vault` | operator |
| GET/POST | `/platform/wallets/{darwin_id}/policy` | `wallets` | operator |
| PUT/DELETE | `/platform/wallets/{darwin_id}/preapprovals/{rule_id}` | `wallets` | operator |
| GET | `/platform/wallets/{darwin_id}/preapprovals` | `wallets` | operator |
| GET | `/darwins/{darwin_id}/browser/sessions` | `resources` | owner (owns) |
| GET | `/platform/browser/stats` | `resources` | operator |
| GET | `/darwins/{darwin_id}/resources` | `resources` | owner (owns) |
| DELETE | `/darwins/{darwin_id}/resources/{resource_id}` | `resources` | owner (owns) |
| GET | `/platform/resources` | `resources` | operator |
| GET | `/platform/capabilities` | `platform` | any token |
| GET | `/platform/money/capabilities` | `platform` | any token |
| GET | `/platform/darwins/{darwin_id}/forensics` | `platform` | operator |
| GET | `/platform/darwins/{darwin_id}/forensics/{snapshot_id}` | `platform` | operator |
| GET | `/platform/darwins/{darwin_id}/organism/tables` | `platform` | operator |
| GET | `/platform/darwins/{darwin_id}/organism/tables/{table}/rows` | `platform` | operator |
| GET | `/platform/sms/verification-code` | `platform` | operator |
| POST | `/platform/darwins/{darwin_id}/reproduce_with_amendment` | `platform` | operator |
| GET | `/darwins/{darwin_id}/alerts` | `alerts` | owner (owns) |
| GET | `/platform/alerts` | `alerts` | operator |
| POST | `/platform/signing/rotate` | `signing` | operator |
| GET | `/platform/signing/public_key` | `signing` | operator |
| GET | `/platform/audit` | `audit` | operator |
| GET | `/platform/staging/status` | `audit` | operator |
| GET | `/platform/staging/history` | `audit` | any token |
| GET | `/platform/snapshot` | `audit` | operator |
| GET | `/healthz` | `ops` | public |
| GET | `/readyz` | `ops` | public (`503` on HA followers) |
| GET | `/metrics` | `ops` | public |
| GET | `/store/{darwin_id}` | `store` | public — x402 offer |
| POST | `/store/{darwin_id}/report` | `store` | public — x402 payment (`402` without one) |

Every endpoint writes to the audit log before mutating state. The supervisor is the single writer for Darwin lifecycle transitions; nothing else has that grant.

## The escalation router

When a neuron files an escalation:

1. The neuron pauses, persisting its state to the LangGraph checkpoint.
2. An `EscalationTicket` is created in the platform DB with `darwin_id`, `neuron`, `blocker_type`, `summary`, `proposed_action`, `evidence_uris`.
3. The router classifies the escalation:
   - **Captcha-class** → ephemeral, owner has minutes to respond before timeout
   - **KYC-class** → up to 72 hours, then auto-fail
   - **Approval-class** (spend, action category) → up to 24 hours, then auto-deny
   - **Information-class** (Darwin needs an answer) → up to 7 days
4. Notification fires through the configured sinks (fan-out): W3C Web Push to the owner's installed PWA, Slack, and generic JSON webhooks; CRITICAL monitor alerts can also page PagerDuty. (An SMTP email sink exists in `observability/sinks.py` but has no CLI flag yet.)
5. The user opens the escalation in their dashboard, sees the context, and resolves: approve / deny / provide data.
6. The response is signed by the user's session and posted to the supervisor.
7. The supervisor wakes the paused neuron with the response.

Timeouts produce a deterministic outcome (typically deny + escalate to the strategy engine for re-planning) rather than a hang. A Darwin should never be silently stuck.

## Quotas and anomaly detection

The supervisor watches every Darwin for:

- **Resource consumption.** Hourly LLM token spend, egress bytes, sandbox count vs. configured cap.
- **Spend velocity.** USD/hour, and rate of acceleration.
- **Failure rate.** Neuron errors per dispatch, escalation rate.
- **Heartbeat health.** Did the main loop tick recently? If not, the orchestrator is stuck.
- **Behavioral anomaly.** Is the Darwin doing something it hasn't done before? (Useful for spotting prompt-injection takeovers.)

Thresholds trigger graded responses:

1. Log + dashboard warning.
2. Throttle the offending resource.
3. Pause and notify the owner.
4. Quarantine pending platform review.
5. Force-kill (only for confirmed policy violations or platform-safety incidents).

The supervisor's anomaly detection is itself just a service running heuristics + a small classifier. It is part of the platform, not part of any Darwin. Updates to it apply globally.

## Kill-switch propagation

There are three kill triggers:

1. **Owner kill.** User clicks "terminate" in the dashboard. Confirmation modal because this is destructive.
2. **Platform kill.** The supervisor decides a Darwin must die (policy violation, fraud, abuse, regulator request, compromised state).
3. **Self-kill.** The Darwin reaches treasury-zero and triggers its death sequence per [`05-treasury-and-payments.md`](05-treasury-and-payments.md).

For (1) and (2), the supervisor sets `darwin.kill_flag = true` in the shared state store. The Darwin's orchestrator polls this flag at the top of every main-loop iteration and on entry to every neuron dispatch. Maximum latency from flag-flip to halt is one heartbeat (typically <2 seconds). Network egress for that Darwin is cut at the proxy in parallel, so even if the orchestrator's poll missed, the Darwin cannot take new outbound actions.

See [`09-kill-switches-and-owner-controls.md`](09-kill-switches-and-owner-controls.md) for the operational detail of all three triggers and the owner/operator control surface.

## The web app

The user-facing surface is a Next.js app served from the platform edge. The two centerpiece views are the **Anatomy** and the **Timeline** — see the next subsection. Surrounding them:

- **Home / fleet.** All of the user's Darwins, alive and dead. Status, current treasury, generation, lineage tree.
- **Per-Darwin dashboard.** Live treasury chart, in-flight neurons, current strategy, recent ledger entries, recent events, open escalations. Anchored by the **Anatomy** view (right side) and **Timeline** view (bottom).
- **Anatomy canvas.** A live, animated organism diagram (see below).
- **Timeline canvas.** A horizontal chronological feed of every Darwin "movement" (see below).
- **Escalation inbox.** All pending escalations across all Darwins. Triage UI: approve / deny / provide info.
- **Lineage view.** Ancestry tree of a Darwin's family. Click any ancestor → see what they did, what lessons they passed on, why they died.
- **Wallet & deposits.** Fund a new Darwin, harvest profits, see fiat/USDC conversions.
- **Vault.** Manage credentials and KYC documents the user has shared with their Darwins, and inspect everything the Darwin itself has stored (accounts opened, API keys minted, databases provisioned). Grant or revoke per item.
- **Data browser.** Window into the Darwin's own databases (read-only). The user can browse the content the Darwin produced, the subscriber list it built, the support tickets it handled, etc.
- **Settings.** Notification preferences, harvest rules, hard caps, kill-switch keyboard shortcut.

The web app does not talk to Darwins directly. It talks to the platform API, which talks to the supervisor and to the Darwin's read-only data interfaces (LiteLLM spend table for cost data, per-Darwin DB connection via the platform's read-only AppRole for organism data, etc.).

### The two canvases

The Darwin dashboard has two centerpiece visualizations, designed to make a living organism legible to a human watching it:

**Timeline (horizontal).** A chronological feed of every "movement" the Darwin makes: reasoning steps, LLM calls (with tier, model, latency, cost), tool/neuron invocations, payments, escalations, mutations, reproductions. Think: a unified observability/trace view but designed for humans, not engineers. Each entry is expandable for full detail. Filters by neuron, tier, category. This is the **what is it doing right now** view.

The Timeline reads from the event log, with a fast indexed view per Darwin. Streaming updates over WebSocket so the user sees actions as they happen. See [`../frontend/06-timeline-canvas.md`](../frontend/06-timeline-canvas.md) and [`../frontend/07-realtime-websockets-and-caching.md`](../frontend/07-realtime-websockets-and-caching.md).

**Anatomy (organism map).** A live, animated organism diagram showing the Darwin's structural shape. The orchestrator (the "first cell") sits in the center; neurons branch out as connected cells; active neurons pulse or glow; connections between neurons show in-flight data flow. As a Darwin self-modifies and adds new strategies or specialized neurons, the anatomy grows new branches. The lineage tree (parents above, children below) is a separate but related visualization accessible from this view. This is the **what does it look like** view.

The Anatomy reads from the platform's view of the Darwin's current neuron registry, in-flight task graph (LangGraph state), and recent activity heatmap. Re-renders animate smoothly; new neurons appear with growth animations; dying neurons fade. As built it is a hand-rolled SVG component (`web/components/anatomy/`), no graph library — see [`../frontend/05-anatomy-canvas.md`](../frontend/05-anatomy-canvas.md).

Together: the **Anatomy** answers "what is this organism", the **Timeline** answers "what is it doing". The two views are linked — clicking a neuron on the Anatomy filters the Timeline to that neuron's activity; clicking an action in the Timeline highlights the responsible neuron in the Anatomy.

## Billing model

Two revenue streams for the platform:

1. **Platform fee on profit.** Configurable per plan; default 20% of harvested profit. Computed at harvest time, not on every revenue event (the user could just leave money in the treasury indefinitely, but we don't tax it until they take it out).
2. **Infrastructure pass-through with margin.** The platform absorbs the raw cost of LLM tokens, sandboxes, browser hours, etc., and charges the Darwin's treasury at a small markup. This makes the Darwin's metabolism feel like real bills, which is the metaphor, and gives us non-trivial margin on a busy lineage.

We don't charge a monthly subscription. The platform earns money when the user earns money. If every Darwin dies (which it might), we earn from the infrastructure-passthrough margin on the dying lineage's metabolism on the way down, and from the platform fee on any harvested profit before death. This aligns incentives — the platform wants Darwins to be profitable, not just numerous.

## Observability

The whole platform emits OpenTelemetry traces. Every interesting event (spawn, kill, neuron dispatch, payment, escalation, mutation proposal, reproduction) is a span. Spans are tagged with `darwin_id`, `lineage_root_id`, `generation`, and per-event metadata. `serve --otlp-endpoint` exports them over OTLP/HTTP to an OpenTelemetry collector, which forwards to Jaeger in the bundled stacks; any OTLP backend works. Metrics are Prometheus text on `/metrics` (alert rules and a Grafana dashboard ship in `infra/`).

The audit log is structurally separate from traces. Traces are sampled and rotated; the audit log is permanent and hash-chained. We do not delete audit entries even when a Darwin dies; we retain them per the platform's data-retention policy (which is set by law in each jurisdiction we operate in).

## Module layout

The `src/darwin_platform/` package is organised as bounded contexts. Each subpackage owns one concern and re-exports its public types via `__init__.py`. New code should prefer the subpackage path (`from darwin_platform.payments import BillingService`) over the top-level facade — the facade re-exports a curated subset for convenience, the subpackages are the source of truth.

| Subpackage       | Owns                                                                                                                                                         |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `runtime/`       | Per-Darwin lifecycle: `Supervisor`, `Orchestrator`, `DarwinDriver` Protocol, persistence, leader election.                                                   |
| `tenancy/`       | Multi-tenant isolation: `NamespaceProvisioner` (K8s + Vault + DB), `ProvisioningService`, `VaultAdminService`, cross-pollination.                            |
| `payments/`      | Money flow: `BillingService`, `OmnibusWallet`, `StripeRampService`, `HarvestScheduler`, sanctions screeners.                                                 |
| `security/`      | `auth` (bearer tokens + scopes), `KYCService`, `PlatformSigner` + `KMSSigningService` (Vault Transit via `HvacTransitBackend` in production), Clerk JWT verification, key rotation, vault watermarking. |
| `observability/` | `tracing`, `Monitor` + detectors, `OperatorAuditLog`, `DriftDetector`, `MerkleAnchorPublisher`, `snapshot`, notification + alert sinks.                      |
| `escalations/`   | `EscalationRouter`, `NotificationSink` Protocol, `WebPushNotificationSink` (PWA, VAPID — the wired path), legacy `FCMNotificationSink` (not wired).            |
| `evolution/`     | Phase-1 sandboxed patch staging: `PatchStagingService`, `EvalRunner`. (Not to be confused with `darwin_dna.evolution`, the genome's reproduction machinery.) |
| `infra/`         | Substrate-level resource controls: `egress_proxy` + `BandwidthQuota`, `ThrottleService`, `BrowserCacheService`.                                              |
| `adapters/`      | External-system adapters: wallet (`web3_chain`, `coinbase_cdp`, `multichain`, Solana/Stellar SDKs), Vault (`hvac_vault`, `vault_namespace`), browser (`browser`, `browser_use_cloud`, `stagehand`), `sandbox` (E2B), LiteLLM transport + admin, `x402` + facilitator, fiat ramps (Stripe, MoonPay), `twilio_sms`, `price_oracle`, `lancedb_memory`, organism DB. |
| `k8s/`           | K8s manifest builders + `K8sDarwinDriver`.                                                                                                                   |

`api.py` (the `build_app` factory), `routers/` (one module per API domain — the route table above), `client/` (the typed Python SDK), and `__main__.py` (the `darwin-platform` CLI) sit at the package root since they cross-cut every subpackage.
