"""Opt-in, metadata-only OTLP/HTTP tracing for the API process."""

import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


class MetadataSpanProcessor(BatchSpanProcessor):
    """Exclude MCP SDK spans: their exception events/status can contain upstream text.

    The shared loop records safe tool metadata instead. Anthropic's default
    metadata-only spans pass through unchanged; never enable its content mode.
    """

    def on_end(self, span: ReadableSpan) -> None:
        if span.instrumentation_scope and span.instrumentation_scope.name == 'mcp-python-sdk':
            return
        super().on_end(span)


def configure() -> TracerProvider | None:
    """Leave the API's no-op provider alone unless an endpoint explicitly opts in."""
    if os.environ.get('OTEL_SDK_DISABLED', '').strip().lower() == 'true' or not any(
        os.environ.get(name, '').strip()
        for name in ('OTEL_EXPORTER_OTLP_ENDPOINT', 'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT')
    ):
        return None
    provider = TracerProvider(
        resource=Resource.create({'service.name': os.environ.get('OTEL_SERVICE_NAME') or 'preflight-oss-api'}),
        shutdown_on_exit=False,  # The application lifespan owns flushing and shutdown.
    )
    provider.add_span_processor(MetadataSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    return provider


def get_tracer() -> trace.Tracer:
    """Resolve the API tracer at use time; callers can supply an isolated provider."""
    return trace.get_tracer('preflight.chat')
