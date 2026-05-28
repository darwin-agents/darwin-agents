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
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from darwin_dna.evolution import DarwinRuntime, boot
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


# -- per-Darwin handle -----------------------------------------------------
@dataclass
class _Handle:
    """Internal: every running Darwin's bookkeeping. Not exposed."""

    darwin_id: str
    stack: FakeStack
    runtime: DarwinRuntime
    spawned_at: float
    parent_id: str | None = None
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
        publishing_endpoints: dict[str, str] | None = None,
        analytics_endpoint: str | None = None,
        kyc_service: KYCService | None = None,
        omnibus_wallet: OmnibusWallet | None = None,
        namespace_provisioner: NamespaceProvisioner | None = None,
        drift_detector: DriftDetector | None = None,
        merkle_anchor_publisher: MerkleAnchorPublisher | None = None,
        maintenance_interval_seconds: float = 86400.0,
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
        self._namespace_provisioner: NamespaceProvisioner | None = (
            namespace_provisioner
        )
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
        # Twilio SMS inbox for inbound 2FA codes (doc 15). None = the
        # platform has no shared phone number; accounts.manager escalates
        # every code to the owner instead of the platform auto-answering.
        self._sms_client: Any | None = sms_client
        # Non-LLM resource throttles (doc 09 level 2). The LLM tier
        # throttle lives on the genome's LLMRouter; sandbox / browser /
        # egress live here because their substrates are platform-side.
        self._throttle_service = ThrottleService(clock=clock)
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
        # Background reconciler: when leadership flips, cancel any
        # running drive tasks (we lost the lock and shouldn't keep
        # writing). On gain, we don't auto-resume previously-spawned
        # Darwins — the operator re-spawns via the API once readiness
        # routes traffic back to this pod.
        loop = asyncio.get_event_loop()
        self._leader_watch_task = loop.create_task(self._watch_leadership())

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
            if self._llm_factory is not None:
                # Replace the default FakeLLM with the real adapter. The
                # wallet is already bound; the factory only needs a clock
                # for its own time concerns (timeouts, day rollovers).
                stack.llm = self._llm_factory(stack.wallet, self._clock)
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
            runtime = boot(stack.platform(), sleep=lambda _: None)
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
            handle = _Handle(
                darwin_id=runtime.identity.darwin_id,
                stack=stack,
                runtime=runtime,
                spawned_at=self._clock(),
                parent_id=parent_id,
                escalation_auto=escalation_auto,
            )
            self._darwins[handle.darwin_id] = handle
            annotate_span_with_runtime(s, runtime)
            handle.task = asyncio.create_task(
                self._drive(handle), name=f"darwin:{handle.darwin_id}"
            )
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

    def reconcile_omnibus(self) -> ReconciliationResult | None:
        """Daily reconciliation hook. Returns None when no omnibus is
        wired (single-tenant dev). The result carries the discrepancy +
        a pass/fail flag the ops monitor consumes."""
        if self._omnibus_wallet is None:
            return None
        sum_wallets = 0.0
        for h in self._darwins.values():
            try:
                sum_wallets += float(h.stack.wallet.balance_usdc())
            except Exception:
                continue
        return self._omnibus_wallet.reconcile(
            sum_of_darwin_wallets=sum_wallets,
        )

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
    async def kill(
        self,
        darwin_id: str,
        *,
        reason: str = "owner kill",
        actor: str = "anonymous",
    ) -> None:
        """Flip the platform kill flag. The genome polls it at the loop
        boundary and at every node boundary (doc 09: ≤5s latency), then runs
        ``_halt_on_kill`` which writes the final audit record and anchors."""
        if self._is_k8s(darwin_id):
            await self._k8s_driver.kill(darwin_id, reason=reason)
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
            self._audit.record(
                actor=actor, action="kill", darwin_id=darwin_id,
                metadata={"reason": reason},
            )
            if self._lifecycle is not None:
                try:
                    self._lifecycle.mark_killed(darwin_id, reason=reason)
                except Exception:
                    pass

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
        h = self._get(darwin_id)
        all_records = h.stack.audit_sink.read_all()
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
        spec = DarwinSpec(
            darwin_id=supervisor_id,
            opening_usdc=float(opening_usdc),
            escalation_auto=escalation_auto,
            parent_id=parent_id,
        )
        with span(
            "supervisor.spawn_k8s",
            attributes={
                "opening_usdc": float(opening_usdc),
                "parent_id": parent_id or "",
                "darwin_id": supervisor_id,
            },
        ):
            snapshot = await self._k8s_driver.start(spec)
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
            try:
                roots = self.collect_daily_merkle_roots(day_label)
                if roots:
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
        if self._omnibus_wallet is not None:
            try:
                result = self.reconcile_omnibus()
                if result is not None:
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
            except Exception as exc:  # noqa: BLE001
                self._audit.record(
                    actor="platform.maintenance",
                    action="omnibus_reconcile_failed",
                    metadata={"reason": str(exc)[:200]},
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

    # -- fiat ramp (doc 17) ------------------------------------------------
    def fiat_deposit_quote(
        self,
        *,
        amount_usd: float,
        payment_method: str,
        actor: str = "anonymous",
    ) -> FiatQuote:
        if self._fiat_ramp is None:
            raise SupervisorError("fiat ramp is not configured")
        try:
            quote = self._fiat_ramp.deposit_quote(
                amount_usd=amount_usd, payment_method=payment_method,
            )
        except FiatRampError as exc:
            raise SupervisorError(str(exc)) from exc
        self._audit.record(
            actor=actor,
            action="fiat_deposit_quote",
            darwin_id=None,
            metadata={
                "amount_usd": float(amount_usd),
                "provider": quote.provider,
                "quote_id": quote.quote_id,
            },
        )
        return quote

    def fiat_confirm_deposit(
        self,
        *,
        quote_id: str,
        destination_darwin_id: str | None = None,
        actor: str = "anonymous",
    ) -> FiatDepositReceipt:
        if self._fiat_ramp is None:
            raise SupervisorError("fiat ramp is not configured")
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
                child_runtime = boot(child_platform, sleep=lambda _: None)
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
                # auto-approve / auto-deny preference.
                escalation_auto=parent.escalation_auto,
            )
            self._darwins[child_id] = child_handle
            child_handle.task = asyncio.create_task(
                self._drive(child_handle), name=f"darwin:{child_id}"
            )
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
