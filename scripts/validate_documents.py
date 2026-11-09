"""Validate fictional process paperwork without printing any supplied document text."""

import argparse
from collections import Counter, defaultdict
from datetime import date
import json
from pathlib import Path
import re
from typing import Any
import unicodedata

from document_spec import COUNTS, ROOT, VERSION, Records, history_bounds, read_records, word_range

# Apply each expression case-insensitively to NFKC-normalized full document text.
# Keep this list and data/documents/README.md in sync. Diagnostics contain category
# names only, never the match, metadata values, source lines, or exception text.
BANNED_PATTERNS = (
    ('maintenance', r'\btorqu\w*\b'),
    ('maintenance', r'\brivet\w*\b'),
    ('maintenance', r'\binspect\s+the\b'),
    ('maintenance', r'\breplace\s+the\b'),
    ('maintenance', r'\bremove\s+the\b'),
    ('maintenance', r'\btighten\w*\b'),
    ('maintenance', r'\blubricat\w*\b'),
    ('maintenance', r'\bborescop\w*\b'),
    ('maintenance', r'\bNDT\b'),
    ('maintenance', r'\bpart\s+numbers?\b'),
    ('maintenance', r'\bP\s*/\s*N\b'),
    ('maintenance', r'\bservice\s+bulletins?\b'),
    ('maintenance', r'\bairworthiness\s+directives?\b'),
    ('readiness', r'\bairworthy\b'),
    ('readiness', r'\bcleared\s+for\s+flight\b'),
    ('readiness', r'\bsafe\s+to\s+fly\b'),
    ('readiness', r'\bfit\s+for\s+flight\b'),
    ('readiness', r'\breturn(?:ed|ing)?\s+to\s+service\b'),
    ('certification', r'\bsign(?:ed|ing)?[\s-]+off\b'),
    ('certification', r'\bcertif(?:y|ies|ied|ying|ication|ications)\b'),
    ('certification', r'\breleas(?:e|ed|ing)\s+to\s+service\b'),
    ('certification', r'\bapproved\s+for\s+flight\b'),
    ('real-world', r'\b(?:FAA|EASA|Transport\s+Canada|Boeing|Airbus|Embraer|Bombardier)\b'),
    ('real-world', r'\bATA\s+chapter\b'),
    ('real-world', r'\b(?:Air\s+Canada|WestJet|Delta(?:\s+Air\s+Lines)?|United\s+Airlines|American\s+Airlines)\b'),
    ('real-world', r'\b(?:Southwest\s+Airlines|British\s+Airways|Lufthansa|Emirates|Qatar\s+Airways)\b'),
    ('real-world', r'\b(?:Singapore\s+Airlines|Air\s+France|KLM|Ryanair|easyJet|Qantas|JetBlue)\b'),
    ('url', r'\b[a-z][a-z0-9+.-]*://\S+|\bwww\.\S+'),
    ('url', r'(?<![\w@])(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}\b'),
    ('email', r'(?<![\w.+-])[\w.+-]+@[\w.-]+\.[a-z]{2,63}\b'),
    ('phone', r'(?<![\w-])(?:\+?1[ .-]?)?(?:\(\d{3}\)|\d{3})[ .-]?\d{3}[ .-]?\d{4}(?![\w-])'),
    ('phone', r'(?<![\w-])\d{3}[ .-]\d{4}(?![\w-])'),
    ('phone', r'(?<![\w-])\+\d{1,3}[ .-](?:\d[ .()-]?){7,14}\d(?![\w-])'),
)
ID_TOKEN = re.compile(r'\b(?:DEMO-AIR|ISSUE|TASK|EVT|TEAM|PERSON|SHIFT|DOC)-[\w-]*', re.IGNORECASE)
WORD = re.compile(r"\b\w+(?:[-']\w+)*\b")
REQUIRED = ('doc_id', 'kind', 'title', 'date', 'author', 'team', 'related_ids', 'authored_by', 'spec_version')
MATCHED = ('doc_id', 'kind', 'date', 'author', 'team', 'related_ids', 'spec_version', 'station', 'program')
HEADERS = set(REQUIRED) | {'station', 'program'}


