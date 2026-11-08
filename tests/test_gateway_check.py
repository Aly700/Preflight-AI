"""Gateway parity and timing use injected services; no network or credential lookup."""

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend import gateway_check
from backend.mcp_tools import ToolResult, ToolSpec
from backend.settings import ChatSettings
from preflight_core.tools import TOOLS

TOKEN = 'c' * 64
DIRECT_URL = 'http://direct.example.test/mcp'
GATEWAY_URL = 'https://gateway.example.test/mcp'


class FakeService:
    def __init__(self, *, mismatch=False, names=None, refused=False, failed=False, missing_error='Record not found'):
        self.mismatch = mismatch
        self.names = list(TOOLS) if names is None else names
        self.refused = refused
        self.failed = failed
        self.missing_error = missing_error
        self.calls = []

    async def discover(self):
        return [ToolSpec(name, '', {}) for name in self.names]

    async def call(self, call_id, name, arguments):
        self.calls.append((name, dict(arguments)))
        if self.refused or self.failed:
            return ToolResult(name, call_id, False, error='Tool unavailable')
        if arguments.get('aircraft_id') == 'DEMO-AIR-9999':
            return ToolResult(name, call_id, False, error=self.missing_error)
        return ToolResult(name, call_id, True, payload={'fictional': not self.mismatch, 'arguments': arguments})


