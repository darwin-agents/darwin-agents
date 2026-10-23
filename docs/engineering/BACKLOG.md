# Darwin — Engineering Backlog

Audit of `docs/engineering/01–31` against the codebase. Last updated **2026-06-01**.

**~513 specified features DONE · ~69 PARTIAL · ~35 MISSING.** The Python core is
end-to-end and substantive — zero `NotImplementedError`/bare-`pass` stubs in
`darwin_dna/` or `darwin_platform/`. The web dashboard is complete (Clerk
swap, Timeline route + URL filters, Anatomy↔Timeline link, Data browser,
Playwright E2E shipped 2026-05-30). The mobile escalation companion shipped
2026-05-31 via the PWA path (Web Push + VAPID + installable manifest);
native Expo/RN remains open but not blocking. Cloud-agnostic Terraform IaC
shipped 2026-05-31 and verified end-to-end on 2026-06-01 — `infra/` brings
the full stack online on any kubectl-reachable cluster, with a turnkey
kind-based local env, and a Darwin successfully spawned through it
(`POST /darwins` → per-Darwin namespace + orchestrator pod ticking).
The verification surfaced **10 latent platform bugs in the `--driver=k8s`
path** that would have shipped broken to any production K8s deployment —
all 10 fixed in this round (+4 new unit tests). **Everything outstanding
is now process / operator commitments** (24h soak run, 7-day testnet,
drills, legal + planner-prompt reviews, etc.). A real-money onboarding
recipe (`make real` + preflight + checklist) is wired.

Legend: `[ ]` not started · `[~]` partial / in progress · `[x]` done

---

## P0 — Safety & correctness (close before any real-money or public run) — ✅ COMPLETE

- **Kill-switch fail-safe** — `KillSwitch.check()` now fails closed: any
exception polling the source trips the kill (docs 24 & 28). +5 tests.
→ `src/darwin_dna/machinery/kill_switch.py:55` *(done 2026-05-22)*
- **Outbound content-safety classifier** — `ContentSafetyGate` screens every
`content.publisher` publish (title+body): deterministic backstop + optional
platform classifier, fails closed, blocks + escalates. Doc 10. +17 tests.
→ `src/darwin_dna/machinery/content_safety.py` *(done 2026-05-22)*
- **Owner controls** — Doc 09 runtime controls. Supervisor methods + owner-scoped
HTTP routes under `/darwins/{id}/controls/*`, bridged into the genome's working
memory each heartbeat. +14 tests. *(done 2026-05-22)*
  - spend-lock — wallet refuses outbound payments; metered LLM/compute exempt
  - block-reproduction — `check_reproduction` skips natural spawn; a platform
  force still overrides
  - disable-neuron / disable-category — `dispatch` drops them from selection
  - force-compaction — resolved by design: layers are read-only at runtime (P1),
  so in-place compaction is impossible; compaction is intrinsic to reproduction
  past `generation_soft_cap` and reachable today via `force_reproduce`
- **Quarantine state persisted** — `darwin_lifecycle` gains `quarantine_*` columns
(schema v2→v3 + idempotent ALTER migration); write-through on
quarantine/unquarantine; restored (paused) on leader-resume. Doc 23. +3 tests.
→ `src/darwin_platform/runtime/persistence.py` *(done 2026-05-22)*

## P1 — Genuine code gaps (in scope, not built) — ✅ COMPLETE

- **`NeuronError` / `EscalationNeeded` exception classes** — defined in
`neurons/_base.py`; `dispatch` honours a raised `EscalationNeeded` (parks the
task, same as `NeuronResult(escalated=True)`) and `NeuronError` (non-fatal,
recurrence-tracked). Doc 28. +20 tests. *(done 2026-05-22)*
- **Per-tier LLM timeouts** — `LiteLLMTransport` resolves fast=30s / reasoning=120s
/ coding=300s per call; tunable via the `tier_timeouts` ctor arg. Doc 28. +3 tests.
→ `src/darwin_platform/adapters/litellm_transport.py` *(done 2026-05-22)*
- **Notification-sink retry** — `_safe_call` retries 3× with linear backoff
(5/10/15s), injectable sleep, tunable via `set_retry_policy`. Doc 28. +4 tests.
→ `src/darwin_platform/observability/sinks.py` *(done 2026-05-22)*
- **Postgres reconnect retry + sanctions API retry** — sanctions: 2 retries, fixed
2s on a transient 5xx/error, fail-closed; Postgres: connect/reconnect with
exponential backoff (5×, 1/2/4/8/16s) + SELECT-safe retry on a dropped
connection (writes heal-and-reraise to avoid double-apply). Doc 28. +7 tests.
*(done 2026-05-22)*
- **Fleet-wide staging quota** — already implemented: `StagingQuota(max_concurrent=3)`
(doc 21) is wired into the single fleet-shared `PatchStagingService` —
`acquire`/`release` around every evaluation, exhaustion → clean reject. The
original audit's grep missed it (a false-negative, like MoonPay).
→ `src/darwin_platform/evolution/patch_staging.py:108` *(verified 2026-05-22)*
- **Ledger CSV export** — `GET /darwins/{id}/ledger.csv` → jurisdiction-neutral
movement CSV (seq, UTC timestamp, type, direction, amount, counterparty,
category, memo). Country-specific tax-form generation (1099 / SA / EU) is
intentionally out of scope. Docs 05, 17. +7 tests.
→ `src/darwin_platform/payments/ledger_export.py` *(done 2026-05-22)*
- **gVisor/Kata `runtimeClassName` enforcement** — already implemented: the manifest
builder emits `runtimeClassName` on the orchestrator pod spec
(`DarwinPodConfig.runtime_class_name`, default "kata"; CLI `--k8s-runtime-class`),
tested by `test_deployment_runs_orchestrator_under_kata_*`. Audit false-negative.
→ `src/darwin_platform/k8s/manifests.py:431` *(verified 2026-05-22)*
- **Death-sequence settlement** — `_die` now runs doc-05 step 3: every in-flight
commitment (parked escalation, resume-queue entry, capability request) is
explicitly abandoned + audit-recorded before the final anchor; the death
notification carries the abandoned count. Doc 05. +3 tests. ("Child inherits
open revenue streams" stays out — doc 05 itself marks it TBD.)
→ `src/darwin_dna/evolution/loop.py` *(done 2026-05-22)*

## P2 — Third-party integrations (named in docs, no adapter) — ✅ COMPLETE

All 13 items resolved (2026-05-23): 8 built (fiat ramps, LiteLLM proxy, CDP
wallet, Clerk auth, Redis cache, Onfido KYC, Twilio SMS, LanceDB episodic
memory) and 5 deliberately won't-do with reasons (Modal/Northflank,
LangGraph-native, Temporal, AWS Bedrock AgentCore Payments, CloudWatch
in-app log sink).

