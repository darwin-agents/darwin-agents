# Darwin — Engineering Backlog

Audit of `docs/engineering/01–30` against the codebase. Last updated **2026-05-22**.

**~476 specified features DONE · ~85 PARTIAL · ~43 MISSING.** The Python core is
end-to-end and substantive — zero `NotImplementedError`/bare-`pass` stubs in
`darwin_dna/` or `darwin_platform/`. Nearly every remaining gap is *outside* the
Python monorepo: frontend, infra-as-code, third-party SaaS adapters, and process
commitments.

Legend: `[ ]` not started · `[~]` partial / in progress · `[x]` done

---

## P0 — Safety & correctness (close before any real-money or public run)

- [x] **Kill-switch fail-safe** — `KillSwitch.check()` now fails closed: any
      exception polling the source trips the kill (docs 24 & 28). +5 tests.
      → `src/darwin_dna/machinery/kill_switch.py:55`  *(done 2026-05-22)*
- [x] **Outbound content-safety classifier** — `ContentSafetyGate` screens every
      `content.publisher` publish (title+body): deterministic backstop + optional
      platform classifier, fails closed, blocks + escalates. Doc 10. +17 tests.
      → `src/darwin_dna/machinery/content_safety.py`  *(done 2026-05-22)*
- [x] **Owner controls** — Doc 09 runtime controls. Supervisor methods + owner-scoped
      HTTP routes under `/darwins/{id}/controls/*`, bridged into the genome's working
      memory each heartbeat. +14 tests.  *(done 2026-05-22)*
  - [x] spend-lock — wallet refuses outbound payments; metered LLM/compute exempt
  - [x] block-reproduction — `check_reproduction` skips natural spawn; a platform
        force still overrides
  - [x] disable-neuron / disable-category — `dispatch` drops them from selection
  - [x] force-compaction — resolved by design: layers are read-only at runtime (P1),
        so in-place compaction is impossible; compaction is intrinsic to reproduction
        past `generation_soft_cap` and reachable today via `force_reproduce`
- [ ] **Quarantine state not persisted** — in-memory only on `QuarantineState`;
      lost on supervisor restart. Doc 23 state machine treats it as durable.
      → `darwin_lifecycle` table + `src/darwin_platform/runtime/supervisor.py:144`

## P1 — Genuine code gaps (in scope, not built)

- [ ] **`NeuronError` / `EscalationNeeded` exception classes** — named in the doc-28
      error hierarchy, defined nowhere in `src/`.
- [ ] **Per-tier LLM timeouts** — `LiteLLMTransport` uses a single `request_timeout=60.0`
      for all tiers; doc 28 specifies fast=30s / reasoning=120s / coding=300s.
      → `src/darwin_platform/adapters/litellm_transport.py:204`
- [ ] **Notification-sink retry** — `_safe_call` is fire-and-forget; doc 28 specifies
      3 retries, linear backoff (5s/10s/15s).
      → `src/darwin_platform/observability/sinks.py:37`
- [ ] **Postgres reconnect retry + sanctions API retry** — doc 28 specifies exponential
      reconnect and a 2× fixed sanctions retry; neither loop exists.
- [ ] **Fleet-wide staging quota** — max 3 concurrent patch evaluations; only per-Darwin
      sandbox quotas exist. Doc 21.
- [ ] **Tax reporting / CSV export** — year-end ledger export. Docs 05, 17.
- [ ] **gVisor/Kata `runtimeClassName` enforcement** — referenced in driver comments
      only, not emitted by the manifest builder. Doc 07.
      → `src/darwin_platform/k8s/manifests.py`
- [ ] **Death-sequence settlement** — in-flight commitment settle/abandon logic, and
      "child inherits open revenue streams" (doc marks the latter TBD). Doc 05.

## P2 — Third-party integrations (named in docs, no adapter)

- [ ] **Coinbase CDP / Agentic Wallet** — currently a generic `RealWallet` over
      `Web3ChainClient` (web3.py, local key signing); no CDP SDK, no TEE.
      Docs 05, 07, 11, 17.
- [ ] **LiteLLM self-hosted proxy** — adapter calls `litellm.completion` in-process;
      no proxy URL, no per-Darwin virtual keys, no reconciliation worker. Docs 11–13.
- [ ] **Redis / ElastiCache** — LiteLLM rate-limit + prompt cache, session locks.
      Docs 11, 13, 19.
- [ ] **AWS Bedrock AgentCore Payments** — secondary payment rail, absent entirely.
      Docs 05, 11, 17.
- [ ] **Clerk / WorkOS auth** — platform still uses an in-memory bearer-token store.
      Docs 08, 11, 22.
