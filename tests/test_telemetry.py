"""Tracing configuration and export privacy, entirely in memory; no collector is contacted."""

import importlib
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


class Telemetry(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('backend.telemetry'), 'telemetry module must exist')
        self.telemetry = importlib.import_module('backend.telemetry')

    def test_unconfigured_or_disabled_leaves_api_provider_untouched(self):
        original = trace.get_tracer_provider()
        for environment in (
            {},
            {'OTEL_EXPORTER_OTLP_ENDPOINT': ''},
            {'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT': ''},
            {'OTEL_EXPORTER_OTLP_ENDPOINT': '   '},
            {'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://collector.invalid:4318', 'OTEL_SDK_DISABLED': 'true'},
            {'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT': 'http://collector.invalid/v1/traces', 'OTEL_SDK_DISABLED': 'TRUE'},
        ):
            with (
                self.subTest(environment=environment),
                patch.dict('os.environ', environment, clear=True),
                patch.object(self.telemetry, 'TracerProvider') as provider,
                patch.object(self.telemetry, 'OTLPSpanExporter') as exporter,
                patch.object(trace, 'set_tracer_provider') as install,
            ):
                self.assertIsNone(self.telemetry.configure())
                provider.assert_not_called()
                exporter.assert_not_called()
                install.assert_not_called()
                self.assertIs(trace.get_tracer_provider(), original)

    def test_either_endpoint_enables_batch_export_with_resource_and_service_name(self):
        for endpoint in ('OTEL_EXPORTER_OTLP_ENDPOINT', 'OTEL_EXPORTER_OTLP_TRACES_ENDPOINT'):
            for service in (None, 'custom-api'):
                environment = {
                    endpoint: 'http://collector.invalid:4318',
                    'OTEL_SDK_DISABLED': 'false',
                    'OTEL_RESOURCE_ATTRIBUTES': 'deployment.environment.name=test',
                }
                if service is not None:
                    environment['OTEL_SERVICE_NAME'] = service
                exporter = InMemorySpanExporter()
                with (
                    self.subTest(endpoint=endpoint, service=service),
                    patch.dict('os.environ', environment, clear=True),
                    patch.object(self.telemetry, 'OTLPSpanExporter', return_value=exporter) as factory,
                    patch.object(trace, 'set_tracer_provider') as install,
                ):
                    provider = self.telemetry.configure()
                    self.addCleanup(provider.shutdown)
                    self.assertIsInstance(provider, TracerProvider)
                    install.assert_called_once_with(provider)
                    factory.assert_called_once_with()  # exporter reads standard OTEL_* itself
                    self.assertEqual(provider.resource.attributes['service.name'], service or 'preflight-oss-api')
                    self.assertEqual(provider.resource.attributes['deployment.environment.name'], 'test')
                    self.assertIn('telemetry.sdk.name', provider.resource.attributes)
                    with provider.get_tracer('test').start_as_current_span('test'):
                        pass
                    self.assertTrue(provider.force_flush())
                    self.assertEqual(len(exporter.get_finished_spans()), 1)

    def test_mcp_exception_events_and_status_text_are_not_exported(self):
        from mcp.shared import _otel

        exporter = InMemorySpanExporter()
        with (
            patch.dict('os.environ', {'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://collector.invalid:4318'}, clear=True),
            patch.object(self.telemetry, 'OTLPSpanExporter', return_value=exporter),
            patch.object(trace, 'set_tracer_provider'),
        ):
            provider = self.telemetry.configure()
        self.addCleanup(provider.shutdown)
        with patch.object(_otel, '_tracer', provider.get_tracer('mcp-python-sdk')):
            with (
                self.assertRaisesRegex(ValueError, 'private tool result'),
                _otel.otel_span('MCP send tools/call get_issue_history', kind=trace.SpanKind.CLIENT),
            ):
                raise ValueError('private tool result')
            with _otel.otel_span('MCP send tools/call get_issue_history', kind=trace.SpanKind.CLIENT) as span:
                span.set_status(trace.StatusCode.ERROR, 'private upstream error')
        with provider.get_tracer('anthropic').start_as_current_span('anthropic.messages.create'):
            pass
        self.assertTrue(provider.force_flush())
        self.assertEqual([s.name for s in exporter.get_finished_spans()], ['anthropic.messages.create'])

    def test_app_lifespan_shuts_down_provider_and_flushes_pending_spans(self):
        from backend.app import create_app

        class RetainingExporter(InMemorySpanExporter):
            def shutdown(self):
                self.stopped = True

        for fail in (False, True):
            exporter = RetainingExporter()
            with (
                self.subTest(fail=fail),
                patch.dict('os.environ', {'OTEL_EXPORTER_OTLP_ENDPOINT': 'http://collector.invalid:4318'}, clear=True),
                patch.object(self.telemetry, 'OTLPSpanExporter', return_value=exporter),
                patch.object(trace, 'set_tracer_provider') as install,
            ):
                try:
                    with TestClient(create_app()):
                        provider = install.call_args.args[0]
                        with provider.get_tracer('test').start_as_current_span('pending'):
                            pass
                        if fail:
                            raise RuntimeError('app exit')
                except RuntimeError:
                    if not fail:
                        raise
                self.assertTrue(exporter.stopped)
                self.assertEqual([s.name for s in exporter.get_finished_spans()], ['pending'])

    def test_app_lifespan_without_tracing_does_not_install_a_provider(self):
        from backend.app import create_app

        with patch.dict('os.environ', {}, clear=True), patch.object(trace, 'set_tracer_provider') as install:
            with TestClient(create_app()):
                pass
            install.assert_not_called()