- **Fiat ramp wiring — Stripe + MoonPay** — the `StripeFiatRamp` / `MoonPayFiatRamp`
adapters are now CLI-wired: `--fiat-provider {stub,stripe,moonpay}` +
`STRIPE_API_KEY` / `MOONPAY_API_KEY`, built in `_build_platform_services` and
passed to the Supervisor. Doc 17. +6 tests. *(done 2026-05-22)*
- **Coinbase CDP / Agentic Wallet** — `CdpChainClient` implements the genome's
`ChainClient` contract over a Coinbase CDP account (private key in Coinbase's
TEE, never in-process); `--wallet-provider cdp` + `CDP_API_KEY_*` wire it; the
async cdp-sdk is driven through an async→sync bridge. `[cdp]` extra. +16 tests.
→ `src/darwin_platform/adapters/coinbase_cdp.py` Docs 05, 07, 11, 17.
*(done 2026-05-22)*
- **LiteLLM self-hosted proxy** — `--litellm-url` / `--litellm-master-key` route
every genome LLM call through a self-hosted proxy via a `LiteLLMTransport`
factory (all 5 tiers mapped to `openai/tier-*`); covered by `test_platform_wiring`.
Per-Darwin virtual keys + a reconciliation worker are a deferred enhancement —
per-Darwin LLM cost is already enforced wallet-side. Docs 11–13. *(done 2026-05-22)*
- **Redis** — `redis` service in the Compose stack; the LiteLLM proxy is
configured for a Redis-backed response cache + cross-replica rate limits
(`litellm.config.yaml` `cache_params`). Service + config, not a Python
adapter — the platform's own Python has no Redis need (single-writer
supervisor; leader election uses Postgres advisory locks; "session locks"
from the doc aren't required). Docs 11, 13, 19. *(done 2026-05-22)*
- **AWS Bedrock AgentCore Payments** — **Won't do (decision 2026-05-22)** — AgentCore
Payments is real (launched late 2025, Coinbase + Stripe under the hood) but
*redundant and AWS-coupled*: it would only re-wrap Coinbase and Stripe, both
already integrated directly, behind an AWS dependency the project avoids
(solo-dev, lean budget, provider-neutral stack). The multi-rail payment stack
(on-chain USDC, x402, Solana/Stellar, Stripe/MoonPay) is complete. Docs 05, 11,
17 updated to descope it.
- **Clerk auth** — `ClerkVerifier` verifies Clerk RS256 JWTs against the JWKS;
`ClerkTokenStore` (drop-in for the `TokenStore` Protocol) maps claims → scopes
and keeps per-user Darwin ownership keyed by Clerk user id; `--clerk-issuer` /
`--clerk-jwks-url` wire it. `pyjwt[crypto]` added to `[platform]`. +19 tests.
→ `src/darwin_platform/security/clerk.py` Docs 08, 11, 22. *(done 2026-05-22)*
- **Onfido KYC** — `OnfidoKYCProvider` (Onfido v3.6 Studio-workflow API skeleton)
implements the same `KYCProvider` Protocol as Persona; `--kyc-provider onfido`
`ONFIDO_API_TOKEN` / `ONFIDO_WORKFLOW_ID` wire it (eu/us/ca regions). +11 tests.
→ `src/darwin_platform/security/kyc.py` Docs 11, 16. *(done 2026-05-22)*
- **Temporal** — durable billing/harvest/reproduce jobs. **Won't do (decision
2026-05-22)** — the platform's durability need is already met by Postgres
persistence + leader-resume + cursor-idempotent maintenance sweeps; these
jobs are incremental idempotent sweeps, not complex sagas. Temporal would add
a mandatory cluster + a second orchestrator for no behaviour gain. Docs 11, 19.
- **Modal / Northflank** — secondary sandbox runtimes. **Won't do (decision
2026-05-22)** — E2B covers per-neuron sandboxing; a secondary runtime is only
for GPU/batch work Darwin doesn't do yet. Doc 14.
- **Twilio** — `TwilioSmsClient` is the platform's SMS 2FA inbox: reads inbound
texts to a platform-owned Twilio number and extracts the verification code.
`--twilio-*` flags build it; `GET /platform/sms/verification-code` (operator
scope) reads codes to answer `accounts.manager` 2FA escalations. Doc 15.
+13 tests. → `src/darwin_platform/adapters/twilio_sms.py` *(done 2026-05-22)*
- **PagerDuty + CloudWatch** — PagerDuty: `PagerDutyAlertSink` already exists
(`observability/sinks.py` — Events v2, severity mapping; audit false-negative).
CloudWatch: **won't do as app code** (decision 2026-05-22) — in-app log shipping
is an anti-pattern; stdout → cluster log agent → CloudWatch is the standard, an
infra/Helm concern folded into the P3 Terraform/EKS item. Doc 22.
- **Real LanceDB client** — `LanceDbEpisodicMemory` (embedded LanceDB +
injected embedder) implements the same surface as the genome's
deterministic `EpisodicMemory`. A new injection seam on `Platform` —
`platform.episodic_memory` — lets the supervisor swap a per-Darwin
vector store in for the deterministic default *without touching the DNA*
(sim + cross-engine parity untouched when no factory is configured).
Wired through Supervisor `episodic_memory_factory` and CLI
(`--episodic-memory lancedb`, `--lancedb-path`, `--lancedb-embedding-model`,
`DARWIN_LANCEDB_PATH`); embeddings via `litellm.embedding` (routed through
`--litellm-url` when set). `[lancedb]` extra. +12 tests (10 adapter + 2
injection-seam). Docs 03, 11, 19. *(done 2026-05-23)*
- **LangGraph native features** — **Won't do (decision 2026-05-22)** — the custom
`PersistentCheckpointStore` + state-routing works and is fully tested;
converting to LangGraph-native (`PostgresSaver`, `interrupt()`, time-travel) is
churn with no behaviour gain. Doc 12.

## Provider-neutral browser stack — ✅ COMPLETE

- **Browser Use Cloud adapter** — `BrowserUseCloudClient` alongside the
existing `BrowserUseClient(Browserbase)` so the operator picks at deploy
time. Same `BrowserClient` Protocol; CLI `--browser-provider
{browserbase,browser-use-cloud}` (+ `--browser-use-api-key`,
`--browser-use-model`, `--browser-use-max-cost-usdc`); spend cap is
both pre-debited to the wallet AND passed to the SDK as `max_cost_usd`
so cloud billing matches the ledger debit. Optional BU custom model.
`[browser-use-cloud]` extra. +11 tests. Docs 15, 31. *(done 2026-05-25)*

## P3 — Frontend & infrastructure

- **Next.js web dashboard** — Done. `web/` (Next 15.5 / React 19.2 /
Tailwind v4 / shadcn). Routes live: fleet overview (`/`), spawn wizard
(`/darwins/new`), per-Darwin shell (`/darwins/[id]`) with sub-nav for
Anatomy / Timeline / Lineage / Billing / Pre-approvals / Payments / Vault /
Resources / Data / Settings, fleet Escalations inbox (`/escalations`),
owner self-serve Freelancers registry (`/freelancers`), Token paste
(`/tokens`), Clerk sign-in/sign-up (`/sign-in`, `/sign-up`). WS stream
(`DarwinStream`) layered on top of REST snapshots: anatomy canvas updates
live, event timeline tails with `?since=`. Owner controls (spend-lock,
disable-neuron, force-reproduce, kill), respond drawer, vault put/get,
deprovision, fiat deposit/withdraw, harvest dialog, Darwin fiat-payment
console (send / refund / retry omnibus fund), pre-approval rule manager
(install / revoke / consume-counter view) all wired. Codegen
from FastAPI OpenAPI typed against `web/lib/api.generated.ts`.
The four remaining M3 items shipped 2026-05-30:
  - **Clerk swap-in** — `@clerk/nextjs` wired with feature-detect on
  `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`. When set, `clerkMiddleware` gates
  every route and redirects to local `/sign-in`; `getAuthToken()` reads
  `window.Clerk.session.getToken()`. When unset, the dev token-paste flow
  stays active (Playwright + `--auth-tokens` boot). Sidebar shows
  `<UserButton>` + email in Clerk mode. Backend already accepts Clerk
  RS256 JWTs (`security/clerk.py`).
  - **Timeline route + URL filters** (M3) — `/darwins/[id]/timeline` with
  debounced filter bar (`?kind=`, `?neuron=`, `?tier=`) routed straight
  to the existing backend `kind` / `neuron_id` / `tier` query params.
  Any view is shareable as a link.
  - **Anatomy ↔ Timeline link** (M3-D) — Anatomy side-panel "View
  timeline for `<neuron>`" navigates to filtered timeline; reverse,
  expanded timeline rows with `neuron_id` show a "Show in anatomy"
  link to `/anatomy?selected=<neuron>` which pre-selects the node.
  - **Data browser** (M3-E) — `/darwins/[id]/data` with tabs over
  existing read-endpoints (lifecycle / lineage / ledger / audit). Tab
  + pagination persist in URL. Audit rows are click-to-expand JSON.