def normalized(text: str) -> str:
    return unicodedata.normalize('NFKC', text)


def spec_problems(spec: Any, records: Records) -> list[str]:
    """Reject malformed plans before using their values in document diagnostics."""
    if (
        not isinstance(spec, dict)
        or spec.get('version') != VERSION
        or type(spec.get('seed')) is not int
        or not isinstance(spec.get('slots'), list)
        or len(spec['slots']) != 60
    ):
        return ['SPEC: invalid specification']
    slots = spec['slots']
    strings = ('doc_id', 'kind', 'title', 'date', 'author', 'team')
    for index, slot in enumerate(slots, start=1):
        if (
            not isinstance(slot, dict)
            or any(not isinstance(slot.get(key), str) or not slot[key].strip() for key in strings)
            or slot['doc_id'] != f'DOC-{index:04}'
            or slot['kind'] not in COUNTS
            or not isinstance(slot.get('related_ids'), list)
            or any(not isinstance(ident, str) for ident in slot['related_ids'])
            or any(key in slot and not isinstance(slot[key], str) for key in ('station', 'program'))
        ):
            return ['SPEC: invalid specification']
    if Counter(slot['kind'] for slot in slots) != COUNTS:
        return ['SPEC: invalid specification kind counts']
    first, last = history_bounds(records)
    allowed_related = {ident for name in ('aircraft', 'issues', 'tasks', 'teams') for ident in records[name]}
    problems = []
    for slot in slots:
        doc_id = slot['doc_id']

        def problem(rule: str, doc_id: str = doc_id) -> None:
            problems.append(f'{doc_id}: {rule}')

        if slot['author'] not in records['people']:
            problem('invalid slot author')
        elif records['people'][slot['author']]['team_id'] != slot['team']:
            problem('slot author team mismatch')
        if slot['team'] not in records['teams']:
            problem('invalid slot team')
        if slot.get('word_range') != word_range(slot['kind']):
            problem('invalid slot word range')
        try:
            day = date.fromisoformat(slot['date'])
            if day.isoformat() != slot['date'] or day.year != 2026 or not first <= day <= last:
                problem('invalid slot date')
        except ValueError:
            problem('invalid slot date')
        related = set(slot['related_ids'])
        if not 1 <= len(related) <= 6 or len(related) != len(slot['related_ids']) or not related <= allowed_related:
            problem('invalid slot related IDs')
        aircraft = related & records['aircraft'].keys()
        issues = related & records['issues'].keys()
        for field in ('station', 'program'):
            if field in slot and (
                slot[field] not in {row[field] for row in records['aircraft'].values()}
                or any(records['aircraft'][ident][field] != slot[field] for ident in aircraft)
            ):
                problem(f'invalid slot {field}')
        if slot['kind'] == 'handover' and 'station' not in slot:
            problem('missing slot station')
        if slot['kind'] == 'memo' and 'program' not in slot:
            problem('missing slot program')
        for ident in issues:
            if aircraft and records['issues'][ident]['aircraft_id'] not in aircraft:
                problem('slot issue aircraft mismatch')
            if slot['date'] < records['issues'][ident]['opened_at'][:10]:
                problem('slot date precedes related issue')
        for ident in related & records['tasks'].keys():
            if issues and records['tasks'][ident]['issue_id'] not in issues:
                problem('slot task issue mismatch')
            if slot['date'] < records['tasks'][ident]['created_at'][:10]:
                problem('slot date precedes related task')
    return list(dict.fromkeys(problems))


def parse_document(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0] != '---':
        raise ValueError('front matter must start with delimiter')
    try:
        end = lines.index('---', 1)
    except ValueError:
        raise ValueError('front matter requires closing delimiter') from None
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(':')
        if not separator or key not in HEADERS or key in fields:
            raise ValueError('front matter requires unique supported key: value lines')
        fields[key] = value.strip()
    return fields, '\n'.join(lines[end + 1 :])


