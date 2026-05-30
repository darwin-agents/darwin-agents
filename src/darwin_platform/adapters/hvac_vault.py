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
        auth_method: str = "token",
        role_id: str | None = None,
        secret_id: str | None = None,
        k8s_role: str | None = None,
        k8s_jwt_path: str = "/var/run/secrets/kubernetes.io/serviceaccount/token",
        client_factory=None,
    ) -> None:
        try:
            import hvac
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "HvacVaultClient requires the [hvac] extra: "
                "`pip install darwin-dna[hvac]`"
            ) from exc
        self._auth_method = auth_method
        self._token = token
        self._role_id = role_id
        self._secret_id = secret_id
        self._k8s_role = k8s_role
        self._k8s_jwt_path = k8s_jwt_path
        if client_factory is None:
            if not url:
                raise ValueError("url is required when client_factory is None")
            if auth_method == "token" and not token:
                raise ValueError("token is required for auth_method='token'")
            if auth_method == "approle" and not (role_id and secret_id):
                raise ValueError(
                    "role_id + secret_id are required for auth_method='approle'"
                )
            if auth_method == "kubernetes" and not k8s_role:
                raise ValueError(
                    "k8s_role is required for auth_method='kubernetes'"
                )
            self._client = hvac.Client(url=url)
            self._authenticate()
        else:
            self._client = client_factory()
        self._mount = mount_point
        self._v2 = v2

    # -- auth --------------------------------------------------------------
    def _authenticate(self) -> None:
        """(Re)establish the client token for the configured auth method.

        A root/static ``token`` is the dev path. ``approle`` and
        ``kubernetes`` are the production methods — their tokens are leased
        and expire, so :meth:`_retry` re-invokes this on an auth failure to
        transparently re-login (no background renewal thread needed)."""
        m = self._auth_method
        if m == "token":
            self._client.token = self._token
        elif m == "approle":
            self._client.auth.approle.login(
                role_id=self._role_id, secret_id=self._secret_id,
            )
        elif m == "kubernetes":
            with open(self._k8s_jwt_path) as fh:
                jwt = fh.read().strip()
            self._client.auth.kubernetes.login(role=self._k8s_role, jwt=jwt)
        else:
            raise ValueError(f"unknown vault auth_method {m!r}")

    @staticmethod
    def _is_auth_error(exc: Exception) -> bool:
        # hvac raises Forbidden (403) / Unauthorized (401) on an expired or
        # insufficient token; missing keys raise InvalidPath (handled as
        # "no such secret", NOT an auth failure → don't re-login on those).
        name = type(exc).__name__
        return name in ("Forbidden", "Unauthorized") or (
            "permission denied" in str(exc).lower()
        )

    def _retry(self, fn):
        """Run a KV op; on an AUTH failure (leased token expired),
        re-authenticate once and retry. Token auth has nothing to refresh,
        so it just propagates."""
        try:
            return fn()
        except Exception as exc:
            if self._auth_method != "token" and self._is_auth_error(exc):
                self._authenticate()
                return fn()
            raise

    # -- VaultClient surface ----------------------------------------------
    def kv_put(self, key: str, value: str) -> None:
        def _put():
            if self._v2:
                self._client.secrets.kv.v2.create_or_update_secret(
                    mount_point=self._mount, path=key,
                    secret={"value": value},
                )
            else:
                self._client.secrets.kv.v1.create_or_update_secret(
                    mount_point=self._mount, path=key,
                    secret={"value": value},
                )
        self._retry(_put)

    def kv_get(self, key: str) -> str | None:
        def _read():
            if self._v2:
                resp = self._client.secrets.kv.v2.read_secret_version(
                    mount_point=self._mount,
                    path=key,
                    raise_on_deleted_version=False,
                )
                return ((resp or {}).get("data") or {}).get("data") or {}
            resp = self._client.secrets.kv.v1.read_secret(
                mount_point=self._mount, path=key,
            )
            return (resp or {}).get("data") or {}
        try:
            data = self._retry(_read)
        except Exception:
            # Fail-closed: Vault's tiered vault interprets None as "no
            # such key" and refuses to surface anything sensitive. (Auth
            # errors are retried once inside _retry before reaching here.)
            return None
        v = data.get("value")
        return None if v is None else str(v)

    def kv_delete(self, key: str) -> None:
        def _delete():
            if self._v2:
                # Hard delete all versions; matches the FakeVault's
                # `MemoryOwnerGrantStore`-shaped semantics where a
                # consume-once token is gone forever.
                self._client.secrets.kv.v2.delete_metadata_and_all_versions(
                    mount_point=self._mount, path=key,
                )
            else:
                self._client.secrets.kv.v1.delete_secret(
                    mount_point=self._mount, path=key,
                )
        self._retry(_delete)
