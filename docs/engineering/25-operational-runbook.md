# Operational runbook

How to deploy, monitor, interpret, and recover the Darwin platform — actionable from day 1 of the Phase 0 soak test. This is a living document; add entries as new failure modes are discovered.

## Deployment procedures

### Bare metal (development)

```bash
pip install -e ".[platform,postgres]"

# Dev (SQLite, no auth, fakes for all adapters):
darwin-platform serve --db state.db

# Dev with real LLM (LiteLLM proxy running on :4000):
darwin-platform serve --db state.db --litellm-url http://localhost:4000

# Dev with real wallet (Base Sepolia testnet):
darwin-platform serve --db state.db \
  --rpc-url https://sepolia.base.org \
  --wallet-key 0xYOUR_PRIVATE_KEY
```

### Docker Compose (local stack)

```bash
cd infra/docker
cp ../../.env.example .env   # add LLM provider keys (.env.example: repo root)
docker compose up --build
```

Services: supervisor (:8000), postgres (:5432), litellm (:4000), prometheus (:9090), jaeger (:16686).

See [`infra/docker/README.md`](../../infra/docker/README.md) for full documentation.

### Helm (Kubernetes)

```bash
# Dev (defaults — no auth, SQLite):
helm install darwin infra/charts/darwin-platform

# Production:
helm install darwin infra/charts/darwin-platform \
  --namespace darwin --create-namespace \
  --set image.tag=sha-abc1234 \
  --set auth.enabled=true \
  --set auth.existingSecret=darwin-platform-tokens \
  --set database.existingSecret=darwin-db-creds \
  --set leaderElection.enabled=true \
  --set tracing.endpoint=http://otel-collector:4318 \
  --set serviceMonitor.enabled=true
```

See [`infra/charts/darwin-platform/README.md`](../../infra/charts/darwin-platform/README.md) for the full values reference.

## Health-check endpoints

| Endpoint | Auth | Returns | Meaning |
| -------- | ---- | ------- | ------- |
| `GET /healthz` | None | `{"status": "ok"}` | Process is alive and can serve HTTP. Kubernetes `livenessProbe` target. |
| `GET /readyz` | None | `200` or `503` | `200` = this instance is the leader (drives Darwins, routes mutations). `503` = follower (warm standby). Kubernetes `readinessProbe` target — only the leader receives traffic. |
| `GET /metrics` | None | Prometheus text format | Standard metrics scrape endpoint. |

**Interpretation:**
- `/healthz` 200 but `/readyz` 503 → this pod is a healthy follower. Normal in HA mode.
- `/healthz` failing → the process is dead or hung. Kubernetes will restart it.
- `/readyz` flapping → leader election is unstable (Postgres connection issues, network partition).

## Metrics reference

### Prometheus metric names

`GET /metrics` is the authoritative list — run `curl -s localhost:8000/metrics`
for the complete, current set. The operationally important metrics:

| Metric | Type | What it means |
| ------ | ---- | ------------- |
| `darwin_platform_darwins` | gauge | Live Darwin count. |
| `darwin_platform_uptime_seconds` | gauge | Supervisor process uptime. |
| `darwin_neuron_dispatch_total` | counter | Neuron dispatches; labelled by `darwin_id`, `neuron`, `status`. |
| `darwin_daily_spend_usdc` | gauge | Per-Darwin spend today (`darwin_id`). |
| `darwin_llm_cost_usdc` / `darwin_revenue_usdc` | gauge | Per-Darwin LLM cost and revenue (`darwin_id`). |
| `darwin_platform_alerts_total` | counter | Monitor alerts fired. |
| `darwin_platform_escalations_open` | gauge | Open escalation tickets. |
| `darwin_platform_audit_entries_total` | counter | Audit records persisted. |
| `darwin_platform_staging_evaluations_total` / `darwin_platform_staging_quota` | counter / gauge | Patch-staging activity and the fleet concurrency quota. |
| `darwin_platform_sanctions_refusals_total` | counter | Outbound transfers refused by sanctions screening. |
| `darwin_platform_billing_revenue_usdc` / `darwin_platform_billing_harvest_fees_usdc` | gauge | Platform billing totals. |

