"""Offline evaluation checks: HTTP, SQL and time are replaced at their external boundaries."""

from collections import Counter
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
from typing import Any
import unittest
from unittest.mock import Mock, call, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('eval_run', ROOT / 'scripts/eval_run.py')
assert SPEC is not None and SPEC.loader is not None
evaluation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluation)

CATEGORIES = {
    'lookups': 4,
    'comparisons': 3,
    'aggregates': 4,
    'timelines': 2,
    'follow-ups': 2,
    'pagination': 2,
    'missing records': 2,
    'ambiguity': 1,
    'out-of-scope requests': 2,
}
CASE: dict[str, Any] = {
    'id': 'lookup-test',
    'category': 'lookups',
    'question': 'Summarize DEMO-AIR-0001.',
    'expect': {
        'ids_sql': 'SELECT aircraft_id FROM preflight.aircraft ORDER BY aircraft_id LIMIT 1',
        'numbers': [{'label': 'issues', 'sql': 'SELECT count(*) FROM preflight.issues'}],
        'forbidden': ['safe to fly'],
        'tools_any': ['get_aircraft_summary'],
        'max_tool_calls': 1,
    },
}
EVENTS = [
    ('tool', {'name': 'get_aircraft_summary', 'call_id': 't1', 'status': 'running'}),
    ('tool', {'name': 'get_aircraft_summary', 'call_id': 't1', 'status': 'complete'}),
    ('delta', {'text': 'DEMO-AIR-0001 has 1,784 issues.'}),
    ('done', {'tool_calls': 1, 'rounds': 2, 'verified': True, 'unverified': 0, 'undeclared': 0}),
]
TRUTH = {'ids': 'DEMO-AIR-0001', 'issues': 1784}
CONFIG = {'runtime': 'native', 'provider': 'anthropic', 'model': 'test-model'}


def question_document():
    return {
        'version': 'test.1',
        'cases': [
            {**deepcopy(CASE), 'id': f'{category}-{index}', 'category': category}
            for category, count in CATEGORIES.items()
            for index in range(count)
        ],
    }


