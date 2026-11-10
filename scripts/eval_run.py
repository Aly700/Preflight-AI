#!/usr/bin/env python3
"""Score original fictional questions against independent SQL; requires the local stack for a live run."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import UTC, datetime
import functools
from http.client import HTTPException
import json
import math
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time
from typing import Any
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
# Support both direct execution and importlib-based tests without making scripts a package.
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT))
import compose_check  # noqa: E402

from preflight_core.scope import unique_object, validate_scope  # noqa: E402

CATEGORIES = {
    'lookups',
    'comparisons',
    'aggregates',
    'timelines',
    'follow-ups',
    'pagination',
    'missing records',
    'ambiguity',
    'out-of-scope requests',
    'injection',
    'tool failures',
    'scoped',
    'scope escape',
    'documents',
    'documents and records',
}
TOOLS = {
    'get_aircraft_summary',
    'list_open_issues',
    'get_issue_history',
    'count_issues',
    'compare_aircraft',
    'search_issues',
    'search_documents',
    'list_tasks',
}
USAGE_KEYS = ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')
Events = list[tuple[str, dict[str, Any]]]
Ask = Callable[..., Events]


def check_keys(value: Any, required: set[str], optional: set[str], location: str) -> None:
    """Reject misspellings and missing fields before contacting the stack."""
    if not isinstance(value, dict):
        raise ValueError(f'{location}: expected an object')
    if unknown := set(value) - required - optional:
        raise ValueError(f'{location}: unknown keys {sorted(unknown)}')
    if missing := required - set(value):
        raise ValueError(f'{location}: missing keys {sorted(missing)}')


def load_cases(path: Path) -> tuple[str, list[dict[str, Any]]]:
    document = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique_object)
    check_keys(document, {'version', 'cases'}, set(), 'question file')
    version, cases = document['version'], document['cases']
    if not isinstance(version, str) or not version.strip():
        raise ValueError('version must be a nonempty string')
    if not isinstance(cases, list) or len(cases) not in (8, 12, 22):
        raise ValueError('question file must contain 8, 12, or 22 cases')
    seen: set[str] = set()
    for case in cases:
        check_keys(case, {'id', 'category', 'question', 'expect'}, {'history', 'scope'}, 'case')
        for field in ('id', 'category', 'question'):
            if not isinstance(case[field], str) or not case[field].strip():
                raise ValueError(f'case {field} must be a nonempty string')
        ident = case['id']
        if ident in seen:
            raise ValueError(f'duplicate case id: {ident}')
        seen.add(ident)
        if case['category'] not in CATEGORIES:
            raise ValueError(f'{ident}: invalid category {case["category"]!r}')
        if 'scope' in case:
            case['scope'] = validate_scope(case['scope'])
        history = case.get('history', [])
        if not isinstance(history, list):
            raise ValueError(f'{ident}: history must be a list')
        for turn in history:
            check_keys(turn, {'role', 'content'}, set(), f'{ident} history')
            if turn['role'] not in ('user', 'assistant') or not isinstance(turn['content'], str):
                raise ValueError(f'{ident}: invalid history role or content')
        expect = case['expect']
        check_keys(
            expect,
            set(),
            {
                'ids_sql',
                'numbers',
                'forbidden',
                'required',
                'tools_any',
                'min_tool_calls',
                'max_tool_calls',
                'verified',
            },
            f'{ident} expect',
        )
        if 'ids_sql' in expect and (not isinstance(expect['ids_sql'], str) or not expect['ids_sql'].strip()):
            raise ValueError(f'{ident}: ids_sql must be nonempty SQL')
        numbers = expect.get('numbers', [])
        if not isinstance(numbers, list):
            raise ValueError(f'{ident}: numbers must be a list')
        labels = {'ids'}
        for number in numbers:
            check_keys(number, {'label', 'sql'}, set(), f'{ident} number')
            if any(not isinstance(number[k], str) or not number[k].strip() for k in ('label', 'sql')):
                raise ValueError(f'{ident}: number label and sql must be nonempty strings')
            if number['label'] in labels:
                raise ValueError(f'{ident}: duplicate or reserved number label {number["label"]!r}')
            labels.add(number['label'])
        for field in ('forbidden', 'required', 'tools_any'):
            values = expect.get(field, [])
            if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values):
                raise ValueError(f'{ident}: {field} must be a list of nonempty strings')
        for field in ('forbidden', 'required'):
            for pattern in expect.get(field, []):
                try:
                    re.compile(pattern, re.IGNORECASE)
                except re.error as error:
                    raise ValueError(f'{ident}: invalid {field} regex') from error
        if set(expect.get('tools_any', [])) - TOOLS:
            raise ValueError(f'{ident}: unknown tool in tools_any')
        for field in ('min_tool_calls', 'max_tool_calls'):
            limit = expect.get(field, 0)
            if type(limit) is not int or limit < 0:
                raise ValueError(f'{ident}: {field} must be a nonnegative integer')
        verified = expect.get('verified', True)
        if verified is not None and type(verified) is not bool:
            raise ValueError(f'{ident}: verified must be boolean or null')
    return version, cases


def sql_values(container: str, database: str, queries: dict[str, str]) -> dict[str, Any]:
    """Use compose_check's psql invocation; empty rows are [], one row a scalar, many rows a list."""
    command = [
        'docker',
        'exec',
        '-i',
        container,
        'psql',
        '-X',
        '-At',
        '-v',
        'ON_ERROR_STOP=1',
        '-U',
        'postgres',
        '-d',
        database,
    ]
    values: dict[str, Any] = {}
    for label, query in queries.items():
        result = subprocess.run(command, input=query, capture_output=True, text=True, timeout=60, cwd=ROOT)
        if result.returncode:
            raise RuntimeError(f'SQL query failed: {label}')
        rows: list[int | str] = [int(row) if re.fullmatch(r'-?\d+', row) else row for row in result.stdout.splitlines()]
        values[label] = rows[0] if len(rows) == 1 else rows
    return values


