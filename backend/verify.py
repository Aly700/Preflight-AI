"""The claims contract: the `final_answer` tool the model calls last, and the check of what it declares."""

from dataclasses import dataclass, field
import re
from typing import Any

from preflight_core.models import RECORD_ID

FINAL_ANSWER_TOOL: dict[str, Any] = {
    'name': 'final_answer',
    'description': (
        'Declare the claims in the answer you have already written as text: every record ID you cited '
        'and every number you stated, each number with the id of the tool call and the JSON path (dotted '
        'keys, [n] for list items) it came from. This tool carries no answer text. Call it exactly once, '
        'after the answer text, as the last thing you do. Claims that cannot be verified are shown to the '
        'reader as unverified.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'cited_ids': {
                'type': 'array',
                'description': 'Every record ID that appears in the answer text',
                'items': {'type': 'string'},
            },
            'numbers': {
                'type': 'array',
                'description': 'Every count or quantity stated in the answer text',
                'items': {
                    'type': 'object',
                    'properties': {
                        'label': {'type': 'string', 'description': 'What the number is, in a few words'},
                        'value': {'type': 'number'},
                        'call_id': {'type': 'string', 'description': 'The id of the tool call the number came from'},
                        'path': {
                            'type': 'string',
                            'description': "JSON path inside that result, e.g. 'total' or 'items[0].issue_id'",
                        },
                    },
                    'required': ['label', 'value', 'call_id', 'path'],
                },
            },
        },
        'required': ['cited_ids', 'numbers'],
    },
}


def record_ids(payload: Any) -> list[str]:
    """Collect fictional record IDs from `*_id` fields only, never from free text."""
    found: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key.endswith('_id') and isinstance(item, str) and RECORD_ID.fullmatch(item):
                    found.add(item)
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(payload)
    return sorted(found)


@dataclass
class Verification:
    verified: bool
    unverified: list[str] = field(default_factory=list)
    undeclared: int = 0
    note: str = ''


ID_PATTERN = re.compile(r'\b(?:DEMO-AIR|ISSUE|TASK|EVT|TEAM|PERSON|SHIFT)-[0-9]+\b')
DATE = re.compile(r'\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?')
TIME = re.compile(r'\b\d{1,2}:\d{2}(?::\d{2})?\b')
INTEGER = re.compile(r'(?<![\w.-])\d{1,9}(?![\w.-])')
PATH_PART = re.compile(r'\[(\d+)\]|([^.\[\]]+)')


def resolve_path(payload: Any, path: str) -> Any:
    """Follow dotted keys and [n] indexes into a tool result; LookupError when the path is absent."""
    value = payload
    try:
        for index, key in PATH_PART.findall(path):
            value = value[int(index)] if index else value[key]
    except KeyError, IndexError, TypeError:
        raise LookupError(path) from None
    return value


def same_number(declared: float, actual: Any) -> bool:
    if isinstance(actual, bool) or not isinstance(actual, (int, float)):
        return False
    if isinstance(declared, int) and isinstance(actual, int):
        return declared == actual
    return abs(float(declared) - float(actual)) <= 1e-9


def number_token(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


PATH_CHARS = 80


def candidates(reference: str, results: dict[str, dict[str, Any]], names: dict[str, str]) -> list[dict[str, Any]] | str:
    """The result a declared number refers to: the exact call id, or the tool's name when it was called once.

    Returns a reason string instead when the reference is ambiguous.
    """
    if reference in results:
        return [results[reference]]
    matches = [results[call_id] for call_id, name in names.items() if name == reference and call_id in results]
    if len(matches) > 1:
        return 'names a tool that was called more than once; use the call id'
    return matches


def short(path: str) -> str:
    return path if len(path) <= PATH_CHARS else path[:PATH_CHARS] + '…'


def verify_claims(
    *,
    prose: str,
    claims: dict[str, Any] | None,
    results: dict[str, dict[str, Any]],
    seen_ids: set[str],
    names: dict[str, str] | None = None,
) -> Verification:
    """Check declared numbers against the results they name and prose IDs against what tools returned.

    Declared mismatches are reported to the reader. Undeclared bare integers are
    only counted (dates, times and years excluded), for evaluation. A number may
    name its tool call by id or, failing that, by tool name.
    """
    names = names or {}
    unverified: list[str] = []
    declared_tokens: set[str] = set()
    numbers = list(claims.get('numbers') or []) if claims else []
    raw_cited = claims.get('cited_ids') if claims else None
    cited: set[str] = set()
    if raw_cited is None:
        pass
    elif isinstance(raw_cited, list):
        cited = {str(c) for c in raw_cited}
    else:
        unverified.append('cited_ids is malformed')
    for item in numbers:
        if not isinstance(item, dict):
            unverified.append('a declared number is malformed')
            continue
        label = str(item.get('label') or 'number')
        value = item.get('value')
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            unverified.append(f'{label}: the declared value is not a number')
            continue
        declared_tokens.add(number_token(value))
        options = candidates(str(item.get('call_id', '')), results, names)
        if isinstance(options, str):
            unverified.append(f'{label} ({number_token(value)}) {options}')
            continue
        if not options:
            unverified.append(f'{label} ({number_token(value)}) refers to a tool call that returned nothing')
            continue
        path = short(str(item.get('path', '')))
        actuals: list[Any] = []
        for payload in options:
            try:
                actuals.append(resolve_path(payload, str(item.get('path', ''))))
            except LookupError:
                continue
        if not actuals:
            unverified.append(f'{label} ({number_token(value)}) has no value at {path!r} in that result')
            continue
        numeric = [a for a in actuals if isinstance(a, (int, float)) and not isinstance(a, bool)]
        if not numeric:
            # Record text is data; it never reaches the note.
            unverified.append(f'{label} ({number_token(value)}): the value at {path!r} is not a number')
            continue
        if not any(same_number(value, actual) for actual in numeric):
            unverified.append(f'{label} ({number_token(value)}) does not match the tool result ({numeric[0]!r})')
    prose_ids = set(ID_PATTERN.findall(prose))
    for record in sorted(prose_ids - cited):
        unverified.append(f'{record} is not among the cited records')
    for record in sorted(cited - seen_ids):
        unverified.append(f'{record} was cited but no tool returned it')
    stripped = TIME.sub(' ', DATE.sub(' ', ID_PATTERN.sub(' ', prose)))
    undeclared = 0
    for match in INTEGER.finditer(stripped):
        token = match.group()
        if len(token) == 4 and 1900 <= int(token) <= 2100:
            continue
        if token not in declared_tokens:
            undeclared += 1
    if claims is None and (prose_ids or undeclared):
        unverified.append('no claims were declared for the records or numbers in this answer')
    note = ('Unverified: ' + '; '.join(unverified)) if unverified else ''
    return Verification(verified=not unverified, unverified=unverified, undeclared=undeclared, note=note)
