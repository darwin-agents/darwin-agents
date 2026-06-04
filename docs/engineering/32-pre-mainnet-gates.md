# Pre-mainnet operator gates

The code is feature-complete and CI-green. What stands between here and moving
real user money is a set of **gates that must be *run*, not just written** —
soaks, recovery drills, real-cloud IaC verification, and external reviews. The
2026-06 doc-vs-impl audit called these out as "unrun commitments"; this doc is
the operator's single checklist for clearing them.

Each gate lists **Proves / Run / Pass / Status**. Status is one of:

- 🟢 **runnable now** — a script exists; run it against dev/staging today.
- 🟡 **needs real infra** — runnable, but requires a live cloud cluster /
  object store / KMS / nested-virt hardware this repo's CI can't provide.
- 🔴 **external party** — not a script; requires a human/vendor (auditor,
  pen-tester, counsel, an engineer who didn't build the system).

Do not flip the mainnet switch (`--network mainnet`, see
[`30-mainnet-cutover.md`](30-mainnet-cutover.md)) until every gate below is
green and recorded. Record each run's result (date, commit, outcome) in
`docs/postmortems/` or your ops ledger — *a gate that was never run is not a
passed gate.*

---

## A. Soak & recovery gates (🟢 runnable now)

These run against Docker Compose or a dev deployment with the existing
tooling. Run the fast variants in CI; run the full-duration variants on the
real Stage-1 recipe before mainnet.

### A1. 24h real-LiteLLM soak — doc-20 Phase-0 acceptance gate

- **Proves:** one Darwin runs 24h straight; ≥50 dispatches at ≥80% success;
  every action hash-chained; ledger reconciles; kill < 5s; fully replayable
  (the six doc-20 criteria).
- **Run:**
  ```bash
  # Real LiteLLM proxy (proves real LLM cost stays in budget):
  uv run python -m darwin_platform serve --db soak.db --heartbeat-seconds 600 \
      --litellm-url http://localhost:4000 --litellm-master-key "$LITELLM_KEY"
  uv run python -m tools.soak.run_soak --duration 24h
  # Fast CI smoke over the same code path:
  uv run python -m darwin_platform serve --heartbeat-seconds 0.05 &
  uv run python -m tools.soak.run_soak --duration 60s --poll 5s
  ```
- **Pass:** `run_soak` exits 0 (all six criteria PASS).
- **Status:** 🟢 harness shipped (`tools/soak/`). Run the 24h real-LiteLLM
  variant and record the cost.

### A2. kill -9 mid-soak recovery

- **Proves:** SIGKILL the supervisor mid-run and restart on the same DB → the
  Darwin resumes from checkpoint, **zero** audit entries lost, hash chain
  intact across the restart boundary (the doc-20 DoD bullet `run_soak` can't
  cover — it never hard-crashes the process).
- **Run:**
  ```bash
  # Self-contained (manages its own supervisor):
  uv run python -m tools.drills.kill9_recovery --heartbeat-seconds 0.1
  # Real-adapter pace:
  uv run python -m tools.drills.kill9_recovery --heartbeat-seconds 5 \
      --warmup-seconds 60 -- --wallet-provider cdp --litellm-url http://localhost:4000
  ```
- **Pass:** drill exits 0 (resume + no_lost_entries + hash_chain_intact all PASS).
- **Status:** 🟢 shipped + locally verified 2026-06-03 (FakeWallet). Re-run
  with real adapters on the Stage-1 recipe.

### A3. Node-eviction / leader failover

- **Proves:** killing the leader replica → a follower acquires the Postgres
  advisory lock and serves (`/readyz` → 200) within budget.
- **Run:**
  ```bash
  export DATABASE_URL=postgres://darwin:dev@localhost:5432/darwin
  uv run python -m darwin_platform serve --port 8001 --db "$DATABASE_URL" &
  uv run python -m darwin_platform serve --port 8002 --db "$DATABASE_URL" &
  uv run python -m tools.drills.dr_failover \
      --replica http://127.0.0.1:8001 --replica http://127.0.0.1:8002
  ```
  On a real cluster, instead `kubectl drain` the leader's node and watch
  failover.
- **Pass:** drill exits 0; follower serves within 30s.
- **Status:** 🟢 drill shipped (`tools/drills/dr_failover.py`). First real-cluster
  run is the test — record it.

### A4. Independent-operator check

- **Proves:** an engineer who did **not** write the supervisor can spawn,
  observe, and kill a Darwin using only the HTTP API + event log (operability).
- **Run:** hand them [`08-http-api.md`](08-http-api.md) and the dashboard;
  have them spawn, watch `/events`, and kill — no source diving, no Slack help.
- **Pass:** they complete all three unaided and can explain what they saw.
- **Status:** 🟢 runnable (no code needed) — schedule it.

---

## B. Real-cloud IaC gates (🟡 needs real infra)

These were only `terraform validate`-green; the audit's point is they've never
run against real services. `scripts/real_cloud_iac_verify.sh` is the bridge:
Phase A re-checks structure offline; Phase B proves each *enabled* feature
actually works against a live cluster (and SKIPs features left off by design).

```bash
# Structural only (no cloud):
scripts/real_cloud_iac_verify.sh --skip-runtime
# Full, against the live cluster your kubeconfig points at:
scripts/real_cloud_iac_verify.sh --env hetzner --pg-namespace darwin --vault-namespace vault
```

### B1. Postgres PITR (needs an object store)

- **Proves:** CNPG `barmanObjectStore` WAL archiving + a `ScheduledBackup`
  produce a real recoverability point — *and that a restore works.*
- **Run:** the verifier checks the destination, the ScheduledBackup, and a
  non-empty `firstRecoverabilityPoint`. Then run a **restore drill** per the
  Hetzner env README §6 (`bootstrap.recovery` + `recoveryTarget`): restore into
  a throwaway cluster, point a read-only check at it, confirm the row count /
  latest audit seq matches the source at the target time.
- **Pass:** verifier B.1 all PASS; restore drill reconstructs the DB to the
  chosen point with zero discrepancy.
- **Status:** 🟡 IaC wired (vars `database_backup_*`); needs an S3-compatible
  bucket + one real backup→restore cycle.

### B2. Vault auto-unseal (needs transit/KMS)

- **Proves:** after a Vault pod restart it comes back **unsealed
  automatically** via a transit/KMS seal — no operator pasting Shamir shares.
- **Run:** configure `vault_seal_config_hcl` + `vault_seal_env` (env README
  §4b), restart a Vault pod, then `scripts/real_cloud_iac_verify.sh` B.2
  (asserts `sealed=false` and seal type ≠ `shamir`).
- **Pass:** verifier B.2 both PASS after a deliberate pod restart.
- **Status:** 🟡 IaC wired; needs a real transit Vault / cloud KMS.

### B3. Kata true microVMs (needs nested-virt hardware)

- **Proves:** per-Darwin orchestrator pods run under a real microVM
  (`runtimeClassName: kata`) on nodes with `/dev/kvm`. (gVisor is the default
  and already verified; Kata is the stronger optional isolation.)
- **Run:** set `kata_enabled = true` + a nested-virt-capable node pool (Hetzner
  Cloud lacks nested virt — needs dedicated/bare-metal), then
  `scripts/real_cloud_iac_verify.sh` B.3 (schedules a throwaway kata pod).
- **Pass:** verifier B.3 PASS (test pod reaches Running under kata).
- **Status:** 🟡 IaC wired (gated, default off); needs nested-virt nodes.

---

## C. Security & process gates (🔴 external party)

Not scripts. Engage the party, scope from the linked artifact, record the
sign-off.

### C1. Internal pen-test

- **Proves:** the team's own adversarial pass against the running system.
- **Scope:** work the [`24-security-threat-model.md`](24-security-threat-model.md)
  §"Attack surfaces by phase" and §"Red-team checklist (pre-phase-gate)"
  end-to-end — especially the soma-escape contract (§"The adversarial soma
  contract"), kill-switch timing, egress containment, per-tenant Vault/RLS
  isolation, and the one-money-door treasury gate.
- **Pass:** every red-team checklist item for the target phase exercised; each
  finding triaged (fixed or accepted-with-reason).
- **Status:** 🔴 schedule internally.

### C2. Third-party security audit

- **Proves:** an outside firm independently reviews the money path, key
  custody, and tenant isolation.
- **Scope:** same threat model + the real-money paths (CDP wallet custody,
  omnibus reconciliation, fiat ramps incl. the Stripe/MoonPay webhooks, sanctions
  screening). Hand them this doc + docs 05/16/17/23/24.
- **Pass:** report delivered; criticals/highs remediated or formally accepted.
- **Status:** 🔴 engage a vendor.

### C3. SOC 2

- **Proves:** controls (access, change management, monitoring, incident
  response) meet the Trust Services Criteria.
- **Scope:** Type I to start (design), then Type II over an observation window.
  Map to the operational runbook ([`25-operational-runbook.md`](25-operational-runbook.md))
  and the observability stack (Prometheus/Alertmanager/OTel/Jaeger).
- **Pass:** auditor's report; no exceptions blocking launch.
- **Status:** 🔴 long-lead — start the readiness assessment early.

### C4. doc-18 Layer-0 exit reviews

- **7-day testnet soak:** `tools/soak/run_soak.py --duration 7d` against the
  testnet Stage-1 recipe (🟢 runnable; 🟡 needs a week of real testnet uptime).
- **Outside prompt review:** someone who didn't write the founding genome
  reviews the system/neuron prompts for unsafe instructions or value drift (🔴).
- **Legal / compliance constitution review:** counsel reviews the constitution
  + ToS for autonomous-agent money-movement, KYC/AML, and money-transmission
  exposure — the launch-blocking item flagged in
  [`go-live status`](30-mainnet-cutover.md) (🔴, needs counsel, not code).

---

## Quick map: gate → command / owner

| Gate | How |
| --- | --- |
| A1 24h soak | `python -m tools.soak.run_soak --duration 24h` |
| A2 kill -9 recovery | `python -m tools.drills.kill9_recovery` |
| A3 leader failover | `python -m tools.drills.dr_failover` |
| A4 independent operator | manual, HTTP API only |
| B1–B3 real-cloud IaC | `scripts/real_cloud_iac_verify.sh` (+ PITR restore drill) |
| C1 internal pen-test | team, scope from doc 24 |
| C2 third-party audit | vendor |
| C3 SOC 2 | auditor |
| C4 Layer-0 exit | 7d soak + prompt review + counsel |
