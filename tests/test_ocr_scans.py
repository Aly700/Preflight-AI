"""OCR uses recorded TSV fixtures and mocked subprocesses, never a real binary."""

import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TSV = """level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext
1\t1\t0\t0\t0\t0\t0\t0\t1275\t1650\t-1\t
5\t1\t2\t1\t1\t1\t20\t90\t80\t20\t60\tNext
5\t1\t1\t1\t1\t2\t70\t20\t80\t20\t80.5\tqueue
5\t1\t1\t1\t2\t1\t20\t45\t80\t20\t0\tcontext
5\t1\t1\t1\t1\t1\t20\t20\t40\t20\t99.5\tDemo
5\t1\t2\t1\t1\t2\t90\t90\t10\t20\t-1\t
"""


class OCRScansTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / 'scripts/ocr_scans.py').is_file(), 'Implement the OCR reader')
        with patch.object(sys, 'path', [str(ROOT / 'scripts'), *sys.path]):
            self.ocr = importlib.import_module('ocr_scans')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def test_tsv_lines_follow_hierarchy_then_word_order(self):
        text, confidence = self.ocr.parse_tsv(TSV)
        self.assertEqual(text, 'Demo queue\ncontext\nNext')
        self.assertEqual(confidence, 60)

    def test_decimal_tsv_confidence_ignores_unscored_words_and_preserves_zero(self):
        # Fixed TSV rows exercise the decimal confidences written by Tesseract,
        # including an unscored word (not merely an empty hierarchy row).
        recorded = TSV + '5\t1\t2\t1\t2\t1\t20\t120\t80\t20\t-1\tunscored\n'
        recorded += '5\t1\t2\t1\t3\t1\t20\t150\t80\t20\t92.750000\tword\n'
        text, confidence = self.ocr.parse_tsv(recorded)
        self.assertIn('unscored', text)
        self.assertAlmostEqual(confidence, 66.55)

    def test_annotation_regions_exclude_handwriting_from_printed_ocr(self):
        self.assertTrue(hasattr(self.ocr, 'parse_tsv'), 'TSV parser must expose printed-only selection')
        import inspect

        self.assertIn('excluded_regions', inspect.signature(self.ocr.parse_tsv).parameters)
        # Exclude the last block, preserving the printed lines and their confidence.
        text, confidence = self.ocr.parse_tsv(TSV, excluded_regions=[[0, 85, 200, 115]])
        self.assertEqual(text, 'Demo queue\ncontext')
        self.assertEqual(confidence, 60)

    def test_empty_tsv_has_no_confidence_not_a_fabricated_zero(self):
        self.assertEqual(self.ocr.parse_tsv(TSV.splitlines()[0] + '\n'), ('', None))
        with self.assertRaisesRegex(ValueError, 'TSV'):
            self.ocr.parse_tsv('not a TSV')

    def test_cer_uses_character_edits_and_whitespace_normalization(self):
        for reference, actual, expected in (
            ('kitten', 'sitting', 3 / 6),
            ('abc', 'axc', 1 / 3),
            ('abc', 'ab', 1 / 3),
            ('abc', 'abcd', 1 / 3),
            (' a\n b\t c ', 'a b c', 0),
            ('', '', 0),
            ('a', 'aaa', 2),
            ('Queue', 'queue', 1 / 5),
        ):
            with self.subTest(reference=reference, actual=actual):
                self.assertAlmostEqual(self.ocr.character_error_rate(reference, actual), expected)
        with self.assertRaisesRegex(ValueError, 'empty reference'):
            self.ocr.character_error_rate('', 'text')

    def test_report_aggregates_by_kind_and_ranks_worst_five(self):
        rows = [
            {'doc_id': f'DOC-{i:04}', 'kind': 'memo' if i < 4 else 'form', 'edits': i, 'reference_characters': 10}
            for i in range(1, 8)
        ]
        report = self.ocr.aggregate_report(rows)
        self.assertEqual(report['document_count'], 7)
        self.assertEqual(report['by_kind']['memo']['count'], 3)
        self.assertAlmostEqual(report['by_kind']['memo']['cer'], 0.2)
        self.assertEqual([r['doc_id'] for r in report['worst_five']], [f'DOC-{i:04}' for i in range(7, 2, -1)])
        unequal = self.ocr.aggregate_report(
            [
                {'doc_id': 'DOC-0001', 'kind': 'memo', 'edits': 1, 'reference_characters': 2},
                {'doc_id': 'DOC-0002', 'kind': 'memo', 'edits': 1, 'reference_characters': 8},
            ]
        )
        self.assertAlmostEqual(unequal['by_kind']['memo']['cer'], 0.2)
        self.assertAlmostEqual(unequal['by_kind']['memo']['mean_cer'], 0.3125)

    def test_tesseract_command_has_explicit_english_tsv_and_no_shell(self):
        page = self.directory / 'a page; literal.jpg'
        with patch.object(self.ocr.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, TSV, '')) as run:
            self.assertEqual(
                self.ocr.ocr_page(page),
                {
                    'text': 'Demo queue\ncontext\nNext',
                    'printed_text': 'Demo queue\ncontext\nNext',
                    'mean_word_confidence': 60,
                },
            )
        args, kwargs = run.call_args
        self.assertEqual(args[0], ['tesseract', str(page.resolve()), 'stdout', '-l', 'eng', '--dpi', '150', 'tsv'])
        self.assertFalse(kwargs.get('shell', False))
        self.assertTrue(kwargs['check'])
        self.assertGreater(kwargs['timeout'], 0)

    def test_ocr_manifest_roundtrip_and_report_without_binary(self):
        render = importlib.import_module('render_scans')
        documents = self.directory / 'documents'
        documents.mkdir()
        source = documents / 'DOC-0001.md'
        source.write_text(
            '---\ndoc_id: DOC-0001\nkind: memo\ntitle: Queue\ndate: 2026-08-11\n'
            'author: PERSON-0001\n---\n# Demo queue\n\ncontext Next'
        )
        scans = self.directory / 'scans'
        manifest = render.render_document(source, scans, 123)
        output = documents / 'ocr'
        with patch.object(
            self.ocr.subprocess,
            'run',
            side_effect=[
                subprocess.CompletedProcess([], 0, 'tesseract 5.fixture\nleptonica fixture', ''),
                subprocess.CompletedProcess([], 0, TSV, ''),
            ],
        ):
            self.ocr.run_ocr(scans, output, None)
        result = json.loads((output / 'DOC-0001.json').read_text())
        self.assertEqual(result['renderer_version'], manifest['renderer_version'])
        self.assertEqual(result['seed'], 124)
        self.assertEqual(result['tesseract_version'], 'tesseract 5.fixture')
        self.assertEqual(result['pages'][0]['text'], 'Demo queue\ncontext\nNext')
        self.assertEqual(result['pages'][0]['mean_word_confidence'], 60)
        self.assertIn('scan_sha256', result['pages'][0])
        self.assertEqual(result['pages'][0]['scan_sha256'], manifest['pages'][0]['sha256'])
        reference = manifest['pages'][0]['printed_reference']
        # A perfect transcription of typed content must score zero despite the
        # different body, letterhead/footer and an extra handwritten OCR string.
        result['pages'][0]['printed_text'] = reference
        result['pages'][0]['text'] = reference + '\nD. Worker 0001 COPY'
        (output / 'DOC-0001.json').write_text(json.dumps(result))
        with patch.object(self.ocr.subprocess, 'run', side_effect=AssertionError('Report must not invoke tesseract')):
            report = self.ocr.build_report(documents, output, None, scans=scans)
        self.assertEqual(report['by_kind']['memo']['cer'], 0)
        self.assertEqual(report['documents'][0]['reference_characters'], len(self.ocr.normalize_whitespace(reference)))
        self.assertEqual(report['documents'][0]['pages'][0]['mean_word_confidence'], 60)
        self.assertEqual(json.loads((output / 'report.json').read_text()), report)
        second = documents / 'DOC-0002.md'
        second.write_text(source.read_text().replace('DOC-0001', 'DOC-0002'))
        self.assertEqual(self.ocr.build_report(documents, output, None, scans=scans)['missing_ocr'], ['DOC-0002'])
        (scans / 'DOC-0001-p1.jpg').write_bytes(b'changed scan')
        with (
            patch.object(self.ocr, 'tesseract_version', return_value='tesseract fixture'),
            patch.object(self.ocr, 'ocr_page', side_effect=AssertionError('Must reject before OCR')),
            self.assertRaisesRegex(ValueError, 'scan checksum mismatch'),
        ):
            self.ocr.run_ocr(scans, output, None)
        source.write_text(source.read_text() + '\nChanged')
        with self.assertRaisesRegex(ValueError, 'source changed'):
            self.ocr.build_report(documents, output, None, scans=scans)

    def test_report_rejects_legacy_reference_and_different_render(self):
        render = importlib.import_module('render_scans')
        source = self.directory / 'DOC-0001.md'
        source.write_text(
            '---\ndoc_id: DOC-0001\nkind: memo\ntitle: Queue\ndate: 2026-08-11\nauthor: PERSON-0001\n---\nQueue wording'
        )
        scans, output = self.directory / 'scans', self.directory / 'ocr'
        manifest = render.render_document(source, scans, 123)
        with (
            patch.object(self.ocr, 'tesseract_version', return_value='tesseract fixture'),
            patch.object(self.ocr, 'run_tesseract', return_value=TSV),
        ):
            self.ocr.run_ocr(scans, output, None)
        result = json.loads((output / 'DOC-0001.json').read_text())
        self.assertIn('printed_text', result['pages'][0])
        manifest['pages'][0].pop('printed_reference')
        (scans / 'DOC-0001.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'printed reference'):
            self.ocr.build_report(self.directory, output, None, scans=scans)
        manifest['pages'][0]['printed_reference'] = 'Queue'
        manifest['renderer_version'] = 'other-renderer'
        (scans / 'DOC-0001.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'render changed'):
            self.ocr.build_report(self.directory, output, None, scans=scans)

    def test_missing_tesseract_has_actionable_error(self):
        with (
            patch.object(self.ocr.subprocess, 'run', side_effect=FileNotFoundError),
            self.assertRaisesRegex(ValueError, 'tesseract.*tools/ingest'),
        ):
            self.ocr.tesseract_version()


if __name__ == '__main__':
    unittest.main()
