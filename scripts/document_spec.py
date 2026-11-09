"""Plan independently authored fictional paperwork; never generate document bodies."""

import argparse
from collections import defaultdict
import csv
from datetime import date, timedelta
import json
from pathlib import Path
import random
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
VERSION = 'documents-2026-11-08.1'
SEED = 20261108
COUNTS = {'policy': 8, 'glossary': 2, 'handover': 14, 'memo': 10, 'coordination': 14, 'retrospective': 8, 'form': 4}
FILES = {
    'aircraft': 'aircraft_id',
    'issues': 'issue_id',
    'tasks': 'task_id',
    'activity_events': 'event_id',
    'teams': 'team_id',
    'people': 'person_id',
    'shifts': 'shift_id',
}
TOPICS = {
    'policy': (
        'Issue-queue intake',
        'Triage ownership',
        'Queue-priority discussion',
        'Blocked queue escalation',
        'Reopened queue routing',
        'Review queue scheduling',
        'Queue context requirements',
        'Triage meeting cadence',
    ),
    'glossary': ('Issue statuses and priorities', 'Task statuses, task kinds and subsystems'),
    'handover': (
        'Station shift queue handover',
        'Station shift ownership handover',
        'Station shift discussion handover',
    ),
    'memo': ('Program queue review', 'Program coordination review', 'Program backlog discussion'),
    'coordination': ('Team queue coordination', 'Team ownership discussion', 'Team review scheduling'),
    'retrospective': ('Queue intake retrospective', 'Triage cadence retrospective', 'Handover context retrospective'),
    'form': (
        'Escalation request',
        'Queue-priority change request',
        'Ownership discussion request',
        'Review meeting request',
    ),
}

type Records = dict[str, dict[str, dict[str, str]]]


def word_range(kind: str) -> list[int]:
    return {'form': [120, 250], 'glossary': [300, 600]}.get(kind, [180, 500])


def read_records(directory: Path) -> Records:
    records = {}
    for name, key in FILES.items():
        with (directory / f'{name}.csv').open(encoding='utf-8', newline='') as handle:
            records[name] = {row[key]: row for row in csv.DictReader(handle)}
    return records


def history_bounds(records: Records) -> tuple[date, date]:
    days = [
        date.fromisoformat(value[:10])
        for table in records.values()
        for row in table.values()
        for key, value in row.items()
        if key.endswith('_at') and value
    ]
    return min(days), max(days)


def generate_spec(records: Records, config: dict[str, Any], seed: int) -> dict[str, Any]:
    rng = random.Random(seed)
    _, last_day = history_bounds(records)
    last_day = min(last_day, date.fromisoformat(config['date_end_utc'][:10]))
    tasks_by_issue: dict[str, list[str]] = defaultdict(list)
    people_by_team: dict[str, list[str]] = defaultdict(list)
    for ident, row in sorted(records['tasks'].items()):
        tasks_by_issue[row['issue_id']].append(ident)
    for ident, row in sorted(records['people'].items()):
        people_by_team[row['team_id']].append(ident)
    slots: list[dict[str, Any]] = []
    for kind, count in COUNTS.items():
        for index in range(count):
            issue_id = rng.choice(sorted(tasks_by_issue))
            issue = records['issues'][issue_id]
            aircraft = records['aircraft'][issue['aircraft_id']]
            candidates = tasks_by_issue[issue_id]
            tasks = sorted(rng.sample(candidates, rng.randint(1, min(3, len(candidates)))))
            task = records['tasks'][rng.choice(tasks)]
            team = task['assigned_team_id']
            author = rng.choice(people_by_team[team])
            earliest = max(
                date.fromisoformat(config['date_start_utc'][:10]),
                date.fromisoformat(aircraft['record_created_at'][:10]),
                date.fromisoformat(issue['opened_at'][:10]),
                *(date.fromisoformat(records['tasks'][ident]['created_at'][:10]) for ident in tasks),
            )
            day = earliest + timedelta(days=rng.randint(0, (last_day - earliest).days))
            slot: dict[str, Any] = {
                'doc_id': f'DOC-{len(slots) + 1:04}',
                'kind': kind,
                'title': f'{TOPICS[kind][index % len(TOPICS[kind])]} {index + 1:02}',
                'date': day.isoformat(),
            }
            if kind in {'handover', 'coordination', 'form'}:
                slot['station'] = aircraft['station']
            if kind not in {'policy', 'glossary'}:
                slot['program'] = aircraft['program']
            slot.update(
                author=author,
                team=team,
                word_range=word_range(kind),
                related_ids=[issue['aircraft_id'], issue_id, *tasks, team],
            )
            slots.append(slot)
    return {
        'version': VERSION,
        'seed': seed,
        'vocabulary': {
            key: config[key] for key in ('issue_statuses', 'task_statuses', 'priorities', 'subsystems', 'task_kinds')
        },
        'slots': slots,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed', type=int, default=SEED)
    parser.add_argument('--data', type=Path, default=ROOT / 'data/synthetic')
    parser.add_argument('--config', type=Path, default=ROOT / 'config/synthetic_spec.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'data/documents/spec.json')
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding='utf-8'))
        spec = generate_spec(read_records(args.data), config, args.seed)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    except OSError, UnicodeError, ValueError, KeyError, TypeError, IndexError:
        print('SPEC: cannot generate specification from inputs')
        return 1
    print(f'Generated {len(spec["slots"])} document slots (seed {args.seed}).')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
