#!/usr/bin/env bash
#
# Darwin platform smoke test — proves a real, end-to-end interaction:
#   1. spawns a Darwin (parent A)
#   2. watches it THINK via the real LLM + act through its neurons
#   3. forces it to REPRODUCE — a genuine cross-Darwin economic interaction
#      (parent A funds child B out of its wallet; a lineage edge is created)
#   4. shows the lineage tree + both wallets + real LLM billing
#   5. kills both to stop the spend meter
#
# Auth is permissive when the supervisor runs without --auth-tokens (the
# compose default), so no bearer token is needed. Requires: curl, jq.
#
#   ./scripts/smoke_test.sh                  # against http://localhost:8000
#   BASE=http://host:8000 ./scripts/smoke_test.sh
#
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
OPENING="${OPENING:-100}"        # opening_usdc for the parent
WATCH_SECS="${WATCH_SECS:-45}"   # how long to watch the parent think
# Bearer token. When the supervisor runs with --auth-tokens, use the
# platform_operator token so this script can reach BOTH the parent and the
# reproduced child (an owner token only auto-owns ids it spawns directly).
# Leave empty for permissive (no --auth-tokens) mode.
TOKEN="${TOKEN:-}"
AUTH=(); [ -n "$TOKEN" ] && AUTH=(-H "authorization: Bearer $TOKEN")
api() { curl -fsS "${AUTH[@]}" "$@"; }   # fail loud on non-2xx

command -v jq  >/dev/null || { echo "need jq";  exit 1; }
command -v curl >/dev/null || { echo "need curl"; exit 1; }

echo "== 0. health =="
api "$BASE/healthz" && echo " ok"

echo; echo "== 1. spawn parent A (opening \$$OPENING) =="
A=$(api -X POST "$BASE/darwins" -H 'content-type: application/json' \
      -d "{\"opening_usdc\": $OPENING, \"escalation_auto\": \"approve\"}" \
    | tee /dev/stderr | jq -r .darwin_id)
echo "parent A = $A"

echo; echo "== 2. watch A think for ${WATCH_SECS}s (real LLM + neuron events) =="
# Tail the event log incrementally; surface only LLM + neuron + payment kinds
# so you can SEE real cognition (kind=llm.*) rather than the FakeLLM.
since=0
deadline=$(( $(date +%s) + WATCH_SECS ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  page=$(api "$BASE/darwins/$A/events?since=$since&limit=200")
  # Real cognition shows up as perceive/plan/reflect/neuron/outcome events;
  # the LLM calls are embedded inside the plan/reflect steps (not separate
  # llm.* events), so match the drive-loop kinds directly.
  echo "$page" | jq -r '.events[]
    | select(.kind | test("perceive|plan|reflect|neuron|outcome|payment|reproduc|birth|harvest|escal"))
    | "  seq=\(.seq) \(.kind)  \(.payload.tier // .payload.neuron_id // .payload.summary // "")"'
  since=$(echo "$page" | jq -r '(.events | map(.seq) | max) // '"$since")
  sleep 5
done

echo; echo "== 3. status + did the real LLM actually run? =="
api "$BASE/darwins/$A" | jq '{darwin_id, alive, tick, balance_usdc, days_elapsed}'
echo "-- cognitive event histogram (proves the drive loop ran):"
api "$BASE/darwins/$A/events?limit=1000" | jq '[.events[].kind] | group_by(.) | map({(.[0]): length}) | add'
echo "-- billing.infrastructure.events = metered LLM/compute calls:"
api "$BASE/darwins/$A/billing" | jq '.infrastructure'

echo; echo "== 4. force A to reproduce -> child B (parent funds child) =="
api -X POST "$BASE/darwins/$A/reproduce/force" | jq .
echo "   (waiting for child to be born...)"
B=""
for _ in $(seq 1 12); do
  sleep 5
  B=$(api "$BASE/darwins/$A/lineage" \
      | jq -r '[.edges[]? | select(.parent_id=="'"$A"'") | .child_id] | last // empty')
  [ -n "$B" ] && break
done
[ -n "$B" ] && echo "child B = $B" || echo "no child yet (raise WATCH_SECS / check A's balance vs reproduction cost)"

echo; echo "== 5. the interaction, made visible =="
echo "-- lineage tree from A:"
api "$BASE/darwins/$A/lineage/tree" | jq .
echo "-- A wallet (should have dropped by the child's seed + its own spend):"
api "$BASE/darwins/$A" | jq '{darwin_id, balance_usdc}'
if [ -n "$B" ]; then
  echo "-- B wallet (seeded by A):"
  api "$BASE/darwins/$B" | jq '{darwin_id, parent_id, generation, balance_usdc}'
  echo "-- A ledger (look for the transfer_out_for_child / reproduction debit):"
  api "$BASE/darwins/$A/ledger" | jq '.[-5:]? // .'
fi

echo; echo "== 6. cleanup: kill both to stop the spend meter =="
api -X POST "$BASE/darwins/$A/kill" -H 'content-type: application/json' \
    -d '{"reason":"smoke test done"}' | jq .
[ -n "$B" ] && api -X POST "$BASE/darwins/$B/kill" -H 'content-type: application/json' \
    -d '{"reason":"smoke test done"}' | jq .
echo "done."