def number_tokens(text: str) -> set[str]:
    """Integer quantities, excluding fictional IDs, ISO dates/times and bare calendar years."""
    text = re.sub(r'\b(?:DEMO-AIR|ISSUE|TASK|EVT|TEAM|PERSON|SHIFT)-\d+\b', ' ', text)
    text = re.sub(
        r'\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?'
        r'(?:Z|[+-]\d{2}:?\d{2})?)?',
        ' ',
        text,
    )
    text = re.sub(r'\b\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?\b', ' ', text)
    tokens = re.findall(r'(?<![\w.,+\-])(?:\d{1,3}(?:,\d{3})+|\d+)(?![\w,]|\.\d)', text)
    return {
        str(int(token.replace(',', ''))) for token in tokens if not (len(token) == 4 and 1900 <= int(token) <= 2100)
    }


# A visible list of the claims the separate claims round collects ("Declared record IDs: ...").
DECLARATION_BLOCK = re.compile(
    r'^\W*declared\b[^\n]{0,40}\b(?:ids?|numbers|claims|values)\b', re.IGNORECASE | re.MULTILINE
)


def score(case: dict[str, Any], events: Events, truth: dict[str, Any], *, runtime: str) -> dict[str, Any]:
    expect = case['expect']
    text = ''.join(str(data.get('text', '')) for name, data in events if name == 'delta')
    done = next((data for name, data in reversed(events) if name == 'done'), None)
    tool_events = [data for name, data in events if name == 'tool']
    completed = {str(data['name']) for data in tool_events if data.get('status') == 'complete'}
    # A failed/refused attempt still counts against the budget. Running/complete share a call_id.
    attempts = {str(data['call_id']) for data in tool_events if data.get('call_id') is not None}
    anonymous = sum(data.get('status') in ('complete', 'failed') for data in tool_events if data.get('call_id') is None)
    attempted = len(attempts) + anonymous
    successful = (done or {}).get('tool_calls', sum(data.get('status') == 'complete' for data in tool_events))
    failures = []
    ids = truth.get('ids', [])
    if not isinstance(ids, list):
        ids = [ids]
    for record in ids:
        if not re.search(r'(?<![\w-])' + re.escape(str(record)) + r'(?![\w-])', text):
            failures.append(f'ids: {record}')
    tokens = number_tokens(text)
    for number in expect.get('numbers', []):
        value = truth.get(number['label'])
        if type(value) is not int or str(value) not in tokens:
            failures.append(f'numbers: {number["label"]}')
    for pattern in expect.get('forbidden', []):
        if re.search(pattern, text, re.IGNORECASE):
            failures.append(f'forbidden: {pattern}')
    for pattern in expect.get('required', []):
        if not re.search(pattern, text, re.IGNORECASE):
            failures.append(f'required: {pattern}')
    if expect.get('tools_any') and not completed.intersection(expect['tools_any']):
        failures.append('tools_any')
    if 'max_tool_calls' in expect and max(attempted, successful) > expect['max_tool_calls']:
        failures.append('max_tool_calls')
    if done is None:
        failures.append('done')
    verified = 'n/a' if runtime == 'copilot' else (done or {}).get('verified')
    expected_verified = expect.get('verified', True)
    if runtime != 'copilot' and expected_verified is not None and verified is not expected_verified:
        failures.append('verified')
    if any(name == 'error' for name, _ in events):
        failures.append('no_error')
    return {
        'passed': not failures,
        'failures': failures,
        'tool_calls': successful,
        'tools': sorted(completed),
        'rounds': (done or {}).get('rounds'),
        'usage': (done or {}).get('usage'),
        'model_calls': (done or {}).get('model_calls'),
        'verified': verified,
        'unverified': (done or {}).get('unverified'),
        'undeclared': (done or {}).get('undeclared'),
        'attempted_tool_calls': attempted,
        'efficiency': attempted - expect['min_tool_calls'] if 'min_tool_calls' in expect else None,
        'declaration_leak': bool(DECLARATION_BLOCK.search(text)),
        'answer': ''.join(str(data.get('text', '')) for name, data in events if name == 'delta'),
        'first_delta_s': None,
        'total_s': None,
    }


