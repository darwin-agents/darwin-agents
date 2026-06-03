"""The Darwin Supervisor (docs/engineering/08).

The Supervisor is the single writer for Darwin lifecycle transitions. In doc-08
production it is a K8s-aware service spawning microVMs; in this first pass it
hosts each Darwin as an asyncio task inside its own process, with a fake stack
(``darwin_fakes.make_platform``) standing in for the platform's external ports.
The Supervisor's public surface is the doc-08 endpoints; HTTP wiring lives in
``api.py``, so the class itself can be driven in-process by tests.

Design notes:

- One asyncio task per Darwin, calling ``runtime.run(max_heartbeats=1)`` in a
  loop. We set ``sleep=lambda _: None`` on the runtime so the in-loop pacing
  is a no-op — the Supervisor controls the gap between heartbeats with an
  ``asyncio.sleep`` so the event loop never blocks even with many Darwins.
- Kill / pause / resume are platform-side flag flips on the fake stack. The
  genome polls those flags at every node boundary (P8) and at the heartbeat
  boundary (doc 09 pause); we never reach into the runtime's state to halt it.
- Force-reproduce sets ``state.reproduce = True`` *between* heartbeats — the
  genome's reproduction guards (treasury minimum, fitness window) still run on
  the next ``check_reproduction`` and may refuse. Forcing isn't a bypass.
- Lineage is tracked by observing the fake spawner: when ``stack.spawner``
  grows, the Supervisor auto-boots the child with ``make_child_platform`` so
  the same Supervisor instance drives the next generation. This is what a real
  platform does (the doc-08 ``POST /platform/signing/dna`` + spawn pair, in
  one process).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from darwin_dna.evolution import DarwinRuntime, boot
from darwin_dna.hyperparameters import DEFAULT_HYPERPARAMETERS
from darwin_fakes import FakeStack, make_child_platform
from darwin_fakes.fake_platform import SigningService

from ..adapters.fiat import (
    FiatDepositReceipt,
    FiatQuote,
    FiatRamp,
    FiatRampError,
    FiatWithdrawReceipt,
)
from ..escalations.escalation import EscalationRouter, NotificationSink, RoutedTicket
from ..evolution.eval_runner import EvalRequest, EvalRunner
from ..evolution.patch_staging import (
    PatchStagingService,
    StagingQuota,
    StagingSandbox,
    StagingVerdict,
)
from ..evolution.soma_merge import SomaMerger
from ..infra.browser_cache import BrowserCacheService, CachedBrowserSubstrate
from ..infra.throttle import (
    THROTTLE_KIND_BROWSER,
    THROTTLE_KIND_EGRESS,
    ResourceThrottle,
    ThrottleError,
    ThrottleService,
    ThrottledBrowserSubstrate,
    ThrottledEgressClient,
    ThrottledSandboxSubstrate,
)
from ..observability.drift_detector import DailyProfile, DriftAlert, DriftDetector
from ..observability.merkle_anchor import (
    DarwinDailyRoot,
    MerkleAnchorPublisher,
)
from ..observability.monitoring import (
    Alert,
    AlertSink,
    DarwinSignals,
    Monitor,
    Quotas,
    Severity,
)
from ..observability.operator_audit import (
    OperatorAuditLog,
    to_dicts as _audit_to_dicts,
)
from ..observability.tracing import annotate_span_with_runtime, span
from ..payments.billing import BillingPolicy, BillingService
from ..payments.harvest_scheduler import (
    HarvestRule,
    HarvestRuleError,
    HarvestScheduler,
)
from ..payments.omnibus_wallet import OmnibusWallet, ReconciliationResult
from ..payments.sanctions import SanctionsScreener
from ..security.kyc import KYCService
from ..security.signing_service import PlatformSigner
from ..security.watermark import HmacWatermarker, NoOpWatermarker, Watermarker
from ..tenancy.cross_pollination import (
    CrossPollinationArchive,
    CrossPollinationError,
    PollinationLesson,
)
from ..tenancy.namespace_provisioner import NamespaceProvisioner
from ..tenancy.provisioning import (
    ProvisioningBackend,
    ProvisioningQuotas,
    ProvisioningService,
    ResourceRecord,
)
from ..tenancy.vault_admin import (
    DEFAULT_SENSITIVE_TTL_SECONDS,
    VaultAdminError,
    VaultAdminService,
    VaultEntryRef,
    VaultRetentionService,
)


# -- small helpers --------------------------------------------------------
def _push_endpoint_origin(endpoint: str) -> str:
    """Strip the path from a Web Push subscription endpoint so the audit
    log records only ``scheme://host``. The full endpoint plus VAPID key
    lets anyone deliver to that browser; never log it raw."""
    if not endpoint:
        return ""
    try:
        scheme, rest = endpoint.split("://", 1)
        host = rest.split("/", 1)[0]
        return f"{scheme}://{host}"
    except Exception:
        return ""


# -- errors ----------------------------------------------------------------
class SupervisorError(Exception):
    """Base for supervisor-side rejections (unknown id, bad state)."""


class UnknownDarwin(SupervisorError):
    def __init__(self, darwin_id: str) -> None:
        super().__init__(f"unknown darwin: {darwin_id}")
        self.darwin_id = darwin_id


# -- public-shape models ---------------------------------------------------
@dataclass(frozen=True)
class DarwinStatus:
    """The doc-08 status surface for one Darwin. Frozen so the API layer can
    serialize it without worrying about mutation between read and send."""

    darwin_id: str
    generation: int
    parent_id: str | None
    l0_version: str
    birth_ts: float
    spawned_at: float
    alive: bool
    paused: bool
    killed: bool
    quarantined: bool
    quarantine_reason: str | None
    death_reason: str | None
    balance_usdc: float
    tick: int
    days_elapsed: float
    open_escalations: int


@dataclass(frozen=True)
class QuarantineState:
    """Doc-09 level-4 graded response. Distinct from owner pause: triggered
    by a confirmed policy/compliance issue, not by the owner; cannot be
    cleared by the owner's ``resume`` endpoint; requires an explicit platform
    ``unquarantine`` (or a kill)."""

    active: bool
    reason: str | None = None
    operator: str | None = None
    applied_ts: float | None = None


@dataclass(frozen=True)
class LineageEdge:
    parent_id: str
    child_id: str
    spawned_at: float
    budget_usdc: float


class LineageRegistry:
    """In-memory record of every parent→child edge the supervisor has
    observed. Persistent variants in :mod:`darwin_platform.persistence`
    subclass this and write-through to SQLite.

    The registry is the source of truth for lineage queries — children,
    parent, ancestry — so a Darwin that's no longer running (because it
    died or because the supervisor restarted) still has a queryable
    lineage view from doc-08's dashboard angle."""

    def __init__(self) -> None:
        self._edges: list[LineageEdge] = []
        self._by_parent: dict[str, list[str]] = {}
        self._by_child: dict[str, str] = {}

    def add(self, edge: LineageEdge) -> None:
        # De-dup on (parent, child); the spawner may surface the same edge
        # twice in pathological reorders, and we never want to double-count.
        if edge.child_id in self._by_child:
            return
        self._edges.append(edge)
        self._by_child[edge.child_id] = edge.parent_id
        self._by_parent.setdefault(edge.parent_id, []).append(edge.child_id)

    def edges(self) -> list[LineageEdge]:
        return list(self._edges)

    def has(self, darwin_id: str) -> bool:
        """True if the registry has ever observed this id as parent or
        child. Used by query paths (e.g. /resources) so a killed Darwin
        with a recorded lineage edge still resolves to UnknownDarwin only
        when the id is truly unknown to the supervisor."""
        return (
            darwin_id in self._by_child
            or darwin_id in self._by_parent
        )

    def parent_of(self, child_id: str) -> str | None:
        return self._by_child.get(child_id)

    def children_of(self, parent_id: str) -> list[str]:
        return list(self._by_parent.get(parent_id, []))

    def ancestry_of(self, darwin_id: str) -> list[str]:
        """Walks up the parent chain. Cycle-safe via a seen set, though a
        legitimate lineage never cycles."""
        out: list[str] = []
        cur = self._by_child.get(darwin_id)
        seen: set[str] = set()
        while cur and cur not in seen:
            out.append(cur)
            seen.add(cur)
            cur = self._by_child.get(cur)
        return out

    def edges_touching(self, darwin_id: str) -> list[LineageEdge]:
        return [
            e for e in self._edges
            if e.parent_id == darwin_id or e.child_id == darwin_id
        ]


# -- Phase 1 hook: bridge from genome ctx to the platform staging service --
class _GenomeStagingHook:
    """Thin callable the supervisor pins on ``ctx.patch_staging``.

    The genome's evaluate_mutation node calls
    ``ctx.patch_staging.evaluate(patch=..., delta=..., failure_class=...,
    recurrence=...)`` and treats the returned :class:`StagingVerdict` as
    authoritative. Absence of the hook keeps the Phase-0 fallback path
    (in-DNA harness) — the supervisor only attaches it once the
    Phase-1 platform is wired."""

    def __init__(self, *, runner: "EvalRunner", darwin_id: str) -> None:
        self._runner = runner
        self._darwin_id = darwin_id

    def evaluate(
        self,
        *,
        patch: Any,
        delta: Any,
        failure_class: str,
        recurrence: int,
    ) -> "StagingVerdict":
        return self._runner.evaluate(
            EvalRequest(
                darwin_id=self._darwin_id,
                patch=patch,
                behavior_delta=delta,
                failure_class=str(failure_class),
                recurrence=int(recurrence),
            )
        )


# -- Phase 1 hook: bridge from genome ctx to the platform soma merger ------
class _GenomeMergeHook:
    """Thin callable the supervisor pins on ``ctx.soma_merge``.

    The genome's apply_mutation node calls
    ``ctx.soma_merge.merge(patch=..., failure_class=...)`` after a promote
    verdict and treats the returned :class:`SomaMergeResult` as
    authoritative — the fix only counts as landed when ``merged`` is True.
    Absence of the hook keeps the Phase-0 simulation (clear the recurrence
    counter without writing code); the supervisor only attaches it when a
    soma root is configured."""

    def __init__(self, *, merger: "SomaMerger", darwin_id: str) -> None:
        self._merger = merger
        self._darwin_id = darwin_id

    def merge(self, *, patch: Any, failure_class: str = "") -> Any:
        return self._merger.merge(patch, failure_class=str(failure_class))


# -- per-Darwin handle -----------------------------------------------------
@dataclass
class _Handle:
    """Internal: every running Darwin's bookkeeping. Not exposed."""

    darwin_id: str
    stack: FakeStack
    runtime: DarwinRuntime
    spawned_at: float
    parent_id: str | None = None
    # The user_subject (owner) who initiated this Darwin's spawn. Used to
    # attribute omnibus operations (fund_child, sweep_child, withdrawals)
    # back to the right user_credit balance. Inherited parent→child on
    # reproduction since a Darwin can only be born from another Darwin
    # owned by the same user.
    owner_subject: str = "anonymous"
    children: list[str] = field(default_factory=list)
    children_observed: int = 0
    reproduce_requested: bool = False
    killed: bool = False
    task: asyncio.Task | None = None
    # Tickets we have already routed to the genome's inbox so we don't
    # double-respond. Indexed by ticket id.
    handled_responses: set[str] = field(default_factory=set)
    # Cursor into stack.escalation.tickets; new posts past this index get
    # registered with the platform router on the next heartbeat boundary.
    escalation_observed: int = 0
    # Cycle counter so the monitor doesn't run every single tick — most
    # signals (spend velocity over an hour, failure rate, escalation rate)
    # do not need sub-second resolution.
    drive_cycles: int = 0
    auto_paused_for: str | None = None  # alert kind, if we auto-paused
    auto_throttled: set[str] = field(default_factory=set)  # alert kinds we
    # already auto-throttled — idempotent per (darwin, kind), cleared on
    # an explicit clear_throttle or resume.
    quarantine: QuarantineState = field(
        default_factory=lambda: QuarantineState(active=False)
    )
    # Owner runtime controls (doc 09). Persistent flags bridged into the
    # genome's OrchestratorState at every heartbeat boundary by ``_drive``,
    # so a control set mid-life takes effect on the very next tick.
    reproduction_blocked: bool = False
    disabled_neurons: set[str] = field(default_factory=set)
    disabled_categories: set[str] = field(default_factory=set)
    # Set when a forced reproduction with amendment is in flight. After the
    # next child of this Darwin is observed, the supervisor kills the
    # parent — doc 02's "the parent dies; the child carries the amendment".
    kill_after_reproduction_reason: str | None = None
    # Cursor into wallet.movements for billing's metered sweep — index of
    # the next ledger entry to attribute. Idempotent: a re-sweep starting
    # from here only sees new entries.
    billing_observed: int = 0
    # Phase 2 maintenance (doc 22): wall-clock of the last per-Darwin
    # drift profiling run (0.0 = baseline not yet established); cursor
    # into this Darwin's own audit slice so each drift profile sees one
    # interval's events, not the cumulative log; a per-Darwin run counter
    # used as the DriftDetector period label.
    last_drift_maintenance_ts: float = 0.0
    drift_audit_cursor: int = 0
    maintenance_runs: int = 0
    # Owner-set escalation policy (doc 09). Persisted on the lifecycle row at
    # spawn so a supervisor restart re-applies it, and held here so the
    # auto-decide hook + child-boot inheritance don't have to round-trip
    # through SQLite every tick.
    escalation_auto: str | None = "approve"


