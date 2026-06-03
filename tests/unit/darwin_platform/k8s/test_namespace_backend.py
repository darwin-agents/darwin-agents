"""DriverK8sNamespaceBackend (doc 22): the passive K8s side of the
NamespaceProvisioner.

The driver owns namespace creation; this adapter only derives the name and
owns a tolerant teardown sweep. These tests pin that contract: no namespace is
created here, NetworkPolicy is the driver's job, and a delete against an
already-gone namespace is success (the deferred reaper runs long after the
driver deleted it at kill).
"""

from __future__ import annotations

import pytest

from darwin_platform.k8s.manifests import namespace_name
from darwin_platform.k8s.namespace_backend import DriverK8sNamespaceBackend
from darwin_platform.tenancy.namespace_provisioner import K8sNamespaceBackend


class _FakeApi:
    def __init__(self, *, delete_error: Exception | None = None) -> None:
        self.created: list = []
        self.deleted: list[str] = []
        self._delete_error = delete_error

    def create_namespace(self, body) -> None:  # should never be called here
        self.created.append(body)

    def delete_namespace(self, name: str) -> None:
        if self._delete_error is not None:
            raise self._delete_error
        self.deleted.append(name)


class _NotFound(Exception):
    def __init__(self) -> None:
        super().__init__("namespaces \"darwin-d1\" not found")
        self.status = 404


def test_conforms_to_protocol():
    assert isinstance(
        DriverK8sNamespaceBackend(api=_FakeApi()), K8sNamespaceBackend
    )


def test_create_namespace_derives_name_without_creating():
    api = _FakeApi()
    b = DriverK8sNamespaceBackend(api=api, namespace_prefix="darwin-")
    ns = b.create_namespace(darwin_id="D1")
    assert ns == namespace_name("darwin-", "D1")
    # The driver creates the namespace, not this adapter.
    assert api.created == []


def test_apply_network_policy_is_noop():
    api = _FakeApi()
    b = DriverK8sNamespaceBackend(api=api)
    assert b.apply_network_policy(namespace="darwin-d1") is None


def test_delete_namespace_calls_through():
    api = _FakeApi()
    b = DriverK8sNamespaceBackend(api=api)
    b.delete_namespace(namespace="darwin-d1")
    assert api.deleted == ["darwin-d1"]


def test_delete_tolerates_already_gone():
    api = _FakeApi(delete_error=_NotFound())
    b = DriverK8sNamespaceBackend(api=api)
    # Reaper runs after the driver already deleted it — must not raise.
    b.delete_namespace(namespace="darwin-d1")


def test_delete_reraises_unexpected_error():
    api = _FakeApi(delete_error=RuntimeError("apiserver unreachable"))
    b = DriverK8sNamespaceBackend(api=api)
    with pytest.raises(RuntimeError, match="unreachable"):
        b.delete_namespace(namespace="darwin-d1")
