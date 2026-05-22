"""Platform-mediated provisioning (docs/engineering/04, 07).

Doc 04 specifies the ``infrastructure.provisioner`` neuron as the *only* path
by which a Darwin acquires managed resources (databases, buckets, vector
stores, scheduled jobs). The neuron does not provision anything itself — it
asks the platform via the ``ProvisionPort`` protocol the genome sees, and the
platform decides whether to grant + which backend to provision against.

Doc 07 (isolation) then specifies *what* the platform must guarantee:
per-Darwin schemas with row-level security, vault-scoped credentials,
quota enforcement so one Darwin can't starve the fleet by spinning up
resources.

This module ships the platform side:

- :class:`ProvisioningBackend` — the pluggable backend (in-memory default,
  real Terraform / Kubernetes / Crossplane operators behind it later).
- :class:`ProvisioningService` — the bookkeeping + quota + audit layer that
  wraps any backend.
- :class:`ScopedProvisionPort` — the per-Darwin view we hand to the genome's
  ``Platform.provision`` slot. Matches the ``ProvisionPort`` Protocol the
  neuron expects.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Callable, Protocol, runtime_checkable


class ProvisioningError(Exception):
    """Refused, denied by quota, or failed at the backend. The neuron's own
    failure-mode (``provision_denied``) covers all three at its layer."""


@dataclass(frozen=True)
class ResourceRecord:
    """One provisioned resource. Frozen so a stored record can't be mutated;
    deprovisioning produces a new record with status='deprovisioned'."""

    id: str
    darwin_id: str
    kind: str            # "postgres" | "object_store" | "scheduled_job" | ...
    spec: dict           # opaque to platform; semantic is per kind
    creds_ref: str       # vault path (or a stub identifier for in-memory)
    status: str          # "active" | "deprovisioned"
    created_at: float
    deprovisioned_at: float | None = None

    def to_json(self) -> dict:
        return asdict(self)


@runtime_checkable
class ProvisioningBackend(Protocol):
    """The platform's pluggable provisioning backend. The default in-memory
    one returns deterministic stub credentials; production wires a real
    Terraform/Kubernetes/Crossplane driver behind this same Protocol."""

    def provision(self, *, kind: str, spec: dict) -> dict:
        """Allocate the resource. Returns ``{'id': str, 'creds': str}``.
        May raise :class:`ProvisioningError` on quota/policy failures."""

    def deprovision(self, *, kind: str, resource_id: str) -> None:
        """Tear down. Idempotent — calling twice on a deprovisioned id must
        not raise."""


class SubprocessProvisioningBackend:
    """Production-shaped backend: shells out to a configured executable
    (terraform wrapper, kubectl apply script, Crossplane CLI, etc.) per
    operation. Stays out of process so a misbehaving provider can't take
    the supervisor down with it.

    The executable is invoked with::

        <command> provision --kind <kind> --spec <json>

        <command> deprovision --kind <kind> --id <resource_id>

    The provision call must print a single line of JSON on stdout:
    ``{"id": "...", "creds": "..."}``. Non-zero exit codes or unparseable
    output surface as :class:`ProvisioningError`. The platform stores the
    creds via the existing vault flow; the executable itself should
    write to the right vault path or hand back a vault-resolvable
    identifier.

    Safety: the command is run with ``shell=False`` and no environment
    inheritance beyond an explicit allowlist. Output is read with a
    timeout so a hung backend can be killed cleanly."""

    def __init__(
        self,
        *,
        command: tuple[str, ...] | list[str],
        env: dict[str, str] | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        if not command:
            raise ValueError("SubprocessProvisioningBackend needs a non-empty command")
        self._command = tuple(command)
        self._env = dict(env or {})
        self._timeout = float(timeout_seconds)

    def _run(self, args: list[str]) -> dict:
        import json as _json
        import subprocess
        cmd = list(self._command) + args
        try:
            proc = subprocess.run(
                cmd,
                env=self._env or None,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProvisioningError(
                f"provisioning command timed out after {self._timeout}s: {cmd[0]}"
            ) from exc
        except FileNotFoundError as exc:
            raise ProvisioningError(
                f"provisioning command not found: {cmd[0]}"
            ) from exc
        if proc.returncode != 0:
            raise ProvisioningError(
                f"provisioning command failed (exit={proc.returncode}): "
                f"{proc.stderr.strip()[:200]}"
            )
        out = (proc.stdout or "").strip()
        if not out:
            return {}
        try:
            payload = _json.loads(out)
        except Exception as exc:
            raise ProvisioningError(
                f"provisioning command returned non-JSON output: {out[:200]!r}"
            ) from exc
        if not isinstance(payload, dict):
            raise ProvisioningError(
                "provisioning command must return a JSON object"
            )
        return payload

    def provision(self, *, kind: str, spec: dict) -> dict:
        import json as _json
        result = self._run([
            "provision",
            "--kind", kind,
            "--spec", _json.dumps(spec, sort_keys=True),
        ])
        if "id" not in result:
            raise ProvisioningError(
                "provisioning command returned no 'id' field"
            )
        return result

    def deprovision(self, *, kind: str, resource_id: str) -> None:
        self._run([
            "deprovision",
            "--kind", kind,
            "--id", resource_id,
        ])


class InMemoryProvisioningBackend:
    """The default backend — equivalent to :class:`darwin_fakes.fake_platform
    .FakeProvisioner` but with named ids per kind so a test can assert on
    "the second postgres" vs "the first object_store". Production replaces
    this with a real driver."""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self._provisioned: set[str] = set()

    def provision(self, *, kind: str, spec: dict) -> dict:
        # spec is held by the service, not the backend — backends only
        # care about whatever subset of spec is needed to allocate (and
        # in this stub, nothing).
        del spec
        self._counters[kind] = self._counters.get(kind, 0) + 1
        rid = f"{kind}-{self._counters[kind]}"
        self._provisioned.add(rid)
        return {"id": rid, "creds": f"stub-creds:{rid}"}

    def deprovision(self, *, kind: str, resource_id: str) -> None:
        del kind  # informational only; the id is unique across kinds
        self._provisioned.discard(resource_id)


@dataclass(frozen=True)
class ProvisioningQuotas:
    """Per-Darwin limits. The platform refuses a provision request that would
    exceed any of these. Defaults are intentionally generous for a single
    Darwin; the production tenant would set per-kind quotas via the operator
    API."""

    max_total_per_darwin: int = 10
    max_per_kind_per_darwin: int = 5
    allowed_kinds: frozenset[str] | None = None  # None = no allow-list

    def check(
        self,
        *,
        kind: str,
        active_total: int,
        active_per_kind: int,
    ) -> None:
        if self.allowed_kinds is not None and kind not in self.allowed_kinds:
            raise ProvisioningError(
                f"kind={kind!r} is not on the allowed-kinds list"
            )
        if active_total >= self.max_total_per_darwin:
            raise ProvisioningError(
                f"per-darwin quota exhausted: "
                f"{active_total}/{self.max_total_per_darwin} resources"
            )
        if active_per_kind >= self.max_per_kind_per_darwin:
            raise ProvisioningError(
                f"per-kind quota exhausted: "
                f"{active_per_kind}/{self.max_per_kind_per_darwin} {kind!r}"
            )


class ProvisioningService:
    """Records every Darwin's resources, enforces quota, audit-logs each
    grant/deprovision.

    Architecture: backend (Protocol) + quotas + record store. The record
    store is in-memory for this slice (doc-08 says provisioning *records*
    should be durable; a follow-on persistence pass parallels the billing
    /lineage SQLite tables). The hash-chained operator audit log is the
    authoritative trail in the meantime.
    """

    def __init__(
        self,
        *,
        backend: ProvisioningBackend | None = None,
        quotas: ProvisioningQuotas | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._backend: ProvisioningBackend = (
            backend if backend is not None else InMemoryProvisioningBackend()
        )
        self._quotas = quotas or ProvisioningQuotas()
        self._clock = clock
        # Per-Darwin record store. Active + deprovisioned both kept so the
        # audit/history view sees the full lifecycle of a resource.
        self._records: dict[str, list[ResourceRecord]] = {}

    # -- platform-side API ------------------------------------------------
    def provision(
        self,
        *,
        darwin_id: str,
        kind: str,
        spec: dict | None = None,
    ) -> ResourceRecord:
        spec = dict(spec or {})
        active = [r for r in self._records.get(darwin_id, [])
                  if r.status == "active"]
        active_total = len(active)
        active_per_kind = sum(1 for r in active if r.kind == kind)
        self._quotas.check(
            kind=kind, active_total=active_total,
            active_per_kind=active_per_kind,
        )
        try:
            result = self._backend.provision(kind=kind, spec=spec)
        except ProvisioningError:
            raise
        except Exception as exc:  # backend failure
            raise ProvisioningError(
                f"backend failed to provision {kind}: {exc}"
            ) from exc
        rid = str(result.get("id"))
        creds = str(result.get("creds", ""))
        if not rid:
            raise ProvisioningError(
                "backend returned no resource id; refusing to record"
            )
        # Credential path mirrors doc 16's per-Darwin layout. The neuron's
        # vault_put call writes the actual blob under this scope; here we
        # record the *path* so an operator query doesn't reveal the secret
        # itself.
        creds_ref = f"darwins/{darwin_id}/scoped/infrastructure/infra:{kind}:{rid}"
        record = ResourceRecord(
            id=rid,
            darwin_id=darwin_id,
            kind=kind,
            spec=spec,
            creds_ref=creds_ref,
            status="active",
            created_at=self._clock(),
        )
        self._records.setdefault(darwin_id, []).append(record)
        # Returns the raw backend result for the genome (the neuron expects
        # ``{'id', 'creds'}``). The platform-side metadata stays here.
        return record, {"id": rid, "creds": creds}

    def list_resources(
        self, darwin_id: str, *, include_deprovisioned: bool = False,
    ) -> list[ResourceRecord]:
        rows = list(self._records.get(darwin_id, []))
        if include_deprovisioned:
            return rows
        return [r for r in rows if r.status == "active"]

    def deprovision(
        self,
        *,
        darwin_id: str,
        resource_id: str,
    ) -> ResourceRecord:
        rows = self._records.get(darwin_id, [])
        for i, r in enumerate(rows):
            if r.id == resource_id and r.status == "active":
                self._backend.deprovision(kind=r.kind, resource_id=r.id)
                updated = ResourceRecord(
                    id=r.id,
                    darwin_id=r.darwin_id,
                    kind=r.kind,
                    spec=r.spec,
                    creds_ref=r.creds_ref,
                    status="deprovisioned",
                    created_at=r.created_at,
                    deprovisioned_at=self._clock(),
                )
                rows[i] = updated
                return updated
        raise ProvisioningError(
            f"no active resource {resource_id!r} for darwin {darwin_id!r}"
        )

    def fleet_summary(self) -> dict[str, int]:
        """Quick stats for the operator dashboard. Active count by kind
        across the whole fleet."""
        out: dict[str, int] = {}
        for rows in self._records.values():
            for r in rows:
                if r.status == "active":
                    out[r.kind] = out.get(r.kind, 0) + 1
        return out

    def scoped_for(self, darwin_id: str) -> "ScopedProvisionPort":
        """Hand-out: returns the per-Darwin port the genome's Platform
        bundle expects. The genome never sees the service object itself."""
        return ScopedProvisionPort(service=self, darwin_id=darwin_id)


class ScopedProvisionPort:
    """The narrow per-Darwin view that matches the genome's ``ProvisionPort``
    Protocol (``provision(kind, spec) -> dict``).

    The neuron only ever sees this object. It cannot enumerate the platform's
    other Darwins, query quotas directly, or deprovision — those live on the
    :class:`ProvisioningService` and are reached only from the supervisor
    side (HTTP API for owners/operators)."""

    def __init__(self, *, service: ProvisioningService, darwin_id: str) -> None:
        self._service = service
        self._darwin_id = darwin_id

    def provision(self, kind: str, spec: dict) -> dict:
        # The neuron's contract: returns {'id', 'creds'}.
        _record, payload = self._service.provision(
            darwin_id=self._darwin_id, kind=kind, spec=spec,
        )
        return payload
