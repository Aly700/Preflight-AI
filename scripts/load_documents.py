#!/usr/bin/env python3
"""Replace fictional documents and passages in one verified PostgreSQL transaction.

Standard library only. Uses psql inside an explicitly named container, with the
same stdin transaction and ON_ERROR_STOP contract as load_postgres.py. No password
is read. --sql-only validates and renders the transaction without contacting Docker.
"""

from __future__ import annotations

import argparse
from datetime import date
import json
import math
from pathlib import Path
import re
import subprocess
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TABLE_IDS = {
    'aircraft': 'aircraft_id',
    'issues': 'issue_id',
    'tasks': 'task_id',
    'activity_events': 'event_id',
    'teams': 'team_id',
    'people': 'person_id',
    'shifts': 'shift_id',
}
DOCUMENT_FIELDS = ('doc_id', 'kind', 'title', 'date', 'station', 'program', 'author', 'team', 'related_ids')
PASSAGE_FIELDS = {*DOCUMENT_FIELDS, 'passage_id', 'ordinal', 'text', 'embedding'}
KINDS = {'policy', 'glossary', 'handover', 'memo', 'coordination', 'retrospective', 'form'}


def sql_text(value: str | None) -> str:
    if value is None:
        return 'NULL'
    # Escape newlines as well as SQL quotes: input text can never become a psql
    # command on its own line. E strings make backslash handling explicit.
    if '\0' in value:
        raise ValueError('NUL is not allowed in document text')
    escaped = value.replace('\\', '\\\\').replace("'", "''").replace('\n', '\\n').replace('\r', '\\r')
    return "E'" + escaped + "'"


def read_passages(path: Path) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    documents: dict[str, dict[str, Any]] = {}
    passages = []
    ordinals: dict[str, int] = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        row = json.loads(line)
        if not isinstance(row, dict) or set(row) != PASSAGE_FIELDS:
            raise ValueError('Invalid passage fields')
        doc_id, ordinal = row['doc_id'], row['ordinal']
        if (
            not isinstance(doc_id, str)
            or not re.fullmatch(r'DOC-[0-9]{4}', doc_id)
            or type(ordinal) is not int
            or ordinal != ordinals.get(doc_id, 0) + 1
            or row['passage_id'] != f'{doc_id}#{ordinal}'
        ):
            raise ValueError('Invalid or duplicate passage ID/ordinal')
        for field in ('kind', 'title', 'date', 'author', 'team', 'text'):
            if not isinstance(row[field], str) or not row[field].strip() or '\0' in row[field]:
                raise ValueError('Invalid passage metadata or text')
        if row['kind'] not in KINDS or date.fromisoformat(row['date']).isoformat() != row['date']:
            raise ValueError('Invalid document kind or date')
        for field in ('station', 'program'):
            if row[field] is not None and not isinstance(row[field], str):
                raise ValueError('Invalid document scope')
        if not isinstance(row['related_ids'], list) or not all(isinstance(ident, str) for ident in row['related_ids']):
            raise ValueError('Invalid related IDs')
        vector = row['embedding']
        if (
            not isinstance(vector, list)
            or len(vector) != 384
            or not all(type(value) in (int, float) and math.isfinite(value) for value in vector)
        ):
            raise ValueError('Invalid embedding vector')
        if not math.isclose(math.sqrt(sum(value * value for value in vector)), 1, abs_tol=1e-5):
            raise ValueError('Embedding vector must be normalised')
        metadata = {field: row[field] for field in DOCUMENT_FIELDS}
        if doc_id in documents and documents[doc_id] != metadata:
            raise ValueError('Inconsistent document metadata')
        documents[doc_id] = metadata
        ordinals[doc_id] = ordinal
        passages.append(row)
    if not passages:
        raise ValueError('No passages to load')
    return documents, passages