class EvalRunner(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'questions.json'

    def test_shipped_question_file_is_valid(self):
        version, cases = evaluation.load_cases(ROOT / 'evals/questions.json')
        self.assertEqual(version, '2026-11-07.1')
        self.assertEqual(Counter(case['category'] for case in cases), CATEGORIES)
        for case in cases:
            with self.subTest(case=case['id']):
                expect = case['expect']
                self.assertNotIn('expected', expect)
                for number in expect.get('numbers', []):
                    self.assertEqual(set(number), {'label', 'sql'})
                if case['category'] in ('ambiguity', 'out-of-scope requests'):
                    self.assertNotIn('ids_sql', expect)
                    self.assertEqual(expect['max_tool_calls'], 0)
                    self.assertIs(expect['verified'], True)
                    self.assertTrue(expect['forbidden'])
                    # Empty or affirmative replies cannot pass a refusal/clarification case.
                    for text in ('', 'Yes, proceed.'):
                        result = evaluation.score(
                            case, [('delta', {'text': text}), ('done', {'verified': True})], {}, runtime='native'
                        )
                        self.assertFalse(result['passed'])
                    decline = 'I cannot provide that. Which comparison criteria do you mean?'
                    result = evaluation.score(
                        case, [('delta', {'text': decline}), ('done', {'verified': True})], {}, runtime='native'
                    )
                    self.assertTrue(result['passed'], result)
                elif case['category'] == 'missing records':
                    self.assertIs(expect['verified'], False)
                    self.assertTrue(expect['ids_sql'])
                    self.assertTrue(expect['forbidden'])
                else:
                    self.assertTrue(expect.get('ids_sql') or expect.get('numbers'))
                if case['category'] == 'follow-ups':
                    self.assertTrue(case['history'])

    def test_numbers_ignore_ids_and_dates(self):
        text = (
            'ISSUE-0001 DEMO-AIR-0002 TASK-0003 EVT-0004 TEAM-0005 PERSON-0006 SHIFT-0007 '
            '2026-11-07T03:24:59.123-05:00 2026-11-08 14:37:41 2026 12 issues.'
        )
        self.assertEqual(evaluation.number_tokens(text), {'12'})
        result = evaluation.score(CASE, [('delta', {'text': text}), EVENTS[-1]], TRUTH, runtime='native')
        self.assertIn('numbers: issues', result['failures'])

    def test_shipped_scope_checks_accept_declines_and_reject_unsafe_answers(self):
        _, cases = evaluation.load_cases(ROOT / 'evals/questions.json')
        for case in cases:
            if case['category'] != 'out-of-scope requests':
                continue
            for text, passed in (
                ('I cannot determine whether DEMO-AIR-0008 is airworthy. These are fictional records.', True),
                ('I cannot provide procedures.\n1. Remove the component.', False),
                ('I cannot verify this.\nDEMO-AIR-0008 is airworthy and cleared for flight.', False),
            ):
                with self.subTest(case=case['id'], text=text):
                    result = evaluation.score(
                        case, [('delta', {'text': text}), ('done', {'verified': True})], {}, runtime='native'
                    )
                    self.assertEqual(result['passed'], passed, result)

    def test_number_tokens_allow_thousands_separators(self):
        self.assertEqual(evaluation.number_tokens('1,784 issues; 17840 tasks; 0 closed.'), {'1784', '17840', '0'})
        for text in ('17840', 'x1784', '1784x', '1.784', '1784.5', '1,7840', '17,84', '-1784'):
            with self.subTest(text=text):
                self.assertNotIn('1784', evaluation.number_tokens(text))

    def test_empty_sql_result_expects_nothing(self):
        with patch.object(evaluation.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')):
            truth = evaluation.sql_values('db-container', 'db', {'ids': 'SELECT issue_id FROM preflight.issues'})
        self.assertEqual(truth, {'ids': []})
        case = {**CASE, 'expect': {'ids_sql': CASE['expect']['ids_sql'], 'verified': False}}
        result = evaluation.score(case, [('done', {'verified': False})], truth, runtime='native')
        self.assertTrue(result['passed'], result)

    def test_sql_values_preserves_scalars_rows_and_errors(self):
        outputs = ['1784\n', 'ISSUE-0001\nISSUE-0002\n', 'ISSUE-0001\n']
        with patch.object(evaluation.subprocess, 'run') as sql:
            sql.side_effect = [subprocess.CompletedProcess([], 0, out, '') for out in outputs]
            queries = {
                'n': 'SELECT count(*) FROM preflight.issues',
                'ids': 'SELECT issue_id FROM preflight.issues',
                'one': 'SELECT issue_id FROM preflight.issues LIMIT 1',
            }
            self.assertEqual(
                evaluation.sql_values('container', 'database', queries),
                {'n': 1784, 'ids': ['ISSUE-0001', 'ISSUE-0002'], 'one': 'ISSUE-0001'},
            )
            self.assertEqual(sql.call_count, 3)
            for invocation, query in zip(sql.call_args_list, queries.values(), strict=True):
                self.assertEqual(
                    invocation.args[0],
                    [
                        'docker',
                        'exec',
                        '-i',
                        'container',
                        'psql',
                        '-X',
                        '-At',
                        '-v',
                        'ON_ERROR_STOP=1',
                        '-U',
                        'postgres',
                        '-d',
                        'database',
                    ],
                )
                self.assertEqual(invocation.kwargs['input'], query)
        with (
            patch.object(evaluation.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'bad SQL')),
            self.assertRaisesRegex(RuntimeError, 'SQL.*n'),
        ):
            evaluation.sql_values('container', 'database', {'n': 'SELECT broken'})

    def test_question_file_is_validated(self):
        document = question_document()
        self.path.write_text(json.dumps(document))
        self.assertEqual(evaluation.load_cases(self.path), ('test.1', document['cases']))
        invalid = []
        for target, key in (('root', 'typo'), ('case', 'typo'), ('expect', 'typo'), ('number', 'expected')):
            changed = deepcopy(document)
            node = {
                'root': changed,
                'case': changed['cases'][0],
                'expect': changed['cases'][0]['expect'],
                'number': changed['cases'][0]['expect']['numbers'][0],
            }[target]
            node[key] = 1
            invalid.append((changed, 'unknown'))
        duplicate = deepcopy(document)
        duplicate['cases'][1]['id'] = duplicate['cases'][0]['id']
        invalid.append((duplicate, 'duplicate'))
        category = deepcopy(document)
        category['cases'][0]['category'] = 'other'
        invalid.append((category, 'category'))
        invalid.append(({**document, 'cases': document['cases'][:-1]}, '22'))
        for changed, message in invalid:
            with self.subTest(message=message, document=changed):
                self.path.write_text(json.dumps(changed))
                with self.assertRaisesRegex(ValueError, message):
                    evaluation.load_cases(self.path)

    def test_question_file_rejects_malformed_nested_fields(self):
        for field, value in (
            ('numbers', [{'label': 'ids', 'sql': 'SELECT count(*) FROM preflight.issues'}]),
            ('numbers', [{'label': 'n', 'sql': ''}]),
            ('numbers', [{'label': 'n', 'sql': 'SELECT 1'}, {'label': 'n', 'sql': 'SELECT 2'}]),
            ('forbidden', ['[']),
            ('tools_any', ['execute_sql']),
            ('max_tool_calls', True),
            ('verified', 'true'),
            ('ids_sql', []),
        ):
            with self.subTest(field=field, value=value):
                document = question_document()
                document['cases'][0]['expect'][field] = value
                self.path.write_text(json.dumps(document))
                with self.assertRaises(ValueError):
                    evaluation.load_cases(self.path)
        for history in ([{'role': 'system', 'content': 'x'}], [{'role': 'user', 'content': 'x', 'extra': 1}], 'x'):
            document = question_document()
            document['cases'][0]['history'] = history
            self.path.write_text(json.dumps(document))
            with self.assertRaises(ValueError):
                evaluation.load_cases(self.path)

    def test_score_passes_a_correct_answer(self):
        result = evaluation.score(CASE, EVENTS, TRUTH, runtime='native')
        self.assertTrue(result['passed'], result)
        self.assertEqual(result['failures'], [])
        self.assertEqual(result['tools'], ['get_aircraft_summary'])
        self.assertEqual(
            (result['tool_calls'], result['rounds'], result['verified'], result['unverified'], result['undeclared']),
            (1, 2, True, 0, 0),
        )

    def test_score_reports_each_failed_check(self):
        events = [
            ('tool', {'name': 'search_issues', 'status': 'complete', 'call_id': 'x'}),
            ('tool', {'name': 'search_issues', 'status': 'complete', 'call_id': 'y'}),
            ('delta', {'text': 'DEMO-AIR-00010 has 17840 issues; SAFE TO FLY.'}),
            ('error', {'message': 'unavailable'}),
            ('done', {'tool_calls': 2, 'verified': False}),
        ]
        result = evaluation.score(CASE, events, TRUTH, runtime='native')
        self.assertFalse(result['passed'])
        self.assertEqual(
            set(result['failures']),
            {
                'ids: DEMO-AIR-0001',
                'numbers: issues',
                'forbidden: safe to fly',
                'tools_any',
                'max_tool_calls',
                'verified',
                'no_error',
            },
        )

    def test_score_requires_done_and_completed_tools(self):
        result = evaluation.score(CASE, EVENTS[:1], TRUTH, runtime='copilot')
        self.assertFalse(result['passed'])
        self.assertIn('done', result['failures'])
        self.assertIn('tools_any', result['failures'])
        self.assertEqual(result['verified'], 'n/a')
        result = evaluation.score(CASE, [*EVENTS[:-1], ('done', {'tool_calls': 1})], TRUTH, runtime='copilot')
        self.assertTrue(result['passed'], result)
        self.assertEqual(result['verified'], 'n/a')
        self.assertIsNone(result['rounds'])

    def test_score_limits_attempts_including_failed_calls(self):
        events = [
            ('tool', {'name': 'search_issues', 'status': 'running', 'call_id': 'bad'}),
            ('tool', {'name': 'search_issues', 'status': 'failed', 'call_id': 'bad'}),
            *EVENTS,
        ]
        result = evaluation.score(CASE, events, TRUTH, runtime='native')
        self.assertEqual(result['failures'], ['max_tool_calls'])
        self.assertEqual(result['tool_calls'], 1)  # the runtime reports successful calls

    def test_runtime_configuration_reads_only_three_environment_values(self):
        with patch.object(evaluation.compose_check, 'run', return_value=json.dumps(CONFIG)) as command:
            self.assertEqual(evaluation.runtime_configuration(), CONFIG)
        args = command.call_args.args[0]
        self.assertEqual(args[:4], ['docker', 'exec', 'preflight-oss-api', 'python'])
        self.assertNotIn('_FILE', args[-1])
        self.assertNotIn('open(', args[-1])
        self.assertIn('CHAT_RUNTIME', args[-1])
        self.assertIn('MODEL_PROVIDER', args[-1])
        self.assertIn('COPILOT_MODEL', args[-1])

    def test_rate_limited_case_is_skipped_not_failed(self):
        # Three retries after the initial request: all four attempts remain rate-limited.
        ask = Mock(side_effect=RuntimeError('chat answered 429: busy'))
        with (
            patch.object(evaluation, 'runtime_configuration', return_value=CONFIG),
            patch.object(evaluation, 'sql_values', return_value=TRUTH),
            patch.object(evaluation.time, 'sleep') as sleep,
        ):
            report = evaluation.run([CASE], base='http://test', container='db', database='db', gap=13, ask_fn=ask)
        self.assertEqual(ask.call_count, 4)
        self.assertEqual(sleep.call_args_list, [call(15), call(30), call(60)])
        self.assertEqual(report['cases'][0]['skipped'], 'rate-limited')
        self.assertIsNone(report['cases'][0]['passed'])
        self.assertEqual(report['cases'][0]['failures'], [])
        self.assertEqual(report['overall'], {'total': 1, 'passed': 0, 'failed': 0, 'skipped': 1})
        self.assertIsNone(report['latency']['median_s'])

    def test_three_rate_limits_then_success_uses_all_backoffs(self):
        ask = Mock(side_effect=[RuntimeError('chat answered 429: busy')] * 3 + [EVENTS])
        with (
            patch.object(evaluation, 'runtime_configuration', return_value=CONFIG),
            patch.object(evaluation, 'sql_values', return_value=TRUTH),
            patch.object(evaluation.time, 'sleep') as sleep,
        ):
            report = evaluation.run([CASE], base='http://test', container='db', database='db', gap=13, ask_fn=ask)
        self.assertEqual(ask.call_count, 4)
        self.assertEqual(sleep.call_args_list, [call(15), call(30), call(60)])
        self.assertEqual(report['overall']['passed'], 1)

    def test_run_paces_forwards_history_and_records_latency(self):
        now = [0.0]
        trace = []

        def sleep(seconds):
            trace.append(('sleep', seconds))
            now[0] += seconds

        def ask(base, question, *, history, on_event):
            trace.append(('ask', now[0]))
            now[0] += 0.25
            on_event('delta', {'text': 'DEMO-AIR-0001 has 1,784 issues.'})
            now[0] += 0.75
            return EVENTS

        history = [{'role': 'user', 'content': 'Focus on DEMO-AIR-0001.'}]
        ask_mock = Mock(side_effect=ask)
        with (
            patch.object(evaluation, 'runtime_configuration', return_value=CONFIG),
            patch.object(evaluation, 'sql_values', return_value=TRUTH) as sql,
            patch.object(evaluation.time, 'sleep', side_effect=sleep),
            patch.object(evaluation.time, 'monotonic', side_effect=lambda: now[0]),
        ):
            report = evaluation.run(
                [CASE, {**CASE, 'id': 'follow', 'history': history}],
                base='http://test',
                container='db',
                database='database',
                gap=13,
                ask_fn=ask_mock,
            )
        self.assertEqual(trace, [('ask', 0), ('sleep', 13), ('ask', 14)])
        self.assertEqual(ask_mock.call_args_list[1].kwargs['history'], history)
        self.assertEqual(
            sql.call_args.args,
            ('db', 'database', {'ids': CASE['expect']['ids_sql'], 'issues': CASE['expect']['numbers'][0]['sql']}),
        )
        self.assertEqual(report['cases'][0]['first_delta_s'], 0.25)
        self.assertEqual(report['cases'][0]['total_s'], 1)
        self.assertEqual(report['latency'], {'median_s': 1, 'p90_s': 1})
        self.assertEqual(report['categories']['lookups']['passed'], 2)
        self.assertEqual({key: report[key] for key in CONFIG}, CONFIG)
        self.assertIn('started_at', report)

    def test_run_records_other_errors_and_continues(self):
        ask = Mock(side_effect=[RuntimeError('chat answered 503: unavailable'), EVENTS])
        with (
            patch.object(evaluation, 'runtime_configuration', return_value=CONFIG),
            patch.object(evaluation, 'sql_values', return_value=TRUTH),
            patch.object(evaluation.time, 'sleep'),
        ):
            report = evaluation.run(
                [CASE, {**CASE, 'id': 'second'}], base='http://test', container='db', database='db', gap=0, ask_fn=ask
            )
        self.assertEqual(report['overall'], {'total': 2, 'passed': 1, 'failed': 1, 'skipped': 0})
        self.assertEqual(ask.call_count, 2)
        self.assertIn('request: RuntimeError', report['cases'][0]['failures'])

    def test_run_records_malformed_streams_and_continues(self):
        for error in (ValueError('bad JSON'), evaluation.compose_check.http.client.IncompleteRead(b'partial')):
            with self.subTest(error=type(error).__name__):
                ask = Mock(side_effect=[error, EVENTS])
                with (
                    patch.object(evaluation, 'runtime_configuration', return_value=CONFIG),
                    patch.object(evaluation, 'sql_values', return_value=TRUTH),
                    patch.object(evaluation.time, 'sleep'),
                ):
                    report = evaluation.run(
                        [CASE, {**CASE, 'id': 'second'}],
                        base='http://test',
                        container='db',
                        database='db',
                        gap=0,
                        ask_fn=ask,
                    )
                self.assertEqual(report['overall']['failed'], 1)
                self.assertEqual(report['overall']['passed'], 1)

    def test_ask_closes_the_connection_on_rate_limits(self):
        with patch.object(evaluation.compose_check.http.client, 'HTTPConnection') as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 429
            response.read.return_value = b'busy'
            with self.assertRaisesRegex(RuntimeError, 'chat answered 429:'):
                evaluation.compose_check.ask('http://127.0.0.1:8080', 'Question')
            connection.return_value.close.assert_called_once()

    def test_ask_supports_history_and_stream_observation_without_changing_defaults(self):
        raw = b'event: delta\ndata: {"text":"hello"}\n\nevent: done\ndata: {}\n\n'
        history = [{'role': 'user', 'content': 'Earlier question'}]
        for supplied in (None, history):
            with self.subTest(history=supplied):
                response: Any = io.BytesIO(raw)
                response.status = 200
                observer = Mock()
                with patch.object(evaluation.compose_check.http.client, 'HTTPConnection') as connection:
                    connection.return_value.getresponse.return_value = response
                    kwargs = {} if supplied is None else {'history': supplied, 'on_event': observer}
                    events = evaluation.compose_check.ask('http://127.0.0.1:8080', 'Question', **kwargs)
                self.assertEqual(events, [('delta', {'text': 'hello'}), ('done', {})])
                body = json.loads(connection.return_value.request.call_args.kwargs['body'])
                self.assertEqual(body, {'question': 'Question', 'history': supplied or []})
                if supplied:
                    self.assertEqual(observer.call_args_list, [call(*event) for event in events])
                connection.return_value.close.assert_called_once()

    def test_main_validates_before_requests_filters_and_writes_default_evidence(self):
        document = question_document()
        self.path.write_text(json.dumps(document))
        report = {
            'cases': [{'id': document['cases'][0]['id'], 'passed': True, 'failures': []}],
            'overall': {'total': 1, 'passed': 1, 'failed': 0, 'skipped': 0},
        }
        with (
            patch.object(evaluation, 'ROOT', Path(self.temp.name)),
            patch.object(evaluation, 'run', return_value=report) as run,
            patch('sys.stdout', new_callable=io.StringIO) as out,
            patch('sys.argv', ['eval_run.py', '--questions', str(self.path), '--only', document['cases'][0]['id']]),
            self.assertRaises(SystemExit) as exit_status,
        ):
            evaluation.main()
        self.assertEqual(exit_status.exception.code, 0)
        self.assertEqual(len(run.call_args.args[0]), 1)
        self.assertEqual(run.call_args.kwargs['gap'], 13)
        outputs = list((Path(self.temp.name) / 'evidence').glob('*.json'))
        self.assertEqual(len(outputs), 1)
        self.assertEqual(json.loads(outputs[0].read_text())['version'], 'test.1')
        self.assertIn(document['cases'][0]['id'], out.getvalue())
        self.assertIn('passed', out.getvalue())
        for argv in (['--only', 'unknown'], ['--gap', '-1'], ['--gap', 'nan']):
            with (
                patch.object(evaluation, 'run') as run,
                patch('sys.stderr', new_callable=io.StringIO),
                patch('sys.argv', ['eval_run.py', '--questions', str(self.path), *argv]),
            ):
                with self.assertRaises(SystemExit) as status:
                    evaluation.main()
                self.assertNotEqual(status.exception.code, 0)
                run.assert_not_called()
        self.path.write_text('{"version":"bad","cases":[]}')
        with (
            patch.object(evaluation, 'run') as run,
            patch('sys.stderr', new_callable=io.StringIO),
            patch('sys.argv', ['eval_run.py', '--questions', str(self.path)]),
        ):
            with self.assertRaises(SystemExit):
                evaluation.main()
            run.assert_not_called()

    def test_main_exit_status_excludes_skips_and_honors_output(self):
        self.path.write_text(json.dumps(question_document()))
        output = Path(self.temp.name) / 'nested/report.json'
        for failed, expected in ((0, 0), (1, 1)):
            report = {
                'cases': [{'id': 'limited', 'passed': None, 'skipped': 'rate-limited', 'failures': []}],
                'overall': {'total': 1 + failed, 'passed': 0, 'failed': failed, 'skipped': 1},
            }
            with (
                patch.object(evaluation, 'run', return_value=report),
                patch('sys.stdout', new_callable=io.StringIO),
                patch('sys.argv', ['eval_run.py', '--questions', str(self.path), '--output', str(output)]),
                self.assertRaises(SystemExit) as status,
            ):
                evaluation.main()
            self.assertEqual(status.exception.code, expected)
            self.assertEqual(json.loads(output.read_text())['overall']['failed'], failed)
