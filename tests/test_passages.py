"""Passage boundaries and record provenance are independent of OCR accuracy."""

from collections import defaultdict
import hashlib
import importlib
from itertools import pairwise
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
WORD = re.compile(r"\b\w+(?:[-']\w+)*\b")
FIELDS = set('passage_id doc_id ordinal kind title date station program author team related_ids text embedding'.split())


class FakeEmbedder:
    def embed_passages(self, texts):
        return np.full((len(texts), 384), 0.123456789, dtype=np.float32)


class PassageTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / 'scripts/build_passages.py').is_file(), 'Implement the passage builder')
        self.builder = importlib.import_module('scripts.build_passages')

    def test_cleanup_drops_short_noise_keeps_real_lines_and_joins_hyphenation(self):
        text = '|\n—\n1\nx 2\nABC\n123\nA real line.\ncoor-\n|\ndination   desk\nISSUE-0084\n'
        self.assertEqual(self.builder.clean_ocr(text), 'ABC 123 A real line. coordination desk ISSUE-0084')
        self.assertEqual(self.builder.clean_ocr('  déjà vu\n中文文\n!!!'), 'déjà vu 中文文')

    def test_word_limit_exact_overlap_and_complete_coverage(self):
        words = [f'word{i}' for i in range(351)]
        chunks = self.builder.chunk_text(' '.join(words))
        self.assertEqual([len(WORD.findall(chunk)) for chunk in chunks], [120, 120, 120, 51])
        recovered = WORD.findall(chunks[0])
        for previous, current in pairwise(chunks):
            self.assertEqual(WORD.findall(previous)[-20:], WORD.findall(current)[:20])
            recovered.extend(WORD.findall(current)[20:])
        self.assertEqual(recovered, words)

    def test_prefers_last_sentence_boundary_and_makes_progress(self):
        text = ' '.join(f'word{i}' + ('.' if i in (59, 104, 129, 159, 180) else '') for i in range(181))
        chunks = self.builder.chunk_text(text)
        self.assertEqual(len(WORD.findall(chunks[0])), 105)
        self.assertTrue(chunks[0].endswith('word104.'))
        self.assertEqual(WORD.findall(chunks[0])[-20:], WORD.findall(chunks[1])[:20])
        awkward = self.builder.chunk_text('Short. ' + 'long ' * 300)
        self.assertTrue(all(20 < len(WORD.findall(chunk)) <= 120 for chunk in awkward))
        self.assertEqual(self.builder.chunk_text(''), [])
        self.assertEqual(self.builder.chunk_text('— |'), [])
        self.assertEqual(self.builder.chunk_text('Only one sentence.'), ['Only one sentence.'])

    def fixture(self, directory):
        (directory / 'ocr').mkdir()
        for ident, label in [('DOC-0002', 'second'), ('DOC-0001', 'first')]:
            source = (
                '---\n' + f'doc_id: {ident}\nkind: memo\ntitle: Fixture {label}\ndate: 2026-10-01\n'
                'author: PERSON-0001\nteam: TEAM-0001\nrelated_ids: ISSUE-0001, TASK-0001\n'
                'authored_by: Preflight AI project\nspec_version: documents-2026-11-08.1\n---\n'
                + f'{label} clean-body '
                * 150
            )
            (directory / f'{ident}.md').write_text(source)
            ocr = {
                'doc_id': ident,
                'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
                'pages': [{'page_number': 1, 'text': f'{label} ISSUE-9999 PERSON-9999 TEAM-9999 ' * 90}],
            }
            (directory / 'ocr' / f'{ident}.json').write_text(json.dumps(ocr))

    def test_build_is_byte_identical_ids_reset_and_metadata_comes_only_from_front_matter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            first, second = root / 'first.jsonl', root / 'second.jsonl'
            self.builder.build_passages(root, first, FakeEmbedder())
            self.builder.build_passages(root, second, FakeEmbedder())
            self.assertEqual(first.read_bytes(), second.read_bytes())
            rows = [json.loads(line) for line in first.read_text().splitlines()]
            by_doc = defaultdict(list)
            for row in rows:
                self.assertEqual(set(row), FIELDS)
                self.assertEqual(row['related_ids'], ['ISSUE-0001', 'TASK-0001'])
                self.assertEqual(row['author'], 'PERSON-0001')
                self.assertEqual(row['team'], 'TEAM-0001')
                self.assertIsNone(row['station'])
                self.assertIsNone(row['program'])
                self.assertEqual(row['embedding'], [0.123457] * 384)
                by_doc[row['doc_id']].append(row)
            self.assertEqual(list(by_doc), ['DOC-0001', 'DOC-0002'])
            for ident, passages in by_doc.items():
                for ordinal, row in enumerate(passages, 1):
                    self.assertEqual(row['passage_id'], f'{ident}#{ordinal}')
                    self.assertEqual(row['ordinal'], ordinal)
                    self.assertNotIn('second' if ident == 'DOC-0001' else 'first', row['text'])
            self.builder.build_passages(root, second, FakeEmbedder(), clean=True)
            self.assertNotEqual(first.read_bytes(), second.read_bytes())
            self.assertNotIn('ISSUE-9999', second.read_text())
            self.assertIn('clean-body', second.read_text())

    def test_clean_cli_requires_separate_output_before_loading_a_model(self):
        for extra in ([], ['--output', str(ROOT / 'data/documents/passages.jsonl')]):
            result = subprocess.run(
                [sys.executable, '-B', 'scripts/build_passages.py', '--clean', *extra],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn('--clean requires a separate --output', result.stderr)

    def test_checked_in_passages_cover_every_document_and_keep_front_matter_ids(self):
        path = ROOT / 'data/documents/passages.jsonl'
        self.assertTrue(path.is_file(), 'Build the OCR passage fixture')
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        by_doc = defaultdict(list)
        for row in rows:
            by_doc[row['doc_id']].append(row)
            self.assertEqual(set(row), FIELDS)
            self.assertTrue(0 < len(WORD.findall(row['text'])) <= 120)
            vector = np.array(row['embedding'])
            self.assertEqual(vector.shape, (384,))
            self.assertTrue(np.isfinite(vector).all())
            self.assertAlmostEqual(float(np.linalg.norm(vector)), 1, places=5)
            self.assertTrue(all(round(value, 6) == value for value in row['embedding']))
        sources = sorted((ROOT / 'data/documents').glob('DOC-*.md'))
        self.assertEqual(list(by_doc), [path.stem for path in sources])
        for source in sources:
            # Independent, minimal front-matter oracle; never extract identities from OCR.
            header = dict(line.split(': ', 1) for line in source.read_text().split('---')[1].strip().splitlines())
            for ordinal, row in enumerate(by_doc[source.stem], 1):
                self.assertEqual(row['passage_id'], f'{source.stem}#{ordinal}')
                self.assertEqual(row['ordinal'], ordinal)
                for key in ('kind', 'title', 'date', 'author', 'team', 'station', 'program'):
                    self.assertEqual(row[key], header.get(key))
                self.assertEqual(row['related_ids'], [value.strip() for value in header['related_ids'].split(',')])
