"""Platform-side vault administration (docs/engineering/16 §"Bidirectional
shared vault" + §"KYC handling").

The genome's :class:`darwin_dna.machinery.vault.TieredVault` covers the
*read* model (public / scoped / sensitive). The *write* path that doc 16
describes ("the owner uploads KYC docs / credentials / payment methods")
lives outside the genome — the owner uploads through this admin surface,
and the platform writes directly into the per-Darwin namespace.

This module exposes:

- :class:`VaultAdminService` — owner-write / list / delete operations
  against any raw ``put/get`` vault backend, with audit-friendly metadata
  written as a sidecar key per upload.
- :class:`VaultRetentionService` — tracks expiry for sensitive-tier entries
  (default 90-day TTL from upload + every grant consumption refreshes the
  clock — doc 16 KYC retention rule). A periodic sweep returns the
  expired paths so the supervisor can purge them from the raw vault.

Both services are pure-Python and deterministic; the supervisor wires them
into the drive cycle (sweep every N heartbeats) and the HTTP layer
(POST/DELETE /darwins/{id}/vault).
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from ..security.watermark import NoOpWatermarker, Watermarker


# Doc 16: "retention 90 days after last use" for KYC and sensitive uploads.
DEFAULT_SENSITIVE_TTL_SECONDS: float = 90 * 86_400.0
DEFAULT_SCOPED_TTL_SECONDS: float | None = None  # no auto-expiry by default


class VaultAdminError(Exception):
    """Raised on invalid path, unknown key, or platform-side refusal."""


@dataclass(frozen=True)
class VaultEntryRef:
    """A pointer to one vault entry. We never carry the *value* through this
    object — operator audit + owner UI reads list of entries, not their
    contents."""

    darwin_id: str
    tier: str          # "public" | "scoped" | "sensitive"
    scope: str | None
    key: str
    path: str          # the full raw-vault path
    sha256: str | None  # hash of the stored value, useful for compliance
    size_bytes: int | None
    uploaded_at: float
    uploaded_by: str | None
    expires_at: float | None  # only set when the retention service tracks it
    watermarked: bool = False  # True iff the embedded HMAC stamp was added

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class _RetentionRecord:
    """Internal: tracks expiry for one sensitive vault entry."""

    darwin_id: str
    path: str
    tier: str
    ttl_seconds: float
    uploaded_at: float
    last_accessed_at: float

    @property
    def expires_at(self) -> float:
        return self.last_accessed_at + self.ttl_seconds


class VaultRetentionService:
    """Tracks per-path TTL for sensitive entries. The supervisor calls
    :meth:`sweep_expired` periodically and deletes the returned paths from
    the raw vault.

    Lifecycle:

    - :meth:`register` — called on owner upload. Sets the initial expiry
      based on TTL (default 90 days for sensitive tier).
    - :meth:`touch` — called when the entry is read (grant consumed).
      Resets the "last accessed" clock so the 90-day window restarts from
      the *last use*, matching doc 16.
    - :meth:`sweep_expired` — returns the paths whose clock has run out.
      The supervisor performs the actual delete.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        sensitive_ttl_seconds: float = DEFAULT_SENSITIVE_TTL_SECONDS,
    ) -> None:
        self._clock = clock
        self._sensitive_ttl = float(sensitive_ttl_seconds)
        # Keyed by (darwin_id, path). One record per sensitive entry.
        self._records: dict[tuple[str, str], _RetentionRecord] = {}

    def register(
        self,
        *,
        darwin_id: str,
        path: str,
        tier: str,
        ttl_seconds: float | None = None,
    ) -> _RetentionRecord:
        """Begin tracking. ``ttl_seconds=None`` uses the tier default."""
        ttl = self._tier_default(tier) if ttl_seconds is None else float(ttl_seconds)
        now = self._clock()
        rec = _RetentionRecord(
            darwin_id=darwin_id, path=path, tier=tier, ttl_seconds=ttl,
            uploaded_at=now, last_accessed_at=now,
        )
        self._records[(darwin_id, path)] = rec
        return rec

    def _tier_default(self, tier: str) -> float:
        if tier == "sensitive":
            return self._sensitive_ttl
        # Scoped/public: no auto-expiry by default. Caller may pass an
        # explicit ttl to force one (e.g. ephemeral session tokens).
        return float("inf")

    def touch(self, *, darwin_id: str, path: str) -> None:
        """Mark this entry as freshly read; resets the TTL window. Called
        whenever the genome consumes an owner grant for this path."""
        rec = self._records.get((darwin_id, path))
        if rec is not None:
            rec.last_accessed_at = self._clock()

    def unregister(self, *, darwin_id: str, path: str) -> None:
        """Drop tracking. Called on explicit delete; sweep does this too
        once it surfaces an expired record."""
        self._records.pop((darwin_id, path), None)

    def expires_at(self, *, darwin_id: str, path: str) -> float | None:
        rec = self._records.get((darwin_id, path))
        if rec is None or rec.ttl_seconds == float("inf"):
            return None
        return rec.expires_at

    def sweep_expired(self) -> list[_RetentionRecord]:
        """Return + forget every record whose clock has run out. The
        caller is responsible for actually deleting the paths from the
        raw vault — this service only knows about the metadata."""
        now = self._clock()
        expired: list[_RetentionRecord] = []
        for key, rec in list(self._records.items()):
            if rec.ttl_seconds == float("inf"):
                continue
            if rec.expires_at <= now:
                expired.append(rec)
                del self._records[key]
        return expired

    def all_records(self, darwin_id: str | None = None) -> list[_RetentionRecord]:
        if darwin_id is None:
            return list(self._records.values())
        return [r for r in self._records.values() if r.darwin_id == darwin_id]


