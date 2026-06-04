# Operational drills

Scripted exercises for the safety-critical procedures named in
[`docs/engineering/09 §operational drills`](../../docs/engineering/09-kill-switches-and-owner-controls.md).
The point is to practise them in a controlled drill *before* the
moment they're needed for real, so the operator at 3am isn't reading
docs.

Run them against a **dev or staging** platform, not production —
unless production drilling is the explicit exercise.

| Drill | Module | Prereqs | Verifies |
| --- | --- | --- | --- |
| Kill-switch | `tools.drills.kill_switch` | A running `darwin-platform serve` | Spawned Darwin halts within 5 s of `POST /darwins/{id}/kill`. Reuses the soak gate's `criterion_5_kill_latency`. |
| kill -9 recovery | `tools.drills.kill9_recovery` | None — manages its own supervisor subprocess; use a persistent `--db` | SIGKILLs the supervisor mid-run and restarts it on the same DB; asserts the Darwin resumes from checkpoint (tick advances), **zero** pre-kill audit entries are lost, and the hash chain still verifies across the restart boundary (doc-20 DoD). |
| DR failover | `tools.drills.dr_failover` | Two supervisor replicas sharing one Postgres | Killing the leader causes a follower to acquire the advisory lock and `/readyz` → 200 within 30 s. |
| Vault seal/unseal | `tools.drills.vault_seal` | Reachable Vault + admin token + unseal key(s) + `[hvac]` extra | `sys/seal` → confirms sealed → `sys/unseal` with key share(s) → confirms unsealed. Optional platform `/readyz` probe at each phase. |

## Quick usage

```bash
# Kill-switch (against a local platform)
uv run python -m darwin_platform serve --heartbeat-seconds 0.05 &
uv run python -m tools.drills.kill_switch

# kill -9 recovery (self-contained — manages its own supervisor)
uv run python -m tools.drills.kill9_recovery --heartbeat-seconds 0.1
# Real-adapter pace (pass serve flags after --):
#   uv run python -m tools.drills.kill9_recovery --heartbeat-seconds 5 \
#       --warmup-seconds 60 -- --wallet-provider cdp --litellm-url http://localhost:4000

# DR failover (against two supervisors sharing Postgres)
uv run python -m tools.drills.dr_failover \
    --replica http://127.0.0.1:8001 \
    --replica http://127.0.0.1:8002

# Vault seal (against a dev Vault)
vault server -dev -dev-root-token-id=devroot &
uv run python -m tools.drills.vault_seal \
    --vault-addr http://127.0.0.1:8200 \
    --vault-token devroot \
    --unseal-key <PRINTED_KEY>
```

Each drill exits **0** on PASS, non-zero on FAIL — drill results can be
piped into a runbook ledger or a Slack notifier.

## Verification status

- **Kill-switch:** locally verified 2026-05-25 against an in-process
  supervisor; PASS, 0.07 s latency.
- **kill -9 recovery:** locally verified 2026-06-03 (FakeWallet,
  `--heartbeat-seconds 0.1`, `--warmup-seconds 6`); PASS — resumed
  tick 50→51, 358 pre-kill audit events intact, hash chain valid across
  the restart boundary. Re-run with real adapters before mainnet.
- **DR failover:** code reviewed; not locally drilled (requires
  Postgres + two supervisor replicas). First operator run *is* the
  test — record the result in `docs/postmortems/` if anything surprises.
- **Vault seal/unseal:** code reviewed; not locally drilled (requires
  Vault server). Same caveat — first operator run is the test.

A drill that's never been run is a drill that doesn't work. Run each
of these end-to-end against a dev environment before relying on them
during an incident.
