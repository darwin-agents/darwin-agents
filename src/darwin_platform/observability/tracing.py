"""OpenTelemetry tracing scaffold (docs/engineering/08 §Observability).

Doc 08: "The whole platform emits OpenTelemetry traces. Every interesting
event (spawn, kill, neuron dispatch, payment, escalation, mutation proposal,
reproduction) is a span. Spans are tagged with darwin_id, lineage_root_id,
generation, and per-event metadata."

This module is the platform side of that contract. The Supervisor's methods
wrap their bodies in spans using :func:`span` (a context manager); the
default tracer provider is a NoOp until something configures the SDK with an
exporter, so:

- Tests that don't care about traces pay zero overhead (NoOp spans).
- Tests that *do* care register an InMemorySpanExporter and assert.
- Production code wires up a real exporter (OTLP, Jaeger, Honeycomb) at
  startup; the supervisor code does not need to change.

The OpenTelemetry API is on the platform extra (``pip install
darwin-dna[platform]``). When the API is unavailable (slim install) we
fall back to a tiny shim so imports don't fail — the price is that the
fallback `span()` is a no-op even if the test wants to see the trace.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import Span as _OtelSpan, Status, StatusCode

    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - slim install path
    _otel_trace = None  # type: ignore[assignment]
    _OtelSpan = None  # type: ignore[assignment]
    Status = None  # type: ignore[assignment]
    StatusCode = None  # type: ignore[assignment]
    _OTEL_AVAILABLE = False


_INSTRUMENTATION_NAME = "darwin-platform"


def is_otel_available() -> bool:
    """True when the OpenTelemetry API is importable. Tests use this to
    skip tracing assertions on slim installs."""
    return _OTEL_AVAILABLE


def get_tracer():
    """Return the platform's tracer. With OTel installed this is a real
    tracer (NoOp until the SDK configures an exporter); without OTel it's
    the fallback shim defined below."""
    if _OTEL_AVAILABLE:
        return _otel_trace.get_tracer(_INSTRUMENTATION_NAME)
    return _NoOpTracer()


# Module-level guard so a double-call (e.g. test + serve) doesn't stack
# two exporters on the provider.
_OTLP_CONFIGURED = False


def otlp_traces_url(endpoint: str) -> str:
    """Normalise a collector endpoint to the OTLP/HTTP traces URL.

    The OTLP/HTTP spec puts traces at ``/v1/traces``; operators pass the
    collector base (``http://jaeger:4318``) and we append the path when
    it is not already there. Pure — extracted so it is unit-testable
    without installing a global TracerProvider."""
    url = endpoint.rstrip("/")
    if not url.endswith("/v1/traces"):
        url = f"{url}/v1/traces"
    return url


def configure_otlp_tracing(
    endpoint: str,
    *,
    service_name: str = _INSTRUMENTATION_NAME,
    headers: dict[str, str] | None = None,
) -> bool:
    """Wire a real OTLP/HTTP span exporter so the spans the supervisor
    already emits via :func:`span` actually leave the process.

    ``endpoint`` is the collector's base URL (e.g. ``http://jaeger:4318``
    in the dev compose stack); the OTLP/HTTP traces path
    (``/v1/traces``) is appended when absent. Called once at
    ``darwin-platform serve`` startup when ``--otlp-endpoint`` (or the
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` env var) is set.

    Returns True when an exporter was installed, False when OpenTelemetry
    is not available (slim install — tracing stays NoOp). Raises
    :class:`ImportError` when the OTLP exporter package is missing so the
    operator gets a clear install hint rather than silent data loss.
    """
    global _OTLP_CONFIGURED
    if not _OTEL_AVAILABLE:
        return False
    if _OTLP_CONFIGURED:
        return True
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
    except ImportError as exc:  # pragma: no cover - missing optional dep
        raise ImportError(
            "OTLP trace export requires opentelemetry-exporter-otlp-proto-http "
            "— `pip install darwin-dna[platform]`"
        ) from exc

    traces_url = otlp_traces_url(endpoint)

    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name})
    )
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=traces_url, headers=headers or {})
        )
    )
    _otel_trace.set_tracer_provider(provider)
    _OTLP_CONFIGURED = True
    return True


@contextmanager
def span(
    name: str,
    *,
    darwin_id: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> Iterator[Any]:
    """Start a span. The returned object has ``set_attribute(k, v)`` and
    ``set_status(...)`` whether OTel is installed or not — call sites can
    use them unconditionally.

    Common attributes (per doc 08): ``darwin_id``, ``generation``,
    ``lineage_root_id``. ``span()`` accepts ``darwin_id`` as a top-level
    argument and any extra attributes via the ``attributes`` dict.
    """
    tracer = get_tracer()
    attrs = {}
    if darwin_id is not None:
        attrs["darwin_id"] = darwin_id
    if attributes:
        for k, v in attributes.items():
            if v is not None:
                attrs[k] = v
    if _OTEL_AVAILABLE:
        with tracer.start_as_current_span(name, attributes=attrs) as s:
            try:
                yield s
            except Exception as exc:  # pragma: no cover - re-raised
                s.set_status(Status(StatusCode.ERROR, str(exc)))
                s.record_exception(exc)
                raise
    else:  # pragma: no cover
        yield _NoOpSpan()


def annotate_span_with_runtime(
    s: Any, runtime: Any, *, extras: dict[str, Any] | None = None
) -> None:
    """Tag a span with the doc-08 standard attributes derived from a Darwin's
    runtime: ``darwin_id``, ``generation``, ``lineage_root_id``. Safe with
    either a NoOp or a real span."""
    try:
        identity = runtime.identity
    except Exception:
        return
    lineage = list(getattr(identity, "lineage", []) or [])
    root_id = lineage[0] if lineage else identity.darwin_id
    s.set_attribute("darwin_id", identity.darwin_id)
    s.set_attribute("generation", int(getattr(identity, "generation", 0)))
    s.set_attribute("lineage_root_id", root_id)
    if extras:
        for k, v in extras.items():
            if v is not None:
                s.set_attribute(k, v)


# -- fallback shim when OTel isn't installed -------------------------------
class _NoOpSpan:  # pragma: no cover - exercised only without OTel
    def set_attribute(self, *_args, **_kw) -> None:
        return None

    def set_status(self, *_args, **_kw) -> None:
        return None

    def record_exception(self, *_args, **_kw) -> None:
        return None

    def __enter__(self) -> "_NoOpSpan":
        return self

    def __exit__(self, *_a) -> None:
        return None


class _NoOpTracer:  # pragma: no cover - exercised only without OTel
    @contextmanager
    def start_as_current_span(self, *_a, **_kw):
        yield _NoOpSpan()
