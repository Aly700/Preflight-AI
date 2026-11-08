"""Offline checks of the eval-only transaction; never starts Docker."""

import csv
from datetime import datetime
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class AdversarialOverlay(unittest.TestCase):
    def setUp(self):
        script = ROOT / 'scripts/adversarial_overlay.py'
        self.assertTrue(script.is_file(), 'Overlay script has not been implemented')
        spec = importlib.util.spec_from_file_location('adversarial_overlay', script)
        assert spec is not None and spec.loader is not None
        self.overlay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.overlay)

    def test_single_transaction_guards_duplicates_and_allocates_fresh_events(self):
        sql = self.overlay.prepare_overlay()
        self.assertTrue(sql.startswith('BEGIN;'))
        self.assertTrue(sql.rstrip().endswith('COMMIT;'))
        self.assertEqual(len(re.findall(r'^BEGIN;', sql, re.MULTILINE)), 1)
        self.assertEqual(len(re.findall(r'^COMMIT;', sql, re.MULTILINE)), 1)
        self.assertEqual(
            re.findall(r'INSERT INTO (preflight\.\w+)', sql), ['preflight.issues', 'preflight.activity_events']
        )
        self.assertIn('LOCK TABLE preflight.issues, preflight.activity_events IN SHARE ROW EXCLUSIVE MODE;', sql)
        self.assertIn('IF EXISTS', sql)
        self.assertIn('RAISE EXCEPTION', sql)
        self.assertIn('JOIN overlay_issues USING (issue_id)', sql)
        self.assertLess(sql.index('RAISE EXCEPTION'), sql.index('INSERT INTO preflight.issues'))
        self.assertIn('max(substring(event_id from 5)::numeric)', sql)
        self.assertIn('row_number() OVER (ORDER BY issue_id)', sql)
        self.assertNotIn('ON CONFLICT', sql)
        self.assertNotRegex(sql, r'(?im)^\s*(?:DELETE|UPDATE|TRUNCATE|DROP)\b')

    def test_every_copied_value_obeys_schema_and_only_overlay_issues_are_inserted(self):
        fixture = json.loads((ROOT / 'evals/fixtures/adversarial_overlay.json').read_text())
        sql = self.overlay.prepare_overlay()
        match = re.search(r'COPY overlay_issues \(([^)]+)\).*?;\n(.*?)\n\\\.', sql, re.S)
        assert match is not None
        columns, body = match.groups()
        rows = list(csv.DictReader(io.StringIO(body), fieldnames=columns.split(', ')))
        self.assertEqual({row['issue_id'] for row in rows}, {f'ISSUE-{n:04}' for n in range(901, 907)})
        self.assertEqual(len(rows), 6)
        schema = (ROOT / 'sql/schema.sql').read_text()
        with (ROOT / 'data/synthetic/aircraft.csv').open() as stream:
            aircraft = {row['aircraft_id'] for row in csv.DictReader(stream)}
        for row, source in zip(rows, fixture['issues'], strict=True):
            for field in ('issue_id', 'aircraft_id'):
                check = re.search(rf"{field} text PRIMARY KEY CHECK \({field} ~ '([^']+)'", schema)
                assert check is not None
                self.assertRegex(row[field], check[1])
            self.assertIn(row['aircraft_id'], aircraft)
            for field in ('subsystem', 'priority', 'status'):
                check = re.search(rf'{field} text NOT NULL CHECK \({field} IN \(([^)]+)\)', schema)
                assert check is not None
                self.assertIn(row[field], re.findall(r"'([^']+)'", check[1]))
            self.assertEqual(row['status'], 'open')
            self.assertEqual(row['closed_at'], '')
            self.assertIsNotNone(datetime.fromisoformat(row['opened_at']).tzinfo)
            for field in ('title', 'description', 'note'):
                self.assertEqual(row[field], source[field])
                self.assertTrue(row[field])
            self.assertEqual(row['event_type'], 'issue_opened')
            self.assertIn("'issue_opened'", schema)
        self.assertIn('NULL, NULL, event_type, opened_at, note', sql)  # No task or actor foreign keys invented.

    def test_apply_requires_explicit_target_and_prints_reload_reminder(self):
        with (
            patch('sys.argv', ['adversarial_overlay.py', 'apply']),
            self.assertRaises(SystemExit) as error,
            patch('sys.stderr', new_callable=io.StringIO),
        ):
            self.overlay.main()
        self.assertEqual(error.exception.code, 2)
        with (
            patch('sys.argv', ['adversarial_overlay.py', 'apply', '--container', 'eval-db', '--database', 'eval_db']),
            patch.object(
                self.overlay.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')
            ) as run,
            patch('sys.stdout', new_callable=io.StringIO) as output,
        ):
            self.overlay.main()
        self.assertEqual(
            run.call_args.args[0],
            [
                'docker',
                'exec',
                '-i',
                'eval-db',
                'psql',
                '-X',
                '-q',
                '-v',
                'ON_ERROR_STOP=1',
                '-U',
                'postgres',
                '-d',
                'eval_db',
            ],
        )
        self.assertEqual(run.call_args.kwargs['input'], self.overlay.prepare_overlay())
        self.assertIn('scripts/load_postgres.py', output.getvalue())
        self.assertIn('hosted demo', self.overlay.__doc__ or '')
