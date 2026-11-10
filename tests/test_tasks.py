"""Read-only task listing: real MCP validation and bound SQL checked offline."""

from contextlib import contextmanager
from copy import deepcopy
import sqlite3
from typing import Any
import unittest
from unittest.mock import Mock

from mcp import Client, MCPError

from mcp_service.repository import Repository
from mcp_service.server import create_server
from preflight_core.tools import TOOLS

TASK = {
    'task_id': 'TASK-0001',
    'issue_id': 'ISSUE-0001',
    'aircraft_id': 'DEMO-AIR-0001',
    'task_kind': 'review',
    'status': 'awaiting_review',
    'assigned_team_id': 'TEAM-0001',
    'assigned_person_id': None,
    'created_at': '2026-01-01T00:00:00Z',
    'completed_at': None,
}
FILTERS: dict[str, Any] = {
    'status': 'awaiting_review',
    'task_kind': 'review',
    'assigned_team_id': 'TEAM-0001',
    'assigned_person_id': 'PERSON-0001',
    'aircraft_id': 'DEMO-AIR-0001',
    'issue_id': 'ISSUE-0001',
}


class TaskSQL(unittest.TestCase):
    def test_sql_binds_all_filters_combines_with_and_and_counts_before_paging(self):
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = {'count': 19}
        connection.execute.return_value.fetchall.return_value = [TASK]
        result = Repository(connection).list_tasks(**FILTERS, limit=1, offset=2)
        self.assertEqual(result, {'total': 19, 'items': [TASK], 'limit': 1, 'offset': 2})
        count, page = connection.execute.call_args_list
        for call in (count, page):
            sql, args = call.args
            sql = ' '.join(sql.split())
            self.assertIn('FROM preflight.tasks t JOIN preflight.issues i ON i.issue_id = t.issue_id', sql)
            self.assertIn(' AND '.join(f'{"i" if key == "aircraft_id" else "t"}.{key} = %s' for key in FILTERS), sql)
            self.assertEqual(sql.count('%s'), len(args))
            for value in FILTERS.values():
                self.assertNotIn(value, sql)
        self.assertIn('count(*)', count.args[0])
        self.assertNotIn('LIMIT', count.args[0])
        self.assertIn('ORDER BY t.task_id LIMIT %s OFFSET %s', page.args[0])
        self.assertEqual(list(count.args[1]), list(FILTERS.values()))
        self.assertEqual(list(page.args[1]), [*FILTERS.values(), 1, 2])

    def test_blank_filters_add_no_condition(self):
        for blank in (None, '', '   '):
            connection = Mock()
            connection.execute.return_value.fetchone.return_value = {'count': 0}
            connection.execute.return_value.fetchall.return_value = []
            result = Repository(connection).list_tasks(**dict.fromkeys(FILTERS, blank))
            self.assertEqual(result, {'total': 0, 'items': [], 'limit': 20, 'offset': 0})
            for call in connection.execute.call_args_list:
                self.assertNotIn('WHERE', call.args[0])

    def test_actual_sql_filters_join_order_total_and_empty_page(self):
        # SQLite executes the SELECT shape with only the placeholder syntax adapted.
        database = sqlite3.connect(':memory:')
        self.addCleanup(database.close)
        database.row_factory = sqlite3.Row
        database.execute("ATTACH DATABASE ':memory:' AS preflight")
        database.execute('CREATE TABLE preflight.issues (issue_id TEXT, aircraft_id TEXT)')
        database.execute(
            'CREATE TABLE preflight.tasks (task_id TEXT, issue_id TEXT, task_kind TEXT, status TEXT, '
            'assigned_team_id TEXT, assigned_person_id TEXT, created_at TEXT, completed_at TEXT)'
        )
        database.executemany(
            'INSERT INTO preflight.issues VALUES (?, ?)',
            [('ISSUE-0001', 'DEMO-AIR-0001'), ('ISSUE-0002', 'DEMO-AIR-0002')],
        )
        for ident, team, status, issue in [
            ('0003', '0001', 'awaiting_review', '0001'),
            ('0001', '0001', 'awaiting_review', '0001'),
            ('0002', '0002', 'awaiting_review', '0001'),
            ('0004', '0001', 'completed', '0001'),
            ('0005', '0001', 'awaiting_review', '0002'),
        ]:
            database.execute(
                'INSERT INTO preflight.tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                [
                    f'TASK-{ident}',
                    f'ISSUE-{issue}',
                    'review',
                    status,
                    f'TEAM-{team}',
                    'PERSON-0001',
                    TASK['created_at'],
                    None,
                ],
            )
        connection = Mock()
        connection.execute.side_effect = lambda sql, args: database.execute(sql.replace('%s', '?'), args)
        repo = Repository(connection)
        page = repo.list_tasks(**FILTERS, limit=1, offset=1)
        self.assertEqual(page['total'], 2)
        self.assertEqual(dict(page['items'][0]), {**TASK, 'task_id': 'TASK-0003', 'assigned_person_id': 'PERSON-0001'})
        self.assertEqual(repo.list_tasks(**FILTERS, offset=2)['items'], [])
        self.assertEqual(repo.list_tasks(**FILTERS, offset=2)['total'], 2)