# -- helpers ---------------------------------------------------------------
def _seg(s: str) -> str:
    """Mirror :func:`darwin_dna.machinery.vault._seg` so the platform writes
    to the exact same path the genome reads from. A key with '/' would
    otherwise smuggle scope/tier into its segment."""
    return str(s).replace("/", "_").strip()


def _build_path(*, darwin_id: str, tier: str, scope: str | None, key: str) -> str:
    if tier not in ("public", "scoped", "sensitive"):
        raise VaultAdminError(
            f"unknown tier {tier!r}; expected public|scoped|sensitive"
        )
    base = f"darwins/{_seg(darwin_id)}/{tier}"
    if tier == "scoped":
        return f"{base}/{_seg(scope or 'shared')}/{_seg(key)}"
    return f"{base}/{_seg(key)}"


def _sidecar_path(path: str) -> str:
    """The metadata sidecar key. Stores upload context (sha256, uploaded_by,
    original filename if provided) so an operator audit can correlate a
    stored blob with the upload that produced it."""
    return f"{path}.__meta__"


def _hash_value(value: str) -> str:
    import hashlib
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# -- the admin service -----------------------------------------------------
class VaultAdminService:
    """Owner-write / list / delete operations against a Darwin's raw vault.

    The constructor takes a *resolver* — a callable mapping a darwin_id to
    its raw vault backend — so the same admin service can sit in front of
    every Darwin in the fleet (each with its own per-Darwin namespace) and
    the operator doesn't have to plumb individual handles."""

    def __init__(
        self,
        *,
        vault_resolver: Callable[[str], Any],
        retention: VaultRetentionService | None = None,
        clock: Callable[[], float] = time.time,
        watermarker: Watermarker | None = None,
    ) -> None:
        self._resolve = vault_resolver
        self._retention = retention or VaultRetentionService(clock=clock)
        self._clock = clock
        # Doc 16: invisible watermark embedded into the document bytes
        # so leaked KYC docs are traceable to (darwin_id, upload_ts)
        # even when the audit chain is unreachable. Defaults to the
        # NoOp variant; production wires HmacWatermarker via Supervisor.
        self._watermarker: Watermarker = watermarker or NoOpWatermarker()
        # Per-Darwin index of what we wrote. The raw vault stores values
        # under their full paths; this index keeps the listing query cheap
        # (and avoids enumerating the underlying KV which a real provider
        # may charge for).
        self._index: dict[str, dict[str, VaultEntryRef]] = {}

    @property
    def retention(self) -> VaultRetentionService:
        return self._retention

    def put(
        self,
        *,
        darwin_id: str,
        tier: str,
        key: str,
        value: str,
        scope: str | None = None,
        uploaded_by: str | None = None,
        ttl_seconds: float | None = None,
        original_filename: str | None = None,
    ) -> VaultEntryRef:
        if not key:
            raise VaultAdminError("key is required")
        if value is None:
            raise VaultAdminError("value is required (use delete to remove)")
        path = _build_path(
            darwin_id=darwin_id, tier=tier, scope=scope, key=key,
        )
        vault = self._resolve(darwin_id)
        if vault is None:
            raise VaultAdminError(f"no vault for darwin {darwin_id!r}")
        # Write the value, then the sidecar metadata. Order matters: the
        # value is the source of truth; the sidecar is observable
        # provenance for the operator audit view. If the second write
        # fails, the value is still present and the next put() overwrites.
        uploaded_at = self._clock()
        # Watermark embedding (doc 16). Sensitive-tier uploads get the
        # invisible HMAC stamp; others pass through the NoOp by default.
        # The watermark record's hmac goes into the sidecar metadata so
        # forensics has the "expected" value at hand.
        stored_value = value
        wm_record = None
        if tier == "sensitive":
            try:
                value_bytes = value.encode("utf-8") if isinstance(value, str) else value
                marked, wm_record = self._watermarker.stamp(
                    value=value_bytes, darwin_id=darwin_id,
                    upload_ts=uploaded_at,
                    original_filename=original_filename,
                )
                stored_value = (
                    marked.decode("utf-8", errors="replace")
                    if isinstance(value, str) else marked
                )
            except Exception:
                # Watermarking must NEVER block an upload — the audit
                # sidecar is still the primary record.
                wm_record = None
        sha = _hash_value(value)  # hash of the ORIGINAL bytes for sidecar
        meta = {
            "sha256": sha,
            "size_bytes": len(value.encode("utf-8")),
            "uploaded_at": uploaded_at,
            "uploaded_by": uploaded_by or "unknown",
            "tier": tier,
            "scope": scope,
            "original_filename": original_filename,
            "watermark": wm_record.to_dict() if wm_record is not None else None,
        }
        vault.put(path, stored_value)
        # Sidecar is JSON for inspectability. Fail-soft: if the backend
        # doesn't accept the metadata write, the value still landed.
        try:
            import json
            vault.put(_sidecar_path(path), json.dumps(meta))
        except Exception:
            pass
        # Retention: track sensitive entries (and any with an explicit TTL).
        track = tier == "sensitive" or ttl_seconds is not None
        expires_at: float | None = None
        if track:
            rec = self._retention.register(
                darwin_id=darwin_id, path=path, tier=tier,
                ttl_seconds=ttl_seconds,
            )
            expires_at = (
                rec.expires_at if rec.ttl_seconds != float("inf") else None
            )
        ref = VaultEntryRef(
            darwin_id=darwin_id, tier=tier, scope=scope, key=key, path=path,
            sha256=sha, size_bytes=meta["size_bytes"],
            uploaded_at=meta["uploaded_at"],
            uploaded_by=meta["uploaded_by"],
            expires_at=expires_at,
            watermarked=wm_record is not None and wm_record.hmac_b64 != "",
        )
        self._index.setdefault(darwin_id, {})[path] = ref
        return ref

    def list(
        self,
        darwin_id: str,
        *,
        tier: str | None = None,
    ) -> list[VaultEntryRef]:
        rows = list(self._index.get(darwin_id, {}).values())
        # Refresh expires_at from retention (touch may have moved it).
        out: list[VaultEntryRef] = []
        for r in rows:
            if tier is not None and r.tier != tier:
                continue
            exp = self._retention.expires_at(darwin_id=r.darwin_id, path=r.path)
            if exp != r.expires_at:
                # Refresh the cached snapshot — touch() reset it.
                r = VaultEntryRef(
                    darwin_id=r.darwin_id, tier=r.tier, scope=r.scope,
                    key=r.key, path=r.path, sha256=r.sha256,
                    size_bytes=r.size_bytes, uploaded_at=r.uploaded_at,
                    uploaded_by=r.uploaded_by, expires_at=exp,
                    watermarked=r.watermarked,
                )
                self._index[darwin_id][r.path] = r
            out.append(r)
        return out

    def delete(
        self,
        *,
        darwin_id: str,
        tier: str,
        key: str,
        scope: str | None = None,
    ) -> VaultEntryRef:
        path = _build_path(
            darwin_id=darwin_id, tier=tier, scope=scope, key=key,
        )
        idx = self._index.get(darwin_id, {})
        ref = idx.get(path)
        if ref is None:
            raise VaultAdminError(
                f"no entry at {path!r}; refusing delete (would no-op silently)"
            )
        self._raw_delete(darwin_id=darwin_id, path=path)
        del idx[path]
        self._retention.unregister(darwin_id=darwin_id, path=path)
        return ref

    def _raw_delete(self, *, darwin_id: str, path: str) -> None:
        vault = self._resolve(darwin_id)
        if vault is None:
            raise VaultAdminError(f"no vault for darwin {darwin_id!r}")
        deleter = getattr(vault, "delete", None)
        if callable(deleter):
            deleter(path)
            try:
                deleter(_sidecar_path(path))
            except Exception:
                pass
        else:
            # Backend has no delete; overwrite with an empty string. Real
            # backends should implement delete; this branch is a fail-safe
            # so we never leave secret bytes lying around just because the
            # adapter is partial.
            vault.put(path, "")
            try:
                vault.put(_sidecar_path(path), "")
            except Exception:
                pass

    def touch_sensitive(self, *, darwin_id: str, path: str) -> None:
        """Convenience wrapper around the retention service. Used by the
        supervisor when an owner grant for this path is consumed by the
        genome — the read counts as "use" and refreshes the TTL clock."""
        self._retention.touch(darwin_id=darwin_id, path=path)

    def sweep_expired(self) -> list[VaultEntryRef]:
        """Delete every sensitive entry whose retention window has run out.
        Returns the refs that were purged (for the caller to log).

        The supervisor calls this on a heartbeat cadence."""
        purged: list[VaultEntryRef] = []
        for rec in self._retention.sweep_expired():
            idx = self._index.get(rec.darwin_id, {})
            ref = idx.pop(rec.path, None)
            try:
                self._raw_delete(darwin_id=rec.darwin_id, path=rec.path)
            except Exception:
                # Backend transient failure: re-register so the next sweep
                # tries again. Better to over-retain than to drop the audit
                # trail by silently forgetting an entry that's still in the
                # raw vault.
                self._retention.register(
                    darwin_id=rec.darwin_id, path=rec.path, tier=rec.tier,
                    ttl_seconds=rec.ttl_seconds,
                )
                continue
            if ref is not None:
                purged.append(ref)
        return purged
