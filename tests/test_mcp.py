"""Exercise the real MCP SDK with fictional fixtures; live SQL is checked separately."""

from collections import Counter
from contextlib import contextmanager
import csv
from datetime import datetime
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import patch

from mcp import Client, MCPError
import psycopg
from starlette.testclient import TestClient

from mcp_service.server import create_http_app, create_server
from preflight_core.settings import SettingsError

DATA = Path(__file__).resolve().parents[1] / 'data' / 'synthetic'
TOKEN = '1' * 64  # Test-only value; deployment generates its own secret.


class FixtureRepository:
    """Independent CSV oracle for protocol tests, never a production fallback."""

    def __init__(self):
        self.rows: dict[str, list[dict[str, Any]]] = {}
        self.calls = 0
        self.failure = None
        for table in ('aircraft', 'issues', 'tasks', 'activity_events'):
            with (DATA / (table + '.csv')).open(newline='') as handle:
                self.rows[table] = [{k: v or None for k, v in r.items()} for r in csv.DictReader(handle)]

    def healthy(self):
        return True

    def rows_for(self, aircraft_id):
        return [i for i in self.rows['issues'] if i['aircraft_id'] == aircraft_id]

    def aircraft_summary(self, aircraft_id):
        self.calls += 1
        aircraft = next((a for a in self.rows['aircraft'] if a['aircraft_id'] == aircraft_id), None)
        if aircraft is None:
            raise LookupError('Aircraft not found')
        issues = [i for i in self.rows['issues'] if i['aircraft_id'] == aircraft_id]
        ids = {i['issue_id'] for i in issues}
        tasks = [t for t in self.rows['tasks'] if t['issue_id'] in ids]
        issue_counts = dict.fromkeys(('open', 'in_progress', 'blocked', 'reopened', 'closed'), 0)
        task_counts = dict.fromkeys(('todo', 'in_progress', 'awaiting_review', 'completed', 'blocked'), 0)
        issue_counts.update(Counter(i['status'] for i in issues))
        task_counts.update(Counter(t['status'] for t in tasks))
        return {
            'aircraft': aircraft,
            'issue_count': len(issues),
            'unresolved_issue_count': sum(i['status'] != 'closed' for i in issues),
            'issue_status_counts': issue_counts,
            'task_count': len(tasks),
            'task_status_counts': task_counts,
        }

    def open_issues(self, aircraft_id, priority, limit, offset):
        self.calls += 1
        rows = [
            i
            for i in self.rows['issues']
            if i['status'] != 'closed'
            and (aircraft_id is None or i['aircraft_id'] == aircraft_id)
            and (priority is None or i['priority'] == priority)
        ]
        return {'total': len(rows), 'limit': limit, 'offset': offset, 'items': rows[offset : offset + limit]}

    def issue_counts(self, group_by, aircraft_id, status, subsystem, priority, unresolved_only):
        self.calls += 1
        column = {'subsystem': 'subsystem', 'priority': 'priority', 'status': 'status', 'aircraft': 'aircraft_id'}[
            group_by
        ]
        rows = [
            i
            for i in self.rows['issues']
            if (aircraft_id is None or i['aircraft_id'] == aircraft_id)
            and (status is None or i['status'] == status)
            and (subsystem is None or i['subsystem'] == subsystem)
            and (priority is None or i['priority'] == priority)
            and (not unresolved_only or i['status'] != 'closed')
        ]
        counts = Counter(r[column] for r in rows)
        return {
            'group_by': group_by,
            'unresolved_only': unresolved_only,
            'total': len(rows),
            'groups': [{'key': key, 'count': count} for key, count in sorted(counts.items())],
        }

    def compare_aircraft(self, aircraft_ids):
        self.calls += 1
        summaries = [self.aircraft_summary(i) for i in dict.fromkeys(aircraft_ids)]
        ranked = sorted(
            (
                {
                    'aircraft_id': s['aircraft']['aircraft_id'],
                    'unresolved_issue_count': s['unresolved_issue_count'],
                    'issue_count': s['issue_count'],
                }
                for s in summaries
            ),
            key=lambda r: (-r['unresolved_issue_count'], r['aircraft_id']),
        )
        spread = ranked[0]['unresolved_issue_count'] - ranked[-1]['unresolved_issue_count']
        return {'summaries': summaries, 'ranked_by_unresolved': ranked, 'unresolved_spread': spread}

    def search_issues(self, status, subsystem, priority, aircraft_id, opened_after, opened_before, limit, offset):
        self.calls += 1
        rows = [
            i
            for i in self.rows['issues']
            if (status is None or i['status'] == status)
            and (subsystem is None or i['subsystem'] == subsystem)
            and (priority is None or i['priority'] == priority)
            and (aircraft_id is None or i['aircraft_id'] == aircraft_id)
            and (opened_after is None or datetime.fromisoformat(i['opened_at']) >= opened_after)
            and (opened_before is None or datetime.fromisoformat(i['opened_at']) <= opened_before)
        ]
        rows.sort(key=lambda i: i['issue_id'])
        return {'total': len(rows), 'limit': limit, 'offset': offset, 'items': rows[offset : offset + limit]}

    def issue_history(self, issue_id, limit, offset):
        self.calls += 1
        issue = next((i for i in self.rows['issues'] if i['issue_id'] == issue_id), None)
        if issue is None:
            raise LookupError('Issue not found')
        events = sorted(
            (e for e in self.rows['activity_events'] if e['issue_id'] == issue_id),
            key=lambda e: (e['occurred_at'], e['event_id']),
        )
        return {
            'issue': issue,
            'total_events': len(events),
            'limit': limit,
            'offset': offset,
            'events': events[offset : offset + limit],
        }


