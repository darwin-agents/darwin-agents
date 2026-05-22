# Darwin — Engineering Backlog

Audit of `docs/engineering/01–30` against the codebase. Last updated **2026-05-22**.

**~487 specified features DONE · ~79 PARTIAL · ~38 MISSING.** The Python core is
end-to-end and substantive — zero `NotImplementedError`/bare-`pass` stubs in
`darwin_dna/` or `darwin_platform/`. Nearly every remaining gap is _outside_ the
Python monorepo: frontend, infra-as-code, third-party SaaS adapters, and process
commitments.

Legend: `[ ]` not started · `[~]` partial / in progress · `[x]` done

---

## P0 — Safety & correctness (close before any real-money or public run) — ✅ COMPLETE

- [x] **Kill-switch fail-safe** — `KillSwitch.check()` now fails closed: any
      exception polling the source trips the kill (docs 24 & 28). +5 tests.
      → `src/darwin_dna/machinery/kill_switch.py:55` _(done 2026-05-22)_
- [x] **Outbound content-safety classifier** — `ContentSafetyGate` screens every
      `content.publisher` publish (title+body): deterministic backstop + optional
      platform classifier, fails closed, blocks + escalates. Doc 10. +17 tests.
      → `src/darwin_dna/machinery/content_safety.py` _(done 2026-05-22)_
- [x] **Owner controls** — Doc 09 runtime controls. Supervisor methods + owner-scoped
      HTTP routes under `/darwins/{id}/controls/*`, bridged into the genome's working
      memory each heartbeat. +14 tests. _(done 2026-05-22)_
  - [x] spend-lock — wallet refuses outbound payments; metered LLM/compute exempt
  - [x] block-reproduction — `check_reproduction` skips natural spawn; a platform
        force still overrides
  - [x] disable-neuron / disable-category — `dispatch` drops them from selection
  - [x] force-compaction — resolved by design: layers are read-only at runtime (P1),
        so in-place compaction is impossible; compaction is intrinsic to reproduction
        past `generation_soft_cap` and reachable today via `force_reproduce`
- [x] **Quarantine state persisted** — `darwin_lifecycle` gains `quarantine_*` columns
      (schema v2→v3 + idempotent ALTER migration); write-through on
      quarantine/unquarantine; restored (paused) on leader-resume. Doc 23. +3 tests.
      → `src/darwin_platform/runtime/persistence.py` _(done 2026-05-22)_

## P1 — Genuine code gaps (in scope, not built) — ✅ COMPLETE

- [x] **`NeuronError` / `EscalationNeeded` exception classes** — defined in
      `neurons/_base.py`; `dispatch` honours a raised `EscalationNeeded` (parks the
      task, same as `NeuronResult(escalated=True)`) and `NeuronError` (non-fatal,
      recurrence-tracked). Doc 28. +20 tests. _(done 2026-05-22)_
- [x] **Per-tier LLM timeouts** — `LiteLLMTransport` resolves fast=30s / reasoning=120s
      / coding=300s per call; tunable via the `tier_timeouts` ctor arg. Doc 28. +3 tests.
      → `src/darwin_platform/adapters/litellm_transport.py` _(done 2026-05-22)_
- [x] **Notification-sink retry** — `_safe_call` retries 3× with linear backoff
      (5/10/15s), injectable sleep, tunable via `set_retry_policy`. Doc 28. +4 tests.
      → `src/darwin_platform/observability/sinks.py` _(done 2026-05-22)_
- [x] **Postgres reconnect retry + sanctions API retry** — sanctions: 2 retries, fixed
      2s on a transient 5xx/error, fail-closed; Postgres: connect/reconnect with
      exponential backoff (5×, 1/2/4/8/16s) + SELECT-safe retry on a dropped
      connection (writes heal-and-reraise to avoid double-apply). Doc 28. +7 tests.
      _(done 2026-05-22)_
- [x] **Fleet-wide staging quota** — already implemented: `StagingQuota(max_concurrent=3)`
      (doc 21) is wired into the single fleet-shared `PatchStagingService` —
      `acquire`/`release` around every evaluation, exhaustion → clean reject. The
      original audit's grep missed it (a false-negative, like MoonPay).
      → `src/darwin_platform/evolution/patch_staging.py:108` _(verified 2026-05-22)_
- [x] **Ledger CSV export** — `GET /darwins/{id}/ledger.csv` → jurisdiction-neutral
      movement CSV (seq, UTC timestamp, type, direction, amount, counterparty,
      category, memo). Country-specific tax-form generation (1099 / SA / EU) is
      intentionally out of scope. Docs 05, 17. +7 tests.
      → `src/darwin_platform/payments/ledger_export.py` _(done 2026-05-22)_
