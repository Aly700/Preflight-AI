"""Repeated-run summary: per-case pass rates, per-run spread and failure kinds, from saved reports only."""

import importlib.util
import io
import json
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('eval_summary', ROOT / 'scripts/eval_summary.py')
assert SPEC is not None and SPEC.loader is not None
summary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(summary)


def case(ident: str, failures: list[str], *, category: str = 'lookups', total_s: float = 5.0, skipped: str = ''):
    result: dict[str, Any] = {
        'id': ident,
        'category': category,
        'passed': not failures and not skipped,
        'failures': failures,
        'total_s': None if skipped else total_s,
    }
    if skipped:
        result['skipped'] = skipped
    return result


def report(cases: list[dict[str, Any]], *, version: str = 'v1', started: str = '2026-11-08T00:00:00+00:00'):
    return {'version': version, 'started_at': started, 'model': 'model-a', 'runtime': 'native', 'cases': cases}


class FailureKinds(unittest.TestCase):
    def test_each_scorer_label_maps_to_one_kind(self):
        expected = {
            'ids: ISSUE-0001': 'content',
            'numbers: total': 'content',
            'forbidden: safe to fly': 'content',
            'required: cannot': 'content',
            'no_error': 'content',
            'done': 'content',
            'verified': 'verification',
            'tools_any': 'tools',
            'max_tool_calls': 'tools',
            'something new': 'other',
        }
        for label, kind in expected.items():
            with self.subTest(label=label):
                self.assertEqual(summary.failure_kind(label), kind)


class Summarise(unittest.TestCase):
    def setUp(self):
        self.runs = [
            report([case('a', []), case('b', ['verified']), case('c', ['numbers: x'])], started='1'),
            report([case('a', []), case('b', []), case('c', ['numbers: x', 'tools_any'])], started='2'),
            report([case('a', ['max_tool_calls']), case('b', []), case('c', ['ids: ISSUE-0001'])], started='3'),
        ]

    def test_per_run_strict_and_content_counts(self):
        result = summary.summarise(self.runs)
        self.assertEqual(result['runs'], 3)
        self.assertEqual([run['strict'] for run in result['per_run']], [1, 2, 1])
        # Content passes ignore verification and tool-choice failures.
        self.assertEqual([run['content'] for run in result['per_run']], [2, 2, 2])
        self.assertEqual(result['strict'], {'mean': 4 / 3, 'min': 1, 'max': 2})
        self.assertEqual(result['content'], {'mean': 2, 'min': 2, 'max': 2})

    def test_per_case_rates_and_failure_kinds(self):
        cases = {item['id']: item for item in summary.summarise(self.runs)['cases']}
        self.assertEqual((cases['a']['strict_passes'], cases['a']['content_passes'], cases['a']['runs']), (2, 3, 3))
        self.assertEqual(cases['b']['kinds'], {'verification': 1})
        self.assertEqual(cases['c']['kinds'], {'content': 3, 'tools': 1})
        self.assertEqual(cases['c']['failures'], {'numbers: x': 2, 'tools_any': 1, 'ids: ISSUE-0001': 1})

    def test_flaky_and_always_failing_cases_are_listed(self):
        result = summary.summarise(self.runs)
        self.assertEqual(result['flaky'], ['a', 'b'])
        self.assertEqual(result['always_failing'], ['c'])
        self.assertEqual(result['failure_counts'], {'content': 3, 'tools': 2, 'verification': 1})

    def test_skipped_cases_count_in_neither_rate(self):
        runs = [report([case('a', [])]), report([case('a', [], skipped='rate-limited')])]
        result = summary.summarise(runs)
        self.assertEqual(result['cases'][0]['runs'], 1)
        self.assertEqual(result['cases'][0]['skipped'], 1)
        self.assertEqual([run['strict'] for run in result['per_run']], [1, 0])
        self.assertEqual([run['scored'] for run in result['per_run']], [1, 0])
        self.assertEqual(result['flaky'], [])

    def test_latency_is_reported_per_run_from_scored_cases(self):
        runs = [report([case('a', [], total_s=2.0), case('b', [], total_s=6.0), case('c', [], total_s=4.0)])]
        self.assertEqual(summary.summarise(runs)['per_run'][0]['median_s'], 4.0)

    def test_declaration_leaks_are_counted_per_run_and_absent_in_older_reports(self):
        leaked = {**case('a', []), 'declaration_leak': True}
        runs = [report([leaked, case('b', [])]), report([case('a', []), case('b', [])])]
        result = summary.summarise(runs)
        self.assertEqual([run['declaration_leaks'] for run in result['per_run']], [1, 0])
        self.assertIn('declaration blocks in answers 1, 0', summary.table(result))

    def test_reports_must_share_a_version_and_case_set(self):
        with self.assertRaisesRegex(ValueError, 'version'):
            summary.summarise([report([case('a', [])]), report([case('a', [])], version='v2')])
        with self.assertRaisesRegex(ValueError, 'cases'):
            summary.summarise([report([case('a', [])]), report([case('b', [])])])
        with self.assertRaisesRegex(ValueError, 'at least one'):
            summary.summarise([])


class Cli(unittest.TestCase):
    def test_writes_json_and_prints_a_table(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for index, failures in enumerate(([], ['verified'])):
                path = Path(directory) / f'run{index}.json'
                path.write_text(json.dumps(report([case('a', failures)])), encoding='utf-8')
                paths.append(str(path))
            output = Path(directory) / 'summary.json'
            stdout = io.StringIO()
            with patch('sys.stdout', stdout):
                code = summary.main([*paths, '--output', str(output)])
            self.assertEqual(code, 0)
            written = json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual(written['runs'], 2)
            self.assertIn('a', stdout.getvalue())
            self.assertIn('strict 1/1, 0/1', stdout.getvalue())

    def test_invalid_input_exits_two_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'broken.json'
            path.write_text('{', encoding='utf-8')
            stderr = io.StringIO()
            with patch('sys.stderr', stderr), patch('sys.stdout', io.StringIO()):
                code = summary.main([str(path)])
            self.assertEqual(code, 2)
            self.assertNotIn('Traceback', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