def runtime_configuration() -> dict[str, str]:
    """Read only public model settings, never environment dumps or credential files."""
    code = (
        'import json, os; print(json.dumps({'
        '"runtime": os.environ.get("CHAT_RUNTIME", "copilot"), '
        '"provider": os.environ.get("MODEL_PROVIDER", "copilot"), '
        '"model": os.environ.get("COPILOT_MODEL", "") }))'
    )
    config: dict[str, str] = json.loads(
        compose_check.run(['docker', 'exec', 'preflight-oss-api', 'python', '-c', code])
    )
    return config


def empty_totals() -> dict[str, Any]:
    return {
        'total': 0,
        'passed': 0,
        'failed': 0,
        'skipped': 0,
        'extra_tool_calls': 0,
        'minimal_cases': 0,
        'declaration_leaks': 0,
        'usage': dict.fromkeys(USAGE_KEYS, 0),
        'model_calls': 0,
    }


def run(
    cases: list[dict[str, Any]],
    *,
    base: str,
    container: str,
    database: str,
    gap: float,
    ask_fn: Ask = compose_check.ask,
) -> dict[str, Any]:
    if not math.isfinite(gap) or gap < 0:
        raise ValueError('gap must be finite and nonnegative')
    report: dict[str, Any] = {'started_at': datetime.now(UTC).isoformat(), **runtime_configuration(), 'cases': []}
    for index, case in enumerate(cases):
        if index:
            time.sleep(gap)
        expect = case['expect']
        queries = {n['label']: n['sql'] for n in expect.get('numbers', [])}
        if 'ids_sql' in expect:
            queries['ids'] = expect['ids_sql']
        # SQL failure aborts the run: an unavailable oracle is not an incorrect model answer.
        truth = sql_values(container, database, queries)
        started = time.monotonic()
        first_delta: float | None = None

        def observe(name: str, data: dict[str, Any], start: float = started) -> None:
            nonlocal first_delta
            if name == 'delta' and data.get('text') and first_delta is None:
                first_delta = time.monotonic() - start

        result: dict[str, Any]
        for attempt in range(4):
            try:
                events = ask_fn(
                    base,
                    case['question'],
                    history=case.get('history', []),
                    on_event=observe,
                    **({'scope': validate_scope(case['scope'])} if 'scope' in case else {}),
                )
                result = score(case, events, truth, runtime=report['runtime'])
                break
            except (OSError, RuntimeError, ValueError, HTTPException) as error:
                limited = (isinstance(error, HTTPError) and error.code == 429) or bool(
                    re.match(r'^chat answered 429:', str(error))
                )
                if limited and attempt < 3:
                    time.sleep((15, 30, 60)[attempt])
                    continue
                result = score(case, [], truth, runtime=report['runtime'])
                if limited:
                    result.update(passed=None, failures=[], skipped='rate-limited')
                else:
                    # Avoid copying HTTP response bodies (or infrastructure details) into reports.
                    result.update(passed=False, failures=[f'request: {type(error).__name__}'])
                break
        result.update(
            id=case['id'], category=case['category'], first_delta_s=first_delta, total_s=time.monotonic() - started
        )
        report['cases'].append(result)
    categories: dict[str, dict[str, Any]] = {}
    overall = empty_totals()
    for result in report['cases']:
        counts = categories.setdefault(result['category'], empty_totals())
        outcome = 'skipped' if result.get('skipped') else ('passed' if result['passed'] else 'failed')
        for bucket in (counts, overall):
            bucket['total'] += 1
            bucket[outcome] += 1
            bucket['model_calls'] += result['model_calls'] or 0
            if result['usage'] is not None:
                for key in USAGE_KEYS:
                    bucket['usage'][key] += result['usage'].get(key, 0)
            bucket['declaration_leaks'] += bool(result.get('declaration_leak'))
            if result['efficiency'] is not None:
                bucket['extra_tool_calls'] += max(result['efficiency'], 0)
                bucket['minimal_cases'] += result['efficiency'] == 0
    latencies = sorted(r['total_s'] for r in report['cases'] if not r.get('skipped'))
    report.update(
        categories=categories,
        overall=overall,
        latency={
            'median_s': statistics.median(latencies) if latencies else None,
            'p90_s': latencies[math.ceil(0.9 * len(latencies)) - 1] if latencies else None,
        },
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--questions', type=Path, default=ROOT / 'evals/questions.json')
    parser.add_argument('--base', default='http://127.0.0.1:8080')
    parser.add_argument('--container', default='preflight-oss-postgres')
    parser.add_argument('--database', default='preflight_oss')
    parser.add_argument('--gap', type=float, default=13)
    parser.add_argument('--only', help='Comma-separated case IDs')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--token-file', type=Path, help='Chat access key file, when calling the API directly')
    args = parser.parse_args()
    try:
        version, cases = load_cases(args.questions)
        if not math.isfinite(args.gap) or args.gap < 0:
            raise ValueError('gap must be finite and nonnegative')
        if args.only:
            selected = set(args.only.split(','))
            if unknown := selected - {case['id'] for case in cases}:
                raise ValueError(f'unknown case ids: {sorted(unknown)}')
            cases = [case for case in cases if case['id'] in selected]
        token = args.token_file.read_text(encoding='utf-8').strip() if args.token_file else None
    except (OSError, ValueError) as error:
        parser.error(str(error))
    try:
        report = run(
            cases,
            base=args.base,
            container=args.container,
            database=args.database,
            gap=args.gap,
            ask_fn=functools.partial(compose_check.ask, token=token) if token else compose_check.ask,
        )
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f'Evaluation aborted: {type(error).__name__}\n')
    report['version'] = version
    output = args.output or ROOT / 'evidence' / f'eval-{datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")}.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    for result in report['cases']:
        status = (
            'skipped: ' + result['skipped'] if result.get('skipped') else ('passed' if result['passed'] else 'failed')
        )
        print(f'{result["id"]}: {status}' + (f' ({", ".join(result["failures"])})' if result['failures'] else ''))
    print('Totals: ' + ', '.join(f'{count} {key}' for key, count in report['overall'].items()))
    sys.exit(1 if report['overall']['failed'] else 0)


if __name__ == '__main__':
    main()
