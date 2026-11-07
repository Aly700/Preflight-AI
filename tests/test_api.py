"""HTTP contract tests; live PostgreSQL comparisons live in verify_api.py."""

import csv
import importlib
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import psycopg

ROOT = Path(__file__).resolve().parents[1]


class FixtureRepository:
    """Isolate HTTP serialization from network access in the local tests."""

    def healthy(self):
        return True

    def records(self, table):
        with (ROOT / 'data/synthetic' / f'{table}.csv').open(newline='') as handle:
            return [{key: value or None for key, value in row.items()} for row in csv.DictReader(handle)]

    def aircraft(self):
        return self.records('aircraft')

    def issues(self):
        return self.records('issues')


class ApiContract(unittest.TestCase):
    def setUp(self):
        try:
            self.module = importlib.import_module('backend.app')
        except ModuleNotFoundError:
            self.fail('The required FastAPI backend has not been implemented')
        self.app = self.module.create_app(allowed_hosts=['testserver'])
        self.app.dependency_overrides[self.module.get_repository] = FixtureRepository
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def test_health_checks_database(self):
        response = self.client.get('/health')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ok', 'database': 'ok'})

    def test_aircraft_response_preserves_contract_and_utc(self):
        response = self.client.get('/api/aircraft')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['count'], 12)
        self.assertEqual(len(payload['items']), 12)
        self.assertEqual(set(payload['items'][0]), set(FixtureRepository().aircraft()[0]))
        self.assertEqual(
            [r['aircraft_id'] for r in payload['items']], [r['aircraft_id'] for r in FixtureRepository().aircraft()]
        )
        self.assertTrue(all(r['record_created_at'].endswith('Z') for r in payload['items']))

    def test_issues_preserve_open_closed_and_reopened_states(self):
        response = self.client.get('/api/issues')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['count'], 180)
        self.assertEqual(len(payload['items']), 180)
        self.assertEqual(
            {r['status'] for r in payload['items']}, {'open', 'in_progress', 'blocked', 'reopened', 'closed'}
        )
        for row in payload['items']:
            self.assertEqual(row['closed_at'] is not None, row['status'] == 'closed')
            self.assertTrue(row['opened_at'].endswith('Z'))
        self.assertEqual(set(payload['items'][0]), set(FixtureRepository().issues()[0]))

    def test_database_failure_is_503_without_connection_details(self):
        self.app.dependency_overrides.clear()
        with patch('backend.app.open_database', side_effect=psycopg.OperationalError('sensitive connection detail')):
            for path in ('/health', '/api/aircraft', '/api/issues'):
                with self.subTest(path=path):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.json(), {'detail': 'Database unavailable'})
                    self.assertNotIn('sensitive', response.text)

    def test_missing_connection_configuration_fails_closed(self):
        self.app.dependency_overrides.clear()
        with patch.dict('os.environ', {}, clear=True):
            response = self.client.get('/health')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'detail': 'Database unavailable'})

    def test_host_allowlist_is_explicit_configuration(self):
        with patch.dict('os.environ', {'PREFLIGHT_ALLOWED_HOSTS': 'demo.example'}):
            configured = self.module.create_app()
        default = self.module.create_app()
        for app in (configured, default):
            app.dependency_overrides[self.module.get_repository] = FixtureRepository
        with TestClient(configured) as client:
            self.assertEqual(client.get('/health').status_code, 400)  # TestClient sends Host: testserver
            self.assertEqual(client.get('/health', headers={'Host': 'demo.example'}).status_code, 200)
        with TestClient(default) as client:
            self.assertEqual(client.get('/health').status_code, 400)
            self.assertEqual(client.get('/health', headers={'Host': 'localhost'}).status_code, 200)
            self.assertEqual(client.get('/health', headers={'Host': '127.0.0.1:8000'}).status_code, 200)

    def test_mutating_http_methods_are_not_available(self):
        for path in ('/health', '/api/aircraft', '/api/issues'):
            for method in ('post', 'put', 'patch', 'delete'):
                with self.subTest(path=path, method=method):
                    self.assertEqual(getattr(self.client, method)(path).status_code, 405)


if __name__ == '__main__':
    unittest.main()
