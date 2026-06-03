"""Real per-Darwin Vault isolation backend (docs/engineering/16 §"Namespace
per Darwin").

The :class:`darwin_platform.tenancy.NamespaceProvisioner` orchestrates the
three per-Darwin tenant namespaces (K8s + Vault + organism DB) through small
backend Protocols. Until now the only :class:`VaultNamespaceBackend` was the
in-memory ``StubVaultNamespaceBackend`` — so per-Darwin Vault isolation was
*application-enforced* (the genome only ever asks for its own
``darwins/{id}/...`` paths) rather than *infrastructure-enforced*. The
docs-vs-impl audit flagged this: "no Vault namespaces (stub only) → tenant
isolation is application-enforced, not Vault-enforced".

This module ships the real backend. Vault Enterprise *namespaces* are a paid
feature; doc 16 §"Operational notes" sanctions the open-source fallback we
implement here: **one path-scoped policy + AppRole per Darwin** on a shared KV
mount. The effect is the same security property — a token issued for Darwin A
is refused, *by Vault*, on Darwin B's paths:

- :meth:`create_namespace` writes a per-Darwin policy granting access only to
  ``<mount>/data/darwins/{id}/*`` (+ the matching ``metadata`` paths for KV
  v2 listing/delete) and enables the AppRole auth method (idempotent).
- :meth:`issue_approle` creates an AppRole bound to that policy and returns a
  ``role_id`` + a fresh ``secret_id``; the orchestrator pod logs in with them
  and receives a token that *cannot* read another Darwin's paths.
- :meth:`write_secret` writes a KV v2 secret under the Darwin's prefix.
- :meth:`delete_namespace` revokes access (deletes the AppRole + the policy)
  and best-effort purges the Darwin's KV subtree.

The hvac client is injected (``client`` or ``client_factory``) so the whole
backend is unit-testable against a recording stub and never touches a real
Vault in tests. The admin token it uses needs ``sys`` (policy) + ``auth``
(approle) write capability — the platform control-plane token, never a
per-Darwin one.
"""

from __future__ import annotations

from typing import Any, Callable


class VaultNamespaceError(Exception):
    """A Vault namespace admin operation failed."""


def _policy_segment(darwin_id: str) -> str:
    """A path-safe darwin id for policy/role names + KV paths. Mirrors the
    genome's ``_seg`` (slashes would let an id forge another's path)."""
    return str(darwin_id).replace("/", "_").strip()


def _scoped_policy_hcl(*, mount_point: str, prefix: str) -> str:
    """HCL granting full KV v2 access to exactly one Darwin's subtree and
    nothing else. ``prefix`` is e.g. ``darwins/<id>``.

    KV v2 splits a logical path ``<mount>/<p>`` into a ``<mount>/data/<p>``
    (the value) and ``<mount>/metadata/<p>`` (versions + listing), so the
    policy must cover both for read/write/delete/list to work."""
    caps = '["create", "read", "update", "delete", "list"]'
    return (
        f'path "{mount_point}/data/{prefix}/*" {{\n'
        f'  capabilities = {caps}\n'
        f'}}\n'
        f'path "{mount_point}/metadata/{prefix}/*" {{\n'
        f'  capabilities = {caps}\n'
        f'}}\n'
        # Allow listing the subtree root itself (KV v2 list is on metadata).
        f'path "{mount_point}/metadata/{prefix}" {{\n'
        f'  capabilities = ["list", "read"]\n'
        f'}}\n'
    )


