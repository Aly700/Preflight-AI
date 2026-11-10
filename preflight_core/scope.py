"""Shared, bounded filter vocabulary and strict request scope validation (standard library only).

Regenerate the UI options with `python -B -m preflight_core.scope` from the repository root.
Dates select inclusive UTC calendar days; null/blank values must be omitted from request scope.
"""

from datetime import date
import json
from pathlib import Path
import re
from typing import Any, Literal, get_args

Priority = Literal['low', 'medium', 'high']
Subsystem = Literal['hydraulics', 'avionics', 'electrical', 'cabin', 'structures', 'controls']
IssueStatus = Literal['open', 'in_progress', 'blocked', 'reopened', 'closed']
TaskStatus = Literal['todo', 'in_progress', 'awaiting_review', 'completed', 'blocked']
TaskKind = Literal['review', 'investigation', 'documentation', 'inspection', 'rectification']
DocumentKind = Literal['policy', 'glossary', 'handover', 'memo', 'coordination', 'retrospective', 'form']
RECORD_ID = re.compile(r'^(?:(?:DEMO-AIR|ISSUE|TASK|EVT|TEAM|PERSON|SHIFT)-[0-9]+|DOC-[0-9]{4}(?:#[0-9]+)?)$')

SCOPE_OPTIONS: dict[str, dict[str, Any]] = {
    'entity': {'enum': ['issues', 'tasks', 'documents']},
    'aircraft_id': {'pattern': r'^DEMO-AIR-[0-9]{4}$', 'maxLength': 13},
    'subsystem': {'enum': list(get_args(Subsystem))},
    'priority': {'enum': list(get_args(Priority))},
    'issue_status': {'enum': [*get_args(IssueStatus), 'unresolved']},
    'opened_after': {'pattern': r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$', 'format': 'date', 'maxLength': 10},
    'opened_before': {'pattern': r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$', 'format': 'date', 'maxLength': 10},
    'task_status': {'enum': list(get_args(TaskStatus))},
    'task_kind': {'enum': list(get_args(TaskKind))},
    'team_id': {'pattern': r'^TEAM-[0-9]{4}$', 'maxLength': 9},
    'document_kind': {'enum': list(get_args(DocumentKind))},
    'related_id': {'pattern': RECORD_ID.pattern, 'maxLength': 64},
}


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """JSON object hook: a later value must never silently replace a selected filter."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate object field')
        result[key] = value
    return result


def validate_scope(value: Any) -> dict[str, str]:
    """Return a fresh validated object. Only absent fields represent no selection."""
    if not isinstance(value, dict) or set(value) - SCOPE_OPTIONS.keys():
        raise ValueError('Scope must be an object with known filter fields')
    validated: dict[str, str] = {}
    for key, item in value.items():
        spec = SCOPE_OPTIONS[key]
        if (
            not isinstance(item, str)
            or ('enum' in spec and item not in spec['enum'])
            or ('maxLength' in spec and len(item) > spec['maxLength'])
            or ('pattern' in spec and re.fullmatch(spec['pattern'], item) is None)
        ):
            raise ValueError(f'Invalid scope field: {key}')
        if spec.get('format') == 'date':
            try:
                date.fromisoformat(item)
            except ValueError:
                raise ValueError(f'Invalid scope field: {key}') from None
        validated[key] = item
    if (
        'opened_after' in validated
        and 'opened_before' in validated
        and validated['opened_after'] > validated['opened_before']
    ):
        raise ValueError('Scope opened_after must be on or before opened_before')
    return validated


def scope_note(scope: dict[str, str]) -> str:
    """A structured context line; never rewrite the user's question."""
    return 'Selected scope: ' + json.dumps(scope, sort_keys=True, ensure_ascii=True)


if __name__ == '__main__':
    destination = Path(__file__).resolve().parents[1] / 'frontend/src/scope-options.json'
    destination.write_text(json.dumps(SCOPE_OPTIONS, indent=2) + '\n', encoding='utf-8')
