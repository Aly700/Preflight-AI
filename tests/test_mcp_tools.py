"""Tool discovery and calls over the real MCP HTTP app, bearer middleware included; no network."""

from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from test_mcp import FixtureRepository

from backend import mcp_tools
from backend.aws_auth import SigV4Auth
from backend.mcp_tools import McpTools, anthropic_tools
from backend.settings import ChatSettings
from mcp_service.server import create_http_app
from preflight_core.tools import TOOLS

TOKEN = '2' * 64
URL = 'http://testserver/mcp'


class McpToolsContract(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repo = FixtureRepository()

        @contextmanager
        def factory():
            yield self.repo

        self.app = create_http_app(factory, token=TOKEN, allowed_hosts=['testserver'])

    @asynccontextmanager
    async def running(self):
        # The MCP session manager starts in the app's lifespan, which an ASGI transport does not run by itself;
        # anyio requires the scope to open and close in the same task, so each test enters it.
        async with self.app.router.lifespan_context(self.app):
            yield

    @asynccontextmanager
    async def client_factory(self, url, headers):
        http = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self.app), base_url='http://testserver', headers=headers
        )
        async with http, Client(streamable_http_client(url, http_client=http)) as client:
            yield client

    def tools(self, token=TOKEN):
        return McpTools(URL, token, client_factory=self.client_factory)

    async def test_prefixed_discovery_filters_and_preserves_registry_checks(self):
        def offered(name, properties):
            return SimpleNamespace(name=name, description='Fictional tool', input_schema={'properties': properties})

        listing = [offered('target___' + name, {}) for name in reversed(list(TOOLS))]
        listing += [
            offered('get_aircraft_summary', {'force': {}}),
            offered('other___get_aircraft_summary', {}),
            offered('target___unknown_tool', {}),
            offered('target___get_issue_history', {'force': {}}),
        ]
        # The invalid history definition must be dropped even after prefix stripping.
        listing = [
            tool for tool in listing if tool.name != 'target___get_issue_history' or tool.input_schema['properties']
        ]

        @asynccontextmanager
        async def factory(url, headers):
            yield SimpleNamespace(list_tools=AsyncMock(return_value=SimpleNamespace(tools=listing)))

        tools = McpTools(URL, TOKEN, client_factory=factory, tool_prefix='target___')
        with self.assertLogs('preflight.tools', level='WARNING'):
            specs = await tools.discover()
        self.assertEqual([spec.name for spec in specs], [name for name in TOOLS if name != 'get_issue_history'])

    async def test_call_prefixes_service_name_and_preserves_validation_and_errors(self):
        payload = self.repo.aircraft_summary('DEMO-AIR-0001')
        call = AsyncMock(return_value=SimpleNamespace(is_error=False, structured_content=payload))

        @asynccontextmanager
        async def factory(url, headers):
            yield SimpleNamespace(call_tool=call)

        tools = McpTools(URL, TOKEN, client_factory=factory, tool_prefix='target___')
        result = await tools.call('c', 'get_aircraft_summary', {'aircraft_id': 'DEMO-AIR-0001'})
        self.assertTrue(result.ok)
        self.assertEqual(result.name, 'get_aircraft_summary')
        call.assert_awaited_once_with('target___get_aircraft_summary', {'aircraft_id': 'DEMO-AIR-0001'})
        for message in ('Record not found', 'Database unavailable', 'private upstream detail'):
            call.return_value = SimpleNamespace(is_error=True, content=[SimpleNamespace(text='Gateway: ' + message)])
            result = await tools.call('c', 'get_aircraft_summary', {'aircraft_id': 'DEMO-AIR-9999'})
            self.assertFalse(result.ok)
            self.assertEqual(result.error, message if message != 'private upstream detail' else 'Tool unavailable')
        before = call.await_count
        self.assertFalse((await tools.call('c', 'target___get_aircraft_summary', {})).ok)
        self.assertFalse((await tools.call('c', 'get_aircraft_summary', {'force': True})).ok)
        self.assertEqual(call.await_count, before)

    async def test_no_token_sends_no_authorization_header(self):
        captured = []

        @asynccontextmanager
        async def factory(url, headers):
            captured.append(headers)
            yield SimpleNamespace(list_tools=AsyncMock(return_value=SimpleNamespace(tools=[])))

        await McpTools(URL, None, client_factory=factory).discover()
        self.assertEqual(captured, [{}])

    async def test_default_factory_passes_auth_to_http_client(self):
        auth = SigV4Auth('ca-central-1', credentials=lambda: None)
        with (
            patch('backend.mcp_tools.httpx2.AsyncClient') as http,
            patch('backend.mcp_tools.streamable_http_client'),
            patch('backend.mcp_tools.Client') as client,
        ):
            client.return_value.__aenter__.return_value.list_tools = AsyncMock(return_value=SimpleNamespace(tools=[]))
            await McpTools(URL, None, auth=auth, timeout=3.5).discover()
            http.assert_called_once_with(headers={}, timeout=3.5, auth=auth)

    async def test_build_tool_service_selects_auth_and_prefix(self):
        captured = []

        @asynccontextmanager
        async def factory(url, headers):
            captured.append((url, headers))
            tool = SimpleNamespace(name='target___get_aircraft_summary', description='', input_schema={})
            yield SimpleNamespace(list_tools=AsyncMock(return_value=SimpleNamespace(tools=[tool])))

        for mode in ('bearer', 'sigv4'):
            with (
                self.subTest(mode=mode),
                patch('backend.mcp_tools.default_client_factory', return_value=factory) as build,
            ):
                settings = ChatSettings(
                    'a' * 64,
                    None,
                    TOKEN,
                    URL,
                    runtime='native',
                    aws_region='ca-central-1',
                    mcp_auth=mode,
                    mcp_tool_prefix='target___',
                )
                specs = await mcp_tools.build_tool_service(settings).discover()
                self.assertEqual([spec.name for spec in specs], ['get_aircraft_summary'])
                auth = build.call_args.args[1]
                if mode == 'sigv4':
                    self.assertIsInstance(auth, SigV4Auth)
                    self.assertEqual(auth.region, 'ca-central-1')
                    self.assertEqual(captured[-1], (URL, {}))
                else:
                    self.assertIsNone(auth)
                    self.assertEqual(captured[-1], (URL, {'Authorization': 'Bearer ' + TOKEN}))

    async def test_native_runtime_constructs_tools_through_builder(self):
        from backend.agent_runtime import stream_answer

        settings = ChatSettings('a' * 64, None, None, URL, runtime='native')
        with (
            patch('backend.agent_runtime.build_tool_service') as build,
            patch('backend.agent_runtime.build_model_client', return_value=AsyncMock()),
            patch('backend.agent_runtime.Loop') as loop,
        ):
            loop.return_value.run = AsyncMock()
            _ = [event async for event in stream_answer('Fictional question', settings)]
            build.assert_called_once_with(settings)
            self.assertIs(loop.call_args.args[2], build.return_value)

    async def test_discover_returns_the_six_registry_tools_with_schemas(self):
        async with self.running():
            specs = await self.tools().discover()
            self.assertEqual(
                [s.name for s in specs],
                [
                    'get_aircraft_summary',
                    'list_open_issues',
                    'get_issue_history',
                    'count_issues',
                    'compare_aircraft',
                    'search_issues',
                ],
            )
            page = next(s for s in specs if s.name == 'list_open_issues')
            self.assertEqual(set(page.input_schema['properties']), {'aircraft_id', 'priority', 'limit', 'offset'})
            self.assertTrue(page.description)

    async def test_call_validates_result_and_collects_payload(self):
        async with self.running():
            result = await self.tools().call('call-1', 'get_aircraft_summary', {'aircraft_id': 'DEMO-AIR-0001'})
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.call_id, 'call-1')
            self.assertEqual(result.payload['issue_count'], self.repo.aircraft_summary('DEMO-AIR-0001')['issue_count'])
            self.assertEqual(result.payload['aircraft']['aircraft_id'], 'DEMO-AIR-0001')

    async def test_unknown_tool_or_argument_is_refused_without_a_call(self):
        async with self.running():
            before = self.repo.calls
            unknown = await self.tools().call('c', 'drop_table', {})
            extra = await self.tools().call(
                'c', 'get_aircraft_summary', {'aircraft_id': 'DEMO-AIR-0001', 'force': True}
            )
            self.assertFalse(unknown.ok)
            self.assertFalse(extra.ok)
            self.assertEqual(self.repo.calls, before)
            self.assertEqual(unknown.error, 'Tool unavailable')

    async def test_transport_failure_is_reported_not_raised(self):
        async with self.running():
            result = await self.tools(token='3' * 64).call(
                'c', 'get_aircraft_summary', {'aircraft_id': 'DEMO-AIR-0001'}
            )
            self.assertFalse(result.ok)
            self.assertEqual(result.error, 'Tool unavailable')

    async def test_transport_failure_logs_leaf_exception_types(self):
        secret = 'upstream-secret https://private.invalid'
        errors = (
            (RuntimeError(secret), 'RuntimeError'),
            (
                ExceptionGroup(
                    secret,
                    [
                        ValueError(secret),
                        ExceptionGroup(secret, [TimeoutError(secret), ValueError(secret)]),
                        ConnectionError(secret),
                    ],
                ),
                'ConnectionError,TimeoutError,ValueError',
            ),
        )
        for error, expected in errors:
            with self.subTest(error=type(error).__name__):

                @asynccontextmanager
                async def factory(url, headers, error=error):
                    yield SimpleNamespace(call_tool=AsyncMock(side_effect=error))

                tools = McpTools(URL, TOKEN, client_factory=factory)
                with self.assertLogs('preflight.tools', level='WARNING') as captured:
                    result = await tools.call('c', 'get_aircraft_summary', {'aircraft_id': 'DEMO-AIR-0001'})
                self.assertFalse(result.ok)
                self.assertEqual(result.error, 'Tool unavailable')
                self.assertEqual(
                    captured.output, [f'WARNING:preflight.tools:Tool get_aircraft_summary failed: {expected}']
                )
                self.assertNotIn(secret, '\n'.join(captured.output))

    async def test_missing_record_is_reported_as_not_found(self):
        async with self.running():
            missing = await self.tools().call('c', 'get_issue_history', {'issue_id': 'ISSUE-9999'})
            self.assertFalse(missing.ok)
            self.assertEqual(missing.error, 'Record not found')

    async def test_http_client_is_closed_after_each_use(self):
        clients = []

        @asynccontextmanager
        async def factory(url, headers):
            http = httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=self.app), base_url='http://testserver', headers=headers
            )
            clients.append(http)
            async with http, Client(streamable_http_client(url, http_client=http)) as client:
                yield client

        tools = McpTools(URL, TOKEN, client_factory=factory)
        async with self.running():
            await tools.discover()
            await tools.call('c', 'get_aircraft_summary', {'aircraft_id': 'DEMO-AIR-0001'})
        self.assertEqual(len(clients), 2)
        self.assertTrue(all(http.is_closed for http in clients))

    async def test_anthropic_tools_exclude_final_answer(self):
        async with self.running():
            specs = await self.tools().discover()
        tools = anthropic_tools(specs)
        self.assertNotIn('final_answer', [t['name'] for t in tools])
        self.assertEqual([t['name'] for t in tools], [s.name for s in specs])
        self.assertEqual(len(tools), 6)
        self.assertEqual(set(tools[0]), {'name', 'description', 'input_schema'})


if __name__ == '__main__':
    unittest.main()
