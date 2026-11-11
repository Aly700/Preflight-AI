"""Allowlisted SELECTs with bound values; no caller-supplied SQL or CSV reads."""

import json
from typing import Any, get_args

import numpy as np
from numpy.typing import NDArray
import psycopg

from mcp_service.embeddings import get_embedder
from preflight_core.scope import IssueStatus, Priority, Subsystem

Row = dict[str, Any]
ISSUE_FIELDS = 'issue_id, aircraft_id, title, description, subsystem, priority, status, opened_at, closed_at'
# Grouping and filter columns are chosen from these fixed names; input values only ever bind as parameters.
GROUP_COLUMNS = {'subsystem': 'subsystem', 'priority': 'priority', 'status': 'status', 'aircraft': 'aircraft_id'}
FILTER_COLUMNS = {'aircraft_id': 'aircraft_id', 'status': 'status', 'subsystem': 'subsystem', 'priority': 'priority'}
PASSAGE_FIELDS = (
    'p.passage_id, p.doc_id, d.title, d.kind, d.doc_date AS date, d.station, d.program, d.related_ids,'
    ' d.author_id, d.team_id, p.text'
)


def fuse_passages(vector: list[Row], keyword: list[Row], top_k: int) -> list[Row]:
    """RRF with one-based ranks and k=60; equal scores sort by passage ID."""
    fused: dict[str, Row] = {}
    for rows, rank_field in ((vector, 'vector_rank'), (keyword, 'keyword_rank')):
        for rank, row in enumerate(rows, 1):
            passage = fused.setdefault(
                row['passage_id'], {**row, 'score': 0.0, 'vector_rank': None, 'keyword_rank': None}
            )
            passage[rank_field] = rank
            passage['score'] += 1 / (60 + rank)
    return sorted(fused.values(), key=lambda row: (-row['score'], row['passage_id']))[:top_k]


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


def document_filters(kind: str | None, related_id: str | None, limit: int) -> tuple[str, list[Any]]:
    # The literal integer limit preserves the existing SQL and index plan shape.
    if type(limit) is not int or limit < 1:
        raise ValueError('Candidate limit must be a positive integer')
    clauses: list[str] = []
    values: list[Any] = []
    if kind is not None:
        clauses.append('d.kind = %s')
        values.append(kind)
    if related_id is not None:
        clauses.append('d.related_ids @> ARRAY[%s]')
        values.append(related_id)
    return (' WHERE ' + ' AND '.join(clauses)) if clauses else '', values


