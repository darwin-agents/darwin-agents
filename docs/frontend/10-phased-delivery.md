# Phased delivery

Aligned with [`../overview/06-roadmap-and-phases.md`](../overview/06-roadmap-and-phases.md) and engineering build plans 20 / 21 / 22.

## Phase 0 — Operator observability (weeks 0–8)

**Goal:** Prove a human can spawn, watch, and kill a Darwin without curl.

| Deliverable | Acceptance |
|-------------|------------|
| `web/` package scaffold | `pnpm build` passes in CI |
| Single fleet page | Lists Darwins from `GET /darwins` |
| Event log page per Darwin | Paginated `GET /events`, filter by type |
| Kill button | Confirm modal → `POST /kill`; status updates |
| Dev bearer auth | Documented token flow |
| No WebSocket | Polling acceptable |

Explicitly **out of scope:** Anatomy canvas, Clerk, vault, billing UI, mobile.

Definition of done (from doc 20): engineer who didn't write supervisor can observe and kill via UI during soak.

**Backend dependencies:** existing API only.

---

## Phase 1 — Evolution visibility (weeks 8–16)

**Goal:** Support evolution loop operators; still not full consumer polish.

| Deliverable | Acceptance |
|-------------|------------|
| Lineage page | Renders `GET /lineage/tree` (list or d3) |
| Mutation / reproduction events | Distinct Timeline row types |
| Spawn wizard (minimal) | `POST /darwins` with budget |
| Per-Darwin settings stub | Pause/resume + force reproduce |
| Treasury chart | Recharts on ledger + billing endpoints |

**Out of scope:** Full Anatomy, Clerk, escrow inbox polish, mobile.

---

## Phase 2 — Multi-tenant alpha (weeks 16–28)

**Goal:** Friendly users fund Darwins, watch live, resolve escalations on phone.

| Deliverable | Acceptance |
|-------------|------------|
| Clerk (or WorkOS) | Sign-up → platform JWT |
| Full per-Darwin dashboard | Anatomy + Timeline linked |
| WebSocket stream | <2s perceived latency for new events |
| Vault + data browser | Read/revoke per docs 07/16 |
| Escalation inbox + respond | End-to-end with push |
| Expo mobile MVP | ≥1 alpha user resolves captcha/approval via push |
| Billing + deposits | Stripe ramp UI |
| Harvest rules UI | CRUD + force harvest |
| Owner controls complete | All doc 09 controls |
| Browser takeover | Captcha escalation embed |

Definition of done (roadmap): 5+ alpha users run ≥2 weeks; ≥1 escalation resolved via mobile push.

**Backend dependencies:**

- `GET /darwins/{id}/anatomy`, `WSS .../stream`
- `GET /darwins/{id}/data/{table}`
- Clerk token exchange
- FCM notification sink

Module map reference: [`../engineering/22-phase-two-multi-tenant-build-plan.md`](../engineering/22-phase-two-multi-tenant-build-plan.md) (`web/`, `mobile/`).

---

## Phase 3+ — Deferrals

| Item | When |
|------|------|
| Marketing site in Next.js | Public beta |
| i18n | Post-beta |
| Full mobile parity | Phase 4 |
| Operator admin in same app | Never — separate internal app |
| PWA offline | Not planned |

---

## CI checklist (when `web/` exists)

```yaml
# Illustrative — add to .github/workflows when implemented
- pnpm install --frozen-lockfile
- pnpm lint
- pnpm typecheck
- pnpm test
- pnpm build
- playwright test (optional Phase 2)
```

## Open API tasks for backend

Track alongside frontend milestones:

1. OpenAPI published at `/openapi.json` for codegen.
2. Anatomy snapshot endpoint.
3. Multiplexed Darwin WebSocket stream.
4. Aggregated `GET /escalations` for owner (optional; else N+1 in inbox).
5. Data browser read API with RLS-safe table list.

---

## Effort estimate (rough)

| Phase | Frontend person-weeks |
|-------|----------------------|
| 0 | 1–2 |
| 1 | 2–3 |
| 2 | 8–12 (includes mobile 2–3) |

Parallelize with backend WS/auth; Phase 2 is the first user-facing quality bar.
