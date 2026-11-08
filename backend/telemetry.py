"""Opt-in, metadata-only OTLP/HTTP tracing for the API process."""

from collections.abc import Sequence
import os

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, Decision, ParentBased, Sampler, SamplingResult
from opentelemetry.trace import Link, SpanKind
from opentelemetry.trace.span import TraceState
from opentelemetry.util.types import Attributes


class MetadataSpanProcessor(BatchSpanProcessor):
    """Exclude MCP SDK spans: their exception events/status can contain upstream text.

    The shared loop records safe tool metadata instead. Anthropic's default
    metadata-only spans pass through unchanged; never enable its content mode.
    """

    def on_end(self, span: ReadableSpan) -> None:
        if span.instrumentation_scope and span.instrumentation_scope.name == 'mcp-python-sdk':
            return
        super().on_end(span)


class SkipHealthChecks(Sampler):
    """Drop the container health check's traces, which arrive every few seconds; sample the rest."""

    def should_sample(
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        trace_state: TraceState | None = None,
    ) -> SamplingResult:
        if (attributes or {}).get('url.path') == '/health' or name.endswith(' /health'):
            return SamplingResult(Decision.DROP)
        return ALWAYS_ON.should_sample(parent_context, trace_id, name, kind, attributes, links, trace_state)

    def get_description(self) -> str:
        return 'SkipHealthChecks'


def configure() -> TracerProvider | None:
    """Leave the API's no-op provider alone unless an endpoint explicitly opts in."""
    if os.environ.get('OTEL_SDK_DISABLED', '').strip().lower() == 'true' or not any(
        os.environ.get(name, '').strip()
        for name in ('OTEL_EXPORTER_OTLP_ENDPOINT', 'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT')
    ):
        return None
    provider = TracerProvider(
        resource=Resource.create({'service.name': os.environ.get('OTEL_SERVICE_NAME') or 'preflight-oss-api'}),
        # A health check's child spans follow its root's decision, so the whole trace is dropped.
        sampler=ParentBased(root=SkipHealthChecks()),
        shutdown_on_exit=False,  # The application lifespan owns flushing and shutdown.
    )
    provider.add_span_processor(MetadataSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    return provider


def get_tracer() -> trace.Tracer:
    """Resolve the API tracer at use time; callers can supply an isolated provider."""
    return trace.get_tracer('preflight.chat')
