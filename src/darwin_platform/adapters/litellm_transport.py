"""Real LiteLLM gateway adapter (docs/engineering/13).

The genome's ``LLMRouter`` accepts any ``LLMTransport`` (``complete(tier, req)``).
The fake stack wires ``FakeLLM`` for deterministic offline runs; this module
wires the real thing: BerriAI's LiteLLM client, mapping capability tiers to
fallback chains of concrete models and translating the genome's task-shaped
requests into LiteLLM-shaped completion calls.

Architecture:

- One :class:`LiteLLMTransport` per Darwin. Holds the per-tier model
  fallback chain (``tier_mappings``), a markup rate consistent with the
  platform's :class:`BillingPolicy`, and a wallet to debit.
- Each genome request specifies a task in ``request["task"]``:
  ``plan`` / ``write_long_form`` / ``propose_patch``. A small per-task
  handler builds the messages, calls LiteLLM, parses the response back
  into the genome-expected dict shape, and debits the wallet at the
  marked-up rate. The genome never sees a provider name, a model name,
  or an API key — exactly as P6 (no provider lock-in) requires.
- Costs come from LiteLLM's ``response.usage`` (the platform's underlying
  provider cost). The Darwin's wallet is debited at ``provider_cost *
  (1 + markup_rate)``, so the platform's billing layer's per-debit
  attribution math matches.
- Errors from LiteLLM (provider down, rate-limited, etc.) are caught
  and re-raised as :class:`LLMError` so the genome's loop handles them
  gracefully (degrade tier, back off).

Production deployers will iterate on the handlers' prompts — the
defaults here are sane starting points, not finished prompt engineering.
The system prompts in ``orchestrator_skeleton/prompts/`` are the
canonical text for ``plan``; the other two tasks use compact local
system prompts that callers can override via ``system_prompts``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from darwin_dna.machinery.llm_router import LLMError, LLMTransport, Tier


_PLAN_SYSTEM_FALLBACK = (
    "You are the planner of a Darwin agent. Respond with a single concise "
    "sentence describing the next action to take or 'sleep' to wait."
)
_WRITER_SYSTEM_FALLBACK = (
    "You are an experienced content writer. Produce a well-structured "
    "long-form article on the requested topic at the requested length. "
    "Write naturally; do not preface with 'Sure' or 'Here is the article'."
)
_PATCH_SYSTEM_FALLBACK = (
    "You are a coding agent proposing a structured behavior change to fix a "
    "recurring failure. Respond with a JSON object containing exactly: "
    '{"rationale": <short string>, "behavior_delta": {"kind": "backoff", '
    '"failure_class": <string>, "after_failures": <int>}, "changes": '
    '[{"path": <string>, "diff": <string>}]} — no other keys, no prose '
    "around the JSON."
)


@dataclass(frozen=True)
class TierMapping:
    """Capability tier → ordered fallback chain of LiteLLM-recognised model
    identifiers. The first model is the primary; the rest are tried on
    provider error (rate limit, outage). Empty chains are refused at
    construction so a missing tier doesn't fail silently mid-loop."""

    models: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.models:
            raise ValueError("TierMapping needs at least one model")


@dataclass
class _CompletionResult:
    text: str
    provider_cost_usdc: float
    model: str
    raw: Any  # for caller-side debugging / tests


