"""Summarise repeated evaluation runs: per-case pass rates, per-run spread and failure kinds.

Reads reports written by eval_run.py for the same question set. Content failures
(wrong or missing records and numbers, a missing decline, a forbidden phrase, an
error) say whether the answer was right; verification and tool-choice failures
are stricter checks on how it was produced.

    python3 -B scripts/eval_summary.py evidence/run1.json evidence/run2.json --output evidence/summary.json
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Sequence
import json
from pathlib import Path
import statistics
import sys
from typing import Any

CONTENT_PREFIXES = ('ids:', 'numbers:', 'forbidden:', 'required:')
CONTENT_LABELS = frozenset({'no_error', 'done'})
TOOL_LABELS = frozenset({'tools_any', 'max_tool_calls'})


def failure_kind(label: str) -> str:
    """content, verification, tools or other, for one scorer failure label."""
    if label.startswith(CONTENT_PREFIXES) or label in CONTENT_LABELS:
        return 'content'
    if label == 'verified':
        return 'verification'
    if label in TOOL_LABELS:
        return 'tools'
    return 'other'


def spread(values: list[int]) -> dict[str, float]:
    return {'mean': statistics.mean(values), 'min': min(values), 'max': max(values)}


def summarise(reports: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate reports of one question set; skipped cases count in no rate."""
    if not reports:
        raise ValueError('need at least one report')
    versions = {report.get('version') for report in reports}
    if len(versions) != 1:
        raise ValueError(f'reports mix question-set versions: {sorted(map(str, versions))}')
    order = [result['id'] for result in reports[0]['cases']]
    if any(sorted(result['id'] for result in report['cases']) != sorted(order) for report in reports):
        raise ValueError('reports cover different cases')
    per_case: dict[str, dict[str, Any]] = {
        result['id']: {
            'id': result['id'],
            'category': result.get('category'),
            'runs': 0,
            'skipped': 0,
            'strict_passes': 0,
            'content_passes': 0,
            'kinds': Counter(),
            'failures': Counter(),
        }
        for result in reports[0]['cases']
    }
    per_run: list[dict[str, Any]] = []
    for report in reports:
        strict = content = scored = 0
        latencies: list[float] = []
        for result in report['cases']:
            entry = per_case[result['id']]
            if result.get('skipped'):
                entry['skipped'] += 1
                continue
            scored += 1
            entry['runs'] += 1
            failures = list(result.get('failures') or [])
            kinds = {failure_kind(label) for label in failures}
            entry['failures'].update(failures)
            entry['kinds'].update(kinds)
            strict += not failures
            content += 'content' not in kinds
            entry['strict_passes'] += not failures
            entry['content_passes'] += 'content' not in kinds
            if isinstance(result.get('total_s'), (int, float)):
                latencies.append(float(result['total_s']))
        per_run.append(
            {
                'started_at': report.get('started_at'),
                'model': report.get('model'),
                'scored': scored,
                'strict': strict,
                'content': content,
                'median_s': statistics.median(latencies) if latencies else None,
            }
        )
    cases = [
        {**entry, 'kinds': dict(entry['kinds']), 'failures': dict(entry['failures'])}
        for entry in (per_case[ident] for ident in order)
    ]
    failure_counts: Counter[str] = Counter()
    for entry in cases:
        failure_counts.update(entry['kinds'])
    return {
        'version': versions.pop(),
        'runs': len(reports),
        'models': sorted({str(run['model']) for run in per_run}),
        'per_run': per_run,
        'strict': spread([run['strict'] for run in per_run]),
        'content': spread([run['content'] for run in per_run]),
        'cases': cases,
        'flaky': sorted(entry['id'] for entry in cases if 0 < entry['strict_passes'] < entry['runs']),
        'always_failing': sorted(entry['id'] for entry in cases if entry['runs'] and not entry['strict_passes']),
        'failure_counts': dict(sorted(failure_counts.items())),
    }


def table(result: dict[str, Any]) -> str:
    runs = result['per_run']
    lines = [
        f'{result["version"]}: {result["runs"]} runs of {", ".join(result["models"])}',
        'strict ' + ', '.join(f'{run["strict"]}/{run["scored"]}' for run in runs),
        'content ' + ', '.join(f'{run["content"]}/{run["scored"]}' for run in runs),
        'median s ' + ', '.join('-' if run['median_s'] is None else f'{run["median_s"]:.1f}' for run in runs),
        'failure kinds over all case runs: ' + (json.dumps(result['failure_counts']) or '{}'),
    ]
    for entry in result['cases']:
        if entry['strict_passes'] < entry['runs'] or entry['skipped']:
            kinds = ', '.join(f'{kind} {count}' for kind, count in sorted(entry['kinds'].items()))
            lines.append(
                f'  {entry["id"]}: strict {entry["strict_passes"]}/{entry["runs"]}, '
                f'content {entry["content_passes"]}/{entry["runs"]}' + (f' ({kinds})' if kinds else '')
            )
    return '\n'.join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('reports', nargs='+', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        result = summarise([json.loads(path.read_text(encoding='utf-8')) for path in args.reports])
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f'Cannot summarise: {type(error).__name__}: {error}', file=sys.stderr)
        return 2
    if args.output is not None:
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(table(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