### Alert thresholds (default `Quotas`)

| Detector | WARN threshold | CRITICAL threshold | Graded response |
| -------- | -------------- | ------------------ | --------------- |
| `spend_velocity` | > $100/hour | > $300/hour | Level 2 (throttle) → Level 3 (pause) |
| `failure_rate` | > 30% lifetime | > 60% lifetime | Level 2 → Level 3 |
| `heartbeat` | > 1200s gap | > 3000s gap | Level 1 (log) → Level 3 (pause) |
| `escalation_rate` | > 5/hour | > 20/hour | Level 1 → Level 3 |
| `spread_spend` | > 30 debits/hour below $1 floor | > 80 debits/hour | Level 2 → Level 3 |

### What to look at first

When something seems wrong:

1. **`GET /darwins/{id}`** → alive? paused? killed? balance?
2. **`GET /darwins/{id}/events?limit=20`** → last 20 audit entries. Look for `outcome:fail`, `event:kill`, `event:death`.
3. **Prometheus** → `darwin_neuron_dispatch_total{status="fail"}` rate. Spike = the Darwin is stuck on a broken neuron path.
4. **Jaeger** → trace by `darwin_id` tag. Long spans = slow LLM calls or hung browser sessions.

## Failure modes and recovery

These are the predicted failure modes from [`18-layer-zero-build-plan.md`](18-layer-zero-build-plan.md) § "What goes wrong and what we'll do", turned into concrete runbook entries.

---

### FM-1: The planner's system prompt is too vague

**Detection:** The Darwin dispatches neurons but with random/incoherent tasks. The `plan` field in audit records has no clear structure. Revenue is zero despite many dispatches.

**Signals:**
- High `darwin_neuron_dispatch_total` with status=ok but zero revenue growth in ledger.
- Planner LLM responses (logged in audit records) show aimless output.

**Response:**
1. Pause the Darwin: `POST /darwins/{id}/pause`.
2. Review the planner outputs in the audit log: look for the `intent:plan` records.
3. Edit `src/darwin_dna/orchestrator_skeleton/prompts/planner_system.txt` with tighter examples and structured output schema.
4. Restart with the updated prompt (new deploy → fresh boot from checkpoint).

**Verification:** After restart, the next 10 plan outputs show structured, goal-directed decisions.

---

### FM-2: LLM cost burn exceeds revenue

**Detection:** The `spend_velocity` monitor fires WARN or CRITICAL. The billing ledger shows metered costs growing but revenue flat.

**Signals:**
- `darwin_platform_alerts_total{kind="spend_velocity"}` incrementing.
- `darwin_daily_spend_usdc` increasing faster than expected (> $20/day for a content Darwin).

**Response:**
1. Check which tier is burning money: filter audit records by `intent:*` kind and look at `cost_usdc` in outcomes.
2. If the planner is calling reasoning-tier too often: adjust `Hyperparameters.planner_tier` to `fast` temporarily.
3. If mutation proposals are burning coding-tier: raise `Hyperparameters.min_dispatches_before_mutation` so the Darwin earns first.
4. If browser sessions are expensive: check `browser_cache.py` promotion stats — is the cache working?

**Verification:** After adjustment, daily LLM cost drops below $10 and the Darwin's balance trajectory stabilizes or grows.

---

### FM-3: The reflect node triggers mutation too often

**Detection:** `darwin_platform_staging_evaluations_total` is high relative to heartbeats. The Darwin spends more time coding than earning.

**Signals:**
- Multiple `intent:mutation.propose` records per hour.
- `recurring_failures` dict in the checkpoint has entries with count=3 (the mutation gate threshold) cycling rapidly.

**Response:**
1. Review the failure classes: are they genuine recurring issues or normal variance?
2. If variance: tighten the recurrence definition in `Hyperparameters` — require same error *code* (not just same error message) and a longer observation window (e.g., 24h instead of 8h).
3. If genuine failures but non-fixable: add the failure class to `dead_ends` in the mutation archive so the Darwin stops re-proposing.

**Verification:** Mutation proposal rate drops to < 1/day. The Darwin's heartbeat time is spent on earning, not coding.