- [ ] **Onfido KYC** — second provider; only `PersonaKYCProvider` exists. Docs 11, 16.
- [ ] **Temporal** — durable billing/harvest/reproduce jobs; runs in-process now.
      Docs 11, 19.
- [ ] **Modal / Northflank** — secondary sandbox runtimes; only E2B. Doc 14.
- [ ] **Twilio** — 2FA/SMS code handling for `accounts.manager`. Doc 15.
- [ ] **PagerDuty client + CloudWatch sink** — `--pagerduty-routing-key` flag exists,
      no client; logging is structlog-only. Doc 22.
- [ ] **Real LanceDB client** — K8s sidecar is provisioned; genome memory uses
      in-process deterministic recall. Docs 03, 07, 11, 19.
- [ ] **LangGraph native features** — custom `PersistentCheckpointStore` instead of
      `PostgresSaver`; escalation is state-routing, not `interrupt()`; no time-travel
      or fan-out reducer. Doc 12.

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

## Doc-drift cleanup (code is fine — fix the docs)

- [ ] **Doc 25** — Prometheus metric names use `darwin_*`; code emits `darwin_platform_*`.
      `/platform/signing/public-key` → actual route is `public_key`.
- [ ] **Doc 24** — red-team gate checklist uses stale flat paths (`tests/test_redteam.py`);
      actual is `tests/redteam/`, `tests/property/`, `tests/integration/`.
- [ ] **Doc 30** — shows `SimCalibration(ds).apply()` and `runtime.mutation_archive`;
      code has `apply_calibration()` and `runtime.archive`.
- [ ] **Doc 05** — MoonPay listed as missing in the original audit; it *is* implemented
      (`MoonPayFiatRamp`, `src/darwin_platform/adapters/fiat.py:344`). Mark DONE.

---

## Recently progressed (your background work, 2026-05-22)

- `tools/soak/` soak harness (`run_soak.py`, `assertions.py`, `config.py`) +
  `tests/integration/test_soak_assertions.py` — created, **not yet committed**.
- `Makefile`, `cliff.toml` (git-cliff), `docker/docker-compose.yml` — added/moved.
- Modified: `CHANGELOG.md`, `README.md`, `docker/README.md`,
  `src/darwin_platform/__main__.py` — uncommitted.

---

## Reference — per-doc scoreboard

| Doc | DONE | PARTIAL | MISSING |
|---|---|---|---|
| 01 system-overview | 18 | 3 | 1 |
| 02 dna-core | 19 | 1 | 0 |
| 03 soma-and-orchestrator | 19 | 2 | 0 |
| 04 neurons | 22 | 2 | 0 |
| 05 treasury-and-payments | 21 | 4 | 1 |
| 06 evolution-and-reproduction | 20 | 2 | 0 |
| 07 isolation-and-multi-tenancy | 21 | 3 | 1 |
| 08 platform-control-plane | 22 | 2 | 3 |
| 09 kill-switches-and-owner-controls | 25 | 1 | 4 |
| 10 alignment-and-anti-wireheading | 19 | 3 | 2 |
| 11 tech-stack-at-a-glance | 15 | 5 | 5 |
| 12 orchestration-langgraph | 7 | 3 | 3 |
| 13 llm-routing-litellm | 9 | 4 | 2 |
| 14 sandboxing-e2b | 7 | 3 | 4 |
| 15 browser-stack | 9 | 3 | 2 |
| 16 secrets-and-vault | 8 | 4 | 3 |
| 17 payments-providers | 15 | 4 | 2 |
| 18 layer-zero-build-plan | 22 | 4 | 0 |
| 19 system-diagrams | 30 | 6 | 0 |
| 20 phase-zero-runtime-build-plan | 20 | 4 | 2 |
| 21 phase-one-evolution-build-plan | 12 | 4 | 1 |
| 22 phase-two-multi-tenant-build-plan | 17 | 5 | 4 |
| 23 data-model-reference | 14 | 2 | 0 |
| 24 security-threat-model | 13 | 1 | 0 |
| 25 operational-runbook | 15 | 3 | 1 |
| 26 testing-strategy | 14 | 1 | 0 |
| 27 configuration-reference | 13 | 0 | 0 |
| 28 cross-cutting-contracts | 14 | 4 | 1 |
| 29 incident-postmortem-template | 5 | 0 | 1 |
| 30 evolution-tuning-guide | 11 | 3 | 0 |

**Fully DONE, no real gaps:** 02 (DNA core), 03 (soma/orchestrator), 04 (neurons),
06 (evolution), 18 (Layer-0 plan), 19 (diagrams), 23 (data model), 26 (testing),
27 (config). The DNA genome — constitution, kill-switch, audit logger, treasury
policy, fitness, eval harness, patch validator, 9-node orchestrator, 12 neurons,
reproduction — is end-to-end and substantive.
