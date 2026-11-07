#!/usr/bin/env python3
"""Validate fictional CSV fixtures and import them transactionally into PostgreSQL.

Uses the standard library and psql inside an existing Docker container named
explicitly on the command line; there is no default target. No database
password is read or printed. Existing different data causes rollback.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
FIELDS = {
    'teams': 'team_id team_name'.split(),
    'people': 'person_id display_name team_id'.split(),
    'aircraft': 'aircraft_id program stage station record_created_at'.split(),
    'issues': 'issue_id aircraft_id title description subsystem priority status opened_at closed_at'.split(),
    'tasks': 'task_id issue_id task_kind status assigned_team_id assigned_person_id created_at completed_at'.split(),
    'activity_events': 'event_id issue_id task_id actor_id event_type occurred_at note'.split(),
    'shifts': 'shift_id person_id team_id shift_start_at shift_end_at'.split(),
}


def prepare_load(data: Path) -> tuple[str, dict[str, int]]:
    """Prepare a complete transaction without accessing a database."""
    config_bytes = (ROOT / 'config/synthetic_spec.json').read_bytes()
    spec = json.loads(config_bytes)
    manifest = json.loads((data / 'manifest.json').read_text(encoding='utf-8'))
    expected_files = {f'{name}.csv' for name in FIELDS}
    if (
        manifest.get('phase') != 'week1_structured_data'
        or set(manifest.get('files', {})) != expected_files
        or manifest.get('config_sha256') != hashlib.sha256(config_bytes).hexdigest()
    ):
        raise ValueError('Invalid manifest or configuration hash')
    if manifest.get('counts') != spec['counts']:
        raise ValueError('Manifest count mismatch')
    verified = {}
    for name, fields in FIELDS.items():
        filename = f'{name}.csv'
        raw = (data / filename).read_bytes()
        metadata = manifest['files'][filename]
        if hashlib.sha256(raw).hexdigest() != metadata.get('sha256'):
            raise ValueError(f'CSV hash mismatch: {filename}')
        text = raw.decode('utf-8')
        reader = csv.DictReader(io.StringIO(text, newline=''))
        records = list(reader)
        if reader.fieldnames != fields or any(set(r) != set(fields) or None in r.values() for r in records):
            raise ValueError(f'CSV column mismatch: {filename}')
        if len(records) != spec['counts'][name] or len(records) != metadata.get('rows'):
            raise ValueError(f'CSV count mismatch: {filename}')
        if '\\.' in text.splitlines():
            raise ValueError('CSV contains a psql COPY terminator')
        verified[name] = text
    parts = [
        'BEGIN;',
        "SET LOCAL TIME ZONE 'UTC';",
        "SET LOCAL lock_timeout = '10s';",
        'SELECT pg_advisory_xact_lock(20261103);',
        (ROOT / 'sql/schema.sql').read_text(encoding='utf-8'),
    ]
    for name, fields in FIELDS.items():
        columns = ', '.join(fields)
        parts += [
            f'CREATE TEMP TABLE incoming_{name} (LIKE preflight.{name}) ON COMMIT DROP;',
            f'COPY incoming_{name} ({columns}) FROM STDIN WITH (FORMAT csv, HEADER true);',
            verified[name].rstrip('\n'),
            '\\.',
            f'INSERT INTO preflight.{name} ({columns}) SELECT {columns} FROM incoming_{name} ON CONFLICT DO NOTHING;',
            f"""DO $$ BEGIN
                IF EXISTS (SELECT {columns} FROM preflight.{name} EXCEPT SELECT {columns} FROM incoming_{name})
                   OR EXISTS (SELECT {columns} FROM incoming_{name} EXCEPT SELECT {columns} FROM preflight.{name})
                THEN RAISE EXCEPTION 'Existing {name} differs from the fixture; refusing to overwrite'; END IF;
            END $$;""",
        ]
    parts += [(ROOT / 'sql/verify_data.sql').read_text(encoding='utf-8'), 'COMMIT;']
    return '\n'.join(parts) + '\n', spec['counts']


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=ROOT / 'data/synthetic')
    parser.add_argument('--container', help='Name of the running PostgreSQL container (no default)')
    parser.add_argument('--database', help='Target database inside that container (no default)')
    parser.add_argument('--sql-only', action='store_true', help='Print the transaction without connecting')
    args = parser.parse_args()
    if not args.sql_only:
        # No defaults: the operator names the target every time.
        if args.container is None or args.database is None:
            parser.error('--container and --database are required unless --sql-only is given')
        if not all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', x) for x in (args.container, args.database)):
            parser.error('Invalid container or database name')
    try:
        sql, counts = prepare_load(args.data)
        if args.sql_only:
            print(sql, end='')
            return
        command = [
            'docker',
            'exec',
            '-i',
            args.container,
            'psql',
            '-X',
            '-q',
            '-v',
            'ON_ERROR_STOP=1',
            '-U',
            'postgres',
            '-d',
            args.database,
        ]
        result = subprocess.run(command, input=sql, text=True, capture_output=True, timeout=120)
        if result.returncode:
            raise ValueError('PostgreSQL import failed and rolled back: ' + result.stderr.strip())
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        parser.error(str(exc))
    print(json.dumps({'result': 'verified', 'counts': counts, 'transaction': 'committed'}, sort_keys=True))


if __name__ == '__main__':
    main()
