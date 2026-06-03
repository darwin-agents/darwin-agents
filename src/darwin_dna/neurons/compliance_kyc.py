"""compliance.kyc_submitter — the sensitive-read neuron (docs/engineering/16, 04).

This is the consumer that closes the **escalation → owner grant → sensitive
read** loop. The tier model and the platform's one-time grant minting already
exist (a SENSITIVE read requires a fresh owner-granted token; the supervisor
mints one when an owner approves a ``kyc`` escalation and threads it back into
the resumed task's payload). What was missing — and what the docs-vs-impl audit
flagged ("no neuron performs a sensitive read; the loop fails closed") — is a
neuron that actually *asks* for and *uses* a sensitive secret. Without one the
SENSITIVE tier is unreachable in production.

The loop this neuron drives:

1. **First pass** — the neuron needs the owner's KYC document (a SENSITIVE-tier
   secret) to submit it to a verification provider, but has no grant token. It
   escalates with a ``kyc`` blocker and parks. (It does *not* read anything.)
2. **Owner approves** — the platform recognises the ``kyc`` ticket class, mints
   a single-use grant token, and attaches it to the approval response.
   ``perceive`` threads it into this task's payload; the task is re-dispatched.
3. **Resume pass** — the neuron now holds ``grant_token``. It performs the
   SENSITIVE read (the token self-destructs on use), submits the document to
   the provider endpoint, and returns only the *outcome* — never the document.

Secret hygiene (P7, doc 16): the document value is never placed in the
NeuronResult output and never logged to the audit. The audit records only the
document *key*, the endpoint, and the submission status — the blast-radius
metadata, not the secret. The single legitimate place the value travels is the
HTTP body to the verification provider, which is the whole point of the read.
"""

from __future__ import annotations

from ..machinery.vault import VaultAccessError, VaultTier
from ._base import (
    BaseNeuron,
    Blocker,
    CostEstimate,
    FailureMode,
    NeuronContext,
    NeuronResult,
    Plan,
    Task,
)


class ComplianceKycSubmitter(BaseNeuron):
    name = "compliance.kyc_submitter"
    version = "1.0.0"
    # ``web.operate`` so the orchestrator grants the x402 HTTP client (the
    # submission channel); the read itself is gated by the owner grant, not by
    # a capability scope, so no extra scope tag is needed to reach SENSITIVE.
    capability_tags = frozenset({"compliance.submit", "web.operate"})
    cost_estimate = CostEstimate(
        usd_p50=0.0, usd_p95=0.02,
        latency_p50_ms=400, latency_p95_ms=5000,
        # High by design: the first pass almost always escalates for owner
        # approval before any sensitive data can be touched.
        escalation_probability=0.9,
    )
    failure_modes = [
        FailureMode(
            code="kyc",
            description="owner approval required to release the KYC document",
            escalation_hint="owner approves; the platform mints a one-time "
            "vault grant for the release",
        ),
        FailureMode(
            code="grant_invalid",
            description="owner grant token missing, forged, or already spent",
            escalation_hint="not human-actionable; re-request approval",
        ),
        FailureMode(
            code="document_missing",
            description="no KYC document is stored under the requested key",
            escalation_hint="owner uploads the document first",
        ),
        FailureMode(
            code="no_endpoint",
            description="no submission endpoint / HTTP client configured",
            escalation_hint="owner wires the verification provider endpoint",
        ),
        FailureMode(
            code="submission_failed",
            description="the verification provider rejected the submission",
            escalation_hint="not human-actionable; retry, then surface to owner",
        ),
    ]

    async def plan(self, task: Task, context: NeuronContext) -> Plan:
        # Carry the request fields — including a ``grant_token`` threaded in by
        # perceive on a resume — straight through to execute.
        payload = dict(task.payload)
        return Plan(
            steps=[f"submit KYC document "
                   f"{payload.get('document_key', '<key>')!r}"],
            est_cost=self.cost_estimate,
            payload=payload,
        )

    async def execute(self, plan: Plan, context: NeuronContext) -> NeuronResult:
        document_key = plan.payload.get("document_key")
        endpoint = plan.payload.get("endpoint")
        grant_token = plan.payload.get("grant_token")
        if not document_key:
            return NeuronResult(
                ok=False, error_code="submission_failed",
                output={"reason": "no document_key in task"},
            )
        vault = context.vault
        if vault is None:
            return NeuronResult(
                ok=False, error_code="grant_invalid",
                output={"reason": "no vault view"},
            )

        # First pass: no grant yet → escalate for owner approval. Returning
        # error_code="kyc" is what the dispatch node classifies into the KYC
        # ticket class, which is what makes the supervisor mint the one-time
        # grant on approval. We touch no secret on this pass.
        if not grant_token:
            ticket = await self.escalate(
                Blocker(
                    kind="kyc",
                    detail=f"release KYC document {document_key!r} for "
                    f"submission to the verification provider",
                ),
                context,
            )
            return NeuronResult(
                ok=False, escalated=True, error_code="kyc",
                output={"ticket": ticket.id},
            )

        # Resume pass: present the one-time grant for the SENSITIVE read. The
        # key + endpoint are safe to log; the document value never is.
        intent = context.audit.log_intent(
            "neuron.compliance.kyc_submitter",
            {"document_key": document_key, "endpoint": endpoint},
        )
        try:
            document = vault.get(
                document_key, tier=VaultTier.SENSITIVE, grant_token=grant_token,
            )
        except VaultAccessError:
            # Spent / forged token, or no grant store — fail closed.
            context.audit.log_outcome(intent, "fail", {"reason": "grant_rejected"})
            return NeuronResult(
                ok=False, error_code="grant_invalid",
                output={"reason": "owner grant rejected"},
            )
        except Exception as exc:  # noqa: BLE001 - any vault error fails closed
            context.audit.log_outcome(intent, "fail", {"reason": "vault_error"})
            return NeuronResult(
                ok=False, error_code="grant_invalid",
                output={"reason": str(exc)},
            )
        if document is None:
            context.audit.log_outcome(intent, "fail", {"reason": "document_missing"})
            return NeuronResult(
                ok=False, error_code="document_missing",
                output={"document_key": document_key},
            )

        # USE the document — submit it to the provider. This is the one place
        # the value legitimately travels; it is never returned or audited.
        http = getattr(context, "http", None)
        if http is None or not endpoint:
            context.audit.log_outcome(intent, "fail", {"reason": "no_endpoint"})
            return NeuronResult(
                ok=False, error_code="no_endpoint",
                output={"reason": "no HTTP client or endpoint configured"},
            )
        try:
            resp = http.request(
                "POST", endpoint,
                json={"document_key": document_key, "document": document},
            )
            status = int(getattr(resp, "status_code", 0))
        except Exception as exc:  # noqa: BLE001
            context.audit.log_outcome(intent, "fail", {"reason": "submission_error"})
            return NeuronResult(
                ok=False, error_code="submission_failed",
                output={"reason": str(exc)},
            )
        if 200 <= status < 300:
            context.audit.log_outcome(
                intent, "ok",
                {"document_key": document_key, "status": status},
            )
            # Note: NO document in the output — only the outcome.
            return NeuronResult(
                ok=True,
                output={"submitted": True, "document_key": document_key,
                        "status": status},
            )
        context.audit.log_outcome(intent, "fail", {"status": status})
        return NeuronResult(
            ok=False, error_code="submission_failed", output={"status": status},
        )
