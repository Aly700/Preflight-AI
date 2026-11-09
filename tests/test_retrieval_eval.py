"""Retrieval scoring, local ranking and cached Titan calls without a network or database."""

from copy import deepcopy
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any
import unittest
from unittest.mock import Mock, patch

import numpy as np

from mcp_service import repository

ROOT = Path(__file__).resolve().parents[1]
CASE: dict[str, Any] = {
    'id': 'case-01',
    'question': 'private question marker',
    'doc_ids': ['DOC-0001'],
    'phrases': ['target phrase'],
}
GOLD = {'version': 'test-v1', 'cases': [CASE]}


def passage(ident, text='private passage marker', doc_id='DOC-0001'):
    return {'passage_id': ident, 'doc_id': doc_id, 'text': text}


class CandidateTests(unittest.TestCase):
    def test_search_is_fusion_of_extracted_candidates_with_unchanged_sql_and_filters(self):
        self.assertTrue(hasattr(repository.Repository, 'vector_candidates'), 'Extract vector_candidates')
        self.assertTrue(hasattr(repository.Repository, 'keyword_candidates'), 'Extract keyword_candidates')
        a, b, c = [passage(f'DOC-0001#{i}') for i in range(1, 4)]
        vector = np.array([0.6, 0.8], dtype=np.float32)
        for kind, related in ((None, None), ('memo', None), (None, 'ISSUE-0001'), ('memo', 'ISSUE-0001')):
            with self.subTest(kind=kind, related=related):
                connection = Mock()
                connection.execute.return_value.fetchall.side_effect = [[b, a], [a, c], [b, a], [a, c]]
                repo = repository.Repository(connection)
                with patch.object(repository, 'get_embedder') as embedder:
                    embedder.return_value.embed_query.return_value = vector
                    vectors = repo.vector_candidates(vector, kind, related)
                    keywords = repo.keyword_candidates(CASE['question'], kind, related)
                    embedder.assert_not_called()
                    actual = repo.search_documents(CASE['question'], 2, kind, related)
                self.assertEqual(vectors, [b, a])
                self.assertEqual(keywords, [a, c])
                self.assertEqual(actual, {'results': repository.fuse_passages(vectors, keywords, 2)})
                self.assertEqual(connection.execute.call_args_list[:2], connection.execute.call_args_list[2:])

    def test_candidate_limits_are_integer_only_and_default_to_twenty(self):
        self.assertTrue(hasattr(repository.Repository, 'vector_candidates'), 'Extract candidate methods')
        connection = Mock()
        repo = repository.Repository(connection)
        methods: list[tuple[Any, Any]] = [
            (repo.vector_candidates, np.array([1.0])),
            (repo.keyword_candidates, 'question'),
        ]
        for method, query in methods:
            method(query, None, None)
            self.assertIn('LIMIT 20', connection.execute.call_args.args[0])
            method(query, 'memo', 'DOC-0001', limit=7)
            self.assertIn('LIMIT 7', connection.execute.call_args.args[0])
            for bad in (0, -1, True, '1; DROP TABLE preflight.passages'):
                with self.assertRaises(ValueError):
                    method(query, None, None, limit=bad)


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / 'scripts/retrieval_eval.py').is_file(), 'Implement the retrieval evaluator')
        self.evaluation = importlib.import_module('scripts.retrieval_eval')

    def test_relevance_normalises_case_and_whitespace_with_doc_restriction_and_alternatives(self):
        case = {**CASE, 'phrases': ['absent', ' TARGET\n\tPhrase ']}
        self.assertTrue(self.evaluation.is_relevant(case, passage('p1', 'A target\u00a0 PHRASE here.')))
        self.assertFalse(self.evaluation.is_relevant(case, passage('p2', 'target phrase', 'DOC-0002')))
        self.assertFalse(self.evaluation.is_relevant(case, passage('p3', 'target unrelated phrase')))
        self.assertFalse(self.evaluation.is_relevant({**case, 'phrases': []}, passage('p4', 'anything')))

    def test_metrics_boundaries_misses_and_document_only_hits(self):
        cases = [{**CASE, 'id': f'case-{i}'} for i in range(6)]
        miss = passage('m', 'no evidence', 'DOC-0002')
        hit = passage('h', 'target phrase')
        rankings = {
            cases[0]['id']: [hit],
            cases[1]['id']: [miss] * 4 + [hit],
            cases[2]['id']: [passage('d')] + [miss] * 4 + [hit],
            cases[3]['id']: [miss] * 9 + [hit],
            cases[4]['id']: [miss] * 10 + [hit],
            cases[5]['id']: [],
        }
        result = self.evaluation.make_report({'version': 'v1', 'cases': cases}, {'example': rankings}, mode='offline')
        metric = result['variants']['example']
        self.assertEqual(metric['cases'], 6)
        self.assertAlmostEqual(metric['passage_recall@5'], 2 / 6)
        self.assertAlmostEqual(metric['document_recall@5'], 3 / 6)
        self.assertAlmostEqual(metric['mrr@10'], (1 + 1 / 5 + 1 / 6 + 1 / 10) / 6)
        self.assertEqual([row['first_relevant_rank']['example'] for row in result['cases']], [1, 5, 6, 10, None, None])

    def test_ranks_are_positions_even_when_scores_tie(self):
        ranks = {
            'case-01': [{**passage('a', doc_id='DOC-0002'), 'score': 1}, {**passage('b', 'target phrase'), 'score': 1}]
        }
        report = self.evaluation.make_report(GOLD, {'tied': ranks}, mode='offline')
        self.assertEqual(report['cases'][0]['first_relevant_rank']['tied'], 2)
        self.assertEqual(report['variants']['tied']['mrr@10'], 0.5)

    def test_report_is_deterministic_and_contains_only_allowlisted_fields(self):
        gold = {**GOLD, 'note': 'private note marker'}
        rows = {'case-01': [passage('private-passage-id', 'private passage marker target phrase')]}
        original = deepcopy((gold, rows))
        report = self.evaluation.make_report(gold, {'bge_ocr': rows}, mode='offline')
        self.assertEqual(report, self.evaluation.make_report(gold, {'bge_ocr': rows}, mode='offline'))
        self.assertEqual((gold, rows), original)
        self.assertEqual(set(report), {'version', 'mode', 'variants', 'cases'})
        self.assertEqual(report['cases'], [{'id': 'case-01', 'first_relevant_rank': {'bge_ocr': 1}}])
        self.assertEqual(
            set(report['variants']['bge_ocr']), {'cases', 'passage_recall@5', 'document_recall@5', 'mrr@10'}
        )
        encoded = json.dumps(report) + self.evaluation.table(report)
        for forbidden in ('private', 'target phrase', 'DOC-0001', 'question', 'text', 'phrases'):
            self.assertNotIn(forbidden, encoded)
        with self.assertRaisesRegex(ValueError, 'cases'):
            self.evaluation.make_report({'version': 'empty', 'cases': []}, {}, mode='offline')
        with self.assertRaisesRegex(ValueError, 'cases'):
            self.evaluation.make_report(GOLD, {'incomplete': {}}, mode='offline')

    def test_cosine_ranking_normalises_vectors_breaks_ties_by_id_and_limits(self):
        rows = [passage('c'), passage('b'), passage('a'), passage('d')]
        vectors = np.array([[1, 1], [3, 0], [10, 0], [-1, 0]], dtype=np.float32)
        original = deepcopy(rows)
        ranked = self.evaluation.cosine_rank(rows, vectors, np.array([2, 0]), limit=3)
        self.assertEqual([row['passage_id'] for row in ranked], ['a', 'b', 'c'])
        self.assertEqual(rows, original)
        for bad in (np.zeros((4, 2)), np.full((4, 2), np.nan), vectors[:3]):
            with self.assertRaises(ValueError):
                self.evaluation.cosine_rank(rows, bad, np.array([2, 0]))
        with self.assertRaises(ValueError):
            self.evaluation.cosine_rank(rows, vectors, np.array([0, 0]))

    def fixture(self, directory):
        source = directory / 'DOC-0001.md'
        source.write_text(
            '---\ndoc_id: DOC-0001\nkind: memo\ntitle: Fixture\ndate: 2026-10-01\n'
            'author: PERSON-0001\nteam: TEAM-0001\nrelated_ids: ISSUE-0001\n'
            'authored_by: test\nspec_version: fixture\n---\n' + 'target phrase. ' * 90,
            encoding='utf-8',
        )
        rows = [
            {**passage('DOC-0001#1', 'OCR lost the answer'), 'embedding': [5.0, 0.0]},
            {**passage('DOC-0002#1', 'target phrase', 'DOC-0002'), 'embedding': [0.0, 3.0]},
        ]
        (directory / 'passages.jsonl').write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')

    def test_offline_uses_stored_ocr_embeddings_and_clean_chunking_in_memory(self):
        builder = importlib.import_module('scripts.build_passages')

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.fixture(directory)
            before = {p.name: p.read_bytes() for p in directory.iterdir()}
            embedder = Mock()
            embedder.embed_query.return_value = np.array([1.0, 0.0])
            embedder.embed_passages.side_effect = lambda texts: np.array([[1.0, 0.0]] * len(texts))
            result = self.evaluation.evaluate_offline(GOLD, directory, embedder)
            self.assertEqual(result['cases'][0]['first_relevant_rank'], {'bge_ocr': None, 'bge_clean': 1})
            self.assertEqual(result['variants']['bge_ocr']['document_recall@5'], 1)
            self.assertEqual(result['variants']['bge_clean']['mrr@10'], 1)
            expected = builder.chunk_text(builder.read_document(directory / 'DOC-0001.md')[1])
            embedder.embed_passages.assert_called_once_with(expected)
            embedder.embed_query.assert_called_once_with(CASE['question'])
            self.assertEqual({p.name: p.read_bytes() for p in directory.iterdir()}, before)
            titan = Mock()
            titan.embed_query.return_value = np.array([1.0, 0.0])
            titan.embed_passages.side_effect = lambda texts: np.array([[1.0, 0.0]] * len(texts))
            report = self.evaluation.evaluate_offline(GOLD, directory, embedder, titan=titan)
            self.assertEqual(set(report['variants']), {'bge_ocr', 'bge_clean', 'titan_ocr', 'titan_clean'})
            self.assertEqual(titan.embed_passages.call_args_list[0].args[0], ['OCR lost the answer', 'target phrase'])
            self.assertEqual(titan.embed_passages.call_args_list[1].args[0], expected)
            titan.embed_query.assert_called_once_with(CASE['question'])

    def test_system_uses_all_three_real_repository_entrypoints_and_database_text(self):
        connection = Mock()
        # Gold document present, but its OCR text lost the phrase: document recall only.
        row = passage('DOC-0001#1', 'OCR lost the answer')
        connection.execute.return_value.fetchall.return_value = [row]
        embedder = Mock()
        embedder.embed_query.return_value = np.array([1.0, 0.0], dtype=np.float32)
        repo = repository.Repository(connection)
        with patch.object(repository, 'get_embedder', return_value=embedder):
            result = self.evaluation.evaluate_system(GOLD, repo, embedder)
        self.assertEqual(set(result['variants']), {'keyword', 'vector', 'hybrid'})
        self.assertEqual(connection.execute.call_count, 4)
        for metrics in result['variants'].values():
            self.assertEqual(metrics['passage_recall@5'], 0)
            self.assertEqual(metrics['document_recall@5'], 1)
        with patch.object(repo, 'search_documents', wraps=repo.search_documents) as hybrid:
            with patch.object(repository, 'get_embedder', return_value=embedder):
                self.evaluation.evaluate_system(GOLD, repo, embedder)
            hybrid.assert_called_once_with(CASE['question'], 10, None, None)

    def test_cli_writes_deterministic_report_and_loads_gold_without_using_stdin(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.fixture(directory)
            gold = directory / 'gold.json'
            gold.write_text(json.dumps(GOLD), encoding='utf-8')
            output = directory / 'results/report.json'
            args = [
                '--mode',
                'offline',
                '--gold',
                str(gold),
                '--documents',
                str(directory),
                '--model-dir',
                str(directory / 'model'),
                '--output',
                str(output),
            ]
            with (
                patch.object(self.evaluation, 'Embedder') as embedder,
                patch('sys.stdin', None),
                patch('sys.stdout', new_callable=io.StringIO) as stdout,
            ):
                embedder.return_value.embed_query.return_value = np.array([1.0, 0.0])
                embedder.return_value.embed_passages.side_effect = lambda texts: np.array([[1.0, 0.0]] * len(texts))
                self.assertEqual(self.evaluation.main(args), 0)
                first = output.read_bytes()
                self.assertEqual(self.evaluation.main(args), 0)
                self.assertEqual(output.read_bytes(), first)
                self.assertNotIn('private', stdout.getvalue())
                embedder.assert_called_with(directory / 'model')
            self.assertEqual(json.loads(first)['cases'][0]['id'], 'case-01')

    def test_script_supports_python_dash_with_service_packages_on_import_path(self):
        source = (ROOT / 'scripts/retrieval_eval.py').read_text(encoding='utf-8')
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [sys.executable, '-B', '-', '--help'],
                input=source,
                text=True,
                capture_output=True,
                cwd=temporary,
                timeout=30,
                env={**os.environ, 'PYTHONPATH': str(ROOT)},
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--gold', result.stdout)

    def test_system_cli_uses_service_database_helper_and_repeatable_snapshot(self):
        import psycopg

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            gold = directory / 'gold.json'
            gold.write_text(json.dumps(GOLD), encoding='utf-8')
            output = directory / 'result.json'
            with (
                patch('preflight_core.database.open_database') as database,
                patch.object(self.evaluation, 'get_embedder') as embedder,
                patch.object(self.evaluation, 'evaluate_system', return_value={'variants': {}}) as evaluate,
                patch('sys.stdout', new_callable=io.StringIO),
                patch('sys.stdin', None),
            ):
                self.assertEqual(
                    self.evaluation.main(['--mode', 'system', '--gold', str(gold), '--output', str(output)]), 0
                )
                database.assert_called_once_with(application_name='preflight-oss-retrieval-eval')
                connection = database.return_value.__enter__.return_value
                self.assertEqual(connection.isolation_level, psycopg.IsolationLevel.REPEATABLE_READ)
                self.assertIs(evaluate.call_args.args[1].connection, connection)
                self.assertIs(evaluate.call_args.args[2], embedder.return_value)

    def test_gold_validation_rejects_empty_duplicate_and_blank_phrase_cases(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'gold.json'
            path.write_text(json.dumps(GOLD), encoding='utf-8')
            self.assertEqual(self.evaluation.load_gold(path), GOLD)
            for bad in (
                {},
                {**GOLD, 'cases': []},
                {**GOLD, 'cases': [CASE, CASE]},
                {**GOLD, 'cases': [{**CASE, 'phrases': ['  ']}]},
                {**GOLD, 'cases': [{**CASE, 'doc_ids': []}]},
                {**GOLD, 'cases': [{**CASE, 'question': None}]},
            ):
                path.write_text(json.dumps(bad), encoding='utf-8')
                with self.assertRaises(ValueError):
                    self.evaluation.load_gold(path)

    def test_cli_never_prints_arbitrary_runtime_error_text(self):
        with (
            patch.object(self.evaluation, 'load_gold', side_effect=RuntimeError('private question marker')),
            patch('sys.stderr', new_callable=io.StringIO) as stderr,
        ):
            self.assertEqual(self.evaluation.main(['--mode', 'system', '--output', 'unused.json']), 2)
            self.assertNotIn('private', stderr.getvalue())

    def test_titan_cache_is_keyed_by_model_and_exact_text_without_storing_text(self):
        key = self.evaluation.titan_cache_key
        self.assertEqual(key('model-a', 'private text'), key('model-a', 'private text'))
        self.assertNotEqual(key('model-a', 'private text'), key('model-b', 'private text'))
        self.assertNotEqual(key('model-a', 'private text'), key('model-a', 'Private text'))
        self.assertRegex(key('model-a', 'private text'), r'^[a-f0-9]{64}$')
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            client = Mock()
            client.invoke_model.side_effect = lambda **kwargs: {
                'body': io.BytesIO(json.dumps({'embedding': [1.0] + [0.0] * 1023}).encode())
            }
            with patch.object(self.evaluation.importlib, 'import_module') as importer:
                importer.return_value.client.return_value = client
                titan = self.evaluation.TitanEmbedder(directory, region='ca-central-1')
                importer.assert_not_called()
                first = titan.embed_passages(['private passage marker', 'private passage marker'])
                titan.embed_query('private question marker')
                importer.assert_called_once_with('boto3')
                importer.return_value.client.assert_called_once_with('bedrock-runtime', region_name='ca-central-1')
            self.assertEqual(first.shape, (2, 1024))
            self.assertEqual(client.invoke_model.call_count, 2)
            call = client.invoke_model.call_args.kwargs
            self.assertEqual(call['modelId'], 'amazon.titan-embed-text-v2:0')
            self.assertEqual(
                json.loads(call['body']), {'inputText': CASE['question'], 'dimensions': 1024, 'normalize': True}
            )
            with patch.object(
                self.evaluation.importlib, 'import_module', side_effect=AssertionError('cache must avoid AWS')
            ):
                cached = self.evaluation.TitanEmbedder(directory, region='ca-central-1')
                np.testing.assert_array_equal(cached.embed_passages(['private passage marker'] * 2), first)
                cached.embed_query('private question marker')
            self.assertEqual(len(list(directory.glob('*.json'))), 2)
            for path in directory.glob('*.json'):
                self.assertNotIn('private', path.name + path.read_text())

    def test_boto3_is_imported_only_on_cache_miss_with_clear_missing_dependency_error(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(
                self.evaluation.importlib, 'import_module', side_effect=ModuleNotFoundError('boto3')
            ) as importer,
        ):
            titan = self.evaluation.TitanEmbedder(Path(temporary), region='ca-central-1')
            importer.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, 'boto3.*--titan'):
                titan.embed_query('private question marker')

    def test_titan_errors_do_not_expose_text_and_invalid_vectors_are_not_cached(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with patch.object(self.evaluation.importlib, 'import_module') as importer:
                client = importer.return_value.client.return_value
                client.invoke_model.side_effect = RuntimeError('private question marker')
                titan = self.evaluation.TitanEmbedder(directory, region='ca-central-1')
                with self.assertRaisesRegex(RuntimeError, 'Titan embedding request failed') as error:
                    titan.embed_query(CASE['question'])
                self.assertNotIn('private', str(error.exception))
                self.assertEqual(list(directory.iterdir()), [])
                client.invoke_model.side_effect = None
                client.invoke_model.return_value = {'body': io.BytesIO(b'{"embedding": [0, 0]}')}
                with self.assertRaisesRegex(ValueError, 'Invalid Titan embedding'):
                    titan.embed_query(CASE['question'])
                self.assertEqual(list(directory.iterdir()), [])
