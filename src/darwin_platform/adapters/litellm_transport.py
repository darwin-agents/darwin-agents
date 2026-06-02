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

import asyncio
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from darwin_dna.machinery.llm_router import LLMError, Tier


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


# -- circuit breaker -------------------------------------------------------
@dataclass
class _CircuitState:
    """Per-model circuit state. ``state`` ∈ {closed, open, half_open}.
    closed = healthy; open = skip; half_open = one trial call allowed."""

    state: str = "closed"
    consecutive_failures: int = 0
    opened_at: float = 0.0


class CircuitBreaker:
    """Per-model circuit breaker for the LLM fallback chain (doc 13).

    Logic:
    - Each model starts ``closed``. ``record_failure`` increments the
      counter; after ``failure_threshold`` it goes ``open``.
    - ``open`` lasts ``open_seconds``. After the cooldown the next
      ``can_call`` returns True (and flips the state to ``half_open``).
      The trial call's result determines whether the circuit re-opens
      (on failure) or closes (on success).
    - ``record_success`` always closes the circuit and resets the counter.

    This sits in front of the existing fallback chain: a model with an
    open circuit is *skipped* rather than tried-and-caught. That's the
    win — we stop hammering a known-down provider that LiteLLM would
    otherwise eat 30s of timeout on for every call.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        open_seconds: float = 30.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if open_seconds <= 0:
            raise ValueError("open_seconds must be > 0")
        self._failure_threshold = int(failure_threshold)
        self._open_seconds = float(open_seconds)
        self._clock = clock
        self._states: dict[str, _CircuitState] = {}

    def can_call(self, model: str) -> bool:
        st = self._states.get(model)
        if st is None or st.state == "closed":
            return True
        if st.state == "half_open":
            return True
        # state == "open" — check cooldown
        if (self._clock() - st.opened_at) >= self._open_seconds:
            st.state = "half_open"
            return True
        return False

    def record_success(self, model: str) -> None:
        st = self._states.setdefault(model, _CircuitState())
        st.state = "closed"
        st.consecutive_failures = 0
        st.opened_at = 0.0

    def record_failure(self, model: str) -> None:
        st = self._states.setdefault(model, _CircuitState())
        st.consecutive_failures += 1
        if (
            st.state == "half_open"
            or st.consecutive_failures >= self._failure_threshold
        ):
            st.state = "open"
            st.opened_at = self._clock()

    def state_of(self, model: str) -> str:
        st = self._states.get(model)
        return "closed" if st is None else st.state

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """For tests + ops dashboards."""
        return {
            model: {
                "state": st.state,
                "consecutive_failures": st.consecutive_failures,
                "opened_at": st.opened_at,
            }
            for model, st in self._states.items()
        }


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


@dataclass
class _PreparedCall:
    """A per-task request, ready to send and parse. Splitting the task
    handlers into prepare (build messages) + parse (shape the response) lets
    the synchronous ``complete`` and the native-async ``acomplete`` share the
    exact same request shaping and response parsing — only the provider call
    in the middle differs (blocking-isolated vs awaited)."""

    messages: list[dict[str, Any]]
    parse: Callable[[_CompletionResult], dict[str, Any]]
    max_tokens: int | None = None
    response_format: dict[str, Any] | None = None


# Per-tier request timeouts (docs/engineering/28 timeout table). A slow
# tier needs a longer leash; a fast-tier call hanging for five minutes is a
# bug, not patience. Tiers absent here fall back to ``request_timeout``.
_DEFAULT_TIER_TIMEOUTS: dict[str, float] = {
    "fast": 30.0,
    "reasoning": 120.0,
    "coding": 300.0,
}


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
        litellm_acompletion: Callable[..., Any] | None = None,
        litellm_completion_cost: Callable[..., float] | None = None,
        request_timeout: float = 60.0,
        tier_timeouts: dict[str, float] | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        darwin_id: str | None = None,
    ) -> None:
        if not isinstance(tier_mappings, dict) or not tier_mappings:
            raise ValueError("tier_mappings must be a non-empty dict")
        self._wallet = wallet
        # Per-Darwin attribution (doc 13 §Observability + §Caching). When the
        # transport is bound to a Darwin, every completion carries
        # ``metadata.darwin_id`` so the proxy tags the spend-log row and the
        # cache key with it — that's what the spend-table reconciliation
        # worker keys off and what keeps one Darwin's cache from polluting
        # another's. ``None`` (e.g. tests, a shared-key dev run) omits it.
        self._darwin_id = str(darwin_id) if darwin_id else None
        self._breaker = circuit_breaker or CircuitBreaker()
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
        # Per-tier timeouts (doc 28): start from the doc's table, let a
        # deployer override per tier. Tiers not listed use ``request_timeout``.
        self._tier_timeouts: dict[str, float] = {
            **_DEFAULT_TIER_TIMEOUTS,
            **{k: float(v) for k, v in (tier_timeouts or {}).items()},
        }
        # The genuine gateway uses ``_acompletion`` — the native-async client
        # driven on an isolated loop (see ``_invoke_completion`` and
        # BUG-drive-loop-tick1-freeze). An injected sync ``litellm_completion``
        # is a test double / custom transport and is called directly. Resolve
        # whatever wasn't injected from the real litellm module: the sync
        # completion only when there is no async path at all, the async client
        # only when neither was injected.
        self._acompletion: Callable[..., Any] | None = litellm_acompletion
        need_completion = litellm_completion is None and self._acompletion is None
        need_acompletion = self._acompletion is None and litellm_completion is None
        need_cost = litellm_completion_cost is None
        if need_completion or need_cost or need_acompletion:
            try:
                import litellm  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover
                raise ImportError(
                    "LiteLLMTransport requires the 'litellm' optional dep: "
                    "`pip install darwin-dna[llm]`"
                ) from exc
            if need_completion:
                litellm_completion = litellm.completion
            if need_acompletion:
                self._acompletion = litellm.acompletion
            if need_cost:
                litellm_completion_cost = litellm.completion_cost
        # When only the async path exists, the sync completion must never be
        # reached — guard it so a regression surfaces loudly instead of
        # silently re-entering the loop.
        if litellm_completion is None:
            def litellm_completion(**_kwargs: Any) -> Any:  # noqa: ANN401
                raise LLMError(
                    "synchronous litellm.completion is unavailable; "
                    "the async (acompletion) path must be used"
                )
        self._completion: Callable[..., Any] = litellm_completion
        self._completion_cost: Callable[..., float] = litellm_completion_cost
        # Per-task request preparers — keyed by request['task']. Each builds
        # the messages + a parser that shapes the provider response into the
        # narrow dict the genome's caller expects (see FakeLLM for the
        # reference shapes). Shared by ``complete`` and ``acomplete``.
        self._preparers: dict[
            str,
            Callable[[Tier, dict[str, Any]], _PreparedCall],
        ] = {
            "plan": self._prepare_plan,
            "write_long_form": self._prepare_write_long_form,
            "propose_patch": self._prepare_propose_patch,
        }

    # -- LLMTransport ------------------------------------------------------
    def _resolve(
        self, tier: str, request: dict[str, Any]
    ) -> tuple[Tier, _PreparedCall, str]:
        """Validate the tier + task and build the prepared call. Shared
        entry-point bookkeeping for both ``complete`` and ``acomplete``."""
        try:
            tier_enum = Tier(tier)
        except ValueError as exc:
            raise LLMError(f"unknown tier: {tier!r}") from exc
        task = request.get("task")
        prep = self._preparers.get(task) if isinstance(task, str) else None
        if prep is None:
            raise LLMError(f"unknown task: {task!r}")
        return tier_enum, prep(tier_enum, request), str(task)

    def complete(self, tier: str, request: dict[str, Any]) -> dict[str, Any]:
        tier_enum, call, task = self._resolve(tier, request)
        try:
            result = self._call_with_fallback(
                tier_enum, call.messages,
                response_format=call.response_format,
                max_tokens=call.max_tokens,
            )
            return call.parse(result)
        except LLMError:
            raise
        except Exception as exc:  # provider error, parser error, etc.
            raise LLMError(f"{task} via litellm failed: {exc}") from exc

    async def acomplete(
        self, tier: str, request: dict[str, Any]
    ) -> dict[str, Any]:
        """Native-async twin of :meth:`complete`. Awaits ``litellm.acompletion``
        directly on the caller's event loop — no worker thread, no blocking,
        and crucially no sync→async re-entrancy (which is what froze the
        supervisor's uvloop drive loop; see BUG-drive-loop-tick1-freeze). This
        is the path the genome's nodes/neurons use in live operation so one
        Darwin's LLM call never stalls the loop the others share."""
        tier_enum, call, task = self._resolve(tier, request)
        try:
            result = await self._acall_with_fallback(
                tier_enum, call.messages,
                response_format=call.response_format,
                max_tokens=call.max_tokens,
            )
            return call.parse(result)
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

    def _timeout_for(self, tier: Tier) -> float:
        """The request timeout for ``tier`` (doc 28). Slow tiers get a
        longer leash; tiers without a configured timeout use the default."""
        return self._tier_timeouts.get(tier.value, self._timeout)

    def _base_kwargs(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tier: Tier,
        *,
        response_format: dict[str, Any] | None,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        """Build the LiteLLM call kwargs shared by the sync + async paths.

        Centralises per-request shaping — model, messages, timeout, optional
        JSON mode / token cap, and the per-Darwin ``metadata`` LiteLLM
        attributes spend + cache against (doc 13). Keeping it in one place
        means the two fallback loops can never drift on what they send."""
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "timeout": self._timeout_for(tier),
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if self._darwin_id is not None:
            # LiteLLM logs ``metadata`` against the call's virtual key in its
            # spend table and folds it into the cache key; ``tier`` rides
            # along so per-tier spend stays attributable post-hoc.
            kwargs["metadata"] = {
                "darwin_id": self._darwin_id,
                "tier": tier.value,
            }
        return kwargs

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
        skipped: list[str] = []
        for model in self._models_for(tier):
            if not self._breaker.can_call(model):
                skipped.append(model)
                continue
            try:
                kwargs = self._base_kwargs(
                    model, messages, tier,
                    response_format=response_format, max_tokens=max_tokens,
                )
                response = self._invoke_completion(kwargs, self._timeout_for(tier))
            except Exception as exc:
                last_exc = exc
                self._breaker.record_failure(model)
                continue
            text = _extract_text(response)
            cost = _extract_cost(response, self._completion_cost)
            self._bill_wallet(tier, cost)
            self._breaker.record_success(model)
            return _CompletionResult(
                text=text, provider_cost_usdc=cost, model=model, raw=response
            )
        if skipped and last_exc is None:
            raise LLMError(
                f"every model in tier {tier.value} is circuit-open "
                f"(skipped {len(skipped)})"
            )
        raise LLMError(
            f"every model in tier {tier.value} failed; "
            f"last error: {last_exc!r}"
        )

    async def _acall_with_fallback(
        self,
        tier: Tier,
        messages: list[dict[str, Any]],
        *,
        response_format: dict[str, Any] | None = None,
        max_tokens: int | None = None,
    ) -> _CompletionResult:
        """Async twin of :meth:`_call_with_fallback`. Identical fallback,
        circuit-breaker, and billing semantics; the only difference is the
        provider call is *awaited* (cooperative, non-blocking) instead of run
        on an isolated worker thread."""
        last_exc: Exception | None = None
        skipped: list[str] = []
        for model in self._models_for(tier):
            if not self._breaker.can_call(model):
                skipped.append(model)
                continue
            try:
                kwargs = self._base_kwargs(
                    model, messages, tier,
                    response_format=response_format, max_tokens=max_tokens,
                )
                response = await self._aprovider_call(kwargs)
            except Exception as exc:
                last_exc = exc
                self._breaker.record_failure(model)
                continue
            text = _extract_text(response)
            cost = _extract_cost(response, self._completion_cost)
            self._bill_wallet(tier, cost)
            self._breaker.record_success(model)
            return _CompletionResult(
                text=text, provider_cost_usdc=cost, model=model, raw=response
            )
        if skipped and last_exc is None:
            raise LLMError(
                f"every model in tier {tier.value} is circuit-open "
                f"(skipped {len(skipped)})"
            )
        raise LLMError(
            f"every model in tier {tier.value} failed; "
            f"last error: {last_exc!r}"
        )

    async def _aprovider_call(self, kwargs: dict[str, Any]) -> Any:
        """One provider call on the async path. The genuine gateway awaits
        the native-async client directly on the running loop — no thread, no
        re-entrancy. An injected sync double (test/fake) has no async client,
        so it is called inline; that's safe because such doubles are instant
        and never touch the network or the loop."""
        if self._acompletion is not None:
            return await self._acompletion(**kwargs)
        return self._completion(**kwargs)

    def _invoke_completion(
        self, kwargs: dict[str, Any], call_timeout: float
    ) -> Any:
        """Make one provider completion call.

        For an injected test double (or any non-litellm completion), call it
        directly — same as before, fully synchronous, no surprises.

        For the genuine LiteLLM gateway, drive the **native-async** client
        (``litellm.acompletion``) on a private event loop in a dedicated
        worker thread, and block this thread for the result. We must NEVER
        call the synchronous ``litellm.completion`` from the supervisor's
        running event loop: litellm bridges sync→async by re-entering the
        running loop (via ``nest_asyncio``), and on uvloop that corrupts the
        loop's timer scheduling — I/O keeps working but ``asyncio.sleep``
        never fires again, so the drive loop freezes after one heartbeat
        (BUG-drive-loop-tick1-freeze). The async client on its own fresh
        ``asyncio.run`` loop needs no re-entrancy, so nest_asyncio is never
        triggered against the supervisor loop. A fresh thread per call keeps
        a wedged provider call from blocking the next one.

        Blocking the caller here is the SAME cost the old synchronous path
        already paid (it ran ``litellm.completion`` inline on the loop); the
        win is that the loop's timers survive the call.
        """
        if self._acompletion is None:
            return self._completion(**kwargs)

        box: dict[str, Any] = {}

        def _worker() -> None:
            try:
                box["result"] = asyncio.run(self._acompletion(**kwargs))
            except BaseException as exc:  # noqa: BLE001 - re-raised on caller
                box["error"] = exc

        thread = threading.Thread(
            target=_worker, name="darwin-litellm", daemon=True
        )
        thread.start()
        # Margin beyond litellm's own per-call timeout so the provider-side
        # timeout fires first with a clean error rather than this backstop.
        thread.join(call_timeout + 30.0)
        if thread.is_alive():
            raise LLMError(
                f"litellm acompletion did not return within "
                f"{call_timeout + 30.0:.0f}s"
            )
        if "error" in box:
            raise box["error"]
        return box["result"]

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        """For ops inspection + tests; the breaker holds per-model state."""
        return self._breaker

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

    # -- task preparers (build messages + parser; see _PreparedCall) -------
    def _prepare_plan(self, tier: Tier, request: dict[str, Any]) -> _PreparedCall:
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

        def parse(result: _CompletionResult) -> dict[str, Any]:
            return {"text": result.text, "cost_usdc": result.provider_cost_usdc}

        return _PreparedCall(messages=messages, parse=parse, max_tokens=300)

    def _prepare_write_long_form(
        self, tier: Tier, request: dict[str, Any]
    ) -> _PreparedCall:
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

        def parse(result: _CompletionResult) -> dict[str, Any]:
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

        return _PreparedCall(messages=messages, parse=parse, max_tokens=cap)

    def _prepare_propose_patch(
        self, tier: Tier, request: dict[str, Any]
    ) -> _PreparedCall:
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

        def parse(result: _CompletionResult) -> dict[str, Any]:
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

        return _PreparedCall(
            messages=messages,
            parse=parse,
            response_format={"type": "json_object"},
            max_tokens=800,
        )


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