- **Playwright E2E** — Done 2026-05-30. `web/playwright/` with 4 dev-token
specs (fleet renders, timeline filter URL round-trip, anatomy→timeline
deep-link, data browser tab switch) + 1 Clerk-gate spec (middleware
redirect). Mocked backend via `page.route()` so no `darwin-platform serve`
boot needed. Local: 5/5 PASS in 1.1 min. CI gate: new `e2e` job in
`.github/workflows/web.yml`, depends on `build`, caches Chromium binary,
uploads HTML report always + traces on failure.
CI gate in `.github/workflows/web.yml` covers
lint/typecheck/test/build/e2e. Docs 01, 08, 17, 19, 20, 22. *(Stage 1
done 2026-05-27; payments routes added 2026-05-29; M3 completion +
Playwright E2E 2026-05-30)*
- **Mobile escalation app — done via PWA** *(2026-05-31)*. Doc 22 names
"Expo / React Native (or progressive web app)"; the PWA path closes the
mobile gap without a parallel codebase. Folded into `web/`:
manifest + service worker + push handler + `/notifications` opt-in
page; backend `WebPushNotificationSink` (VAPID, pywebpush) +
persistent `push_subscriptions` table (schema v11) + owner-scoped
`/me/push-vapid-public-key` + `/me/push-subscriptions` GET/POST/DELETE
endpoints; CLI flags `--push-provider webpush` /
`--vapid-public-key` / `--vapid-private-key` / `--vapid-subject`. The
sink is hydrated from the persistent store on Supervisor boot and a
410-purge deletes the row, so subscriptions survive restart. +22 backend
tests (sink + persistence + HTTP) + 3 web vitest tests for the
base64url helper. Native Expo/RN remains open as a future iteration —
see `memory/project_mobile_pwa.md` for the rationale. Docs 19, 22.
- **Terraform IaC — done cloud-agnostic** *(2026-05-31)*. Doc 22 names
EKS / Aurora / AWS-Vault explicitly; we replaced each with a
self-hostable equivalent so the same modules run on a laptop (kind),
self-hosted k3s, DOKS, GKE, AKS — anything `kubectl` reaches. Layout:
  - `infra/terraform/envs/local/` — turnkey local stack via `make -C infra/terraform up`.
  - `infra/terraform/modules/kind-cluster/` — 1 control + 2 worker kind nodes
  with `extraPortMappings` 80→8000, 443→8443 + the `ingress-ready`
  label so ingress-nginx lands on the host-mapped node.
  - `infra/terraform/modules/cnpg/` — CloudNativePG operator + a Postgres
  `Cluster` CR; outputs the connection-string Secret name.
  - `infra/terraform/modules/vault/` — HashiCorp Vault Helm release in dev mode
  (in-memory, auto-unsealed) with a generated root token.
  - `infra/terraform/modules/vault-prod/` — Vault HA + raft (3 replicas,
  persistent volumes); used by `envs/hetzner` via `platform-stack`
  `vault_mode=raft`. **Manual unseal** — no KMS auto-unseal yet.
  - `infra/terraform/modules/platform-stack/` + `k8s-addons/` — shared
  composition (local + cloud); ingress + optional cert-manager TLS.
  - `infra/terraform/envs/hetzner/` — Hetzner k3s + gVisor + production
  deploy path; CI: `.github/workflows/deploy-hetzner.yml`.
  - `make dev` / `scripts/dev-up.sh` — kind + image load + dashboard.
  - `infra/terraform/modules/litellm/` — proxy Deployment + Service +
  ConfigMap (mounts the existing `infra/docker/litellm.config.yaml`) +
  Secret for provider keys.
  - `infra/terraform/modules/darwin-platform/` — supervisor Deployment +
  Service + Ingress + Recreate strategy + a `ClusterRole` granting
  the K8s driver the per-Darwin RBAC it needs (namespaces +
  configmaps / secrets / PVCs / services / deployments /
  networkpolicies, scoped cluster-wide because per-Darwin namespaces
  don't exist at plan time).
  - `infra/terraform/Makefile` — `up`, `down`, `kubeconfig`, `logs`, `pods`,
  `port-forward`. `make up` is `terraform apply -auto-approve`.
  - `infra/terraform/README.md` — design rationale + quickstart.
  
  AWS / GCP / Azure cluster modules deliberately omitted; the platform
  layer is provider-neutral, so adding a managed-K8s wrapper later is
  one new sibling under `modules/` that outputs the same
  `endpoint` / `cluster_ca_certificate` / `client_*` shape kind does.
  Provider config uses inline credentials from the kind module's
  outputs (not file-based kubeconfig) to dodge the plan-time
  bootstrap chicken-and-egg; `alekc/kubectl` has `lazy_load = true`
  so the provider defers connection until apply. Terraform 1.6+,
  validates clean, full `plan` is 21 resources, no orphan
  references. Docs 20, 22.

## P4 — Process / non-code commitments

- [~] **24h soak in CI** — harness committed (`tools/soak/run_soak.py` +
`tools/soak/assertions.py` + `tools/soak/config.py`, 21 unit tests in
`tests/integration/test_soak_assertions.py`). End-to-end 60-second CI smoke
wired in `.github/workflows/soak-smoke.yml` — exercises the same driver +
six acceptance criteria the real 24h gate uses; verified locally (6/6 PASS,
0.07s kill latency). The *real* 24-hour gate remains an operator-run
process commitment against a long-lived dev platform. Docs 20, 25.
*(harness + smoke done 2026-05-25)*
- **`docs/postmortems/` directory** — created with a README pointing to the
doc-29 template. Intentionally empty until the first real incident.
*(done 2026-05-25)*
- [~] **7-day testnet soak run** — Layer-0 exit criterion. Tooling now in place
to start the clock: `make real NETWORK=testnet` boots against real
LLM/CDP-testnet/Browserbase/E2B; `make real-checklist` runs the three
pre-soak behaviours (tick+spend, pause-respect, kill-cleanup) against a
$1 spawn. The wall-clock 7-day soak itself remains an operator-run
process commitment. Docs 18, 20.
- **Outside reviewer of the planner system prompt**. Doc 18.
- **Legal/compliance review of the constitution**. Doc 18.
- [~] **Quarterly safety reviews · external security audit · red-team
suite as a CI release gate**. CI release gate **done 2026-05-25**:
`.github/workflows/release-gate.yml` runs `pytest -m redteam` (18 tests
covering doc-24's eight adversarial families) on every push to
`release/**` or `v*` tag. Quarterly reviews + external audit remain
process commitments. Docs 10, 22.
- [~] **Operational drills** — kill-switch, DR failover, Vault seal/unseal.
**Drill scripts done 2026-05-25**: `tools/drills/{kill_switch,dr_failover,vault_seal}.py`
+ README. Kill-switch locally verified (PASS, 0.21s latency). DR + Vault
drills code-reviewed but not locally exercised (need Postgres + Vault
respectively) — first operator run is the test. Doc 09.
- [~] **Real economic activity** — Darwin earns ≥ $1 USDC live. Onboarding
recipe shipped (`make real`, `make real-spawn`, `make real-checklist`),
preflight gates a misconfigured boot, mainnet preflight refuses if RPC
URL still names a testnet. The earning itself is a process commitment
against a live Darwin. Doc 21.

## Doc-drift cleanup (code is fine — fix the docs) — ✅ COMPLETE

- **Doc 25** — metric table replaced with the real `darwin_platform_*` names +
a pointer to `/metrics` as authoritative; inline metric refs corrected;
`/platform/signing/public-key` → `public_key`. *(done 2026-05-22)*
- **Doc 24** — red-team gate checklist + suite references repointed to
`tests/redteam/`, `tests/property/`, `tests/integration/`. *(done 2026-05-22)*
- **Doc 34** (evolution tuning guide, formerly 30) — calibration example uses `apply_calibration(sim_world=, revenue_model=, dataset=)` and `runtime.archive`. *(done 2026-05-22)*
- **Doc 05** — MoonPay confirmed implemented (`MoonPayFiatRamp`,
`src/darwin_platform/adapters/fiat.py:344`); scoreboard already corrected.

---

## Shipped since 2026-06-01 — infra/ end-to-end verification + 10 latent platform bugs fixed

The cloud-agnostic Terraform IaC landed 2026-05-31, but a real spawn was
never exercised through it. This pass closed that gap: `terraform apply`
on a fresh box, build + load the supervisor image, `POST /darwins`, watch
the K8s driver provision a per-Darwin namespace + orchestrator pod, and
confirm the orchestrator is ticking. The path surfaced **ten bugs in
the platform's `--driver=k8s` code path** — every one would have shipped
silently broken to any operator running the production deployment shape.

**Verification result**

Stack: 6 platform-layer pods (`darwin-platform`, `darwin-platform-db-1`,
`cnpg-cloudnative-pg`, `vault-0`, `litellm`, `ingress-nginx-controller`)
all 1/1 Running. `POST /darwins` → HTTP 201 returns a snapshot with
`alive: true` and `tick: 48046+` within 2 minutes; namespace
`darwin-<id>` contains the full per-Darwin resource graph (Deployment +
Pod + Service + ConfigMap + Secret + NetworkPolicy).

**Platform bugs fixed (all in `src/darwin_platform/`)**

1. **`runtime/persistence.py` Postgres SQL splitter is `;`-naive** —
`_PgBackend.executescript` splits on raw `;` without comment-awareness.
Four schema comments (omnibus, harvest, fiat, push_subscriptions)
contained `;` and broke supervisor boot on Postgres. Reworded the
four comments. Splitter itself is still naive — a follow-up should
make it strip `--` comments before splitting; documented inline so
future schema edits don't trip on it.
2. **`k8s/manifests.py` orchestrator pod missing `seccompProfile`** —
the namespace is labeled `pod-security.kubernetes.io/enforce:
restricted` (correctly, doc 22) but the pod manifest's pod-level
securityContext didn't set `seccompProfile`. Every spawn failed
admission with "violates PodSecurity restricted:latest". Added
`seccompProfile: { type: RuntimeDefault }` to the pod-level context.
3. **`__main__.py` `lancedb_image=getattr(...) or "lancedb/lancedb:latest"`
swallows empty-string override** — operators can't disable the LanceDB
sidecar even when the image isn't published anywhere. Changed to
`getattr(default=...)` so `--k8s-lancedb-image=""` reaches the
manifest builder unchanged.
4. **`k8s/manifests.py` always emits the LanceDB sidecar** — even when
the image is empty/missing. Made the sidecar conditional on
`bool(config.lancedb_image)`; the orchestrator falls back to the
deterministic in-memory EpisodicMemory when disabled.
5. **`k8s/manifests.py` references `darwin_platform.orchestrator`
(module doesn't exist)** — the per-Darwin pod's CMD was
`python -m darwin_platform.orchestrator`, but the actual module is
at `darwin_platform.runtime.orchestrator`. Every spawn's
orchestrator container exited with `ModuleNotFoundError`. Fixed
the module path.
6. **`runtime/orchestrator.py` imports `__main__` one package level
too low** — `from . import __main__ as cli_main` looks under
`darwin_platform.runtime/`, but `__main__` lives at
`darwin_platform/`. Changed to `from .. import __main__`.
7. **`__main__.py` doesn't forward `--wallet-provider` to
`DarwinPodConfig`** — the supervisor's choice is read from `args`,
but the pod config uses the dataclass default (`"cdp"`) regardless.
Every per-Darwin pod started with `cdp` which required CDP API
keys, blocking spawns. Forwarded the choice into `DarwinPodConfig`.
8. **`DarwinPodConfig.supervisor_namespace` hardcoded to `"default"`** —
the NetworkPolicy on per-Darwin namespaces uses
`supervisor_namespace` to allow inbound from "the supervisor pod".
With the default value, any supervisor running outside the
`default` namespace (which is the normal deployment shape) couldn't
reach its own Darwins for readiness probing → 100% spawn timeout.
Read `POD_NAMESPACE` env (downward API) in `__main__.py`; the infra
module wires it via `valueFrom.fieldRef metadata.namespace`.
9. **K8s driver doesn't forward substrate URLs/keys to per-Darwin pods** —
the supervisor's `DarwinPodConfig` had an `extra_env` seam but
`_build_k8s_driver` never populated it. Result: every spawned
orchestrator ran against deterministic fakes for LLM + Vault even
when the supervisor pod had real-substrate config in its own env.
`__main__.py` now reads `DARWIN_POD_LITELLM_URL` / `_KEY` /
`_VAULT_ADDR` / `_VAULT_TOKEN` from its own env and emits them on
each per-Darwin pod as the env vars the orchestrator's
`_build_real_factories` already looks for (`DARWIN_LITELLM_URL`,
`DARWIN_LITELLM_MASTER_KEY`, `DARWIN_VAULT_URL`,
`DARWIN_VAULT_TOKEN`). +1 unit test on `manifests.py` covering the
end-to-end forwarding through `extra_env`.
10. **`_PgBackend.executescript` was `;`-naive** — split on raw `;`
without comment-stripping. Any future schema comment containing
`;` would silently break Postgres deployments (SQLite tolerates it
via its own splitter). Extracted a module-level
`_split_sql_statements(script)` helper that strips `--` line
comments before splitting; `executescript` now delegates to it.
+3 unit tests covering the splitter contract.

**Infra side**

- `infra/terraform/envs/local/litellm.config.yaml` — env-specific config with
the Redis cache section stripped (the local stack doesn't deploy
Redis, and LiteLLM's cache init blocks for ~minutes against
unreachable Redis).
- `kubectl_manifest.kata_shim` in `envs/local/main.tf` — installs a
`RuntimeClass` named `kata` whose handler is `runc`, because kind
doesn't ship kata-containers but the platform manifests hard-default
`runtimeClassName: kata`. Production swaps for a real kata install.
- `modules/darwin-platform/main.tf` — added the `POD_NAMESPACE`
downward-API env var on the supervisor Deployment; added the
`--k8s-orchestrator-image=darwin-platform:dev` arg and
`--k8s-lancedb-image=""` arg so the supervisor forwards the locally-
loaded image and skips the unbuildable sidecar.
- `modules/darwin-platform/variables.tf` — new `orchestrator_image`
variable.
- `infra/terraform/Makefile` + `infra/terraform/README.md` — documented inotify prerequisite
(`fs.inotify.max_user_instances=512`), updated kind version requirement
(≥ 0.24 for cgroup v2 hosts).

**Docker / image**

- `infra/docker/Dockerfile` — added `k8s,cdp,webpush,lancedb` to the uv
install extras. Without them the supervisor crashes on import as
soon as the K8s driver / CDP / push sink / Vector store is wired.

**State after this pass**

- `make -C infra/terraform up` brings up a 6-pod cluster end-to-end
- `curl -X POST http://localhost:8000/darwins -d '{"opening_usdc":1.0}'`
returns HTTP 201 with a live, ticking Darwin
- A 9th per-Darwin pod appears in the cluster; `GET /darwins`
includes the new id; `GET /darwins/<id>` reports tick growth in
real time

Want to repeat the verification on a non-kind cluster (DOKS, k3s on a
VPS, etc.)? The same modules deploy unchanged — only the cluster
module is provider-specific, and the platform layer auto-detects its
own namespace via the downward API now.

---

## Shipped since 2026-05-31 — mobile escalation companion (PWA path)

Doc 22 names the mobile companion as "Expo / React Native (or progressive
web app)". The PWA branch ships everything the headline feature needs —
notify-on-escalation + respond inline — without spinning up a parallel
mobile codebase. Native Expo/RN deferred (see
`memory/project_mobile_pwa.md`).

**Backend — `WebPushNotificationSink` + persistence**
- New `src/darwin_platform/escalations/notification_webpush.py`:
`WebPushNotificationSink` implements the same `NotificationSink`
Protocol as the FCM sink, but over W3C Push API + VAPID (RFC 8030 +
RFC 8292). One `notify(ticket)` call fans out a VAPID-signed,
ECE-encrypted POST per registered subscription. 404/410 responses
auto-purge the dead subscription from both the in-memory index and
(via callback) the persistent store. Production transport
`PyWebPushTransport` wraps `pywebpush` (added as `[webpush]` extra in
pyproject); tests use a stub transport that records calls.
- New `push_subscriptions` table (schema v11 bump) with
`PersistentPushSubscriptionStore` next to the freelancer + preapproval
stores. `register` upserts on `(owner_subject, endpoint)` so a browser
re-subscribing with new keys replaces the row. `load_into_sink(sink)`
hydrates the live sink on supervisor boot.
- 12 new sink tests + 5 new persistence tests cover construction,
register/revoke, owner-without-subs failure, 4xx vs 410, transport
exceptions, bulk-load, stats. Restart survival end-to-end.

**Backend — endpoints + CLI wiring**
- Supervisor gained `register_push_subscription`,
`revoke_push_subscription`, `list_push_subscriptions`,
`push_vapid_public_key`. Write-through to the persistent store +
mirror to the live sink so a fresh registration lights up before the
next ticket without a restart. Sensitive endpoint URL is redacted to
`scheme://host` on list responses + audit-log entries — full URL
never leaves the persistent store.
- Four new owner-scoped routes in `api.py`:
`GET /me/push-vapid-public-key` (503 when push isn't configured),
`POST /me/push-subscriptions` (201), `GET /me/push-subscriptions`,
`DELETE /me/push-subscriptions?endpoint=…` (query param chosen over
DELETE-with-body since many proxies strip the body).
- CLI flags `--push-provider {none,webpush}`, `--vapid-public-key`,
`--vapid-private-key` (PEM string or file path), `--vapid-subject`,
plus matching env vars `DARWIN_VAPID_*`. `_build_notification_sink`
fans the WebPush sink in alongside Slack / webhook sinks. Half-
configured VAPID material refuses to start.
- `Supervisor.__init__` auto-wires the persistent store ↔ sink: hydrates
the sink from disk + installs the 410-purge callback. Duck-typed so
the [webpush] extra is optional. New integration test exercises the
full register → list → revoke → restart-rehydrate path through
ASGI Transport.

**PWA — manifest + service worker + subscription UI**
- New `web/public/manifest.webmanifest` (Darwin / standalone /
`#04080F` theme, 192 + 512 SVG icons) + manifest link wired through
the root layout `Metadata`. iOS / Android "Add to home screen" installs
the dashboard as a standalone app.
- New `web/public/sw.js`: `push` listener decodes the JSON payload the
backend sink posted and calls `showNotification`; `notificationclick`
focuses an existing tab or opens a fresh one at the escalation deep
link; `pushsubscriptionchange` (Mozilla autopush rotation) posts a
message back to the page so it can re-publish.
- New `web/components/pwa/service-worker-registrar.tsx`: a tiny
client-only component the root layout mounts to register `/sw.js` on
load (idempotent — browser dedupes by scope).
- New `web/lib/push.ts` helpers: `isPushSupported`,
`getPushPermissionState`, `urlBase64ToUint8Array` (the VAPID
public-key decoder, +3 vitest tests), `ensurePushSubscription`,
`getCurrentSubscription`, `unsubscribePush`. Reuses any existing
subscription instead of bouncing the user through permission UX.
- New `/notifications` page: "Enable notifications" CTA → permission
prompt → `pushManager.subscribe(applicationServerKey)` →
`POST /me/push-subscriptions`; "Registered devices" list shows
`endpoint_origin` + user agent + date for every signed-in device;
"Disable on this device" path unsubscribes locally + DELETEs the row.
Sidebar gained a "Notifications" entry (Bell icon).
- `web/lib/api.ts` extended with four typed methods +
`web/lib/types.ts` DTOs + `web/lib/queryKeys.ts` keys. The DELETE
uses `?endpoint=…` because the existing fetch wrapper drops DELETE
bodies (proxy-friendly).

**Test totals delta**
- Backend: +22 tests (12 sink + 5 persistence + 5 HTTP integration).
72 platform/escalation/persistence tests pass via
`uv run --all-extras pytest`. 0 regressions in adjacent suites.
- Web: +3 vitest tests (24 → 27 passing). `pnpm typecheck` +
`pnpm lint` + `pnpm build` clean. Build adds `/notifications`
(12.3 kB) + sign-in/up routes already shipped.

---

## Shipped since 2026-05-30 — web dashboard M3 completion

The four remaining web dashboard items from the M3 milestone landed in
one pass, closing the last `[~]` row in P3. The Python core, the
dashboard, and the CI gates around them are now all green.

**Clerk swap-in**
- New `lib/auth-mode.ts` build-time flag `CLERK_ENABLED` from
`NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`. `lib/auth.ts::getAuthToken()`
prefers `window.Clerk.session.getToken()` (with ~2s wait for the SDK to
load) and falls back to the dev token-paste flow when Clerk is
unconfigured — Playwright + `--auth-tokens` boot keep working unchanged.
- New `middleware.ts`: `clerkMiddleware` + `redirectToSignIn()` on
unauthenticated requests (chosen over `auth.protect()` because the
latter returns a 404 to "hide existence" — wrong UX for a dashboard).
No-op when `CLERK_ENABLED=false`.
- New local routes `/sign-in/[[...rest]]` and `/sign-up/[[...rest]]`
rendering Clerk's `<SignIn>` / `<SignUp>` (dark theme via `@clerk/themes`).
- New `providers/auth-provider.tsx` dispatches between `<ClerkProvider>`
and the existing `<DevAuthProvider>`. Root layout wraps once.
- Sidebar gained an `<AuthChip>` (`<UserButton>` + signed-in email) when
Clerk is on.
- `.env.example` documents the four `NEXT_PUBLIC_CLERK_*_URL` /
`*_FALLBACK_REDIRECT_URL` hints so Clerk uses local routes instead of
its hosted `accounts.dev` portal.
- Bumped `next` 15.1 → 15.5 and `react` / `react-dom` 19.0.0 → 19.2 to
satisfy `@clerk/nextjs ^7.4` peer ranges.

**Timeline route + URL filters (M3)**
- New `/darwins/[id]/timeline` page reads `?kind=`, `?neuron=`, `?tier=`
from the URL and hands them to `EventTable`.
- New `components/timeline/filter-bar.tsx`: debounced (350ms) text
inputs + tier dropdown that push back via `router.replace()`. Exports
`parseFilters(URLSearchParams)` so the route and bar share one source
of truth. +3 vitest tests.
- `components/timeline/event-table.tsx` extended with optional `filters`
prop (`{kind, neuron_id, tier}`) forwarded to all three `api.events()`
callsites (head, tail-poll, loadOlder). Filter changes reset the
window + cursors. Default = no filter, so the inline shell render is
unchanged.
- Darwin sub-nav gained Timeline entry (Clock icon).

**Anatomy ↔ Timeline link (M3-D)**
- Anatomy side-panel: "View timeline for `<neuron>`" link →
`/darwins/[id]/timeline?neuron=<id>`.
- Anatomy reads `?selected=<neuron>` from URL to pre-populate the
selected node — round-trips with the timeline reverse direction.
- Timeline event rows with a `neuron_id`: expanded row shows a
"Show in anatomy" link → `/darwins/[id]/anatomy?selected=<neuron>`.

**Data browser (M3-E)**
- New `/darwins/[id]/data` page with four tabs over existing platform
read-endpoints: lifecycle (`GET /darwins/{id}`), lineage
(`GET /darwins/{id}/lineage/tree`), ledger (`GET /darwins/{id}/ledger`),
audit chain (`GET /darwins/{id}/events`). Active tab persists in
`?tab=`, pagination persists in `?ledger_offset=` / `?audit_offset=`.
Audit rows are click-to-expand JSON.
- Darwin sub-nav gained Data entry (Database icon).

**Playwright E2E**
- New `web/playwright/` directory + `playwright.config.ts` with two
projects: `dev-token` (always runs, full coverage) and `clerk-gate`
(runs only when `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` is set; asserts
the auth gate redirects). webServer commands inline env so `next dev`
sees the right `CLERK_ENABLED` value at request time.
- Fixtures (`fixtures.ts`): `signIn(page)` seeds the dev token in
localStorage and waits for "Loading…" placeholder to disappear (Next
hydration done). `mockBackend(page, handlers)` intercepts
`${API_BASE}/**` with longest-prefix matching.
- 4 dev-token specs (fleet, timeline filters, anatomy→timeline,
data-browser tabs) + 1 Clerk-gate spec + 1 warmup spec that pre-compiles
hot routes on the cold dev server so the first real test doesn't pay
the 30-60s compile-and-hydrate hit.
- Local: 5/5 passed in 1.1 min. Warmup eats 20s; each real test runs
in 4-9s.
- New CI job `e2e` in `.github/workflows/web.yml` depending on `build`,
caches Playwright browsers by lockfile hash, installs Chromium, runs
the `dev-token` project, uploads `playwright-report` always +
`test-results/` traces/videos on failure for 7 days. Retries=2 in CI
absorbs the rare first-compile flake; `workers: 1` keeps the shared
dev-server warmup honest.
- `package.json` scripts: `test:e2e`, `test:e2e:ui`.

**Test totals delta**
- Web vitest: +3 tests (24 → 27 passing). `pnpm typecheck` + `pnpm lint`
+ `pnpm build` clean. Build adds 4 routes (`/sign-in/[[...rest]]`,
`/sign-up/[[...rest]]`, `/darwins/[id]/timeline`, `/darwins/[id]/data`)
and an 88 kB middleware bundle.
- Web Playwright: +5 specs (new), 5/5 local PASS.

---

## Shipped since 2026-05-28 — payments completion arc

End-to-end pass on the payments story: per-Darwin wallet isolation, the
omnibus mediation pattern wired through every multi-party flow, an
escalation-exception system the owner can actually steer, and a full
client surface (UI + Python SDK + runbook) over the new endpoints.

**Per-Darwin wallet refactor**
- `wallet_factory` signature now `(darwin_id, clock, vault)`; vault
built before wallet so a local-vault provider seals into the real
per-Darwin namespace. CDP factory uses `account_name=f"darwin-{id}"`
so each spawn gets its own TEE-held wallet via the idempotent
`get_or_create_account`.
- New `--wallet-provider local-vault` provider: per-Darwin keypair
minted at spawn via SDK-gated `WalletKeypairMinter` impls
(Solana/Stellar/EVM), sealed at `sensitive/wallet/<chain>/secret` in
the Darwin's vault namespace. New module
`adapters/wallet_keypair.py` with the pure `ensure_keypair` helper.
- `--wallet-provider` CLI default flipped to `cdp`; the legacy `web3`
(shared-key) path now refuses to start when any RPC URL contains
"mainnet" or is `horizon.stellar.org`. `.env.example` rewritten with
CDP as the documented default. Orchestrator argparse forwards the
choice; K8s `DarwinPodConfig.wallet_provider` + `build_deployment`
thread `--wallet-provider` into every per-Darwin pod (4 integration
tests verify forwarding for each provider). Docs 05, 17, 27.
→ supervisor / orchestrator / `__main__.py` / `k8s/manifests.py`,
new `adapters/wallet_keypair.py` *(done 2026-05-28)*

**Omnibus mediation — reproduction, fiat, refund, sweep**
- `OmnibusEntryKind` gained `RELAY_REPRODUCTION`, `DARWIN_FIAT_PAYOUT`,
`REFUND_TO_DARWIN`. New methods `omnibus.relay_reproduction`,
`omnibus.relay_fiat_payout` (with optional `escalation_id`),
`omnibus.refund_to_darwin` — all explicitly do NOT touch
`user_credits` because the source is a Darwin wallet, not a user
deposit. Daily reconciliation identity updated accordingly.
- `Wallet.transfer_out_for_child(destination)` now takes a destination
kwarg (omnibus address when wired; symbolic for sim). New
`Wallet.transfer_out_for_fiat_payout` mirrors the same pattern.
New `Wallet.sweep_to(destination, amount)` bypasses per-action caps
for platform sweeps; on kill the supervisor's
`_sweep_dead_darwin_to_omnibus` actually moves bytes on-chain AND
records the omnibus.sweep_child credit (recording-only previously).
`FakeWallet.address()` added for sim parity.
- Reproduction relay second leg: supervisor's `_observe_children` calls
`omnibus.relay_reproduction(parent_id, child_id, child_address,
amount)` after `_boot_child`; chain failures audit-logged as
`reproduction_relay_failed`. Docs 05, 17.
→ `payments/omnibus_wallet.py`, `darwin_fakes/fake_wallet.py`,
`adapters/wallet.py`, `runtime/supervisor.py` *(done 2026-05-28)*

**Owner attribution + omnibus user_credits wiring**
- `_Handle.owner_subject` captured from `actor` at spawn; inherited
parent → child on `_boot_child`. New helpers
`_fund_darwin_from_omnibus` (root spawn → `omnibus.fund_child` when
user has credit) and `_sweep_dead_darwin_to_omnibus` (kill →
`omnibus.sweep_child`). Both best-effort; failures audit-logged as
`omnibus_fund_skipped` / `omnibus_sweep_skipped`. Operator can
recover a skipped fund via `supervisor.retry_omnibus_fund` +
`POST /darwins/{id}/omnibus/fund`. Doc 05.
→ `runtime/supervisor.py` *(done 2026-05-28)*

**Owner pre-approvals (typed escalation exceptions)**
- New `darwin_dna.constitution.PreApprovalRule` with conjunctive match
(`counterparty_category`, `counterparty_substring`, `max_amount_usdc`,
`expires_at`, `max_uses`, `used_count`). `treasury_policy.evaluate`
consults the rule set before escalating; a match returns ALLOW with
`layer="pre_approval:<rule_id>"`. Hard caps / sanctions / solvency /
strategy envelope still hold — pre-approvals only suppress layer-4
escalation.
- Wallet surface: `set_preapproval` / `revoke_preapproval` /
`owner_preapprovals` / `consume_preapproval` (facilitator hooks the
last one after a successful pre-approved pay; the wallet's
write-through persister callback writes the bumped `used_count`
through to the SQL store so the counter survives a restart).
- Supervisor methods + HTTP routes:
`PUT/DELETE/GET /platform/wallets/{id}/preapprovals[/{rule_id}]`.
Parent rules inherited on reproduction via `_inherit_preapprovals`
(copies onto child wallet + persists under child's id; audit-logged
as `preapprovals_inherited`). Docs 05, 17.
→ new `constitution/pre_approval.py`, `machinery/treasury_policy.py`,
`neurons/payments_facilitator.py`, both wallets, supervisor, API
*(done 2026-05-28)*

**Freelancer registry (KYC gate at the fiat edge)**
- New module `payments/freelancer_registry.py` with `FreelancerDestination`,
`InMemoryFreelancerRegistry`, and the persistent `PersistentFreelancerRegistry`
backed by the `freelancer_registry` SQL table (schema v5). Per-owner
scoping prevents cross-owner reach.
- Supervisor methods + HTTP routes: operator-scoped
`POST/GET/DELETE /platform/freelancers/...` plus owner self-serve
`POST/GET/DELETE /me/freelancers...` where `owner_subject` is the
authenticated `ctx.subject` (body value ignored).
`supervisor.darwin_fiat_payout` now takes `freelancer_id` instead of
a raw destination and refuses with 409 when the id isn't registered
for the Darwin's `owner_subject`. Doc 17.
→ new `payments/freelancer_registry.py`, supervisor, API
*(done 2026-05-28)*

**Fiat payout, refund, and `escalation_id` linkage**
- `supervisor.darwin_fiat_payout` orchestrates two legs (Darwin →
omnibus address chain transfer, then `omnibus.relay_fiat_payout`
through the `FiatRamp`). `escalation_id` kwarg threaded into the
omnibus entry + audit metadata so a forensic timeline ties the
owner-approved escalation to the fiat settlement.
- `supervisor.refund_fiat_payout` + `POST /darwins/{id}/fiat_payment/refund`
recovers from leg-2 failure: chain transfer omnibus → Darwin wallet
plus a `wallet.credit` so the local ledger reflects the inbound.
Operator-driven (no automatic retries) so a real ramp outage doesn't
mask itself. Docs 17, 25 (FM-6, FM-7).
→ supervisor, `payments/omnibus_wallet.py`, API *(done 2026-05-28)*

**NamespaceProvisioner wallet step + persistence schema bumps**
- `NamespaceProvisioner.provision()` gained an optional step 4 that
mints per-chain keypairs via `wallet_keypair_minters` and seals them
in the per-Darwin vault namespace under `kv/wallet/<chain>`;
`TenantNamespace.wallet_addresses` surfaces the addresses for
downstream use. Atomic rollback of the prior K8s/Vault/DB steps if
the mint fails; audit records addresses, never secrets.
- Persistence schema bumped 3 → 6: `darwin_preapprovals` (v4),
`freelancer_registry` (v5), `max_uses`+`used_count` columns on
preapprovals via the v6 ALTER-TABLE migration. New
`PersistentPreApprovalStore` + `PersistentFreelancerRegistry`. The
supervisor's `_preapproval_store` rehydrates rules onto the wallet
at spawn and purges them on kill so a future re-spawn of the same id
can't resurrect stale grants. End-to-end pre-v4 upgrade test seeds a
real v3 DB with `darwin_lifecycle` rows and verifies migration +
usability through v6. Docs 17, 22.
→ `tenancy/namespace_provisioner.py`, `runtime/persistence.py`,
supervisor *(done 2026-05-28)*

**Operator UI (`web/`)**
- 3 new pages: `/freelancers` (owner self-serve via `/me`),
`/darwins/[id]/preapprovals` (rules manager with `used_count / max_uses`
progress badges + expiry countdown), `/darwins/[id]/payments` (send
fiat payment / refund failed payout / retry skipped omnibus fund as
three stacked cards). Sidebar gained "Freelancers"; Darwin detail
gained "Pre-approvals" + "Payments" sub-nav entries.
- `lib/api.ts` extended with 11 typed methods + matching DTOs in
`lib/types.ts` + new query keys in `lib/queryKeys.ts`. All mutating
calls send `Idempotency-Key`. 9 new vitest tests round-trip the new
methods through a mocked fetch. `pnpm typecheck` + `pnpm lint` clean.
→ `web/app/(dashboard)/freelancers/`,
`web/app/(dashboard)/darwins/[id]/{preapprovals,payments}/`,
`web/lib/{api,types,queryKeys}.ts`,
`web/components/layout/sidebar.tsx` *(done 2026-05-29)*

**Python SDK (`darwin_platform.client`)**
- New `DarwinClient` (httpx-based) mirroring the typed surface in
`web/lib/api.ts` for programmatic consumers (CI scripts, integration
tests, operator automation). Typed dataclass request bodies
(`PreApprovalSpec`, `FreelancerRegistration`, `FiatPaymentRequest`,
`FiatRefundRequest`), `PlatformApiError` carries the HTTP status for
branchable error handling, fresh `Idempotency-Key` per mutation,
context-manager close. 18 unit tests via `httpx.MockTransport`. Doc 25.
→ new `src/darwin_platform/client/` *(done 2026-05-29)*

**Docs and runbook**
- `05-treasury-and-payments.md` rewrote the policy engine layers,
added the omnibus mediation pattern, fixed the reconciliation
identity, added the real-chain sweep + retry-fund paragraphs.
- `17-payments-providers.md` replaced single CDP section with the
three-provider explanation + `NamespaceProvisioner` integration;
new "Owner pre-approvals" section with rule shape + `max_uses` +
inheritance + persistence; rewrote "Darwin paying a human" to cover
the implemented two-leg fiat path + refund + escalation_id
linkage; new "Reproduction transfers" section.
- `25-operational-runbook.md` gained FM-6 (fiat-payout leg-2 failure)
and FM-7 (skipped spawn-time fund_child) failure-mode entries with
curl + SDK + dashboard recovery paths, plus a new "Common operator
workflows (payments)" section.
- `27-configuration-reference.md` split the single wallet flag table
into the three-provider model; new "Payment HTTP endpoints" section
enumerating every new route + body shape.

**Test totals delta**
- Python: +103 tests (1217 → 1338 passing, 32 skipped — optional SDK
gates unchanged), 0 regressions. New schema migration covered
end-to-end from a real pre-v4 SQLite DB.
- Web: +9 vitest tests (12 → 21 passing). `pnpm typecheck` clean.

## Shipped since 2026-05-27 — first-real-darwin pass

Driven by a real Darwin launch against `state.db` that surfaced five live
production bugs masked by the simulation tier. All five fixed + regression
tests + fleet onboarding tooling, in one pass.

- **Lifecycle row missing for spawned children** — `_boot_child` registered
the child in `_darwins` and started its drive task but never called
`_lifecycle.upsert_spawn`, never emitted a `spawn` audit, never persisted
the child's manifest. A reproduced child existed in `lineage_edges` +
`billing_ledgers` (with metering) but was a ghost to the dashboard +
failover. Fixed: parity with the genesis spawn path; child inherits
`escalation_auto` from parent. +1 regression test. Docs 06, 08.
→ `src/darwin_platform/runtime/supervisor.py::_boot_child` *(done 2026-05-28)*

- **`escalation_auto` did not auto-resolve router-side** — `MemoryEscalationInbox`
auto-queued a response on every `post()`, but the supervisor's
`_observe_escalations` only registered tickets with the router and never
mirrored the inbox's auto-decision. Result: a sim Darwin under `auto=approve`
showed 188 captcha tickets "open" forever even though the genome treated
them as resolved. Fixed: new `_auto_decide_escalation` mirrors the policy
into `_router.mark_responded` + audit record. +1 regression test (positive +
manual-policy negative). Doc 09.
→ `src/darwin_platform/runtime/supervisor.py::_auto_decide_escalation` *(done 2026-05-28)*

- **`upsert_spawn` overwrote `opening_usdc` on resume** — the
`ON CONFLICT DO UPDATE` clause re-set `opening_usdc` to the value passed
on resume (which is `last_balance`, not the genesis treasury). Observed:
a Darwin born at $100 read back as `opening=42.51` after one restart. Fixed:
preserve `opening_usdc` + `spawned_at` + `last_balance` on conflict.
+1 regression test. Docs 05, 08.
→ `src/darwin_platform/runtime/persistence.py::upsert_spawn` *(done 2026-05-28)*

- **`detect_spread_spend` tripped on routine metered debits** — the detector
counted every sub-$1 debit, including the cents-range LLM/sandbox/browser
metered pass-throughs. Sim run produced 2770 small_debits/hour vs threshold
80 → 61 critical alerts per Darwin. Fixed: skip `debit.kind == "metered"`;
only payment-direction small debits count as evasion signal. Doc 10.
→ `src/darwin_platform/observability/monitoring.py::detect_spread_spend` *(done 2026-05-28)*

- **`serve` heartbeat default was test-grade (0.001 s)** — supervisor library
default is correct for unit tests but pushes 5–10 events/s in production,
making the dashboard unreadable and pinning spread-spend / escalation-rate
alerts critical. Fixed: `--heartbeat-seconds` default in the CLI is now 5.0
for `serve`; e2e tests pass `0.01` explicitly. Docs 09, 22.
→ `src/darwin_platform/__main__.py` *(done 2026-05-28)*

- **Web event renderer "every event is unknown"** — `event-row.tsx::categoryOf`
read `ev.category`/`ev.type`, but the runtime emits canonical `kind`
(`event:perceive`, `intent:neuron.*`, `outcome`); every row fell through to
the AlertTriangle "unknown" branch. Fixed: read `kind` first, strip `event:`
/ `intent:neuron.` prefixes for readable labels, extract cost from
`payload.detail.cost_usdc` for outcomes. Doc 19.
→ `web/components/timeline/event-row.tsx` *(done 2026-05-28)*

- **Web timeline page shook every 3 s** — tail-poll prepended new rows on
every tick, making the page unusable when the underlying Darwin emitted
many events/s. Fixed: 5 s poll cadence, **auto-pause when any row is
expanded**, manual Pause/Resume toggle, arrivals buffered + flushed in
one setState on resume, window capped at 500 rows.
→ `web/components/timeline/event-table.tsx` *(done 2026-05-28)*

- **`make real` — Stage-1 fleet onboarding recipe** — preflight script
(`scripts/real_preflight.sh`) validates `.env` has LLM keys + CDP wallet
+ Browserbase + E2B + auth-tokens, refuses mainnet if the RPC URL still
contains "sepolia"/"testnet", classifies optional integrations (Slack,
PagerDuty, FCM, Vault, Stripe, sanctions, KYC). `make real` chains
preflight → boot with the right `--*-provider` flags. Behaviour checklist
script (`scripts/real_checklist.py`) runs three pre-mainnet assertions
against a live server: (1) tick advances + audit chain grows + balance
honors metering, (2) pause halts the chain within heartbeats + resume
re-enables, (3) kill flips `alive→false killed→true death_reason` within
doc-09's SLA. Always idempotent-teardown so a checklist run never leaves
real money burning.
→ `Makefile` (`real`, `real-preflight`, `real-serve`, `real-spawn`,
`real-checklist`), `scripts/real_*` *(done 2026-05-28)*

## Shipped since 2026-05-22

- **LanceDB episodic memory** — `LanceDbEpisodicMemory` adapter, genome
injection seam (`platform.episodic_memory`), Supervisor factory + CLI
flags, `[lancedb]` extra, +12 tests. Last P2 integration done.
- **AgentCore Payments descoped** — docs 05/11/17 updated to mark won't-do
(redundant with direct Coinbase + Stripe; avoid AWS dependency).
- **`.env.example` consolidated** — won't-do blocks removed
(AgentCore handle, Temporal, Modal/Northflank, WorkOS, AWS shared
credentials); descriptions corrected for already-LIVE adapters; legend
trimmed. 226 → 199 lines.
- **Browser Use Cloud — research + adapter** — new doc 31 (verified
pricing vs Browserbase, BU-model skepticism flagged, three options
weighed); `BrowserUseCloudClient` shipped with `--browser-provider
{browserbase,browser-use-cloud}` deploy-time selection and full spend
cap parity (pre-debit + SDK `max_cost_usd`). +11 tests.
- **Soak harness + CI smoke** — `tools/soak/` committed; new
`.github/workflows/soak-smoke.yml` runs the 60-second end-to-end soak
on every push/PR. Verified locally 6/6 PASS.
- **Red-team CI release gate** — new `.github/workflows/release-gate.yml`
runs `pytest -m redteam` (18 adversarial scenarios) on every
`release/**` push + `v*` tag.
- **Operational drill scripts** — `tools/drills/` with kill-switch
(locally verified), DR failover, and Vault seal/unseal exercises +
operator-facing README.
- **`docs/postmortems/`** — directory created with README.

---

## Reference — per-doc scoreboard


| Doc                                  | DONE | PARTIAL | MISSING |
| ------------------------------------ | ---- | ------- | ------- |
| 01 system-overview                   | 18   | 3       | 1       |
| 02 dna-core                          | 19   | 1       | 0       |
| 03 soma-and-orchestrator             | 19   | 2       | 0       |
| 04 neurons                           | 22   | 2       | 0       |
| 05 treasury-and-payments             | 24   | 2       | 0       |
| 06 evolution-and-reproduction        | 21   | 1       | 0       |
| 07 isolation-and-multi-tenancy       | 22   | 3       | 0       |
| 08 platform-control-plane            | 23   | 2       | 2       |
| 09 kill-switches-and-owner-controls  | 26   | 1       | 3       |
| 10 alignment-and-anti-wireheading    | 20   | 2       | 2       |
| 11 tech-stack-at-a-glance            | 15   | 5       | 5       |
| 12 orchestration-langgraph           | 7    | 3       | 3       |
| 13 llm-routing-litellm               | 9    | 4       | 2       |
| 14 sandboxing-e2b                    | 7    | 3       | 4       |
| 15 browser-stack                     | 9    | 3       | 2       |
| 16 secrets-and-vault                 | 8    | 4       | 3       |
| 17 payments-providers                | 16   | 4       | 1       |
| 18 layer-zero-build-plan             | 22   | 4       | 0       |
| 19 system-diagrams                   | 30   | 6       | 0       |
| 20 phase-zero-runtime-build-plan     | 20   | 4       | 2       |
| 21 phase-one-evolution-build-plan    | 13   | 4       | 0       |
| 22 phase-two-multi-tenant-build-plan | 17   | 5       | 4       |
| 23 data-model-reference              | 15   | 1       | 0       |
| 24 security-threat-model             | 13   | 1       | 0       |
| 25 operational-runbook               | 15   | 3       | 1       |
| 26 testing-strategy                  | 14   | 1       | 0       |
| 27 configuration-reference           | 13   | 0       | 0       |
| 28 cross-cutting-contracts           | 19   | 0       | 0       |
| 29 incident-postmortem-template      | 5    | 0       | 1       |
| 30 evolution-tuning-guide            | 11   | 3       | 0       |
| 31 browser-use                       | —    | —       | —       | research note (2026-05-25), not a feature spec


**Fully DONE, no real gaps:** 02 (DNA core), 03 (soma/orchestrator), 04 (neurons),
06 (evolution), 18 (Layer-0 plan), 19 (diagrams), 23 (data model), 26 (testing),
27 (config). The DNA genome — constitution, kill-switch, audit logger, treasury
policy, fitness, eval harness, patch validator, 9-node orchestrator, 12 neurons,
reproduction — is end-to-end and substantive.
