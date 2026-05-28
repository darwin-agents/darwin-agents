"""In-memory platform control plane. NOT part of the genome.

Implements every platform-side port the genome's ``Platform`` bundle needs:
the kill flag, the append-only audit sink, the Merkle anchor store, the
checkpoint store, the escalation inbox, the HSM-stand-in signing service, the
provisioner, and the child spawner. Plus ``mint_genesis`` — the platform's
spawn endpoint behaviour: mint identity, seal Layer 0, sign the manifest.

The signing private key lives here and is never handed to the genome — the
genome only ever gets ``public_key_pem()`` and verifies.
"""

from __future__ import annotations

import time
from copy import deepcopy

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from darwin_dna._canonical import canonical_bytes
from darwin_dna._version import LAYER_0_VERSION
from darwin_dna.identity import Identity
from darwin_dna.layers import LAYER_0_LESSONS, serialize_lessons
from darwin_dna.manifest import (
    LayerEntry,
    Manifest,
    ManifestIdentity,
    layer_signed_payload,
    sha256_hex,
)


# -- kill switch -----------------------------------------------------------
class KillFlag:
    def __init__(self) -> None:
        self._killed = False
        self._reason: str | None = None

    def flip(self, reason: str = "operator kill") -> None:
        self._killed = True
        self._reason = reason

    def is_killed(self) -> bool:
        return self._killed

    def reason(self) -> str | None:
        return self._reason


# -- pause flag (resumable; NOT a kill) ------------------------------------
class PauseFlag:
    """Owner/supervisor pause. Unlike :class:`KillFlag` this is reversible:
    ``pause()`` then ``resume()`` and the same life continues."""

    def __init__(self) -> None:
        self._paused = False
        self._reason: str | None = None

    def pause(self, reason: str = "owner paused") -> None:
        self._paused = True
        self._reason = reason

    def resume(self) -> None:
        self._paused = False
        self._reason = None

    def is_paused(self) -> bool:
        return self._paused

    def reason(self) -> str | None:
        return self._reason


# -- audit sink (append-only) ---------------------------------------------
class MemoryAuditSink:
    def __init__(self) -> None:
        self._records: list[dict] = []

    def append(self, record: dict) -> None:
        # Append-only on purpose: no update, no delete. Deep-copied so a
        # later caller mutation cannot rewrite committed history.
        self._records.append(deepcopy(record))

    def read_all(self) -> list[dict]:
        return [deepcopy(r) for r in self._records]


# -- merkle anchor store ---------------------------------------------------
class MemoryAnchorStore:
    def __init__(self) -> None:
        self._roots: dict[tuple[str, str], str] = {}

    def put_root(self, darwin_id: str, day: str, root: str) -> None:
        self._roots[(darwin_id, day)] = root

    def get_root(self, darwin_id: str, day: str) -> str | None:
        return self._roots.get((darwin_id, day))


# -- checkpoint store ------------------------------------------------------
class MemoryCheckpointStore:
    def __init__(self) -> None:
        self._latest: dict[str, dict] = {}
        self.saves = 0

    def save(self, darwin_id: str, node: str, state: dict) -> None:
        self._latest[darwin_id] = {"node": node, "state": deepcopy(state)}
        self.saves += 1

    def load(self, darwin_id: str) -> dict | None:
        return self._latest.get(darwin_id)


# -- escalation inbox + owner response channel -----------------------------
class MemoryEscalationInbox:
    """Owner notification bus + supervisor response channel.

    ``auto`` lets an *unattended* sim/test progress deterministically as if
    an attentive owner answered every ticket the same way ("approve" models a
    prompt owner / fast captcha solving — the realistic baseline). Set
    ``auto=None`` to drive responses manually via :meth:`respond` (and to
    exercise timeouts by advancing the clock).
    """

    def __init__(self, auto: str | None = "approve") -> None:
        self.tickets: list[dict] = []
        self._auto = auto
        self._responses: list[dict] = []  # queued, drained by poll_responses

    def post(self, ticket: dict) -> None:
        self.tickets.append(deepcopy(ticket))
        if self._auto is not None:
            self._responses.append(
                {"ticket": ticket.get("ticket"), "decision": self._auto,
                 "data": {}}
            )

    def respond(self, ticket: str, decision: str, data: dict | None = None) -> None:
        """Owner (or a test) resolves a ticket: approve | deny | data."""
        self._responses.append(
            {"ticket": ticket, "decision": decision, "data": data or {}}
        )

    def poll_responses(self, darwin_id: str) -> list[dict]:
        out, self._responses = self._responses, []
        return [deepcopy(r) for r in out]


