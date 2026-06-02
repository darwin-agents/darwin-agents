"""LiteLLM admin API client + per-Darwin spend reconciliation (docs/engineering/13).

doc 13 §Deployment: each Darwin gets **one LiteLLM virtual key**, minted at
spawn and stored in the Darwin's vault, scoped with a per-key budget, allowed
tiers, and a rate limit. The proxy enforces those budgets and logs every call
against the key's metadata (``darwin_id``). The audit (docs-vs-impl 2026-06)
flagged that the platform shipped a *single shared master key for every
Darwin* — no per-Darwin attribution, no per-Darwin budget ceiling, and no
spend table to reconcile against. This module is the platform's client for the
admin surface that closes that gap:

- :class:`LiteLLMAdminClient` mints / inspects / revokes virtual keys via the
  proxy's ``/key/generate``, ``/key/info``, ``/key/delete`` endpoints, all
  authenticated with the platform master key. The supervisor calls
  ``generate_key`` at spawn and ``delete_key`` on kill, so a dead Darwin's key
  stops working the moment it dies.
- :class:`LiteLLMSpendReconciler` reads each key's proxy-side spend and
  reconciles it against the Darwin's in-process LLM debits (doc 13
  §Observability: "a small reconciliation worker reading LiteLLM's per-key
  spend table"). The in-process wallet debit stays the *authoritative* ledger
  posting (one money door, doc 05); this worker is the cross-check that the
  proxy and the ledger agree, and surfaces drift as an alert rather than
  silently double-posting.

The HTTP client is injected (an ``httpx.Client``-shaped object exposing
``.post`` and ``.get``) so the whole module is unit-testable with a stub and
never touches the network in tests. Without an injected client it lazily
builds a real ``httpx.Client`` from the ``[platform]`` extra.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class LiteLLMAdminError(RuntimeError):
    """A LiteLLM admin API call failed (non-2xx response or transport error)."""


class _HttpResponse(Protocol):  # pragma: no cover - structural type only
    status_code: int
    text: str

    def json(self) -> Any: ...


class _HttpClient(Protocol):  # pragma: no cover - structural type only
    def post(self, url: str, **kwargs: Any) -> _HttpResponse: ...

    def get(self, url: str, **kwargs: Any) -> _HttpResponse: ...


class LiteLLMAdminClient:
    """Client for the LiteLLM proxy's key-management admin API (doc 13).

    Authenticates every call with the platform ``master_key`` (the proxy's
    ``general_settings.master_key``). All requests carry an explicit timeout
    so a wedged proxy can never stall the supervisor's spawn path.

    ``http`` accepts any object with ``httpx.Client``-shaped ``.post`` and
    ``.get`` methods; tests inject a stub. When omitted, a real
    ``httpx.Client`` is built lazily on first use.
    """

    def __init__(
        self,
        *,
        base_url: str,
        master_key: str,
        http: _HttpClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        if not master_key:
            raise ValueError("master_key is required")
        self._base = base_url.rstrip("/")
        self._master_key = str(master_key)
        self._http = http
        self._timeout = float(timeout)

    # -- HTTP plumbing -----------------------------------------------------
    def _client(self) -> _HttpClient:
        if self._http is None:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover - dep guard
                raise ImportError(
                    "LiteLLMAdminClient needs httpx: "
                    "pip install 'darwin-dna[platform]'"
                ) from exc
            self._http = httpx.Client()
        return self._http

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._master_key}",
            "Content-Type": "application/json",
        }

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        try:
            resp = self._client().post(
                self._base + path,
                headers=self._headers(),
                json=payload,
                timeout=self._timeout,
            )
        except Exception as exc:  # transport-level failure
            raise LiteLLMAdminError(f"POST {path} failed: {exc}") from exc
        return self._parse(path, resp)

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        try:
            resp = self._client().get(
                self._base + path,
                headers=self._headers(),
                params=params,
                timeout=self._timeout,
            )
        except Exception as exc:  # transport-level failure
            raise LiteLLMAdminError(f"GET {path} failed: {exc}") from exc
        return self._parse(path, resp)

    @staticmethod
    def _parse(path: str, resp: _HttpResponse) -> Any:
        status = int(getattr(resp, "status_code", 0) or 0)
        if not (200 <= status < 300):
            body = (getattr(resp, "text", "") or "")[:300]
            raise LiteLLMAdminError(f"{path} returned HTTP {status}: {body}")
        try:
            return resp.json()
        except Exception as exc:  # non-JSON body
            raise LiteLLMAdminError(
                f"{path} returned a non-JSON body"
            ) from exc

    # -- key lifecycle -----------------------------------------------------
    def generate_key(
        self,
        *,
        darwin_id: str,
        max_budget: float | None = None,
        budget_duration: str | None = None,
        models: tuple[str, ...] | list[str] | None = None,
        rpm_limit: int | None = None,
        tpm_limit: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Mint a virtual key scoped to ``darwin_id``.

        The key's ``metadata.darwin_id`` is what every spend-log row and the
        per-key spend table key off, giving per-Darwin attribution. Returns
        the proxy's full response dict; ``["key"]`` is the secret the Darwin's
        transport authenticates with. Raises :class:`LiteLLMAdminError` if the
        proxy returns no key.
        """
        if not darwin_id:
            raise ValueError("darwin_id is required")
        payload: dict[str, Any] = {
            "key_alias": f"darwin-{darwin_id}",
            "metadata": {"darwin_id": darwin_id, **(metadata or {})},
        }
        if max_budget is not None:
            payload["max_budget"] = float(max_budget)
        if budget_duration is not None:
            payload["budget_duration"] = str(budget_duration)
        if models:
            payload["models"] = list(models)
        if rpm_limit is not None:
            payload["rpm_limit"] = int(rpm_limit)
        if tpm_limit is not None:
            payload["tpm_limit"] = int(tpm_limit)
        result = self._post("/key/generate", payload)
        if not isinstance(result, dict) or not result.get("key"):
            raise LiteLLMAdminError(
                "/key/generate returned no 'key' in its response"
            )
        return result

    def key_info(self, key: str) -> dict[str, Any]:
        """Fetch the proxy's record for ``key`` (spend, budget, limits).

        LiteLLM wraps the record under an ``info`` envelope; this unwraps it
        so callers always see the flat record.
        """
        if not key:
            raise ValueError("key is required")
        result = self._get("/key/info", {"key": key})
        if isinstance(result, dict):
            info = result.get("info")
            if isinstance(info, dict):
                return info
            return result
        return {}

    def key_spend(self, key: str) -> float:
        """The cumulative provider-side spend (USD) the proxy has metered
        against ``key``. Returns 0.0 when the field is absent or unparseable."""
        info = self.key_info(key)
        try:
            return float(info.get("spend", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def delete_key(self, key: str) -> None:
        """Revoke ``key`` on the proxy. Called on Darwin death so a dead
        Darwin's credential stops working immediately. A blank key is a
        no-op (a Darwin that never minted one)."""
        if not key:
            return
        self._post("/key/delete", {"keys": [key]})


@dataclass(frozen=True)
class LLMSpendReconciliation:
    """The result of reconciling one Darwin's proxy spend against its ledger.

    ``proxy_spend_usdc`` is the provider-side cost the proxy metered;
    ``expected_ledger_usdc`` is that grossed up by the platform markup (what
    the wallet *should* have been debited); ``ledger_spend_usdc`` is what the
    wallet actually recorded. ``drift_usdc = ledger - expected`` — positive
    means the wallet over-charged relative to the proxy, negative means the
    proxy metered calls the wallet never saw.
    """

    darwin_id: str
    proxy_spend_usdc: float
    expected_ledger_usdc: float
    ledger_spend_usdc: float
    drift_usdc: float
    within_tolerance: bool
    error: str | None = None


class LiteLLMSpendReconciler:
    """Reconcile each Darwin's proxy-side spend against its in-process ledger.

    doc 13 §Observability promises "a small reconciliation worker reading
    LiteLLM's per-key spend table". The wallet debit inside
    :class:`LiteLLMTransport` is the authoritative ledger posting (one money
    door, doc 05); this reconciler is the *cross-check* — it confirms the
    proxy's metered spend and the wallet's recorded LLM debits agree, and
    flags drift for the supervisor to alert on (a proxy outage that dropped
    spend logs, a transport that billed without a successful call, etc.).

    The wallet is debited at the *marked-up* rate while the proxy records the
    raw provider cost, so the comparison grosses the proxy spend up by
    ``markup_rate`` before differencing — otherwise every Darwin would show a
    constant markup-sized "drift".
    """

    def __init__(
        self,
        *,
        admin_client: LiteLLMAdminClient,
        markup_rate: float = 0.10,
        tolerance_usdc: float = 0.05,
    ) -> None:
        self._admin = admin_client
        self._markup = max(0.0, float(markup_rate))
        self._tolerance = max(0.0, float(tolerance_usdc))

    @property
    def tolerance_usdc(self) -> float:
        return self._tolerance

    @property
    def markup_rate(self) -> float:
        return self._markup

    def reconcile_key(
        self,
        *,
        darwin_id: str,
        virtual_key: str,
        ledger_spend_usdc: float,
    ) -> LLMSpendReconciliation:
        """Reconcile a single Darwin. Never raises: a proxy fetch failure is
        captured in ``error`` and reported as within-tolerance (an
        unreachable proxy is an availability concern, not a money
        discrepancy) so a transient blip doesn't page on a false drift."""
        ledger = float(ledger_spend_usdc)
        try:
            proxy = self._admin.key_spend(virtual_key)
        except Exception as exc:  # noqa: BLE001 - reported, never propagated
            return LLMSpendReconciliation(
                darwin_id=darwin_id,
                proxy_spend_usdc=0.0,
                expected_ledger_usdc=0.0,
                ledger_spend_usdc=ledger,
                drift_usdc=0.0,
                within_tolerance=True,
                error=str(exc)[:200],
            )
        expected = proxy * (1.0 + self._markup)
        drift = ledger - expected
        return LLMSpendReconciliation(
            darwin_id=darwin_id,
            proxy_spend_usdc=proxy,
            expected_ledger_usdc=expected,
            ledger_spend_usdc=ledger,
            drift_usdc=drift,
            within_tolerance=abs(drift) <= self._tolerance,
            error=None,
        )


def ledger_llm_spend_usdc(movements: Any) -> float:
    """Sum a wallet's in-process LLM debits (memo ``llm:<tier>``).

    ``LiteLLMTransport._bill_wallet`` records each call as a metered debit
    with memo ``llm:<tier>`` (e.g. ``llm:reasoning``). This pulls those rows
    out of a wallet's ``movements()`` list so the reconciler can compare them
    against the proxy's per-key spend. Robust to both debit row shapes and
    missing fields — anything unparseable contributes 0.
    """
    total = 0.0
    try:
        rows = list(movements)
    except TypeError:
        return 0.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        debit = row.get("debit")
        if not isinstance(debit, dict):
            continue
        memo = debit.get("memo")
        if not (isinstance(memo, str) and memo.startswith("llm:")):
            continue
        try:
            total += float(debit.get("amount_usdc", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
    return total
