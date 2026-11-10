"""Pure request-scope projection onto discovered tool arguments; no tool or model calls."""

from collections.abc import Mapping
from datetime import UTC, date, datetime, time
from typing import Any

ISSUE_TOOLS = frozenset({'list_open_issues', 'search_issues', 'count_issues'})


class ScopeConflict(ValueError):
    def __init__(self, field: str) -> None:
        super().__init__(f'Outside the selected scope: {field}')


def date_boundary(value: str, field: str) -> str:
    """Convert an inclusive calendar date to the issue search's timezone-aware timestamp."""
    boundary = time.min if field == 'opened_after' else time.max
    return datetime.combine(date.fromisoformat(value), boundary, tzinfo=UTC).isoformat()


def apply_scope(
    tool_name: str,
    arguments: Mapping[str, Any],
    scope: Mapping[str, str],
    input_schema: Mapping[str, Any],
) -> dict[str, Any]:
    """Fill omitted scoped arguments or refuse a conflicting value without mutating inputs.

    Enforcement is limited to arguments a tool actually supports. `entity` is context,
    not a tool allowlist. For issue status, unresolved includes any non-closed status.
    """
    result = dict(arguments)
    properties = input_schema.get('properties', {})
    mapped: dict[str, Any] = {}
    for field in ('aircraft_id', 'subsystem', 'priority', 'opened_after', 'opened_before'):
        if field in scope and field in properties:
            mapped[field] = date_boundary(scope[field], field) if field.startswith('opened_') else scope[field]
    if tool_name in ISSUE_TOOLS and 'issue_status' in scope:
        status = scope['issue_status']
        if status == 'unresolved':
            if 'status' in properties and result.get('status') == 'closed':
                raise ScopeConflict('status')
            if 'unresolved_only' in properties:
                mapped['unresolved_only'] = True
            elif tool_name != 'list_open_issues' and 'status' in properties:
                # A stale discovery schema must not silently turn unresolved into all statuses.
                raise ScopeConflict('status')
        elif 'status' in properties:
            mapped['status'] = status
            if status == 'closed' and result.get('unresolved_only') is True:
                raise ScopeConflict('unresolved_only')
    if tool_name == 'list_tasks':
        for source, target in (('task_status', 'status'), ('task_kind', 'task_kind'), ('team_id', 'assigned_team_id')):
            if source in scope and target in properties:
                mapped[target] = scope[source]
    if tool_name == 'search_documents':
        for source, target in (('document_kind', 'kind'), ('related_id', 'related_id')):
            if source in scope and target in properties:
                mapped[target] = scope[source]
        if (
            'related_id' not in scope
            and scope.get('entity') == 'documents'
            and 'aircraft_id' in scope
            and 'related_id' in properties
        ):
            mapped['related_id'] = scope['aircraft_id']
    if tool_name == 'compare_aircraft' and 'aircraft_id' in scope:
        aircraft = result.get('aircraft_ids', [])
        if not isinstance(aircraft, list) or any(value != scope['aircraft_id'] for value in aircraft):
            raise ScopeConflict('aircraft_ids')
    for field, expected in mapped.items():
        if field in result:
            actual = result[field]
            if field in ('opened_after', 'opened_before') and isinstance(actual, str):
                # Accept equivalent ISO timestamps (including Z) and the selected calendar date.
                if actual == scope[field]:
                    result[field] = expected
                    continue
                try:
                    if datetime.fromisoformat(actual) == datetime.fromisoformat(expected):
                        continue
                except ValueError:
                    pass
            if type(actual) is not type(expected) or actual != expected:
                raise ScopeConflict(field)
        else:
            result[field] = expected
    return result
