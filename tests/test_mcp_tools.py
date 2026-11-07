"""Tool discovery and calls over the real MCP HTTP app, bearer middleware included; no network."""

from contextlib import asynccontextmanager, contextmanager
import unittest

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from test_mcp import FixtureRepository

from backend.mcp_tools import McpTools, anthropic_tools
from mcp_service.server import create_http_app

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

    def client_factory(self, url, headers):
        http = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self.app), base_url='http://testserver', headers=headers
        )
        return Client(streamable_http_client(url, http_client=http))

    def tools(self, token=TOKEN):
        return McpTools(URL, token, client_factory=self.client_factory)

    async def test_discover_returns_the_three_registry_tools_with_schemas(self):
        async with self.running():
            specs = await self.tools().discover()
            self.assertEqual([s.name for s in specs], ['get_aircraft_summary', 'list_open_issues', 'get_issue_history'])
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
            missing = await self.tools().call('c', 'get_issue_history', {'issue_id': 'ISSUE-9999'})
            self.assertFalse(missing.ok)
            self.assertEqual(missing.error, 'Tool unavailable')

    async def test_anthropic_tools_include_final_answer_last(self):
        async with self.running():
            specs = await self.tools().discover()
            tools = anthropic_tools(specs)
            self.assertEqual([t['name'] for t in tools][-1], 'final_answer')
            self.assertEqual(len(tools), 4)
            self.assertEqual(set(tools[0]), {'name', 'description', 'input_schema'})
            self.assertIn('cited_ids', tools[-1]['input_schema']['properties'])


if __name__ == '__main__':
    unittest.main()
