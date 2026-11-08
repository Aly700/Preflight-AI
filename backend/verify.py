"""The claims contract: the `final_answer` tool the model calls last, and the check of what it declares."""

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import re
from typing import Any

from preflight_core.models import RECORD_ID

FINAL_ANSWER_TOOL: dict[str, Any] = {
    'name': 'final_answer',
    'description': (
        'Declare the claims in the answer you have already written as text: every record ID you cited '
        'and every number you stated, each number with either call_id and path for the id of the tool call '
        'and the JSON path (dotted keys, [n] for list items) it came from, or derived for a computed number. '
        'This tool carries no answer text. Call it exactly once, '
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
                    'description': (
                        'Give either call_id and path for a number read from a result, or derived to compute it.'
                    ),
                    'properties': {
                        'label': {'type': 'string', 'description': 'What the number is, in a few words'},
                        'value': {'type': 'number'},
                        'call_id': {'type': 'string', 'description': 'The id of the tool call the number came from'},
                        'path': {
                            'type': 'string',
                            'description': "JSON path inside that result, e.g. 'total' or 'items[0].issue_id'",
                        },
                        'derived': {
                            'type': 'object',
                            'description': 'Compute a sum, difference or count from tool results.',
                            'properties': {
                                'op': {'type': 'string', 'enum': ['sum', 'difference', 'count']},
                                'operands': {
                                    'type': 'array',
                                    'description': (
                                        'Sum uses 2 to 12 numbers; difference uses 2 numbers; count uses 1 list.'
                                    ),
                                    'items': {
                                        'type': 'object',
                                        'properties': {'call_id': {'type': 'string'}, 'path': {'type': 'string'}},
                                        'required': ['call_id', 'path'],
                                    },
                                },
                                'where': {
                                    'type': 'object',
                                    'description': (
                                        'For count only: include objects with this field value; ignore case.'
                                    ),
                                    'properties': {
                                        'field': {'type': 'string'},
                                        'equals': {'type': ['string', 'number', 'boolean']},
                                    },
                                    'required': ['field', 'equals'],
                                },
                                'distinct': {
                                    'type': 'string',
                                    'description': (
                                        'For count only: count distinct non-null values of this field after where; '
                                        'dotted fields are allowed and strings ignore case.'
                                    ),
                                },
                            },
                            'required': ['op', 'operands'],
                        },
                    },
                    'required': ['label', 'value'],
                },
            },
        },
        'required': ['cited_ids', 'numbers'],
    },
}


def record_ids(payload: Any) -> list[str]:
    """Collect fictional record IDs from `*_id` and `key` fields, never from free text."""
    found: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if (key.endswith('_id') or key == 'key') and isinstance(item, str) and RECORD_ID.fullmatch(item):
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
THOUSANDS = re.compile(r'\b\d{1,3}(?:,\d{3})+\b')
INTEGER = re.compile(r'(?<![\w.-])\d{1,9}(?![\w.-])')
PATH_PART = re.compile(r'\[([^\[\]]+)\]|([^.\[\]]+)')
SELECTOR = re.compile(r"""\s*\??\s*([^\s=.\[\]]+(?:\.[^\s=.\[\]]+)*)\s*==?\s*(?:'([^']*)'|"([^"]*)"|([^'"]+?))\s*""")


def scan_prose(prose: str) -> tuple[set[str], list[str]]:
    """Return record IDs and bare integer tokens, excluding dates, times and years."""
    prose_ids = set(ID_PATTERN.findall(prose))
    stripped = TIME.sub(' ', DATE.sub(' ', ID_PATTERN.sub(' ', prose)))
    stripped = THOUSANDS.sub(lambda match: match.group().replace(',', ''), stripped)
    integers = [match.group() for match in INTEGER.finditer(stripped)]
    return prose_ids, [token for token in integers if not (len(token) == 4 and 1900 <= int(token) <= 2100)]


def field_value(value: Any, path: str) -> Any:
    """Read a dotted object field, returning None when it is absent."""
    for key in path.split('.'):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def selector_matches(value: Any, expected: str) -> bool:
    """Match a selector literal without confusing booleans with numbers."""
    folded = expected.casefold()
    if folded in ('true', 'false'):
        return isinstance(value, bool) and value == (folded == 'true')
    if isinstance(value, str):
        return value.casefold() == folded
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return Decimal(str(value)) == Decimal(expected)
    except InvalidOperation:
        return False


def resolve_path(payload: Any, path: str) -> Any:
    """Follow keys, indexes and unique [field=value] selectors, with a final list .length."""
    value = payload
    length_suffix = path.endswith('.length')
    try:
        for segment, key in PATH_PART.findall(path.removesuffix('.length') if length_suffix else path):
            if not segment:
                value = value[key]
            elif segment.isdecimal():
                value = value[int(segment)]
            else:
                selector = SELECTOR.fullmatch(segment)
                if not isinstance(value, list) or selector is None:
                    raise LookupError(path)
                field_path = selector.group(1)
                expected = next(part for part in selector.groups()[1:] if part is not None)
                matches = [
                    item
                    for item in value
                    if isinstance(item, dict) and selector_matches(field_value(item, field_path), expected)
                ]
                if len(matches) != 1:
                    raise LookupError(path)
                value = matches[0]
    except KeyError, IndexError, TypeError:
        raise LookupError(path) from None
    if length_suffix:
        if not isinstance(value, list):
            raise LookupError(path)
        return len(value)
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


