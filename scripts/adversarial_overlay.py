#!/usr/bin/env python3
"""Apply fictional prompt-injection records to an isolated local evaluation database.

This overlay must never be used on the hosted demo. Standard library only;
psql runs inside the explicitly named container, in one transaction. Removal
requires clearing the local fictional tables and a full reload with
scripts/load_postgres.py; see evals/README.md. No credentials are read here.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'evals/fixtures/adversarial_overlay.json'
ISSUE_FIELDS = 'issue_id aircraft_id title description subsystem priority status opened_at closed_at'.split()
FIELDS = [*ISSUE_FIELDS, 'event_type', 'note']


def prepare_overlay() -> str:
    """Build the transaction without database access; the database checks constraints."""
    rows = json.loads(FIXTURE.read_text(encoding='utf-8'))['issues']
    if len(rows) != 6 or {row['issue_id'] for row in rows} != {f'ISSUE-{n:04}' for n in range(901, 907)}:
        raise ValueError('Expected exactly ISSUE-0901 through ISSUE-0906')
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=FIELDS, lineterminator='\n')
    writer.writerows(rows)
    data = buffer.getvalue()
    if '\\.' in data.splitlines():
        raise ValueError('Overlay contains a psql COPY terminator')
    columns = ', '.join(ISSUE_FIELDS)
    return f"""BEGIN;
SET LOCAL TIME ZONE 'UTC';
SET LOCAL lock_timeout = '10s';
SELECT pg_advisory_xact_lock(20261103);
LOCK TABLE preflight.issues, preflight.activity_events IN SHARE ROW EXCLUSIVE MODE;
CREATE TEMP TABLE overlay_issues (LIKE preflight.issues INCLUDING CONSTRAINTS) ON COMMIT DROP;
ALTER TABLE overlay_issues ADD COLUMN event_type text NOT NULL, ADD COLUMN note text NOT NULL;
COPY overlay_issues ({', '.join(FIELDS)}) FROM STDIN WITH (FORMAT csv);
{data}\\.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM preflight.issues JOIN overlay_issues USING (issue_id)) THEN
        RAISE EXCEPTION 'Overlay issue IDs already exist; refusing to overwrite';
    END IF;
END $$;
INSERT INTO preflight.issues ({columns}) SELECT {columns} FROM overlay_issues;
INSERT INTO preflight.activity_events (event_id, issue_id, task_id, actor_id, event_type, occurred_at, note)
SELECT 'EVT-' || (base.n + row_number() OVER (ORDER BY issue_id))::text,
       issue_id, NULL, NULL, event_type, opened_at, note
FROM overlay_issues CROSS JOIN (
    SELECT coalesce(max(substring(event_id from 5)::numeric), 0) AS n FROM preflight.activity_events
) base;
COMMIT;
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['apply'])
    parser.add_argument('--container', required=True)
    parser.add_argument('--database', required=True)
    args = parser.parse_args()
    if not all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', value) for value in (args.container, args.database)):
        parser.error('Invalid container or database name')
    try:
        sql = prepare_overlay()
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
            # PostgreSQL errors can quote record contents; do not copy them into logs.
            raise ValueError('Overlay failed and rolled back; check duplicate IDs and the local schema')
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        parser.error(str(error))
    print('Overlay applied. Never use it on the hosted demo. After evaluation, clear the local fictional tables')
    print('and fully reload with scripts/load_postgres.py (see evals/README.md).')


if __name__ == '__main__':
    main()
