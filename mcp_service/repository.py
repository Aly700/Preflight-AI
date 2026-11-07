"""Allowlisted SELECTs with bound values; no caller-supplied SQL or CSV reads."""

from typing import Any

import psycopg

Row = dict[str, Any]
ISSUE_FIELDS = 'issue_id, aircraft_id, title, description, subsystem, priority, status, opened_at, closed_at'
# Grouping and filter columns are chosen from these fixed names; input values only ever bind as parameters.
GROUP_COLUMNS = {'subsystem': 'subsystem', 'priority': 'priority', 'status': 'status', 'aircraft': 'aircraft_id'}
FILTER_COLUMNS = {'aircraft_id': 'aircraft_id', 'status': 'status', 'subsystem': 'subsystem', 'priority': 'priority'}


def issue_filters(values: dict[str, Any], *, unresolved_only: bool = False) -> tuple[str, list[Any]]:
    """A WHERE clause from the present filters, columns fixed, values bound."""
    clauses: list[str] = []
    bound: list[Any] = []
    for name, column in FILTER_COLUMNS.items():
        if values.get(name) is not None:
            clauses.append(column + ' = %s')
            bound.append(values[name])
    if values.get('opened_after') is not None:
        clauses.append('opened_at >= %s')
        bound.append(values['opened_after'])
    if values.get('opened_before') is not None:
        clauses.append('opened_at <= %s')
        bound.append(values['opened_before'])
    if unresolved_only:
        clauses.append("status <> 'closed'")
    return (' WHERE ' + ' AND '.join(clauses)) if clauses else '', bound


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

    def issue_counts(
        self,
        group_by: str,
        aircraft_id: str | None,
        status: str | None,
        subsystem: str | None,
        priority: str | None,
        unresolved_only: bool,
    ) -> Row:
        column = GROUP_COLUMNS[group_by]
        filters = {'aircraft_id': aircraft_id, 'status': status, 'subsystem': subsystem, 'priority': priority}
        where, values = issue_filters(filters, unresolved_only=unresolved_only)
        groups = self.connection.execute(
            'SELECT '
            + column
            + ' AS key, count(*) AS count FROM preflight.issues'
            + where
            + ' GROUP BY key ORDER BY key',
            values,
        ).fetchall()
        return {
            'group_by': group_by,
            'unresolved_only': unresolved_only,
            'filters': filters,
            'total': sum(g['count'] for g in groups),
            'groups': [{'key': g['key'], 'count': g['count']} for g in groups],
        }

    def compare_aircraft(self, aircraft_ids: list[str]) -> Row:
        summaries = [self.aircraft_summary(aircraft_id) for aircraft_id in dict.fromkeys(aircraft_ids)]
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

    def search_issues(
        self,
        status: str | None,
        subsystem: str | None,
        priority: str | None,
        aircraft_id: str | None,
        opened_after: Any,
        opened_before: Any,
        limit: int,
        offset: int,
    ) -> Row:
        where, values = issue_filters(
            {
                'status': status,
                'subsystem': subsystem,
                'priority': priority,
                'aircraft_id': aircraft_id,
                'opened_after': opened_after,
                'opened_before': opened_before,
            }
        )
        counted = self.connection.execute('SELECT count(*) AS count FROM preflight.issues' + where, values).fetchone()
        items = self.connection.execute(
            'SELECT ' + ISSUE_FIELDS + ' FROM preflight.issues' + where + ' ORDER BY issue_id LIMIT %s OFFSET %s',
            [*values, limit, offset],
        ).fetchall()
        return {'total': counted['count'] if counted else 0, 'limit': limit, 'offset': offset, 'items': items}
