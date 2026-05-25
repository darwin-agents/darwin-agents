"""In-memory fakes for the external world. NOT part of the Layer 0 genome.

``make_platform`` wires the whole fake stack into the genome's ``Platform``
bundle so a test or the simulation can boot a real Darwin in one call.
"""

from __future__ import annotations

import time
from typing import Callable

from darwin_dna.evolution import Platform

from .fake_llm import FakeLLM
from .fake_platform import (
    FakeProvisioner,
    FakeSpawner,
    KillFlag,
    PauseFlag,
    MemoryAnchorStore,
    MemoryAuditSink,
    MemoryCheckpointStore,
    MemoryEscalationInbox,
    SigningService,
    mint_genesis,
)
from .fake_sandbox import FakeBrowser, FakeSandbox
from .fake_vault import FakeVault, MemoryOwnerGrantStore
from .fake_wallet import FakeWallet

__all__ = [
    "FakeLLM",
    "FakeBrowser",
    "FakeSandbox",
    "FakeVault",
    "MemoryOwnerGrantStore",
    "FakeWallet",
    "KillFlag",
    "PauseFlag",
    "MemoryAuditSink",
    "MemoryAnchorStore",
    "MemoryCheckpointStore",
    "MemoryEscalationInbox",
    "SigningService",
    "FakeProvisioner",
    "FakeSpawner",
    "mint_genesis",
    "FakeStack",
    "make_platform",
]


class FakeStack:
    """Holds references to every fake so a test can poke the world (flip the
    kill switch, post revenue, read the audit log) after boot."""

    def __init__(self, *, opening_usdc: float, clock: Callable[[], float],
                 captcha_every: int = 7, escalation_auto: str | None = "approve",
                 signer: SigningService | None = None,
                 llm=None):
        self.clock = clock
        # The platform's signing key. Real deployments have one HSM-held key;
        # callers (e.g. the supervisor hosting a fleet) inject the shared
        # instance so every Darwin's manifest is signed by the same key.
        # Default = own key, fine for single-Darwin tests.
        self.signer = signer or SigningService()
        # Optional injection point for a real LLM transport (e.g.
        # ``darwin_platform.adapters.LiteLLMTransport``). When None, the
        # deterministic FakeLLM is used so offline tests + the 90-day sim
        # remain bit-for-bit reproducible.
        self._llm_override = llm
        self.kill = KillFlag()
        self.pause = PauseFlag()
        self.audit_sink = MemoryAuditSink()
        self.anchor_store = MemoryAnchorStore()
        self.checkpoint = MemoryCheckpointStore()
        self.escalation = MemoryEscalationInbox(auto=escalation_auto)
        self.provision = FakeProvisioner()
        self.vault = FakeVault()
        self.vault_grants = MemoryOwnerGrantStore()
        self.wallet = FakeWallet(opening_usdc=opening_usdc, clock=clock)
        if self._llm_override is not None:
            self.llm = self._llm_override
        else:
            self.llm = FakeLLM(wallet=self.wallet)
        self.browser = FakeBrowser(wallet=self.wallet, captcha_every=captcha_every)
        self.spawner = FakeSpawner(parent_wallet=self.wallet)
        # Optional x402 HTTP client (doc 17). None in fakes — tests that want
        # to exercise the 402 path inject a stub via stack.x402_client before
        # calling stack.platform().
        self.x402_client = None
        # Sandbox substrate (doc 14). Default is the deterministic FakeSandbox
        # so the 90-day sim never touches a real e2b microVM. Tests that want
        # to drive the real adapter inject a stub via stack.sandbox.
        self.sandbox = FakeSandbox()
        # Episodic memory substrate (doc 03). Default None — the genome falls
        # back to its deterministic in-process EpisodicMemory (sim parity).
        # Supervisor/tests inject a real substrate (e.g. ``LanceDbEpisodicMemory``)
        # via ``stack.episodic_memory = ...`` before ``stack.platform()``.
        self.episodic_memory = None
        self.manifest, self.layer_blobs, self.layer_names, self.identity = (
            mint_genesis(self.signer, birth_ts=clock())
        )

    def platform(self) -> Platform:
        return Platform(
            manifest=self.manifest,
            layer_blobs=self.layer_blobs,
            layer_names=self.layer_names,
            kill_source=self.kill,
            pause_source=self.pause,
            audit_sink=self.audit_sink,
            anchor_store=self.anchor_store,
            llm_transport=self.llm,
            wallet=self.wallet,
            provision=self.provision,
            vault=self.vault,
            vault_grants=self.vault_grants,
            checkpoint=self.checkpoint,
            escalation=self.escalation,
            signer=self.signer,
            spawner=self.spawner,
            browser=self.browser,
            x402_client=self.x402_client,
            sandbox=self.sandbox,
            episodic_memory=self.episodic_memory,
        )


def make_platform(
    *, opening_usdc: float = 100.0, clock: Callable[[], float] = time.time,
    captcha_every: int = 7, escalation_auto: str | None = "approve",
    signer: SigningService | None = None,
    llm=None,
) -> tuple[Platform, FakeStack]:
    """One-call fake world. Returns (platform, stack); keep the stack to
    drive and inspect the world during a run. ``escalation_auto`` controls
    the unattended owner-response policy (default: approve). ``signer``
    overrides the per-stack key so a multi-Darwin host (e.g. the platform
    Supervisor) can share one platform-side signing identity. ``llm``
    overrides the default deterministic FakeLLM with a real adapter
    (e.g. ``darwin_platform.adapters.LiteLLMTransport``)."""
    stack = FakeStack(opening_usdc=opening_usdc, clock=clock,
                      captcha_every=captcha_every,
                      escalation_auto=escalation_auto,
                      signer=signer,
                      llm=llm)
    return stack.platform(), stack


def make_child_platform(
    *,
    signer,
    child_manifest_json: str,
    child_layer_blobs: dict,
    opening_usdc: float,
    clock: Callable[[], float] = time.time,
    captcha_every: int = 9,
) -> tuple[Platform, FakeStack]:
    """Boot the *next generation*: a fresh fake world that uses the sealed
    child DNA. Critically it keeps the SAME signer, so the child's manifest
    must verify against the platform key exactly as a real spawn would.
    """
    from darwin_dna.manifest import Manifest

    stack = FakeStack(opening_usdc=opening_usdc, clock=clock,
                      captcha_every=captcha_every)
    stack.signer = signer  # same platform key as the parent
    stack.manifest = Manifest.model_validate_json(child_manifest_json)
    stack.layer_blobs = {int(k): v for k, v in child_layer_blobs.items()}
    stack.layer_names = {
        e.index: e.name for e in stack.manifest.layers
    }
    stack.identity = None  # established from the verified manifest at boot
    return stack.platform(), stack
