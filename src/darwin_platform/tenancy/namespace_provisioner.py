"""Per-Darwin namespace lifecycle (docs/engineering/22 §"Per-Darwin Kubernetes namespaces").

The K8s manifests + driver in :mod:`darwin_platform.k8s` already
provision the per-Darwin K8s namespace, NetworkPolicy, and orchestrator
pod. Phase 2 adds two parallel namespaces that share the K8s namespace's
lifecycle (create together, tear down together):

* **Vault Enterprise namespace** (doc 22 §"Vault Enterprise namespaces"):
  ``vault/darwin-{id}`` with KV v2 + Transit mounts. Per-Darwin AppRole
  + ACL policies matching capability tiers.
* **Organism database** (doc 22 §"Per-Darwin databases"): a fresh
  Postgres database on the shared organism Aurora cluster, with a
  dedicated role + the connection string written into the Darwin's
  vault namespace under ``tier=scoped``.

The :class:`NamespaceProvisioner` orchestrates these three steps. It
takes Protocols for each backend so tests pass stubs and production
wires real clients (kubernetes-python-client, hvac, asyncpg). On
spawn, all three are provisioned atomically (any failure rolls back
the partial state). On death, all three are torn down.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol, runtime_checkable


class ProvisioningError(Exception):
    """One step of the per-Darwin namespace lifecycle failed. The
    provisioner attempts a best-effort rollback before re-raising."""


class TenantNamespaceStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    # The Darwin was killed but its data is RETAINED until teardown_due_ts
    # (doc 09 commitment #3: data survives ≥90 days post-death so the owner
    # can inspect it / a re-spawn can inherit accounts + databases).
    DECEASED = "deceased"
    TEARDOWN = "teardown"
    ERROR = "error"


@dataclass
class TenantNamespace:
    """A per-Darwin namespace bundle: K8s, Vault, and organism DB
    handles, all sharing one lifecycle.

    ``wallet_addresses`` is populated when the provisioner is configured
    with :class:`WalletKeypairMinter` instances — one entry per chain,
    mapping the chain name to the on-chain address the minted keypair
    controls. Empty when no minters are configured (lazy wallet-factory
    provisioning stays the in-process default)."""

    darwin_id: str
    k8s_namespace: str
    vault_namespace: str
    organism_db_name: str
    organism_db_role: str
    organism_db_connection_ref: str  # vault path where the DSN lives
    status: TenantNamespaceStatus = TenantNamespaceStatus.PENDING
    created_at: float = 0.0
    # When set (status == DECEASED), the namespace + organism DB are retained
    # until this wall-clock time; the reaper only then runs the destructive
    # teardown (doc 09 #3).
    teardown_due_ts: float | None = None
    last_error: str = ""
    wallet_addresses: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "darwin_id": self.darwin_id,
            "k8s_namespace": self.k8s_namespace,
            "vault_namespace": self.vault_namespace,
            "organism_db_name": self.organism_db_name,
            "organism_db_role": self.organism_db_role,
            "organism_db_connection_ref": self.organism_db_connection_ref,
            "status": self.status.value,
            "created_at": self.created_at,
            "last_error": self.last_error,
            "wallet_addresses": dict(self.wallet_addresses),
            "metadata": dict(self.metadata),
        }


@runtime_checkable
class K8sNamespaceBackend(Protocol):
    """Minimal surface the provisioner needs for the K8s side.

    Implementations may wrap :class:`K8sDarwinDriver` or call the
    kubernetes-python-client directly. The provisioner does not own
    the orchestrator pod — that's the driver's job; it only owns the
    *namespace* + NetworkPolicy + RBAC scaffolding."""

    def create_namespace(
        self, *, darwin_id: str, labels: dict[str, str] | None = None,
    ) -> str:
        """Returns the namespace name."""
        ...

    def apply_network_policy(self, *, namespace: str) -> None: ...

    def delete_namespace(self, *, namespace: str) -> None: ...


@runtime_checkable
class VaultNamespaceBackend(Protocol):
    """Vault Enterprise namespace operations. Open-source Vault has no
    native namespaces; tests + dev wire the
    :class:`StubVaultNamespaceBackend` below, which is a path-based
    shim with the same surface."""

    def create_namespace(
        self, *, darwin_id: str,
    ) -> str:
        """Returns the namespace path (e.g. ``"darwin-id123"``)."""
        ...

    def write_secret(
        self, *, namespace: str, path: str, value: dict[str, Any],
    ) -> None: ...

    def issue_approle(
        self, *, namespace: str, role_name: str,
    ) -> dict[str, str]:
        """Returns ``{"role_id": str, "secret_id": str}``."""
        ...

    def delete_namespace(self, *, namespace: str) -> None: ...


@runtime_checkable
class OrganismDbBackend(Protocol):
    """Per-Darwin Postgres database provisioning on the shared organism
    cluster. Real impl runs CREATE DATABASE + CREATE ROLE against the
    cluster admin role; tests use the in-memory stub."""

    def create_database(
        self, *, darwin_id: str,
    ) -> tuple[str, str, str]:
        """Returns ``(database_name, role_name, dsn)`` where ``dsn`` is
        the full Postgres connection string with the new role's
        credentials embedded."""
        ...

    def drop_database(
        self, *, database_name: str, role_name: str,
    ) -> None: ...


# ---------------------------------------------------------------------------
# Stub backends (tests + the 90-day sim)
# ---------------------------------------------------------------------------
class StubK8sBackend:
    """In-memory K8s backend. Tracks every namespace it has been asked
    to create + every NetworkPolicy applied — tests assert on the
    sequence."""

    def __init__(self, *, prefix: str = "darwin-") -> None:
        self._prefix = prefix
        self.namespaces: list[str] = []
        self.policies: list[str] = []
        self.deletions: list[str] = []

    def create_namespace(self, *, darwin_id: str, labels=None) -> str:
        ns = f"{self._prefix}{darwin_id}"[:63].lower()
        self.namespaces.append(ns)
        return ns

    def apply_network_policy(self, *, namespace: str) -> None:
        self.policies.append(namespace)

    def delete_namespace(self, *, namespace: str) -> None:
        self.deletions.append(namespace)


class StubVaultNamespaceBackend:
    """In-memory vault namespace backend. Each "namespace" is just a
    dict scope; secrets live in a per-namespace map. Lets the
    provisioner exercise full lifecycle without a real Vault."""

    def __init__(self) -> None:
        self.namespaces: dict[str, dict[str, Any]] = {}
        self.deletions: list[str] = []

    def create_namespace(self, *, darwin_id: str) -> str:
        ns = f"darwin-{darwin_id}"
        if ns in self.namespaces:
            raise ProvisioningError(f"vault namespace {ns} already exists")
        self.namespaces[ns] = {"secrets": {}, "approles": {}}
        return ns

    def write_secret(self, *, namespace, path, value):
        if namespace not in self.namespaces:
            raise ProvisioningError(f"unknown vault namespace {namespace}")
        self.namespaces[namespace]["secrets"][path] = dict(value)

    def issue_approle(self, *, namespace, role_name):
        if namespace not in self.namespaces:
            raise ProvisioningError(f"unknown vault namespace {namespace}")
        role_id = f"role-{role_name}-{secrets.token_hex(4)}"
        secret_id = secrets.token_hex(16)
        self.namespaces[namespace]["approles"][role_name] = {
            "role_id": role_id, "secret_id": secret_id,
        }
        return {"role_id": role_id, "secret_id": secret_id}

    def delete_namespace(self, *, namespace):
        self.namespaces.pop(namespace, None)
        self.deletions.append(namespace)


class StubOrganismDbBackend:
    """In-memory organism-DB backend. Returns deterministic database +
    role names; the DSN is symbolic."""

    def __init__(self, *, dsn_template: str = "postgresql://{role}:{secret}@db/{database}") -> None:
        self._template = dsn_template
        self.databases: dict[str, dict[str, str]] = {}
        self.dropped: list[str] = []

    def create_database(self, *, darwin_id: str):
        db = f"organism_{darwin_id}".replace("-", "_")[:63]
        role = f"role_{darwin_id}".replace("-", "_")[:63]
        secret_val = secrets.token_urlsafe(16)
        dsn = self._template.format(role=role, secret=secret_val, database=db)
        self.databases[db] = {"role": role, "secret": secret_val, "dsn": dsn}
        return (db, role, dsn)

    def drop_database(self, *, database_name, role_name):
        self.databases.pop(database_name, None)
        self.dropped.append(database_name)


# ---------------------------------------------------------------------------
# The provisioner
# ---------------------------------------------------------------------------
class NamespaceProvisioner:
    """Atomic create-and-teardown of the three per-Darwin tenant
    namespaces.

    Lifecycle:

    ::

        provisioner.provision(darwin_id="d-1")  # K8s + Vault + DB
        ...
        provisioner.teardown(darwin_id="d-1")   # reverse order

    Idempotent on ``provision`` (re-provisioning an existing Darwin
    returns the cached :class:`TenantNamespace`).
    """

    def __init__(
        self,
        *,
        k8s: K8sNamespaceBackend,
        vault: VaultNamespaceBackend,
        organism_db: OrganismDbBackend,
        clock: Callable[[], float] = time.time,
        audit_recorder: Callable[[str, str, dict[str, Any]], None] | None = None,
        wallet_keypair_minters: dict[str, Any] | None = None,
    ) -> None:
        self._k8s = k8s
        self._vault = vault
        self._db = organism_db
        self._clock = clock
        self._recorder = audit_recorder
        # Optional per-chain keypair minters (doc 17: per-Darwin self-custodial
        # wallets). When set, provision() mints a fresh keypair per chain and
        # seals the secret in the per-Darwin vault namespace. Keys: chain name
        # ("solana", "stellar", "base"); values: WalletKeypairMinter for that
        # chain. None keeps the legacy behaviour (no wallet provisioning at
        # this layer; the lazy wallet-factory path handles it instead).
        self._wallet_minters: dict[str, Any] = dict(wallet_keypair_minters or {})
        self._tenants: dict[str, TenantNamespace] = {}

    def provision(
        self,
        *,
        darwin_id: str,
        actor: str = "platform.supervisor",
        labels: dict[str, str] | None = None,
    ) -> TenantNamespace:
        if darwin_id in self._tenants:
            return self._tenants[darwin_id]

        # Step 1: K8s namespace + NetworkPolicy.
        try:
            ns = self._k8s.create_namespace(darwin_id=darwin_id, labels=labels)
            self._k8s.apply_network_policy(namespace=ns)
        except Exception as exc:
            self._audit("tenant_provision_failed", actor, {
                "darwin_id": darwin_id, "step": "k8s", "reason": str(exc),
            })
            raise ProvisioningError(f"k8s step failed: {exc}") from exc

        # Step 2: Vault namespace + AppRole for the orchestrator pod.
        try:
            vault_ns = self._vault.create_namespace(darwin_id=darwin_id)
            approle = self._vault.issue_approle(
                namespace=vault_ns, role_name="orchestrator",
            )
        except Exception as exc:
            self._k8s.delete_namespace(namespace=ns)
            self._audit("tenant_provision_failed", actor, {
                "darwin_id": darwin_id, "step": "vault", "reason": str(exc),
            })
            raise ProvisioningError(f"vault step failed: {exc}") from exc

        # Step 3: organism database + DSN written into the vault.
        try:
            db_name, db_role, dsn = self._db.create_database(darwin_id=darwin_id)
            connection_ref = "kv/organism/dsn"
            self._vault.write_secret(
                namespace=vault_ns, path=connection_ref,
                value={"dsn": dsn, "role": db_role, "database": db_name},
            )
        except Exception as exc:
            # Rollback the prior steps. Failures in rollback are
            # best-effort — the operator is alerted via the audit log.
            self._safe(lambda: self._vault.delete_namespace(namespace=vault_ns))
            self._safe(lambda: self._k8s.delete_namespace(namespace=ns))
            self._audit("tenant_provision_failed", actor, {
                "darwin_id": darwin_id, "step": "organism_db",
                "reason": str(exc),
            })
            raise ProvisioningError(f"organism_db step failed: {exc}") from exc

        # Step 4 (optional): per-chain wallet keypair mint + seal. Each
        # configured chain gets a fresh keypair; the secret + address are
        # written into the per-Darwin vault namespace at ``kv/wallet/<chain>``.
        # The address is surfaced on the TenantNamespace so the supervisor
        # can hand it to omnibus.fund_child or use it as a transfer-out
        # destination without re-reading the secret.
        wallet_addresses: dict[str, str] = {}
        if self._wallet_minters:
            try:
                for chain, minter in self._wallet_minters.items():
                    if getattr(minter, "chain", None) != chain:
                        raise ProvisioningError(
                            f"minter for {chain!r} declares chain "
                            f"{getattr(minter, 'chain', None)!r}",
                        )
                    kp = minter.mint()
                    self._vault.write_secret(
                        namespace=vault_ns,
                        path=f"kv/wallet/{chain}",
                        value={"secret": kp.secret, "address": kp.address},
                    )
                    wallet_addresses[chain] = kp.address
            except Exception as exc:
                self._safe(lambda: self._vault.delete_namespace(namespace=vault_ns))
                self._safe(lambda: self._db.drop_database(
                    database_name=db_name, role_name=db_role,
                ))
                self._safe(lambda: self._k8s.delete_namespace(namespace=ns))
                self._audit("tenant_provision_failed", actor, {
                    "darwin_id": darwin_id, "step": "wallet_keypair",
                    "reason": str(exc),
                })
                raise ProvisioningError(
                    f"wallet_keypair step failed: {exc}"
                ) from exc

        tenant = TenantNamespace(
            darwin_id=darwin_id,
            k8s_namespace=ns,
            vault_namespace=vault_ns,
            organism_db_name=db_name,
            organism_db_role=db_role,
            organism_db_connection_ref=connection_ref,
            status=TenantNamespaceStatus.READY,
            created_at=self._clock(),
            wallet_addresses=wallet_addresses,
            metadata={
                "approle_role_id": approle["role_id"],
                # secret_id intentionally NOT recorded here — caller
                # passes it into the orchestrator pod's secret bundle
                # and the supervisor must not log it.
            },
        )
        self._tenants[darwin_id] = tenant
        self._audit("tenant_provisioned", actor, {
            "darwin_id": darwin_id,
            "k8s_namespace": ns,
            "vault_namespace": vault_ns,
            "organism_db_name": db_name,
            # Addresses (not secrets) are safe to include in the audit
            # record so the operator timeline shows which chain wallet
            # each Darwin owns.
            "wallet_addresses": dict(wallet_addresses),
        })
        return tenant

    def schedule_teardown(
        self,
        *,
        darwin_id: str,
        due_ts: float,
        actor: str = "platform.supervisor",
    ) -> None:
        """Mark a killed Darwin's namespace for DEFERRED teardown.

        Counterpart to the destructive :meth:`teardown`: on kill we keep the
        organism DB + vault namespace intact and only record when they may be
        reaped, honouring doc 09 commitment #3 ("data survives ≥90 days
        post-death" — so the owner can inspect it and a re-spawn can inherit
        accounts/databases). Idempotent."""
        tenant = self._tenants.get(darwin_id)
        if tenant is None:
            return
        tenant.status = TenantNamespaceStatus.DECEASED
        tenant.teardown_due_ts = float(due_ts)
        self._audit("tenant_teardown_scheduled", actor, {
            "darwin_id": darwin_id, "teardown_due_ts": float(due_ts),
        })

    def due_for_teardown(self, now: float) -> list[str]:
        """Darwin ids whose retention window has elapsed and are ready to be
        reaped. The supervisor's fleet-maintenance pass calls this, then
        :meth:`teardown` for each."""
        return [
            did for did, t in self._tenants.items()
            if t.status == TenantNamespaceStatus.DECEASED
            and t.teardown_due_ts is not None
            and now >= t.teardown_due_ts
        ]

    def teardown(
        self,
        *,
        darwin_id: str,
        actor: str = "platform.supervisor",
    ) -> None:
        tenant = self._tenants.get(darwin_id)
        if tenant is None:
            return
        tenant.status = TenantNamespaceStatus.TEARDOWN
        # Tear down in reverse order. Each step is best-effort + audit-logged
        # so the operator can see what was/wasn't cleaned up.
        errors: list[str] = []
        try:
            self._db.drop_database(
                database_name=tenant.organism_db_name,
                role_name=tenant.organism_db_role,
            )
        except Exception as exc:
            errors.append(f"organism_db:{exc}")
        try:
            self._vault.delete_namespace(namespace=tenant.vault_namespace)
        except Exception as exc:
            errors.append(f"vault:{exc}")
        try:
            self._k8s.delete_namespace(namespace=tenant.k8s_namespace)
        except Exception as exc:
            errors.append(f"k8s:{exc}")
        if errors:
            tenant.status = TenantNamespaceStatus.ERROR
            tenant.last_error = "; ".join(errors)
        else:
            del self._tenants[darwin_id]
        self._audit("tenant_torn_down", actor, {
            "darwin_id": darwin_id, "errors": errors or None,
        })

    def get(self, darwin_id: str) -> TenantNamespace | None:
        return self._tenants.get(darwin_id)

    def all(self) -> list[TenantNamespace]:
        return list(self._tenants.values())

    # -- internals --------------------------------------------------------
    def _audit(self, action: str, actor: str, metadata: dict[str, Any]) -> None:
        if self._recorder is None:
            return
        try:
            self._recorder(actor, action, metadata)
        except Exception:
            pass

    @staticmethod
    def _safe(fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:
            pass
