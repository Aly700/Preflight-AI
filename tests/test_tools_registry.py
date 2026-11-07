"""The tool registry is the single declaration; the MCP server and the adapter must agree with it."""

from contextlib import contextmanager
from types import SimpleNamespace
import unittest

from mcp import Client
from test_mcp import FixtureRepository

from backend import copilot_chat
from mcp_service.server import create_server
from preflight_core.models import AircraftComparison, IssueCounts
from preflight_core.tools import MCP_SERVER_NAME, QUALIFIED_NAMES, TOOL_CONTRACTS, TOOLS


class RegistryAgreement(unittest.IsolatedAsyncioTestCase):
    async def test_server_exposes_exactly_the_registry_with_matching_schemas(self):
        repository = FixtureRepository()

        @contextmanager
        def factory():
            yield repository

        async with Client(create_server(repository_factory=factory)) as client:
            listing = await client.list_tools()
        tools = {tool.name: tool for tool in listing.tools}
        self.assertEqual(set(tools), set(TOOLS))
        for name, contract in TOOLS.items():
            with self.subTest(tool=name):
                self.assertEqual(set(tools[name].input_schema['properties']), set(contract.arguments))
                schema = tools[name].output_schema
                assert schema is not None
                self.assertEqual(set(schema['properties']), set(contract.result.model_fields))

    def test_adapter_policies_recognise_registry_tools_only(self):
        for contract in TOOL_CONTRACTS:
            request = SimpleNamespace(kind='mcp', server_name=MCP_SERVER_NAME, tool_name=contract.name, read_only=True)
            self.assertEqual(copilot_chat.permission(request, {}).kind, 'approve-once')
            decision = copilot_chat.ToolPolicy().before({'toolName': contract.qualified_name}, {})
            self.assertEqual(decision['permissionDecision'], 'allow')
        rogue = SimpleNamespace(kind='mcp', server_name=MCP_SERVER_NAME, tool_name='execute_sql', read_only=True)
        self.assertEqual(copilot_chat.permission(rogue, {}).kind, 'reject')
        decision = copilot_chat.ToolPolicy().before({'toolName': MCP_SERVER_NAME + '-execute_sql'}, {})
        self.assertEqual(decision['permissionDecision'], 'deny')
        self.assertEqual(len(TOOL_CONTRACTS), 6)  # widening the allowlist is a reviewed decision, not a side effect
        self.assertEqual(set(QUALIFIED_NAMES.values()), set(TOOLS))

    def test_new_result_models_reject_extra_fields(self):
        counts = {
            'group_by': 'subsystem',
            'unresolved_only': True,
            'filters': {'aircraft_id': None, 'status': None, 'subsystem': None, 'priority': None},
            'total': 3,
            'groups': [{'key': 'cabin', 'count': 1}, {'key': 'hydraulics', 'count': 2}],
        }
        self.assertEqual(IssueCounts.model_validate(counts).model_dump(mode='json'), counts)
        with self.assertRaises(ValueError):
            IssueCounts.model_validate({**counts, 'sql': 'SELECT 1'})
        with self.assertRaises(ValueError):
            IssueCounts.model_validate({**counts, 'group_by': 'actor'})
        self.assertIn('summaries', AircraftComparison.model_fields)
        self.assertIn('unresolved_spread', AircraftComparison.model_fields)
        self.assertEqual(TOOLS['search_issues'].result.__name__, 'IssuePage')
        self.assertEqual(TOOLS['compare_aircraft'].arguments, frozenset({'aircraft_ids'}))

    def test_results_validate_only_against_their_own_contract(self):
        page = {'total': 0, 'limit': 50, 'offset': 0, 'items': []}
        self.assertEqual(copilot_chat.validate_result('list_open_issues', page), page)
        with self.assertRaises(ValueError):
            copilot_chat.validate_result('get_aircraft_summary', page)


if __name__ == '__main__':
    unittest.main()
