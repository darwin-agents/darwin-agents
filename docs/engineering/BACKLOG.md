# Darwin — Engineering Backlog

Audit of `docs/engineering/01–31` against the codebase. Last updated **2026-05-29**.

**~492 specified features DONE · ~76 PARTIAL · ~36 MISSING.** The Python core is
end-to-end and substantive — zero `NotImplementedError`/bare-`pass` stubs in
`darwin_dna/` or `darwin_platform/`. Nearly every remaining gap is *outside* the
Python monorepo: infra-as-code, the mobile companion, and process commitments.
The web dashboard is now substantially built (Stage-1 ship), and a real-money
onboarding recipe (`make real` + preflight + checklist) is wired.

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

- [~] **Next.js web dashboard** — Substantially shipped. `web/` (Next 15 / React 19 /
Tailwind v4 / shadcn). Routes live: fleet overview (`/`), spawn wizard
(`/darwins/new`), per-Darwin shell (`/darwins/[id]`) with sub-nav for
Anatomy / Lineage / Billing / Pre-approvals / Payments / Vault / Resources / Settings,
fleet Escalations inbox (`/escalations`), owner self-serve Freelancers
registry (`/freelancers`), Token paste (`/tokens`). WS stream
(`DarwinStream`) layered on top of REST snapshots: anatomy canvas updates
live, event timeline tails with `?since=`. Owner controls (spend-lock,
disable-neuron, force-reproduce, kill), respond drawer, vault put/get,
deprovision, fiat deposit/withdraw, harvest dialog, Darwin fiat-payment
console (send / refund / retry omnibus fund), pre-approval rule manager
(install / revoke / consume-counter view) all wired. Codegen
from FastAPI OpenAPI typed against `web/lib/api.generated.ts`. CI gate
in `.github/workflows/web.yml` (lint/typecheck/test/build).
Remaining gaps: full Timeline route with URL-encoded filters (today's
EventTable on the Darwin shell handles most needs), linked Anatomy↔Timeline
canvas (M3-D), data browser (M3-E), Clerk swap-in (auth seam in place but
still on `--auth-tokens`), Playwright E2E (no `playwright/` directory yet).
Docs 01, 08, 17, 19, 20, 22. *(Stage 1 done 2026-05-27; payments routes
added 2026-05-29)*
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
- **Doc 30** — calibration example uses `apply_calibration(sim_world=, revenue_model=, dataset=)` and `runtime.archive`. *(done 2026-05-22)*
- **Doc 05** — MoonPay confirmed implemented (`MoonPayFiatRamp`,
`src/darwin_platform/adapters/fiat.py:344`); scoreboard already corrected.

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