class Repository:
    def __init__(self, connection: psycopg.Connection[Row]) -> None:
        self.connection = connection

    def healthy(self) -> bool:
        row = self.connection.execute('SELECT 1 AS ready').fetchone()
        return row is not None and row['ready'] == 1

    def vector_candidates(
        self, query_vector: NDArray[np.float32], kind: str | None, related_id: str | None, limit: int = 20
    ) -> list[Row]:
        vector = json.dumps(query_vector.tolist())
        where, values = document_filters(kind, related_id, limit)
        # Keep cosine distance as the direct ascending ORDER BY expression so the
        # planner can use passages_embedding_idx (HNSW vector_cosine_ops).
        return self.connection.execute(
            'SELECT '
            + PASSAGE_FIELDS
            + ' FROM preflight.passages p JOIN preflight.documents d ON d.doc_id = p.doc_id'
            + where
            + f' ORDER BY p.embedding <=> %s::vector LIMIT {limit}',
            [*values, vector],
        ).fetchall()

    def keyword_candidates(self, query: str, kind: str | None, related_id: str | None, limit: int = 20) -> list[Row]:
        where, values = document_filters(kind, related_id, limit)
        # plainto_tsquery removes English stop words and escapes lexemes; OR keeps
        # any meaningful term rather than requiring every term of a question.
        return self.connection.execute(
            "WITH question AS (SELECT replace(plainto_tsquery('english', %s)::text, ' & ', ' | ')::tsquery AS terms) "
            'SELECT '
            + PASSAGE_FIELDS
            + ' FROM preflight.passages p JOIN preflight.documents d ON d.doc_id = p.doc_id CROSS JOIN question'
            + (where + ' AND ' if where else ' WHERE ')
            + 'p.text_search @@ question.terms'
            + f' ORDER BY ts_rank_cd(p.text_search, question.terms) DESC, p.passage_id LIMIT {limit}',
            [query, *values],
        ).fetchall()

    def search_documents(self, query: str, top_k: int, kind: str | None, related_id: str | None) -> Row:
        # The MCP handler runs the entire repository call in its bounded worker pool,
        # including model loading and inference. No query or passage text is logged.
        vector_rows = self.vector_candidates(get_embedder().embed_query(query), kind, related_id)
        keyword_rows = self.keyword_candidates(query, kind, related_id)
        return {'results': fuse_passages(vector_rows, keyword_rows, top_k)}

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
        opened_after: Any = None,
        opened_before: Any = None,
    ) -> Row:
        column = GROUP_COLUMNS[group_by]
        filters = {
            'aircraft_id': aircraft_id,
            'status': status,
            'subsystem': subsystem,
            'priority': priority,
            'opened_after': opened_after,
            'opened_before': opened_before,
        }
        where, values = issue_filters(filters, unresolved_only=unresolved_only)
        groups = self.connection.execute(
            'SELECT '
            + column
            + ' AS key, count(*) AS count FROM preflight.issues'
            + where
            + ' GROUP BY key ORDER BY key',
            values,
        ).fetchall()
        domain = list(get_args({'status': IssueStatus, 'priority': Priority, 'subsystem': Subsystem}.get(group_by)))
        if filters[column] is not None:
            domain = [filters[column]]
        if group_by == 'status' and unresolved_only:
            domain = [key for key in domain if key != 'closed']
        if group_by == 'aircraft' or aircraft_id is not None:
            aircraft = self.connection.execute(
                'SELECT aircraft_id FROM preflight.aircraft'
                + (' WHERE aircraft_id = %s' if aircraft_id is not None else '')
                + ' ORDER BY aircraft_id',
                [aircraft_id] if aircraft_id is not None else [],
            ).fetchall()
            if group_by == 'aircraft' or not aircraft:
                domain = [row['aircraft_id'] for row in aircraft]
        counts = {g['key']: g['count'] for g in groups}
        # A key the database returned always stays, so the total never silently drops a group.
        filled = [{'key': key, 'count': counts.get(key, 0)} for key in sorted(set(domain) | counts.keys())]
        return {
            'group_by': group_by,
            'unresolved_only': unresolved_only,
            'filters': filters,
            'total': sum(g['count'] for g in filled),
            'groups': filled,
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
        unresolved_only: bool = False,
    ) -> Row:
        where, values = issue_filters(
            {
                'status': status,
                'subsystem': subsystem,
                'priority': priority,
                'aircraft_id': aircraft_id,
                'opened_after': opened_after,
                'opened_before': opened_before,
            },
            unresolved_only=unresolved_only,
        )
        counted = self.connection.execute('SELECT count(*) AS count FROM preflight.issues' + where, values).fetchone()
        items = self.connection.execute(
            'SELECT ' + ISSUE_FIELDS + ' FROM preflight.issues' + where + ' ORDER BY issue_id LIMIT %s OFFSET %s',
            [*values, limit, offset],
        ).fetchall()
        return {'total': counted['count'] if counted else 0, 'limit': limit, 'offset': offset, 'items': items}

    def list_tasks(
        self,
        status: str | None = None,
        task_kind: str | None = None,
        assigned_team_id: str | None = None,
        assigned_person_id: str | None = None,
        aircraft_id: str | None = None,
        issue_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Row:
        clauses: list[str] = []
        values: list[Any] = []
        for column, value in (
            ('t.status', status),
            ('t.task_kind', task_kind),
            ('t.assigned_team_id', assigned_team_id),
            ('t.assigned_person_id', assigned_person_id),
            ('i.aircraft_id', aircraft_id),
            ('t.issue_id', issue_id),
        ):
            if value is not None and value.strip():
                clauses.append(column + ' = %s')
                values.append(value)
        source = ' FROM preflight.tasks t JOIN preflight.issues i ON i.issue_id = t.issue_id'
        if clauses:
            source += ' WHERE ' + ' AND '.join(clauses)
        counted = self.connection.execute('SELECT count(*) AS count' + source, values).fetchone()
        items = self.connection.execute(
            'SELECT t.task_id, t.issue_id, i.aircraft_id, t.task_kind, t.status, t.assigned_team_id, '
            't.assigned_person_id, t.created_at, t.completed_at' + source + ' ORDER BY t.task_id LIMIT %s OFFSET %s',
            [*values, limit, offset],
        ).fetchall()
        return {'total': counted['count'] if counted else 0, 'limit': limit, 'offset': offset, 'items': items}
