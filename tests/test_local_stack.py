"""The local Compose stack's helpers: secrets bootstrap, Compose configuration and the stack check.

Nothing here contacts a database or a model; Docker-dependent checks skip with a
reason when Docker is unavailable, as the built-page test does without a build.
"""

import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SecretsBootstrap(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'secrets'
        self.ensure_secrets = load_script('init_secrets').ensure_secrets

    def test_secrets_are_generated_once_with_private_modes_and_placeholders_for_keys(self):
        first = self.ensure_secrets(self.directory)
        self.assertEqual(first['mcp-token'], 'created')
        self.assertEqual(first['model-api-key'], 'placeholder')
        self.assertEqual(first['copilot-token'], 'placeholder')
        token = (self.directory / 'mcp-token').read_text()
        self.assertRegex(token, r'^[a-f0-9]{64}\n$')
        self.assertRegex((self.directory / 'chat-token').read_text(), r'^[a-f0-9]{64}\n$')
        # Files are world-readable so a container user can read the bind-mounted file on Linux;
        # privacy on the host comes from the 0700 directory.
        for name in ('postgres-password', 'api-password', 'mcp-password', 'chat-token', 'mcp-token', 'model-api-key'):
            self.assertEqual(stat.S_IMODE((self.directory / name).stat().st_mode), 0o644, name)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertEqual((self.directory / 'model-api-key').read_text(), '')
        (self.directory / 'model-api-key').write_text('sk-test\n')
        (self.directory / 'model-api-key').chmod(0o600)  # a key copied in by hand
        second = self.ensure_secrets(self.directory)
        self.assertEqual(second['mcp-token'], 'kept')
        self.assertEqual(second['model-api-key'], 'present')
        self.assertEqual(stat.S_IMODE((self.directory / 'model-api-key').stat().st_mode), 0o644)
        self.assertEqual((self.directory / 'mcp-token').read_text(), token)

    def test_cli_prints_statuses_and_no_values(self):
        out = subprocess.run(
            [sys.executable, '-B', 'scripts/init_secrets.py', '--directory', str(self.directory)],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=60,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        report = json.loads(out.stdout)
        self.assertEqual(report['chat-token'], 'created')
        self.assertNotIn((self.directory / 'chat-token').read_text().strip(), out.stdout)
        self.assertNotIn((self.directory / 'postgres-password').read_text().strip(), out.stdout)


def docker_compose_available() -> bool:
    try:
        return subprocess.run(['docker', 'compose', 'version'], capture_output=True, timeout=30).returncode == 0 and (
            subprocess.run(['docker', 'info'], capture_output=True, timeout=30).returncode == 0
        )
    except OSError, subprocess.TimeoutExpired:
        return False


class ComposeConfiguration(unittest.TestCase):
    def setUp(self):
        self.ensure_secrets = load_script('init_secrets').ensure_secrets

    def test_tracing_is_opt_in_with_only_jaeger_ui_published_on_loopback(self):
        text = (ROOT / 'compose.yaml').read_text()
        api = text.split('\n  preflight-oss-api:\n', 1)[1].split('\n  preflight-oss-web:\n', 1)[0]
        self.assertIn('OTEL_EXPORTER_OTLP_ENDPOINT: ${OTEL_EXPORTER_OTLP_ENDPOINT:-}', api)
        self.assertIn('OTEL_SERVICE_NAME: preflight-oss-api', api)
        self.assertIn('\n  preflight-oss-jaeger:\n', text)
        jaeger = text.split('\n  preflight-oss-jaeger:\n', 1)[1].split('\nsecrets:', 1)[0]
        self.assertIn('container_name: preflight-oss-jaeger', jaeger)
        self.assertIn("profiles: ['observability']", jaeger)
        self.assertIn(
            'jaegertracing/jaeger:2.22.0@sha256:836b967b5abcf1f7758066aa6b348619267a2f1459f72d472840570f9bd88a17',
            jaeger,
        )
        self.assertIn('"127.0.0.1:16686:16686"', jaeger)
        self.assertNotIn('4318:', jaeger)
        self.assertNotIn('OTEL_', (ROOT / 'deploy/compose.host.yaml').read_text())

    def test_api_passes_gateway_configuration_and_keeps_mcp_token_secret(self):
        text = (ROOT / 'compose.yaml').read_text()
        api = text.split('\n  preflight-oss-api:\n', 1)[1].split('\n  preflight-oss-web:\n', 1)[0]
        for setting in (
            'MCP_AUTH: ${MCP_AUTH:-bearer}',
            'MCP_TOOL_PREFIX: ${MCP_TOOL_PREFIX:-}',
            'PREFLIGHT_MCP_URL: ${PREFLIGHT_MCP_URL:-http://preflight-oss-mcp:8001/mcp}',
        ):
            with self.subTest(setting=setting):
                self.assertIn(setting, api)
        self.assertIn('MCP_TOKEN_FILE: /run/secrets/mcp-token', api)
        self.assertIn('      - mcp-token\n', api.split('    secrets:\n', 1)[1])

    def test_postgres_healthcheck_uses_tcp(self):
        text = (ROOT / 'compose.yaml').read_text()
        self.assertIn('pg_isready -h 127.0.0.1 -U postgres -d preflight_oss', text)

    def test_ci_waits_with_timeout_logs_failures_and_builds_web(self):
        text = (ROOT / '.github/workflows/ci.yml').read_text()
        compose, containers = text.split('\n  compose:\n', 1)[1].split('\n  containers:\n', 1)
        with self.subTest(check='wait timeout'):
            self.assertIn('- run: docker compose up -d --wait --wait-timeout 180', compose)
        with self.subTest(check='failure logs before teardown'):
            self.assertIn('- run: docker compose logs --no-color\n        if: failure()\n', compose)
            self.assertLess(compose.index('docker compose logs --no-color'), compose.index('docker compose down -v'))
        with self.subTest(check='web image for each platform'):
            self.assertIn('arch: [amd64, arm64]', containers)
            self.assertIn('- run: docker build --platform linux/${{ matrix.arch }} frontend', containers)
        with self.subTest(check='API image layers cached between runs'):
            self.assertIn('cache-from: type=gha,scope=backend-${{ matrix.arch }}', containers)
            self.assertIn('- run: docker compose -f compose.yaml -f .github/compose.ci.yaml build', compose)

    def test_roles_script_uses_psql_variables_not_string_interpolation(self):
        script = ROOT / 'compose/postgres-roles.sh'
        text = script.read_text()
        self.assertIn('set -eu', text)
        self.assertIn("PASSWORD :'", text)
        self.assertNotIn("PASSWORD '$", text)
        self.assertIn("<<'SQL'", text)
        for role in ('api', 'mcp'):
            with self.subTest(role=role, check='no password argv'):
                self.assertNotIn(f'-v {role}_password=', text)
            with self.subTest(role=role, check='read inside psql'):
                self.assertIn(r'\set ' + f'{role}_password `cat /run/secrets/{role}-password`', text)
            with self.subTest(role=role, check='SQL binding'):
                self.assertIn(f"PASSWORD :'{role}_password'", text)
        self.assertIn('preflight_reader', text)
        self.assertTrue(script.stat().st_mode & stat.S_IXUSR, 'roles script must be executable')

    def test_host_runtime_defaults_to_native_and_can_be_switched(self):
        if not docker_compose_available():
            self.skipTest('docker is not available')
        base = {key: value for key, value in os.environ.items() if key != 'CHAT_RUNTIME'}
        for extra, expected in (({}, 'native'), ({'CHAT_RUNTIME': 'langgraph'}, 'langgraph')):
            rendered = subprocess.run(
                [
                    *('docker', 'compose', '--env-file', os.devnull, '-f', str(ROOT / 'compose.yaml')),
                    *('-f', str(ROOT / 'deploy/compose.host.yaml'), 'config', '--format', 'json'),
                ],
                capture_output=True,
                text=True,
                check=True,
                cwd=ROOT,
                env={**base, **extra, 'COMPOSE_PROFILES': ''},
                timeout=120,
            ).stdout
            with self.subTest(runtime=expected):
                api = json.loads(rendered)['services']['preflight-oss-api']
                self.assertEqual(api['environment']['CHAT_RUNTIME'], expected)

    def test_host_compose_config_publishes_the_api_and_mcp_on_loopback_only(self):
        # These prerequisites can be checked without Docker or any secret files.
        self.assertIn('AWS_REGION: ${AWS_REGION:-}', (ROOT / 'compose.yaml').read_text())
        override = ROOT / 'deploy/compose.host.yaml'
        self.assertTrue(override.is_file(), 'deploy/compose.host.yaml must exist')
        if not docker_compose_available():
            self.skipTest('docker is not available')
        rendered = subprocess.run(
            [
                'docker',
                'compose',
                '--env-file',
                os.devnull,
                '-f',
                str(ROOT / 'compose.yaml'),
                '-f',
                str(override),
                'config',
                '--format',
                'json',
            ],
            capture_output=True,
            text=True,
            check=True,
            cwd=ROOT,
            env={**os.environ, 'COMPOSE_PROFILES': ''},
            timeout=120,
        ).stdout
        services = json.loads(rendered)['services']
        self.assertEqual(set(services), {'preflight-oss-postgres', 'preflight-oss-mcp', 'preflight-oss-api'})
        published = {name: service['ports'] for name, service in services.items() if service.get('ports')}
        # The database is never published; the MCP port is fronted by a private TLS listener for AgentCore Gateway.
        bindings = {
            name: [(port['host_ip'], str(port['published']), port['target']) for port in ports]
            for name, ports in published.items()
        }
        self.assertEqual(
            bindings,
            {'preflight-oss-api': [('127.0.0.1', '8100', 8000)], 'preflight-oss-mcp': [('127.0.0.1', '8101', 8001)]},
        )
        for name, service in services.items():
            self.assertEqual(service['container_name'], name)
        environment = services['preflight-oss-api']['environment']
        self.assertEqual(environment['MODEL_PROVIDER'], 'bedrock')
        self.assertEqual(environment['CHAT_RUNTIME'], 'native')
        self.assertTrue({'127.0.0.1', 'localhost'}.issubset(environment['PREFLIGHT_ALLOWED_HOSTS'].split(',')))

    @unittest.skipUnless(docker_compose_available(), 'docker is not available')
    def test_compose_config_publishes_only_the_web_entry_point_and_uses_explicit_names(self):
        self.ensure_secrets(ROOT / '.secrets')
        rendered = subprocess.run(
            ['docker', 'compose', '-f', str(ROOT / 'compose.yaml'), 'config', '--format', 'json'],
            capture_output=True,
            text=True,
            check=True,
            cwd=ROOT,
            timeout=120,
        ).stdout
        config = json.loads(rendered)
        services = config['services']
        self.assertEqual(
            set(services), {'preflight-oss-postgres', 'preflight-oss-mcp', 'preflight-oss-api', 'preflight-oss-web'}
        )
        published = {name: s['ports'] for name, s in services.items() if s.get('ports')}
        self.assertEqual(list(published), ['preflight-oss-web'])
        self.assertIn('-h 127.0.0.1', ' '.join(services['preflight-oss-postgres']['healthcheck']['test']))
        port = published['preflight-oss-web'][0]
        self.assertEqual((port['host_ip'], str(port['published']), port['target']), ('127.0.0.1', '8080', 8080))
        for name, service in services.items():
            self.assertEqual(service['container_name'], name)
        self.assertIn('preflight-oss-mcp:*', services['preflight-oss-mcp']['environment']['MCP_ALLOWED_HOSTS'])
        self.assertEqual(
            services['preflight-oss-api']['environment']['PREFLIGHT_MCP_URL'], 'http://preflight-oss-mcp:8001/mcp'
        )
        self.assertNotIn('preflight-api', rendered)
        self.assertNotIn('preflight-mcp:', rendered)
        self.assertNotIn('preflight-postgres', rendered)
        self.assertEqual(
            sorted(config['secrets']),
            sorted(
                [
                    'postgres-password',
                    'api-password',
                    'mcp-password',
                    'chat-token',
                    'mcp-token',
                    'copilot-token',
                    'model-api-key',
                ]
            ),
        )


FACTS: dict[str, Any] = {
    'counts': {
        'aircraft': 12,
        'issues': 180,
        'tasks': 480,
        'activity_events': 920,
        'teams': 6,
        'people': 36,
        'shifts': 150,
    },
    'unresolved_total': 162,
    'first_unresolved': ['ISSUE-0001', 'ISSUE-0003', 'ISSUE-0004', 'ISSUE-0006', 'ISSUE-0007'],
    'aircraft_0001': {
        'issue_count': 15,
        'unresolved_issue_count': 12,
        'issue_status_counts': {'open': 7, 'in_progress': 3, 'blocked': 1, 'reopened': 1, 'closed': 3},
        'task_count': 38,
        'task_status_counts': {'todo': 23, 'in_progress': 3, 'awaiting_review': 2, 'completed': 9, 'blocked': 1},
    },
    'issue_0001_events': 7,
    'unresolved_by_subsystem': {
        'avionics': 30,
        'cabin': 25,
        'controls': 28,
        'electrical': 26,
        'hydraulics': 27,
        'structures': 26,
    },
    'aircraft_0002': {
        'issue_count': 15,
        'unresolved_issue_count': 12,
        'issue_status_counts': {'open': 2, 'in_progress': 7, 'blocked': 0, 'reopened': 3, 'closed': 3},
        'task_count': 40,
        'task_status_counts': {'todo': 20, 'in_progress': 5, 'awaiting_review': 3, 'completed': 10, 'blocked': 2},
    },
    'closed_total': 18,
    'first_closed': ['ISSUE-0002', 'ISSUE-0005', 'ISSUE-0008', 'ISSUE-0010', 'ISSUE-0011'],
}
COUNTS: dict[str, Any] = {
    'group_by': 'subsystem',
    'unresolved_only': True,
    'filters': {'aircraft_id': None, 'status': None, 'subsystem': None, 'priority': None},
    'total': 162,
    'groups': [{'key': k, 'count': c} for k, c in sorted(FACTS['unresolved_by_subsystem'].items())],
}
COMPARISON: dict[str, Any] = {
    'summaries': [
        {'aircraft': {'aircraft_id': 'DEMO-AIR-0001'}, **FACTS['aircraft_0001']},
        {'aircraft': {'aircraft_id': 'DEMO-AIR-0002'}, **FACTS['aircraft_0002']},
    ],
    'ranked_by_unresolved': [
        {'aircraft_id': 'DEMO-AIR-0001', 'unresolved_issue_count': 12, 'issue_count': 15},
        {'aircraft_id': 'DEMO-AIR-0002', 'unresolved_issue_count': 12, 'issue_count': 15},
    ],
    'unresolved_spread': 0,
}
CLOSED: dict[str, Any] = {
    'total': 18,
    'limit': 5,
    'offset': 0,
    'items': [{'issue_id': i, 'status': 'closed'} for i in FACTS['first_closed']],
}
PAGE = {
    'total': 162,
    'limit': 5,
    'offset': 0,
    'items': [{'issue_id': i, 'status': 'open'} for i in FACTS['first_unresolved']],
}
SUMMARY = {'aircraft': {'aircraft_id': 'DEMO-AIR-0001'}, **FACTS['aircraft_0001']}
HISTORY = {'issue': {'issue_id': 'ISSUE-0001'}, 'total_events': 7, 'limit': 1, 'offset': 0, 'events': []}


class StackCheck(unittest.TestCase):
    def setUp(self):
        self.check = load_script('compose_check')

    def test_service_health_accepts_json_lines_and_arrays(self):
        entries = [
            {'Service': 'preflight-oss-postgres', 'Health': 'healthy', 'State': 'running'},
            {'Service': 'preflight-oss-api', 'Health': '', 'State': 'running'},
            {'Service': 'preflight-oss-mcp', 'State': 'exited'},
            {'Service': 'preflight-oss-web'},
        ]
        expected = {
            'preflight-oss-postgres': 'healthy',
            'preflight-oss-api': 'running',
            'preflight-oss-mcp': 'exited',
            'preflight-oss-web': 'unknown',
        }
        outputs = {
            'json lines': '\n' + '\n\n'.join(json.dumps(entry) for entry in entries) + '\n',
            'compact array': json.dumps(entries),
            'formatted array': '\n' + json.dumps(entries, indent=2) + '\n',
        }
        for shape, output in outputs.items():
            with self.subTest(shape=shape), patch.object(self.check, 'run', return_value=output) as run:
                self.assertEqual(self.check.service_health('test-project'), expected)
                run.assert_called_once_with(
                    ['docker', 'compose', '-p', 'test-project', 'ps', '--all', '--format', 'json']
                )

    def test_compare_evidence_flags_a_total_mismatch_and_accepts_a_match(self):
        events = [
            ('status', {}),
            ('evidence', {'results': [{'tool': 'list_open_issues', 'result': PAGE}]}),
            ('done', {}),
        ]
        self.assertEqual(self.check.compare_evidence(events, FACTS), [])
        wrong = [('evidence', {'results': [{'tool': 'list_open_issues', 'result': {**PAGE, 'total': 161}}]})]
        mismatches = self.check.compare_evidence(wrong, FACTS)
        self.assertEqual(len(mismatches), 1)
        self.assertIn('list_open_issues', mismatches[0])

    def test_an_error_event_fails_the_check(self):
        events = [('status', {'message': 'Checking'}), ('error', {'message': 'x'})]
        self.assertEqual(self.check.compare_evidence(events, FACTS), ['answer ended with error: x'])

    def test_compare_tools_checks_all_three_tools(self):
        results = {
            'get_aircraft_summary': SUMMARY,
            'list_open_issues': PAGE,
            'get_issue_history': HISTORY,
            'count_issues': COUNTS,
            'compare_aircraft': COMPARISON,
            'search_issues': CLOSED,
        }
        self.assertEqual(self.check.compare_tools(results, FACTS), [])
        wrong = {**results, 'get_issue_history': {**HISTORY, 'total_events': 6}}
        mismatches = self.check.compare_tools(wrong, FACTS)
        self.assertEqual(len(mismatches), 1)
        self.assertIn('get_issue_history', mismatches[0])
        missing = self.check.compare_tools({'list_open_issues': PAGE}, FACTS)
        self.assertEqual(len(missing), 5)

    def test_compare_tools_checks_the_three_new_tools(self):
        results = {
            'get_aircraft_summary': SUMMARY,
            'list_open_issues': PAGE,
            'get_issue_history': HISTORY,
            'count_issues': COUNTS,
            'compare_aircraft': COMPARISON,
            'search_issues': CLOSED,
        }
        self.assertEqual(self.check.compare_tools(results, FACTS), [])
        wrong_count = {**COUNTS, 'groups': [{'key': 'avionics', 'count': 1}, *COUNTS['groups'][1:]]}
        mismatches = self.check.compare_tools({**results, 'count_issues': wrong_count}, FACTS)
        self.assertEqual(len(mismatches), 1)
        self.assertIn('count_issues', mismatches[0])
        wrong_spread = {**COMPARISON, 'unresolved_spread': 3}
        mismatches = self.check.compare_tools({**results, 'compare_aircraft': wrong_spread}, FACTS)
        self.assertEqual(len(mismatches), 1)
        self.assertIn('compare_aircraft', mismatches[0])
        open_item = {**CLOSED, 'items': [{'issue_id': 'ISSUE-0001', 'status': 'open'}]}
        mismatches = self.check.compare_tools({**results, 'search_issues': open_item}, FACTS)
        self.assertEqual(len(mismatches), 1)
        self.assertIn('search_issues', mismatches[0])

    def test_compare_tools_rejects_empty_aircraft_ranking(self):
        results = {
            'get_aircraft_summary': SUMMARY,
            'list_open_issues': PAGE,
            'get_issue_history': HISTORY,
            'count_issues': COUNTS,
            'compare_aircraft': {**COMPARISON, 'ranked_by_unresolved': []},
            'search_issues': CLOSED,
        }
        mismatches = self.check.compare_tools(results, FACTS)
        self.assertEqual(len(mismatches), 1)
        self.assertIn('compare_aircraft', mismatches[0])

    def test_compare_tools_rejects_swapped_aircraft_summary_ids(self):
        swapped = [
            {**COMPARISON['summaries'][0], 'aircraft': {'aircraft_id': 'DEMO-AIR-0002'}},
            {**COMPARISON['summaries'][1], 'aircraft': {'aircraft_id': 'DEMO-AIR-0001'}},
        ]
        results = {
            'get_aircraft_summary': SUMMARY,
            'list_open_issues': PAGE,
            'get_issue_history': HISTORY,
            'count_issues': COUNTS,
            'compare_aircraft': {**COMPARISON, 'summaries': swapped},
            'search_issues': CLOSED,
        }
        mismatches = self.check.compare_tools(results, FACTS)
        self.assertEqual(len(mismatches), 1)
        self.assertIn('compare_aircraft', mismatches[0])

    def test_compare_tools_rejects_empty_search_page(self):
        results = {
            'get_aircraft_summary': SUMMARY,
            'list_open_issues': PAGE,
            'get_issue_history': HISTORY,
            'count_issues': COUNTS,
            'compare_aircraft': COMPARISON,
            'search_issues': {**CLOSED, 'items': []},
        }
        mismatches = self.check.compare_tools(results, FACTS)
        self.assertEqual(len(mismatches), 1)
        self.assertIn('search_issues', mismatches[0])

    def test_compare_tools_rejects_wrong_first_search_id(self):
        wrong_items = [{**CLOSED['items'][0], 'issue_id': 'ISSUE-9999'}, *CLOSED['items'][1:]]
        results = {
            'get_aircraft_summary': SUMMARY,
            'list_open_issues': PAGE,
            'get_issue_history': HISTORY,
            'count_issues': COUNTS,
            'compare_aircraft': COMPARISON,
            'search_issues': {**CLOSED, 'items': wrong_items},
        }
        mismatches = self.check.compare_tools(results, FACTS)
        self.assertEqual(len(mismatches), 1)
        self.assertIn('search_issues', mismatches[0])

    def test_sse_parser_reads_frames_until_done(self):
        raw = (
            b'event: status\ndata: {"message": "Checking"}\n\n'
            b'event: delta\ndata: {"text": "162 unresolved"}\n\n'
            b'event: done\ndata: {"tool_calls": 1}\n\n'
            b'event: delta\ndata: {"text": "ignored after done"}\n\n'
        )
        events = list(self.check.parse_sse(io.BytesIO(raw)))
        self.assertEqual([name for name, _ in events], ['status', 'delta', 'done'])
        self.assertEqual(events[1][1], {'text': '162 unresolved'})

    def test_answer_mentions_total(self):
        events = [('delta', {'text': 'There are 16'}), ('delta', {'text': '2 unresolved issues.'}), ('done', {})]
        self.assertTrue(self.check.answer_mentions(events, FACTS))
        self.assertFalse(self.check.answer_mentions([('delta', {'text': 'many'})], FACTS))


if __name__ == '__main__':
    unittest.main()