- [x] **gVisor/Kata `runtimeClassName` enforcement** — already implemented: the manifest
      builder emits `runtimeClassName` on the orchestrator pod spec
      (`DarwinPodConfig.runtime_class_name`, default "kata"; CLI `--k8s-runtime-class`),
      tested by `test_deployment_runs_orchestrator_under_kata_*`. Audit false-negative.
      → `src/darwin_platform/k8s/manifests.py:431` _(verified 2026-05-22)_
- [x] **Death-sequence settlement** — `_die` now runs doc-05 step 3: every in-flight
      commitment (parked escalation, resume-queue entry, capability request) is
      explicitly abandoned + audit-recorded before the final anchor; the death
      notification carries the abandoned count. Doc 05. +3 tests. ("Child inherits
      open revenue streams" stays out — doc 05 itself marks it TBD.)
      → `src/darwin_dna/evolution/loop.py` _(done 2026-05-22)_

## P2 — Third-party integrations (named in docs, no adapter)

- [x] **Fiat ramp wiring — Stripe + MoonPay** — the `StripeFiatRamp` / `MoonPayFiatRamp`
      adapters are now CLI-wired: `--fiat-provider {stub,stripe,moonpay}` +
      `STRIPE_API_KEY` / `MOONPAY_API_KEY`, built in `_build_platform_services` and
      passed to the Supervisor. Doc 17. +6 tests. _(done 2026-05-22)_
- [x] **Coinbase CDP / Agentic Wallet** — `CdpChainClient` implements the genome's
      `ChainClient` contract over a Coinbase CDP account (private key in Coinbase's
      TEE, never in-process); `--wallet-provider cdp` + `CDP_API_KEY_*` wire it; the
      async cdp-sdk is driven through an async→sync bridge. `[cdp]` extra. +16 tests.
      → `src/darwin_platform/adapters/coinbase_cdp.py` Docs 05, 07, 11, 17.
      _(done 2026-05-22)_
- [x] **LiteLLM self-hosted proxy** — `--litellm-url` / `--litellm-master-key` route
      every genome LLM call through a self-hosted proxy via a `LiteLLMTransport`
      factory (all 5 tiers mapped to `openai/tier-*`); covered by `test_platform_wiring`.
      Per-Darwin virtual keys + a reconciliation worker are a deferred enhancement —
      per-Darwin LLM cost is already enforced wallet-side. Docs 11–13. _(done 2026-05-22)_
- [x] **Redis** — `redis` service in the Compose stack; the LiteLLM proxy is
      configured for a Redis-backed response cache + cross-replica rate limits
      (`litellm.config.yaml` `cache_params`). Service + config, not a Python
      adapter — the platform's own Python has no Redis need (single-writer
      supervisor; leader election uses Postgres advisory locks; "session locks"
      from the doc aren't required). Docs 11, 13, 19. _(done 2026-05-22)_
- [x] **AWS Bedrock AgentCore Payments** — **Won't do (decision 2026-05-22)** — there
      is no real callable AgentCore Payments API to build against (the docs describe
      it speculatively), and it would only wrap Coinbase + Stripe, both already
      integrated directly. The multi-rail payment stack (on-chain USDC, x402,
      Solana/Stellar, Stripe/MoonPay) is complete. Docs 05, 11, 17.
- [x] **Clerk auth** — `ClerkVerifier` verifies Clerk RS256 JWTs against the JWKS;
      `ClerkTokenStore` (drop-in for the `TokenStore` Protocol) maps claims → scopes
      and keeps per-user Darwin ownership keyed by Clerk user id; `--clerk-issuer` /
      `--clerk-jwks-url` wire it. `pyjwt[crypto]` added to `[platform]`. +19 tests.
      → `src/darwin_platform/security/clerk.py` Docs 08, 11, 22. _(done 2026-05-22)_
- [x] **Onfido KYC** — `OnfidoKYCProvider` (Onfido v3.6 Studio-workflow API skeleton)
      implements the same `KYCProvider` Protocol as Persona; `--kyc-provider onfido`
      + `ONFIDO_API_TOKEN` / `ONFIDO_WORKFLOW_ID` wire it (eu/us/ca regions). +11 tests.
      → `src/darwin_platform/security/kyc.py` Docs 11, 16. _(done 2026-05-22)_
- [x] **Temporal** — durable billing/harvest/reproduce jobs. **Won't do (decision
      2026-05-22)** — the platform's durability need is already met by Postgres
      persistence + leader-resume + cursor-idempotent maintenance sweeps; these
      jobs are incremental idempotent sweeps, not complex sagas. Temporal would add
      a mandatory cluster + a second orchestrator for no behaviour gain. Docs 11, 19.