def document_problems(text: str, slot: dict[str, Any], known_ids: set[str]) -> tuple[list[str], str]:
    problems = []
    for category, pattern in BANNED_PATTERNS:
        if re.search(pattern, normalized(text), re.IGNORECASE):
            problems.append(f'banned {category}')
    try:
        fields, body = parse_document(text)
    except ValueError as error:
        # Only literal messages from our parser, never underlying parse errors.
        return [*dict.fromkeys(problems), str(error)], ''
    for key in (*REQUIRED, *(key for key in ('station', 'program') if key in slot)):
        if key not in fields:
            problems.append(f'missing field {key}')
    for key in MATCHED:
        if key not in fields:
            continue
        expected = VERSION if key == 'spec_version' else slot.get(key)
        actual: Any = fields[key]
        if key == 'related_ids':
            actual = [part.strip() for part in fields[key].split(',')]
        if actual != expected:
            problems.append(f'field {key} differs from slot')
    title = ' '.join(normalized(fields.get('title', '')).casefold().split())
    if 'title' in fields and not title:
        problems.append('title must be nonempty')
    if not fields.get('authored_by', ''):
        problems.append('authored_by must be nonempty')
    low, high = word_range(slot['kind'])
    if not low <= len(WORD.findall(normalized(body))) <= high:
        problems.append(f'body word count outside {low}-{high}')
    allowed = {*slot['related_ids'], slot['author'], slot['team']}
    for ident in set(ID_TOKEN.findall(normalized(body))):
        if ident not in known_ids:
            problems.append('unknown body ID')
        elif ident not in allowed:
            problems.append('body ID not allowed by slot')
    return list(dict.fromkeys(problems)), title


def validate(documents: Path, spec_path: Path, data: Path, *, allow_missing: bool = False) -> list[str]:
    try:
        spec = json.loads(spec_path.read_text(encoding='utf-8'))
        records = read_records(data)
        problems = spec_problems(spec, records)
    except OSError, UnicodeError, ValueError, KeyError, TypeError:
        return ['SPEC: invalid specification or CSV inputs']
    if problems:
        return problems
    slots = {slot['doc_id']: slot for slot in spec['slots']}
    known = {ident for table in records.values() for ident in table} | slots.keys()
    titles: dict[str, list[str]] = defaultdict(list)
    paths = {path.stem: path for path in sorted(documents.glob('DOC-*.md'))}
    if not allow_missing:
        problems.extend(f'{ident}: missing document' for ident in sorted(slots.keys() - paths.keys()))
    for ident, path in paths.items():
        # Never echo malformed filenames or IDs supplied in front matter.
        label = ident if re.fullmatch(r'DOC-[0-9]{4}', ident) else 'DOC-????'
        if ident not in slots:
            problems.append(f'{label}: unexpected document')
            continue
        try:
            if path.is_symlink():
                problems.append(f'{label}: document must not be a symbolic link')
                continue
            text = path.read_text(encoding='utf-8')
        except OSError, UnicodeError:
            problems.append(f'{label}: cannot read UTF-8 document')
            continue
        rules, title = document_problems(text, slots[ident], known)
        problems.extend(f'{label}: {rule}' for rule in rules)
        if title:
            titles[title].append(ident)
    for matching in titles.values():
        if len(matching) > 1:
            problems.extend(f'{ident}: duplicate title' for ident in matching)
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--documents', type=Path, default=ROOT / 'data/documents')
    parser.add_argument('--spec', type=Path, default=ROOT / 'data/documents/spec.json')
    parser.add_argument('--data', type=Path, default=ROOT / 'data/synthetic')
    parser.add_argument(
        '--allow-missing', action='store_true', help='Permit missing slots; validate all present files.'
    )
    args = parser.parse_args()
    problems = validate(args.documents, args.spec, args.data, allow_missing=args.allow_missing)
    if problems:
        print('\n'.join(problems))
        return 1
    count = len(list(args.documents.glob('DOC-*.md')))
    suffix = f' ({60 - count} missing slots allowed)' if args.allow_missing else ''
    print(f'Validated {count} documents{suffix}.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
