"""Execute count SQL offline, including date endpoints and combined filters."""

from datetime import UTC, datetime
import sqlite3
from typing import Any
import unittest
from unittest.mock import Mock

from mcp_service.repository import Repository


class CountSQL(unittest.TestCase):
    def test_date_bounds_bind_in_one_grouped_query_and_include_both_endpoints(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.execute("ATTACH DATABASE ':memory:' AS preflight")
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
        after = datetime(2026, 1, 1, tzinfo=UTC)
        before = datetime(2026, 1, 31, 23, 59, 59, 999999, tzinfo=UTC)
        repo = Repository(connection)
        args: dict[str, Any] = {
            'group_by': 'status',
            'aircraft_id': 'DEMO-AIR-0001',
            'status': None,
            'subsystem': 'cabin',
            'priority': 'high',
            'unresolved_only': True,
        }
        count = repo.issue_counts(**args, opened_after=after, opened_before=before)
        self.assertEqual(count['groups'], [{'key': 'blocked', 'count': 1}, {'key': 'open', 'count': 1}])
        self.assertEqual(count['total'], 2)
        connection.execute.assert_called_once()
        sql, values = connection.execute.call_args.args
        self.assertIn('opened_at >= %s AND opened_at <= %s', sql)
        self.assertNotIn('2026-', sql)
        self.assertEqual(values[-2:], [after, before])
        self.assertEqual(count['filters']['opened_after'], after)
        self.assertEqual(count['filters']['opened_before'], before)
        self.assertEqual(repo.issue_counts(**args, opened_after=after)['total'], 3)
        self.assertEqual(repo.issue_counts(**args, opened_before=before)['total'], 3)
        self.assertEqual(repo.issue_counts(**args)['total'], 4)
        self.assertEqual(repo.issue_counts(**args, opened_after=before, opened_before=after)['total'], 0)