class LiteLLMTransport:
    """Real LLM gateway. Implements :class:`LLMTransport`.

    ``tier_mappings`` maps a tier value (e.g. ``"reasoning"``) to its
    fallback model chain. The genome's ``LLMRouter`` resolves the
    per-day cap and then calls ``complete(tier, request)``; we look up
    the chain, build the task-appropriate messages, ask LiteLLM, debit
    the wallet, and return the genome-expected shape.

    ``markup_rate`` should match the
    :class:`darwin_platform.BillingPolicy.infrastructure_markup_rate` in
    force so the platform's billing-side attribution stays consistent
    with what the Darwin's wallet shows.

    ``litellm_completion`` defaults to the real ``litellm.completion``;
    tests inject a mock. ``litellm_completion_cost`` defaults to
    LiteLLM's helper for extracting per-call cost (used as a fallback
    when ``response.usage.cost`` is absent).
    """

    def __init__(
        self,
        *,
        wallet: Any,
        tier_mappings: dict[str, TierMapping],
        markup_rate: float = 0.10,
        system_prompts: dict[str, str] | None = None,
        litellm_completion: Callable[..., Any] | None = None,
        litellm_completion_cost: Callable[..., float] | None = None,
        request_timeout: float = 60.0,
    ) -> None:
        if not isinstance(tier_mappings, dict) or not tier_mappings:
            raise ValueError("tier_mappings must be a non-empty dict")
        self._wallet = wallet
        self._tiers: dict[str, TierMapping] = dict(tier_mappings)
        self._markup = float(markup_rate)
        if self._markup < 0:
            raise ValueError("markup_rate must be non-negative")
        self._system_prompts: dict[str, str] = {
            "plan": _PLAN_SYSTEM_FALLBACK,
            "write_long_form": _WRITER_SYSTEM_FALLBACK,
            "propose_patch": _PATCH_SYSTEM_FALLBACK,
            **(system_prompts or {}),
        }
        self._timeout = float(request_timeout)
        if litellm_completion is None or litellm_completion_cost is None:
            try:
                import litellm  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover
                raise ImportError(
                    "LiteLLMTransport requires the 'litellm' optional dep: "
                    "`pip install darwin-dna[llm]`"
                ) from exc
            litellm_completion = litellm_completion or litellm.completion
            litellm_completion_cost = (
                litellm_completion_cost or litellm.completion_cost
            )
        self._completion: Callable[..., Any] = litellm_completion
        self._completion_cost: Callable[..., float] = litellm_completion_cost
        # Per-task handlers — keyed by request['task']. Returning a
        # narrowly-shaped dict that matches what the genome's caller
        # expects (see FakeLLM for the reference shapes).
        self._handlers: dict[
            str,
            Callable[[Tier, dict[str, Any]], dict[str, Any]],
        ] = {
            "plan": self._handle_plan,
            "write_long_form": self._handle_write_long_form,
            "propose_patch": self._handle_propose_patch,
        }

    # -- LLMTransport ------------------------------------------------------
    def complete(self, tier: str, request: dict[str, Any]) -> dict[str, Any]:
        try:
            tier_enum = Tier(tier)
        except ValueError as exc:
            raise LLMError(f"unknown tier: {tier!r}") from exc
        task = request.get("task")
        handler = self._handlers.get(task) if isinstance(task, str) else None
        if handler is None:
            raise LLMError(f"unknown task: {task!r}")
        try:
            return handler(tier_enum, request)
        except LLMError:
            raise
        except Exception as exc:  # provider error, parser error, etc.
            raise LLMError(f"{task} via litellm failed: {exc}") from exc

    # -- internals ---------------------------------------------------------
    def _models_for(self, tier: Tier) -> tuple[str, ...]:
        mapping = self._tiers.get(tier.value)
        if mapping is None:
            raise LLMError(
                f"no model chain configured for tier {tier.value!r}"
            )
        return mapping.models

    def _call_with_fallback(
        self,
        tier: Tier,
        messages: list[dict[str, Any]],
        *,
        response_format: dict[str, Any] | None = None,
        max_tokens: int | None = None,
    ) -> _CompletionResult:
        """Try each model in the chain until one returns a non-error. Bills
        the wallet for the SUCCESSFUL call only; failed attempts incur no
        Darwin-side cost (the platform absorbs them, like a real gateway)."""
        last_exc: Exception | None = None
        for model in self._models_for(tier):
            try:
                kwargs: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "timeout": self._timeout,
                }
                if response_format is not None:
                    kwargs["response_format"] = response_format
                if max_tokens is not None:
                    kwargs["max_tokens"] = max_tokens
                response = self._completion(**kwargs)
            except Exception as exc:
                last_exc = exc
                continue
            text = _extract_text(response)
            cost = _extract_cost(response, self._completion_cost)
            self._bill_wallet(tier, cost)
            return _CompletionResult(
                text=text, provider_cost_usdc=cost, model=model, raw=response
            )
        raise LLMError(
            f"every model in tier {tier.value} failed; "
            f"last error: {last_exc!r}"
        )

    def _bill_wallet(self, tier: Tier, provider_cost_usdc: float) -> float:
        """Charge the Darwin's wallet at the marked-up rate. Returns the
        charged amount (the gross the wallet records as a metered debit).
        A wallet refusal (out of funds, day cap) re-raises so the genome
        notices and handles it like any tier exhaustion."""
        charged = provider_cost_usdc * (1.0 + self._markup)
        try:
            self._wallet.debit(charged, f"llm:{tier.value}")
        except Exception as exc:
            raise LLMError(
                f"wallet refused LLM debit ${charged:.4f}: {exc}"
            ) from exc
        return charged

    # -- task handlers -----------------------------------------------------
    def _handle_plan(self, tier: Tier, request: dict[str, Any]) -> dict[str, Any]:
        context = {
            k: request.get(k)
            for k in (
                "strategy_prior",
                "balance_usdc",
                "lessons",
                "prior_outcomes",
                "untrusted_observations",
            )
            if k in request
        }
        messages = [
            {"role": "system", "content": self._system_prompts["plan"]},
            {"role": "user", "content": json.dumps(context, sort_keys=True)},
        ]
        result = self._call_with_fallback(tier, messages, max_tokens=300)
        return {"text": result.text, "cost_usdc": result.provider_cost_usdc}

    def _handle_write_long_form(
        self, tier: Tier, request: dict[str, Any]
    ) -> dict[str, Any]:
        topic = str(request.get("topic", "the topic"))
        target_words = int(request.get("target_words", 800))
        quality = str(request.get("quality", "standard"))
        messages = [
            {"role": "system", "content": self._system_prompts["write_long_form"]},
            {
                "role": "user",
                "content": (
                    f"Topic: {topic}\n"
                    f"Target length: ~{target_words} words.\n"
                    f"Quality bar: {quality}.\n"
                    f"Start the article with a top-level '# {topic.title()}' "
                    f"heading."
                ),
            },
        ]
        # ~1.4 tokens per word, +20% headroom — LiteLLM normalises across
        # provider tokenisers so this works as a reasonable cap.
        cap = int(target_words * 1.7) + 200
        result = self._call_with_fallback(tier, messages, max_tokens=cap)
        body = result.text
        words = len(body.split())
        # Real quality assessment would need a separate eval pass (doc 03's
        # reflect node could do it later). For now a per-tier default that
        # matches FakeLLM's convention — 0.72 for reasoning, 0.55 otherwise.
        # Production deployers should replace with a real grader.
        quality_score = 0.72 if tier is Tier.REASONING else 0.55
        return {
            "text": body,
            "words": words,
            "quality_score": quality_score,
            "cost_usdc": result.provider_cost_usdc,
        }

    def _handle_propose_patch(
        self, tier: Tier, request: dict[str, Any]
    ) -> dict[str, Any]:
        failure_class = str(request.get("failure_class", "unknown"))
        guidance = str(request.get("guidance", ""))
        messages = [
            {"role": "system", "content": self._system_prompts["propose_patch"]},
            {
                "role": "user",
                "content": (
                    f"Recurring failure class: {failure_class}\n"
                    f"Guidance: {guidance}\n"
                    f"Respond with JSON only."
                ),
            },
        ]
        result = self._call_with_fallback(
            tier,
            messages,
            response_format={"type": "json_object"},
            max_tokens=800,
        )
        try:
            parsed = json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise LLMError(
                f"propose_patch response was not valid JSON: {exc}"
            ) from exc
        # The genome consumes a specific shape; refuse anything else here
        # rather than let the orchestrator's mutation gate read junk.
        if not isinstance(parsed, dict):
            raise LLMError("propose_patch response was not a JSON object")
        rationale = str(parsed.get("rationale", ""))
        behavior_delta = parsed.get("behavior_delta") or {
            "kind": "backoff",
            "failure_class": failure_class,
            "after_failures": 2,
        }
        if not isinstance(behavior_delta, dict):
            raise LLMError("propose_patch behavior_delta must be a JSON object")
        changes = parsed.get("changes") or []
        if not isinstance(changes, list):
            raise LLMError("propose_patch changes must be a JSON array")
        return {
            "rationale": rationale,
            "changes": changes,
            "behavior_delta": behavior_delta,
            "cost_usdc": result.provider_cost_usdc,
        }