# -- signing service (HSM stand-in) ---------------------------------------
class SigningService:
    """Ed25519. The platform's birth-time signer. Private key never leaves
    this object.

    Construction options:
      - ``SigningService()`` — generate a fresh key (default).
      - ``SigningService.from_private_bytes(b)`` — load from a persisted
        raw 32-byte private key (used by HA resume so manifests signed
        before a process restart still verify against the same public
        key after the restart).
    """

    def __init__(self) -> None:
        self._sk = Ed25519PrivateKey.generate()

    @classmethod
    def from_private_bytes(cls, raw: bytes) -> "SigningService":
        inst = cls.__new__(cls)
        inst._sk = Ed25519PrivateKey.from_private_bytes(bytes(raw))
        return inst

    def sign(self, message: bytes) -> bytes:
        return self._sk.sign(bytes(message))

    def public_key_pem(self) -> bytes:
        return self._sk.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def private_bytes(self) -> bytes:
        """Raw 32-byte private key. Used by the platform's HA-resume
        persistence ONLY — never written to the audit chain or any
        log. The genome never sees this; it only ever holds the
        public key."""
        return self._sk.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )


# -- provisioner -----------------------------------------------------------
class FakeProvisioner:
    def __init__(self) -> None:
        self._n = 0

    def provision(self, kind: str, spec: dict) -> dict:
        self._n += 1
        return {"id": f"{kind}-{self._n}", "creds": f"fake-cred-{self._n}"}


# -- child spawner ---------------------------------------------------------
class FakeSpawner:
    """Provisions the child (records the lineage edge) and moves the starting
    budget out of the parent's treasury (the omnibus transfer).

    ``omnibus_destination`` is the on-chain address the parent's reproduction
    transfer should land at — the supervisor sets it to the
    :class:`OmnibusWallet` address when one is wired; left ``None`` the
    transfer falls back to the wallet's symbolic default (tests + the sim
    don't care where the bytes go)."""

    def __init__(self, parent_wallet, *, omnibus_destination: str | None = None) -> None:
        self._parent_wallet = parent_wallet
        self._omnibus_destination = omnibus_destination
        self.children: list[dict] = []

    def spawn(self, *, child_manifest: str, child_layer_blobs: dict,
              budget_usdc: float, parent_id: str) -> dict:
        if self._omnibus_destination is not None:
            self._parent_wallet.transfer_out_for_child(
                budget_usdc, destination=self._omnibus_destination,
            )
        else:
            self._parent_wallet.transfer_out_for_child(budget_usdc)
        record = {
            "parent_id": parent_id,
            "budget_usdc": budget_usdc,
            "layers": sorted(child_layer_blobs),
            "spawned_at": time.time(),
            # Retain the sealed child DNA so the platform can actually boot
            # the next generation (and tests can verify it verifies).
            "child_manifest": child_manifest,
            "child_layer_blobs": dict(child_layer_blobs),
        }
        self.children.append(record)
        return {"status": "spawned", "parent_id": parent_id,
                "budget_usdc": budget_usdc, "layers": record["layers"],
                "spawned_at": record["spawned_at"]}


# -- the spawn endpoint: mint + seal + sign genesis ------------------------
def mint_genesis(
    signer: SigningService, *, birth_ts: float | None = None
) -> tuple[Manifest, dict[int, bytes], dict[int, str], Identity]:
    """Behave like the platform's spawn endpoint for a generation-0 Darwin:
    mint identity, seal the (empty) Layer 0 lessons, sign the manifest."""
    bt = time.time() if birth_ts is None else float(birth_ts)
    identity = Identity(
        darwin_id=Identity.genesis(l0_version=LAYER_0_VERSION).darwin_id,
        generation=0,
        parent_id=None,
        birth_ts=bt,
        lineage=[],
        l0_version=LAYER_0_VERSION,
    )

    blob = serialize_lessons(LAYER_0_LESSONS)  # b"[]" — no inherited lessons
    name = "layer_0_lessons"
    digest = sha256_hex(blob)
    entry = LayerEntry(
        index=0,
        name=name,
        sha256=digest,
        signature=signer.sign(
            layer_signed_payload(
                darwin_id=identity.darwin_id,
                l0_version=identity.l0_version,
                index=0,
                name=name,
                sha256=digest,
            )
        ).hex(),
    )
    body = {
        "identity": ManifestIdentity(
            darwin_id=identity.darwin_id,
            generation=0,
            parent_id=None,
            birth_ts=bt,
            lineage=[],
            l0_version=identity.l0_version,
        ).model_dump(),
        "layers": [entry.model_dump()],
        "created_at": bt,
    }
    manifest = Manifest(**body, signature=signer.sign(canonical_bytes(body)).hex())
    return manifest, {0: blob}, {0: name}, identity
