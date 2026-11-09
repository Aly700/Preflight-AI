"""Offline rendering contracts; authored fixtures never enter the corpus."""

from dataclasses import replace
import importlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


class RenderScansTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / 'scripts/render_scans.py').is_file(), 'Implement the scan renderer')
        with patch.object(sys, 'path', [str(ROOT / 'scripts'), *sys.path]):
            self.render = importlib.import_module('render_scans')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def fixture(self, body='Queue context remains with the fictional team.', kind='memo'):
        path = self.directory / 'DOC-0001.md'
        path.write_text(
            f'---\ndoc_id: DOC-0001\nkind: {kind}\ntitle: Queue context\ndate: 2026-08-11\n'
            'program: DEMO-PROGRAM-ALPHA\nauthor: PERSON-0025\nteam: TEAM-0001\n'
            'related_ids: TEAM-0001\nauthored_by: fixture / fixture\nspec_version: fixture\n---\n' + body,
            encoding='utf-8',
        )
        return path

    def test_font_checksums_verified_before_loading(self):
        fonts = self.directory / 'fonts'
        shutil.copytree(ROOT / 'assets/fonts', fonts)
        self.assertEqual(len(self.render.verify_fonts(fonts)), 4)
        target = fonts / 'courierprime/CourierPrime-Regular.ttf'
        target.write_bytes(target.read_bytes() + b'changed')
        with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch.*CourierPrime-Regular'):
            self.render.load_fonts(fonts)

    def test_missing_checksum_is_rejected(self):
        fonts = self.directory / 'fonts'
        shutil.copytree(ROOT / 'assets/fonts', fonts)
        (fonts / 'README.md').write_text('No checksums here.\n')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.render.load_fonts(fonts)

    def test_layout_wraps_and_paginates_without_losing_text(self):
        body = '# Queue notes\n\n' + ' '.join(f'word{n}' for n in range(1500))
        document = self.render.read_document(self.fixture(body))
        fonts = self.render.load_fonts()
        pages = self.render.layout_document(document, fonts)
        self.assertGreater(len(pages), 2)
        text = ' '.join(box.text for page in pages for box in page.boxes)
        for n in range(1500):
            self.assertIn(f'word{n}', text.split())
        for page in pages:
            for box in page.boxes:
                self.assertGreaterEqual(box.x, 130)
                self.assertLessEqual(box.x + box.width, 1145)
                self.assertLessEqual(box.y + box.height, 1425)
                self.assertLessEqual(fonts[box.style].getlength(box.text), box.width + 1)

    def test_markdown_lists_fields_and_table_preserve_content(self):
        body = '# Queue\n\nFirst paragraph\ncontinues here.\n\n- One item\n2. Two items\n\n'
        body += 'Owner: Demo Worker\nComment: \n\n| Label | Value |\n| --- | --- |\n| Team | Fictional |'
        doc = self.render.read_document(self.fixture(body, 'form'))
        blocks = self.render.parse_body(doc.body)
        self.assertEqual(
            [block.kind for block in blocks], ['heading', 'paragraph', 'list', 'list', 'field', 'field', 'table']
        )
        self.assertEqual(
            self.render.clean_body_text(doc.body),
            'Queue\nFirst paragraph continues here.\nOne item\nTwo items\n'
            'Owner: Demo Worker\nComment:\nLabel Value\nTeam Fictional',
        )
        pages = self.render.layout_document(doc, self.render.load_fonts())
        self.assertTrue(any(box.style == 'hand' and box.text == 'Demo Worker' for p in pages for box in p.boxes))
        self.assertTrue(any(box.style == 'label' and box.text == 'Owner:' for p in pages for box in p.boxes))
        self.assertGreater(sum(len(page.rules) for page in pages), 4)

    def test_long_form_and_table_cells_paginate(self):
        body = 'Context: ' + ' '.join(f'form{n}' for n in range(500))
        body += '\n\n| Context | Note |\n| --- | --- |\n| ' + ' '.join(f'cell{n}' for n in range(600)) + ' | End |'
        doc = self.render.read_document(self.fixture(body, 'form'))
        pages = self.render.layout_document(doc, self.render.load_fonts())
        self.assertGreater(len(pages), 2)
        words = ' '.join(box.text for p in pages for box in p.boxes).split()
        self.assertIn('form499', words)
        self.assertIn('cell599', words)
        self.assertTrue(all(box.y + box.height <= 1425 for p in pages for box in p.boxes))

    def test_same_seed_produces_identical_jpegs_and_manifest(self):
        path = self.fixture()
        outputs = [self.directory / name for name in ('first', 'second', 'other')]
        manifests = [
            self.render.render_document(path, out, seed) for out, seed in zip(outputs, (50, 50, 51), strict=True)
        ]
        first, second, other = [(out / 'DOC-0001-p1.jpg').read_bytes() for out in outputs]
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertEqual(manifests[0], manifests[1])
        self.assertEqual(manifests[0]['seed'], 51)
        self.assertEqual(manifests[0]['renderer_version'], self.render.RENDERER_VERSION)
        self.assertEqual((outputs[0] / 'DOC-0001.json').read_bytes(), (outputs[1] / 'DOC-0001.json').read_bytes())
        with Image.open(outputs[0] / 'DOC-0001-p1.jpg') as image:
            self.assertEqual(image.size, (1275, 1650))
            self.assertEqual(image.info['dpi'], (150, 150))

    def test_each_effect_is_pure_deterministic_and_preserves_array_contract(self):
        source = np.full((180, 140, 3), 245, dtype=np.uint8)
        source[40:100, 40:100] = 35
        original = source.copy()
        for seed in range(8):
            params = self.render.scan_parameters(seed)
            for effect in self.render.SCAN_EFFECTS:
                with self.subTest(effect=effect.__name__, seed=seed):
                    result = effect(source, params)
                    self.assertEqual(result.shape, source.shape)
                    self.assertEqual(result.dtype, np.uint8)
                    self.assertGreaterEqual(result.min(), 0)
                    self.assertLessEqual(result.max(), 255)
                    np.testing.assert_array_equal(source, original)
                    np.testing.assert_array_equal(result, effect(source, params))

    def test_rotation_noise_blur_and_compression_caps(self):
        source = np.full((100, 100, 3), 128, dtype=np.uint8)
        for seed in range(100):
            params = self.render.scan_parameters(seed)
            self.assertGreaterEqual(abs(params.rotation), 0.4)
            self.assertLessEqual(abs(params.rotation), 1.2)
            self.assertLessEqual(params.blur, 0.4)
            self.assertLessEqual(params.noise, 4)
            self.assertTrue(60 <= params.quality <= 78)
            self.assertTrue(0.972 <= params.scale <= 0.98)
            self.assertTrue(0.3 <= params.shadow <= 0.48)
            self.assertTrue(0.16 <= params.bleed <= 0.24)
            self.assertTrue(all(abs(offset) <= 8 for offset in params.offset))
            noisy = self.render.paper_grain(source, params)
            self.assertLessEqual(np.abs(noisy.astype(int) - source).max(), 4)

    def test_scanner_bed_is_visible_without_darkening_the_page_center(self):
        source = np.full((330, 255, 3), 250, dtype=np.uint8)
        params = self.render.scan_parameters(18)
        scanned = self.render.scan_geometry(source, params)
        self.assertLess(float(scanned[:8].mean()), 210)
        self.assertLess(float(scanned[:, :8].mean()), 210)
        self.assertGreater(float(scanned[70:260, 60:190].mean()), 245)
        # A soft edge includes intermediate shadow tones, not a binary cutout.
        self.assertGreater(int(((scanned[:, :, 0] > 100) & (scanned[:, :, 0] < 220)).sum()), 50)

    def test_ink_density_can_fade_and_darken_without_changing_white_paper(self):
        source = np.full((180, 140, 3), 65, dtype=np.uint8)
        source[:10] = 255
        result = self.render.uneven_ink(source, self.render.scan_parameters(29))
        self.assertLess(int(result[10:].min()), 50)
        self.assertGreater(int(result[10:].max()), 80)
        np.testing.assert_array_equal(result[:10], source[:10])

    def test_streak_is_faint_and_at_most_two_rows(self):
        source = np.full((180, 140, 3), 240, dtype=np.uint8)
        params = replace(self.render.scan_parameters(29), artifact='streak')
        result = self.render.scanner_marks(source, params)
        difference = source.astype(int) - result.astype(int)
        rows = np.any(difference != 0, axis=(1, 2))
        self.assertTrue(1 <= int(rows.sum()) <= 2)
        self.assertTrue(4 <= int(difference.max()) <= 12)

    def test_existing_title_in_paper_header_is_not_duplicated(self):
        fonts = self.render.load_fonts()
        for prefix in (
            '# Queue context',
            'POLICY 1 — Queue context',
            'POLICY 1\nQueue context',
            'MEMORANDUM\nSubject: Queue context',
            'Form: Queue context',
        ):
            with self.subTest(prefix=prefix):
                document = self.render.read_document(self.fixture(prefix + '\n\nKeep the question visible.'))
                text = ' '.join(b.text for p in self.render.layout_document(document, fonts) for b in p.boxes)
                self.assertEqual(text.count('Queue context'), 1)
                self.assertIn('Keep the question visible.', text)

    def test_form_record_ids_are_printed_and_blanks_keep_their_boxes(self):
        body = 'Aircraft: DEMO-AIR-0001\nIssue: ISSUE-0001\nContext: See TASK-0001 for the question.\n'
        body += 'Owner: Demo Worker 0025\nComment: \nFollow-up: '
        document = self.render.read_document(self.fixture(body, 'form'))
        pages = self.render.layout_document(document, self.render.load_fonts())
        boxes = [b for p in pages for b in p.boxes]
        for token in ('DEMO-AIR-0001', 'ISSUE-0001', 'TASK-0001'):
            containing = [b for b in boxes if token in b.text]
            self.assertTrue(containing)
            self.assertTrue(all(b.style != 'hand' for b in containing))
        self.assertTrue(any(b.style == 'hand' and b.text == 'Demo Worker 0025' for b in boxes))
        self.assertGreaterEqual(sum(b.style == 'hand' and not b.text for b in boxes), 2)

    def test_signature_and_annotations_use_names_and_seeded_clerical_marks(self):
        self.assertTrue(hasattr(self.render, 'prepare_page'), 'Need a shared printed/handwritten page layout')
        fonts = self.render.load_fonts()
        document = self.render.read_document(self.fixture('Owner: Demo Worker 0025\nNote: carry the question', 'form'))
        page = self.render.layout_document(document, fonts)[0]
        params = replace(self.render.scan_parameters(29), handwriting=True, margin_note=True, stamp='COPY')
        prepared = self.render.prepare_page(document, page, 1, 2, fonts, params, ('Demo Worker 0025', 'Demo Team 01'))
        hands = [b for b in prepared.boxes if b.style == 'hand']
        self.assertTrue(any(b.text in ('D. Worker 0025', 'DW 25') for b in hands))
        self.assertFalse(any('PERSON-' in b.text or 'Queue context' in b.text for b in hands))
        self.assertTrue(any(b.text in ('seen', 'file', 'copy to desk lead', 'ok') for b in hands))
        typed = '\n'.join(b.text for b in prepared.boxes if b.style != 'hand')
        self.assertIn('Demo Worker 0025, Demo Team 01', typed)
        self.assertIn('Page 1 of 2', typed)
        # Variation is bounded, fitted to the cell and repeatable.
        repeated = self.render.prepare_page(document, page, 1, 2, fonts, params, ('Demo Worker 0025', 'Demo Team 01'))
        self.assertEqual(prepared, repeated)
        for original, changed in zip([b for b in page.boxes if b.style == 'hand'], hands, strict=False):
            self.assertLessEqual(abs(original.y - changed.y), 3)
            self.assertLessEqual(abs(changed.font_size - fonts['hand'].size), 2)
        self.assertGreater(len({b.font_size for b in hands}), 1)
        self.assertTrue(
            any(b.y != a.y for a, b in zip([b for b in page.boxes if b.style == 'hand'], hands, strict=False))
        )
        # The form's stamp may cross a ruled cell edge without hiding its printed label.
        stamp = prepared.stamps[0]
        self.assertTrue(any(min(rule[1], rule[3]) <= stamp.y <= max(rule[1], rule[3]) for rule in page.rules))

    def test_annotation_bounds_follow_the_same_transform_as_the_image(self):
        bounds = (450, 650, 950, 690)
        mask = Image.new('L', self.render.PAGE_SIZE, 0)
        from PIL import ImageDraw

        ImageDraw.Draw(mask).rectangle(bounds, fill=255)
        for seed in (1, 19, 71):
            params = self.render.scan_parameters(seed)
            actual = np.asarray(self.render.page_transform(mask, params))
            ys, xs = np.nonzero(actual > 10)
            left, top, right, bottom = self.render.scan_region(bounds, params)
            self.assertLessEqual(left, int(xs.min()))
            self.assertLessEqual(top, int(ys.min()))
            self.assertGreaterEqual(right, int(xs.max()))
            self.assertGreaterEqual(bottom, int(ys.max()))
            self.assertLess(right - left, 520)
            self.assertLess(bottom - top, 65)

    def test_unknown_author_and_team_mismatch_fail_before_rendering(self):
        source = self.fixture()
        output = self.directory / 'scans'
        source.write_text(source.read_text().replace('PERSON-0025', 'PERSON-9999'))
        with self.assertRaisesRegex(ValueError, 'Author or team'):
            self.render.render_document(source, output, 50)
        self.assertFalse(output.exists())
        source = self.fixture()
        source.write_text(source.read_text().replace('team: TEAM-0001', 'team: TEAM-0002'))
        with self.assertRaisesRegex(ValueError, 'author/team mismatch'):
            self.render.render_document(source, output, 50)
        self.assertFalse(output.exists())

    def test_manifest_has_all_printed_page_text_but_no_handwriting_or_stamps(self):
        body = 'Aircraft: DEMO-AIR-0001\nNote: handwritten only\nBlank: \n' * 15
        path = self.fixture(body, 'form')
        manifest = self.render.render_document(path, self.directory / 'scans', 50)
        self.assertGreater(len(manifest['pages']), 1)
        for number, page in enumerate(manifest['pages'], 1):
            self.assertIn('printed_reference', page)
            reference = page['printed_reference']
            self.assertIn('Preflight Demo Program', reference)
            self.assertIn('DOC-0001', reference)
            self.assertIn('Demo Worker 0025, Demo Team 01', reference)
            self.assertIn(f'Page {number} of {len(manifest["pages"])}', reference)
            self.assertNotIn('handwritten only', reference)
            self.assertNotIn('D. Worker', reference)
            self.assertNotIn('RECEIVED', reference)
            self.assertTrue(page['excluded_regions'])
        reference = '\n'.join(p['printed_reference'] for p in manifest['pages'])
        self.assertEqual(reference.count('DEMO-AIR-0001'), 15)
        self.assertEqual(reference.count('Note:'), 15)

    def test_cli_looks_up_names_from_the_selected_synthetic_directory(self):
        self.fixture()
        (self.directory / 'spec.json').write_text('{"seed": 200}')
        synthetic = self.directory / 'synthetic'
        synthetic.mkdir()
        (synthetic / 'people.csv').write_text(
            'person_id,display_name,team_id\nPERSON-0025,Demo Worker 0125,TEAM-0001\n'
        )
        (synthetic / 'teams.csv').write_text('team_id,team_name\nTEAM-0001,Demo Team 91\n')
        output = self.directory / 'scans'
        result = subprocess.run(
            [
                sys.executable,
                '-B',
                str(ROOT / 'scripts/render_scans.py'),
                '--documents',
                str(self.directory),
                '--synthetic',
                str(synthetic),
                '--output',
                str(output),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        manifest = json.loads((output / 'DOC-0001.json').read_text())
        self.assertIn('Demo Worker 0125, Demo Team 91', manifest['pages'][0]['printed_reference'])

    def test_grayscale_proportion_across_spec_slots(self):
        spec = json.loads((ROOT / 'data/documents/spec.json').read_text())
        gray = sum(self.render.scan_parameters(spec['seed'] + int(s['doc_id'][4:])).grayscale for s in spec['slots'])
        self.assertTrue(15 <= gray <= 25, gray)
        source = np.full((20, 20, 3), [250, 245, 233], dtype=np.uint8)
        for seed in range(20):
            params = self.render.scan_parameters(seed)
            result = self.render.scan_color(source, params)
            self.assertEqual(bool(np.array_equal(result[:, :, 0], result[:, :, 2])), params.grayscale)

    def test_only_selection_and_invalid_document_ids(self):
        self.fixture()
        self.assertEqual([p.name for p in self.render.select_documents(self.directory, 'DOC-0001')], ['DOC-0001.md'])
        with self.assertRaisesRegex(ValueError, 'missing'):
            self.render.select_documents(self.directory, 'DOC-0002')
        with self.assertRaisesRegex(ValueError, 'document ID'):
            self.render.select_documents(self.directory, '../DOC-0001')
        path = self.fixture().with_name('DOC-0002.md')
        path.symlink_to(self.directory / 'DOC-0001.md')
        with self.assertRaisesRegex(ValueError, 'symbolic link'):
            self.render.read_document(path)

    def test_kind_selects_serif_or_typewriter(self):
        fonts = self.render.load_fonts()
        for kind in ('memo', 'policy', 'glossary', 'retrospective', 'handover', 'coordination', 'form'):
            document = self.render.read_document(self.fixture('Fictional queue context.', kind))
            pages = self.render.layout_document(document, fonts)
            body = [box for page in pages for box in page.boxes if box.text == 'Fictional queue context.']
            self.assertEqual(len(body), 1)
            expected = 'serif' if kind in ('memo', 'policy', 'glossary', 'retrospective') else 'type'
            self.assertEqual(body[0].style, expected)

    def test_cli_uses_mounted_spec_and_removes_stale_pages(self):
        self.fixture()
        (self.directory / 'spec.json').write_text('{"seed": 200}')
        output = self.directory / 'scans'
        output.mkdir()
        (output / 'DOC-0001-p9.jpg').write_bytes(b'stale page')
        unrelated = output / 'DOC-0002-p1.jpg'
        unrelated.write_bytes(b'unrelated document')
        result = subprocess.run(
            [
                sys.executable,
                '-B',
                str(ROOT / 'scripts/render_scans.py'),
                '--documents',
                str(self.directory),
                '--output',
                str(output),
                '--only',
                'DOC-0001',
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=self.directory,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads((output / 'DOC-0001.json').read_text())['seed'], 201)
        self.assertFalse((output / 'DOC-0001-p9.jpg').exists())
        self.assertEqual(unrelated.read_bytes(), b'unrelated document')


if __name__ == '__main__':
    unittest.main()