def field_matches(value: Any, expected: Any) -> bool:
    """Compare a count filter without treating booleans as numbers."""
    if isinstance(value, str) and isinstance(expected, str):
        return value.casefold() == expected.casefold()
    if isinstance(value, bool) or isinstance(expected, bool):
        return value is expected
    return bool(value == expected)


def derived_value(derived: Any, results: dict[str, dict[str, Any]], names: dict[str, str]) -> float | str:
    """Compute a declaration, or return a short reason that never includes record text."""
    if not isinstance(derived, dict):
        return 'the derivation is malformed'
    op = derived.get('op')
    if op not in ('sum', 'difference', 'count'):
        return 'the derivation has an unknown operation'
    operands = derived.get('operands')
    if not isinstance(operands, list):
        return 'the derivation operands must be a list'
    if len(operands) > 12:
        return 'the derivation has more than 12 operands'
    if op == 'sum' and len(operands) < 2:
        return 'sum requires 2 to 12 operands'
    if op == 'difference' and len(operands) != 2:
        return 'difference requires exactly 2 operands'
    if op == 'count' and len(operands) != 1:
        return 'count requires exactly 1 operand'
    where = derived.get('where')
    if 'where' in derived:
        if op != 'count':
            return 'where is only allowed for count'
        if (
            not isinstance(where, dict)
            or not isinstance(where.get('field'), str)
            or not isinstance(where.get('equals'), (str, int, float, bool))
        ):
            return 'the count filter is malformed'
    distinct = derived.get('distinct')
    if 'distinct' in derived:
        if op != 'count':
            return 'distinct is only allowed for count'
        if not isinstance(distinct, str) or not distinct.strip():
            return 'distinct must be a non-empty string'
    numeric: list[float] = []
    for operand in operands:
        if not isinstance(operand, dict):
            return 'a derivation operand is malformed'
        call_id, path = operand.get('call_id'), operand.get('path')
        if not isinstance(call_id, str) or not isinstance(path, str):
            return 'a derivation operand needs call_id and path'
        options = candidates(call_id, results, names)
        if isinstance(options, str):
            return options
        if not options:
            return 'refers to a tool call that returned nothing'
        try:
            actual = resolve_path(options[0], path)
        except LookupError:
            return f'has no value at {short(path)!r} in that result'
        if op == 'count':
            if not isinstance(actual, list):
                return f'the value at {short(path)!r} is not a list'
            items = (
                actual
                if where is None
                else [
                    item
                    for item in actual
                    if isinstance(item, dict) and field_matches(item.get(where['field']), where['equals'])
                ]
            )
            if distinct is None:
                return len(items)
            unique: list[Any] = []
            for item in items:
                value = field_value(item, distinct)
                if value is not None and not any(field_matches(value, seen) for seen in unique):
                    unique.append(value)
            return len(unique)
        if isinstance(actual, bool) or not isinstance(actual, (int, float)):
            return f'the value at {short(path)!r} is not a number'
        numeric.append(actual)
    return sum(numeric) if op == 'sum' else numeric[0] - numeric[1]


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
    name its tool call by id or, failing that, by tool name, or declare a
    derivation whose operands reference tool results in the same way.
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
        cited = {c for c in raw_cited if isinstance(c, str) and RECORD_ID.fullmatch(c)}
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
        # An unused optional field filled with null or a blank string is absent, not a second form.
        has_path = isinstance(item.get('path'), str) and bool(item['path'].strip())
        has_derived = item.get('derived') is not None
        if not has_path and not has_derived:
            unverified.append(f'{label} ({number_token(value)}) declares neither a path nor a derivation')
            continue
        if has_path and has_derived:
            unverified.append(f'{label} ({number_token(value)}) declares both a path and a derivation')
            continue
        if has_derived:
            computed = derived_value(item['derived'], results, names)
            if isinstance(computed, str):
                unverified.append(f'{label} ({number_token(value)}): {computed}')
            elif not (
                same_number(value, computed)
                or (item['derived']['op'] == 'difference' and same_number(value, abs(computed)))
            ):
                unverified.append(
                    f'{label} ({number_token(value)}) does not match the derived value ({number_token(computed)})'
                )
            continue
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
    prose_ids, integers = scan_prose(prose)
    for record in sorted(prose_ids - cited):
        unverified.append(f'{record} is not among the cited records')
    for record in sorted(cited - seen_ids):
        unverified.append(f'{record} was cited but no tool returned it')
    undeclared = sum(token not in declared_tokens for token in integers)
    if claims is None and (prose_ids or undeclared):
        unverified.append('no claims were declared for the records or numbers in this answer')
    note = ('Unverified: ' + '; '.join(unverified)) if unverified else ''
    return Verification(verified=not unverified, unverified=unverified, undeclared=undeclared, note=note)
