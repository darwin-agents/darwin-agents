"""Tenancy: per-Darwin isolation (docs/engineering/22).

Each Darwin runs in its own K8s namespace + Vault namespace + organism
database, all provisioned together by :class:`NamespaceProvisioner`.
:mod:`vault_admin` is the bidirectional vault surface owners use to
write secrets the Darwin's neurons consume. :mod:`cross_pollination`
is the archive that lets vetted lessons flow from one lineage's
children into another lineage's spawn (doc 06 phase 3).
"""

from __future__ import annotations

from .cross_pollination import (
    CrossPollinationArchive,
    CrossPollinationError,
    PollinationLesson,
    verify_lesson,
)
from .namespace_provisioner import (
    K8sNamespaceBackend,
    NamespaceProvisioner,
    OrganismDbBackend,
    StubK8sBackend,
    StubOrganismDbBackend,
    StubVaultNamespaceBackend,
    TenantNamespace,
    TenantNamespaceStatus,
    VaultNamespaceBackend,
)
from .namespace_provisioner import ProvisioningError as TenantProvisioningError
from .provisioning import (
    InMemoryProvisioningBackend,
    ProvisioningBackend,
    ProvisioningError,
    ProvisioningQuotas,
    ProvisioningService,
    ResourceRecord,
    ScopedProvisionPort,
    SubprocessProvisioningBackend,
)
from .vault_admin import (
    VaultAdminError,
    VaultAdminService,
    VaultEntryRef,
    VaultRetentionService,
)

__all__ = [
    "CrossPollinationArchive",
    "CrossPollinationError",
    "InMemoryProvisioningBackend",
    "K8sNamespaceBackend",
    "NamespaceProvisioner",
    "OrganismDbBackend",
    "PollinationLesson",
    "ProvisioningBackend",
    "ProvisioningError",
    "ProvisioningQuotas",
    "ProvisioningService",
    "ResourceRecord",
    "ScopedProvisionPort",
    "StubK8sBackend",
    "StubOrganismDbBackend",
    "StubVaultNamespaceBackend",
    "SubprocessProvisioningBackend",
    "TenantNamespace",
    "TenantNamespaceStatus",
    "TenantProvisioningError",
    "VaultAdminError",
    "VaultAdminService",
    "VaultEntryRef",
    "VaultNamespaceBackend",
    "VaultRetentionService",
    "verify_lesson",
]