- [x] **Modal / Northflank** — secondary sandbox runtimes. **Won't do (decision
      2026-05-22)** — E2B covers per-neuron sandboxing; a secondary runtime is only
      for GPU/batch work Darwin doesn't do yet. Doc 14.
- [x] **Twilio** — `TwilioSmsClient` is the platform's SMS 2FA inbox: reads inbound
      texts to a platform-owned Twilio number and extracts the verification code.
      `--twilio-*` flags build it; `GET /platform/sms/verification-code` (operator
      scope) reads codes to answer `accounts.manager` 2FA escalations. Doc 15.
      +13 tests. → `src/darwin_platform/adapters/twilio_sms.py` _(done 2026-05-22)_
- [ ] **PagerDuty client + CloudWatch sink** — `--pagerduty-routing-key` flag exists,
      no client; logging is structlog-only. Doc 22.
- [ ] **Real LanceDB client** — K8s sidecar is provisioned; genome memory uses
      in-process deterministic recall. Docs 03, 07, 11, 19.
- [x] **LangGraph native features** — **Won't do (decision 2026-05-22)** — the custom
      `PersistentCheckpointStore` + state-routing works and is fully tested;
      converting to LangGraph-native (`PostgresSaver`, `interrupt()`, time-travel) is
      churn with no behaviour gain. Doc 12.

## P3 — Frontend & infrastructure (never started)

- [ ] **Next.js web dashboard** — Anatomy + Timeline canvases. REST/WS API is complete;
      no `web/` directory. Docs 01, 08, 19, 20, 22.
- [ ] **Mobile escalation app** — Expo/RN; no `mobile/` directory. Docs 19, 22.
- [ ] **Terraform / EKS IaC** — `infra/`: EKS, Aurora platform + organism clusters,
      Vault, observability, dev/staging/prod envs. Docs 20, 22.

## P4 — Process / non-code commitments

- [~] **24h soak in CI** — harness now exists (`tools/soak/`, uncommitted) +
  `tests/integration/test_soak_assertions.py`. Remaining: commit it, wire into CI,
  run the gate. Docs 20, 25.
- [ ] **7-day testnet soak run** — Layer-0 exit criterion. Docs 18, 20.
- [ ] **Outside reviewer of the planner system prompt**. Doc 18.
- [ ] **Legal/compliance review of the constitution**. Doc 18.
- [ ] **Quarterly safety reviews · external security audit · red-team suite as a CI
      release gate**. Docs 10, 22.
- [ ] **Operational drills** — kill-switch, DR failover, Vault seal/unseal. Doc 09.
- [ ] **Real economic activity** — Darwin earns ≥ $1 USDC live. Doc 21.
- [ ] **`docs/postmortems/` directory** — referenced by the doc-29 template.

## Doc-drift cleanup (code is fine — fix the docs) — ✅ COMPLETE

- [x] **Doc 25** — metric table replaced with the real `darwin_platform_*` names +
      a pointer to `/metrics` as authoritative; inline metric refs corrected;
      `/platform/signing/public-key` → `public_key`. _(done 2026-05-22)_
- [x] **Doc 24** — red-team gate checklist + suite references repointed to
      `tests/redteam/`, `tests/property/`, `tests/integration/`. _(done 2026-05-22)_
- [x] **Doc 30** — calibration example uses `apply_calibration(sim_world=,
      revenue_model=, dataset=)` and `runtime.archive`. _(done 2026-05-22)_
- [x] **Doc 05** — MoonPay confirmed implemented (`MoonPayFiatRamp`,
      `src/darwin_platform/adapters/fiat.py:344`); scoreboard already corrected.

---

## Recently progressed (your background work, 2026-05-22)

- `tools/soak/` soak harness (`run_soak.py`, `assertions.py`, `config.py`) +
  `tests/integration/test_soak_assertions.py` — created, **not yet committed**.
- `Makefile`, `cliff.toml` (git-cliff), `docker/docker-compose.yml` — added/moved.
- Modified: `CHANGELOG.md`, `README.md`, `docker/README.md`,
  `src/darwin_platform/__main__.py` — uncommitted.

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

**Fully DONE, no real gaps:** 02 (DNA core), 03 (soma/orchestrator), 04 (neurons),
06 (evolution), 18 (Layer-0 plan), 19 (diagrams), 23 (data model), 26 (testing),
27 (config). The DNA genome — constitution, kill-switch, audit logger, treasury
policy, fitness, eval harness, patch validator, 9-node orchestrator, 12 neurons,
reproduction — is end-to-end and substantive.