def prepare_load(path: Path) -> tuple[str, dict[str, int]]:
    documents, passages = read_passages(path)
    counts = {'documents': len(documents), 'passages': len(passages)}
    missing = ' OR '.join(f"to_regclass('preflight.{table}') IS NULL" for table in TABLE_IDS)
    empty = ' OR '.join(f'NOT EXISTS (SELECT 1 FROM preflight.{table})' for table in TABLE_IDS)
    known_ids = ' UNION ALL '.join(
        f'SELECT 1 FROM preflight.{table} WHERE {column} = r.related_id' for table, column in TABLE_IDS.items()
    )
    parts = [
        'BEGIN;',
        "SET LOCAL lock_timeout = '10s';",
        'SELECT pg_advisory_xact_lock(20261103);',
        f"""DO $$ BEGIN
            IF {missing} THEN RAISE EXCEPTION 'Base tables must be loaded first (missing tables)'; END IF;
            IF {empty} THEN RAISE EXCEPTION 'Base tables must be loaded first (empty tables)'; END IF;
        END $$;""",
        'LOCK TABLE ' + ', '.join(f'preflight.{table}' for table in TABLE_IDS) + ' IN SHARE MODE;',
        (ROOT / 'sql/documents.sql').read_text(encoding='utf-8'),
        'DELETE FROM preflight.passages;',
        'DELETE FROM preflight.documents;',
    ]
    for doc in documents.values():
        values = [sql_text(doc[field]) for field in DOCUMENT_FIELDS[:-1]]
        values.append('ARRAY[' + ','.join(sql_text(ident) for ident in doc['related_ids']) + ']::text[]')
        parts.append(
            'INSERT INTO preflight.documents '
            '(doc_id, kind, title, doc_date, station, program, author_id, team_id, related_ids) VALUES ('
            + ','.join(values)
            + ');'
        )
    for row in passages:
        vector = '[' + ','.join(str(float(value)) for value in row['embedding']) + ']'
        values = [
            sql_text(row['passage_id']),
            sql_text(row['doc_id']),
            str(row['ordinal']),
            sql_text(row['text']),
            sql_text(vector) + '::vector',
        ]
        parts.append(
            'INSERT INTO preflight.passages (passage_id, doc_id, ordinal, text, embedding) VALUES ('
            + ','.join(values)
            + ');'
        )
    parts += [
        f"""DO $$ BEGIN
            IF (SELECT count(*) FROM preflight.documents) <> {counts['documents']}
                THEN RAISE EXCEPTION 'Document count mismatch'; END IF;
            IF (SELECT count(*) FROM preflight.passages) <> {counts['passages']}
                THEN RAISE EXCEPTION 'Passage count mismatch'; END IF;
            IF EXISTS (SELECT 1 FROM preflight.documents d CROSS JOIN LATERAL unnest(d.related_ids) r(related_id)
                       WHERE r.related_id IS NULL OR NOT EXISTS ({known_ids}))
                THEN RAISE EXCEPTION 'Unknown related ID'; END IF;
        END $$;""",
        'COMMIT;',
    ]
    return '\n'.join(parts) + '\n', counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--passages', type=Path, default=ROOT / 'data/documents/passages.jsonl')
    parser.add_argument('--container', help='Name of the running PostgreSQL container (no default)')
    parser.add_argument('--database', help='Target database inside that container (no default)')
    parser.add_argument('--sql-only', action='store_true', help='Print the transaction without connecting')
    args = parser.parse_args()
    if not args.sql_only:
        if args.container is None or args.database is None:
            parser.error('--container and --database are required unless --sql-only is given')
        if not all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', value) for value in (args.container, args.database)):
            parser.error('Invalid container or database name')
    try:
        sql, counts = prepare_load(args.passages)
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
            raise ValueError('PostgreSQL document import failed and rolled back: ' + result.stderr.strip())
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        parser.error(str(error))
    print(json.dumps({'result': 'verified', 'counts': counts, 'transaction': 'committed'}, sort_keys=True))


if __name__ == '__main__':
    main()
