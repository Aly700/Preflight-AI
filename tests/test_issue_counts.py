"""Execute count SQL offline, including date endpoints and combined filters."""

from datetime import UTC, datetime
import sqlite3
from typing import Any
import unittest
from unittest.mock import Mock

from test_local_stack import load_script

from mcp_service.repository import Repository


class CountSQL(unittest.TestCase):
    def setUp(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.execute("ATTACH DATABASE ':memory:' AS preflight")
        db.execute('CREATE TABLE preflight.aircraft (aircraft_id TEXT PRIMARY KEY)')
        db.executemany('INSERT INTO preflight.aircraft VALUES (?)', [(f'DEMO-AIR-{i:04}',) for i in (3, 1, 2)])
        db.execute(
            'CREATE TABLE preflight.issues (aircraft_id TEXT, status TEXT, subsystem TEXT, '
            'priority TEXT, opened_at TEXT)'
        )
        for aircraft, status, subsystem, priority, timestamp in [
            ('DEMO-AIR-0001', 'open', 'cabin', 'high', '2026-01-01T00:00:00+00:00'),
            ('DEMO-AIR-0001', 'blocked', 'cabin', 'high', '2026-01-31T23:59:59.999999+00:00'),
            ('DEMO-AIR-0001', 'open', 'cabin', 'high', '2025-12-31T23:59:59.999999+00:00'),
            ('DEMO-AIR-0001', 'open', 'cabin', 'high', '2026-02-01T00:00:00+00:00'),
            ('DEMO-AIR-0001', 'closed', 'cabin', 'high', '2026-01-10T00:00:00+00:00'),
            ('DEMO-AIR-0002', 'open', 'cabin', 'high', '2026-01-10T00:00:00+00:00'),
            ('DEMO-AIR-0001', 'open', 'controls', 'high', '2026-01-10T00:00:00+00:00'),
            ('DEMO-AIR-0001', 'open', 'cabin', 'low', '2026-01-10T00:00:00+00:00'),
        ]:
            db.execute(
                'INSERT INTO preflight.issues VALUES (?, ?, ?, ?, ?)',
                (aircraft, status, subsystem, priority, timestamp),
            )
        connection = Mock()
        connection.execute.side_effect = lambda sql, args: db.execute(
            sql.replace('%s', '?'), [value.isoformat() if isinstance(value, datetime) else value for value in args]
        )
        self.db = db
        self.connection = connection
        self.repo = Repository(connection)

    def count(self, group_by='status', **filters):
        args: dict[str, Any] = {
            'aircraft_id': None,
            'status': None,
            'subsystem': None,
            'priority': None,
            'unresolved_only': False,
        }
        result = self.repo.issue_counts(group_by, **(args | filters))
        self.assertEqual(result['total'], sum(g['count'] for g in result['groups']))
        keys = [g['key'] for g in result['groups']]
        self.assertEqual(keys, sorted(keys))
        return {g['key']: g['count'] for g in result['groups']}

    def test_status_domain_includes_zero_counts_for_a_subsystem(self):
        self.assertEqual(
            self.count(subsystem='controls'),
            {'blocked': 0, 'closed': 0, 'in_progress': 0, 'open': 1, 'reopened': 0},
        )
        self.connection.execute.assert_called_once()

    def test_unresolved_status_domain_excludes_closed(self):
        self.assertEqual(
            self.count(subsystem='controls', unresolved_only=True),
            {'blocked': 0, 'in_progress': 0, 'open': 1, 'reopened': 0},
        )

    def test_status_filter_restricts_domain_even_when_empty(self):
        self.assertEqual(self.count(subsystem='controls', status='blocked'), {'blocked': 0})
        self.assertEqual(self.count(status='closed', unresolved_only=True), {})

    def test_priority_and_subsystem_domains_include_zeros_and_respect_filters(self):
        self.assertEqual(self.count('priority', subsystem='controls'), {'high': 1, 'low': 0, 'medium': 0})
        self.assertEqual(
            self.count('subsystem', priority='low'),
            {'avionics': 0, 'cabin': 1, 'controls': 0, 'electrical': 0, 'hydraulics': 0, 'structures': 0},
        )
        self.assertEqual(self.count('priority', priority='medium'), {'medium': 0})
        self.assertEqual(self.count('subsystem', subsystem='avionics'), {'avionics': 0})
        self.assertEqual(self.connection.execute.call_count, 4)

    def test_aircraft_domain_includes_aircraft_without_issues(self):
        self.assertEqual(
            self.count('aircraft', subsystem='controls'),
            {'DEMO-AIR-0001': 1, 'DEMO-AIR-0002': 0, 'DEMO-AIR-0003': 0},
        )
        self.assertEqual(self.count('aircraft', aircraft_id='DEMO-AIR-0003'), {'DEMO-AIR-0003': 0})
        self.assertEqual(self.count('priority', aircraft_id='DEMO-AIR-0003'), {'high': 0, 'low': 0, 'medium': 0})

    def test_a_key_outside_the_known_values_still_counts(self):
        self.db.execute(
            "INSERT INTO preflight.issues VALUES ('DEMO-AIR-0002', 'deferred', 'controls', 'high', '2026-01-10')"
        )
        self.assertEqual(self.count(subsystem='controls')['deferred'], 1)

    def test_unknown_aircraft_filter_has_no_groups_and_is_bound(self):
        for group in ('status', 'priority', 'subsystem', 'aircraft'):
            for aircraft in ('DEMO-AIR-9999', "DEMO-AIR-9999' OR '1'='1"):
                with self.subTest(group=group, aircraft=aircraft):
                    self.connection.reset_mock()
                    self.assertEqual(self.count(group, aircraft_id=aircraft), {})
                    queries = [c.args for c in self.connection.execute.call_args_list]
                    self.assertTrue(any('FROM preflight.aircraft' in sql for sql, _ in queries))
                    for sql, values in queries:
                        self.assertNotIn(aircraft, sql)
                        self.assertIn(aircraft, values)

    def test_compose_count_oracles_include_zero_subsystems(self):
        # Execute the independent aggregate SQL without PostgreSQL's JSON wrapper.
        facts_sql = load_script('compose_check').FACTS_SQL
        for fact, alias, expected_cabin in (
            ('unresolved_by_subsystem', 'by_subsystem', 6),
            ('dated_unresolved_by_subsystem', 'dated_by_subsystem', 0),
        ):
            with self.subTest(fact=fact):
                prefix = facts_sql.split('SELECT json_build_object(', 1)[0]
                query = facts_sql.split(f"'{fact}',", 1)[1].split('\n    (', 1)[1].split(f') {alias}', 1)[0]
                rows = self.db.execute(prefix + query).fetchall()
                self.assertEqual(
                    {r['subsystem']: r['n'] for r in rows},
                    {
                        'avionics': 0,
                        'cabin': expected_cabin,
                        'controls': int(expected_cabin > 0),
                        'electrical': 0,
                        'hydraulics': 0,
                        'structures': 0,
                    },
                )

    def test_date_bounds_bind_in_one_grouped_query_and_include_both_endpoints(self):
        after = datetime(2026, 1, 1, tzinfo=UTC)
        before = datetime(2026, 1, 31, 23, 59, 59, 999999, tzinfo=UTC)
        repo, connection = self.repo, self.connection
        args: dict[str, Any] = {
            'group_by': 'status',
            'aircraft_id': 'DEMO-AIR-0001',
            'status': None,
            'subsystem': 'cabin',
            'priority': 'high',
            'unresolved_only': True,
        }
        count = repo.issue_counts(**args, opened_after=after, opened_before=before)
        self.assertEqual(
            count['groups'],
            [
                {'key': 'blocked', 'count': 1},
                {'key': 'in_progress', 'count': 0},
                {'key': 'open', 'count': 1},
                {'key': 'reopened', 'count': 0},
            ],
        )
        self.assertEqual(count['total'], 2)
        grouped = [c.args for c in connection.execute.call_args_list if 'GROUP BY' in c.args[0]]
        self.assertEqual(len(grouped), 1)
        sql, values = grouped[0]
        self.assertIn('opened_at >= %s AND opened_at <= %s', sql)
        self.assertNotIn('2026-', sql)
        self.assertEqual(values[-2:], [after, before])
        self.assertEqual(count['filters']['opened_after'], after)
        self.assertEqual(count['filters']['opened_before'], before)
        self.assertEqual(repo.issue_counts(**args, opened_after=after)['total'], 3)
        self.assertEqual(repo.issue_counts(**args, opened_before=before)['total'], 3)
        self.assertEqual(repo.issue_counts(**args)['total'], 4)
        self.assertEqual(repo.issue_counts(**args, opened_after=before, opened_before=after)['total'], 0)
