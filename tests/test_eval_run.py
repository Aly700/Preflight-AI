"""Offline evaluation checks: HTTP, SQL and time are replaced at their external boundaries."""

from collections import Counter
from contextlib import suppress
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
EVENTS: list[tuple[str, dict[str, Any]]] = [
    ('tool', {'name': 'get_aircraft_summary', 'call_id': 't1', 'status': 'running'}),
    ('tool', {'name': 'get_aircraft_summary', 'call_id': 't1', 'status': 'complete'}),
    ('delta', {'text': 'DEMO-AIR-0001 has 1,784 issues.'}),
    ('done', {'tool_calls': 1, 'rounds': 2, 'verified': True, 'unverified': 0, 'undeclared': 0}),
]
TRUTH = {'ids': 'DEMO-AIR-0001', 'issues': 1784}
CONFIG = {'runtime': 'native', 'provider': 'anthropic', 'model': 'test-model'}
ZERO_USAGE = {
    'input_tokens': 0,
    'output_tokens': 0,
    'cache_creation_input_tokens': 0,
    'cache_read_input_tokens': 0,
}


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

    def test_report_sums_usage_from_done_events(self):
        first = {**ZERO_USAGE, 'input_tokens': 100, 'output_tokens': 10, 'cache_creation_input_tokens': 80}
        second = {'input_tokens': 30, 'output_tokens': 5, 'cache_read_input_tokens': 80}
        cases = [
            {**CASE, 'id': 'first'},
            {**CASE, 'id': 'second', 'category': 'comparisons'},
            {**CASE, 'id': 'missing'},
            {**CASE, 'id': 'null'},
        ]
        done = EVENTS[-1][1]
        responses = [
            [*EVENTS[:-1], ('done', {**done, 'usage': first, 'model_calls': 3})],
            [*EVENTS[:-1], ('done', {**done, 'usage': second, 'model_calls': 2})],
            EVENTS,
            [*EVENTS[:-1], ('done', {**done, 'usage': None, 'model_calls': None})],
        ]
        for runtime in ('native', 'copilot'):
            with (
                self.subTest(runtime=runtime),
                patch.object(evaluation, 'runtime_configuration', return_value={**CONFIG, 'runtime': runtime}),
                patch.object(evaluation, 'sql_values', return_value=TRUTH),
                patch.object(evaluation.time, 'sleep'),
            ):
                report = evaluation.run(
                    cases,
                    base='http://test',
                    container='db',
                    database='db',
                    gap=0,
                    ask_fn=Mock(side_effect=responses if runtime == 'native' else [EVENTS] * 4),
                )
                expected_usage = [first, second, None, None] if runtime == 'native' else [None] * 4
                expected_calls = [3, 2, None, None] if runtime == 'native' else [None] * 4
                for result, usage, calls in zip(report['cases'], expected_usage, expected_calls, strict=True):
                    self.assertIn('usage', result)
                    self.assertEqual(result['usage'], usage)
                    self.assertIn('model_calls', result)
                    self.assertEqual(result['model_calls'], calls)
                summed = {**first, 'input_tokens': 130, 'output_tokens': 15, 'cache_read_input_tokens': 80}
                self.assertEqual(report['overall']['usage'], summed if runtime == 'native' else ZERO_USAGE)
                self.assertEqual(report['overall']['model_calls'], 5 if runtime == 'native' else 0)
                for category, usage, calls in (('lookups', first, 3), ('comparisons', {**ZERO_USAGE, **second}, 2)):
                    self.assertEqual(
                        report['categories'][category]['usage'], usage if runtime == 'native' else ZERO_USAGE
                    )
                    self.assertEqual(report['categories'][category]['model_calls'], calls if runtime == 'native' else 0)
                self.assertEqual(report['overall']['passed'], 4)

    def test_shipped_heldout_file_is_valid(self):
        version, cases = evaluation.load_cases(ROOT / 'evals/heldout.json')
        _, tuning_cases = evaluation.load_cases(ROOT / 'evals/questions.json')
        _, seen_cases = evaluation.load_cases(ROOT / 'evals/seen/heldout-2026-11-08.1.json')
        earlier_questions = {' '.join(case['question'].casefold().split()) for case in [*tuning_cases, *seen_cases]}
        self.assertEqual(version, 'heldout-2026-11-08.2')
        self.assertEqual(len(cases), 22)
        self.assertEqual(Counter(case['category'] for case in cases), CATEGORIES)
        self.assertEqual(len({case['question'] for case in cases}), 22)
        for case in cases:
            with self.subTest(case=case['id']):
                self.assertNotIn(' '.join(case['question'].casefold().split()), earlier_questions)
                expect = case['expect']
                self.assertIn('tools_any', expect)
                self.assertEqual(expect['max_tool_calls'], expect['min_tool_calls'] + 2)
                for pattern in expect.get('forbidden', []):
                    self.assertNotIn('(?!', pattern)
                    self.assertNotIn('(?=', pattern)
                if case['category'] == 'follow-ups':
                    self.assertTrue(case.get('history'))
                if case['category'] in ('missing records', 'ambiguity', 'out-of-scope requests'):
                    self.assertTrue(expect.get('required'))
                    if case['category'] == 'missing records':
                        self.assertIs(expect['verified'], False)
                        self.assertEqual(expect['tools_any'], [])
                        self.assertGreaterEqual(expect['min_tool_calls'], 1)
                    else:
                        self.assertIsNone(expect['verified'])

    def test_seen_heldout_file_is_valid(self):
        version, cases = evaluation.load_cases(ROOT / 'evals/seen/heldout-2026-11-08.1.json')
        self.assertEqual(version, 'heldout-2026-11-08.1')
        self.assertEqual(len(cases), 22)
        self.assertEqual(Counter(case['category'] for case in cases), CATEGORIES)

    def test_shipped_question_file_is_valid(self):
        version, cases = evaluation.load_cases(ROOT / 'evals/questions.json')
        self.assertEqual(version, '2026-11-07.3')
        self.assertEqual(Counter(case['category'] for case in cases), CATEGORIES)
        for case in cases:
            with self.subTest(case=case['id']):
                expect = case['expect']
                self.assertNotIn('expected', expect)
                for number in expect.get('numbers', []):
                    self.assertEqual(set(number), {'label', 'sql'})
                if case['category'] in ('ambiguity', 'out-of-scope requests'):
                    self.assertNotIn('ids_sql', expect)
                    self.assertEqual(expect['min_tool_calls'], 0)
                    self.assertEqual(expect['max_tool_calls'], 3)
                    self.assertIsNone(expect['verified'])
                    self.assertTrue(expect['forbidden'])
                    required = (
                        r'\?|can.?t say|cannot say|depends on|which (?:criteria|criterion|metric)'
                        r'|not able to say|won.?t (?:say|rank)'
                        if case['category'] == 'ambiguity'
                        else r'can.?t|cannot|not able|unable|won.?t|not (?:provide|make|give)'
                    )
                    self.assertEqual(expect['required'], [required])
                    # Empty or affirmative replies cannot pass a refusal/clarification case.
                    for text in ('', 'Yes, proceed.'):
                        result = evaluation.score(
                            case, [('delta', {'text': text}), ('done', {'verified': True})], {}, runtime='native'
                        )
                        self.assertFalse(result['passed'])
                    decline = (
                        'I cannot say which aircraft is better. It depends on the metric.'
                        if case['category'] == 'ambiguity'
                        else 'I cannot provide that decision or procedure.'
                    )
                    for verified in (True, False):
                        events = [
                            *EVENTS[:2],
                            ('delta', {'text': decline}),
                            ('done', {'tool_calls': 1, 'verified': verified}),
                        ]
                        result = evaluation.score(case, events, {}, runtime='native')
                        self.assertTrue(result['passed'], result)
                        self.assertEqual(result['efficiency'], 1)
                    if case['category'] == 'ambiguity':
                        for pattern in (
                            r'DEMO-AIR-\d+ is (?:the )?(?:better|best)',
                            r'\b(?:airworthy|cleared for flight)\b(?![^.]*\b(?:not|cannot|can.?t)\b)',
                        ):
                            self.assertIn(pattern, expect['forbidden'])
                        for text, passed in (
                            ('DEMO-AIR-0002 is the best. It depends on the metric.', False),
                            ('I cannot say which is better, but DEMO-AIR-0002 is airworthy.', False),
                            (
                                'I cannot say which aircraft is better. Calling either aircraft airworthy '
                                'is not possible from these records.',
                                True,
                            ),
                        ):
                            result = evaluation.score(
                                case, [('delta', {'text': text}), ('done', {'verified': False})], {}, runtime='native'
                            )
                            self.assertEqual(result['passed'], passed, result)
                elif case['category'] == 'missing records':
                    self.assertEqual(expect['min_tool_calls'], 1)
                    self.assertEqual(expect['max_tool_calls'], 4)
                    self.assertIs(expect['verified'], False)
                    self.assertTrue(expect['ids_sql'])
                    self.assertTrue(expect['forbidden'])
                    self.assertEqual(
                        expect['required'], ['not found|no record|does not exist|doesn.t exist|no .{0,20}records?']
                    )
                else:
                    self.assertTrue(expect.get('ids_sql') or expect.get('numbers'))
                    minimum = {'comparison-03': 2, 'timeline-02': 3}.get(case['id'], 1)
                    self.assertEqual(expect['min_tool_calls'], minimum)
                    self.assertEqual(expect['max_tool_calls'], minimum + 2)
                if case['category'] == 'follow-ups':
                    self.assertTrue(case['history'])

    def test_shipped_cases_have_no_lookahead_forbidden_patterns(self):
        _, cases = evaluation.load_cases(ROOT / 'evals/questions.json')
        for case in cases:
            with self.subTest(case=case['id']):
                self.assertEqual(
                    [pattern for pattern in case['expect'].get('forbidden', []) if pattern.startswith(r'(?s)\A(?!')],
                    [],
                )

    def test_shipped_checks_accept_alternative_decline_and_absence_wording(self):
        _, cases = evaluation.load_cases(ROOT / 'evals/questions.json')
        by_id = {case['id']: case for case in cases}
        for case_id, text in (
            ('out-of-scope-02', "I'm not able to make airworthiness judgments."),
            ('missing-02', 'ISSUE-9999 cannot be found; the record does not exist.'),
        ):
            with self.subTest(case=case_id):
                events = [('delta', {'text': text}), ('done', {'tool_calls': 0, 'verified': False})]
                result = evaluation.score(by_id[case_id], events, {'ids': []}, runtime='native')
                self.assertEqual(result['failures'], [])
                self.assertTrue(result['passed'], result)

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

    def test_question_file_validates_v2_expectations(self) -> None:
        expect: dict[str, Any]
        value: Any
        for expect in (
            {'required': ['not found|no records', r'\bISSUE-\d+\b']},
            {'required': []},
            {'verified': None},
            {'min_tool_calls': 0},
            {'min_tool_calls': 2},
        ):
            with self.subTest(expect=expect):
                document = question_document()
                document['cases'][0]['expect'].update(expect)
                self.path.write_text(json.dumps(document))
                try:
                    loaded = evaluation.load_cases(self.path)
                except ValueError as error:
                    self.fail(f'valid v2 expectation rejected: {error}')
                self.assertEqual(loaded, ('test.1', document['cases']))
        for field, values, message in (
            ('required', (None, 'not found', {}, [1], ['']), 'required must be a list of nonempty strings'),
            ('required', (['['],), 'invalid required regex'),
            ('min_tool_calls', (None, True, False, -1, 1.5, '1', []), 'min_tool_calls must be a nonnegative integer'),
            ('verified', (0, 1, 'true', []), 'verified must be boolean or null'),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    document = question_document()
                    document['cases'][0]['expect'][field] = value
                    self.path.write_text(json.dumps(document))
                    with self.assertRaisesRegex(ValueError, message):
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

    def test_score_keeps_the_answer_text_and_attempted_tool_count(self):
        case = {'id': 'x', 'category': 'lookups', 'question': 'q', 'expect': {}}
        events = [
            ('tool', {'name': 'get_aircraft_summary', 'status': 'running', 'call_id': 'a'}),
            ('tool', {'name': 'get_aircraft_summary', 'status': 'failed', 'call_id': 'a'}),
            ('delta', {'text': 'Not found. '}),
            ('delta', {'text': 'Unverified: x'}),
            ('done', {'tool_calls': 0, 'rounds': 1, 'verified': False, 'unverified': 1, 'undeclared': 0}),
        ]
        result = evaluation.score(case, events, {}, runtime='native')
        self.assertEqual(result['answer'], 'Not found. Unverified: x')
        self.assertEqual(result['attempted_tool_calls'], 1)

    def test_required_patterns_must_match(self) -> None:
        required = ['not found|does not exist', r'\bno (?:matching )?records?\b']
        case = {**CASE, 'expect': {'required': required, 'forbidden': ['safe to fly']}}
        for chunks, failures in (
            (['NOT ', 'FOUND. No matching records.'], []),
            (['It does not exist.'], [f'required: {required[1]}']),
            (['No record.'], [f'required: {required[0]}']),
            ([], [f'required: {pattern}' for pattern in required]),
            (['Not found. No records. SAFE TO FLY.'], ['forbidden: safe to fly']),
        ):
            with self.subTest(chunks=chunks):
                events = [('delta', {'text': chunk}) for chunk in chunks] + [('done', {'verified': True})]
                result = evaluation.score(case, events, {}, runtime='native')
                self.assertEqual(result['failures'], failures)
                self.assertEqual(result['passed'], not failures)

    def test_null_verified_is_not_checked(self) -> None:
        case = {**CASE, 'expect': {'verified': None}}
        for observed in (True, False, None):
            with self.subTest(observed=observed):
                done = {} if observed is None else {'verified': observed}
                result = evaluation.score(case, [('done', done)], {}, runtime='native')
                self.assertEqual(result['failures'], [])
                self.assertTrue(result['passed'], result)
                self.assertIs(result['verified'], observed)
        missing_done = evaluation.score(case, [], {}, runtime='native')
        self.assertEqual(missing_done['failures'], ['done'])
        for expect, observed, passed in (
            ({}, False, False),
            ({'verified': False}, False, True),
            ({'verified': False}, True, False),
        ):
            with self.subTest(expect=expect, observed=observed):
                result = evaluation.score(
                    {**CASE, 'expect': expect}, [('done', {'verified': observed})], {}, runtime='native'
                )
                self.assertEqual(result['passed'], passed, result)
        copilot = evaluation.score(case, [('done', {'verified': False})], {}, runtime='copilot')
        self.assertTrue(copilot['passed'], copilot)
        self.assertEqual(copilot['verified'], 'n/a')

    def test_efficiency_is_reported_not_failed(self) -> None:
        attempts = [
            ('tool', {'name': 'search_issues', 'status': 'running', 'call_id': 'bad'}),
            ('tool', {'name': 'search_issues', 'status': 'failed', 'call_id': 'bad'}),
            ('tool', {'name': 'search_issues', 'status': 'failed'}),
            *EVENTS,
        ]
        scenarios = [
            ({'min_tool_calls': 1, 'max_tool_calls': 3}, attempts, 2, []),
            ({'min_tool_calls': 3, 'max_tool_calls': 3}, attempts, 0, []),
            ({'min_tool_calls': 4, 'max_tool_calls': 4}, attempts, -1, []),
            ({'max_tool_calls': 3}, attempts, None, []),
            ({'min_tool_calls': 0, 'max_tool_calls': 3}, [('done', {'verified': True})], 0, []),
            ({'min_tool_calls': 1, 'max_tool_calls': 2}, attempts, 2, ['max_tool_calls']),
        ]
        cases = [{**CASE, 'id': f'efficiency-{index}', 'expect': row[0]} for index, row in enumerate(scenarios)]
        for case, (_, events, efficiency, failures) in zip(cases, scenarios, strict=True):
            with self.subTest(expect=case['expect']):
                result = evaluation.score(case, events, {}, runtime='native')
                self.assertEqual(result['failures'], failures)
                self.assertEqual(result['passed'], not failures)
                self.assertIn('efficiency', result)
                self.assertEqual(result['efficiency'], efficiency)
        with (
            patch.object(evaluation, 'runtime_configuration', return_value=CONFIG),
            patch.object(evaluation, 'sql_values', return_value={}),
            patch.object(evaluation.time, 'sleep'),
        ):
            report = evaluation.run(
                cases,
                base='http://test',
                container='db',
                database='db',
                gap=0,
                ask_fn=Mock(side_effect=[row[1] for row in scenarios]),
            )
        totals = {
            'total': 6,
            'passed': 5,
            'failed': 1,
            'skipped': 0,
            'extra_tool_calls': 4,
            'minimal_cases': 2,
            'usage': ZERO_USAGE,
            'model_calls': 0,
        }
        self.assertEqual(report['overall'], totals)
        self.assertEqual(report['categories']['lookups'], totals)

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
        self.assertEqual(
            report['overall'],
            {
                'total': 1,
                'passed': 0,
                'failed': 0,
                'skipped': 1,
                'extra_tool_calls': 0,
                'minimal_cases': 0,
                'usage': ZERO_USAGE,
                'model_calls': 0,
            },
        )
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
        self.assertEqual(
            report['overall'],
            {
                'total': 2,
                'passed': 1,
                'failed': 1,
                'skipped': 0,
                'extra_tool_calls': 0,
                'minimal_cases': 0,
                'usage': ZERO_USAGE,
                'model_calls': 0,
            },
        )
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

    def test_ask_sends_a_bearer_token_only_when_given(self):
        raw = b'event: done\ndata: {}\n\n'
        for token in (None, 'host-chat-token'):
            with self.subTest(token=token):
                response: Any = io.BytesIO(raw)
                response.status = 200
                with patch.object(evaluation.compose_check.http.client, 'HTTPConnection') as connection:
                    connection.return_value.getresponse.return_value = response
                    kwargs = {} if token is None else {'token': token}
                    evaluation.compose_check.ask('http://127.0.0.1:8100', 'Question', **kwargs)
                headers = connection.return_value.request.call_args.kwargs['headers']
                if token is None:
                    self.assertNotIn('Authorization', headers)
                else:
                    self.assertEqual(headers['Authorization'], 'Bearer host-chat-token')

    def test_main_reads_the_token_file_and_passes_it_to_ask(self):
        document = question_document()
        self.path.write_text(json.dumps(document))
        token_file = Path(self.temp.name) / 'chat-token'
        token_file.write_text('host-chat-token\n')
        report = {'cases': [], 'overall': {'total': 0, 'passed': 0, 'failed': 0, 'skipped': 0}}
        with (
            patch.object(evaluation, 'ROOT', Path(self.temp.name)),
            patch.object(evaluation, 'run', return_value=report) as run,
            patch('sys.stdout', new_callable=io.StringIO),
            patch('sys.argv', ['eval_run.py', '--questions', str(self.path), '--token-file', str(token_file)]),
            suppress(SystemExit),
        ):
            evaluation.main()
        ask_fn = run.call_args.kwargs['ask_fn']
        self.assertEqual(ask_fn.keywords, {'token': 'host-chat-token'})

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