class RealVaultNamespaceBackend:
    """hvac-backed :class:`VaultNamespaceBackend` (doc 16).

    Args:
        url / token: admin Vault address + token (control-plane; needs sys +
            approle write). Ignored when ``client``/``client_factory`` is given.
        mount_point: KV v2 mount the per-Darwin subtrees live under
            (default ``"secret"``). One policy is written per Darwin scoping
            ``darwins/{id}/`` on this mount.
        approle_path: auth mount path for AppRole (default ``"approle"``).
        token_ttl / token_max_ttl: TTL (seconds) for tokens the Darwin's
            AppRole login mints. Short by default — the orchestrator re-logins
            transparently (see :class:`HvacVaultClient`).
        client / client_factory: test seam — inject a hvac-shaped stub.
    """

    def __init__(
        self,
        *,
        url: str | None = None,
        token: str | None = None,
        mount_point: str = "secret",
        approle_path: str = "approle",
        token_ttl: int = 1800,
        token_max_ttl: int = 14400,
        client: Any | None = None,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._mount = mount_point
        self._approle_path = approle_path
        self._token_ttl = int(token_ttl)
        self._token_max_ttl = int(token_max_ttl)
        if client is not None:
            self._client = client
        elif client_factory is not None:
            self._client = client_factory()
        else:
            try:
                import hvac
            except ImportError as exc:  # pragma: no cover - dep guard
                raise ImportError(
                    "RealVaultNamespaceBackend requires the [hvac] extra: "
                    "pip install 'darwin-dna[hvac]'"
                ) from exc
            if not (url and token):
                raise ValueError(
                    "url + token are required when no client is injected"
                )
            self._client = hvac.Client(url=url, token=token)
        self._approle_enabled = False

    # -- naming ------------------------------------------------------------
    def _policy_name(self, darwin_id: str) -> str:
        return f"darwin-{_policy_segment(darwin_id)}"

    def _prefix(self, darwin_id: str) -> str:
        return f"darwins/{_policy_segment(darwin_id)}"

    def _role_name(self, namespace: str, role_name: str) -> str:
        # ``namespace`` is the prefix we returned from create_namespace,
        # e.g. ``darwins/<id>``. Flatten it into an AppRole role name.
        flat = namespace.replace("/", "-")
        return f"{flat}-{_policy_segment(role_name)}"

    # -- VaultNamespaceBackend surface ------------------------------------
    def create_namespace(self, *, darwin_id: str) -> str:
        """Provision the per-Darwin policy (+ ensure AppRole auth is on).
        Returns the path prefix the provisioner uses as the ``namespace``
        handle for subsequent ``write_secret`` / ``issue_approle`` calls."""
        if not darwin_id:
            raise VaultNamespaceError("darwin_id is required")
        prefix = self._prefix(darwin_id)
        self._ensure_approle_enabled()
        policy = _scoped_policy_hcl(mount_point=self._mount, prefix=prefix)
        try:
            self._client.sys.create_or_update_policy(
                name=self._policy_name(darwin_id), policy=policy,
            )
        except Exception as exc:
            raise VaultNamespaceError(
                f"failed to write policy for {darwin_id!r}: {exc}"
            ) from exc
        return prefix

    def write_secret(
        self, *, namespace: str, path: str, value: dict[str, Any],
    ) -> None:
        """Write a KV v2 secret under the Darwin's prefix
        (``<namespace>/<path>``)."""
        full = f"{namespace.rstrip('/')}/{path.lstrip('/')}"
        try:
            self._client.secrets.kv.v2.create_or_update_secret(
                mount_point=self._mount, path=full, secret=dict(value),
            )
        except Exception as exc:
            raise VaultNamespaceError(
                f"failed to write secret {full!r}: {exc}"
            ) from exc

    def issue_approle(
        self, *, namespace: str, role_name: str,
    ) -> dict[str, str]:
        """Create (or update) an AppRole bound to the namespace's policy and
        return fresh ``{role_id, secret_id}`` login credentials. The policy
        name is derived from the namespace prefix so the role can only ever
        touch this Darwin's paths."""
        # namespace is ``darwins/<id>``; recover the policy name from it.
        darwin_id = namespace.split("/", 1)[1] if "/" in namespace else namespace
        policy_name = self._policy_name(darwin_id)
        role = self._role_name(namespace, role_name)
        try:
            self._client.auth.approle.create_or_update_approle(
                role_name=role,
                token_policies=[policy_name],
                token_ttl=self._token_ttl,
                token_max_ttl=self._token_max_ttl,
                mount_point=self._approle_path,
            )
            role_id = (
                self._client.auth.approle.read_role_id(
                    role_name=role, mount_point=self._approle_path,
                )["data"]["role_id"]
            )
            secret_id = (
                self._client.auth.approle.generate_secret_id(
                    role_name=role, mount_point=self._approle_path,
                )["data"]["secret_id"]
            )
        except Exception as exc:
            raise VaultNamespaceError(
                f"failed to issue approle {role!r}: {exc}"
            ) from exc
        return {"role_id": str(role_id), "secret_id": str(secret_id)}

    def delete_namespace(self, *, namespace: str) -> None:
        """Revoke the Darwin's access (delete its AppRole(s) + policy) and
        best-effort purge its KV subtree. Deleting the policy is the
        security-critical step — any token still bound to it loses all
        capability immediately."""
        darwin_id = namespace.split("/", 1)[1] if "/" in namespace else namespace
        policy_name = self._policy_name(darwin_id)
        # Delete any AppRoles whose name starts with the namespace prefix.
        self._safe(lambda: self._delete_namespace_approles(namespace))
        # Delete the policy (revokes capability for any lingering token).
        self._safe(
            lambda: self._client.sys.delete_policy(name=policy_name)
        )
        # Best-effort purge of the KV subtree.
        self._safe(lambda: self._purge_kv_subtree(namespace))

    # -- internals ---------------------------------------------------------
    def _ensure_approle_enabled(self) -> None:
        if self._approle_enabled:
            return
        try:
            self._client.sys.enable_auth_method(
                method_type="approle", path=self._approle_path,
            )
        except Exception as exc:
            # Already enabled → Vault returns a 400 "path is already in use".
            # That's success for our purposes; anything else propagates.
            if "already in use" not in str(exc).lower():
                raise VaultNamespaceError(
                    f"failed to enable approle auth: {exc}"
                ) from exc
        self._approle_enabled = True

    def _delete_namespace_approles(self, namespace: str) -> None:
        flat = namespace.replace("/", "-")
        roles: list[str] = []
        try:
            listed = self._client.auth.approle.list_roles(
                mount_point=self._approle_path,
            )
            roles = list(((listed or {}).get("data") or {}).get("keys") or [])
        except Exception:
            roles = []
        for role in roles:
            if str(role).startswith(flat):
                self._safe(
                    lambda r=role: self._client.auth.approle.delete_role(
                        role_name=r, mount_point=self._approle_path,
                    )
                )

    def _purge_kv_subtree(self, namespace: str) -> None:
        """Delete every KV v2 entry under ``namespace`` (one level deep is
        enough for our flat-ish layout; nested keys are walked too)."""
        self._purge_kv_path(namespace.rstrip("/"))

    def _purge_kv_path(self, path: str) -> None:
        try:
            listed = self._client.secrets.kv.v2.list_secrets(
                mount_point=self._mount, path=path,
            )
            keys = list(((listed or {}).get("data") or {}).get("keys") or [])
        except Exception:
            keys = []
        for key in keys:
            child = f"{path}/{str(key).rstrip('/')}"
            if str(key).endswith("/"):
                self._purge_kv_path(child)
            else:
                self._safe(
                    lambda c=child: self._client.secrets.kv.v2
                    .delete_metadata_and_all_versions(
                        mount_point=self._mount, path=c,
                    )
                )

    @staticmethod
    def _safe(fn: Callable[[], Any]) -> None:
        try:
            fn()
        except Exception:
            pass


def build_vault_namespace_backend(
    *,
    url: str,
    token: str,
    mount_point: str = "secret",
    approle_path: str = "approle",
) -> RealVaultNamespaceBackend:
    """Construct the production hvac-backed backend. Kept thin so the
    NamespaceProvisioner wiring (a follow-on slice) can build it from CLI
    flags / env without importing hvac at module load."""
    return RealVaultNamespaceBackend(
        url=url, token=token, mount_point=mount_point,
        approle_path=approle_path,
    )
