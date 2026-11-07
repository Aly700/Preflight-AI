"""Preflight checks must reject damaged fixtures before contacting PostgreSQL."""

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PostgresImportPreflight(unittest.TestCase):
    def setUp(self):
        script = ROOT / 'scripts/load_postgres.py'
        self.assertTrue(script.is_file(), 'PostgreSQL loader has not been implemented')
        spec = importlib.util.spec_from_file_location('load_postgres', script)
        assert spec is not None and spec.loader is not None
        self.loader = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.loader)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / 'synthetic'
        shutil.copytree(ROOT / 'data/synthetic', self.data)

    def test_valid_fixture_can_be_prepared_without_database_access(self):
        sql, counts = self.loader.prepare_load(self.data)
        self.assertEqual(sum(counts.values()), 1784)
        self.assertTrue(sql.startswith('BEGIN;'))
        self.assertTrue(sql.rstrip().endswith('COMMIT;'))

    def test_target_container_and_database_must_be_explicit(self):
        script = ROOT / 'scripts/load_postgres.py'
        result = subprocess.run(
            [sys.executable, '-B', str(script)], capture_output=True, text=True, timeout=60, cwd=ROOT
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn('--container and --database are required', result.stderr)
        self.assertNotIn('docker', result.stderr)
        preview = subprocess.run(
            [sys.executable, '-B', str(script), '--sql-only', '--data', str(self.data)],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=ROOT,
        )
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertTrue(preview.stdout.startswith('BEGIN;'))

    def test_modified_csv_is_rejected_before_loading(self):
        path = self.data / 'issues.csv'
        path.write_bytes(path.read_bytes().replace(b'Fictional', b'Altered', 1))
        with self.assertRaisesRegex(ValueError, 'hash'):
            self.loader.prepare_load(self.data)

    def test_missing_manifest_file_entry_is_rejected(self):
        path = self.data / 'manifest.json'
        manifest = json.loads(path.read_text())
        del manifest['files']['tasks.csv']
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'manifest'):
            self.loader.prepare_load(self.data)

    def test_truncated_csv_is_rejected_even_with_updated_hash(self):
        path = self.data / 'tasks.csv'
        path.write_bytes(b'\n'.join(path.read_bytes().split(b'\n')[:-2]) + b'\n')
        manifest_path = self.data / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['files']['tasks.csv']['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'count'):
            self.loader.prepare_load(self.data)


if __name__ == '__main__':
    unittest.main()