---

### FM-4: Treasury policy engine bug (payment leaked)

**Detection:** The ledger shows a payment that should have been blocked by the treasury policy. The wallet balance is lower than the policy should allow.

**Signals:**
- Ledger → wallet reconciliation shows a discrepancy where wallet < ledger_expected.
- Audit log shows `outcome:ok` on a payment that should have been `outcome:refused`.
- The Coinbase Agentic Wallet's own policy enforcement may have caught it (check Coinbase dashboard).

**Response — this is the worst case:**
1. **IMMEDIATELY** kill the Darwin: `POST /darwins/{id}/kill` with reason "treasury policy breach".
2. Document the specific transaction: amount, recipient, timestamp, which policy should have blocked it.
3. Check the Coinbase wallet dashboard: did the wallet-level hard cap catch it? If yes, the actual loss is bounded.
4. Post-mortem: identify the policy engine bug. Write a regression test.
5. Patch the policy engine. Cut a new Layer 0 version.
6. For any existing Darwins: use forced reproduction with amendment layer to propagate the fix.

**Verification:** The regression test exercises the exact scenario that leaked. The patched policy engine correctly refuses.

---

### FM-5: Audit log out of sync with ledger

**Detection:** The daily Merkle anchor reconciliation fails, OR the soak test's invariant I7 (ledger reconciles to wallet with zero discrepancy) fails.

**Signals:**
- `AuditError` in logs ("Merkle anchor divergence" or "audit chain broken").
- The Darwin halts automatically (audit failure → halt is a hard invariant).

**Response:**
1. The Darwin is already halted (by design — "no log, no action").
2. Export the full audit chain: `GET /darwins/{id}/events`.
3. Export the wallet's on-chain transaction history from the block explorer.
4. Identify the point of divergence: which entry is missing or mismatched?
5. If the audit has a gap: the chain is permanently broken for that Darwin. The Darwin cannot resume. Document, learn, add a regression test.
6. If the ledger is wrong but the audit is intact: manually correct the ledger entry and resume the Darwin from checkpoint.

**Verification:** New Darwin spawned after the fix passes the 90-day sim with zero reconciliation errors.

---

### FM-6: Darwin → fiat payout failed on leg 2

**Detection:** A `darwin_fiat_payout` returned 409 from the fiat ramp (Stripe Connect / MoonPay outage), OR the audit log shows the supervisor's `darwin_fiat_payout_second_leg_failed` event.

**Signals:**
- `OmnibusError("fiat ramp withdraw failed: ...")` in the audit metadata.
- The Darwin's USDC is debited (leg 1 succeeded) but no fiat receipt in the ramp dashboard.
- `omnibus.ledger()` shows no `DARWIN_FIAT_PAYOUT` entry for that timestamp.

**Response:**
1. Confirm the ramp is back up (Stripe status, MoonPay dashboard). Don't refund until you've decided whether to retry.
2. **If retrying:** re-run `POST /darwins/{id}/fiat_payment` with a fresh `Idempotency-Key`. The omnibus already has the USDC from leg 1, so the second leg will fire against the existing balance.
3. **If giving up:** call the refund route to put the Darwin's USDC back where it came from.

```bash
# Refund via curl
curl -X POST "$PLATFORM/darwins/$DID/fiat_payment/refund" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"amount_usdc": 25.0, "reason": "ramp_outage_2026-05-30"}'
```

Or via SDK:

```python
from darwin_platform.client import DarwinClient, FiatRefundRequest

client = DarwinClient(base_url=PLATFORM, token=TOKEN)
client.darwin_fiat_refund(
    darwin_id=did,
    request=FiatRefundRequest(amount_usdc=25.0, reason="ramp_outage"),
)
```

Or via dashboard: open `/darwins/{id}/payments`, fill the "Refund failed payout" card, click submit.

**Verification:** The Darwin's wallet `balance_usdc` returns to the pre-payout value; the omnibus ledger gains a `REFUND_TO_DARWIN` entry; `audit_log.entries(action="refund_fiat_payout")` shows the operator's reason.

---

### FM-7: Spawn-time omnibus fund_child skipped

