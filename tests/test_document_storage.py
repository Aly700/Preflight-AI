"""SQL generation and image contracts without Docker or database access."""

import importlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
HASHES = (
    '828e1496d7fabb79cfa4dcd84fa38625c0d3d21da474a00f08db0f559940cf35',
    'd241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66',
    '094f8e891b932f2000c92cfc663bac4c62069f5d8af5b5278c4306aef3084750',
)


class DocumentSQLTests(unittest.TestCase):
    def test_schema_constraints_indexes_and_read_only_grants(self):
        path = ROOT / 'sql/documents.sql'
        self.assertTrue(path.is_file(), 'Implement document storage DDL')
        sql = re.sub(r'\s+', ' ', path.read_text())
        for fragment in (
            'CREATE EXTENSION IF NOT EXISTS vector;',
            'CREATE TABLE IF NOT EXISTS preflight.documents',
            'CREATE TABLE IF NOT EXISTS preflight.passages',
            "doc_id ~ '^DOC-[0-9]{4}$'",
            "passage_id ~ '^DOC-[0-9]{4}#[0-9]+$'",
            'REFERENCES preflight.people',
            'REFERENCES preflight.teams',
            'REFERENCES preflight.documents',
            'related_ids text[] NOT NULL',
            'ordinal > 0',
            'UNIQUE (doc_id, ordinal)',
            "tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED",
            'embedding vector(384) NOT NULL',
            'USING gin (text_search)',
            'USING hnsw (embedding vector_cosine_ops)',
            'GRANT SELECT ON preflight.documents, preflight.passages TO preflight_reader;',
        ):
            self.assertIn(fragment, sql)
        self.assertEqual(sql.count('CREATE INDEX IF NOT EXISTS'), 2)
        for kind in ('policy', 'glossary', 'handover', 'memo', 'coordination', 'retrospective', 'form'):
            self.assertIn(f"'{kind}'", sql)
        self.assertIn("CHECK (station IN ('DEMO-STATION-01', 'DEMO-STATION-02', 'DEMO-STATION-03'))", sql)
        self.assertIn("CHECK (program IN ('DEMO-PROGRAM-ALPHA', 'DEMO-PROGRAM-BETA'))", sql)

    def test_compose_image_model_directory_and_ci_load_order(self):
        compose = (ROOT / 'compose.yaml').read_text()
        postgres = compose.split('\n  preflight-oss-postgres:\n')[1].split('\n  preflight-oss-mcp:\n')[0]
        # The same PostgreSQL image and operating system as before, so the data volume's collation is unchanged;
        # pgvector comes from the PostgreSQL project's signed Debian package.
        self.assertIn('context: ./compose/postgres', postgres)
        dockerfile = (ROOT / 'compose/postgres/Dockerfile').read_text()
        self.assertIn(
            'FROM postgres:17@sha256:ff87138949ad9a46f32ff23bd6852b52ce024a13dffe9b4087be98292cdd3b4f', dockerfile
        )
        self.assertIn('postgresql-17-pgvector=0.8.6-1.pgdg13+2', dockerfile)
        self.assertNotIn('pgvector/pgvector', compose)
        mcp = compose.split('\n  preflight-oss-mcp:\n')[1].split('\n  preflight-oss-api:\n')[0]
        self.assertIn('PREFLIGHT_EMBEDDING_MODEL_DIR: /opt/models/bge-small-en-v1.5', mcp)
        ci = (ROOT / '.github/workflows/ci.yml').read_text().split('\n  compose:\n')[1]
        self.assertLess(ci.index('scripts/load_postgres.py'), ci.index('scripts/load_documents.py'))
        self.assertLess(ci.index('scripts/load_documents.py'), ci.index('scripts/compose_check.py'))

    def test_model_download_is_revision_pinned_retried_verified_and_readable(self):
        dockerfile = (ROOT / 'mcp_service/Dockerfile').read_text()
        self.assertIn(
            'python:3.14-slim@sha256:930e1abba7c4e8f96ed93e8012c35d174d7732752731bbd5e8e757d7ec6f3966', dockerfile
        )
        self.assertIn('https://huggingface.co/BAAI/bge-small-en-v1.5/resolve/', dockerfile)
        self.assertIn('5c38ec7c405ec4b44b94cc5a9bb96e735b38267a', dockerfile)
        for sha256 in HASHES:
            self.assertIn(sha256, dockerfile)
        self.assertIn('for attempt in 1 2 3 4', dockerfile)
        self.assertIn('sleep $((attempt * 20))', dockerfile)
        self.assertIn('sha256sum -c', dockerfile)
        self.assertIn('chmod -R a+rX /opt/models/bge-small-en-v1.5', dockerfile)
        self.assertLess(dockerfile.index('sha256sum -c'), dockerfile.index('USER 10001:10001'))


class DocumentLoadTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / 'scripts/load_documents.py').is_file(), 'Implement the document loader')
        self.loader = importlib.import_module('scripts.load_documents')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'passages.jsonl'
        self.row = {
            'passage_id': 'DOC-0001#1',
            'doc_id': 'DOC-0001',
            'ordinal': 1,
            'kind': 'memo',
            'title': "Clerk's note",
            'date': '2026-10-01',
            'station': None,
            'program': None,
            'author': 'PERSON-0001',
            'team': 'TEAM-0001',
            'related_ids': ['ISSUE-0001', 'TASK-0001'],
            'text': "Clerk's note\n\\.\nnext, line",
            'embedding': [1.0] + [0.0] * 383,
        }

    def write_rows(self, rows):
        self.path.write_text(''.join(json.dumps(row) + '\n' for row in rows))

    def test_transaction_guards_replacement_vectors_related_ids_and_counts(self):
        self.write_rows([self.row, {**self.row, 'ordinal': 2, 'passage_id': 'DOC-0001#2'}])
        sql, counts = self.loader.prepare_load(self.path)
        self.assertEqual(counts, {'documents': 1, 'passages': 2})
        self.assertTrue(sql.startswith('BEGIN;'))
        self.assertTrue(sql.rstrip().endswith('COMMIT;'))
        self.assertEqual(len(re.findall(r'^BEGIN;', sql, re.MULTILINE)), 1)
        self.assertEqual(len(re.findall(r'^COMMIT;', sql, re.MULTILINE)), 1)
        self.assertIn('Base tables must be loaded', sql)
        self.assertLess(sql.index('Base tables must be loaded'), sql.index('DELETE FROM preflight.passages'))
        self.assertLess(sql.index('DELETE FROM preflight.passages'), sql.index('DELETE FROM preflight.documents'))
        self.assertIn('[' + ','.join(['1.0'] + ['0.0'] * 383) + ']', sql)
        self.assertIn('unnest(d.related_ids)', sql)
        for table in ('aircraft', 'issues', 'tasks', 'activity_events', 'teams', 'people', 'shifts'):
            self.assertIn(f"to_regclass('preflight.{table}')", sql)
            self.assertIn(f'FROM preflight.{table}', sql)
        self.assertIn('Document count mismatch', sql)
        self.assertIn('Passage count mismatch', sql)
        self.assertIn('Unknown related ID', sql)
        self.assertNotIn('\n\\.\nnext', sql)  # no COPY terminator injection

    def test_invalid_vectors_duplicate_ids_and_inconsistent_metadata_are_rejected(self):
        for rows in (
            [],
            [self.row, self.row],
            [{**self.row, 'embedding': [0.0] * 383}],
            [{**self.row, 'embedding': [float('nan')] * 384}],
            [{**self.row, 'passage_id': 'DOC-0002#1'}],
            [{**self.row, 'ordinal': 0}],
            [self.row, {**self.row, 'ordinal': 2, 'passage_id': 'DOC-0001#2', 'author': 'PERSON-0002'}],
        ):
            with self.subTest(rows=len(rows)):
                self.write_rows(rows)
                with self.assertRaises(ValueError):
                    self.loader.prepare_load(self.path)

    def test_cli_requires_explicit_target_and_sql_only_does_not_invoke_docker(self):
        self.write_rows([self.row])
        with patch.object(sys, 'argv', ['load_documents.py', '--passages', str(self.path)]):
            with patch.object(self.loader.subprocess, 'run') as run, self.assertRaises(SystemExit) as error:
                self.loader.main()
            self.assertEqual(error.exception.code, 2)
            run.assert_not_called()
        result = subprocess.run(
            [sys.executable, '-B', 'scripts/load_documents.py', '--passages', str(self.path), '--sql-only'],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith('BEGIN;'))

    def test_compose_check_compares_documents_and_passages_individually(self):
        self.write_rows([self.row])
        check = importlib.import_module('scripts.compose_check')
        self.assertTrue(hasattr(check, 'compare_document_counts'), 'Check passage fixture counts in compose_check')
        self.assertEqual(check.compare_document_counts({'documents': 1, 'passages': 1}, self.path), [])
        self.assertEqual(len(check.compare_document_counts({'documents': 2, 'passages': 0}, self.path)), 2)
        self.assertIn("'document_counts'", check.FACTS_SQL)
        self.assertIn('FROM preflight.passages', check.FACTS_SQL)