class McpContract(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repo = FixtureRepository()

        @contextmanager
        def factory():
            if self.repo.failure:
                raise self.repo.failure
            yield self.repo

        self.factory = factory
        self.server = create_server(repository_factory=factory)

    async def test_only_the_six_read_only_tools_are_discoverable(self):
        async with Client(self.server) as client:
            listing = await client.list_tools()
        self.assertEqual(
            {t.name for t in listing.tools},
            {
                'get_aircraft_summary',
                'list_open_issues',
                'get_issue_history',
                'count_issues',
                'compare_aircraft',
                'search_issues',
            },
        )
        for tool in listing.tools:
            assert tool.annotations is not None
            self.assertTrue(tool.annotations.read_only_hint)
            self.assertFalse(tool.annotations.destructive_hint)
            self.assertTrue(tool.annotations.idempotent_hint)
            self.assertFalse(tool.annotations.open_world_hint)
            self.assertIsNotNone(tool.output_schema)

    async def test_aircraft_summary_preserves_issue_and_task_distinction(self):
        async with Client(self.server) as client:
            result = await client.call_tool('get_aircraft_summary', {'aircraft_id': 'DEMO-AIR-0001'})
        self.assertFalse(result.is_error)
        data = result.structured_content
        expected = self.repo.aircraft_summary('DEMO-AIR-0001')
        for key in ('issue_count', 'unresolved_issue_count', 'issue_status_counts', 'task_count', 'task_status_counts'):
            self.assertEqual(data[key], expected[key])
        self.assertEqual(data['aircraft']['aircraft_id'], 'DEMO-AIR-0001')

    async def test_unresolved_issues_filter_and_pages_do_not_overlap(self):
        async with Client(self.server) as client:
            a = await client.call_tool('list_open_issues', {'priority': 'high', 'limit': 3})
            b = await client.call_tool('list_open_issues', {'priority': 'high', 'limit': 3, 'offset': 3})
            filtered = await client.call_tool('list_open_issues', {'aircraft_id': 'DEMO-AIR-0001'})
        expected = self.repo.open_issues(None, 'high', 6, 0)
        joined = a.structured_content['items'] + b.structured_content['items']
        self.assertEqual([i['issue_id'] for i in joined], [i['issue_id'] for i in expected['items']])
        self.assertEqual(a.structured_content['total'], expected['total'])
        self.assertTrue(all(i['status'] != 'closed' for i in joined))
        self.assertTrue(all(i['aircraft_id'] == 'DEMO-AIR-0001' for i in filtered.structured_content['items']))

    async def test_reopened_history_contains_closure_before_reopening(self):
        issue = next(i for i in self.repo.rows['issues'] if i['status'] == 'reopened')
        async with Client(self.server) as client:
            result = await client.call_tool('get_issue_history', {'issue_id': issue['issue_id']})
        data = result.structured_content
        self.assertFalse(result.is_error)
        self.assertIsNone(data['issue']['closed_at'])
        kinds = [e['event_type'] for e in data['events']]
        self.assertLess(kinds.index('issue_closed'), kinds.index('issue_reopened'))
        self.assertTrue(all(e['issue_id'] == issue['issue_id'] for e in data['events']))
        self.assertEqual(data['total_events'], len(data['events']))

    async def test_history_pagination_and_empty_issue_filter(self):
        async with Client(self.server) as client:
            history = await client.call_tool('get_issue_history', {'issue_id': 'ISSUE-0001', 'limit': 1, 'offset': 1})
            empty = await client.call_tool('list_open_issues', {'aircraft_id': 'DEMO-AIR-9999'})
        expected = self.repo.issue_history('ISSUE-0001', 1, 1)
        self.assertEqual(history.structured_content['events'][0]['event_id'], expected['events'][0]['event_id'])
        self.assertEqual(empty.structured_content['total'], 0)
        self.assertEqual(empty.structured_content['items'], [])

    async def test_count_issues_matches_the_csv_oracle_per_group_by(self):
        async with Client(self.server) as client:
            for group_by in ('subsystem', 'priority', 'status', 'aircraft'):
                for unresolved_only in (False, True):
                    with self.subTest(group_by=group_by, unresolved_only=unresolved_only):
                        result = await client.call_tool(
                            'count_issues', {'group_by': group_by, 'unresolved_only': unresolved_only}
                        )
                        self.assertFalse(result.is_error)
                        expected = self.repo.issue_counts(group_by, None, None, None, None, unresolved_only)
                        self.assertEqual(result.structured_content, expected)
                        self.assertEqual(
                            sum(g['count'] for g in result.structured_content['groups']), expected['total']
                        )
            filtered = await client.call_tool('count_issues', {'group_by': 'priority', 'aircraft_id': 'DEMO-AIR-0001'})
        self.assertEqual(
            filtered.structured_content['total'], self.repo.aircraft_summary('DEMO-AIR-0001')['issue_count']
        )

    async def test_count_groups_omit_empty_keys(self):
        closed_only = next(
            (
                a
                for a in self.repo.rows['aircraft']
                if all(i['status'] == 'closed' for i in self.repo.rows_for(a['aircraft_id']))
            ),
            None,
        )
        async with Client(self.server) as client:
            none = await client.call_tool(
                'count_issues',
                {
                    'group_by': 'subsystem',
                    'aircraft_id': 'DEMO-AIR-0001',
                    'subsystem': 'avionics',
                    'status': 'closed',
                    'priority': 'low',
                },
            )
            by_aircraft = await client.call_tool('count_issues', {'group_by': 'aircraft', 'unresolved_only': True})
        keys = [g['key'] for g in by_aircraft.structured_content['groups']]
        self.assertTrue(all(g['count'] > 0 for g in by_aircraft.structured_content['groups']))
        self.assertEqual(keys, sorted(keys))
        if closed_only is not None:
            self.assertNotIn(closed_only['aircraft_id'], keys)
        self.assertLessEqual(none.structured_content['total'], 1)
        self.assertEqual(len(none.structured_content['groups']), none.structured_content['total'])

    async def test_compare_ranks_by_unresolved_and_reports_the_spread(self):
        ids = ['DEMO-AIR-0003', 'DEMO-AIR-0001', 'DEMO-AIR-0002']
        async with Client(self.server) as client:
            result = await client.call_tool('compare_aircraft', {'aircraft_ids': ids})
        self.assertFalse(result.is_error)
        data = result.structured_content
        self.assertEqual([s['aircraft']['aircraft_id'] for s in data['summaries']], ids)
        counts = [r['unresolved_issue_count'] for r in data['ranked_by_unresolved']]
        self.assertEqual(counts, sorted(counts, reverse=True))
        self.assertEqual(data['unresolved_spread'], max(counts) - min(counts))
        expected = self.repo.compare_aircraft(ids)
        self.assertEqual(data['ranked_by_unresolved'], expected['ranked_by_unresolved'])
        self.assertEqual(data['unresolved_spread'], expected['unresolved_spread'])
        for got, want in zip(data['summaries'], expected['summaries'], strict=True):
            for key in ('issue_count', 'unresolved_issue_count', 'issue_status_counts', 'task_count'):
                self.assertEqual(got[key], want[key])

    async def test_compare_rejects_duplicates_and_bounds(self):
        before = self.repo.calls
        async with Client(self.server) as client:
            one = await client.call_tool('compare_aircraft', {'aircraft_ids': ['DEMO-AIR-0001', 'DEMO-AIR-0001']})
            seven = await client.call_tool(
                'compare_aircraft', {'aircraft_ids': [f'DEMO-AIR-000{i}' for i in range(1, 8)]}
            )
            self.assertTrue(one.is_error)
            self.assertTrue(seven.is_error)
            self.assertEqual(self.repo.calls, before)  # bound failures never reach the repository
            missing = await client.call_tool('compare_aircraft', {'aircraft_ids': ['DEMO-AIR-0001', 'DEMO-AIR-9999']})
        self.assertTrue(missing.is_error)
        self.assertIn('not found', ' '.join(getattr(c, 'text', '') for c in missing.content))

    async def test_search_filters_paginate_and_include_closed(self):
        async with Client(self.server) as client:
            closed = await client.call_tool('search_issues', {'status': 'closed', 'limit': 100})
            cabin_a = await client.call_tool('search_issues', {'subsystem': 'cabin', 'limit': 4})
            cabin_b = await client.call_tool('search_issues', {'subsystem': 'cabin', 'limit': 4, 'offset': 4})
            everything = await client.call_tool('search_issues', {'limit': 100, 'offset': 100})
        self.assertTrue(all(i['status'] == 'closed' for i in closed.structured_content['items']))
        self.assertEqual(
            closed.structured_content['total'],
            self.repo.search_issues('closed', None, None, None, None, None, 100, 0)['total'],
        )
        joined = cabin_a.structured_content['items'] + cabin_b.structured_content['items']
        self.assertEqual(
            [i['issue_id'] for i in joined],
            [i['issue_id'] for i in self.repo.search_issues(None, 'cabin', None, None, None, None, 8, 0)['items']],
        )
        self.assertTrue(all(i['subsystem'] == 'cabin' for i in joined))
        self.assertEqual(everything.structured_content['total'], 180)
        self.assertEqual(len(everything.structured_content['items']), 80)

    async def test_search_date_window_can_be_empty(self):
        async with Client(self.server) as client:
            empty = await client.call_tool(
                'search_issues',
                {'opened_after': '2026-09-01T00:00:00+00:00', 'opened_before': '2026-02-01T00:00:00+00:00'},
            )
            window = await client.call_tool(
                'search_issues',
                {
                    'opened_after': '2026-03-01T00:00:00+00:00',
                    'opened_before': '2026-04-01T00:00:00+00:00',
                    'limit': 100,
                },
            )
        self.assertFalse(empty.is_error)
        self.assertEqual((empty.structured_content['total'], empty.structured_content['items']), (0, []))
        self.assertTrue(
            all(
                '2026-03' in i['opened_at'] or '2026-04-01' in i['opened_at']
                for i in window.structured_content['items']
            )
        )
        self.assertGreater(window.structured_content['total'], 0)

    async def test_new_tool_invalid_arguments_never_reach_repository(self):
        cases: list[tuple[str, dict[str, Any]]] = [
            ('count_issues', {'group_by': 'sql'}),
            ('count_issues', {'group_by': 'status', 'subsystem': 'engine'}),
            ('count_issues', {'group_by': 'status', 'unresolved_only': 'yes'}),
            ('search_issues', {'opened_after': 'yesterday'}),
            ('search_issues', {'limit': 101}),
            ('compare_aircraft', {'aircraft_ids': 'DEMO-AIR-0001'}),
            ('compare_aircraft', {'aircraft_ids': ["x' OR 1=1 --", 'DEMO-AIR-0001']}),
        ]
        async with Client(self.server) as client:
            for name, args in cases:
                with self.subTest(name=name, arguments=args):
                    try:
                        result = await client.call_tool(name, args)
                    except MCPError as exc:
                        self.assertEqual(exc.code, -32602)
                    else:
                        self.assertTrue(result.is_error)
        self.assertEqual(self.repo.calls, 0)

    async def test_invalid_arguments_never_reach_repository(self):
        cases: list[tuple[str, dict[str, Any]]] = [
            ('get_aircraft_summary', {'aircraft_id': "x' OR 1=1 --"}),
            ('get_issue_history', {'issue_id': '../private'}),
            ('list_open_issues', {'priority': 'urgent'}),
            ('list_open_issues', {'limit': 0}),
            ('list_open_issues', {'limit': 101}),
            ('list_open_issues', {'limit': True}),
            ('list_open_issues', {'offset': -1}),
            ('list_open_issues', {'offset': 10001}),
            ('list_open_issues', {'sql': 'SELECT * FROM pg_roles'}),
        ]
        async with Client(self.server) as client:
            for name, args in cases:
                with self.subTest(name=name, arguments=args):
                    try:
                        result = await client.call_tool(name, args)
                    except MCPError as exc:
                        self.assertEqual(exc.code, -32602)
                    else:
                        self.assertTrue(result.is_error)
        self.assertEqual(self.repo.calls, 0)

    async def test_missing_records_return_explicit_errors(self):
        async with Client(self.server) as client:
            for name, args in [
                ('get_aircraft_summary', {'aircraft_id': 'DEMO-AIR-9999'}),
                ('get_issue_history', {'issue_id': 'ISSUE-9999'}),
            ]:
                result = await client.call_tool(name, args)
                self.assertTrue(result.is_error)
                self.assertIn('not found', ' '.join(getattr(c, 'text', '') for c in result.content))

    async def test_unknown_tool_cannot_run_arbitrary_sql(self):
        async with Client(self.server) as client:
            try:
                result = await client.call_tool('execute_sql', {'sql': 'DELETE FROM preflight.issues'})
            except MCPError as exc:
                self.assertEqual(exc.code, -32602)
            else:
                self.assertTrue(result.is_error)
        self.assertEqual(self.repo.calls, 0)

    async def test_database_failure_does_not_expose_connection_details(self):
        self.repo.failure = psycopg.OperationalError('DO_NOT_EXPOSE_CONNECTION_DETAILS')
        async with Client(self.server) as client:
            result = await client.call_tool('get_aircraft_summary', {'aircraft_id': 'DEMO-AIR-0001'})
        self.assertTrue(result.is_error)
        text = ' '.join(getattr(c, 'text', '') for c in result.content)
        self.assertIn('Database unavailable', text)
        self.assertNotIn('DO_NOT_EXPOSE', text)

    def test_http_requires_token_for_health_and_protocol(self):
        app = create_http_app(repository_factory=self.factory, token=TOKEN, allowed_hosts=['testserver'])
        with TestClient(app) as client:
            for path in ('/health', '/mcp'):
                self.assertEqual(client.get(path).status_code, 401)
                self.assertEqual(client.get(path, headers={'Authorization': 'Bearer incorrect'}).status_code, 401)
            response = client.get('/health', headers={'Authorization': 'Bearer ' + TOKEN})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {'status': 'ok', 'database': 'ok'})

    def test_http_rejects_untrusted_host_and_origin(self):
        app = create_http_app(repository_factory=self.factory, token=TOKEN, allowed_hosts=['testserver'])
        base = {'Authorization': 'Bearer ' + TOKEN, 'Accept': 'application/json, text/event-stream'}
        with TestClient(app) as client:
            for extra in ({'Host': 'untrusted.example'}, {'Origin': 'https://untrusted.example'}):
                response = client.post(
                    '/mcp', headers=base | extra, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}
                )
                self.assertIn(response.status_code, (403, 421))

    def test_http_host_allowlist_comes_from_configuration(self):
        with patch.dict('os.environ', {'MCP_ALLOWED_HOSTS': 'demo-mcp.example:*'}):
            configured = create_http_app(repository_factory=self.factory, token=TOKEN)
        with patch.dict('os.environ', {}, clear=True):
            default = create_http_app(repository_factory=self.factory, token=TOKEN)
        base = {'Authorization': 'Bearer ' + TOKEN, 'Accept': 'application/json, text/event-stream'}
        body = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}
        for app, allowed in ((configured, 'demo-mcp.example:8001'), (default, '127.0.0.1:8001')):
            with TestClient(app) as client:
                rejected = client.post('/mcp', headers=base | {'Host': 'preflight-mcp:8001'}, json=body)
                self.assertIn(rejected.status_code, (403, 421))
                accepted = client.post('/mcp', headers=base | {'Host': allowed}, json=body)
                self.assertNotIn(accepted.status_code, (403, 421))

    def test_missing_authentication_configuration_fails_closed(self):
        with patch.dict('os.environ', {}, clear=True), self.assertRaisesRegex(SettingsError, 'MCP_TOKEN_FILE'):
            create_http_app(repository_factory=self.factory)

    def test_health_reports_database_failure_without_details(self):
        self.repo.failure = psycopg.OperationalError('DO_NOT_EXPOSE_CONNECTION_DETAILS')
        app = create_http_app(repository_factory=self.factory, token=TOKEN, allowed_hosts=['testserver'])
        with TestClient(app) as client:
            response = client.get('/health', headers={'Authorization': 'Bearer ' + TOKEN})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'error': 'Database unavailable'})

    def test_http_request_body_is_bounded(self):
        app = create_http_app(repository_factory=self.factory, token=TOKEN, allowed_hosts=['testserver'])
        with TestClient(app) as client:
            response = client.post(
                '/mcp',
                headers={
                    'Authorization': 'Bearer ' + TOKEN,
                    'Accept': 'application/json, text/event-stream',
                    'Content-Type': 'application/json',
                },
                content=' ' * 65537,
            )
        self.assertEqual(response.status_code, 413)


if __name__ == '__main__':
    unittest.main()