**Detection:** A new Darwin's wallet shows the FakeWallet's symbolic opening balance, but the owner's `user_credits` weren't debited and no `FUND_CHILD` entry was recorded on the omnibus ledger. The audit log shows `omnibus_fund_skipped`.

**Signals:**
- Chain RPC outage in the supervisor logs around spawn time, OR
- `user_credits[owner_subject]` was 0 at spawn time and the user has since deposited.

**Response:**
1. Confirm the chain is healthy + the user now has enough credit.
2. Retry the fund explicitly:

```bash
curl -X POST "$PLATFORM/darwins/$DID/omnibus/fund" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"amount_usdc": 100.0}'
```

```python
client.retry_omnibus_fund(darwin_id=did, amount_usdc=100.0)
```

Or via dashboard: `/darwins/{id}/payments` → "Retry omnibus fund" card.

**Notes:**
- Refuses for anonymous-owned Darwins — no `user_credits` to attribute the debit to.
- Surface area: this drives `omnibus.fund_child` directly. Any error (`omnibus_fund_retry_failed`) is the same shape an operator sees from a manual `fund_child` call.

**Verification:** `omnibus.ledger()` gains a `FUND_CHILD` entry; `user_credits[owner_subject]` is debited by the amount; the Darwin's on-chain balance now matches the symbolic opening.

---

## Common operator workflows (payments)

### Manage owner pre-approvals

Pre-approvals are typed exceptions to layer-4 escalation. An owner who pays Upwork escrow weekly doesn't want to approve every $30 payment manually.

**Via dashboard:** `/darwins/{id}/preapprovals` lists active rules and exposes install / revoke. The card view shows `used_count / max_uses` and expiry countdown so quota state is visible at a glance.

**Via SDK:**

```python
from darwin_platform.client import DarwinClient, PreApprovalSpec

client = DarwinClient(base_url=PLATFORM, token=TOKEN)

# Install: "auto-allow up to 10 Upwork freelancer payments under $50,
# expiring in 7 days".
client.set_preapproval(
    darwin_id=did,
    rule_id="upwork-weekly",
    spec=PreApprovalSpec(
        max_amount_usdc=50.0,
        counterparty_category="freelancer",
        counterparty_substring="upwork",
        max_uses=10,
        expires_at=time.time() + 7 * 86400,
    ),
)

# List active rules + their consume counter.
for rule in client.list_preapprovals(did):
    print(rule["rule_id"], rule["used_count"], "/", rule.get("max_uses"))

# Revoke.
client.revoke_preapproval(did, "upwork-weekly")
```

**Inheritance:** When a parent reproduces, its rules are copied onto the child's wallet with `used_count` reset to 0. The owner can revoke per-child if they want a narrower scope.

### Register a freelancer (KYC gate)

A `darwin_fiat_payment` only fires against a freelancer registered for the calling owner. Operators onboard the freelancer once; subsequent payments resolve through the registry.

**Via dashboard:** `/freelancers` (owner self-serve, `/me/freelancers` server-side).

**Via SDK:**

```python
from darwin_platform.client import DarwinClient, FreelancerRegistration

client.register_freelancer(FreelancerRegistration(
    owner_subject="alice@example.com",
    freelancer_id="upwork:writer-42",
    fiat_destination="acct_1QAbcDeFgHiJk",
    display_name="Jane Writer",
    kyc_status="verified",
))
```

### Send a Darwin fiat payment

After the genome's facilitator escalates a freelancer payment and the owner approves the escalation, the operator settles it via fiat:

```python
from darwin_platform.client import DarwinClient, FiatPaymentRequest

receipt = client.darwin_fiat_payment(
    darwin_id=did,
    request=FiatPaymentRequest(
        amount_usdc=25.0,
        freelancer_id="upwork:writer-42",
        escalation_id="esc-9001",   # ties payout to approval
    ),
)
print(receipt["tx_hash"], receipt["metadata"]["provider"])
```

Refuses with 409 if the freelancer isn't registered for the Darwin's `owner_subject` — the registry is the platform's KYC gate.