class GatewayCheckContract(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.check = gateway_check

    async def report(self, direct=None, gateway=None, unsigned=None, repeats=3, clock=lambda: 0.0):
        return await self.check.run_check(
            direct or FakeService(),
            gateway or FakeService(),
            unsigned or FakeService(refused=True),
            repeats=repeats,
            clock=clock,
        )

    async def test_matching_services_cover_all_tools_and_missing_record(self):
        direct, gateway, unsigned = FakeService(), FakeService(), FakeService(refused=True)
        report = await self.report(direct, gateway, unsigned)
        self.assertTrue(report['passed'])
        self.assertTrue(report['unsigned_refused'])
        self.assertEqual(report['discovered'], {'direct': list(TOOLS), 'gateway': list(TOOLS)})
        self.assertEqual(len(report['cases']), 7)
        self.assertTrue(all(case['match'] for case in report['cases']))
        self.assertEqual(direct.calls, gateway.calls)
        expected = [
            ('get_aircraft_summary', {'aircraft_id': 'DEMO-AIR-0001'}),
            ('list_open_issues', {'aircraft_id': 'DEMO-AIR-0002', 'limit': 3}),
            ('get_issue_history', {'issue_id': 'ISSUE-0001', 'limit': 3}),
            ('count_issues', {'group_by': 'priority', 'aircraft_id': 'DEMO-AIR-0001'}),
            ('compare_aircraft', {'aircraft_ids': ['DEMO-AIR-0001', 'DEMO-AIR-0002']}),
            ('search_issues', {'aircraft_id': 'DEMO-AIR-0002', 'limit': 3}),
            ('get_aircraft_summary', {'aircraft_id': 'DEMO-AIR-9999'}),
        ]
        self.assertEqual(direct.calls, [case for case in expected for _ in range(3)])
        self.assertEqual(unsigned.calls, [expected[0]])

    async def test_payload_mismatch_fails(self):
        report = await self.report(gateway=FakeService(mismatch=True))
        self.assertFalse(report['passed'])
        self.assertFalse(report['cases'][0]['match'])

    async def test_discovery_mismatch_or_incomplete_discovery_fails(self):
        for direct in (FakeService(), FakeService(names=[])):
            with self.subTest(direct=direct.names):
                report = await self.report(direct=direct, gateway=FakeService(names=[]))
                self.assertFalse(report['passed'])

    async def test_accepted_unsigned_call_fails(self):
        report = await self.report(unsigned=FakeService())
        self.assertFalse(report['unsigned_refused'])
        self.assertFalse(report['passed'])

    async def test_matching_failures_and_wrong_missing_record_error_fail(self):
        for direct, gateway in (
            (FakeService(failed=True), FakeService(failed=True)),
            (FakeService(), FakeService(missing_error='Database unavailable')),
            (FakeService(missing_error='Database unavailable'), FakeService(missing_error='Database unavailable')),
        ):
            with self.subTest(error=gateway.missing_error, failed=direct.failed):
                self.assertFalse((await self.report(direct, gateway))['passed'])

    async def test_all_repeats_must_match(self):
        gateway = FakeService()
        original = gateway.call

        async def one_mismatch(call_id, name, arguments):
            result = await original(call_id, name, arguments)
            if len(gateway.calls) == 1:
                return ToolResult(name, call_id, True, payload={'different': True})
            return result

        with patch.object(gateway, 'call', side_effect=one_mismatch):
            report = await self.report(gateway=gateway)
        self.assertFalse(report['passed'])
        self.assertFalse(report['cases'][0]['match'])

    async def test_medians_use_every_timed_call(self):
        timestamps: list[float] = []
        now = 0.0
        for _ in range(7):
            for duration in (1, 3, 9, 7, 5, 11):
                timestamps.extend((now, now + duration))
                now += duration
        timestamps.extend((now, now + 100))  # unsigned call is timed, but excluded from the service medians
        clock = iter(timestamps)
        report = await self.report(clock=lambda: next(clock))
        self.assertEqual(report['median_ms'], {'direct': 5000, 'gateway': 7000})
        for case in report['cases']:
            self.assertEqual(case['direct_ms'], 5000)
            self.assertEqual(case['gateway_ms'], 7000)
        self.assertEqual(list(clock), [])

    async def test_nonpositive_repeats_are_rejected(self):
        for repeats in (0, -1):
            with self.subTest(repeats=repeats), self.assertRaisesRegex(ValueError, 'repeats must be positive'):
                await self.report(repeats=repeats)


class GatewayCheckCli(unittest.TestCase):
    def setUp(self):
        self.check = gateway_check
        self.settings = ChatSettings(
            'a' * 64,
            None,
            None,
            GATEWAY_URL,
            runtime='native',
            mcp_auth='sigv4',
            aws_region='ca-central-1',
            mcp_tool_prefix='target___',
        )

    def invoke(self, gateway, unsigned, args=()):
        direct = FakeService()
        stdout = io.StringIO()
        with (
            patch.object(self.check.ChatSettings, 'from_environment', return_value=self.settings),
            patch.object(self.check, 'read_mcp_token', return_value=TOKEN) as token,
            patch.object(self.check, 'build_tool_service', return_value=gateway) as build,
            patch.object(self.check, 'McpTools', side_effect=[direct, unsigned]) as services,
            patch('sys.stdout', stdout),
        ):
            code = self.check.main(['--direct-url', DIRECT_URL, *args])
        token.assert_called_once()
        build.assert_called_once_with(self.settings)
        self.assertEqual(services.call_args_list[0].args, (DIRECT_URL, TOKEN))
        self.assertEqual(services.call_args_list[1].args, (GATEWAY_URL, None))
        self.assertEqual(services.call_args_list[1].kwargs, {'tool_prefix': 'target___'})
        return code, stdout.getvalue(), direct

    def test_matching_cli_exits_zero_and_writes_the_same_safe_json(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'report.json'
            code, text, direct = self.invoke(FakeService(), FakeService(refused=True), ['--output', str(output)])
            self.assertEqual(code, 0)
            self.assertEqual(output.read_text(), text)
        report = json.loads(text)
        self.assertTrue(report['passed'])
        self.assertEqual(len(direct.calls), 21)
        self.assertEqual(set(report), {'discovered', 'cases', 'unsigned_refused', 'median_ms', 'passed'})
        self.assertEqual(set(report['cases'][0]), {'name', 'tool', 'match', 'direct_ms', 'gateway_ms'})
        for secret in (TOKEN, DIRECT_URL, GATEWAY_URL, 'AKIDEXAMPLE', 'fake-secret', 'fake-session-token'):
            self.assertNotIn(secret, text)

    def test_mismatches_and_unsigned_acceptance_exit_one(self):
        for gateway, unsigned in (
            (FakeService(mismatch=True), FakeService(refused=True)),
            (FakeService(names=[]), FakeService(refused=True)),
            (FakeService(), FakeService()),
        ):
            with self.subTest(names=gateway.names, mismatch=gateway.mismatch, unsigned=unsigned.refused):
                code, text, direct = self.invoke(gateway, unsigned, ['--repeats', '1'])
                self.assertEqual(code, 1)
                self.assertFalse(json.loads(text)['passed'])
                self.assertEqual(len(direct.calls), 7)

    def test_configuration_or_discovery_errors_emit_only_safe_failure_json(self):
        secret_error = TOKEN + ' https://user:password@gateway.example.test/mcp fake-session-token'
        for failure in ('settings', 'discovery'):
            with self.subTest(failure=failure):
                stdout, stderr = io.StringIO(), io.StringIO()
                gateway = FakeService()
                with (
                    patch.object(self.check.ChatSettings, 'from_environment', return_value=self.settings) as settings,
                    patch.object(self.check, 'read_mcp_token', return_value=TOKEN),
                    patch.object(self.check, 'build_tool_service', return_value=gateway),
                    patch.object(self.check, 'McpTools', side_effect=[FakeService(), FakeService(refused=True)]),
                    patch.object(gateway, 'discover', side_effect=RuntimeError(secret_error)),
                    patch('sys.stdout', stdout),
                    patch('sys.stderr', stderr),
                ):
                    if failure == 'settings':
                        settings.side_effect = RuntimeError(secret_error)
                    code = self.check.main(['--direct-url', DIRECT_URL])
                self.assertEqual(code, 1)
                report = json.loads(stdout.getvalue())
                self.assertFalse(report['passed'])
                # Only the exception type is reported, so a live failure can be diagnosed without leaking values.
                self.assertEqual(report['error'], 'RuntimeError')
                for value in (TOKEN, 'password', 'gateway.example.test', 'fake-session-token'):
                    self.assertNotIn(value, stdout.getvalue() + stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