# -- helpers --------------------------------------------------------------
def _extract_text(response: Any) -> str:
    """Pull message text out of a LiteLLM ``completion`` response (or a
    test mock that mimics the shape). Accepts both object-shaped (real
    LiteLLM) and dict-shaped (cheap mocks) responses."""
    if isinstance(response, dict):
        choices = response.get("choices") or []
    else:
        choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    first = choices[0]
    if isinstance(first, dict):
        msg = first.get("message") or {}
        if isinstance(msg, dict):
            return str(msg.get("content") or "")
        return str(getattr(msg, "content", "") or "")
    msg = getattr(first, "message", None)
    if msg is None:
        return ""
    if isinstance(msg, dict):
        return str(msg.get("content") or "")
    return str(getattr(msg, "content", "") or "")


def _extract_cost(
    response: Any, completion_cost: Callable[..., float]
) -> float:
    """Cost in USD for this response. LiteLLM exposes
    ``response.usage.cost`` in many provider responses; when missing,
    ``litellm.completion_cost(response)`` computes it from token counts
    and the per-model price table. Either fallback path returns 0.0 if
    the cost truly cannot be determined — a real platform should
    monitor and alert if too many calls land in that case."""
    if isinstance(response, dict):
        usage = response.get("usage") or {}
        if isinstance(usage, dict) and "cost" in usage:
            try:
                return float(usage["cost"])
            except (TypeError, ValueError):
                pass
    else:
        usage = getattr(response, "usage", None)
        cost = getattr(usage, "cost", None) if usage is not None else None
        if cost is not None:
            try:
                return float(cost)
            except (TypeError, ValueError):
                pass
    try:
        return float(completion_cost(completion_response=response))
    except Exception:
        return 0.0
