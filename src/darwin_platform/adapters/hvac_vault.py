"""Real :class:`VaultClient` using hvac (docs/engineering/16).

Maps the genome's tiny KV interface (``put`` / ``get`` / ``delete``) onto
HashiCorp Vault's KV v2 secrets engine via the canonical ``hvac``
client. Each Darwin's namespace path
(``darwins/{id}/{tier}/...``) is stored under a configurable KV mount
point so a multi-tenant Vault can isolate platforms via mount-policy
ACLs.

KV v2 vs KV v1
--------------

KV v2 is the default since Vault 0.11 and supports versioning + soft
delete. We use it because:

- ``destroy_secret_versions`` lets ``delete()`` actually remove the
  data (KV v1's "delete" is a hard delete). KV v2's
  ``delete_metadata_and_all_versions`` is the equivalent.
- Versions give us a free rollback path if a credential is corrupted.
- The shape is a stable Vault primitive — production deployers running
  ``vault kv put`` from a CI runner get the same data layout.

If your Vault is configured with KV v1, swap ``v2=True`` to ``False``;
``hvac.api.secrets_engines.KvV1`` is used instead.

Authentication
--------------

hvac is initialised with the operator-provided ``token`` (or AppRole
credentials, but that's a chart-side concern — pass the resulting
client token here). The token's policy MUST allow read+write on the
configured ``mount_point`` path.
"""

from __future__ import annotations


class HvacVaultClient:
    """:class:`darwin_platform.adapters.VaultClient`-conformant wrapper
    over hvac's KV v2 API.

    Args:
        url: Vault address (e.g. ``https://vault.example.com:8200``).
        token: a Vault token with read+write on ``mount_point``. In
            production this comes from an AppRole or k8s auth login
            done at startup by the chart's init container.
        mount_point: the KV mount, default ``"secret"`` (Vault's
            stock). Production multi-tenant setups mount one per
            platform (``"darwin-platform"``) to scope ACLs.
        v2: KV engine version. Default True.
        client_factory: test seam — pass a stub returning a hvac-shaped
            mock. Production defaults to ``hvac.Client(url, token)``.
    """

    def __init__(
        self,
        *,
        url: str | None = None,
        token: str | None = None,
        mount_point: str = "secret",
        v2: bool = True,
        client_factory=None,
    ) -> None:
        try:
            import hvac
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "HvacVaultClient requires the [hvac] extra: "
                "`pip install darwin-dna[hvac]`"
            ) from exc
        if client_factory is None:
            if not url or not token:
                raise ValueError(
                    "url and token are required when client_factory is None"
                )
            self._client = hvac.Client(url=url, token=token)
        else:
            self._client = client_factory()
        self._mount = mount_point
        self._v2 = v2

    # -- VaultClient surface ----------------------------------------------
    def kv_put(self, key: str, value: str) -> None:
        if self._v2:
            self._client.secrets.kv.v2.create_or_update_secret(
                mount_point=self._mount,
                path=key,
                secret={"value": value},
            )
        else:
            self._client.secrets.kv.v1.create_or_update_secret(
                mount_point=self._mount,
                path=key,
                secret={"value": value},
            )

    def kv_get(self, key: str) -> str | None:
        try:
            if self._v2:
                resp = self._client.secrets.kv.v2.read_secret_version(
                    mount_point=self._mount,
                    path=key,
                    raise_on_deleted_version=False,
                )
                data = ((resp or {}).get("data") or {}).get("data") or {}
            else:
                resp = self._client.secrets.kv.v1.read_secret(
                    mount_point=self._mount,
                    path=key,
                )
                data = (resp or {}).get("data") or {}
        except Exception:
            # Fail-closed: Vault's tiered vault interprets None as "no
            # such key" and refuses to surface anything sensitive.
            return None
        v = data.get("value")
        return None if v is None else str(v)

    def kv_delete(self, key: str) -> None:
        if self._v2:
            # Hard delete all versions; matches the FakeVault's
            # `MemoryOwnerGrantStore`-shaped semantics where a
            # consume-once token is gone forever.
            self._client.secrets.kv.v2.delete_metadata_and_all_versions(
                mount_point=self._mount,
                path=key,
            )
        else:
            self._client.secrets.kv.v1.delete_secret(
                mount_point=self._mount,
                path=key,
            )
