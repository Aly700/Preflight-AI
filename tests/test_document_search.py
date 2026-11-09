"""Offline search contracts: fusion, bound SQL, lazy embeddings and the real MCP handler."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import json
import threading
from typing import Any
import unittest
from unittest.mock import Mock, patch

from mcp import Client, MCPError
import numpy as np
from pydantic import ValidationError

from mcp_service import embeddings, repository
from mcp_service.server import create_server
from preflight_core.models import RECORD_ID
from preflight_core.tools import TOOLS

KINDS = ('policy', 'glossary', 'handover', 'memo', 'coordination', 'retrospective', 'form')
PASSAGE = {
    'passage_id': 'DOC-0008#2',
    'doc_id': 'DOC-0008',
    'title': 'Fictional queue sweep',
    'kind': 'coordination',
    'date': '2026-10-01',
    'station': None,
    'program': None,
    'related_ids': ['ISSUE-0001', 'TASK-0001'],
    'text': 'The Friday queue sweep starts at 14:00.',
}
DOCUMENT_RESULT = {'results': [{**PASSAGE, 'score': 2 / 61, 'keyword_rank': 1, 'vector_rank': 1}]}


class FusionTests(unittest.TestCase):
    def fuse(self, vector, keyword, top_k):
        self.assertTrue(hasattr(repository, 'fuse_passages'), 'Implement reciprocal rank fusion')
        return repository.fuse_passages(vector, keyword, top_k)

    def test_ranks_scores_ties_and_single_list_candidates(self):
        a, b, c, d = [{**PASSAGE, 'passage_id': f'DOC-0008#{i}'} for i in range(1, 5)]
        vector, keyword = [b, a, c], [a, b, d]
        original = deepcopy((vector, keyword))
        rows = self.fuse(vector, keyword, 8)
        self.assertEqual([r['passage_id'] for r in rows], [r['passage_id'] for r in (a, b, c, d)])
        self.assertEqual([(r['vector_rank'], r['keyword_rank']) for r in rows], [(2, 1), (1, 2), (3, None), (None, 3)])
        for row, score in zip(rows, [1 / 61 + 1 / 62, 1 / 61 + 1 / 62, 1 / 63, 1 / 63], strict=True):
            self.assertAlmostEqual(row['score'], score)
            self.assertEqual(row['text'], PASSAGE['text'])
        self.assertEqual((vector, keyword), original)
        self.assertEqual(self.fuse(vector, keyword, 1), rows[:1])
        self.assertEqual(self.fuse(vector, keyword, 2), rows[:2])

    def test_empty_lists_and_one_list_only(self):
        self.assertEqual(self.fuse([], [], 5), [])
        for vector, keyword, ranks in (([PASSAGE], [], (1, None)), ([], [PASSAGE], (None, 1))):
            rows = self.fuse(vector, keyword, 5)
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]['vector_rank'], rows[0]['keyword_rank']), ranks)
            self.assertEqual(rows[0]['score'], 1 / 61)


class SearchSQLTests(unittest.TestCase):
    def test_both_candidate_lists_bind_query_vector_and_filters_before_limits(self):
        self.assertTrue(hasattr(repository.Repository, 'search_documents'), 'Implement repository search')
        query = "Friday sweep? '); DROP TABLE preflight.passages; --"
        vector = np.array([1.0] + [0.0] * 383, dtype=np.float32)
        for kind, related in ((None, None), ('coordination', None), (None, 'ISSUE-0001'), ('memo', 'DOC-0001#2')):
            with self.subTest(kind=kind, related=related):
                connection = Mock()
                connection.execute.return_value.fetchall.side_effect = [[PASSAGE], [PASSAGE]]
                with patch.object(repository, 'get_embedder') as embedder:
                    embedder.return_value.embed_query.return_value = vector
                    result = repository.Repository(connection).search_documents(query, 3, kind, related)
                    embedder.return_value.embed_query.assert_called_once_with(query)
                self.assertEqual(result, DOCUMENT_RESULT)
                self.assertEqual(connection.execute.call_count, 2)
                vector_call, keyword_call = connection.execute.call_args_list
                vector_sql, vector_args = vector_call.args
                keyword_sql, keyword_args = keyword_call.args
                vector_sql, keyword_sql = ' '.join(vector_sql.split()), ' '.join(keyword_sql.split())
                self.assertIn('ORDER BY p.embedding <=> %s::vector', vector_sql)
                self.assertEqual(json.loads(vector_args[-1]), vector.tolist())
                self.assertIn("replace(plainto_tsquery('english', %s)::text, ' & ', ' | ')::tsquery", keyword_sql)
                self.assertIn('p.text_search @@', keyword_sql)
                self.assertRegex(keyword_sql, r'ORDER BY ts_rank_cd\(p.text_search, [^)]+\) DESC, p.passage_id')
                self.assertEqual(keyword_args[0], query)
                for sql, args in ((vector_sql, vector_args), (keyword_sql, keyword_args)):
                    self.assertNotIn(query, sql)
                    self.assertEqual(sql.count('%s'), len(args))
                    self.assertIn('LIMIT 20', sql)
                    self.assertIn('preflight.passages p', sql)
                    self.assertIn('JOIN preflight.documents d', sql)
                    self.assertIn('d.doc_date AS date', sql)
                    for value, clause in ((kind, 'd.kind = %s'), (related, 'd.related_ids @> ARRAY[%s]')):
                        if value is not None:
                            self.assertIn(clause, sql)
                            self.assertLess(sql.index(clause), sql.index('LIMIT 20'))
                            self.assertIn(value, args)
                            self.assertNotIn(value, sql)
                        else:
                            self.assertNotIn(clause, sql)

    def test_repository_applies_top_k_after_fusing(self):
        self.assertTrue(hasattr(repository.Repository, 'search_documents'))
        connection = Mock()
        a, b = [{**PASSAGE, 'passage_id': f'DOC-0008#{i}'} for i in (1, 2)]
        connection.execute.return_value.fetchall.side_effect = [[a, b], [b]]
        with patch.object(repository, 'get_embedder') as embedder:
            embedder.return_value.embed_query.return_value = np.ones(384, dtype=np.float32)
            result = repository.Repository(connection).search_documents('sweep', 1, None, None)
        self.assertEqual(len(result['results']), 1)
        self.assertEqual(result['results'][0]['passage_id'], b['passage_id'])
        self.assertAlmostEqual(result['results'][0]['score'], 1 / 62 + 1 / 61)


class LazyEmbedderTests(unittest.TestCase):
    def test_one_lazy_initialisation_across_concurrent_callers(self):
        self.assertTrue(hasattr(embeddings, 'get_embedder'), 'Provide a thread-safe lazy process singleton')
        barrier = threading.Barrier(8)
        with patch.object(embeddings, '_embedder', None), patch.object(embeddings, 'Embedder') as constructor:
            constructor.assert_not_called()

            def load():
                barrier.wait(timeout=5)
                return embeddings.get_embedder()

            with ThreadPoolExecutor(max_workers=8) as pool:
                loaded = list(pool.map(lambda _: load(), range(8)))
            constructor.assert_called_once_with()
            self.assertTrue(all(item is constructor.return_value for item in loaded))


class DocumentContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repo = Mock()
        self.repo.search_documents.return_value = DOCUMENT_RESULT

        @contextmanager
        def factory():
            yield self.repo

        self.server = create_server(factory)

    async def test_discovery_schema_description_defaults_and_worker_thread(self):
        self.assertIn('search_documents', TOOLS)
        self.assertEqual(TOOLS['search_documents'].arguments, frozenset({'query', 'top_k', 'kind', 'related_id'}))
        loop_thread = threading.get_ident()
        worker_threads = []

        def search(*args):
            worker_threads.append(threading.get_ident())
            return DOCUMENT_RESULT

        self.repo.search_documents.side_effect = search
        async with Client(self.server) as client:
            listing = await client.list_tools()
            tool = next(t for t in listing.tools if t.name == 'search_documents')
            props = tool.input_schema['properties']
            self.assertEqual((props['query']['minLength'], props['query']['maxLength']), (3, 300))
            self.assertEqual(
                (props['top_k']['minimum'], props['top_k']['maximum'], props['top_k']['default']), (1, 8, 5)
            )
            self.assertEqual(tool.input_schema['required'], ['query'])
            self.assertIsNotNone(tool.description)
            for phrase in (
                'policies',
                'coordination process',
                'decisions',
                'handovers',
                'meeting outcomes',
                'definitions',
                'counts',
                'statuses',
                'current values',
            ):
                self.assertIn(phrase, tool.description or '')
            result = await client.call_tool('search_documents', {'query': 'Friday sweep'})
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content, DOCUMENT_RESULT)
        self.repo.search_documents.assert_called_once_with('Friday sweep', 5, None, None)
        self.assertNotEqual(worker_threads, [loop_thread])
        self.assertEqual(len(worker_threads), 1)

    async def test_valid_input_boundaries_kinds_and_related_ids(self):
        self.assertIn('search_documents', TOOLS)
        async with Client(self.server) as client:
            for kind in KINDS:
                result = await client.call_tool('search_documents', {'query': 'abc', 'kind': kind, 'top_k': 1})
                self.assertFalse(result.is_error)
                self.repo.search_documents.assert_called_with('abc', 1, kind, None)
            for ident in (
                'ISSUE-0001',
                'TASK-0001',
                'EVT-1',
                'DEMO-AIR-0001',
                'TEAM-1',
                'PERSON-1',
                'SHIFT-1',
                'DOC-0001',
                'DOC-0001#2',
            ):
                result = await client.call_tool(
                    'search_documents', {'query': 'x' * 300, 'top_k': 8, 'related_id': ident}
                )
                self.assertFalse(result.is_error)
                self.repo.search_documents.assert_called_with('x' * 300, 8, None, ident)

    async def test_invalid_inputs_never_reach_repository(self):
        self.assertIn('search_documents', TOOLS)
        cases: list[dict[str, Any]] = [{}, *[{'query': q} for q in ('', 'ab', 'x' * 301, 123, None)]]
        cases += [{'query': 'sweep', 'top_k': k} for k in (0, 9, '5', True, 2.5)]
        cases += [{'query': 'sweep', 'kind': k} for k in ('procedure', 'POLICY', '')]
        cases += [
            {'query': 'sweep', 'related_id': r}
            for r in ('DOC-1', 'DOC-00001', 'DOC-0001#', 'ISSUE-1#2', "x' OR 1=1 --", 'DEMO-STATION-01')
        ]
        cases += [{'query': 'sweep', 'sql': 'SELECT 1'}]
        async with Client(self.server) as client:
            for arguments in cases:
                with self.subTest(arguments=arguments):
                    try:
                        result = await client.call_tool('search_documents', arguments)
                    except MCPError as error:
                        self.assertEqual(error.code, -32602)
                    else:
                        self.assertTrue(result.is_error)
        self.repo.search_documents.assert_not_called()

    def test_typed_results_reject_malformed_passages(self):
        self.assertIn('search_documents', TOOLS)
        model = TOOLS['search_documents'].result
        self.assertEqual(model.model_validate(DOCUMENT_RESULT).model_dump(mode='json'), DOCUMENT_RESULT)
        for change in (
            {'passage_id': 'DOC-0008'},
            {'doc_id': 'DOC-8'},
            {'kind': 'procedure'},
            {'date': 'yesterday'},
            {'related_ids': ['untrusted']},
            {'score': float('nan')},
            {'keyword_rank': 0},
            {'vector_rank': 21},
            {'sql': 'SELECT 1'},
        ):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                model.model_validate({'results': [{**DOCUMENT_RESULT['results'][0], **change}]})
        with self.assertRaises(ValidationError):
            model.model_validate({**DOCUMENT_RESULT, 'sql': 'SELECT 1'})

    def test_record_id_pattern_accepts_documents_and_passages_only_in_doc_namespace(self):
        for ident in ('DOC-0001', 'DOC-9999#12', 'ISSUE-1', 'DEMO-AIR-0001'):
            self.assertIsNotNone(RECORD_ID.fullmatch(ident), ident)
        for ident in ('DOC-1', 'DOC-00001', 'DOC-0001#', 'DOC-0001#2x', 'ISSUE-1#2'):
            self.assertIsNone(RECORD_ID.fullmatch(ident), ident)