class TaskMCP(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repo = Mock()
        self.repo.list_tasks.return_value = {'total': 25, 'limit': 20, 'offset': 0, 'items': [TASK]}

        @contextmanager
        def factory():
            yield self.repo

        self.server = create_server(factory)

    async def test_discovery_defaults_registry_and_validated_output(self):
        self.assertIn('list_tasks', TOOLS)
        async with Client(self.server) as client:
            listing = await client.list_tools()
            tool = next(tool for tool in listing.tools if tool.name == 'list_tasks')
            self.assertEqual(set(tool.input_schema['properties']), set(FILTERS) | {'limit', 'offset'})
            self.assertEqual(tool.input_schema['properties']['limit']['default'], 20)
            self.assertEqual(tool.input_schema['properties']['limit']['maximum'], 50)
            assert tool.annotations is not None
            self.assertTrue(tool.annotations.read_only_hint)
            self.assertFalse(tool.annotations.destructive_hint)
            result = await client.call_tool('list_tasks', {})
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content, self.repo.list_tasks.return_value)
            self.repo.list_tasks.assert_called_once_with(None, None, None, None, None, None, 20, 0)
            self.repo.list_tasks.reset_mock()
            result = await client.call_tool('list_tasks', FILTERS | {'limit': 50, 'offset': 10000})
            self.assertFalse(result.is_error)
            self.repo.list_tasks.assert_called_once_with(*FILTERS.values(), 50, 10000)
        model = TOOLS['list_tasks'].result
        for change in ({'extra': 1}, {'limit': 51}):
            with self.assertRaises(ValueError):
                model.model_validate(self.repo.list_tasks.return_value | change)
        bad = deepcopy(self.repo.list_tasks.return_value)
        bad['items'][0]['status'] = 'closed'
        with self.assertRaises(ValueError):
            model.model_validate(bad)

    async def test_invalid_inputs_are_rejected_before_repository(self):
        cases: list[dict[str, Any]] = [
            {'status': 'closed'},
            {'task_kind': 'repair'},
            {'limit': 0},
            {'limit': 51},
            {'limit': True},
            {'limit': '20'},
            {'offset': -1},
            {'offset': 10001},
            {'offset': False},
            {'extra': 'x'},
        ]
        for key in FILTERS:
            cases += [{key: value} for value in (['x'], 1, "x' OR 1=1 --")]
        async with Client(self.server) as client:
            for args in cases:
                with self.subTest(args=args):
                    try:
                        result = await client.call_tool('list_tasks', args)
                    except MCPError as error:
                        self.assertEqual(error.code, -32602)
                    else:
                        self.assertTrue(result.is_error)
        self.repo.list_tasks.assert_not_called()

    async def test_all_statuses_kinds_and_blank_filters(self):
        async with Client(self.server) as client:
            for key, values in (
                ('status', ['todo', 'in_progress', 'awaiting_review', 'completed', 'blocked']),
                ('task_kind', ['review', 'investigation', 'documentation', 'inspection', 'rectification']),
            ):
                for value in values:
                    result = await client.call_tool('list_tasks', {key: value})
                    self.assertFalse(result.is_error)
            for blank in (None, '', '  '):
                self.repo.list_tasks.reset_mock()
                result = await client.call_tool('list_tasks', dict.fromkeys(FILTERS, blank))
                self.assertFalse(result.is_error)
                self.repo.list_tasks.assert_called_once_with(None, None, None, None, None, None, 20, 0)
