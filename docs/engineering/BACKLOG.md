# Darwin — Engineering Backlog

Audit of `docs/engineering/01–31` against the codebase. Last updated **2026-05-25**.

**~487 specified features DONE · ~79 PARTIAL · ~38 MISSING.** The Python core is
end-to-end and substantive — zero `NotImplementedError`/bare-`pass` stubs in
`darwin_dna/` or `darwin_platform/`. Nearly every remaining gap is *outside* the
Python monorepo: frontend, infra-as-code, third-party SaaS adapters, and process
commitments.

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

## P3 — Frontend & infrastructure (never started)

- **Next.js web dashboard** — Anatomy + Timeline canvases. REST/WS API is complete;
no `web/` directory. Docs 01, 08, 19, 20, 22.
- **Mobile escalation app** — Expo/RN; no `mobile/` directory. Docs 19, 22.
- **Terraform / EKS IaC** — `infra/`: EKS, Aurora platform + organism clusters,
Vault, observability, dev/staging/prod envs. Docs 20, 22.

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
- **7-day testnet soak run** — Layer-0 exit criterion. Docs 18, 20.
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
- **Real economic activity** — Darwin earns ≥ $1 USDC live. Doc 21.

## Doc-drift cleanup (code is fine — fix the docs) — ✅ COMPLETE

- **Doc 25** — metric table replaced with the real `darwin_platform_*` names +
a pointer to `/metrics` as authoritative; inline metric refs corrected;
`/platform/signing/public-key` → `public_key`. *(done 2026-05-22)*
- **Doc 24** — red-team gate checklist + suite references repointed to
`tests/redteam/`, `tests/property/`, `tests/integration/`. *(done 2026-05-22)*
- **Doc 30** — calibration example uses `apply_calibration(sim_world=, revenue_model=, dataset=)` and `runtime.archive`. *(done 2026-05-22)*
- **Doc 05** — MoonPay confirmed implemented (`MoonPayFiatRamp`,
`src/darwin_platform/adapters/fiat.py:344`); scoreboard already corrected.

---

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
| 05 treasury-and-payments             | 23   | 3       | 0       |
| 06 evolution-and-reproduction        | 20   | 2       | 0       |
| 07 isolation-and-multi-tenancy       | 22   | 3       | 0       |
| 08 platform-control-plane            | 22   | 2       | 3       |
| 09 kill-switches-and-owner-controls  | 25   | 1       | 4       |
| 10 alignment-and-anti-wireheading    | 19   | 3       | 2       |
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
