"""Allowlisted SELECTs with bound values; no caller-supplied SQL or CSV reads."""

from typing import Any

import psycopg

Row = dict[str, Any]
ISSUE_FIELDS = 'issue_id, aircraft_id, title, description, subsystem, priority, status, opened_at, closed_at'


class Repository:
    def __init__(self, connection: psycopg.Connection[Row]) -> None:
        self.connection = connection

    def healthy(self) -> bool:
        row = self.connection.execute('SELECT 1 AS ready').fetchone()
        return row is not None and row['ready'] == 1

    def aircraft_summary(self, aircraft_id: str) -> Row:
        aircraft = self.connection.execute(
            """
            SELECT aircraft_id, program, stage, station, record_created_at
            FROM preflight.aircraft WHERE aircraft_id = %s
            """,
            (aircraft_id,),
        ).fetchone()
        if aircraft is None:
            raise LookupError('Aircraft not found')
        issues = dict.fromkeys(('open', 'in_progress', 'blocked', 'reopened', 'closed'), 0)
        tasks = dict.fromkeys(('todo', 'in_progress', 'awaiting_review', 'completed', 'blocked'), 0)
        for r in self.connection.execute(
            """
            SELECT status, count(*) AS count FROM preflight.issues
            WHERE aircraft_id = %s GROUP BY status
            """,
            (aircraft_id,),
        ).fetchall():
            issues[r['status']] = r['count']
        for r in self.connection.execute(
            """
            SELECT t.status, count(*) AS count FROM preflight.tasks t
            JOIN preflight.issues i ON i.issue_id = t.issue_id
            WHERE i.aircraft_id = %s GROUP BY t.status
            """,
            (aircraft_id,),
        ).fetchall():
            tasks[r['status']] = r['count']
        return {
            'aircraft': aircraft,
            'issue_count': sum(issues.values()),
            'unresolved_issue_count': sum(issues.values()) - issues['closed'],
            'issue_status_counts': issues,
            'task_count': sum(tasks.values()),
            'task_status_counts': tasks,
        }

    def open_issues(self, aircraft_id: str | None, priority: str | None, limit: int, offset: int) -> Row:
        where = "status <> 'closed'"
        values: list[Any] = []
        if aircraft_id is not None:
            where += ' AND aircraft_id = %s'
            values.append(aircraft_id)
        if priority is not None:
            where += ' AND priority = %s'
            values.append(priority)
        counted = self.connection.execute(
            'SELECT count(*) AS count FROM preflight.issues WHERE ' + where, values
        ).fetchone()
        items = self.connection.execute(
            'SELECT '
            + ISSUE_FIELDS
            + ' FROM preflight.issues WHERE '
            + where
            + ' ORDER BY issue_id LIMIT %s OFFSET %s',
            [*values, limit, offset],
        ).fetchall()
        return {'total': counted['count'] if counted else 0, 'limit': limit, 'offset': offset, 'items': items}

    def issue_history(self, issue_id: str, limit: int, offset: int) -> Row:
        issue = self.connection.execute(
            'SELECT ' + ISSUE_FIELDS + ' FROM preflight.issues WHERE issue_id = %s', (issue_id,)
        ).fetchone()
        if issue is None:
            raise LookupError('Issue not found')
        counted = self.connection.execute(
            'SELECT count(*) AS count FROM preflight.activity_events WHERE issue_id = %s', (issue_id,)
        ).fetchone()
        events = self.connection.execute(
            """
            SELECT event_id, issue_id, task_id, actor_id, event_type, occurred_at, note
            FROM preflight.activity_events WHERE issue_id = %s
            ORDER BY occurred_at, event_id LIMIT %s OFFSET %s
            """,
            (issue_id, limit, offset),
        ).fetchall()
        return {
            'issue': issue,
            'total_events': counted['count'] if counted else 0,
            'limit': limit,
            'offset': offset,
            'events': events,
        }