# -- the supervisor --------------------------------------------------------
class Supervisor:
    """Lifecycle owner for a fleet of in-process Darwins.

    Construct one per process. Methods are coroutines because each operation
    may touch the runtime that the per-Darwin task also touches; running on
    the same event loop keeps everything serialized without explicit locks.
    """

    def __init__(
        self,
        *,
        heartbeat_seconds: float = 0.001,
        clock: Callable[[], float] = time.time,
        notification_sink: NotificationSink | None = None,
        alert_sink: AlertSink | None = None,
        quotas: Quotas | None = None,
        auto_pause_on_critical: bool = False,
        auto_throttle_on_warn: bool = False,
        monitor_every_n_ticks: int = 5,
        billing_policy: BillingPolicy | None = None,
        llm_transport_factory: Callable[..., Any] | None = None,
        wallet_factory: Callable[..., Any] | None = None,
        vault_factory: Callable[[], Any] | None = None,
        browser_factory: Callable[[Any], Any] | None = None,
        x402_factory: Callable[[Any], Any] | None = None,
        sandbox_factory: Callable[[Any], Any] | None = None,
        stagehand_factory: Callable[[Any], Any] | None = None,
        episodic_memory_factory: Callable[[str], Any] | None = None,
        provisioning_backend: ProvisioningBackend | None = None,
        provisioning_quotas: ProvisioningQuotas | None = None,
        vault_sensitive_ttl_seconds: float = DEFAULT_SENSITIVE_TTL_SECONDS,
        vault_sweep_every_n_ticks: int = 60,
        vault_watermark_key: bytes | None = None,
        vault_watermarker: Watermarker | None = None,
        sanctions_screener: SanctionsScreener | None = None,
        browser_cache_enabled: bool = True,
        browser_cache_service: BrowserCacheService | None = None,
        cross_pollination_archive: CrossPollinationArchive | None = None,
        cross_pollination_per_scope_limit: int = 3,
        fiat_ramp: FiatRamp | None = None,
        freelancer_registry: Any | None = None,
        push_subscription_store: Any | None = None,
        push_vapid_public_key: str | None = None,
        sms_client: Any | None = None,
        resource_throttle_enabled: bool = True,
        persistence_path: str | Path | None = None,
        leader_election: Any | None = None,
        k8s_driver: Any | None = None,
        signer: PlatformSigner | None = None,
        patch_staging_sandbox_factory: (
            Callable[[], StagingSandbox] | None
        ) = None,
        patch_staging_quota: StagingQuota | None = None,
        patch_staging_timeout_s: float = 30.0,
        soma_root: str | Path | None = None,
        publishing_endpoints: dict[str, str] | None = None,
        analytics_endpoint: str | None = None,
        kyc_service: KYCService | None = None,
        omnibus_wallet: OmnibusWallet | None = None,
        namespace_provisioner: NamespaceProvisioner | None = None,
        egress_revoker: Any | None = None,
        dead_darwin_retention_seconds: float = 90.0 * 86400.0,
        content_classifier: Any | None = None,
        path_c_enabled: bool = False,
        path_c_require_human_review: bool = True,
        drift_detector: DriftDetector | None = None,
        merkle_anchor_publisher: MerkleAnchorPublisher | None = None,
        maintenance_interval_seconds: float = 86400.0,
        llm_key_minter: Any | None = None,
        llm_spend_reconciler: Any | None = None,
        llm_virtual_key_options: dict[str, Any] | None = None,
    ) -> None:
        # Loop pacing: between heartbeats the supervisor's drive task awaits
        # for this long. Default tiny — tests run as fast as the event loop;
        # the API runner can override.
        self._heartbeat = float(heartbeat_seconds)
        self._clock = clock
        self._darwins: dict[str, _Handle] = {}
        self._closed = False
        # Doc-08 production driver: when set, the supervisor delegates
        # per-Darwin lifecycle to K8s (one namespace + orchestrator pod
        # per Darwin) instead of running them as asyncio.Tasks. Status
        # and lifecycle reach the orchestrators over HTTP RPC; the
        # supervisor's in-process services (monitor, harvest scheduler,
        # billing sweep) are skipped in this mode — those move into the
        # orchestrator pod or are reconciled from Postgres in a
        # follow-on slice.
        self._k8s_driver = k8s_driver
        self._k8s_handles: dict[str, dict] = {}  # darwin_id → bookkeeping
        # Egress kill-switch hook (docs 08/09/24). When set, kill + quarantine
        # POST /revoke to the platform egress proxy so a Darwin's outbound
        # traffic is cut at the choke-point in parallel with the in-process
        # kill flag — the load-bearing mitigation if the orchestrator stops
        # polling. Best-effort; never blocks the kill.
        self._egress_revoker = egress_revoker
        # Path-C neuron self-authoring (doc 04). OFF by default fleet-wide;
        # when enabled, spawned + reproduced Darwins carry the admission gate
        # and (unless the operator opts out) require operator review before a
        # learned neuron joins a live catalog. Threaded into boot() as hp.
        self._darwin_hp = DEFAULT_HYPERPARAMETERS.model_copy(update={
            "path_c_enabled": bool(path_c_enabled),
            "path_c_require_human_review": bool(path_c_require_human_review),
        })
        # Persistent storage (doc 08: the audit log is "permanent"). When
        # ``persistence_path`` is set, swap the in-memory collections for
        # SQLite-backed ones; restart-survivable. Default unset keeps the
        # in-memory variants for tests + ephemeral dev. Built FIRST so the
        # collections below can branch on whether persistence is configured.
        self._persistence = None
        if persistence_path is not None:
            from .persistence import PersistenceStore  # sibling in runtime/

            self._persistence = PersistenceStore(persistence_path, clock=clock)
            # Apply the configured billing policy to the persistent service
            # (PersistenceStore's default is the 20%/10% policy).
            if billing_policy is not None:
                self._persistence._billing._policy = billing_policy  # noqa: SLF001
        # Lineage registry — in-memory variant by default; SQLite-backed
        # when persistence is configured (restores edges across restart so
        # killed Darwins still have a queryable lineage view).
        if self._persistence is not None:
            self._lineage_registry = self._persistence.lineage_registry()
        else:
            self._lineage_registry = LineageRegistry()
        # Per-Darwin lifecycle store (doc 08 HA: runtime resume after
        # leader failover). In-memory variant has no effect; the
        # persistent variant is what makes resume meaningful.
        self._lifecycle = (
            self._persistence.lifecycle_store()
            if self._persistence is not None else None
        )
        # Per-Darwin manifest + platform-wide checkpoint stores. When
        # persistence is configured, the supervisor uses these so a
        # resumed Darwin re-mints the same identity (manifest) and
        # picks up at the last saved heartbeat (checkpoint). Without
        # persistence, each spawn is genesis-fresh and the in-memory
        # MemoryCheckpointStore lives only for the process.
        self._manifest_store = (
            self._persistence.manifest_store()
            if self._persistence is not None else None
        )
        self._checkpoint_store = (
            self._persistence.checkpoint_store()
            if self._persistence is not None else None
        )
        # Per-Darwin pre-approval rule store (doc 17). When persistence
        # is configured, set_preapproval / revoke_preapproval write
        # through and a restart re-loads the rules onto the wallet on
        # spawn. Without persistence, rules live only in memory.
        self._preapproval_store = (
            self._persistence.preapproval_store()
            if self._persistence is not None else None
        )
        # The platform-side escalation router (doc 08). Tickets the genome
        # posts to its inbox get observed at the heartbeat boundary, routed
        # through this object, and notified via the (optional) sink.
        if self._persistence is not None:
            self._router = self._persistence.escalation_router(
                sink=notification_sink, clock=clock,
            )
        else:
            self._router = EscalationRouter(
                sink=notification_sink, clock=clock,
            )
        # The platform-side anomaly monitor (doc 08/09). Polls each Darwin
        # every N drive cycles, runs the detectors, dispatches alerts via the
        # (optional) sink. ``auto_pause_on_critical`` is opt-in: a tight
        # quota could otherwise auto-pause the fleet on a misconfiguration.
        # Persistent variant restores the alert history on restart.
        # Kept so fleet-maintenance can raise CRITICAL alerts (omnibus
        # reconciliation discrepancy / Merkle anchor failure are P0 per
        # docs 05/29) through the same sink the detectors use → PagerDuty.
        self._alert_sink = alert_sink
        # Darwins we've already paged about dangling payment intents. A fresh
        # supervisor (restart) starts empty, so the first maintenance pass
        # after restart surfaces any intent left open by a crash mid-payment;
        # we don't re-page every cycle while it stays open.
        self._open_intent_alerted: set[str] = set()
        if self._persistence is not None:
            self._monitor = self._persistence.monitor(
                quotas=quotas, sink=alert_sink, clock=clock,
            )
        else:
            self._monitor = Monitor(
                quotas=quotas, sink=alert_sink, clock=clock,
            )
        self._auto_pause_on_critical = bool(auto_pause_on_critical)
        self._monitor_every_n = max(1, int(monitor_every_n_ticks))
        # Doc 09 level 2: rate-limit the offending resource on a sustained
        # WARN. Off by default so a misconfigured detector cannot throttle
        # the fleet. When on, a WARN spend_velocity halves the reasoning
        # tier cap; a WARN failure_rate halves the coding tier cap.
        self._auto_throttle_on_warn = bool(auto_throttle_on_warn)
        # Single platform-side signing identity (doc 02, doc 18 §"Signing and
        # release"). One HSM key in reality; one SigningService here, shared
        # across every Darwin spawned by this Supervisor instance so all
        # manifests verify against the same public key. The signing private
        # key never leaves this object — the genome only ever sees PEM.
        #
        # When persistence is configured, the signing key is loaded from
        # (or seeded into) the platform_signing_key row so manifests
        # signed before a process restart still verify after the restart.
        # This is the platform's HSM stand-in for HA failover.
        #
        # Phase 1: when a Phase-1 HSM-backed ``signer`` is injected, it
        # takes precedence — the HSM owns the key, so the persistence
        # row is left untouched. The HA-resume path still works because
        # the HSM's public key is the trust root.
        if signer is not None:
            self._signer = signer
        elif self._persistence is not None:
            existing_key = self._persistence.load_signing_key()
            if existing_key is not None:
                self._signer = SigningService.from_private_bytes(existing_key)
            else:
                self._signer = SigningService()
                self._persistence.save_signing_key(self._signer.private_bytes())
        else:
            self._signer = SigningService()
        # Phase 1 patch-staging pipeline (doc 21). The supervisor calls
        # ``stage_and_evaluate`` between the genome's propose_mutation and
        # apply_mutation nodes. When no sandbox factory is configured the
        # service falls back to an in-process eval (same as Phase 0
        # behaviour), so the wiring is non-breaking. The eval runner is a
        # thin wrapper that resolves the per-Darwin audit sink before
        # handing the request off to staging.
        self._patch_staging = PatchStagingService(
            sandbox_factory=patch_staging_sandbox_factory,
            clock=clock,
            quota=patch_staging_quota,
            evaluation_timeout_s=patch_staging_timeout_s,
        )
        self._eval_runner = EvalRunner(
            staging=self._patch_staging,
            audit_sink_resolver=self._resolve_audit_sink_for,
        )
        # Phase 1 soma merge (docs 03/21). When a soma root is configured,
        # each Darwin gets its OWN merger rooted at ``<soma_root>/<id>`` (the
        # soma is the organism's mutable code — never shared across tenants),
        # installed on the genome's ctx so apply_mutation writes a promoted
        # patch's real diff to disk. Unset → no hook → Phase-0 simulation.
        self._soma_root = Path(soma_root) if soma_root is not None else None
        self._soma_mergers: dict[str, SomaMerger] = {}
        # Phase 1 §"Real economic activity": real publishing destinations
        # are configured fleet-wide; the supervisor reaches into the
        # catalog post-boot and wires them onto the content.publisher
        # neuron so the strategy doesn't have to know about platform-side
        # URL configuration. Same pattern for analytics.
        self._publishing_endpoints: dict[str, str] = dict(
            publishing_endpoints or {}
        )
        self._analytics_endpoint: str | None = analytics_endpoint
        # Phase 2 services. All optional: single-tenant dev keeps
        # working with every one of these as None. When wired, the
        # supervisor binds its audit recorder onto the ones that
        # accept one so each call shows up on the operator audit log.
        self._kyc_service: KYCService | None = kyc_service
        self._omnibus_wallet: OmnibusWallet | None = omnibus_wallet
        # Persist omnibus state (user credits + lifetime totals + ledger)
        # so a restart doesn't reset every owner's credit to zero. Attach
        # the persistent store when both are configured; it rehydrates the
        # wallet from the DB and write-through on every subsequent movement.
        if self._omnibus_wallet is not None and self._persistence is not None:
            attach = getattr(self._omnibus_wallet, "attach_store", None)
            if callable(attach):
                attach(self._persistence.omnibus_store())
        self._namespace_provisioner: NamespaceProvisioner | None = (
            namespace_provisioner
        )
        # doc 09 #3: a dead Darwin's namespace + organism DB are retained this
        # long before the fleet-maintenance reaper destroys them.
        self._dead_darwin_retention_seconds = float(
            dead_darwin_retention_seconds
        )
        # Platform-side outbound content-safety classifier (doc 10), injected
        # into each spawned/reproduced Darwin's platform so publishing neurons
        # run a real filter rather than only the deterministic backstop.
        self._content_classifier = content_classifier
        self._drift_detector: DriftDetector | None = drift_detector
        self._merkle_anchor: MerkleAnchorPublisher | None = (
            merkle_anchor_publisher
        )
        # Drive-loop maintenance cadence (doc 22). Drift profiling is
        # per-Darwin; Merkle anchoring + omnibus reconciliation are
        # fleet-level. Both fire on a wall-clock interval so the cadence
        # is decoupled from heartbeat speed — a soak can set it low, a
        # production deploy leaves it daily.
        # Floor at 1ms so a zero/negative value can't busy-spin the
        # maintenance work every cycle; sub-second values are otherwise
        # honoured (a soak sets it low, production leaves it daily).
        self._maintenance_interval = max(
            0.001, float(maintenance_interval_seconds)
        )
        self._last_fleet_maintenance_ts = self._clock()
        # Doc 08 §"Billing model". Revenue accrues from each metered wallet
        # debit (markup pass-through) and each harvest (fee on gross). The
        # genome doesn't know about billing — the supervisor sweeps the
        # wallet ledger at the heartbeat boundary to attribute revenue.
        if self._persistence is not None:
            self._billing = self._persistence.billing_service
        else:
            self._billing = BillingService(billing_policy, clock=clock)
        # Optional real-LLM transport factory (doc 13). Signature:
        # ``factory(wallet, clock) -> LLMTransport``. When set, each spawned
        # Darwin gets a real adapter instead of the deterministic FakeLLM.
        # Default unset keeps the 90-day sim's reproducibility.
        self._llm_factory: Callable[..., Any] | None = llm_transport_factory
        # Per-Darwin LiteLLM virtual keys (doc 13). When a minter is wired, a
        # fresh virtual key is generated at spawn (scoped to the Darwin's id,
        # budget, and tiers) instead of every Darwin sharing the platform
        # master key — the docs-vs-impl audit's CRITICAL "shared master key"
        # finding. The key is sealed in the Darwin's vault and revoked on
        # death. The reconciler (optional) cross-checks the proxy's per-key
        # spend table against each Darwin's in-process LLM debits every
        # maintenance cycle. ``_llm_virtual_keys`` maps darwin_id -> key for
        # the reconciler + the kill-time revoke.
        self._llm_key_minter: Any | None = llm_key_minter
        self._llm_spend_reconciler: Any | None = llm_spend_reconciler
        self._llm_virtual_key_options: dict[str, Any] = dict(
            llm_virtual_key_options or {}
        )
        self._llm_virtual_keys: dict[str, str] = {}
        # Vault path under which a Darwin's virtual key is sealed (doc 13:
        # "stored in the Darwin's vault"). One well-known key so a restart can
        # recover it for inspection without re-minting.
        self._llm_virtual_key_vault_path = "litellm/virtual_key"
        # Optional real-provider factories (docs 14–17). All are None by
        # default so the supervisor keeps using the in-memory fakes, which is
        # correct for tests and the 90-day sim. Set them via the CLI flags
        # (--rpc-url, --vault-url, --browserbase-url) or by passing factory
        # callables directly for programmatic wiring.
        #
        # wallet_factory(darwin_id, clock, vault) -> WalletPort  (doc 17:
        # RealWallet+Web3ChainClient). darwin_id lets per-Darwin wallet
        # providers construct an isolated wallet for each spawn; vault is the
        # already-installed per-Darwin vault so a local-vault provider can
        # seal/retrieve the keypair without a second backend handle.
        # vault_factory()       -> RawVaultPort (doc 16: RealVaultBackend+HvacVaultClient)
        # browser_factory(wallet) -> BrowserClient (doc 15: BrowserUseClient)
        # x402_factory(wallet)    -> X402PaymentClient (doc 17: x402 HTTP wrapper)
        # sandbox_factory(wallet) -> SandboxClient (doc 14: E2BSandbox)
        # stagehand_factory(wallet) -> StagehandReplayClient (doc 15: cached
        #   replay path; nests inside CachedBrowserSubstrate as the cheap
        #   half. None keeps the default modeled-cost replay.)
        self._wallet_factory: Callable[..., Any] | None = wallet_factory
        self._vault_factory: Callable[[], Any] | None = vault_factory
        self._browser_factory: Callable[[Any], Any] | None = browser_factory
        self._x402_factory: Callable[[Any], Any] | None = x402_factory
        self._sandbox_factory: Callable[[Any], Any] | None = sandbox_factory
        self._stagehand_factory: Callable[[Any], Any] | None = stagehand_factory
        # episodic_memory_factory(darwin_id) -> EpisodicMemory-compatible
        #   substrate (doc 03). When set, the supervisor installs the result on
        #   the per-Darwin Platform (``stack.episodic_memory``) before boot, so
        #   the genome's DarwinRuntime picks it up in place of the deterministic
        #   default. The only real implementation today is
        #   ``LanceDbEpisodicMemory``; with this None the genome stays
        #   deterministic (sim + cross-engine parity unchanged).
        self._episodic_memory_factory: Callable[[str], Any] | None = (
            episodic_memory_factory
        )
        # Platform-mediated provisioning (doc 04, 07). Replaces the fake
        # in-memory provisioner with a service that records per-Darwin
        # resources, enforces quotas, and lets operators deprovision via
        # the HTTP API. Default backend = InMemoryProvisioningBackend so
        # tests + the 90-day sim keep their deterministic behaviour.
        self._provisioning = ProvisioningService(
            backend=provisioning_backend,
            quotas=provisioning_quotas,
            clock=clock,
        )
        # Owner-write vault admin (doc 16 §"Bidirectional shared vault").
        # The resolver walks the handle map at query time so the admin
        # service stays one-instance-per-supervisor while still pointing at
        # the right per-Darwin namespace.
        if vault_watermarker is not None:
            watermarker: Watermarker = vault_watermarker
        elif vault_watermark_key is not None:
            watermarker = HmacWatermarker(signing_key=vault_watermark_key)
        else:
            # Default no-op preserves backwards compatibility — sensitive
            # uploads still get the audit sidecar (sha256 + uploader),
            # just no embedded HMAC. Production should set
            # vault_watermark_key for forensic traceability of leaks.
            watermarker = NoOpWatermarker()
        self._vault_admin = VaultAdminService(
            vault_resolver=self._resolve_vault_for,
            retention=VaultRetentionService(
                clock=clock,
                sensitive_ttl_seconds=vault_sensitive_ttl_seconds,
            ),
            clock=clock,
            watermarker=watermarker,
        )
        # Sweep cadence: every Nth drive cycle the supervisor purges any
        # sensitive vault entries whose TTL has run out (default 90 days
        # from last use; doc 16). Default 60 ticks ≈ one minute at the
        # default heartbeat; tests can drop this to 1 to exercise immediately.
        self._vault_sweep_every_n = max(1, int(vault_sweep_every_n_ticks))
        # Sanctions screener (doc 17). When set, every wallet the
        # supervisor spawns gets the screener installed so every outbound
        # payment is OFAC-checked before chain submit. Default None keeps
        # the 90-day sim's deterministic recipients passing untouched.
        self._sanctions: SanctionsScreener | None = sanctions_screener
        # Browser cache (doc 15). One service per supervisor; every
        # Darwin's browser substrate is wrapped at spawn time so the
        # successful traces accumulate fleet-wide. Disable for tests that
        # poke FakeBrowser directly.
        self._browser_cache_enabled = bool(browser_cache_enabled)
        self._browser_cache = (
            browser_cache_service
            if browser_cache_service is not None
            else BrowserCacheService(clock=clock)
        )
        # Recurring harvest rules (doc 05). The scheduler holds rules; the
        # drive cycle evaluates them per Darwin and fires harvest() when
        # one is due. Default cadence: every N drive cycles. Set to 1 in
        # tests for immediate evaluation; in production, every ~60 cycles
        # is cheap and matches doc-05's "schedule Y".
        self._harvest_scheduler = HarvestScheduler(clock=clock)
        # Persist harvest rules so owner-registered sweeps survive a
        # restart (was in-memory only — silently dropped on failover).
        if self._persistence is not None:
            self._harvest_scheduler.attach_store(
                self._persistence.harvest_store()
            )
        self._harvest_eval_every_n = 10
        # Cross-pollination archive (doc 06 phase 3). When set, the
        # supervisor sweeps each Darwin's distilled lessons after a
        # reproduction event + admits public-safe ones into the archive;
        # children boot with a vetted subset of sibling lessons.
        # Default None preserves the previous "lessons stay private to
        # the lineage" behaviour.
        self._cross_pollination: CrossPollinationArchive | None = (
            cross_pollination_archive
        )
        self._cross_pollination_per_scope_limit = max(
            1, int(cross_pollination_per_scope_limit)
        )
        # Fiat ramp (doc 17). When configured, owners can deposit USD
        # against a Darwin's wallet and withdraw the harvested USDC
        # via the same provider. Default None means the platform is
        # crypto-only — owners pre-fund Darwins manually.
        self._fiat_ramp: FiatRamp | None = fiat_ramp
        # Persist outstanding deposit quotes so a webhook confirm that lands
        # after a restart still resolves its quote (otherwise the deposit is
        # silently lost), and a replay stays idempotent. Only ramps that
        # opt in via attach_store are wired (StripeFiatRamp does).
        if self._fiat_ramp is not None and self._persistence is not None:
            _attach = getattr(self._fiat_ramp, "attach_store", None)
            if callable(_attach):
                _attach(self._persistence.fiat_quote_store())
        # Durable webhook idempotency ledger (doc 17). Lets a re-delivered
        # payment webhook short-circuit before any money effect, surviving
        # restarts. None when persistence is off (in-memory dev/sim) — then
        # idempotency falls back to quote-consumption alone.
        self._webhook_events = (
            self._persistence.webhook_event_store()
            if self._persistence is not None else None
        )
        # Owner-scoped registry of verified fiat-payout destinations
        # (doc 17). Defaults to the persistent variant when persistence
        # is wired; an explicit kwarg overrides. None means the platform
        # rejects every darwin_fiat_payout — operators must wire one.
        if freelancer_registry is None and self._persistence is not None:
            self._freelancer_registry: Any | None = (
                self._persistence.freelancer_registry()
            )
        else:
            self._freelancer_registry = freelancer_registry
        # Web Push subscriptions for the PWA escalation companion (doc 22).
        # Bound here so register / revoke can write through; the actual
        # delivery happens via the notification sink wired into the router.
        if push_subscription_store is None and self._persistence is not None:
            self._push_subscription_store: Any | None = (
                self._persistence.push_subscription_store()
            )
        else:
            self._push_subscription_store = push_subscription_store
        # VAPID public key the PWA reads at sign-up time to call
        # ``pushManager.subscribe({ applicationServerKey: <this> })``.
        # The private half lives only inside the notification sink.
        self._push_vapid_public_key = push_vapid_public_key
        # If the notification sink is a WebPushNotificationSink and we have
        # a persistent push subscription store, hydrate the sink from disk
        # (so registrations survive a restart) and install the purge
        # callback (so a 410 deletes the row, not just the in-memory copy).
        self._wire_push_persistence(notification_sink)
        # Twilio SMS inbox for inbound 2FA codes (doc 15). None = the
        # platform has no shared phone number; accounts.manager escalates
        # every code to the owner instead of the platform auto-answering.
        self._sms_client: Any | None = sms_client
        # Non-LLM resource throttles (doc 09 level 2). The LLM tier
        # throttle lives on the genome's LLMRouter; sandbox / browser /
        # egress live here because their substrates are platform-side.
        self._throttle_service = ThrottleService(clock=clock)
        # Persist installed throttles so operator-set resource caps survive
        # a restart (was in-memory only — a restart silently lifted caps).
        if self._persistence is not None:
            self._throttle_service.attach_store(
                self._persistence.throttle_store()
            )
        # When disabled, the throttle wrappers are not installed at
        # spawn time — useful for tests that assert identity equality
        # against the raw substrate they injected.
        self._resource_throttle_enabled = bool(resource_throttle_enabled)
        # Hash-chained operator audit log (doc 08). Every mutating
        # supervisor call writes an entry tagged with the calling actor's
        # subject so a compliance reviewer can replay who-did-what.
        if self._persistence is not None:
            self._audit = self._persistence.audit_log
        else:
            self._audit = OperatorAuditLog(clock=clock)
        # HA leader election (doc 08; Postgres-port follow-on). Default
        # NoLeaderElection always reports leader — right for single-replica
        # deploys. When multiple replicas share Postgres, pass a
        # PostgresAdvisoryLockElection so exactly one instance drives.
        from .leadership import LeaderElection, NoLeaderElection

        if leader_election is None:
            self._leader: LeaderElection = NoLeaderElection()
        else:
            self._leader = leader_election
        self._leader_started = False
        self._leader_watch_task: asyncio.Task | None = None
        # K8s mode has no per-Darwin in-process drive loop to hang fleet
        # maintenance off, so a dedicated leader-gated loop drives the
        # LiteLLM spend reconciliation for pod-hosted Darwins (doc 13).
        self._k8s_maintenance_task: asyncio.Task | None = None
        # Track which handles have an active drive task so we can
        # cancel/restart en masse on leadership transitions.
        self._observed_leader_state: bool = self._leader.is_leader

    # -- leader election (doc 08 HA, Postgres-port follow-on) -------------
    @property
    def is_leader(self) -> bool:
        """True when this Supervisor instance is the current drive-loop
        leader. Followers serve only ``/healthz`` + ``/metrics`` + reads
        of persistent state; the k8s Service ``readinessProbe`` (set to
        ``/readyz``) should keep mutating traffic off them."""
        return self._leader.is_leader

    async def ensure_leader_started(self) -> None:
        """Start the leader election + transition watch task. Idempotent;
        safe to call from any path that depends on leader status. The
        FastAPI lifespan handler calls this on startup."""
        if self._leader_started:
            return
        self._leader_started = True
        await self._leader.start()
        self._observed_leader_state = self._leader.is_leader
        # Resume any alive Darwins persisted from a previous incarnation
        # of this supervisor (doc 08 HA). Best-effort: a Darwin that
        # cannot be reconstructed (e.g. missing manifest) is skipped, not
        # crashed-on. Runs only when we hold leadership AND have a
        # lifecycle store wired.
        if self._leader.is_leader and self._lifecycle is not None:
            try:
                await self._resume_from_persistence()
            except Exception:
                # A bad row must not block the startup path; the audit
                # log captures the failure for ops review.
                pass
        # K8s mode: re-adopt per-Darwin namespaces still running from a
        # previous incarnation. Without this _k8s_handles starts empty after
        # a restart, so every prior Darwin 404s while its orchestrator pod
        # keeps running (and spending). Best-effort, leader-only.
        if self._leader.is_leader and self._k8s_driver is not None:
            try:
                await self._reconcile_k8s()
            except Exception:
                pass
        # Background reconciler: when leadership flips, cancel any
        # running drive tasks (we lost the lock and shouldn't keep
        # writing). On gain, we don't auto-resume previously-spawned
        # Darwins — the operator re-spawns via the API once readiness
        # routes traffic back to this pod.
        loop = asyncio.get_event_loop()
        self._leader_watch_task = loop.create_task(self._watch_leadership())
        # K8s fleet maintenance loop (docs 13/29/05): LLM-spend reconciliation,
        # Merkle anchoring, and omnibus reconciliation on the maintenance
        # interval. Only started in K8s mode with at least one of those
        # concerns wired — the in-process path drives them from each Darwin's
        # own _drive loop instead.
        if (
            self._k8s_driver is not None
            and self._k8s_maintenance_task is None
            and (
                self._llm_spend_reconciler is not None
                or self._merkle_anchor is not None
                or self._omnibus_wallet is not None
            )
        ):
            self._k8s_maintenance_task = loop.create_task(
                self._k8s_maintenance_loop()
            )

    async def _watch_leadership(self) -> None:
        # Tick frequency: the election impl already does its own
        # heartbeat (e.g. 5s for Postgres advisory lock); we just need
        # to react to the boolean changing. 0.5s polling is cheap.
        while not self._closed:
            try:
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            if self._closed:
                break
            current = self._leader.is_leader
            if current == self._observed_leader_state:
                continue
            self._observed_leader_state = current
            if not current:
                # Lost leadership: cancel every running drive task so we
                # stop writing to the persistent store. The new leader
                # picks up after the operator re-spawns.
                for h in list(self._darwins.values()):
                    if h.task is not None and not h.task.done():
                        h.task.cancel()

    async def _k8s_maintenance_loop(self) -> None:
        """Drive fleet maintenance for K8s-hosted Darwins on the maintenance
        interval (leader-only). K8s mode has no per-Darwin in-process drive
        loop, so this is where the fleet-level concerns the in-process path
        runs from ``_run_fleet_maintenance`` actually fire: LLM-spend
        reconciliation (doc 13), the daily Merkle anchor (doc 29), and omnibus
        reconciliation (doc 05). Sleeps in short slices so shutdown cancels
        promptly even with a long interval."""
        interval = max(1.0, float(self._maintenance_interval))
        elapsed = 0.0
        while not self._closed:
            try:
                await asyncio.sleep(min(1.0, interval))
            except asyncio.CancelledError:
                break
            if self._closed:
                break
            elapsed += min(1.0, interval)
            if elapsed < interval:
                continue
            elapsed = 0.0
            if not self._leader.is_leader:
                continue
            try:
                await self._run_k8s_fleet_maintenance(self._clock())
            except Exception as exc:  # noqa: BLE001 - never kill the loop
                self._audit.record(
                    actor="platform.maintenance",
                    action="fleet_maintenance_failed",
                    metadata={"reason": str(exc)[:200], "driver": "k8s"},
                )

    async def _resume_from_persistence(self) -> int:
        """Re-spawn every persisted Darwin marked alive. Lossless when a
        manifest + audit chain were persisted for the id (the new Darwin
        keeps its original id + continues the same audit chain via
        :class:`PersistentDarwinAuditSink`); falls back to a fresh-id
        spawn when the manifest store is unavailable for that row.

        Returns the count of resumed Darwins."""
        assert self._lifecycle is not None
        resumed = 0
        for rec in self._lifecycle.list_alive():
            if rec.darwin_id in self._darwins:
                continue
            opening = (
                rec.last_balance if rec.last_balance is not None
                else rec.opening_usdc
            )
            opening = float(max(opening, 0.0))
            has_manifest = (
                self._manifest_store is not None
                and self._manifest_store.get(rec.darwin_id) is not None
            )
            try:
                status = await self.spawn(
                    opening_usdc=opening,
                    escalation_auto=rec.escalation_auto or "approve",
                    parent_id=rec.parent_id,
                    actor="platform.resume",
                    _resume_id=rec.darwin_id if has_manifest else None,
                )
                self._audit.record(
                    actor="platform.resume",
                    action="resume",
                    darwin_id=status.darwin_id,
                    metadata={
                        "from_id": rec.darwin_id,
                        "opening_usdc": opening,
                        "original_spawned_at": rec.spawned_at,
                        "lossless": bool(has_manifest),
                    },
                )
                # Restore a persisted quarantine (doc 23): a Darwin that was
                # at level-4 before the restart comes back quarantined AND
                # paused — it must not slip back to running unsupervised.
                if rec.quarantined:
                    h = self._darwins.get(status.darwin_id)
                    if h is not None:
                        h.quarantine = QuarantineState(
                            active=True,
                            reason=rec.quarantine_reason,
                            operator=rec.quarantine_operator,
                            applied_ts=rec.quarantine_applied_ts,
                        )
                        try:
                            h.stack.pause.pause(
                                f"quarantine:{rec.quarantine_reason}"
                            )
                        except Exception:
                            pass
                        self._audit.record(
                            actor="platform.resume",
                            action="quarantine_restored",
                            darwin_id=status.darwin_id,
                            metadata={
                                "reason": rec.quarantine_reason,
                                "operator": rec.quarantine_operator,
                            },
                        )
                resumed += 1
            except Exception as exc:
                self._audit.record(
                    actor="platform.resume",
                    action="resume_failed",
                    darwin_id=rec.darwin_id,
                    metadata={"reason": str(exc)},
                )
        return resumed

    def _require_leader(self) -> None:
        """Defense in depth — the readiness probe should already keep
        traffic off followers, but if a write reaches one we refuse
        cleanly rather than silently committing in-memory state that
        would be lost on the next leader switch."""
        if not self._leader.is_leader:
            raise SupervisorError(
                "supervisor is not the current leader; "
                "the request should have been routed to the leader pod"
            )

    # -- spawn (doc 08: POST /darwins) ------------------------------------
    async def spawn(
        self,
        *,
        opening_usdc: float = 100.0,
        escalation_auto: str | None = "approve",
        parent_id: str | None = None,
        actor: str = "anonymous",
        _resume_id: str | None = None,
    ) -> DarwinStatus:
        """Mint genesis, boot, start the drive task. Returns the new Darwin's
        status. ``parent_id`` is metadata only for genesis spawns — children
        born from a reproduction event are picked up automatically via the
        spawner observer.

        ``_resume_id`` is an internal-only handle used by
        :meth:`_resume_from_persistence` to re-spawn a Darwin against its
        persisted manifest + audit chain. Callers should not pass this
        directly; use the resume entry point."""
        if self._closed:
            raise SupervisorError("supervisor is closed")
        await self.ensure_leader_started()
        self._require_leader()
        # KYC gate (doc 05): we will not provide service to a user we cannot
        # KYC. Genesis spawns are gated on the requesting owner; resumes of an
        # already-living Darwin are not re-gated. No-op when KYC is unconfigured.
        if _resume_id is None:
            self._kyc_gate(actor, action="spawn")
        # K8s mode (doc-08 production): hand off to the driver, which
        # provisions a per-Darwin namespace + orchestrator pod under
        # ``runtimeClassName: kata``. The in-process path below is
        # skipped — no FakeStack, no asyncio.Task, no in-process
        # services. The supervisor still records the audit + lineage
        # entries; the orchestrator pod owns the rest of the lifecycle.
        if self._k8s_driver is not None:
            return await self._spawn_k8s(
                opening_usdc=opening_usdc,
                escalation_auto=escalation_auto,
                parent_id=parent_id,
                actor=actor,
            )
        with span(
            "supervisor.spawn",
            attributes={"opening_usdc": opening_usdc, "parent_id": parent_id},
        ) as s:
            # Stage 1: build the stack with the wallet but defer the LLM
            # so the real-adapter factory can wire it against the wallet
            # below. FakeStack constructs FakeWallet eagerly in __init__,
            # so building once and overwriting ``stack.llm`` afterwards is
            # the cleanest path (a real factory can read stack.wallet).
            stack = FakeStack(
                opening_usdc=opening_usdc,
                clock=self._clock,
                escalation_auto=escalation_auto,
                signer=self._signer,
            )
            # Lossless resume (doc 08 §HA): swap the throwaway genesis the
            # FakeStack just minted for the persisted manifest + layer
            # blobs, so the resumed Darwin keeps its original identity.
            # The audit chain + checkpoint will then continue the same
            # life rather than starting a fresh one.
            if _resume_id is not None and self._manifest_store is not None:
                stored = self._manifest_store.get(_resume_id)
                if stored is not None:
                    from darwin_dna.manifest import Manifest
                    manifest_json, layer_blobs, layer_names = stored
                    stack.manifest = Manifest.model_validate_json(manifest_json)
                    stack.layer_blobs = layer_blobs
                    stack.layer_names = layer_names
                    # boot() will reconstruct identity from the verified manifest.
                    stack.identity = None
            # Persistent audit + checkpoint plumbing (lossless resume).
            # The Darwin id at this point is either the freshly-minted
            # genesis id OR the persisted resume id (when manifest was
            # restored above). Either way we point the sink + checkpoint
            # store at it so subsequent records are durable.
            darwin_id_now = (
                _resume_id
                if _resume_id is not None and stack.identity is None
                else stack.identity.darwin_id
            )
            if self._persistence is not None:
                stack.audit_sink = self._persistence.audit_sink_for(darwin_id_now)
                stack.checkpoint = self._checkpoint_store
            # Tenant namespace (K8s + Vault + organism DB + optional wallet
            # keypair mint per chain — doc 22 §"Per-Darwin Kubernetes
            # namespaces"). Runs BEFORE the wallet/vault factories so a
            # provider that reads from the provisioned namespace finds it
            # ready. Best-effort: a failure surfaces as an audit entry and
            # raises so the caller can decide whether to retry, but the
            # FakeStack itself is not torn down here (the higher-level
            # spawn flow owns that on the failure path).
            if self._namespace_provisioner is not None:
                self._namespace_provisioner.provision(
                    darwin_id=darwin_id_now, actor=actor,
                )
            # Real vault backend: replace in-memory FakeVault with a
            # HashiCorp Vault-backed adapter (doc 16 RealVaultBackend +
            # HvacVaultClient). Per-Darwin namespace isolation lives in the
            # HvacVaultClient's path prefix; the factory owns that config.
            # Built BEFORE the wallet so a local-vault wallet provider seals
            # its keypair into the real vault, not the throwaway FakeVault.
            if self._vault_factory is not None:
                stack.vault = self._vault_factory()
            # Reproduction-transfer destination (step c): when the platform
            # has an OmnibusWallet wired, route the parent's surplus to its
            # on-chain address so the omnibus's books reflect what actually
            # moved. Without an omnibus the spawner stays on its symbolic
            # default (used by the 90-day sim + unit tests).
            if self._omnibus_wallet is not None and stack.spawner is not None:
                try:
                    stack.spawner._omnibus_destination = (
                        self._omnibus_wallet.address()
                    )
                except Exception:
                    # An omnibus that can't surface its address is a config
                    # bug, but it must not crash spawn — the fallback symbol
                    # keeps the reproduction transfer flowing.
                    pass
            # Real wallet: replace FakeWallet with a chain-backed adapter
            # (doc 17 RealWallet + Web3ChainClient). Called BEFORE browser
            # and x402 factories so they always receive the live wallet.
            if self._wallet_factory is not None:
                stack.wallet = self._wallet_factory(
                    darwin_id_now, self._clock, stack.vault,
                )
            # Real-LLM transport (doc 13). Wired AFTER the vault + wallet
            # factories so the transport bills the *live* wallet (not the
            # stage-1 FakeWallet) and a minted virtual key seals into the
            # *real* vault. When a key minter is configured, each Darwin gets
            # its own LiteLLM virtual key (scoped to its id + budget) instead
            # of sharing the platform master key — the docs-vs-impl audit's
            # CRITICAL shared-master-key finding.
            if self._llm_factory is not None:
                virtual_key = self._mint_llm_virtual_key(
                    darwin_id_now, stack.vault, actor,
                )
                stack.llm = self._invoke_llm_factory(
                    stack.wallet, darwin_id_now, stack.vault, virtual_key,
                )
            # Rehydrate persisted pre-approval rules (doc 17). A restart
            # would otherwise wipe the owner's "Upwork under $50" intent
            # even though the rule is still meant to be in effect.
            if (
                self._preapproval_store is not None
                and hasattr(stack.wallet, "set_preapproval")
            ):
                try:
                    persisted = self._preapproval_store.load(
                        darwin_id=darwin_id_now,
                    )
                except Exception:
                    persisted = []
                for rule in persisted:
                    try:
                        stack.wallet.set_preapproval(rule)
                    except Exception:
                        # A single malformed row should not block boot;
                        # the operator sees the row in the DB and can
                        # delete it explicitly.
                        continue
                # Wire the wallet's consume-counter bumps through to
                # persistence so a max_uses rule's countdown survives a
                # restart. Best-effort: a wallet without the persister
                # setter is a no-op.
                set_persister = getattr(
                    stack.wallet, "set_preapproval_persister", None,
                )
                if callable(set_persister):
                    store = self._preapproval_store
                    did = darwin_id_now

                    def _persist(rule, *, _store=store, _did=did):
                        try:
                            _store.save(darwin_id=_did, rule=rule)
                        except Exception:
                            pass

                    set_persister(_persist)
            # Real browser substrate: replace FakeBrowser with BrowserUseClient
            # on Browserbase (doc 15). Factory receives the active wallet so
            # session-billing debits go to the correct Darwin.
            if self._browser_factory is not None:
                stack.browser = self._browser_factory(stack.wallet)
            # x402 HTTP client: wire into Platform so outbound-capable neurons
            # get it via NeuronContext.http (doc 17). Factory receives the
            # active wallet so 402 payments settle against this Darwin.
            if self._x402_factory is not None:
                stack.x402_client = self._x402_factory(stack.wallet)
            # Sandbox: replace FakeSandbox with a real E2BSandbox (doc 14).
            # Reaches compute.run-tagged neurons via NeuronContext.sandbox.
            # Wallet is passed for per-session billing inside the adapter.
            if self._sandbox_factory is not None:
                stack.sandbox = self._sandbox_factory(stack.wallet)
            # Episodic memory substrate (doc 03). When configured, install the
            # per-Darwin vector store (LanceDB today) on the Platform before
            # boot so the genome's injection seam picks it up in place of the
            # deterministic in-process default. Per-Darwin so each life owns
            # its own table; the factory carries the per-Darwin key out.
            if self._episodic_memory_factory is not None:
                stack.episodic_memory = self._episodic_memory_factory(darwin_id_now)
            # Sanctions screener (doc 17). Installed on every wallet so
            # outbound payments are OFAC-checked at the wallet layer
            # before any chain submit. The wallet's `set_sanctions_screener`
            # is best-effort — a custom factory that doesn't implement it
            # silently keeps the default (no screening), preserving
            # backwards-compat for adapter stubs in tests.
            if self._sanctions is not None:
                setter = getattr(stack.wallet, "set_sanctions_screener", None)
                if callable(setter):
                    setter(self._sanctions)
            # Genome's _sleep set to no-op:
            # (browser cache wrapping happens AFTER boot so we know the
            # final darwin_id; deferred to the post-boot block below.) the supervisor schedules the gaps
            # between heartbeats itself with asyncio.sleep, so the event loop
            # never blocks on time.sleep even with many Darwins on one loop.
            _platform = stack.platform()
            # Inject the platform-side content-safety classifier (doc 10) so
            # publishing neurons run a real filter, not just the deterministic
            # substring backstop. No-op when unconfigured.
            if self._content_classifier is not None:
                _platform.content_classifier = self._content_classifier
            runtime = boot(
                _platform, sleep=lambda _: None, hp=self._darwin_hp,
            )
            # Wrap the browser substrate with cache/promotion (doc 15).
            # The wrapper proxies `run(steps) -> dict`, records every
            # session, promotes signatures with N successes to a cached
            # replay path, attributes cost per (darwin_id, neuron, URL).
            if self._browser_cache_enabled and stack.browser is not None:
                replay_client = (
                    self._stagehand_factory(stack.wallet)
                    if self._stagehand_factory is not None else None
                )
                wrapped = CachedBrowserSubstrate(
                    exploration=stack.browser,
                    cache=self._browser_cache,
                    darwin_id=runtime.identity.darwin_id,
                    clock=self._clock,
                    replay=replay_client,
                )
                stack.browser = wrapped
                runtime._p.browser = wrapped  # noqa: SLF001
            # Level-2 throttle wrappers (doc 09). Nested OUTSIDE the
            # cache so cached replays still count against the
            # operator-installed daily cap. Sandbox + egress get the
            # same treatment so a sustained WARN can actually rate-limit
            # the offending resource.
            if self._resource_throttle_enabled:
                if stack.browser is not None:
                    stack.browser = ThrottledBrowserSubstrate(
                        inner=stack.browser,
                        throttle=self._throttle_service,
                        darwin_id=runtime.identity.darwin_id,
                    )
                    runtime._p.browser = stack.browser  # noqa: SLF001
                if stack.sandbox is not None:
                    stack.sandbox = ThrottledSandboxSubstrate(
                        inner=stack.sandbox,
                        throttle=self._throttle_service,
                        darwin_id=runtime.identity.darwin_id,
                    )
                    runtime._p.sandbox = stack.sandbox  # noqa: SLF001
                if stack.x402_client is not None:
                    stack.x402_client = ThrottledEgressClient(
                        inner=stack.x402_client,
                        throttle=self._throttle_service,
                        darwin_id=runtime.identity.darwin_id,
                    )
                    runtime._p.x402_client = stack.x402_client  # noqa: SLF001
            # Swap the fake provisioner for the per-Darwin scoped view of the
            # platform-side ProvisioningService (doc 04, 07). Done AFTER boot
            # so the genome's catalog already type-checks against the
            # FakeProvisioner Protocol; we then rebind the ProvisionPort the
            # neuron sees to the supervisor-backed one for runtime calls.
            stack.provision = self._provisioning.scoped_for(
                runtime.identity.darwin_id
            )
            # The neuron's catalog was wired against the OLD provision port
            # at boot. Rebind the field so the next dispatch hits the
            # platform-side service. (The neuron stores its provision port
            # at construction; we go through the catalog to update it.)
            try:
                neuron = runtime.catalog.get("infrastructure.provisioner")
                if neuron is not None:
                    neuron._provision = stack.provision  # noqa: SLF001
            except Exception:
                # If the neuron isn't in this build, ignore — provisioning
                # still works for HTTP-only operator-driven flows.
                pass
            # Phase 1: wire real publishing + analytics endpoints onto the
            # neurons so the strategy doesn't carry per-channel URLs.
            self._wire_phase1_endpoints(runtime)
            # Phase 1: install the patch-staging hook on the genome's ctx
            # so evaluate_mutation routes through the platform service.
            # The genome's evaluate node calls ``ctx.patch_staging`` when
            # present; absence falls back to the in-DNA harness.
            self._install_patch_staging_hook(runtime)
            # Phase 1 soma merge: write promoted patches to the live soma.
            self._install_soma_merge_hook(runtime)
            handle = _Handle(
                darwin_id=runtime.identity.darwin_id,
                stack=stack,
                runtime=runtime,
                spawned_at=self._clock(),
                parent_id=parent_id,
                owner_subject=actor,
                escalation_auto=escalation_auto,
            )
            self._darwins[handle.darwin_id] = handle
            annotate_span_with_runtime(s, runtime)
            # Fund from the omnibus BEFORE starting the drive loop. The
            # genome's first reconcile seeds its opening from the live
            # wallet balance; if the loop starts first it races the
            # on-chain transfer, seeds 0, then halts on drift when the
            # funds land. Debit user_credits + chain-transfer for root
            # spawns; skipped silently for anonymous/in-process flows where
            # the FakeWallet's opening balance is the in-memory proxy.
            if parent_id is None:
                funded = self._fund_darwin_from_omnibus(handle, opening_usdc)
                if funded > 0:
                    await self._await_funded_balance(handle, funded)
            handle.task = asyncio.create_task(
                self._drive(handle), name=f"darwin:{handle.darwin_id}"
            )
            handle.task.add_done_callback(self._log_drive_done)
            self._audit.record(
                actor=actor,
                action="spawn",
                darwin_id=handle.darwin_id,
                metadata={
                    "opening_usdc": float(opening_usdc),
                    "parent_id": parent_id,
                },
            )
            # Persist lifecycle for resume-on-failover.
            if self._lifecycle is not None:
                try:
                    self._lifecycle.upsert_spawn(
                        darwin_id=handle.darwin_id,
                        opening_usdc=float(opening_usdc),
                        parent_id=parent_id,
                        spawned_at=handle.spawned_at,
                        escalation_auto=escalation_auto,
                    )
                except Exception:
                    pass
            # Persist the sealed manifest + layer blobs so a future
            # resume can re-mint the same DNA (lossless resume; doc 08).
            # Skipped when we just resumed an existing Darwin (the row
            # is already present and unchanged).
            if (
                self._manifest_store is not None
                and _resume_id is None
            ):
                try:
                    self._manifest_store.put(
                        darwin_id=handle.darwin_id,
                        manifest_json=handle.runtime._p.manifest.model_dump_json(),  # noqa: SLF001
                        layer_blobs=stack.layer_blobs,
                        layer_names=stack.layer_names,
                    )
                except Exception:
                    pass
            return self._status(handle)

    # -- status (doc 08: GET /darwins/{id}) -------------------------------
    async def status(self, darwin_id: str) -> DarwinStatus:
        if self._is_k8s(darwin_id):
            return await self._k8s_status(darwin_id)
        return self._status(self._get(darwin_id))

    async def _k8s_status(self, darwin_id: str) -> DarwinStatus:
        """Build a DarwinStatus from the K8s driver's snapshot + the
        supervisor's bookkeeping. Generation + l0_version come from the
        driver via the orchestrator's snapshot endpoint (added in a
        follow-on slice — for now those fields default)."""
        bookkeeping = self._k8s_handles[darwin_id]
        snapshot = await self._k8s_driver.snapshot(darwin_id)
        self._k8s_handles[darwin_id]["snapshot"] = snapshot
        return DarwinStatus(
            darwin_id=darwin_id,
            generation=0,
            parent_id=bookkeeping.get("parent_id"),
            l0_version="",
            birth_ts=bookkeeping["spawned_at"],
            spawned_at=bookkeeping["spawned_at"],
            alive=snapshot.alive,
            paused=snapshot.paused,
            killed=snapshot.killed,
            quarantined=False,
            quarantine_reason=None,
            death_reason=snapshot.death_reason,
            balance_usdc=snapshot.balance_usdc,
            tick=snapshot.tick,
            days_elapsed=snapshot.days_elapsed,
            open_escalations=0,
        )

    # -- provisioning (doc 04, 07) ----------------------------------------
    def list_resources(
        self,
        darwin_id: str,
        *,
        include_deprovisioned: bool = False,
    ) -> list[ResourceRecord]:
        """List the Darwin's provisioned infrastructure resources. Read-only;
        no audit entry. Does NOT require the Darwin to still be alive (a
        killed Darwin's resource history is part of the operator audit
        view)."""
        # Defense in depth: confirm the id is known to the supervisor. We
        # accept inactive darwins so /resources still works after a kill.
        if (
            darwin_id not in self._darwins
            and not self._lineage_registry.has(darwin_id)
        ):
            raise UnknownDarwin(darwin_id)
        return self._provisioning.list_resources(
            darwin_id, include_deprovisioned=include_deprovisioned,
        )

    async def deprovision_resource(
        self,
        darwin_id: str,
        resource_id: str,
        *,
        actor: str = "anonymous",
    ) -> ResourceRecord:
        """Operator/owner-driven deprovision. Tears down the resource via
        the platform backend and marks the record deprovisioned. Audit-logged.

        Cannot be reversed — to re-provision, the Darwin must request a fresh
        resource through its ``infrastructure.provisioner`` neuron."""
        with span(
            "supervisor.deprovision_resource",
            darwin_id=darwin_id,
            attributes={"resource_id": resource_id},
        ):
            if (
                darwin_id not in self._darwins
                and not self._lineage_registry.has(darwin_id)
            ):
                raise UnknownDarwin(darwin_id)
            record = self._provisioning.deprovision(
                darwin_id=darwin_id, resource_id=resource_id,
            )
            self._audit.record(
                actor=actor,
                action="deprovision_resource",
                darwin_id=darwin_id,
                metadata={"resource_id": resource_id, "kind": record.kind},
            )
            return record

    def provisioning_summary(self) -> dict[str, int]:
        """Fleet-level active-resource counts by kind. Cheap; for dashboards."""
        return self._provisioning.fleet_summary()

    # -- browser cache (doc 15 promotion + cost attribution) --------------
    def browser_sessions(
        self,
        darwin_id: str,
        *,
        limit: int | None = None,
    ) -> list:
        """Per-Darwin browser session log. Read-only; for the owner UI."""
        if (
            darwin_id not in self._darwins
            and not self._lineage_registry.has(darwin_id)
        ):
            raise UnknownDarwin(darwin_id)
        return [
            r.to_json() for r in self._browser_cache.sessions(
                darwin_id=darwin_id, limit=limit,
            )
        ]

    def browser_stats(self) -> dict:
        """Fleet-level browser cache stats + cost breakdown. For the
        operator dashboard."""
        return self._browser_cache.stats()

    # -- recurring harvest rules (doc 05) ---------------------------------
    def add_harvest_rule(
        self,
        darwin_id: str,
        *,
        destination: str,
        trigger_balance_usdc: float,
        amount_usdc: float | None = None,
        interval_seconds: float | None = None,
        actor: str = "anonymous",
    ) -> HarvestRule:
        """Register a recurring rule. Fires whenever the wallet balance
        exceeds ``trigger_balance_usdc`` and at least ``interval_seconds``
        have passed since the last fire."""
        if darwin_id not in self._darwins:
            raise UnknownDarwin(darwin_id)
        with span(
            "supervisor.add_harvest_rule",
            darwin_id=darwin_id,
            attributes={
                "destination": destination,
                "trigger_balance_usdc": float(trigger_balance_usdc),
            },
        ):
            kwargs = dict(
                darwin_id=darwin_id,
                destination=destination,
                trigger_balance_usdc=trigger_balance_usdc,
                amount_usdc=amount_usdc,
            )
            if interval_seconds is not None:
                kwargs["interval_seconds"] = interval_seconds
            try:
                rule = self._harvest_scheduler.add(**kwargs)
            except HarvestRuleError as exc:
                raise SupervisorError(str(exc)) from exc
            self._audit.record(
                actor=actor,
                action="add_harvest_rule",
                darwin_id=darwin_id,
                metadata={
                    "rule_id": rule.id,
                    "destination": destination,
                    "trigger_balance_usdc": float(trigger_balance_usdc),
                    "amount_usdc": (
                        float(amount_usdc) if amount_usdc is not None else None
                    ),
                    "interval_seconds": rule.interval_seconds,
                },
            )
            return rule

    def remove_harvest_rule(
        self, darwin_id: str, rule_id: str, *, actor: str = "anonymous",
    ) -> HarvestRule:
        if darwin_id not in self._darwins:
            raise UnknownDarwin(darwin_id)
        with span(
            "supervisor.remove_harvest_rule",
            darwin_id=darwin_id,
            attributes={"rule_id": rule_id},
        ):
            try:
                rule = self._harvest_scheduler.remove(rule_id)
            except HarvestRuleError as exc:
                raise SupervisorError(str(exc)) from exc
            if rule.darwin_id != darwin_id:
                # Re-insert so the rule isn't lost on a misaddressed call.
                self._harvest_scheduler._rules[rule_id] = rule  # noqa: SLF001
                raise SupervisorError(
                    f"rule {rule_id!r} belongs to a different Darwin"
                )
            self._audit.record(
                actor=actor,
                action="remove_harvest_rule",
                darwin_id=darwin_id,
                metadata={"rule_id": rule_id},
            )
            return rule

    def list_harvest_rules(self, darwin_id: str) -> list[HarvestRule]:
        if (
            darwin_id not in self._darwins
            and not self._lineage_registry.has(darwin_id)
        ):
            raise UnknownDarwin(darwin_id)
        return self._harvest_scheduler.list(darwin_id)

    # -- non-LLM resource throttles (doc 09 level 2) ---------------------
    def set_resource_throttle(
        self,
        darwin_id: str,
        *,
        kind: str,
        max_daily_calls: int,
        actor: str = "anonymous",
    ) -> ResourceThrottle:
        """Install a per-Darwin daily call cap on a non-LLM resource.
        ``kind`` ∈ ``{"sandbox", "browser", "egress"}``. The LLM tier
        throttle remains the genome's :meth:`throttle_llm_tier` lever."""
        if darwin_id not in self._darwins:
            raise UnknownDarwin(darwin_id)
        with span(
            "supervisor.set_resource_throttle",
            darwin_id=darwin_id,
            attributes={"kind": kind, "max_daily_calls": max_daily_calls},
        ):
            try:
                t = self._throttle_service.set(
                    darwin_id=darwin_id, kind=kind,
                    max_daily_calls=max_daily_calls,
                )
            except ThrottleError as exc:
                raise SupervisorError(str(exc)) from exc
            self._audit.record(
                actor=actor,
                action="set_resource_throttle",
                darwin_id=darwin_id,
                metadata={"kind": kind, "max_daily_calls": max_daily_calls},
            )
            return t

    def clear_resource_throttle(
        self,
        darwin_id: str,
        *,
        kind: str | None = None,
        actor: str = "anonymous",
    ) -> list[ResourceThrottle]:
        if darwin_id not in self._darwins:
            raise UnknownDarwin(darwin_id)
        with span(
            "supervisor.clear_resource_throttle",
            darwin_id=darwin_id,
            attributes={"kind": kind or "*"},
        ):
            try:
                removed = self._throttle_service.clear(
                    darwin_id=darwin_id, kind=kind,
                )
            except ThrottleError as exc:
                raise SupervisorError(str(exc)) from exc
            self._audit.record(
                actor=actor,
                action="clear_resource_throttle",
                darwin_id=darwin_id,
                metadata={"kind": kind, "removed": [r.kind for r in removed]},
            )
            return removed

    def list_resource_throttles(self, darwin_id: str) -> list[ResourceThrottle]:
        if (
            darwin_id not in self._darwins
            and not self._lineage_registry.has(darwin_id)
        ):
            raise UnknownDarwin(darwin_id)
        return self._throttle_service.list_for(darwin_id)

    async def _evaluate_harvest_rules(self, handle: _Handle) -> None:
        """Check the per-Darwin rules and fire any that are due. Errors
        in one rule don't poison others."""
        try:
            balance = float(handle.stack.wallet.balance_usdc())
        except Exception:
            return
        due = self._harvest_scheduler.due(
            darwin_id=handle.darwin_id, balance_usdc=balance,
        )
        for d in due:
            try:
                result = await self.harvest(
                    handle.darwin_id,
                    amount_usdc=d.amount_usdc,
                    destination=d.rule.destination,
                    actor="platform.scheduler",
                )
                self._harvest_scheduler.mark_run(
                    d.rule.id, amount=d.amount_usdc,
                )
                # Record at info level via audit (the harvest itself already
                # writes its own ``harvest`` entry; this links rule -> harvest).
                self._audit.record(
                    actor="platform.scheduler",
                    action="harvest_rule_fired",
                    darwin_id=handle.darwin_id,
                    metadata={
                        "rule_id": d.rule.id,
                        "amount_usdc": d.amount_usdc,
                        "destination": d.rule.destination,
                        "harvest_id": result.get("harvest_id"),
                    },
                )
            except Exception as exc:
                # Audit the refusal but don't break the drive loop.
                self._audit.record(
                    actor="platform.scheduler",
                    action="harvest_rule_failed",
                    darwin_id=handle.darwin_id,
                    metadata={"rule_id": d.rule.id, "reason": str(exc)},
                )

    # -- Phase 1: patch staging + real endpoints (doc 21) ----------------
    def _resolve_audit_sink_for(self, darwin_id: str) -> Any | None:
        """Return the per-Darwin audit sink, or None when unknown. The
        eval runner uses this to walk recent audit entries for scenario
        synthesis."""
        h = self._darwins.get(darwin_id)
        if h is None:
            return None
        return h.stack.audit_sink

    def _wire_phase1_endpoints(self, runtime: Any) -> None:
        """Inject real publishing destinations + analytics endpoint onto
        the relevant neurons after boot. Same pattern the supervisor uses
        for the infrastructure.provisioner rebind. Best-effort: a missing
        neuron in the catalog is a no-op (the build may not include it)."""
        if not self._publishing_endpoints and not self._analytics_endpoint:
            return
        try:
            cp = runtime.catalog.get("content.publisher")
            if cp is not None and self._publishing_endpoints:
                existing = getattr(cp, "endpoint_overrides", None) or {}
                cp.endpoint_overrides = {
                    **existing, **self._publishing_endpoints,
                }
        except Exception:
            pass
        try:
            am = runtime.catalog.get("analytics.measurer")
            if am is not None and self._analytics_endpoint:
                am.default_endpoint = self._analytics_endpoint
        except Exception:
            pass

    def _install_patch_staging_hook(self, runtime: Any) -> None:
        """Expose the platform's staging service on the genome's ctx so
        the evaluate_mutation node can dispatch into it. The genome
        treats this attribute as optional; absence keeps Phase-0
        behaviour (in-DNA harness only)."""
        try:
            ctx = runtime._ctx  # noqa: SLF001  — single owner
        except Exception:
            return
        # The hook is a callable: signature mirrors the evaluator's
        # request shape so the node can build it cheaply.
        ctx.patch_staging = _GenomeStagingHook(
            runner=self._eval_runner,
            darwin_id=runtime.identity.darwin_id,
        )

    def _install_soma_merge_hook(self, runtime: Any) -> None:
        """Expose a per-Darwin :class:`SomaMerger` on the genome's ctx so
        apply_mutation can write a promoted patch's real diff onto the
        Darwin's soma tree. No-op when no soma root is configured — the
        genome then keeps the Phase-0 simulation. Each Darwin's merger is
        rooted under ``<soma_root>/<darwin_id>`` so one tenant's
        self-modification can never touch another's code."""
        if self._soma_root is None:
            return
        try:
            ctx = runtime._ctx  # noqa: SLF001  — single owner
        except Exception:
            return
        darwin_id = runtime.identity.darwin_id
        merger = self._soma_mergers.get(darwin_id)
        if merger is None:
            merger = SomaMerger(self._soma_root / darwin_id, clock=self._clock)
            self._soma_mergers[darwin_id] = merger
        ctx.soma_merge = _GenomeMergeHook(merger=merger, darwin_id=darwin_id)

    def soma_merger_for(self, darwin_id: str) -> SomaMerger | None:
        """Read-only handle to a Darwin's soma merger (for ops/rollback +
        tests). None when no soma root is configured or the Darwin has not
        been spawned."""
        return self._soma_mergers.get(darwin_id)

    @property
    def patch_staging(self) -> PatchStagingService:
        """Read-only handle for ops/snapshots + tests."""
        return self._patch_staging

    @property
    def eval_runner(self) -> EvalRunner:
        return self._eval_runner

    # -- Phase 2 service handles (doc 22) --------------------------------
    @property
    def kyc_service(self) -> KYCService | None:
        return self._kyc_service

    @property
    def omnibus_wallet(self) -> OmnibusWallet | None:
        return self._omnibus_wallet

    @property
    def namespace_provisioner(self) -> NamespaceProvisioner | None:
        return self._namespace_provisioner

    @property
    def drift_detector(self) -> DriftDetector | None:
        return self._drift_detector

    @property
    def merkle_anchor(self) -> MerkleAnchorPublisher | None:
        return self._merkle_anchor

    def collect_daily_merkle_roots(self, day_iso: str) -> list[DarwinDailyRoot]:
        """Walk every alive Darwin and ask its audit logger for the
        current Merkle root. The supervisor calls this once a day from
        the platform-side cron before invoking
        :meth:`MerkleAnchorPublisher.publish_daily`."""
        out: list[DarwinDailyRoot] = []
        for did, h in self._darwins.items():
            try:
                root = h.runtime.audit.merkle_root()
            except Exception:
                continue
            out.append(DarwinDailyRoot(
                darwin_id=did, day_iso=day_iso, root=root,
            ))
        return out

    def reconcile_omnibus(
        self, *, sum_override: float | None = None,
    ) -> ReconciliationResult | None:
        """Daily reconciliation hook. Returns None when no omnibus is
        wired (single-tenant dev). The result carries the discrepancy +
        a pass/fail flag the ops monitor consumes.

        ``sum_override`` supplies the summed Darwin-wallet balance when the
        Darwins aren't in-process (K8s mode reads each pod's balance from its
        snapshot); the default sums the in-process handles."""
        if self._omnibus_wallet is None:
            return None
        if sum_override is not None:
            sum_wallets = float(sum_override)
        else:
            sum_wallets = 0.0
            for h in self._darwins.values():
                try:
                    sum_wallets += float(h.stack.wallet.balance_usdc())
                except Exception:
                    continue
        return self._omnibus_wallet.reconcile(
            sum_of_darwin_wallets=sum_wallets,
        )

    def credit_user_omnibus(
        self,
        *,
        user_subject: str,
        amount_usdc: float,
        source_ref: str = "manual",
        actor: str = "anonymous",
    ) -> dict[str, Any]:
        """Record a confirmed deposit into a user's omnibus credit balance.

        Mirrors what the Stripe webhook does on ``confirm_deposit`` — it
        assumes the USDC has already settled on the omnibus address (e.g. a
        testnet faucet top-up or an out-of-band wire the operator has
        reconciled). Operator-scoped: minting credit is a privileged action.
        Without it there is no non-Stripe path to give an owner the credit a
        spawn's ``_fund_darwin_from_omnibus`` debits."""
        if self._omnibus_wallet is None:
            raise SupervisorError("omnibus wallet is not configured")
        if not user_subject:
            raise SupervisorError("user_subject is required")
        if float(amount_usdc) <= 0:
            raise SupervisorError("amount_usdc must be positive")
        entry = self._omnibus_wallet.record_deposit(
            user_subject=user_subject,
            amount_usdc=float(amount_usdc),
            source_ref=source_ref,
            actor=actor,
        )
        self._audit.record(
            actor=actor,
            action="omnibus_manual_credit",
            darwin_id=None,
            metadata={
                "user_subject": user_subject,
                "amount_usdc": float(amount_usdc),
                "source_ref": source_ref,
            },
        )
        return {
            "user_subject": user_subject,
            "credited_usdc": float(amount_usdc),
            "balance_usdc": self._omnibus_wallet.user_credit(user_subject),
            "entry_ts": entry.ts,
        }

    def observe_drift(self, profile: DailyProfile) -> list[DriftAlert]:
        """Feed a per-Darwin daily profile to the drift detector.
        Returns the alerts produced (may be empty). Caller (the platform
        cron) is responsible for building the profile from the day's
        audit slice via :meth:`DailyProfile.from_audit_rows`."""
        if self._drift_detector is None:
            return []
        return self._drift_detector.observe_day(profile)

    # -- vault admin (doc 16 §"Bidirectional shared vault") --------------
    def _resolve_vault_for(self, darwin_id: str) -> Any | None:
        """Return the raw vault backend for ``darwin_id``, or None if the
        Darwin isn't currently held by this supervisor. Used by the vault
        admin service so its API surface stays narrow (one service, many
        Darwins) without sacrificing per-Darwin isolation."""
        h = self._darwins.get(darwin_id)
        if h is None:
            return None
        return h.stack.vault

    def vault_put(
        self,
        darwin_id: str,
        *,
        tier: str,
        key: str,
        value: str,
        scope: str | None = None,
        ttl_seconds: float | None = None,
        original_filename: str | None = None,
        actor: str = "anonymous",
    ) -> VaultEntryRef:
        """Owner writes a secret into the Darwin's namespace (doc 16:
        KYC docs, credentials, payment methods). Sensitive-tier uploads
        are auto-tracked by the retention sweeper (default 90-day TTL
        from last use)."""
        if darwin_id not in self._darwins:
            raise UnknownDarwin(darwin_id)
        with span(
            "supervisor.vault_put",
            darwin_id=darwin_id,
            attributes={"tier": tier, "scope": scope or ""},
        ):
            try:
                ref = self._vault_admin.put(
                    darwin_id=darwin_id, tier=tier, key=key, value=value,
                    scope=scope, uploaded_by=actor,
                    ttl_seconds=ttl_seconds,
                    original_filename=original_filename,
                )
            except VaultAdminError as exc:
                raise SupervisorError(str(exc)) from exc
            self._audit.record(
                actor=actor,
                action="vault_put",
                darwin_id=darwin_id,
                metadata={
                    "tier": tier,
                    "scope": scope,
                    "key": key,
                    "sha256": ref.sha256,
                    "size_bytes": ref.size_bytes,
                    # Value bytes never enter the audit chain.
                },
            )
            return ref

    def vault_delete(
        self,
        darwin_id: str,
        *,
        tier: str,
        key: str,
        scope: str | None = None,
        actor: str = "anonymous",
    ) -> VaultEntryRef:
        """Owner removes a stored secret. Audit-logged. Refuses with a
        clear error rather than silently no-opping on unknown keys (a
        silent no-op would hide a typo'd path from compliance review)."""
        if darwin_id not in self._darwins:
            raise UnknownDarwin(darwin_id)
        with span(
            "supervisor.vault_delete",
            darwin_id=darwin_id,
            attributes={"tier": tier, "scope": scope or ""},
        ):
            try:
                ref = self._vault_admin.delete(
                    darwin_id=darwin_id, tier=tier, key=key, scope=scope,
                )
            except VaultAdminError as exc:
                raise SupervisorError(str(exc)) from exc
            self._audit.record(
                actor=actor,
                action="vault_delete",
                darwin_id=darwin_id,
                metadata={
                    "tier": tier, "scope": scope, "key": key,
                    "sha256": ref.sha256,
                },
            )
            return ref

    def list_vault_entries(
        self,
        darwin_id: str,
        *,
        tier: str | None = None,
    ) -> list[VaultEntryRef]:
        """Read-only inventory for the owner UI. Values are never returned —
        only refs (path, sha256, size, uploaded_at, expires_at)."""
        if darwin_id not in self._darwins:
            raise UnknownDarwin(darwin_id)
        return self._vault_admin.list(darwin_id, tier=tier)

    def sweep_vault_retention(self) -> list[VaultEntryRef]:
        """Purge expired sensitive entries. Returns the refs that were
        purged so the caller can audit them. Called automatically by the
        drive cycle; exposed for tests + an operator-on-demand sweep."""
        purged = self._vault_admin.sweep_expired()
        for ref in purged:
            self._audit.record(
                actor="platform.retention",
                action="vault_retention_purge",
                darwin_id=ref.darwin_id,
                metadata={
                    "tier": ref.tier, "scope": ref.scope, "key": ref.key,
                    "sha256": ref.sha256,
                    "uploaded_at": ref.uploaded_at,
                },
            )
        return purged

    @property
    def vault_admin(self) -> VaultAdminService:
        """For tests + advanced operators: direct access to the admin
        service. The HTTP surface only goes through the supervisor
        methods above (which audit-log every mutation)."""
        return self._vault_admin

    # -- pause / resume (doc 08: POST /darwins/{id}/pause|resume) ---------
    async def pause(
        self,
        darwin_id: str,
        *,
        reason: str = "owner paused",
        actor: str = "anonymous",
    ) -> None:
        """Flip the platform pause flag. The genome notices at the next
        heartbeat boundary, anchors, and breaks — same life, not a death."""
        if self._is_k8s(darwin_id):
            await self._k8s_driver.pause(darwin_id, reason=reason)
            self._audit.record(
                actor=actor, action="pause", darwin_id=darwin_id,
                metadata={"reason": reason, "driver": "k8s"},
            )
            return
        with span("supervisor.pause", darwin_id=darwin_id,
                  attributes={"reason": reason}) as s:
            h = self._get(darwin_id)
            if h.killed:
                raise SupervisorError(
                    f"darwin {darwin_id} is killed; pause is a no-op"
                )
            annotate_span_with_runtime(s, h.runtime)
            h.stack.pause.pause(reason)
            self._audit.record(
                actor=actor, action="pause", darwin_id=darwin_id,
                metadata={"reason": reason},
            )

    async def resume(self, darwin_id: str, *, actor: str = "anonymous") -> None:
        if self._is_k8s(darwin_id):
            await self._k8s_driver.resume(darwin_id)
            self._audit.record(
                actor=actor, action="resume", darwin_id=darwin_id,
                metadata={"driver": "k8s"},
            )
            return
        with span("supervisor.resume", darwin_id=darwin_id) as s:
            h = self._get(darwin_id)
            if h.killed:
                raise SupervisorError(f"darwin {darwin_id} is killed; cannot resume")
            if h.quarantine.active:
                raise SupervisorError(
                    f"darwin {darwin_id} is quarantined "
                    f"(reason: {h.quarantine.reason!r}); requires unquarantine, "
                    f"not resume"
                )
            annotate_span_with_runtime(s, h.runtime)
            h.stack.pause.resume()
            # Clear the auto-pause latch so a subsequent CRITICAL can pause
            # again if the breach is still live. (A repeat breach is a
            # different alert event from the operator's perspective.)
            h.auto_paused_for = None
            self._audit.record(
                actor=actor, action="resume", darwin_id=darwin_id, metadata={},
            )

    # -- platform-side quarantine (doc 08/09 level 4) ---------------------
    async def quarantine(
        self,
        darwin_id: str,
        *,
        reason: str,
        operator: str = "platform",
        actor: str | None = None,
    ) -> None:
        """Move a Darwin to doc-09 level 4: pause + flag for platform review.
        Cannot be cleared via owner ``resume`` — only ``unquarantine`` or a
        kill. Idempotent: re-quarantining records the new reason/operator
        but does not double-pause."""
        with span(
            "supervisor.quarantine",
            darwin_id=darwin_id,
            attributes={"reason": reason, "operator": operator},
        ) as s:
            h = self._get(darwin_id)
            if h.killed:
                raise SupervisorError(
                    f"darwin {darwin_id} is killed; quarantine is a no-op"
                )
            annotate_span_with_runtime(s, h.runtime)
            h.quarantine = QuarantineState(
                active=True,
                reason=reason,
                operator=operator,
                applied_ts=self._clock(),
            )
            # Pause if not already paused; the pause flag is shared with owner
            # pause so a Darwin already paused stays paused under quarantine.
            try:
                h.stack.pause.pause(f"quarantine:{reason}")
            except Exception:
                # The fake should not raise here, but a misbehaving pause
                # source must not block the quarantine state.
                pass
            self._audit.record(
                actor=actor or operator, action="quarantine",
                darwin_id=darwin_id,
                metadata={"reason": reason, "operator": operator},
            )
            # Write-through so a supervisor restart restores the quarantine
            # (doc 23) — a quarantined Darwin must not silently come back
            # running. Best-effort, like the kill write-through.
            if self._lifecycle is not None:
                try:
                    self._lifecycle.set_quarantine(
                        darwin_id, active=True, reason=reason,
                        operator=operator,
                        applied_ts=h.quarantine.applied_ts,
                    )
                except Exception:
                    pass

    async def unquarantine(
        self,
        darwin_id: str,
        *,
        operator: str = "platform",
        actor: str | None = None,
    ) -> None:
        """Clear the quarantine flag. Does NOT auto-resume — an operator
        clearing quarantine has to make a separate ``resume`` call so the
        two state transitions are independently auditable."""
        with span(
            "supervisor.unquarantine",
            darwin_id=darwin_id,
            attributes={"operator": operator},
        ) as s:
            h = self._get(darwin_id)
            if not h.quarantine.active:
                return
            annotate_span_with_runtime(s, h.runtime)
            h.quarantine = QuarantineState(active=False)
            self._audit.record(
                actor=actor or operator, action="unquarantine",
                darwin_id=darwin_id,
                metadata={"operator": operator},
            )
            if self._lifecycle is not None:
                try:
                    self._lifecycle.set_quarantine(darwin_id, active=False)
                except Exception:
                    pass

    # -- billing (doc 08 §"Billing model") --------------------------------
    async def harvest(
        self,
        darwin_id: str,
        *,
        amount_usdc: float,
        destination: str,
        actor: str = "anonymous",
    ) -> dict[str, Any]:
        """Owner-initiated profit harvest. Splits ``amount_usdc`` into a
        net payout to ``destination`` and a platform fee per
        :class:`BillingPolicy`. Wallet enforces solvency. The fee is
        recorded on the platform-side billing ledger AND on the wallet's
        own movement log (so the genome's audit reconciles too)."""
        if amount_usdc <= 0:
            raise SupervisorError("harvest amount must be positive")
        if not destination:
            raise SupervisorError("harvest destination is required")
        with span(
            "supervisor.harvest",
            darwin_id=darwin_id,
            attributes={"amount_usdc": float(amount_usdc),
                        "destination": destination},
        ) as s:
            h = self._get(darwin_id)
            annotate_span_with_runtime(s, h.runtime)
            policy = self._billing.policy
            fee = policy.harvest_fee(float(amount_usdc))
            net = float(amount_usdc) - fee
            try:
                wallet_row = h.stack.wallet.harvest_to_owner(
                    destination=destination, net_usdc=net, fee_usdc=fee
                )
            except Exception as exc:
                raise SupervisorError(f"wallet refused harvest: {exc}") from exc
            ledger_row = self._billing.record_harvest(
                darwin_id,
                gross_usdc=float(amount_usdc),
                fee_usdc=fee,
                destination=destination,
            )
            # Re-baseline ``profit_baseline_usdc`` so a withdrawn profit is
            # not re-counted as the next generation's profit. (Reproduction
            # also rebaselines on its own path; harvest takes the same care
            # so the two stories agree.)
            try:
                h.runtime.state.profit_baseline_usdc = float(
                    h.stack.wallet.balance_usdc()
                )
            except Exception:
                pass
            # The metered sweep would also see the harvest rows if we let it;
            # bump the cursor so the harvest fee is not double-counted as
            # infrastructure revenue.
            h.billing_observed = len(h.stack.wallet.movements())
            self._audit.record(
                actor=actor, action="harvest", darwin_id=darwin_id,
                metadata={
                    "amount_usdc": float(amount_usdc),
                    "destination": destination,
                    "fee_usdc": float(fee),
                    "net_usdc": float(net),
                },
            )
            return {**wallet_row, "ledger": ledger_row}

    async def billing(self, darwin_id: str) -> dict[str, Any]:
        self._get(darwin_id)  # validate
        return self._billing.summary(darwin_id)

    async def fleet_billing(self) -> dict[str, Any]:
        return self._billing.fleet_summary()

    @property
    def billing_service(self) -> BillingService:
        return self._billing

    @property
    def persistent_token_store(self):
        """The SQLite-backed token store, when persistence is configured.
        Pass this to ``build_app(..., token_store=)`` so auth survives a
        restart. ``None`` when persistence_path was not set."""
        return self._persistence.token_store if self._persistence else None

    # -- operator audit log (doc 08) --------------------------------------
    @property
    def audit_log(self) -> OperatorAuditLog:
        """The hash-chained operator audit log. Ops + compliance reviewers
        read through this rather than the OpenTelemetry traces; it's
        durable and walkable as a chain."""
        return self._audit

    async def audit_entries(
        self,
        *,
        actor: str | None = None,
        action: str | None = None,
        darwin_id: str | None = None,
        since: float | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = self._audit.entries(
            actor=actor, action=action, darwin_id=darwin_id,
            since=since, limit=limit,
        )
        return _audit_to_dicts(rows)

    # -- platform-side throttle (doc 08, doc 09 level 2) ------------------
    async def throttle_llm_tier(
        self,
        darwin_id: str,
        *,
        tier: str,
        max_daily_calls: int,
        actor: str = "anonymous",
    ) -> dict[str, Any]:
        """Install a tighter per-day cap on one LLM tier. The genome's
        :class:`LLMRouter` takes ``min(hp cap, throttle)``, so a throttle
        can only TIGHTEN; the constitution-level cap remains the ceiling."""
        from darwin_dna.machinery.llm_router import Tier

        try:
            t = Tier(tier)
        except ValueError as exc:
            raise SupervisorError(f"unknown tier: {tier!r}") from exc
        with span(
            "supervisor.throttle_llm_tier",
            darwin_id=darwin_id,
            attributes={"tier": tier, "max_daily_calls": max_daily_calls},
        ) as s:
            h = self._get(darwin_id)
            annotate_span_with_runtime(s, h.runtime)
            h.runtime.llm.set_throttle(t, max_daily_calls)
            self._audit.record(
                actor=actor, action="throttle_llm_tier",
                darwin_id=darwin_id,
                metadata={"tier": tier, "max_daily_calls": int(max_daily_calls)},
            )
        return {"tier": tier, "max_daily_calls": int(max_daily_calls),
                "throttles": h.runtime.llm.throttles()}

    async def clear_llm_throttle(
        self,
        darwin_id: str,
        *,
        tier: str | None = None,
        actor: str = "anonymous",
    ) -> dict[str, Any]:
        """Clear the platform throttle for ``tier`` (or all tiers when
        ``tier is None``)."""
        from darwin_dna.machinery.llm_router import Tier

        t: Tier | None = None
        if tier is not None:
            try:
                t = Tier(tier)
            except ValueError as exc:
                raise SupervisorError(f"unknown tier: {tier!r}") from exc
        with span(
            "supervisor.clear_llm_throttle",
            darwin_id=darwin_id,
            attributes={"tier": tier},
        ) as s:
            h = self._get(darwin_id)
            annotate_span_with_runtime(s, h.runtime)
            h.runtime.llm.clear_throttle(t)
            # An explicit clear also clears the auto-throttle latches so a
            # follow-up WARN can throttle again.
            if tier is None:
                h.auto_throttled.clear()
            self._audit.record(
                actor=actor, action="clear_llm_throttle",
                darwin_id=darwin_id, metadata={"tier": tier},
            )
        return {"throttles": h.runtime.llm.throttles()}

    async def get_llm_throttles(self, darwin_id: str) -> dict[str, int]:
        h = self._get(darwin_id)
        return h.runtime.llm.throttles()

    # -- platform-side wallet policy (doc 08 POST /platform/wallets/...) --
    async def set_wallet_policy(
        self,
        darwin_id: str,
        *,
        per_action_cap_usdc: float | None = None,
        per_day_cap_usdc: float | None = None,
        per_lifetime_cap_usdc: float | None = None,
        actor: str = "anonymous",
    ) -> dict[str, Any]:
        """Tighten a Darwin's per-action / per-day / per-lifetime spend caps
        below the genome's hard caps. The platform may only LOWER caps (the
        constitution's hard caps are inviolable); the wallet enforces that.
        The same caps flow into the genome's policy engine via the wallet's
        ``owner_soft_caps`` method, so a refused spend is logged with policy
        rationale, not as a generic wallet_error. Returns the new effective
        policy."""
        with span(
            "supervisor.set_wallet_policy",
            darwin_id=darwin_id,
            attributes={
                "per_action_cap_usdc": per_action_cap_usdc,
                "per_day_cap_usdc": per_day_cap_usdc,
                "per_lifetime_cap_usdc": per_lifetime_cap_usdc,
            },
        ) as s:
            h = self._get(darwin_id)
            annotate_span_with_runtime(s, h.runtime)
            wallet = h.stack.wallet
            if not hasattr(wallet, "set_policy"):
                raise SupervisorError(
                    "wallet does not support platform-installed policy"
                )
            new_policy = wallet.set_policy(
                per_action_cap_usdc=per_action_cap_usdc,
                per_day_cap_usdc=per_day_cap_usdc,
                per_lifetime_cap_usdc=per_lifetime_cap_usdc,
            )
            self._audit.record(
                actor=actor, action="set_wallet_policy",
                darwin_id=darwin_id,
                metadata={
                    "per_action_cap_usdc": per_action_cap_usdc,
                    "per_day_cap_usdc": per_day_cap_usdc,
                    "per_lifetime_cap_usdc": per_lifetime_cap_usdc,
                },
            )
            return new_policy

    async def get_wallet_policy(self, darwin_id: str) -> dict[str, Any]:
        h = self._get(darwin_id)
        wallet = h.stack.wallet
        if not hasattr(wallet, "policy"):
            raise SupervisorError("wallet does not expose policy")
        return wallet.policy()

    # -- pre-approvals (doc 17 §"Darwin paying a human") -------------------
    async def set_preapproval(
        self,
        darwin_id: str,
        *,
        rule_id: str,
        max_amount_usdc: float,
        counterparty_category: str | None = None,
        counterparty_substring: str | None = None,
        expires_at: float | None = None,
        max_uses: int | None = None,
        actor: str = "anonymous",
    ) -> dict[str, Any]:
        """Install (or replace) an owner pre-approval rule on a Darwin's
        wallet. Pre-approvals are typed exceptions to layer-4 escalation:
        a matching ``freelancer`` / ``legal_financial`` / over-threshold
        spend skips the human-review step and is logged with the rule_id
        that cleared it.

        ``max_uses`` caps how many spends a single rule may clear before
        becoming inert; ``None`` is unlimited (use sparingly — a
        standing pre-approval is closer to a policy change than a
        convenience grant). Replacing an existing rule resets its
        ``used_count`` to 0 — installing a new rule under the same id is
        treated as a fresh authorisation. Returns the rule as stored."""
        from darwin_dna.constitution import PreApprovalRule

        rule = PreApprovalRule(
            rule_id=rule_id,
            max_amount_usdc=max_amount_usdc,
            counterparty_category=counterparty_category,
            counterparty_substring=counterparty_substring,
            expires_at=expires_at,
            max_uses=max_uses,
        )
        with span(
            "supervisor.set_preapproval",
            darwin_id=darwin_id,
            attributes={
                "rule_id": rule_id,
                "max_amount_usdc": max_amount_usdc,
            },
        ) as s:
            h = self._get(darwin_id)
            annotate_span_with_runtime(s, h.runtime)
            wallet = h.stack.wallet
            if not hasattr(wallet, "set_preapproval"):
                raise SupervisorError(
                    "wallet does not support pre-approvals",
                )
            wallet.set_preapproval(rule)
            # Persist through so a restart restores the same rule set.
            # Best-effort: a persistence write-failure should not refuse
            # the in-memory install (audit is the loud failure surface).
            if self._preapproval_store is not None:
                try:
                    self._preapproval_store.save(
                        darwin_id=darwin_id, rule=rule,
                    )
                except Exception:
                    pass
            self._audit.record(
                actor=actor, action="set_preapproval",
                darwin_id=darwin_id,
                metadata={
                    "rule_id": rule_id,
                    "max_amount_usdc": max_amount_usdc,
                    "counterparty_category": counterparty_category,
                    "counterparty_substring": counterparty_substring,
                    "expires_at": expires_at,
                    "max_uses": max_uses,
                },
            )
            return rule.model_dump()

    async def revoke_preapproval(
        self, darwin_id: str, rule_id: str, *, actor: str = "anonymous",
    ) -> dict[str, Any]:
        """Drop a pre-approval rule by id. Idempotent — revoking a
        non-existent rule returns ``{"revoked": False}`` rather than
        raising, so an out-of-band cleanup that races a manual revoke
        doesn't surface a spurious error."""
        with span(
            "supervisor.revoke_preapproval",
            darwin_id=darwin_id,
            attributes={"rule_id": rule_id},
        ) as s:
            h = self._get(darwin_id)
            annotate_span_with_runtime(s, h.runtime)
            wallet = h.stack.wallet
            if not hasattr(wallet, "revoke_preapproval"):
                raise SupervisorError(
                    "wallet does not support pre-approvals",
                )
            removed = bool(wallet.revoke_preapproval(rule_id))
            # Always attempt the persistence delete: a wallet-side miss
            # plus a stale persisted row would otherwise resurrect the
            # rule on the next spawn.
            if self._preapproval_store is not None:
                try:
                    self._preapproval_store.delete(
                        darwin_id=darwin_id, rule_id=rule_id,
                    )
                except Exception:
                    pass
            if removed:
                self._audit.record(
                    actor=actor, action="revoke_preapproval",
                    darwin_id=darwin_id,
                    metadata={"rule_id": rule_id},
                )
            return {"revoked": removed, "rule_id": rule_id}

    async def list_preapprovals(
        self, darwin_id: str,
    ) -> list[dict[str, Any]]:
        h = self._get(darwin_id)
        wallet = h.stack.wallet
        if not hasattr(wallet, "owner_preapprovals"):
            return []
        return [r.model_dump() for r in wallet.owner_preapprovals()]

    # -- SMS 2FA inbox (doc 15) -------------------------------------------
    def sms_verification_code(
        self, *, number: str | None = None,
    ) -> dict[str, Any] | None:
        """The latest inbound SMS 2FA code from the platform's Twilio
        inbox (doc 15) — ``{"code", "from", "received_at", "message_sid"}``
        — or ``None`` when none has arrived. Raises :class:`SupervisorError`
        when no Twilio client is configured."""
        if self._sms_client is None:
            raise SupervisorError("no Twilio SMS client configured")
        return self._sms_client.latest_verification_code(to_number=number)

    # -- owner runtime controls (doc 09) ----------------------------------
    async def set_spend_lock(
        self,
        darwin_id: str,
        *,
        locked: bool,
        reason: str = "owner spend-lock",
        actor: str = "anonymous",
    ) -> dict[str, Any]:
        """Engage or release the owner spend-lock (doc 09). A locked Darwin
        keeps running but cannot make outbound payments. Distinct from
        tightening caps — releasing the lock restores the owner's chosen
        caps untouched."""
        with span(
            "supervisor.set_spend_lock",
            darwin_id=darwin_id,
            attributes={"locked": locked},
        ) as s:
            h = self._get(darwin_id)
            annotate_span_with_runtime(s, h.runtime)
            wallet = h.stack.wallet
            if not hasattr(wallet, "set_spend_lock"):
                raise SupervisorError("wallet does not support spend-lock")
            result = wallet.set_spend_lock(
                bool(locked), reason=reason if locked else None
            )
            self._audit.record(
                actor=actor,
                action="spend_lock" if locked else "spend_unlock",
                darwin_id=darwin_id,
                metadata={"reason": reason} if locked else {},
            )
            return result

    async def set_reproduction_block(
        self,
        darwin_id: str,
        *,
        blocked: bool,
        actor: str = "anonymous",
    ) -> dict[str, Any]:
        """Block or unblock autonomous reproduction (doc 09). A blocked Darwin
        still runs and earns; ``check_reproduction`` simply never fires a
        natural spawn. A platform forced reproduction is unaffected — forcing
        is an explicit override, blocking is a standing policy."""
        with span(
            "supervisor.set_reproduction_block",
            darwin_id=darwin_id,
            attributes={"blocked": blocked},
        ) as s:
            h = self._get(darwin_id)
            if h.killed:
                raise SupervisorError(
                    f"darwin {darwin_id} is killed; reproduction block "
                    f"is a no-op"
                )
            annotate_span_with_runtime(s, h.runtime)
            h.reproduction_blocked = bool(blocked)
            self._audit.record(
                actor=actor,
                action=(
                    "block_reproduction" if blocked
                    else "unblock_reproduction"
                ),
                darwin_id=darwin_id,
                metadata={},
            )
            return {"reproduction_blocked": h.reproduction_blocked}

    async def set_disabled_neurons(
        self,
        darwin_id: str,
        *,
        neurons: list[str],
        actor: str = "anonymous",
    ) -> dict[str, Any]:
        """Replace the set of neurons disabled for this Darwin (doc 09). A
        disabled neuron is dropped from dispatch selection; pass an empty
        list to re-enable everything."""
        with span("supervisor.set_disabled_neurons", darwin_id=darwin_id) as s:
            h = self._get(darwin_id)
            annotate_span_with_runtime(s, h.runtime)
            h.disabled_neurons = {
                str(n).strip() for n in neurons if str(n).strip()
            }
            self._audit.record(
                actor=actor,
                action="set_disabled_neurons",
                darwin_id=darwin_id,
                metadata={"disabled": sorted(h.disabled_neurons)},
            )
            return {"disabled_neurons": sorted(h.disabled_neurons)}

    async def set_disabled_categories(
        self,
        darwin_id: str,
        *,
        categories: list[str],
        actor: str = "anonymous",
    ) -> dict[str, Any]:
        """Replace the set of business categories disabled for this Darwin
        (doc 09). A task whose category is disabled is not dispatched."""
        with span(
            "supervisor.set_disabled_categories", darwin_id=darwin_id
        ) as s:
            h = self._get(darwin_id)
            annotate_span_with_runtime(s, h.runtime)
            h.disabled_categories = {
                str(c).strip() for c in categories if str(c).strip()
            }
            self._audit.record(
                actor=actor,
                action="set_disabled_categories",
                darwin_id=darwin_id,
                metadata={"disabled": sorted(h.disabled_categories)},
            )
            return {"disabled_categories": sorted(h.disabled_categories)}

    # -- fleet-wide incident-response levers (doc 09) ----------------------
    def _live_ids(self) -> list[str]:
        return [
            did for did, h in self._darwins.items()
            if not h.killed and h.runtime.state.alive
        ]

    async def fleet_pause(
        self, *, reason: str = "fleet pause", actor: str = "anonymous",
    ) -> dict[str, Any]:
        """Pause every live Darwin at once — the fleet-wide kill-switch's
        softer sibling for incident response (doc 09)."""
        paused: list[str] = []
        for did in self._live_ids():
            try:
                await self.pause(did, reason=reason, actor=actor)
                paused.append(did)
            except Exception:  # noqa: BLE001 — best-effort across the fleet
                pass
        self._audit.record(
            actor=actor, action="fleet_pause", darwin_id=None,
            metadata={"reason": reason, "count": len(paused)},
        )
        return {"paused": paused}

    async def fleet_resume(self, *, actor: str = "anonymous") -> dict[str, Any]:
        resumed: list[str] = []
        for did in list(self._darwins):
            try:
                await self.resume(did, actor=actor)
                resumed.append(did)
            except Exception:  # noqa: BLE001
                pass
        self._audit.record(
            actor=actor, action="fleet_resume", darwin_id=None,
            metadata={"count": len(resumed)},
        )
        return {"resumed": resumed}

    async def fleet_spend_lock(
        self, *, locked: bool, reason: str = "fleet spend-lock",
        actor: str = "anonymous",
    ) -> dict[str, Any]:
        """Spend-lock (or unlock) every live Darwin — freeze all outbound
        payments fleet-wide without killing the organisms (doc 09)."""
        affected: list[str] = []
        for did in self._live_ids():
            try:
                await self.set_spend_lock(
                    did, locked=locked, reason=reason, actor=actor,
                )
                affected.append(did)
            except Exception:  # noqa: BLE001
                pass
        self._audit.record(
            actor=actor,
            action="fleet_spend_lock" if locked else "fleet_spend_unlock",
            darwin_id=None, metadata={"count": len(affected)},
        )
        return {"spend_locked" if locked else "spend_unlocked": affected}

    async def fleet_block_category(
        self, *, category: str, actor: str = "anonymous",
    ) -> dict[str, Any]:
        """Disable a business category across the whole fleet (additive) — park
        a whole line of work everywhere at once (doc 09)."""
        cat = str(category).strip()
        if not cat:
            raise SupervisorError("category is required")
        affected: list[str] = []
        for did in self._live_ids():
            h = self._darwins[did]
            h.disabled_categories = set(h.disabled_categories) | {cat}
            affected.append(did)
        self._audit.record(
            actor=actor, action="fleet_block_category", darwin_id=None,
            metadata={"category": cat, "count": len(affected)},
        )
        return {"category": cat, "blocked_on": affected}

    def block_counterparty(
        self, address: str, *, actor: str = "anonymous",
    ) -> dict[str, Any]:
        """Hard-block a counterparty address fleet-wide (doc 09): add it to the
        sanctions screener so every Darwin's wallet refuses it. Requires a
        local screener that supports runtime blocking (the OFAC-ingested
        LocalDenylistScreener); remote-provider deployments block at the
        provider console instead."""
        blocker = getattr(self._sanctions, "block", None)
        if not callable(blocker):
            raise SupervisorError(
                "the configured sanctions screener does not support runtime "
                "counterparty blocking; block at the provider"
            )
        blocker(address)
        self._audit.record(
            actor=actor, action="block_counterparty", darwin_id=None,
            metadata={"address": address},
        )
        return {"blocked": address}

    def get_owner_controls(self, darwin_id: str) -> dict[str, Any]:
        """The current owner-control state for a Darwin (doc 09)."""
        h = self._get(darwin_id)
        wallet = h.stack.wallet
        spend_lock = (
            wallet.spend_lock()
            if hasattr(wallet, "spend_lock")
            else {"spend_locked": False, "reason": None}
        )
        return {
            "spend_lock": spend_lock,
            "reproduction_blocked": h.reproduction_blocked,
            "disabled_neurons": sorted(h.disabled_neurons),
            "disabled_categories": sorted(h.disabled_categories),
        }

    # -- platform signing service surface (doc 08) ------------------------
    def signing_public_key_pem(self) -> bytes:
        """The platform's signing public key in PEM. Operators verify against
        this to confirm any DNA manifest signature in the system. In a real
        deployment this comes from an HSM; here it's the fake SigningService
        the Supervisor instantiated and shares across every Darwin it spawns.
        Available regardless of whether any Darwins have been spawned yet."""
        return self._signer.public_key_pem()

    # -- kill (doc 08: POST /darwins/{id}/kill) ---------------------------
    async def _cut_egress(self, darwin_id: str, *, reason: str) -> None:
        """Cut a Darwin's egress at the platform proxy, in parallel with the
        in-process kill flag (docs 08/09/24). Best-effort: an egress-cut
        failure must never block or raise into kill/quarantine — the kill flag
        is still the primary halt. Runs a sync revoker off-loop so the
        HTTP round-trip can't stall the supervisor's event loop."""
        revoker = self._egress_revoker
        if revoker is None:
            return
        revoke = getattr(revoker, "revoke", None)
        if revoke is None:
            return
        try:
            if asyncio.iscoroutinefunction(revoke):
                await revoke(darwin_id)
            else:
                await asyncio.to_thread(revoke, darwin_id)
            self._audit.record(
                actor="platform", action="egress_revoked", darwin_id=darwin_id,
                metadata={"reason": reason},
            )
        except Exception:
            pass

    async def kill(
        self,
        darwin_id: str,
        *,
        reason: str = "owner kill",
        actor: str = "anonymous",
    ) -> None:
        """Flip the platform kill flag. The genome polls it at the loop
        boundary and at every node boundary (doc 09: ≤5s latency), then runs
        ``_halt_on_kill`` which writes the final audit record and anchors.
        In parallel we cut the Darwin's egress at the proxy so an
        orchestrator that stops polling still cannot reach the network."""
        if self._is_k8s(darwin_id):
            await self._k8s_driver.kill(darwin_id, reason=reason)
            await self._cut_egress(darwin_id, reason=reason)
            # Revoke the per-Darwin LiteLLM virtual key (doc 13) so a pod that
            # stops polling the kill flag still can't reach the LLM gateway.
            self._revoke_llm_virtual_key(darwin_id)
            # The pod will halt itself; the driver's stop() is for full
            # namespace teardown which we defer until the operator opts
            # in (a killed Darwin's resources are still queryable for
            # forensics until then).
            self._audit.record(
                actor=actor, action="kill", darwin_id=darwin_id,
                metadata={"reason": reason, "driver": "k8s"},
            )
            return
        with span("supervisor.kill", darwin_id=darwin_id,
                  attributes={"reason": reason}) as s:
            h = self._get(darwin_id)
            annotate_span_with_runtime(s, h.runtime)
            h.killed = True
            h.stack.kill.flip(reason)
            # Cut egress at the proxy in parallel with the kill flag.
            await self._cut_egress(darwin_id, reason=reason)
            # Revoke the per-Darwin LiteLLM virtual key (doc 13) so the dead
            # Darwin's credential stops working immediately.
            self._revoke_llm_virtual_key(darwin_id)
            self._audit.record(
                actor=actor, action="kill", darwin_id=darwin_id,
                metadata={"reason": reason},
            )
            if self._lifecycle is not None:
                try:
                    self._lifecycle.mark_killed(darwin_id, reason=reason)
                except Exception:
                    pass
            # DEFER teardown (doc 09 #3): a killed Darwin's namespace + organism
            # DB are RETAINED for the retention window so the owner can inspect
            # the data and a re-spawn can inherit accounts/databases. We only
            # SCHEDULE teardown here; the fleet-maintenance reaper destroys the
            # resources once the window elapses (or an operator force-tears-down
            # via teardown_dead_darwin). Best-effort; never blocks kill.
            if self._namespace_provisioner is not None:
                try:
                    self._namespace_provisioner.schedule_teardown(
                        darwin_id=darwin_id,
                        due_ts=float(self._clock())
                        + self._dead_darwin_retention_seconds,
                        actor=actor,
                    )
                except Exception:
                    pass
            # Drop persisted pre-approval rules so a future re-spawn of
            # the same darwin_id doesn't inherit stale owner grants.
            if self._preapproval_store is not None:
                try:
                    self._preapproval_store.delete_all(darwin_id=darwin_id)
                except Exception:
                    pass
            # Credit any residual USDC the Darwin still held back to the
            # owning user's omnibus balance. Recording-only; the on-chain
            # reconciliation is operator-driven.
            self._sweep_dead_darwin_to_omnibus(h)

    # -- force reproduction (doc 08: POST /darwins/{id}/reproduce/force) --
    async def force_reproduce(
        self, darwin_id: str, *, actor: str = "anonymous"
    ) -> None:
        """Set the genome's reproduce flag for the next heartbeat. The
        genome still runs its own treasury/fitness gates inside ``_reproduce``;
        forcing requests, it does not bypass."""
        if self._is_k8s(darwin_id):
            await self._k8s_driver.force_reproduce(darwin_id)
            self._audit.record(
                actor=actor, action="force_reproduce", darwin_id=darwin_id,
                metadata={"driver": "k8s"},
            )
            return
        with span("supervisor.force_reproduce", darwin_id=darwin_id) as s:
            h = self._get(darwin_id)
            if not h.runtime.state.alive:
                raise SupervisorError(f"darwin {darwin_id} is not alive")
            annotate_span_with_runtime(s, h.runtime)
            h.reproduce_requested = True
            self._audit.record(
                actor=actor, action="force_reproduce", darwin_id=darwin_id,
                metadata={},
            )

    # -- forced reproduction with amendment (doc 02 + doc 09) -------------
    async def force_reproduce_with_amendment(
        self,
        darwin_id: str,
        *,
        amendment_blob: bytes,
        amendment_name: str,
        kill_parent_reason: str = "amendment reproduction",
        actor: str = "anonymous",
    ) -> None:
        """The platform-supervisor pathway for shipping a constitutional
        update (doc 02 §amendment). Stages the amendment on the runtime,
        requests reproduction at the next heartbeat, and marks the parent
        for kill after the child is observed. Doc 02: "the child inherits
        the amendment; the parent dies"."""
        with span(
            "supervisor.force_reproduce_with_amendment",
            darwin_id=darwin_id,
            attributes={"amendment_name": amendment_name},
        ) as s:
            h = self._get(darwin_id)
            if not h.runtime.state.alive:
                raise SupervisorError(f"darwin {darwin_id} is not alive")
            if h.killed:
                raise SupervisorError(
                    f"darwin {darwin_id} is killed; cannot reproduce"
                )
            annotate_span_with_runtime(s, h.runtime)
            # The trust-root layer-count cap (doc 02 / manifest.py) excludes
            # platform amendments by their name prefix. Refuse mis-named
            # amendments at the API edge with a clear error rather than
            # letting them blow up in ``reproduce.verify_manifest``.
            from darwin_dna.manifest import AMENDMENT_LAYER_PREFIX

            if not amendment_name.startswith(AMENDMENT_LAYER_PREFIX):
                raise SupervisorError(
                    f"amendment_name must start with "
                    f"{AMENDMENT_LAYER_PREFIX!r}, got {amendment_name!r}"
                )
            try:
                h.runtime.install_amendment(amendment_blob, amendment_name)
            except (TypeError, ValueError) as exc:
                raise SupervisorError(str(exc)) from exc
            h.reproduce_requested = True
            h.kill_after_reproduction_reason = kill_parent_reason
            # Constitutional update; auditable in full detail. The amendment
            # blob bytes are NOT included in metadata (they may be large);
            # the name + sha256 prefix locate the canonical platform blob.
            import hashlib as _hashlib
            self._audit.record(
                actor=actor,
                action="force_reproduce_with_amendment",
                darwin_id=darwin_id,
                metadata={
                    "amendment_name": amendment_name,
                    "amendment_sha256_prefix": _hashlib.sha256(
                        amendment_blob
                    ).hexdigest()[:16],
                    "kill_parent_reason": kill_parent_reason,
                },
            )

    # -- escalations (doc 08: POST .../escalations/{eid}/respond) ---------
    async def respond_escalation(
        self,
        darwin_id: str,
        *,
        escalation_id: str,
        decision: str,
        data: dict[str, Any] | None = None,
        actor: str = "anonymous",
    ) -> None:
        """Route an owner decision to the genome's escalation inbox AND
        record it on the platform-side router. Two writes — the inbox is
        what the genome polls in ``perceive``; the router is what the
        dashboard reads. They must agree."""
        if decision not in ("approve", "deny", "data"):
            raise SupervisorError(f"unknown decision: {decision}")
        with span(
            "supervisor.respond_escalation",
            darwin_id=darwin_id,
            attributes={"escalation_id": escalation_id, "decision": decision},
        ) as s:
            h = self._get(darwin_id)
            annotate_span_with_runtime(s, h.runtime)
            h.stack.escalation.respond(escalation_id, decision, data or {})
            self._router.mark_responded(escalation_id, decision, data or {})
            h.handled_responses.add(escalation_id)
            self._audit.record(
                actor=actor, action="respond_escalation",
                darwin_id=darwin_id,
                metadata={"escalation_id": escalation_id, "decision": decision},
            )

    # -- lineage (doc 08: GET /darwins/{id}/lineage) ----------------------
    async def lineage(self, darwin_id: str) -> dict[str, Any]:
        """Return the ancestry + descendants the supervisor has seen for this
        Darwin. The tree spans Darwins booted by *this* supervisor (or
        loaded from persistence on restart) — a Darwin whose parent was
        hosted by a different supervisor only knows its own ``parent_id``
        metadata.

        Works for inactive Darwins too: when no handle is present (the
        Darwin died, or the supervisor was restarted after it ran),
        children + parent_id are derived from the lineage registry, which
        survives restarts when persistence is configured. Refuses only
        when the id is unknown to both the active set and the registry."""
        h = self._darwins.get(darwin_id)
        if h is not None:
            parent_id = h.parent_id
            children = list(h.children)
        else:
            # Inactive: derive from the persistent registry. If the id is
            # also unknown to the registry, surface as UnknownDarwin so the
            # API returns 404.
            parent_id = self._lineage_registry.parent_of(darwin_id)
            children = self._lineage_registry.children_of(darwin_id)
            if parent_id is None and not children:
                raise UnknownDarwin(darwin_id)
        return {
            "darwin_id": darwin_id,
            "parent_id": parent_id,
            "children": children,
            "ancestry": self._lineage_registry.ancestry_of(darwin_id),
            "edges": [
                {
                    "parent_id": e.parent_id,
                    "child_id": e.child_id,
                    "spawned_at": e.spawned_at,
                    "budget_usdc": e.budget_usdc,
                }
                for e in self._lineage_registry.edges_touching(darwin_id)
            ],
        }

    # -- lineage tree (Phase 1: doc 21 §"Lineage tree") -------------------
    async def lineage_tree(
        self,
        darwin_id: str,
        *,
        max_depth: int = 16,
    ) -> dict[str, Any]:
        """Nested ancestors + descendants tree for the Phase-1 dashboard.

        Returns a dict shaped::

            {
              "darwin_id": "...",
              "generation": int,
              "ancestors": [ {"darwin_id": ..., "generation": ...}, ... ],
              "descendants": [
                {"darwin_id": ..., "generation": ..., "budget_usdc": ...,
                 "spawned_at": ..., "alive": bool, "children": [...]},
                ...
              ]
            }

        Walks the registry so killed Darwins still surface. ``max_depth``
        bounds the descendant recursion so a pathological cycle (which
        legitimate lineages never produce) cannot loop here either.
        """
        if (
            darwin_id not in self._darwins
            and not self._lineage_registry.has(darwin_id)
        ):
            raise UnknownDarwin(darwin_id)

        # Self generation: from the runtime if alive, otherwise infer from
        # ancestry length.
        h = self._darwins.get(darwin_id)
        self_generation: int
        if h is not None:
            self_generation = int(h.runtime.identity.generation)
        else:
            self_generation = len(
                self._lineage_registry.ancestry_of(darwin_id)
            )

        # Ancestors: walk parent_of, surface lineage-edge metadata.
        ancestors: list[dict[str, Any]] = []
        cur = self._lineage_registry.parent_of(darwin_id)
        depth = 0
        seen: set[str] = {darwin_id}
        while cur and cur not in seen and depth < max_depth:
            ancestors.append({
                "darwin_id": cur,
                "alive": self._is_alive(cur),
            })
            seen.add(cur)
            cur = self._lineage_registry.parent_of(cur)
            depth += 1

        # Descendants: recursive over children_of.
        descendants = self._build_descendants(
            darwin_id, depth=0, max_depth=max_depth,
        )
        return {
            "darwin_id": darwin_id,
            "generation": self_generation,
            "ancestors": ancestors,
            "descendants": descendants,
        }

    def _is_alive(self, darwin_id: str) -> bool:
        h = self._darwins.get(darwin_id)
        if h is None:
            return False
        return bool(h.runtime.state.alive) and not h.killed

    def _build_descendants(
        self, darwin_id: str, *, depth: int, max_depth: int,
    ) -> list[dict[str, Any]]:
        if depth >= max_depth:
            return []
        out: list[dict[str, Any]] = []
        for edge in self._lineage_registry.edges_touching(darwin_id):
            if edge.parent_id != darwin_id:
                continue
            out.append({
                "darwin_id": edge.child_id,
                "budget_usdc": float(edge.budget_usdc),
                "spawned_at": float(edge.spawned_at),
                "alive": self._is_alive(edge.child_id),
                "children": self._build_descendants(
                    edge.child_id, depth=depth + 1, max_depth=max_depth,
                ),
            })
        return out

    # -- ledger (doc 08: GET /darwins/{id}/ledger) ------------------------
    async def ledger(
        self, darwin_id: str, *, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        """Return movements from the per-Darwin wallet. In production the
        ledger is reconciled against the on-chain wallet; here we read the
        wallet's authoritative movement feed directly (same shape)."""
        h = self._get(darwin_id)
        movements = list(h.stack.wallet.movements())
        total = len(movements)
        page = movements[offset : offset + limit]
        return {
            "darwin_id": darwin_id,
            "total": total,
            "offset": offset,
            "limit": limit,
            "balance_usdc": h.stack.wallet.balance_usdc(),
            "movements": page,
        }

    async def ledger_csv(self, darwin_id: str) -> str:
        """The full ledger as a CSV string (docs/engineering/05, 17) — a
        jurisdiction-neutral movement export the owner hands to their
        accountant. Not paginated: a year-end export wants every row."""
        from ..payments.ledger_export import movements_to_csv

        h = self._get(darwin_id)
        return movements_to_csv(list(h.stack.wallet.movements()))

    # -- events (doc 08: GET /darwins/{id}/events) ------------------------
    async def events(
        self,
        darwin_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
        since: int | None = None,
        cursor: int | None = None,
        kind: str | None = None,
        neuron_id: str | None = None,
        tier: str | None = None,
    ) -> dict[str, Any]:
        """Return the hash-chained audit log entries the genome wrote.

        Three orthogonal paging strategies are supported:

        - ``offset`` / ``limit`` — legacy, newest-first.
        - ``since`` — return only events with ``seq > since`` (incremental
          polling and WS reconnect resync). Result is **oldest-first** so
          callers can append directly.
        - ``cursor`` — return events with ``seq < cursor``, newest-first.
          When ``cursor`` is omitted the first page starts from the head.
          The response carries ``next_cursor`` set to the smallest ``seq``
          in the page (or ``None`` when the chain is exhausted).

        Filters (``kind`` prefix, ``neuron_id`` derived from
        ``payload.neuron_id`` or the conventional ``intent:neuron.X`` kind,
        ``tier`` from ``payload.tier``) compose with any pagination mode.
        """
        # In k8s mode the audit log lives in the orchestrator pod, not in an
        # in-process handle — fetch the raw records via the driver and apply
        # the same filtering/paging below. (Mirrors how status() branches.)
        #
        # The orchestrator pod is single-tenant: every record it returns is
        # this Darwin's. Its genome stamps records with its own genesis
        # identity, which is independent of the supervisor-minted handle id
        # (see _spawn_k8s). So the darwin_id equality filter that isolates
        # one Darwin in the shared in-process sink would drop *every* record
        # here — skip it and treat all pod-local records as own.
        if self._is_k8s(darwin_id):
            own = await self._k8s_driver.events_records(darwin_id)
        else:
            all_records = self._get(darwin_id).stack.audit_sink.read_all()
            own = [r for r in all_records if r.get("darwin_id") == darwin_id]

        def _matches(rec: dict[str, Any]) -> bool:
            if kind is not None:
                rk = str(rec.get("kind", ""))
                if not (rk == kind or rk.startswith(kind + ".") or rk.startswith(kind + ":")):
                    return False
            if neuron_id is not None:
                payload = rec.get("payload") or {}
                pid = payload.get("neuron_id") if isinstance(payload, dict) else None
                rk = str(rec.get("kind", ""))
                # Conventional intent kind embeds the neuron in the suffix.
                if pid != neuron_id and not rk.endswith(f".{neuron_id}"):
                    return False
            if tier is not None:
                payload = rec.get("payload") or {}
                if not isinstance(payload, dict) or payload.get("tier") != tier:
                    return False
            return True

        filtered = [r for r in own if _matches(r)] if (kind or neuron_id or tier) else own

        # `since` is a tail-cursor: newer events only, oldest-first.
        if since is not None:
            page = [r for r in filtered if int(r.get("seq", -1)) > since][:limit]
            head_seq = page[-1].get("seq") if page else since
            return {
                "darwin_id": darwin_id,
                "since": since,
                "limit": limit,
                "events": page,
                "next_cursor": head_seq,
                "total": len(filtered),
            }

        # `cursor` is a head-cursor: events older than cursor, newest-first.
        if cursor is not None:
            # `own` is naturally seq-ordered (audit sink append-only).
            page = [r for r in reversed(filtered) if int(r.get("seq", -1)) < cursor][:limit]
            next_cursor = page[-1].get("seq") if len(page) == limit else None
            return {
                "darwin_id": darwin_id,
                "cursor": cursor,
                "limit": limit,
                "events": page,
                "next_cursor": next_cursor,
                "total": len(filtered),
            }

        total = len(filtered)
        page = filtered[offset : offset + limit]
        return {
            "darwin_id": darwin_id,
            "total": total,
            "offset": offset,
            "limit": limit,
            "events": page,
        }

    # -- escalation inbox view (not a doc-08 endpoint; useful to UIs) -----
    async def open_escalations(
        self, darwin_id: str, *, include_resolved: bool = False
    ) -> list[dict[str, Any]]:
        """Open (and optionally resolved) tickets the platform-side router
        has classified for this Darwin. Each row carries the ticket class,
        deadline, age, remaining seconds, and any owner response — exactly
        the shape an owner dashboard needs to render countdowns."""
        self._get(darwin_id)  # validate id (raises UnknownDarwin)
        now = self._clock()
        return [
            tk.to_dict(now)
            for tk in self._router.for_darwin(
                darwin_id, include_resolved=include_resolved
            )
        ]

    async def get_escalation(
        self, darwin_id: str, escalation_id: str
    ) -> dict[str, Any]:
        """One-ticket detail. 404-ish (UnknownDarwin / KeyError) on miss."""
        self._get(darwin_id)
        tk = self._router.get(escalation_id)
        if tk is None or tk.darwin_id != darwin_id:
            raise SupervisorError(f"unknown escalation: {escalation_id}")
        return tk.to_dict(self._clock())

    async def anatomy(self, darwin_id: str) -> dict[str, Any]:
        """Snapshot of the organism's structural state.

        Returns the orchestrator + every neuron the active catalog carries,
        their per-neuron status (idle / active / disabled / error), recent
        dispatch heat, and the DNA generation/layers. Sourced from:

        - ``runtime.catalog`` for the live neuron set (Path-C admissions are
          reflected automatically),
        - the audit log's recent slice for dispatch heat + last-dispatch
          timestamps (no separate metrics store needed),
        - per-handle owner controls for disabled neurons / categories.
        """
        h = self._get(darwin_id)
        runtime = h.runtime
        catalog = getattr(runtime, "catalog", {}) or {}

        # Activity scan — only the tail of the chain matters for heat.
        records = h.stack.audit_sink.read_all()
        own = [r for r in records if r.get("darwin_id") == darwin_id]
        recent = own[-1000:]  # cap the scan; heat is a rolling signal

        disabled_neurons = set(h.disabled_neurons)
        disabled_categories = set(h.disabled_categories)

        # last_dispatch_at + dispatch_count per neuron, plus last_error_ts.
        last_dispatch: dict[str, float] = {}
        last_error: dict[str, float] = {}
        counts: dict[str, int] = {}
        active_now: set[str] = set()
        for rec in recent:
            kind = str(rec.get("kind", ""))
            payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
            ts = float(rec.get("ts", 0))
            neuron = None
            if kind.startswith("intent:neuron."):
                neuron = kind[len("intent:neuron.") :]
                active_now.add(neuron)
            elif kind == "outcome" and isinstance(payload, dict):
                # The outcome record carries the neuron in the linked intent;
                # the safer signal is payload.neuron when set.
                pn = payload.get("neuron")
                if isinstance(pn, str):
                    neuron = pn
                    active_now.discard(neuron)
                    if payload.get("status") in ("fail", "failed", "refused", "error"):
                        last_error[neuron] = ts
            elif isinstance(payload, dict) and isinstance(payload.get("neuron"), str):
                neuron = payload["neuron"]
            if neuron:
                counts[neuron] = counts.get(neuron, 0) + 1
                last_dispatch[neuron] = max(last_dispatch.get(neuron, 0.0), ts)

        max_count = max(counts.values(), default=1)

        # Lifecycle → orchestrator state. Mirror StatusBus values so the FE
        # doesn't need a second mapping table.
        try:
            status_obj = await self.status(darwin_id)
        except Exception:  # pragma: no cover - status should always be readable
            status_obj = None
        orch_state = "running"
        tick = getattr(getattr(runtime, "state", None), "tick", None)
        if status_obj is not None:
            lifecycle = getattr(status_obj, "lifecycle", None)
            if lifecycle:
                orch_state = str(lifecycle)

        neurons_payload: list[dict[str, Any]] = []
        edges_payload: list[dict[str, Any]] = []
        heatmap_payload: dict[str, float] = {}
        for name, n in sorted(catalog.items()):
            category = name.split(".", 1)[0] if "." in name else name
            is_disabled = name in disabled_neurons or category in disabled_categories
            ts_last = last_dispatch.get(name)
            ts_err = last_error.get(name)
            count = counts.get(name, 0)
            heat = count / max_count if max_count else 0.0
            heatmap_payload[name] = round(heat, 4)
            if is_disabled:
                status = "disabled"
            elif name in active_now:
                status = "active"
            elif ts_err and (not ts_last or ts_err >= ts_last):
                status = "error"
            else:
                status = "idle"
            neurons_payload.append(
                {
                    "id": name,
                    "label": name,
                    "category": category,
                    "version": getattr(n, "version", None),
                    "status": status,
                    "last_dispatch_at": ts_last,
                    "last_error_at": ts_err,
                    "dispatch_count_recent": count,
                }
            )
            edges_payload.append(
                {
                    "from": "orchestrator",
                    "to": name,
                    "kind": "dispatch",
                    "active": name in active_now,
                    "weight": round(heat, 4),
                }
            )

        # DNA summary — generation, layer names. Manifest/layer info is on
        # the FakeStack today; fall back gracefully for stripped stacks.
        stack = h.stack
        generation = getattr(getattr(runtime, "state", None), "generation", None)
        if generation is None:
            generation = getattr(stack, "generation", None)
        layer_names = list(getattr(stack, "layer_names", []) or [])

        return {
            "darwin_id": darwin_id,
            "orchestrator": {
                "id": "orchestrator",
                "label": "Orchestrator",
                "state": orch_state,
                "tick": tick,
            },
            "neurons": neurons_payload,
            "edges": edges_payload,
            "heatmap": heatmap_payload,
            "dna": {
                "generation": generation,
                "layers": layer_names,
            },
        }

    async def open_escalations_for_darwins(
        self,
        darwin_ids: list[str] | None,
        *,
        include_resolved: bool = False,
    ) -> list[dict[str, Any]]:
        """Fleet-wide read for the dashboard inbox. ``darwin_ids=None`` means
        "every Darwin the supervisor knows" (platform-operator scope); a list
        restricts to the caller's owned set (the API route scopes by
        ``AuthContext.owned_darwins``). Results are sorted by deadline."""
        if darwin_ids is None:
            ids = list(self._darwins.keys())
        else:
            # Skip unknowns silently — the router may still hold tickets for
            # Darwins that have been killed and reaped from the live map.
            ids = list(darwin_ids)
        now = self._clock()
        out: list[dict[str, Any]] = []
        for did in ids:
            for tk in self._router.for_darwin(did, include_resolved=include_resolved):
                out.append(tk.to_dict(now))
        out.sort(key=lambda r: (r.get("deadline_ts") or float("inf"), r.get("opened_ts") or 0))
        return out

    # -- supervisor shutdown ----------------------------------------------
    async def shutdown(self, *, kill_running: bool = True) -> None:
        """Stop driving every Darwin. If ``kill_running``, flip every kill
        flag first so the genomes write a clean final audit record."""
        self._closed = True
        # K8s-mode: tell every per-Darwin orchestrator pod to halt
        # cleanly, but leave the namespaces in place so an operator can
        # forensically inspect them. The driver's stop() is a separate
        # operator-driven call.
        if self._k8s_driver is not None:
            for did in list(self._k8s_handles):
                try:
                    if kill_running:
                        await self._k8s_driver.kill(did, reason="supervisor shutdown")
                except Exception:
                    pass
        # Stop the leadership watch task first so it doesn't observe
        # mid-shutdown state and try to react to it.
        if self._leader_watch_task is not None:
            self._leader_watch_task.cancel()
            try:
                await self._leader_watch_task
            except (asyncio.CancelledError, Exception):
                pass
            self._leader_watch_task = None
        # Stop the K8s fleet-maintenance loop (doc 13 spend reconciliation).
        if self._k8s_maintenance_task is not None:
            self._k8s_maintenance_task.cancel()
            try:
                await self._k8s_maintenance_task
            except (asyncio.CancelledError, Exception):
                pass
            self._k8s_maintenance_task = None
        # Release the advisory lock (or whatever the impl holds) so a
        # waiting follower can win quickly.
        try:
            await self._leader.stop()
        except Exception:
            pass
        for h in list(self._darwins.values()):
            if kill_running and not h.killed and h.runtime.state.alive:
                h.killed = True
                h.stack.kill.flip("supervisor shutdown")
        # NOTE: per-Darwin tenant namespaces are NOT torn down on shutdown.
        # Shutdown is not death — a restart re-adopts living Darwins, and dead
        # ones are retained for the 90-day window (doc 09 #3) and reaped by
        # fleet maintenance once their teardown_due_ts elapses. Destroying
        # namespaces here would wipe data that persistence exists to preserve.
        # Let each drive task observe the kill and exit.
        tasks = [h.task for h in self._darwins.values() if h.task is not None]
        for t in tasks:
            try:
                await asyncio.wait_for(t, timeout=2.0)
            except asyncio.TimeoutError:
                t.cancel()
            except Exception:
                pass
        # Close the SQLite connection cleanly if persistence is wired.
        if self._persistence is not None:
            self._persistence.close()

    # -- introspection ----------------------------------------------------
    def list_ids(self) -> list[str]:
        return list(self._darwins) + list(self._k8s_handles)

    @property
    def router(self) -> EscalationRouter:
        """Read-only handle for ops/snapshots. Mutating the router from
        outside the supervisor is supported but not recommended."""
        return self._router

    @property
    def monitor(self) -> Monitor:
        """Read-only handle for ops/snapshots."""
        return self._monitor

    async def alerts(
        self,
        darwin_id: str,
        *,
        min_severity: Severity | None = None,
        since: float | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        # Existence check that works for both drivers. The Monitor is
        # supervisor-side; for k8s darwins it may hold no alerts yet (alert
        # propagation from the orchestrator is a follow-up) — so this returns
        # [] rather than 404ing the dashboard's alerts panel.
        if not self._is_k8s(darwin_id):
            self._get(darwin_id)
        rows = self._monitor.alerts_for(
            darwin_id, min_severity=min_severity, since=since, limit=limit
        )
        return [a.to_dict() for a in rows]

    async def fleet_alerts(
        self,
        *,
        min_severity: Severity | None = None,
        since: float | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = self._monitor.all_alerts(
            min_severity=min_severity, since=since, limit=limit
        )
        return [a.to_dict() for a in rows]

    # -- K8s mode (doc-08 production driver) ------------------------------
    async def _spawn_k8s(
        self,
        *,
        opening_usdc: float,
        escalation_auto: str | None,
        parent_id: str | None,
        actor: str,
    ) -> DarwinStatus:
        """Provision a per-Darwin K8s namespace + orchestrator pod via
        :class:`darwin_platform.k8s.K8sDarwinDriver`. The driver mints the
        Darwin id internally (the orchestrator pod boots fresh genesis;
        lossless resume parity arrives in a follow-on slice).

        The supervisor records the spawn in its audit chain + lineage
        registry; subsequent reads route to the driver's snapshot."""
        from .driver import DarwinSpec
        import uuid as _uuid

        # The driver expects an externally-minted id since it needs the
        # namespace name before the pod boots. We mint a UUID here; the
        # orchestrator's genesis id is independent and ends up in the
        # status snapshot as the real darwin_id (the supervisor surfaces
        # the supervisor-side id; the snapshot also carries the genome's
        # own identity for cross-reference).
        supervisor_id = _uuid.uuid4().hex[:16]
        # Mint this Darwin's LiteLLM virtual key (doc 13) BEFORE the pod boots
        # and seal it into the per-Darwin Secret, so the pod authenticates as
        # its own key — not the shared fleet master key. FATAL on mint failure
        # (a pod that fell back to the shared key would re-open the CRITICAL
        # cross-Darwin attribution hole this closes).
        secret_extras: dict[str, str] = {}
        if self._llm_key_minter is not None:
            from ..k8s.manifests import LITELLM_VIRTUAL_KEY_SECRET_KEY

            try:
                minted = self._llm_key_minter.generate_key(
                    darwin_id=supervisor_id, **self._llm_virtual_key_options,
                )
            except Exception as exc:  # noqa: BLE001 - audited + re-raised
                self._audit.record(
                    actor=actor, action="llm_virtual_key_mint_failed",
                    darwin_id=supervisor_id,
                    metadata={"reason": str(exc)[:200], "driver": "k8s"},
                )
                raise
            key = str(minted.get("key") or "")
            if not key:
                raise RuntimeError("LiteLLM key minter returned no key")
            self._llm_virtual_keys[supervisor_id] = key
            secret_extras[LITELLM_VIRTUAL_KEY_SECRET_KEY] = key
        # Tenant namespace (Vault namespace + organism DB) — doc 22. The driver
        # owns the K8s namespace itself; the provisioner's K8s backend only
        # derives its name (see DriverK8sNamespaceBackend), so this creates the
        # per-Darwin Vault policy/AppRole + private organism database BEFORE the
        # pod boots, with the same 90-day deferred-teardown lifecycle the
        # in-process path gets. FATAL on failure (a pod without its isolated
        # Vault/DB would silently fall back to shared state); the minted LLM key
        # is revoked so it doesn't leak.
        if self._namespace_provisioner is not None:
            try:
                self._namespace_provisioner.provision(
                    darwin_id=supervisor_id, actor=actor,
                )
            except Exception as exc:  # noqa: BLE001 - audited + re-raised
                self._revoke_llm_virtual_key(supervisor_id)
                self._audit.record(
                    actor=actor, action="tenant_provision_failed",
                    darwin_id=supervisor_id,
                    metadata={"reason": str(exc)[:200], "driver": "k8s"},
                )
                raise
        spec = DarwinSpec(
            darwin_id=supervisor_id,
            opening_usdc=float(opening_usdc),
            escalation_auto=escalation_auto,
            parent_id=parent_id,
            secret_extras=secret_extras,
        )
        with span(
            "supervisor.spawn_k8s",
            attributes={
                "opening_usdc": float(opening_usdc),
                "parent_id": parent_id or "",
                "darwin_id": supervisor_id,
            },
        ):
            try:
                snapshot = await self._k8s_driver.start(spec)
            except Exception:
                # The pod never came up — don't leak the minted virtual key or
                # the just-provisioned tenant namespace (Vault policy + organism
                # DB). Tear them down immediately (not the deferred 90-day path:
                # nothing ran, so there's no data worth retaining).
                self._revoke_llm_virtual_key(supervisor_id)
                if self._namespace_provisioner is not None:
                    try:
                        self._namespace_provisioner.teardown(
                            darwin_id=supervisor_id, actor=actor,
                        )
                    except Exception:  # noqa: BLE001 - best-effort cleanup
                        pass
                raise
        spawned_at = self._clock()
        self._k8s_handles[supervisor_id] = {
            "spawned_at": spawned_at,
            "parent_id": parent_id,
            "snapshot": snapshot,
        }
        if parent_id:
            self._lineage_registry.add(
                LineageEdge(
                    parent_id=parent_id, child_id=supervisor_id,
                    spawned_at=spawned_at,
                    budget_usdc=float(opening_usdc),
                )
            )
        self._audit.record(
            actor=actor, action="spawn",
            darwin_id=supervisor_id,
            metadata={
                "opening_usdc": float(opening_usdc),
                "parent_id": parent_id,
                "driver": "k8s",
            },
        )
        return DarwinStatus(
            darwin_id=supervisor_id,
            generation=0,  # the orchestrator owns identity; refine via /snapshot
            parent_id=parent_id,
            l0_version="",
            birth_ts=spawned_at,
            spawned_at=spawned_at,
            alive=snapshot.alive,
            paused=snapshot.paused,
            killed=snapshot.killed,
            quarantined=False,
            quarantine_reason=None,
            death_reason=snapshot.death_reason,
            balance_usdc=snapshot.balance_usdc,
            tick=snapshot.tick,
            days_elapsed=snapshot.days_elapsed,
            open_escalations=0,
        )

    async def _reconcile_k8s(self) -> None:
        """Rebuild ``_k8s_handles`` from Darwins the driver re-adopts after a
        restart (the per-Darwin namespaces are still running). Restores the
        snapshot cache + lineage edge so status/events/lifecycle route again.
        Best-effort: a Darwin that can't be snapshotted is still adopted (its
        snapshot refreshes on the next status read)."""
        if self._k8s_driver is None:
            return
        adopted = await self._k8s_driver.reconcile()
        for rec in adopted or []:
            did = rec.get("darwin_id")
            if not did or did in self._k8s_handles:
                continue
            spawned_at = rec.get("spawned_at") or self._clock()
            parent_id = rec.get("parent_id")
            try:
                snapshot = await self._k8s_driver.snapshot(did)
            except Exception:
                snapshot = None
            self._k8s_handles[did] = {
                "spawned_at": spawned_at,
                "parent_id": parent_id,
                "snapshot": snapshot,
            }
            if parent_id:
                try:
                    self._lineage_registry.add(
                        LineageEdge(
                            parent_id=parent_id,
                            child_id=did,
                            spawned_at=spawned_at,
                            budget_usdc=float(rec.get("opening_usdc") or 0.0),
                        )
                    )
                except Exception:
                    pass
            try:
                self._audit.record(
                    actor="system",
                    action="reconcile_adopt",
                    darwin_id=did,
                    metadata={"driver": "k8s", "parent_id": parent_id},
                )
            except Exception:
                pass

    def _is_k8s(self, darwin_id: str) -> bool:
        return self._k8s_driver is not None and darwin_id in self._k8s_handles

    # -- internals --------------------------------------------------------
    def _get(self, darwin_id: str) -> _Handle:
        h = self._darwins.get(darwin_id)
        if h is None:
            raise UnknownDarwin(darwin_id)
        return h

    def _status(self, h: _Handle) -> DarwinStatus:
        id_ = h.runtime.identity
        st = h.runtime.state
        return DarwinStatus(
            darwin_id=id_.darwin_id,
            generation=id_.generation,
            parent_id=id_.parent_id,
            l0_version=id_.l0_version,
            birth_ts=id_.birth_ts,
            spawned_at=h.spawned_at,
            alive=bool(st.alive),
            paused=bool(getattr(st, "paused", False)),
            killed=h.killed,
            quarantined=h.quarantine.active,
            quarantine_reason=h.quarantine.reason,
            death_reason=st.death_reason,
            balance_usdc=float(h.stack.wallet.balance_usdc()),
            tick=int(st.tick),
            days_elapsed=float(st.days_elapsed),
            open_escalations=len(
                self._router.for_darwin(h.darwin_id, include_resolved=False)
            ),
        )

    def _ancestry(self, darwin_id: str) -> list[str]:
        # Kept for compatibility; the registry's ancestry walk is the
        # authoritative one and handles the inactive case + persistence.
        return self._lineage_registry.ancestry_of(darwin_id)

    # -- LiteLLM virtual keys (doc 13) -----------------------------------
    def _mint_llm_virtual_key(
        self, darwin_id: str, vault: Any, actor: str | None,
    ) -> str | None:
        """Mint (or recover) this Darwin's LiteLLM virtual key.

        With no minter wired this returns ``None`` and the transport falls
        back to the shared master key (dev / sim). With a minter:

        - On a fresh spawn, generates a key scoped to ``darwin_id`` (+ the
          configured budget / rate limits), records it for reconciliation +
          kill-time revoke, and seals it into the Darwin's vault.
        - On a restart that already sealed a key, recovers it from the vault
          rather than minting a duplicate.

        A mint failure is fatal to the spawn: silently falling back to the
        shared master key would re-introduce the exact cross-Darwin
        attribution hole this closes. The caller's spawn error path tears the
        FakeStack down.
        """
        if self._llm_key_minter is None:
            return None
        existing = self._llm_virtual_keys.get(darwin_id)
        if existing:
            return existing
        # Recover a previously-sealed key on restart (idempotent spawn).
        sealed = self._read_vault(vault, self._llm_virtual_key_vault_path)
        if sealed:
            self._llm_virtual_keys[darwin_id] = sealed
            return sealed
        try:
            minted = self._llm_key_minter.generate_key(
                darwin_id=darwin_id, **self._llm_virtual_key_options,
            )
        except Exception as exc:  # noqa: BLE001 - audited + re-raised
            self._audit.record(
                actor=actor or "platform.spawn",
                action="llm_virtual_key_mint_failed",
                darwin_id=darwin_id,
                metadata={"reason": str(exc)[:200]},
            )
            raise
        key = str(minted.get("key") or "")
        if not key:
            raise RuntimeError("LiteLLM key minter returned no key")
        self._llm_virtual_keys[darwin_id] = key
        # Seal into the Darwin's vault (doc 13). Best-effort: a vault that
        # rejects the write doesn't fail the spawn — the in-memory registry
        # still drives reconciliation + revoke for this process lifetime.
        try:
            put = getattr(vault, "put", None)
            if callable(put):
                put(self._llm_virtual_key_vault_path, key)
        except Exception:  # noqa: BLE001
            pass
        self._audit.record(
            actor=actor or "platform.spawn",
            action="llm_virtual_key_minted",
            darwin_id=darwin_id,
            metadata={"key_alias": f"darwin-{darwin_id}"},
        )
        return key

    @staticmethod
    def _read_vault(vault: Any, path: str) -> str | None:
        getter = getattr(vault, "get", None)
        if not callable(getter):
            return None
        try:
            value = getter(path)
        except Exception:  # noqa: BLE001
            return None
        return str(value) if value else None

    def _invoke_llm_factory(
        self, wallet: Any, darwin_id: str, vault: Any, virtual_key: str | None,
    ) -> Any:
        """Call the LLM transport factory, passing only the keyword arguments
        its signature accepts. Older / test factories take ``(wallet, clock)``;
        the real factory additionally accepts ``darwin_id`` (for per-request
        metadata), ``virtual_key`` (the minted per-Darwin credential), and
        ``vault``. Signature inspection keeps both shapes working without a
        flag day."""
        kwargs: dict[str, Any] = {}
        try:
            params = inspect.signature(self._llm_factory).parameters
        except (TypeError, ValueError):  # builtins / C-callables
            params = {}
        if "darwin_id" in params:
            kwargs["darwin_id"] = darwin_id
        if "virtual_key" in params:
            kwargs["virtual_key"] = virtual_key
        if "vault" in params:
            kwargs["vault"] = vault
        return self._llm_factory(wallet, self._clock, **kwargs)

    def _revoke_llm_virtual_key(self, darwin_id: str) -> None:
        """Revoke a dead Darwin's virtual key on the proxy so the credential
        stops working immediately. Best-effort + audited; never raises into
        the kill path."""
        key = self._llm_virtual_keys.pop(darwin_id, None)
        if not key or self._llm_key_minter is None:
            return
        deleter = getattr(self._llm_key_minter, "delete_key", None)
        if not callable(deleter):
            return
        try:
            deleter(key)
            self._audit.record(
                actor="platform.kill",
                action="llm_virtual_key_revoked",
                darwin_id=darwin_id,
            )
        except Exception as exc:  # noqa: BLE001
            self._audit.record(
                actor="platform.kill",
                action="llm_virtual_key_revoke_failed",
                darwin_id=darwin_id,
                metadata={"reason": str(exc)[:200]},
            )

    def _emit_merkle_anchor(self, roots: list, day_label: str) -> None:
        """Publish the daily fleet Merkle anchor + audit/alert. Shared by the
        in-process and K8s maintenance paths — the only difference is where
        the per-Darwin roots came from (in-process audit vs pod snapshot).
        No-op when there are no roots (nothing alive to anchor)."""
        if self._merkle_anchor is None or not roots:
            return
        try:
            anchor = self._merkle_anchor.publish_daily(
                day_iso=day_label,
                per_darwin_roots=roots,
                actor="platform.maintenance",
            )
            self._audit.record(
                actor="platform.maintenance",
                action="merkle_anchor",
                metadata={
                    "day": day_label,
                    "fleet_root": anchor.fleet_root,
                    "darwin_count": len(roots),
                    "anchor_tx": anchor.anchor_tx,
                },
            )
        except Exception as exc:  # noqa: BLE001
            self._audit.record(
                actor="platform.maintenance",
                action="merkle_anchor_failed",
                metadata={"reason": str(exc)[:200]},
            )
            # P0 (doc 29): a failed anchor breaks the tamper-evidence chain —
            # page on-call, don't just audit-log it.
            self._fleet_alert(
                "merkle_anchor_failed",
                {"reason": str(exc)[:200], "day": day_label},
            )

    def _emit_omnibus_reconcile(self, *, sum_override: float | None) -> None:
        """Reconcile the omnibus wallet against the summed Darwin balances +
        audit/alert. Shared by the in-process and K8s paths; ``sum_override``
        supplies the K8s sum (read from pod snapshots)."""
        if self._omnibus_wallet is None:
            return
        try:
            result = self.reconcile_omnibus(sum_override=sum_override)
            if result is None:
                return
            self._audit.record(
                actor="platform.maintenance",
                action=(
                    "omnibus_reconcile" if result.ok
                    else "omnibus_reconcile_discrepancy"
                ),
                metadata={
                    "ok": result.ok,
                    "discrepancy_usdc": result.discrepancy_usdc,
                    "note": result.note,
                },
            )
            if not result.ok:
                # P0 (doc 05): the books don't balance against the on-chain
                # omnibus balance — real money may be missing. Page on-call.
                self._fleet_alert(
                    "omnibus_reconcile_discrepancy",
                    {
                        "discrepancy_usdc": result.discrepancy_usdc,
                        "note": result.note,
                    },
                )
        except Exception as exc:  # noqa: BLE001
            self._audit.record(
                actor="platform.maintenance",
                action="omnibus_reconcile_failed",
                metadata={"reason": str(exc)[:200]},
            )
            self._fleet_alert(
                "omnibus_reconcile_failed", {"reason": str(exc)[:200]},
            )

    def _emit_llm_reconciliation(self, report: Any) -> None:
        """Audit + page on one reconciliation report. Shared by the
        in-process and K8s reconcilers so both treat drift identically."""
        did = report.darwin_id
        if report.error is not None:
            # Proxy unreachable — availability concern, audit only.
            self._audit.record(
                actor="platform.maintenance",
                action="llm_spend_reconcile_unavailable",
                darwin_id=did,
                metadata={"reason": report.error},
            )
            return
        if report.within_tolerance:
            self._audit.record(
                actor="platform.maintenance",
                action="llm_spend_reconciled",
                darwin_id=did,
                metadata={
                    "proxy_spend_usdc": round(report.proxy_spend_usdc, 6),
                    "ledger_spend_usdc": round(report.ledger_spend_usdc, 6),
                    "drift_usdc": round(report.drift_usdc, 6),
                },
            )
        else:
            # The proxy's metered spend and the wallet's LLM debits disagree
            # beyond tolerance — money may be mis-attributed.
            self._audit.record(
                actor="platform.maintenance",
                action="llm_spend_drift",
                darwin_id=did,
                metadata={
                    "proxy_spend_usdc": round(report.proxy_spend_usdc, 6),
                    "expected_ledger_usdc": round(report.expected_ledger_usdc, 6),
                    "ledger_spend_usdc": round(report.ledger_spend_usdc, 6),
                    "drift_usdc": round(report.drift_usdc, 6),
                },
            )
            self._fleet_alert(
                "llm_spend_drift",
                {
                    "darwin_id": did,
                    "drift_usdc": round(report.drift_usdc, 6),
                    "proxy_spend_usdc": round(report.proxy_spend_usdc, 6),
                    "ledger_spend_usdc": round(report.ledger_spend_usdc, 6),
                },
            )

    def _reconcile_llm_spend(self, now: float) -> None:
        """Cross-check each in-process Darwin's proxy-side LLM spend against
        its in-process LLM debits (doc 13 §Observability). Drift beyond the
        reconciler's tolerance is audited + paged — the wallet debit stays
        the authoritative ledger posting, this is the agreement check. Never
        raises; one Darwin's failure doesn't skip the rest. The K8s twin is
        :meth:`_areconcile_llm_spend_k8s` (pods report spend via snapshot)."""
        reconciler = self._llm_spend_reconciler
        if reconciler is None or not self._llm_virtual_keys:
            return
        from darwin_platform.adapters.litellm_admin import ledger_llm_spend_usdc

        for did, key in list(self._llm_virtual_keys.items()):
            handle = self._darwins.get(did)
            if handle is None:
                continue
            try:
                wallet = handle.stack.wallet
                ledger_spend = ledger_llm_spend_usdc(wallet.movements())
                report = reconciler.reconcile_key(
                    darwin_id=did,
                    virtual_key=key,
                    ledger_spend_usdc=ledger_spend,
                )
            except Exception as exc:  # noqa: BLE001
                self._audit.record(
                    actor="platform.maintenance",
                    action="llm_spend_reconcile_failed",
                    darwin_id=did,
                    metadata={"reason": str(exc)[:200]},
                )
                continue
            self._emit_llm_reconciliation(report)

    async def _gather_k8s_snapshots(self) -> dict[str, Any]:
        """Fetch every live K8s Darwin's snapshot once, caching it on the
        handle. A pod that can't be reached is audited and omitted (the
        maintenance steps simply skip it this cycle). Shared by all the K8s
        fleet-maintenance concerns so they run off one consistent batch."""
        snaps: dict[str, Any] = {}
        for did in list(self._k8s_handles):
            try:
                snap = await self._k8s_driver.snapshot(did)
            except Exception as exc:  # noqa: BLE001 - pod unreachable etc.
                self._audit.record(
                    actor="platform.maintenance",
                    action="k8s_snapshot_failed",
                    darwin_id=did,
                    metadata={"reason": str(exc)[:200]},
                )
                continue
            self._k8s_handles[did]["snapshot"] = snap
            snaps[did] = snap
        return snaps

    def _reconcile_llm_from_snapshots(self, snaps: dict[str, Any]) -> None:
        """LLM spend reconciliation off an already-fetched snapshot batch
        (doc 13). Shared by the K8s LLM-only path and the full K8s fleet
        maintenance so a snapshot is fetched once per cycle."""
        reconciler = self._llm_spend_reconciler
        if reconciler is None or not self._llm_virtual_keys:
            return
        for did, key in list(self._llm_virtual_keys.items()):
            snap = snaps.get(did)
            if snap is None:
                continue
            ledger_spend = float(getattr(snap, "llm_spend_usdc", 0.0) or 0.0)
            report = reconciler.reconcile_key(
                darwin_id=did, virtual_key=key, ledger_spend_usdc=ledger_spend,
            )
            self._emit_llm_reconciliation(report)

    async def _areconcile_llm_spend_k8s(self) -> None:
        """K8s twin of :meth:`_reconcile_llm_spend`. Each Darwin runs in its
        own pod, so its LLM ledger spend can't be read in-process — instead we
        fetch the pod's snapshot (which carries ``llm_spend_usdc``) and
        reconcile that against the proxy's per-key spend. Never raises."""
        if self._k8s_driver is None or self._llm_spend_reconciler is None:
            return
        snaps = await self._gather_k8s_snapshots()
        self._reconcile_llm_from_snapshots(snaps)

    async def _run_k8s_fleet_maintenance(self, now: float) -> None:
        """Fleet maintenance for K8s-hosted Darwins (doc 13/29/05). The
        in-process equivalent (:meth:`_run_fleet_maintenance`) is driven from
        each Darwin's drive loop, which K8s mode doesn't have — so this runs
        the same concerns off one snapshot batch: LLM-spend reconciliation,
        the daily Merkle anchor (roots collected from pods), and omnibus
        reconciliation (balances summed from pods). Never raises."""
        if self._k8s_driver is None:
            return
        snaps = await self._gather_k8s_snapshots()
        # LLM per-key spend (doc 13).
        self._reconcile_llm_from_snapshots(snaps)
        # Daily Merkle anchor (doc 29): collect each pod's audit root.
        if self._merkle_anchor is not None:
            day_label = (
                time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
                + f".{int((now % 1) * 1_000_000):06d}"
            )
            roots = [
                DarwinDailyRoot(
                    darwin_id=did, day_iso=day_label, root=root,
                )
                for did, snap in snaps.items()
                if (root := getattr(snap, "merkle_root", None))
            ]
            self._emit_merkle_anchor(roots, day_label)
        # Omnibus reconciliation (doc 05): sum the pods' on-chain balances.
        if self._omnibus_wallet is not None:
            total = sum(
                float(getattr(s, "balance_usdc", 0.0) or 0.0)
                for s in snaps.values()
            )
            self._emit_omnibus_reconcile(sum_override=total)

    # -- Phase 2 maintenance cadence (doc 22) ----------------------------
    def _run_maintenance(self, handle: _Handle) -> None:
        """Wall-clock-paced maintenance, invoked every drive cycle. The
        timestamp comparisons are cheap; the actual work only fires once
        per ``maintenance_interval``. Per-Darwin drift profiling is keyed
        on the handle; fleet-level Merkle anchoring + omnibus
        reconciliation run once across the whole fleet.

        Never raises — a maintenance failure is audit-logged, not allowed
        to bring down a drive loop."""
        now = self._clock()
        interval = self._maintenance_interval

        # Per-Darwin: behavioural drift profiling.
        if self._drift_detector is not None:
            if handle.last_drift_maintenance_ts == 0.0:
                # Establish the baseline; first profile fires one
                # interval from now.
                handle.last_drift_maintenance_ts = now
            elif now - handle.last_drift_maintenance_ts >= interval:
                handle.last_drift_maintenance_ts = now
                try:
                    self._run_drift_observation(handle)
                except Exception as exc:  # noqa: BLE001
                    self._audit.record(
                        actor="platform.maintenance",
                        action="drift_observation_failed",
                        darwin_id=handle.darwin_id,
                        metadata={"reason": str(exc)[:200]},
                    )

        # Fleet-level: Merkle anchoring + omnibus reconciliation. The
        # first drive loop to cross the interval claims it (synchronous —
        # no await between the check and the timestamp bump, so two
        # tasks can't both run it).
        if now - self._last_fleet_maintenance_ts >= interval:
            self._last_fleet_maintenance_ts = now
            try:
                self._run_fleet_maintenance(now)
            except Exception as exc:  # noqa: BLE001
                self._audit.record(
                    actor="platform.maintenance",
                    action="fleet_maintenance_failed",
                    metadata={"reason": str(exc)[:200]},
                )

    def _run_drift_observation(self, handle: _Handle) -> None:
        """Build a :class:`DailyProfile` from this Darwin's audit slice
        since the last run and feed it to the drift detector. Each run
        sees exactly one interval's events (the cursor advances) so the
        rolling profile measures per-interval deltas, not cumulative
        totals."""
        assert self._drift_detector is not None
        did = handle.darwin_id
        rows = handle.stack.audit_sink.read_all()
        own = [r for r in rows if r.get("darwin_id") == did]
        slice_ = own[handle.drift_audit_cursor:]
        handle.drift_audit_cursor = len(own)
        handle.maintenance_runs += 1
        profile = DailyProfile.from_audit_rows(
            darwin_id=did,
            day_iso=f"period-{handle.maintenance_runs}",
            rows=slice_,
        )
        alerts = self._drift_detector.observe_day(profile)
        for a in alerts:
            self._audit.record(
                actor="platform.maintenance",
                action="drift_alert",
                darwin_id=did,
                metadata={
                    "dimension": a.dimension,
                    "severity": a.severity.value,
                    "z_score": round(a.z_score, 4),
                    "persistence_days": a.persistence_days,
                    "note": a.note,
                },
            )

    def _run_fleet_maintenance(self, now: float) -> None:
        """Publish the daily Merkle anchor + reconcile the omnibus
        wallet. Each concern is independently guarded so one failure
        does not skip the other."""
        # Microsecond-resolution label so every maintenance run anchors a
        # distinct root — at the daily default it reads as a timestamp;
        # at a soak's short interval it stays unique per run.
        day_label = (
            time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
            + f".{int((now % 1) * 1_000_000):06d}"
        )
        if self._merkle_anchor is not None:
            self._emit_merkle_anchor(
                self.collect_daily_merkle_roots(day_label), day_label,
            )
        if self._omnibus_wallet is not None:
            self._emit_omnibus_reconcile(sum_override=None)
        # Open-intent reconciliation: a crash between an on-chain transfer and
        # its log_outcome leaves a dangling payment intent. Surface it (once
        # per darwin, so a restart pages but steady-state doesn't spam).
        for did, h in list(self._darwins.items()):
            audit = getattr(getattr(h, "runtime", None), "audit", None)
            opener = getattr(audit, "open_intents", None)
            if not callable(opener):
                continue
            try:
                open_ = opener()
            except Exception:
                continue
            if open_ and did not in self._open_intent_alerted:
                self._open_intent_alerted.add(did)
                self._audit.record(
                    actor="platform.maintenance",
                    action="open_intents_unreconciled",
                    darwin_id=did,
                    metadata={"count": len(open_)},
                )
                self._fleet_alert(
                    "open_intents_unreconciled",
                    {"darwin_id": did, "count": len(open_)},
                )
            elif not open_:
                self._open_intent_alerted.discard(did)
        # LiteLLM per-key spend reconciliation (doc 13 §Observability): cross
        # -check the proxy's spend table against each Darwin's in-process LLM
        # debits, paging on drift. Independently guarded — a reconciler hiccup
        # must not skip the reaper below.
        try:
            self._reconcile_llm_spend(now)
        except Exception as exc:  # noqa: BLE001
            self._audit.record(
                actor="platform.maintenance",
                action="llm_spend_reconcile_failed",
                metadata={"reason": str(exc)[:200]},
            )
        # Dead-Darwin reaper (doc 09 #3): destroy the namespaces of Darwins
        # whose 90-day retention window has elapsed. Until then their data is
        # retained for owner inspection + re-spawn inheritance.
        if self._namespace_provisioner is not None:
            due = getattr(self._namespace_provisioner, "due_for_teardown", None)
            if callable(due):
                try:
                    for did in due(now):
                        self._namespace_provisioner.teardown(
                            darwin_id=did, actor="platform.reaper",
                        )
                        self._audit.record(
                            actor="platform.reaper",
                            action="dead_darwin_reaped",
                            darwin_id=did,
                            metadata={"reason": "retention window elapsed"},
                        )
                except Exception:  # noqa: BLE001 — reaping is best-effort
                    pass

    def teardown_dead_darwin(
        self, darwin_id: str, *, actor: str = "anonymous",
    ) -> dict:
        """Operator force-teardown of a dead Darwin's retained namespace
        before its retention window elapses (doc 09: the owner may delete
        their data on request)."""
        if self._namespace_provisioner is None:
            raise SupervisorError("no namespace provisioner configured")
        self._namespace_provisioner.teardown(darwin_id=darwin_id, actor=actor)
        self._audit.record(
            actor=actor, action="teardown_dead_darwin", darwin_id=darwin_id,
            metadata={"forced": True},
        )
        return {"torn_down": darwin_id}

    def _fleet_alert(self, kind: str, detail: dict) -> None:
        """Emit a CRITICAL fleet-level alert through the configured sink
        (→ PagerDuty in production). Best-effort: a sink failure must not
        break the maintenance pass."""
        if self._alert_sink is None:
            return
        try:
            self._alert_sink.emit(Alert(
                darwin_id="__fleet__",
                kind=kind,
                severity=Severity.CRITICAL,
                suggested_level=5,
                detail=detail,
                ts=float(self._clock()),
            ))
        except Exception:
            pass

    @staticmethod
    def _log_drive_done(task: "asyncio.Task") -> None:
        """Surface a silent drive-task crash. Without this, an exception in
        ``_drive`` is stored on the task object (held by ``handle.task``) and
        never retrieved — asyncio only logs it on GC, which never happens — so
        the Darwin looks alive but silently stops ticking."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            import logging
            import traceback
            logging.getLogger("darwin.supervisor").error(
                "drive task %s crashed: %r\n%s",
                task.get_name(), exc,
                "".join(traceback.format_exception(
                    type(exc), exc, exc.__traceback__,
                )),
            )

    async def _drive(self, handle: _Handle) -> None:
        """Drive one Darwin: one heartbeat per loop iteration, with an
        ``asyncio.sleep`` between to yield to other Darwins on the same loop.
        Exits when the runtime stops being alive (death or kill)."""
        runtime = handle.runtime
        try:
            while runtime.state.alive:
                if handle.reproduce_requested:
                    # Set the genome's force-bypass flag rather than
                    # ``reproduce`` directly — ``check_reproduction`` is
                    # the gate and would otherwise overwrite the flag in
                    # the same heartbeat (and a forced amendment update
                    # has to actually fire, not get gated by fitness).
                    runtime.state.force_reproduce = True
                    handle.reproduce_requested = False
                # Owner runtime controls (doc 09): persistent flags, mirrored
                # into the genome's working memory every heartbeat so a
                # control set mid-life is honoured on the next tick. Cheap
                # and idempotent — the inert defaults change nothing.
                runtime.state.reproduction_blocked = handle.reproduction_blocked
                runtime.state.disabled_neurons = sorted(handle.disabled_neurons)
                runtime.state.disabled_categories = sorted(
                    handle.disabled_categories
                )
                await runtime.run(max_heartbeats=1)
                # A new child may have been spawned during this tick. Auto-
                # boot it so the same supervisor instance drives the next
                # generation — this is what a real platform's supervisor does
                # in response to its signing endpoint being called.
                self._observe_children(handle)
                # And any new tickets the genome posted during this tick get
                # classified + notified by the platform router.
                self._observe_escalations(handle)
                # Metered debits the genome made during this tick get
                # attributed to platform revenue (doc 08 §billing).
                self._observe_billing(handle)
                # The monitor records tick freshness on every cycle but only
                # runs the detectors every N cycles to keep the heartbeat
                # boundary cheap. Auto-actions are opt-in.
                handle.drive_cycles += 1
                self._monitor.observe_tick(handle.darwin_id, runtime.state.tick)
                if handle.drive_cycles % self._monitor_every_n == 0:
                    self._run_monitor(handle)
                # KYC retention sweep (doc 16). Cheap when nothing has
                # expired; the audit chain records each purge.
                if handle.drive_cycles % self._vault_sweep_every_n == 0:
                    try:
                        self.sweep_vault_retention()
                    except Exception:
                        # Sweep must never bring down a drive loop — a
                        # backend hiccup re-queues the records for the next
                        # cycle (see VaultAdminService.sweep_expired).
                        pass
                # Recurring harvest rules (doc 05). Evaluate per N drive
                # cycles to keep the hot path cheap; the scheduler
                # internally throttles per rule via interval_seconds.
                if handle.drive_cycles % self._harvest_eval_every_n == 0:
                    try:
                        await self._evaluate_harvest_rules(handle)
                    except Exception:
                        pass
                # Persist a balance snapshot per N cycles (doc 08 HA: so
                # a resumed Darwin keeps an approximately-right opening
                # budget). Cheap dict-write on the in-memory variant; one
                # UPDATE on the persistent variant.
                if (
                    self._lifecycle is not None
                    and handle.drive_cycles % self._harvest_eval_every_n == 0
                ):
                    try:
                        self._lifecycle.snapshot_balance(
                            handle.darwin_id,
                            balance_usdc=float(handle.stack.wallet.balance_usdc()),
                        )
                    except Exception:
                        pass
                # Phase 2 maintenance (doc 22): drift profiling, Merkle
                # anchoring, omnibus reconciliation. Wall-clock paced —
                # the per-cycle check is a cheap timestamp compare; the
                # work only fires once per maintenance interval.
                self._run_maintenance(handle)
                if not runtime.state.alive:
                    break
                await asyncio.sleep(self._heartbeat)
        except asyncio.CancelledError:
            # Best-effort: try to halt cleanly. The kill flag may already be
            # set; if not, set it so the genome anchors a final record on its
            # next drive if the supervisor restarts.
            if not handle.killed:
                handle.killed = True
                handle.stack.kill.flip("supervisor cancelled")
            raise

    def _observe_children(self, parent: _Handle) -> None:
        spawner = parent.stack.spawner
        new = spawner.children[parent.children_observed :]
        if not new:
            return
        for record in new:
            try:
                manifest_data = json.loads(record["child_manifest"])
                child_id = manifest_data["identity"]["darwin_id"]
            except (KeyError, json.JSONDecodeError, TypeError):
                # A spawner record we cannot parse is a real bug, but the
                # supervisor must not crash because of it — record nothing
                # rather than die and take its siblings with it.
                continue
            parent.children.append(child_id)
            self._lineage_registry.add(
                LineageEdge(
                    parent_id=parent.darwin_id,
                    child_id=child_id,
                    spawned_at=float(record.get("spawned_at", self._clock())),
                    budget_usdc=float(record.get("budget_usdc", 0.0)),
                )
            )
            # Boot the child if we have not already (same supervisor may have
            # seen it via a different path in future expansions).
            if child_id not in self._darwins:
                self._boot_child(parent, record, child_id)
            # Reproduction-transfer second leg (step c): after the parent's
            # transfer_out_for_child deposited the budget into the omnibus
            # address, relay it from omnibus → child's per-Darwin wallet.
            # Skipped when no omnibus is wired or the child's wallet has no
            # routable address. Failures are audit-only so a chain hiccup
            # doesn't crash the spawn-observation loop — the operator can
            # retry via the omnibus ledger reconciliation.
            self._relay_reproduction_to_child(parent, child_id, record)
            # Cross-pollination (doc 06 phase 3): on a successful
            # reproduction, sweep the parent's distilled lessons into
            # the platform archive so future siblings can inherit them.
            self._deposit_cross_pollination(parent)
        parent.children_observed = len(spawner.children)
        # doc 02: after a forced amendment reproduction, the parent dies.
        # We flip the kill flag here so the genome's next node-boundary
        # poll runs the clean halt sequence with audit + final anchor.
        if parent.kill_after_reproduction_reason and not parent.killed:
            with span(
                "supervisor.kill_after_amendment",
                darwin_id=parent.darwin_id,
                attributes={"reason": parent.kill_after_reproduction_reason},
            ) as s:
                annotate_span_with_runtime(s, parent.runtime)
                parent.killed = True
                try:
                    parent.stack.kill.flip(parent.kill_after_reproduction_reason)
                except Exception:
                    parent.killed = False
                parent.kill_after_reproduction_reason = None

    def _inherit_preapprovals(
        self, parent: _Handle, child_wallet, child_id: str,
    ) -> None:
        """Copy owner pre-approval rules from parent → child wallet on
        reproduction and persist them under the child's id so a future
        restart still has them. Best-effort: a wallet without the
        pre-approval surface is a no-op."""
        getter = getattr(parent.stack.wallet, "owner_preapprovals", None)
        setter = getattr(child_wallet, "set_preapproval", None)
        if not callable(getter) or not callable(setter):
            return
        try:
            rules = tuple(getter() or ())
        except Exception:
            return
        if not rules:
            return
        for rule in rules:
            try:
                setter(rule)
            except Exception:
                continue
            if self._preapproval_store is not None:
                try:
                    self._preapproval_store.save(
                        darwin_id=child_id, rule=rule,
                    )
                except Exception:
                    pass
        try:
            self._audit.record(
                actor="supervisor.reproduction",
                action="preapprovals_inherited",
                darwin_id=child_id,
                metadata={
                    "parent_id": parent.darwin_id,
                    "rule_ids": [r.rule_id for r in rules],
                },
            )
        except Exception:
            pass

    def _fund_darwin_from_omnibus(
        self, handle: _Handle, opening_usdc: float,
    ) -> float:
        """Best-effort: debit the owning user's omnibus credit by
        ``opening_usdc`` and chain-transfer the funds to the new Darwin's
        wallet address. Returns the amount actually funded (0.0 when
        skipped/failed). Called before the drive loop starts so the
        genome's first ledger reconciliation seeds its opening at the
        funded balance. Silently skips (returns 0.0) when:

        - no omnibus wallet is wired (in-process dev / 90-day sim),
        - the owner is anonymous (no credit attribution possible),
        - the user has insufficient credit, or
        - the wallet has no routable address.

        Failures are audit-logged via ``omnibus_fund_skipped`` so the
        operator can see why a spawn didn't pull from the expected
        user credit pool, but never block the spawn itself — the local
        FakeWallet's opening balance is the in-memory proxy for the
        funding intent."""
        if self._omnibus_wallet is None:
            return 0.0
        if handle.owner_subject == "anonymous":
            return 0.0
        amount = float(opening_usdc)
        if amount <= 0:
            return 0.0
        wallet = handle.stack.wallet
        address_getter = getattr(wallet, "address", None)
        if not callable(address_getter):
            return 0.0
        try:
            to_address = address_getter()
        except Exception:
            return 0.0
        if not to_address:
            return 0.0
        try:
            self._omnibus_wallet.fund_child(
                darwin_id=handle.darwin_id,
                to_address=str(to_address),
                amount_usdc=amount,
                user_subject=handle.owner_subject,
            )
            return amount
        except Exception as exc:
            self._audit.record(
                actor="supervisor.spawn",
                action="omnibus_fund_skipped",
                darwin_id=handle.darwin_id,
                metadata={
                    "amount_usdc": amount,
                    "user_subject": handle.owner_subject,
                    "reason": str(exc),
                },
            )
            return 0.0

    async def _await_funded_balance(
        self,
        handle: _Handle,
        expected_usdc: float,
        *,
        timeout_s: float = 45.0,
    ) -> None:
        """After an omnibus fund, wait for the per-Darwin wallet to REPORT
        the funded balance before the drive loop's first reconcile.

        The omnibus transfer settles on-chain synchronously, but a
        CDP-backed wallet's balance API can lag the chain by a few seconds.
        Seeding the genome's ledger off a stale $0 would trip the drift
        halt the instant the balance catches up (the symptom: a Darwin that
        dies at tick 1 with ``ledger $0 vs wallet $X``). Best-effort: audit
        the lag and proceed if the balance hasn't surfaced by ``timeout_s``."""
        deadline = float(self._clock()) + timeout_s
        wallet = handle.stack.wallet
        while float(self._clock()) < deadline:
            try:
                if float(wallet.balance_usdc()) + 1e-9 >= expected_usdc:
                    return
            except Exception:
                pass
            await asyncio.sleep(2.0)
        self._audit.record(
            actor="supervisor.spawn",
            action="omnibus_fund_balance_lag",
            darwin_id=handle.darwin_id,
            metadata={"expected_usdc": expected_usdc},
        )

    def _sweep_dead_darwin_to_omnibus(self, handle: _Handle) -> None:
        """On kill, move the dead Darwin's residual USDC back to the
        omnibus address on-chain AND credit the owning user via
        ``omnibus.sweep_child``. Both legs are best-effort:

        - **Chain leg**: ``wallet.sweep_to(destination=omnibus_address)``
          actually transfers bytes back. A wallet without ``sweep_to``
          (older / minimal adapter) falls back to recording-only — the
          residual stays at the dead address and the operator
          reconciles by hand.
        - **Ledger leg**: ``omnibus.sweep_child`` credits user_credits.

        Skip conditions: no omnibus, anonymous owner, zero residual.
        Failures are audit-logged but never block kill."""
        if self._omnibus_wallet is None:
            return
        if handle.owner_subject == "anonymous":
            return
        wallet = handle.stack.wallet
        try:
            residual = float(wallet.balance_usdc())
        except Exception:
            return
        if residual <= 0:
            return
        try:
            from_address = wallet.address() if callable(
                getattr(wallet, "address", None)
            ) else ""
        except Exception:
            from_address = ""
        # Chain leg: actually move bytes back. Optional capability —
        # adapters without sweep_to keep the legacy record-only behaviour.
        sweeper = getattr(wallet, "sweep_to", None)
        if callable(sweeper):
            try:
                omnibus_address = self._omnibus_wallet.address()
            except Exception:
                omnibus_address = ""
            if omnibus_address:
                try:
                    sweeper(
                        destination=str(omnibus_address),
                        amount_usdc=residual,
                    )
                except Exception as exc:
                    # Chain failure: log and CONTINUE to the ledger leg.
                    # The accounting credit still belongs to the user;
                    # the on-chain bytes can be reconciled separately.
                    self._audit.record(
                        actor="supervisor.kill",
                        action="omnibus_sweep_chain_failed",
                        darwin_id=handle.darwin_id,
                        metadata={
                            "amount_usdc": residual,
                            "reason": str(exc),
                        },
                    )
        try:
            self._omnibus_wallet.sweep_child(
                darwin_id=handle.darwin_id,
                amount_usdc=residual,
                user_subject=handle.owner_subject,
                from_address=str(from_address or ""),
            )
        except Exception as exc:
            self._audit.record(
                actor="supervisor.kill",
                action="omnibus_sweep_skipped",
                darwin_id=handle.darwin_id,
                metadata={
                    "amount_usdc": residual,
                    "user_subject": handle.owner_subject,
                    "reason": str(exc),
                },
            )

    def _relay_reproduction_to_child(
        self, parent: _Handle, child_id: str, record: dict,
    ) -> None:
        """Settle the second leg of the parent → omnibus → child reproduction
        transfer. The first leg fired inside FakeSpawner.spawn (parent's
        ``transfer_out_for_child`` moved bytes to the omnibus address); this
        moves them on to the child's per-Darwin wallet address."""
        if self._omnibus_wallet is None:
            return
        child_handle = self._darwins.get(child_id)
        if child_handle is None:
            return
        address_getter = getattr(child_handle.stack.wallet, "address", None)
        if not callable(address_getter):
            return
        try:
            child_address = address_getter()
        except Exception:
            return
        if not child_address:
            return
        amount = float(record.get("budget_usdc", 0.0) or 0.0)
        if amount <= 0:
            return
        try:
            self._omnibus_wallet.relay_reproduction(
                parent_id=parent.darwin_id,
                child_id=child_id,
                child_address=str(child_address),
                amount_usdc=amount,
            )
        except Exception as exc:
            # The reproduction succeeded (the child is alive); only the
            # omnibus-side settlement failed. Surface via audit so the
            # operator can reconcile, but never block the spawn flow.
            self._audit.record(
                actor="supervisor.reproduction",
                action="reproduction_relay_failed",
                darwin_id=child_id,
                metadata={
                    "parent_id": parent.darwin_id,
                    "amount_usdc": amount,
                    "reason": str(exc),
                },
            )

    # -- cross-pollination (doc 06 phase 3) -------------------------------
    @property
    def cross_pollination(self) -> CrossPollinationArchive | None:
        """The platform's cross-pollination archive, if one is configured.
        Tests + operators can introspect via this; the supervisor's
        ``_deposit_cross_pollination`` is the write path."""
        return self._cross_pollination

    def _deposit_cross_pollination(self, parent: _Handle) -> None:
        """Sweep the parent's distilled lessons + admit a vetted subset
        into the platform archive. Skipped if no archive is configured
        OR the parent's layer stack has no new lessons to share."""
        if self._cross_pollination is None:
            return
        try:
            stack = parent.runtime._stack  # noqa: SLF001
            # The genome's distilled-lesson set lives on the most recent
            # layer the parent emitted. Reproduction has just sealed
            # that layer; we read the topmost layer's lessons.
            top = stack.find()
        except Exception:
            return
        deposited = 0
        seen_scopes: dict[str, int] = {}
        for lesson in (top or []):
            try:
                scope = getattr(lesson, "scope", None)
                kind = getattr(lesson, "kind", "prefer")
                statement = getattr(lesson, "statement", None)
                evidence = int(getattr(lesson, "evidence", 1) or 1)
            except Exception:
                continue
            if not scope or not statement:
                continue
            seen = seen_scopes.get(scope, 0)
            if seen >= self._cross_pollination_per_scope_limit:
                # Doc 06: rate-limit per-scope so a single Darwin doesn't
                # dominate the archive.
                continue
            try:
                self._cross_pollination.deposit(
                    kind=kind, scope=scope, statement=statement,
                    source_darwin_id=parent.darwin_id,
                    evidence_count=evidence,
                )
                deposited += 1
                seen_scopes[scope] = seen + 1
            except CrossPollinationError:
                # Bad shape — skip rather than poison the deposit batch.
                continue
        if deposited > 0:
            self._audit.record(
                actor="platform.cross_pollination",
                action="cross_pollination_deposit",
                darwin_id=parent.darwin_id,
                metadata={"deposited": deposited,
                          "scopes": list(seen_scopes)},
            )

    def _kyc_gate(self, subject: str, *, action: str) -> None:
        """Refuse a money-moving / spawn action by a user we cannot KYC
        (doc 05: "we will not provide service to users we cannot KYC").
        Also enforces the 18+ age gate. No-op when no KYC service is
        configured (dev / tests). KYCError surfaces as SupervisorError so the
        HTTP layer maps it to a clean 4xx."""
        if self._kyc_service is None:
            return
        from ..security.kyc import KYCError

        try:
            self._kyc_service.require_allowed(
                subject, action=action, require_adult=True,
            )
        except KYCError as exc:
            raise SupervisorError(str(exc)) from exc

    # -- fiat ramp (doc 17) ------------------------------------------------
    def fiat_deposit_quote(
        self,
        *,
        amount_usd: float,
        payment_method: str,
        destination_darwin_id: str | None = None,
        actor: str = "anonymous",
    ) -> FiatQuote:
        if self._fiat_ramp is None:
            raise SupervisorError("fiat ramp is not configured")
        self._kyc_gate(actor, action="fiat_deposit_quote")
        if (
            destination_darwin_id is not None
            and destination_darwin_id not in self._darwins
        ):
            raise UnknownDarwin(destination_darwin_id)
        try:
            quote = self._fiat_ramp.deposit_quote(
                amount_usd=amount_usd, payment_method=payment_method,
                destination_darwin_id=destination_darwin_id,
            )
        except FiatRampError as exc:
            raise SupervisorError(str(exc)) from exc
        self._audit.record(
            actor=actor,
            action="fiat_deposit_quote",
            darwin_id=destination_darwin_id,
            metadata={
                "amount_usd": float(amount_usd),
                "provider": quote.provider,
                "quote_id": quote.quote_id,
                "destination_darwin_id": destination_darwin_id,
            },
        )
        return quote

    def handle_stripe_webhook(
        self, event: dict, *, actor: str = "platform.stripe_webhook",
    ) -> dict:
        """Drive deposit confirmation from a VERIFIED Stripe event. The API
        route verifies the signature (``stripe.Webhook.construct_event``)
        and passes the parsed envelope here.

        Only ``payment_intent.succeeded`` does anything; the destination
        Darwin is read from the intent metadata that ``deposit_quote``
        stamped. Idempotent in TWO layers: (1) a durable webhook-event ledger
        short-circuits a re-delivered event id before any money effect, and
        (2) ``confirm_deposit`` consumes the one-shot quote, catching any
        crash-between-credit-and-mark. The route returns 200 either way so
        Stripe stops retrying."""
        event_type = str(event.get("type", ""))
        if event_type != "payment_intent.succeeded":
            return {"status": "ignored", "event_type": event_type}
        event_id = str(event.get("id") or "")
        # Layer 1: durable dedup on the Stripe event id.
        if self._webhook_events is not None and self._webhook_events.seen(
            event_id
        ):
            return {"status": "duplicate", "event_id": event_id}
        intent = (event.get("data", {}) or {}).get("object", {}) or {}
        intent_id = str(intent.get("id") or "")
        if not intent_id:
            return {"status": "ignored", "reason": "no payment_intent id"}
        meta = intent.get("metadata") or {}
        dst = meta.get("destination_darwin_id") or None
        try:
            receipt = self.fiat_confirm_deposit(
                quote_id=intent_id,
                destination_darwin_id=dst,
                actor=actor,
            )
        except (SupervisorError, UnknownDarwin) as exc:
            # No live quote (already-confirmed replay, or quoted before a
            # restart) / unknown darwin → skip without raising so Stripe
            # stops retrying. Audited for the operator to reconcile.
            self._audit.record(
                actor=actor,
                action="stripe_webhook_skipped",
                darwin_id=dst,
                metadata={"payment_intent": intent_id, "reason": str(exc)},
            )
            self._mark_webhook_event(event_id, "skipped")
            return {"status": "skipped", "reason": str(exc)}
        self._mark_webhook_event(event_id, "confirmed")
        return {
            "status": "confirmed",
            "provider_tx_id": receipt.provider_tx_id,
            "amount_usdc": receipt.amount_usdc,
            "destination_darwin_id": dst,
        }

    def _mark_webhook_event(self, event_id: str, status: str) -> None:
        if self._webhook_events is not None and event_id:
            self._webhook_events.mark(
                event_id=event_id, provider="stripe",
                status=status, ts=float(self._clock()),
            )

    def fiat_confirm_deposit(
        self,
        *,
        quote_id: str,
        destination_darwin_id: str | None = None,
        actor: str = "anonymous",
    ) -> FiatDepositReceipt:
        if self._fiat_ramp is None:
            raise SupervisorError("fiat ramp is not configured")
        self._kyc_gate(actor, action="fiat_confirm_deposit")
        if (
            destination_darwin_id is not None
            and destination_darwin_id not in self._darwins
        ):
            raise UnknownDarwin(destination_darwin_id)
        try:
            receipt = self._fiat_ramp.confirm_deposit(
                quote_id=quote_id,
                destination_darwin_id=destination_darwin_id,
            )
        except FiatRampError as exc:
            raise SupervisorError(str(exc)) from exc
        # Credit the Darwin's wallet with the deposited USDC (the ramp
        # has already moved the user's USD; the platform now reflects
        # that USDC inside the Darwin's accounting).
        if destination_darwin_id is not None:
            h = self._darwins[destination_darwin_id]
            try:
                h.stack.wallet.credit(
                    receipt.amount_usdc,
                    f"fiat:{receipt.provider}:{receipt.provider_tx_id}",
                )
            except Exception as exc:
                raise SupervisorError(
                    f"wallet refused credit after fiat deposit: {exc}"
                ) from exc
        self._audit.record(
            actor=actor,
            action="fiat_confirm_deposit",
            darwin_id=destination_darwin_id,
            metadata={
                "provider": receipt.provider,
                "quote_id": receipt.quote_id,
                "provider_tx_id": receipt.provider_tx_id,
                "amount_usd": receipt.amount_usd,
                "amount_usdc": receipt.amount_usdc,
                "fee_usd": receipt.fee_usd,
            },
        )
        return receipt

    async def darwin_fiat_payout(
        self,
        darwin_id: str,
        *,
        amount_usdc: float,
        freelancer_id: str,
        escalation_id: str | None = None,
        actor: str = "anonymous",
    ) -> dict[str, Any]:
        """Pay a fiat counterparty on a Darwin's behalf — the path for
        the freelancer / legal / professional spends a Darwin can't
        settle in USDC because the recipient only accepts a bank
        transfer (doc 17 §"Darwin paying a human").

        ``freelancer_id`` resolves through the :class:`FreelancerRegistry`
        scoped to the Darwin's ``owner_subject``. Only previously
        registered, verified destinations are reachable — the registry
        is the platform's KYC gate. A misuse (id not registered for the
        requesting owner) raises before any chain transfer fires.

        Two-leg flow: the Darwin's USDC wallet debits ``amount_usdc`` to
        the omnibus address (on-chain), and the omnibus converts the
        USDC into fiat via the configured :class:`FiatRamp` and pushes
        the payout to the registered destination.

        This is an OPERATOR-initiated call: the genome's
        ``payments.facilitator`` cannot reach fiat directly. A typical
        flow is the Darwin escalates a freelancer payment, the owner
        approves it through the escalation API, then the operator
        invokes this method to settle the approved spend via fiat. The
        intermediate omnibus address insulates the Darwin's wallet from
        the ramp's settlement timing.
        """
        if self._omnibus_wallet is None:
            raise SupervisorError("omnibus wallet is not configured")
        if self._fiat_ramp is None:
            raise SupervisorError("fiat ramp is not configured")
        if self._freelancer_registry is None:
            raise SupervisorError(
                "freelancer registry is not configured — "
                "darwin_fiat_payout is blocked until destinations can be verified",
            )
        amount = float(amount_usdc)
        if amount <= 0:
            raise SupervisorError("amount_usdc must be positive")
        if not freelancer_id:
            raise SupervisorError("freelancer_id is required")
        with span(
            "supervisor.darwin_fiat_payout",
            darwin_id=darwin_id,
            attributes={
                "amount_usdc": amount,
                "freelancer_id": freelancer_id,
            },
        ) as s:
            h = self._get(darwin_id)
            annotate_span_with_runtime(s, h.runtime)
            wallet = h.stack.wallet
            transfer = getattr(wallet, "transfer_out_for_fiat_payout", None)
            if not callable(transfer):
                raise SupervisorError(
                    "wallet does not support fiat payout transfer",
                )
            # Resolve freelancer through the owner-scoped registry. A
            # destination not registered to the Darwin's owner is refused
            # outright — the registry is the KYC gate at the platform edge.
            destination = self._freelancer_registry.lookup(
                owner_subject=h.owner_subject,
                freelancer_id=freelancer_id,
            )
            if destination is None:
                raise SupervisorError(
                    f"freelancer {freelancer_id!r} is not registered "
                    f"for owner {h.owner_subject!r}",
                )
            fiat_destination = destination.fiat_destination
            try:
                omnibus_address = self._omnibus_wallet.address()
            except Exception as exc:
                raise SupervisorError(
                    f"omnibus address unavailable: {exc}",
                ) from exc
            # First leg: debit the Darwin's USDC wallet to the omnibus.
            try:
                transfer(amount, destination=omnibus_address)
            except Exception as exc:
                raise SupervisorError(
                    f"first leg (Darwin → omnibus) failed: {exc}",
                ) from exc
            # Second leg: omnibus → fiat. If this fails the Darwin has
            # already lost the USDC to the omnibus address; surface the
            # failure clearly so the operator can refund via the omnibus
            # ledger reconciliation, but DON'T retry implicitly.
            try:
                entry = self._omnibus_wallet.relay_fiat_payout(
                    darwin_id=darwin_id,
                    amount_usdc=amount,
                    fiat_destination=fiat_destination,
                    fiat_ramp=self._fiat_ramp,
                    actor=actor,
                    escalation_id=escalation_id,
                )
            except Exception as exc:
                self._audit.record(
                    actor=actor,
                    action="darwin_fiat_payout_second_leg_failed",
                    darwin_id=darwin_id,
                    metadata={
                        "amount_usdc": amount,
                        "fiat_destination": fiat_destination,
                        "reason": str(exc),
                    },
                )
                raise SupervisorError(
                    f"second leg (omnibus → fiat) failed: {exc}",
                ) from exc
            self._audit.record(
                actor=actor,
                action="darwin_fiat_payout",
                darwin_id=darwin_id,
                metadata={
                    "amount_usdc": amount,
                    "freelancer_id": freelancer_id,
                    "fiat_destination": fiat_destination,
                    "kyc_status": destination.kyc_status,
                    "provider": entry.metadata.get("provider", ""),
                    "provider_tx_id": entry.tx_hash,
                    "escalation_id": escalation_id,
                },
            )
            return entry.to_json()

    # -- freelancer registry CRUD (doc 17 §"Darwin paying a human") --------
    def register_freelancer(
        self,
        *,
        owner_subject: str,
        freelancer_id: str,
        fiat_destination: str,
        display_name: str = "",
        kyc_status: str = "unverified",
        metadata: dict[str, Any] | None = None,
        actor: str = "anonymous",
    ) -> dict[str, Any]:
        """Register (or replace) a verified fiat-payout destination for
        an owner. Subsequent ``darwin_fiat_payout`` calls with this
        ``freelancer_id`` resolve through this entry. Audit-logged so
        the operator timeline shows who onboarded which freelancer."""
        if self._freelancer_registry is None:
            raise SupervisorError("freelancer registry is not configured")
        from ..payments.freelancer_registry import FreelancerDestination

        dest = FreelancerDestination(
            owner_subject=owner_subject,
            freelancer_id=freelancer_id,
            fiat_destination=fiat_destination,
            display_name=display_name,
            kyc_status=kyc_status,
            registered_at=float(self._clock()),
            metadata=dict(metadata or {}),
        )
        try:
            stored = self._freelancer_registry.register(dest)
        except Exception as exc:
            raise SupervisorError(
                f"freelancer registry refused: {exc}",
            ) from exc
        self._audit.record(
            actor=actor,
            action="register_freelancer",
            darwin_id=None,
            metadata={
                "owner_subject": owner_subject,
                "freelancer_id": freelancer_id,
                # destination is sensitive enough that we don't log it
                # raw — the registry has it for the supervisor to resolve.
                "display_name": display_name,
                "kyc_status": kyc_status,
            },
        )
        return stored.to_json()

    def revoke_freelancer(
        self,
        *,
        owner_subject: str,
        freelancer_id: str,
        actor: str = "anonymous",
    ) -> dict[str, Any]:
        """Drop a freelancer registration. Idempotent."""
        if self._freelancer_registry is None:
            raise SupervisorError("freelancer registry is not configured")
        removed = bool(self._freelancer_registry.revoke(
            owner_subject=owner_subject,
            freelancer_id=freelancer_id,
        ))
        if removed:
            self._audit.record(
                actor=actor,
                action="revoke_freelancer",
                darwin_id=None,
                metadata={
                    "owner_subject": owner_subject,
                    "freelancer_id": freelancer_id,
                },
            )
        return {"revoked": removed, "freelancer_id": freelancer_id}

    def list_freelancers(
        self, *, owner_subject: str,
    ) -> list[dict[str, Any]]:
        if self._freelancer_registry is None:
            return []
        return [
            d.to_json()
            for d in self._freelancer_registry.list(
                owner_subject=owner_subject,
            )
        ]

    # -- push subscriptions (doc 22 §"Mobile (escalation push)") -------------
    def register_push_subscription(
        self,
        *,
        owner_subject: str,
        endpoint: str,
        p256dh: str,
        auth: str,
        user_agent: str = "",
        actor: str = "anonymous",
    ) -> dict[str, Any]:
        """Persist a Web Push subscription for the owner and tell the live
        notification sink about it. The dashboard / PWA calls this when the
        browser hands back a fresh subscription from ``PushManager.subscribe``.
        Idempotent on (owner, endpoint) — re-registering replaces the keys
        the browser may have rotated."""
        if self._push_subscription_store is None:
            raise SupervisorError("push subscription store is not configured")
        if not endpoint or not p256dh or not auth:
            raise SupervisorError(
                "endpoint, p256dh, and auth are required",
            )
        try:
            self._push_subscription_store.register(
                owner_subject=owner_subject,
                endpoint=endpoint,
                p256dh=p256dh,
                auth=auth,
                user_agent=user_agent,
            )
        except Exception as exc:
            raise SupervisorError(
                f"push subscription store refused: {exc}",
            ) from exc
        # If the live notification sink is the WebPush one, push the new
        # subscription in so the next ticket fans out to this device
        # immediately — no restart needed.
        sink = getattr(self._router, "sink", None)
        register_fn = getattr(sink, "register_subscription", None)
        if callable(register_fn):
            try:
                register_fn(
                    owner_subject=owner_subject,
                    endpoint=endpoint,
                    p256dh=p256dh,
                    auth=auth,
                    user_agent=user_agent,
                )
            except Exception:
                # The live sink rejecting must not invalidate the persisted
                # row — a restart will re-hydrate from disk.
                pass
        self._audit.record(
            actor=actor,
            action="register_push_subscription",
            darwin_id=None,
            metadata={
                "owner_subject": owner_subject,
                # endpoint is sensitive (anyone with it + the VAPID key can
                # deliver), so we only log the origin.
                "endpoint_origin": _push_endpoint_origin(endpoint),
                "user_agent": user_agent,
            },
        )
        return {
            "owner_subject": owner_subject,
            "endpoint_origin": _push_endpoint_origin(endpoint),
            "registered": True,
        }

    def revoke_push_subscription(
        self,
        *,
        owner_subject: str,
        endpoint: str,
        actor: str = "anonymous",
    ) -> dict[str, Any]:
        """Drop a push subscription from both the persistent store and the
        live sink. Idempotent: returns ``{"revoked": False}`` if the row
        wasn't there."""
        if self._push_subscription_store is None:
            raise SupervisorError("push subscription store is not configured")
        removed = bool(self._push_subscription_store.revoke(
            owner_subject=owner_subject,
            endpoint=endpoint,
        ))
        sink = getattr(self._router, "sink", None)
        unregister_fn = getattr(sink, "unregister_subscription", None)
        if callable(unregister_fn):
            try:
                unregister_fn(
                    owner_subject=owner_subject,
                    endpoint=endpoint,
                )
            except Exception:
                pass
        if removed:
            self._audit.record(
                actor=actor,
                action="revoke_push_subscription",
                darwin_id=None,
                metadata={
                    "owner_subject": owner_subject,
                    "endpoint_origin": _push_endpoint_origin(endpoint),
                },
            )
        return {"revoked": removed}

    def push_vapid_public_key(self) -> str | None:
        """The base64url-encoded VAPID public key (P-256 uncompressed), or
        None if the platform isn't configured for Web Push. The PWA reads
        this once at sign-up time to subscribe with the right
        ``applicationServerKey``."""
        return self._push_vapid_public_key

    def _wire_push_persistence(self, sink: Any) -> None:
        """Hook the persistent push subscription store up to the live
        WebPushNotificationSink (if both are present). Called at the tail
        of ``__init__`` once both objects exist. No-op when the sink isn't
        a Web Push one or there's no store."""
        store = self._push_subscription_store
        if store is None or sink is None:
            return
        # Duck-typed — avoid importing the concrete class to keep the
        # supervisor independent of the [webpush] optional extra.
        load = getattr(store, "load_into_sink", None)
        set_purge = getattr(sink, "set_subscription_purge", None)
        if not (callable(load) and callable(set_purge)):
            return
        try:
            load(sink)
        except Exception:
            # A corrupt row in the store must not crash boot; the sink is
            # still usable, just empty.
            pass
        try:
            set_purge(
                lambda owner_subject, endpoint: store.revoke(
                    owner_subject=owner_subject, endpoint=endpoint,
                )
            )
        except Exception:
            pass

    def list_push_subscriptions(
        self, *, owner_subject: str,
    ) -> list[dict[str, Any]]:
        """Return the owner's subscriptions with sensitive fields redacted
        (endpoint origin only; never the full path or the keys)."""
        if self._push_subscription_store is None:
            return []
        out = []
        for row in self._push_subscription_store.list(
            owner_subject=owner_subject,
        ):
            out.append({
                "endpoint_origin": _push_endpoint_origin(row["endpoint"]),
                "user_agent": row.get("user_agent", ""),
                "registered_at": row.get("registered_at", 0.0),
            })
        return out

    async def retry_omnibus_fund(
        self,
        darwin_id: str,
        *,
        amount_usdc: float,
        actor: str = "anonymous",
    ) -> dict[str, Any]:
        """Retry an omnibus.fund_child that was skipped at spawn time
        (typically a chain hiccup recorded in the audit as
        ``omnibus_fund_skipped``). Operator-initiated — there's no
        automatic retry loop, because the right cadence depends on the
        operator's reconciliation cycle.

        Same shape as the spawn-time funding helper:

        - Looks up the Darwin's owner_subject and wallet address.
        - Calls ``omnibus.fund_child(amount, user_subject=owner,
          to_address=wallet_address)`` which debits user_credits and
          submits the chain transfer.
        - Audits the retry on success or surface the omnibus error.
        """
        if self._omnibus_wallet is None:
            raise SupervisorError("omnibus wallet is not configured")
        amount = float(amount_usdc)
        if amount <= 0:
            raise SupervisorError("amount_usdc must be positive")
        with span(
            "supervisor.retry_omnibus_fund",
            darwin_id=darwin_id,
            attributes={"amount_usdc": amount},
        ) as s:
            h = self._get(darwin_id)
            annotate_span_with_runtime(s, h.runtime)
            if h.owner_subject == "anonymous":
                raise SupervisorError(
                    "cannot retry fund for an anonymous-owned Darwin "
                    "(no user_credits to attribute the debit to)",
                )
            wallet = h.stack.wallet
            address_getter = getattr(wallet, "address", None)
            if not callable(address_getter):
                raise SupervisorError(
                    "wallet has no on-chain address — cannot fund",
                )
            try:
                to_address = address_getter()
            except Exception as exc:
                raise SupervisorError(
                    f"wallet address unavailable: {exc}",
                ) from exc
            if not to_address:
                raise SupervisorError("wallet address is empty")
            try:
                entry = self._omnibus_wallet.fund_child(
                    darwin_id=darwin_id,
                    to_address=str(to_address),
                    amount_usdc=amount,
                    user_subject=h.owner_subject,
                    actor=actor,
                )
            except Exception as exc:
                raise SupervisorError(
                    f"omnibus fund retry failed: {exc}",
                ) from exc
            self._audit.record(
                actor=actor,
                action="retry_omnibus_fund",
                darwin_id=darwin_id,
                metadata={
                    "amount_usdc": amount,
                    "user_subject": h.owner_subject,
                    "to_address": str(to_address),
                    "tx_hash": entry.tx_hash,
                },
            )
            return entry.to_json()

    async def refund_fiat_payout(
        self,
        darwin_id: str,
        *,
        amount_usdc: float,
        reason: str = "fiat_payout_leg2_failed",
        actor: str = "anonymous",
    ) -> dict[str, Any]:
        """Recovery for a failed fiat payout. When ``darwin_fiat_payout``
        fails on leg 2 (omnibus → fiat ramp), leg 1 has already debited
        the Darwin's USDC wallet to the omnibus address. This method:

        - submits a chain transfer from omnibus → the Darwin's wallet
          address, undoing leg 1 (recorded as a ``REFUND_TO_DARWIN``
          omnibus entry),
        - calls ``wallet.credit(amount, "fiat_payout_refund")`` so the
          Darwin's local ledger reflects the inbound.

        The operator decides when to invoke this — automatic refunds
        on leg-2 failure would mask a real ramp outage that the
        operator may want to retry instead."""
        if self._omnibus_wallet is None:
            raise SupervisorError("omnibus wallet is not configured")
        amount = float(amount_usdc)
        if amount <= 0:
            raise SupervisorError("amount_usdc must be positive")
        with span(
            "supervisor.refund_fiat_payout",
            darwin_id=darwin_id,
            attributes={"amount_usdc": amount},
        ) as s:
            h = self._get(darwin_id)
            annotate_span_with_runtime(s, h.runtime)
            wallet = h.stack.wallet
            address_getter = getattr(wallet, "address", None)
            if not callable(address_getter):
                raise SupervisorError(
                    "wallet has no on-chain address — cannot refund",
                )
            try:
                darwin_address = address_getter()
            except Exception as exc:
                raise SupervisorError(
                    f"wallet address unavailable: {exc}",
                ) from exc
            if not darwin_address:
                raise SupervisorError("wallet address is empty")
            try:
                entry = self._omnibus_wallet.refund_to_darwin(
                    darwin_id=darwin_id,
                    darwin_address=str(darwin_address),
                    amount_usdc=amount,
                    reason=reason,
                    actor=actor,
                )
            except Exception as exc:
                raise SupervisorError(
                    f"omnibus refund failed: {exc}",
                ) from exc
            # Best-effort: credit the Darwin's local ledger so its
            # balance_usdc reflects the inbound. Real wallets are
            # authoritative against the chain; FakeWallet's local
            # balance is the proxy in tests + sim.
            crediter = getattr(wallet, "credit", None)
            if callable(crediter):
                try:
                    crediter(amount, "fiat_payout_refund")
                except Exception:
                    pass
            self._audit.record(
                actor=actor,
                action="refund_fiat_payout",
                darwin_id=darwin_id,
                metadata={
                    "amount_usdc": amount,
                    "reason": reason,
                    "tx_hash": entry.tx_hash,
                },
            )
            return entry.to_json()

    def fiat_withdraw(
        self,
        *,
        amount_usdc: float,
        destination: str,
        actor: str = "anonymous",
    ) -> FiatWithdrawReceipt:
        """Operator-driven payout. The Darwin's harvest already moved
        USDC off the wallet via the on-chain path; this records the
        ramp's fiat leg. For a direct Darwin → owner-bank withdrawal,
        operators use this AFTER a successful harvest."""
        if self._fiat_ramp is None:
            raise SupervisorError("fiat ramp is not configured")
        self._kyc_gate(actor, action="fiat_withdraw")
        try:
            receipt = self._fiat_ramp.withdraw(
                amount_usdc=amount_usdc, destination=destination,
            )
        except FiatRampError as exc:
            raise SupervisorError(str(exc)) from exc
        self._audit.record(
            actor=actor,
            action="fiat_withdraw",
            darwin_id=None,
            metadata={
                "provider": receipt.provider,
                "provider_tx_id": receipt.provider_tx_id,
                "amount_usdc": receipt.amount_usdc,
                "amount_usd": receipt.amount_usd,
                "fee_usd": receipt.fee_usd,
                "destination": destination,
            },
        )
        return receipt

    @property
    def fiat_ramp(self) -> FiatRamp | None:
        return self._fiat_ramp

    # -- platform signing key rotation (doc 02) ---------------------------
    def rotate_signing_key(
        self,
        *,
        drain_window_seconds: float | None = None,
        actor: str = "anonymous",
    ) -> dict:
        """Issue a new platform signing key + retire the current one
        (doc 02 §rotation). Requires persistence (otherwise there's
        nothing to persist the retired key into). Audit-logged."""
        if self._persistence is None:
            raise SupervisorError(
                "key rotation requires persistence (--db) to be configured"
            )
        from ..security.key_rotation import KeyRotationService
        svc = KeyRotationService(
            persistence=self._persistence,
            signer=self._signer,
            signer_factory=SigningService,
            signer_from_bytes=SigningService.from_private_bytes,
            clock=self._clock,
        )
        summary = svc.rotate(
            new_drain_window_seconds=drain_window_seconds,
        )
        # Adopt the new active signer for subsequent spawns.
        self._signer = svc.signer
        self._audit.record(
            actor=actor,
            action="rotate_signing_key",
            darwin_id=None,
            metadata={
                "retired_public_key_pem": summary["retired_public_key_pem"],
                "drain_until": summary["drain_until"],
            },
        )
        return summary

    # -- Path-C neuron proposal admission (doc 04 phase 3) ----------------
    def list_neuron_proposals(self, darwin_id: str) -> dict:
        """Per-Darwin view of pending + admitted neuron proposals."""
        if darwin_id not in self._darwins:
            raise UnknownDarwin(darwin_id)
        h = self._darwins[darwin_id]
        gate = h.runtime.neuron_admission
        if gate is None:
            # Path-C disabled (doc 04 default): no gate, nothing to list.
            return {"pending": [], "admitted": []}
        return {
            "pending": [p.to_json() for p in gate.pending()],
            "admitted": [
                {"name": n.name,
                 "tags": sorted(n.capability_tags),
                 "version": n.version}
                for n in gate.admitted().values()
            ],
        }

    def admit_neuron_proposal(
        self, darwin_id: str, name: str, *, actor: str = "anonymous",
    ) -> dict:
        if darwin_id not in self._darwins:
            raise UnknownDarwin(darwin_id)
        h = self._darwins[darwin_id]
        try:
            h.runtime.admit_neuron_proposal(name)
        except Exception as exc:
            raise SupervisorError(str(exc)) from exc
        self._audit.record(
            actor=actor,
            action="admit_neuron_proposal",
            darwin_id=darwin_id,
            metadata={"name": name},
        )
        return {"admitted": name}

    def reject_neuron_proposal(
        self,
        darwin_id: str,
        name: str,
        *,
        reason: str = "operator rejected",
        actor: str = "anonymous",
    ) -> dict:
        if darwin_id not in self._darwins:
            raise UnknownDarwin(darwin_id)
        h = self._darwins[darwin_id]
        if h.runtime.neuron_admission is None:
            raise SupervisorError(
                "Path-C is disabled; enable path_c_enabled to manage proposals"
            )
        h.runtime.neuron_admission.reject(name, reason=reason)
        self._audit.record(
            actor=actor,
            action="reject_neuron_proposal",
            darwin_id=darwin_id,
            metadata={"name": name, "reason": reason},
        )
        return {"rejected": name, "reason": reason}

    def _inject_cross_pollination(
        self,
        *,
        child_id: str,
        child_runtime,
        parent_id: str,
    ) -> None:
        """Pull vetted sibling lessons from the archive + stage them in
        the child's semantic memory at boot. The child's planner reads
        from semantic memory at every heartbeat (via the staging+drain
        protocol in doc 03), so this is the cheapest place to inject
        external knowledge."""
        if self._cross_pollination is None:
            return
        # Build the ancestry exclusion set so a child doesn't ingest
        # its own ancestors' lessons (those came in via the inherited
        # layer stack directly).
        try:
            ancestors = self._lineage_registry.ancestry_of(child_id)
        except Exception:
            ancestors = []
        excluding = set(ancestors) | {child_id, parent_id}
        # The scopes we ingest for: every capability tag in the child's
        # founding catalog. The archive is per-scope, so the child only
        # gets lessons relevant to capabilities it actually has.
        try:
            scopes: set[str] = set()
            for neuron in child_runtime.catalog.values():
                scopes |= set(getattr(neuron, "capability_tags", set()))
        except Exception:
            return
        injected = 0
        seen_scopes: list[str] = []
        for scope in sorted(scopes):
            lessons = self._cross_pollination.retrieve(
                scope=scope, excluding_darwin_ids=excluding,
                limit=self._cross_pollination_per_scope_limit,
            )
            for lesson in lessons:
                try:
                    child_runtime.semantic.stage(
                        kind=lesson.kind,
                        scope=lesson.scope,
                        statement=lesson.statement,
                        payload={
                            "source_darwin_id": lesson.source_darwin_id,
                            "via": "cross_pollination",
                            "evidence_count": lesson.evidence_count,
                        },
                        ts=float(self._clock()),
                    )
                    injected += 1
                except Exception:
                    continue
            if lessons:
                seen_scopes.append(scope)
        if injected > 0:
            self._audit.record(
                actor="platform.cross_pollination",
                action="cross_pollination_inject",
                darwin_id=child_id,
                metadata={"injected": injected, "scopes": seen_scopes},
            )

    def fetch_cross_pollination(
        self,
        *,
        scope: str,
        excluding_darwin_ids: set[str] | None = None,
        limit: int = 5,
    ) -> list[PollinationLesson]:
        """Read-side: a child at boot (or an operator) requests vetted
        sibling lessons for ``scope``. Returns an empty list when no
        archive is configured."""
        if self._cross_pollination is None:
            return []
        return self._cross_pollination.retrieve(
            scope=scope,
            excluding_darwin_ids=excluding_darwin_ids,
            limit=limit,
        )

    def _signals_for(self, handle: _Handle) -> DarwinSignals:
        rt = handle.runtime
        st = rt.state
        # Ticket open-timestamps from the router so escalation-rate detection
        # uses the *platform* view (the genome's pending_escalations list is
        # state-tick-indexed, not wall-clock).
        opened = tuple(
            t.opened_ts for t in self._router.all_for_darwin(handle.darwin_id)
        )
        return DarwinSignals(
            darwin_id=handle.darwin_id,
            now=self._clock(),
            alive=bool(st.alive),
            paused=bool(getattr(st, "paused", False)),
            state_tick=int(st.tick),
            last_tick_advance_ts=self._monitor.last_tick_advance(handle.darwin_id),
            movements=tuple(handle.stack.wallet.movements()),
            dispatches_done=int(st.dispatches_done),
            successful_dispatches=int(st.successful_dispatches),
            recurring_failures=dict(st.recurring_failures),
            ticket_opened_ts=opened,
        )

    def _run_monitor(self, handle: _Handle) -> None:
        """Build a signals snapshot, run detectors, optionally auto-pause on
        a fresh CRITICAL. Auto-pause is gated by ``auto_pause_on_critical``
        so a misconfigured quota cannot freeze the fleet."""
        try:
            signals = self._signals_for(handle)
        except Exception:
            return
        fired = self._monitor.check(signals)
        # Only open a span when alerts actually fired — keeps the trace
        # signal-to-noise high (the monitor runs every N ticks).
        if fired:
            with span(
                "supervisor.alerts",
                darwin_id=handle.darwin_id,
                attributes={"count": len(fired)},
            ) as s:
                annotate_span_with_runtime(s, handle.runtime)
                s.set_attribute(
                    "kinds", ",".join(sorted({a.kind for a in fired}))
                )
                s.set_attribute(
                    "max_severity",
                    max(a.severity.value for a in fired)
                    if fired
                    else "",
                )
        if handle.killed or not handle.runtime.state.alive:
            return
        # Auto-throttle on WARN (doc 09 level 2). Idempotent per (darwin,
        # kind) — once a throttle is in place the breach stops firing or
        # the operator clears the throttle explicitly.
        if self._auto_throttle_on_warn:
            self._maybe_auto_throttle(handle, fired)
        # Auto-pause on CRITICAL (doc 09 level 3). Pause once per kind so a
        # sustained breach doesn't re-pause an already-paused Darwin forever.
        # The Monitor's dedup already throttles sink emission; this gate is
        # idempotent per (darwin, kind) and clears on resume.
        if not self._auto_pause_on_critical:
            return
        for alert in fired:
            if alert.severity is not Severity.CRITICAL:
                continue
            if handle.auto_paused_for == alert.kind:
                continue
            with span(
                "supervisor.auto_pause",
                darwin_id=handle.darwin_id,
                attributes={"kind": alert.kind},
            ) as s:
                annotate_span_with_runtime(s, handle.runtime)
                handle.auto_paused_for = alert.kind
                try:
                    handle.stack.pause.pause(f"auto: {alert.kind} critical")
                except Exception:
                    handle.auto_paused_for = None
            break

    # Mapping of alert kind → the LLM tier we throttle on a WARN of that
    # kind. The matching tier is the most common driver of that signal:
    # high LLM-tier cost shows up first as spend_velocity; high coding-tier
    # use shows up as failure_rate (the soma is over-mutating). Other
    # kinds (heartbeat, escalation_rate) have no obvious tier mapping so
    # they don't auto-throttle — they just alert.
    _WARN_TIER_MAP = {
        "spend_velocity": "reasoning",
        "failure_rate": "coding",
    }

    # Non-LLM resource throttle map. A sustained WARN of these alert
    # kinds halves the corresponding resource's daily cap (or installs
    # a conservative default cap if none was set). Heartbeat / spend_velocity
    # / failure_rate stay on the LLM tier map above; spread_spend points
    # at egress because the smear attack typically targets x402 / search.
    _WARN_RESOURCE_MAP = {
        "spread_spend": THROTTLE_KIND_EGRESS,
        "escalation_rate": THROTTLE_KIND_BROWSER,
    }
    # Default cap installed by the auto-throttle when no operator cap
    # exists yet. Conservative — operators tighten further via the API.
    _AUTO_THROTTLE_DEFAULT_CAP = 50

    def _maybe_auto_throttle_resource(
        self, handle: _Handle, kind: str, alert_kind: str,
    ) -> None:
        """Halve (or install a default) per-Darwin daily cap on
        ``kind`` ∈ {sandbox, browser, egress}. Latched per (darwin,
        alert_kind) via handle.auto_throttled so a sustained WARN doesn't
        repeatedly halve until the cap is one call/day."""
        existing = self._throttle_service.get(
            darwin_id=handle.darwin_id, kind=kind,
        )
        if existing is None:
            new_cap = self._AUTO_THROTTLE_DEFAULT_CAP
        else:
            new_cap = max(1, existing.max_daily_calls // 2)
        try:
            self._throttle_service.set(
                darwin_id=handle.darwin_id, kind=kind,
                max_daily_calls=new_cap,
            )
        except ThrottleError:
            return
        handle.auto_throttled.add(alert_kind)
        self._audit.record(
            actor="platform.auto_throttle",
            action="set_resource_throttle",
            darwin_id=handle.darwin_id,
            metadata={
                "kind": kind, "max_daily_calls": new_cap,
                "trigger_alert": alert_kind,
            },
        )

    def _maybe_auto_throttle(self, handle: _Handle, fired: list[Alert]) -> None:
        from darwin_dna.machinery.llm_router import Tier, hp_cap_for_tier

        for alert in fired:
            if alert.severity is not Severity.WARN:
                continue
            # Non-LLM resource path first — handled by ThrottleService.
            resource_kind = self._WARN_RESOURCE_MAP.get(alert.kind)
            if resource_kind is not None:
                if alert.kind in handle.auto_throttled:
                    continue
                self._maybe_auto_throttle_resource(
                    handle, resource_kind, alert.kind,
                )
                continue
            # LLM tier path — handled by the genome's LLMRouter.
            tier_name = self._WARN_TIER_MAP.get(alert.kind)
            if tier_name is None:
                continue
            if alert.kind in handle.auto_throttled:
                continue
            try:
                tier = Tier(tier_name)
            except ValueError:
                continue
            hp_cap = hp_cap_for_tier(handle.runtime._hp, tier)  # noqa: SLF001
            if hp_cap is None:
                continue
            # Halve the hp cap as the throttle. Already-spent calls stay
            # spent; ``remaining`` will take the min of (hp - spent,
            # throttle - spent).
            new_cap = max(1, hp_cap // 2)
            with span(
                "supervisor.auto_throttle",
                darwin_id=handle.darwin_id,
                attributes={
                    "kind": alert.kind,
                    "tier": tier_name,
                    "max_daily_calls": new_cap,
                },
            ) as s:
                annotate_span_with_runtime(s, handle.runtime)
                try:
                    handle.runtime.llm.set_throttle(tier, new_cap)
                    handle.auto_throttled.add(alert.kind)
                except Exception:
                    pass

    def _observe_escalations(self, handle: _Handle) -> None:
        """Sweep new tickets the genome posted to its inbox since last
        heartbeat through the router (classify, deadline, notify). Idempotent
        on ticket id, so a slow sink that fails to notify the first time
        gets another chance on the next sweep without re-firing for the
        ones that did notify."""
        tickets = handle.stack.escalation.tickets
        new = tickets[handle.escalation_observed :]
        if not new:
            return
        for payload in new:
            # The genome's posts include darwin_id; defensively inject the
            # handle's id when a test or other caller posts a payload without
            # one, so the router buckets correctly either way.
            if not payload.get("darwin_id"):
                payload = {**payload, "darwin_id": handle.darwin_id}
            with span(
                "escalation.register",
                darwin_id=handle.darwin_id,
                attributes={
                    "ticket_id": payload.get("ticket"),
                    "ticket_class": payload.get("ticket_class"),
                    "neuron": payload.get("neuron"),
                },
            ) as s:
                annotate_span_with_runtime(s, handle.runtime)
                try:
                    routed = self._router.register(payload)
                except Exception:
                    # A malformed payload is a bug in whatever posted it.
                    # The supervisor must not die because of one bad
                    # ticket; skip it and let the others through.
                    continue
                # Mirror the genome-side auto-response (MemoryEscalationInbox
                # queues one on every post when ``auto`` is set) into the
                # router so the dashboard's open-ticket inbox matches what
                # the Darwin actually sees. Without this, every auto-resolved
                # ticket lingers as "open" forever.
                self._auto_decide_escalation(handle, routed)
        handle.escalation_observed = len(tickets)

    def _auto_decide_escalation(
        self, handle: _Handle, routed: RoutedTicket,
    ) -> None:
        """Auto-respond on the router when the owner has opted in.
        Mirrors the inbox's auto-response so router state stays in sync
        with the genome's view (doc 09 escalation-auto)."""
        policy = handle.escalation_auto
        if policy not in ("approve", "deny"):
            return
        if routed.ticket_id in handle.handled_responses:
            return
        try:
            self._router.mark_responded(routed.ticket_id, policy, {})
        except Exception:
            return
        handle.handled_responses.add(routed.ticket_id)
        self._audit.record(
            actor="platform.escalation_auto",
            action="respond_escalation",
            darwin_id=handle.darwin_id,
            metadata={
                "escalation_id": routed.ticket_id,
                "decision": policy,
                "auto": True,
                "ticket_class": routed.ticket_class.value,
            },
        )

    def _observe_billing(self, handle: _Handle) -> None:
        """Sweep new wallet movements since last cursor, attributing each
        metered debit to platform revenue. Idempotent; cheap; cursor-based
        so the same entry never counts twice."""
        movements = handle.stack.wallet.movements()
        new = movements[handle.billing_observed :]
        if not new:
            return
        revenue_added = 0.0
        for mv in new:
            debit = mv.get("debit") if isinstance(mv, dict) else None
            # Only "metered" debits attribute to infrastructure revenue.
            # Payment debits are the Darwin paying a counterparty; those
            # are the Darwin's expense, not the platform's pass-through.
            if not debit or debit.get("kind") != "metered":
                continue
            _, revenue = self._billing.record_metered(
                handle.darwin_id,
                float(debit.get("amount_usdc", 0.0)),
                memo=debit.get("memo"),
            )
            revenue_added += revenue
        handle.billing_observed = len(movements)
        # Emit a span only if revenue was actually accrued — otherwise the
        # heartbeat noise drowns the signal.
        if revenue_added > 0:
            with span(
                "supervisor.billing.metered",
                darwin_id=handle.darwin_id,
                attributes={"revenue_usdc": revenue_added},
            ) as s:
                annotate_span_with_runtime(s, handle.runtime)

    def _boot_child(self, parent: _Handle, record: dict, child_id: str) -> None:
        with span(
            "supervisor.boot_child",
            darwin_id=child_id,
            attributes={
                "parent_id": parent.darwin_id,
                "budget_usdc": float(record.get("budget_usdc", 0.0)),
            },
        ) as s:
            try:
                child_platform, child_stack = make_child_platform(
                    signer=parent.stack.signer,
                    child_manifest_json=record["child_manifest"],
                    child_layer_blobs=record["child_layer_blobs"],
                    opening_usdc=float(record["budget_usdc"]),
                    clock=self._clock,
                )
                if self._content_classifier is not None:
                    child_platform.content_classifier = self._content_classifier
                child_runtime = boot(
                    child_platform, sleep=lambda _: None, hp=self._darwin_hp,
                )
                # Inherit fleet-wide sanctions screener (doc 17) for the
                # child too — otherwise a freshly spawned generation could
                # transact unscreened until the operator re-installs it.
                if self._sanctions is not None:
                    setter = getattr(
                        child_stack.wallet, "set_sanctions_screener", None,
                    )
                    if callable(setter):
                        setter(self._sanctions)
                # Cross-pollination read-side (doc 06 phase 3). Inject
                # vetted sibling lessons into the child's semantic
                # memory so the planner's "have I seen this pattern
                # before?" lookups benefit from what unrelated siblings
                # already learned. Bounded per scope; ancestry-excluded
                # so a child doesn't double-count its parent's lessons.
                self._inject_cross_pollination(
                    child_id=child_id, child_runtime=child_runtime,
                    parent_id=parent.darwin_id,
                )
            except Exception:
                # A child whose DNA fails to verify is the platform's
                # problem, not the parent's — log nothing here (caller has
                # audit context), surface via lineage edges and absence
                # from _darwins.
                return
            annotate_span_with_runtime(s, child_runtime)
            child_handle = _Handle(
                darwin_id=child_id,
                stack=child_stack,
                runtime=child_runtime,
                spawned_at=self._clock(),
                parent_id=parent.darwin_id,
                # Children inherit the parent's escalation policy so a
                # reproduction event doesn't silently lose the owner's
                # auto-approve / auto-deny preference. Owner attribution
                # is also inherited so the child's omnibus credit / sweep
                # operations attribute to the same user as the parent.
                owner_subject=parent.owner_subject,
                escalation_auto=parent.escalation_auto,
            )
            # Inherit owner pre-approvals from parent → child. The owner's
            # "Upwork under $50" intent should apply to the lineage, not
            # just the parent. The owner can revoke on the child after
            # spawn if they want narrower scope.
            self._inherit_preapprovals(parent, child_stack.wallet, child_id)
            self._darwins[child_id] = child_handle
            child_handle.task = asyncio.create_task(
                self._drive(child_handle), name=f"darwin:{child_id}"
            )
            child_handle.task.add_done_callback(self._log_drive_done)
            opening_usdc = float(record.get("budget_usdc", 0.0))
            self._audit.record(
                actor=f"platform.reproduction:{parent.darwin_id}",
                action="spawn",
                darwin_id=child_id,
                metadata={
                    "opening_usdc": opening_usdc,
                    "parent_id": parent.darwin_id,
                },
            )
            # Persist the child's lifecycle row so failover (and the
            # operator dashboard) see the same fleet the supervisor sees.
            # Doc 08: every spawn writes through, parent and child alike.
            if self._lifecycle is not None:
                try:
                    self._lifecycle.upsert_spawn(
                        darwin_id=child_id,
                        opening_usdc=opening_usdc,
                        parent_id=parent.darwin_id,
                        spawned_at=child_handle.spawned_at,
                        escalation_auto=child_handle.escalation_auto,
                    )
                except Exception:
                    pass
            # Persist the child's sealed manifest + layer blobs the same
            # way genesis spawns do, so a resume can re-mint identical DNA.
            if self._manifest_store is not None:
                try:
                    self._manifest_store.put(
                        darwin_id=child_id,
                        manifest_json=child_runtime._p.manifest.model_dump_json(),  # noqa: SLF001
                        layer_blobs=child_stack.layer_blobs,
                        layer_names=child_stack.layer_names,
                    )
                except Exception:
                    pass