For recovery from leg-2 failures see [FM-6](#fm-6-darwin--fiat-payout-failed-on-leg-2). For recovery from skipped spawn-time funding see [FM-7](#fm-7-spawn-time-omnibus-fund_child-skipped).

---

## Leader failover procedure

When the leader pod dies (eviction, crash, OOM):

1. The Postgres advisory lock auto-releases (connection drop).
2. A follower pod's polling loop wins the lock within one heartbeat (~1s).
3. The new leader reads `darwin_lifecycle WHERE alive=1` from Postgres.
4. For each alive Darwin: loads manifest + checkpoint from persistence, re-boots the runtime, resumes the drive loop.
5. `/readyz` flips to 200 on the new leader; the k8s Service routes traffic there.

**How to verify failover is working:**
```bash
# In a dev cluster, kill the leader pod:
kubectl delete pod -n darwin -l app=darwin-platform --field-selector status.phase=Running

# Watch readyz on the remaining pod:
kubectl exec -n darwin <follower-pod> -- curl -s http://localhost:8000/readyz
# Should return 200 within ~5 seconds.

# Check the Darwin is still running:
curl -s http://darwin-platform:8000/darwins/<id> | jq .alive
# Should be true.
```

**What breaks:** If the follower cannot reach Postgres (network partition), it won't win the lock. The fleet is paused until connectivity restores. No data loss — Darwins just don't advance.

## Soak test operation guide

### Starting a soak run

```bash
# Against Docker Compose:
docker compose up -d
uv run python -m tools.soak.run_soak --duration 24h --url http://localhost:8000

# Against a remote cluster:
uv run python -m tools.soak.run_soak --duration 24h \
  --url https://darwin-dev.internal:8000 \
  --auth-token owner-dev-token
```

### Monitoring during the soak

- **Terminal 1:** soak script progress (polls status every 60s, prints balance + tick).
- **Terminal 2:** `docker compose logs -f supervisor` (or `kubectl logs -f`).
- **Prometheus:** `http://localhost:9090` — watch `darwin_neuron_dispatch_total` rate (should be steady), `darwin_daily_spend_usdc` (should rise slowly).
- **Jaeger:** `http://localhost:16686` — spot-check one trace every few hours for unexpectedly slow spans.

### Interpreting soak results

| Outcome | Meaning | Action |
| ------- | ------- | ------ |
| All 6 criteria pass | Phase 0 gate met | Cut a repo release (`make release VERSION=…`) and/or tag the deploy image |
| Criterion 1 fails (crash within 24h) | Unhandled exception in the loop | Read the crash log; fix; re-run |
| Criterion 2 fails (< 50 dispatches or < 80% success) | Darwin is stuck or failing too much | Check FM-1 (vague planner) or FM-3 (over-mutating) |
| Criterion 3 fails (broken hash chain) | Audit integrity issue | Check FM-5 |
| Criterion 4 fails (ledger mismatch) | Reconciliation bug | Check FM-5 |
| Criterion 5 fails (kill > 5s) | Kill-switch latency issue | Check heartbeat interval; is the Darwin sleeping too long between polls? |
| Criterion 6 fails (cannot replay) | Checkpoint or audit persistence issue | Verify persistence is enabled (`--db`); check disk space |

### Recovering from a failed soak

1. Don't wipe the state — the failure is valuable forensic data.
2. Export the full state: `docker exec darwin-supervisor sqlite3 /data/state.db .dump > soak_failure.sql`
3. Identify the root cause using the failure mode guides above.
4. Fix and re-run. A passing soak after a fix counts.

## Emergency procedures

### Fleet pause (all Darwins)

```bash
# Via API (requires platform_operator token):
for id in $(curl -s -H "Authorization: Bearer $OPS_TOKEN" \
  http://localhost:8000/darwins | jq -r '.[].darwin_id'); do
  curl -X POST -H "Authorization: Bearer $OPS_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"reason": "fleet pause: <reason>"}' \
    "http://localhost:8000/darwins/$id/pause"
done
```

All Darwins halt at their next checkpoint. No data loss. Resume by POSTing to `/resume` for each.

### Fleet kill (nuclear option)

Same loop but `POST /darwins/{id}/kill`. Irreversible. Use only when the fleet is compromised.

### Signing key rotation

```bash
# The supervisor generates a new key on startup when persistence has none.
# To rotate: retire the current key, restart the supervisor.

# 1. Note the current public key:
curl -s http://localhost:8000/platform/signing/public_key

# 2. Restart the supervisor (it will generate a new key):
docker compose restart supervisor

# 3. Verify the new key is different:
curl -s http://localhost:8000/platform/signing/public_key

# Manifests signed by the old key remain valid during the drain window.
# New Darwins spawned after rotation get manifests signed by the new key.
```

### Vault seal (emergency)

If Vault is compromised, seal it to prevent all reads:

```bash
vault operator seal
```

All Darwin vault reads will fail. Neurons that need credentials will get `VaultAccessError` → escalation. This is a hard stop for all business operations but protects secrets.

## Log reading guide

### Structlog fields

The supervisor emits JSON-structured logs. Key fields:

| Field | Meaning |
| ----- | ------- |
| `event` | Log message / event name |
| `darwin_id` | Which Darwin (if applicable) |
| `level` | `info`, `warning`, `error` |
| `ts` | ISO timestamp |
| `span_id` / `trace_id` | OpenTelemetry correlation (when tracing enabled) |
| `tick` | Darwin's heartbeat counter |

### Correlating events

To follow one Darwin's life:
```bash
docker compose logs supervisor | grep '"darwin_id":"<id>"' | jq .
```

To find the moment something went wrong:
```bash
docker compose logs supervisor | grep '"level":"error"' | jq '{ts, event, darwin_id}'
```

### Trace lookup

With Jaeger running, find a slow/failed operation:
1. Open `http://localhost:16686`.
2. Service: `darwin-platform`.
3. Tag: `darwin_id=<id>`.
4. Sort by duration descending — the longest span is usually the bottleneck.

## Repository releases (semver)

Package version (`pyproject.toml`), `CHANGELOG.md`, and git tags for this repo are managed from the root Makefile — not the Layer 0 DNA signing flow in doc 18.

**Prerequisites:** [git-cliff](https://git-cliff.org) on `PATH`; commits on `main` follow [Conventional Commits](https://www.conventionalcommits.org/) (see root `README.md`).

| Step | Command |
| ---- | ------- |
| Sync `[Unreleased]` from commits since the latest tag | `make changelog-sync` (alias `make changelog`) |
| Preview without writing the file | `make changelog-preview` |
| Cut a release (CHANGELOG + version bump + commit + tag + push) | `make release VERSION=0.0.2` |

What `make release` does:

1. Inserts `## [X.Y.Z] - <date>` from git history since the previous tag and clears `[Unreleased]`.
2. Sets `version = "X.Y.Z"` in `pyproject.toml`.
3. `git add CHANGELOG.md pyproject.toml` → `git commit -m "chore(release): prepare vX.Y.Z"`.
4. `git tag -a vX.Y.Z` on that commit.
5. `git push && git push --tags`.

`changelog-sync` **replaces** the whole `[Unreleased]` section; put narrative in commit messages or edit after sync. Opt out of git remotes locally: `NO_PUSH=1`, `NO_COMMIT=1`, `NO_TAG=1`, or `NO_BUMP=1`. Implementation: `scripts/changelog.py` + `cliff.toml`.

After a passing Phase 0 soak (above), a platform deploy may still use a container image tag (`helm install … --set image.tag=…`) — that is independent of this repo semver.

## Periodic operational tasks

| Task | Frequency | Procedure |
| ---- | --------- | --------- |
| Review operator audit log | Daily | `GET /platform/audit` — confirm no unexplained entries |
| Check billing reconciliation | Daily | `GET /platform/billing` — sum of all Darwin balances ≈ omnibus wallet |
| Rotate secrets | Monthly | Signing key rotation (see above); Vault token rotation |
| Backup verification | Weekly | Restore from the latest SQLite/Postgres backup and verify a Darwin can boot |
| Update dependencies | Biweekly | `uv lock --upgrade` → review diff → run full test suite → merge |
| Red-team checklist | Per phase gate | Run the full checklist from [`24-security-threat-model.md`](24-security-threat-model.md) |
