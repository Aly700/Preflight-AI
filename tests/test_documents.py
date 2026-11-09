"""Offline contract tests; all authored document fixtures live in temporary directories."""

from collections import Counter
import csv
from datetime import date
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any
import unittest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data/synthetic'
VERSION = 'documents-2026-11-08.1'
COUNTS = {'policy': 8, 'glossary': 2, 'handover': 14, 'memo': 10, 'coordination': 14, 'retrospective': 8, 'form': 4}
FILES = {
    'aircraft': 'aircraft_id',
    'issues': 'issue_id',
    'tasks': 'task_id',
    'activity_events': 'event_id',
    'teams': 'team_id',
    'people': 'person_id',
    'shifts': 'shift_id',
}


def records():
    result = {}
    for name, key in FILES.items():
        with (DATA / f'{name}.csv').open(encoding='utf-8', newline='') as handle:
            result[name] = {row[key]: row for row in csv.DictReader(handle)}
    return result


def word_range(kind):
    return {'form': [120, 250], 'glossary': [300, 600]}.get(kind, [180, 500])


class DocumentSpecTests(unittest.TestCase):
    def generate(self, output, seed=20261108):
        script = ROOT / 'scripts/document_spec.py'
        self.assertTrue(script.is_file(), 'Implement the deterministic document spec generator')
        result = subprocess.run(
            [sys.executable, '-B', str(script), '--seed', str(seed), '--output', str(output)],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(output.read_text(encoding='utf-8'))

    def test_seed_reproduces_bytes_and_committed_spec(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second, other = (Path(directory) / name for name in ('first.json', 'second.json', 'other.json'))
            self.generate(first)
            self.generate(second)
            self.generate(other, seed=20261109)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertNotEqual(first.read_bytes(), other.read_bytes())
            self.assertEqual(first.read_bytes(), (ROOT / 'data/documents/spec.json').read_bytes())

    def test_slots_have_exact_counts_relations_scope_and_dates(self):
        rows = records()
        config = json.loads((ROOT / 'config/synthetic_spec.json').read_text())
        with tempfile.TemporaryDirectory() as directory:
            spec = self.generate(Path(directory) / 'spec.json')
        self.assertEqual(spec['version'], VERSION)
        self.assertEqual(spec['seed'], 20261108)
        slots = spec['slots']
        self.assertEqual([slot['doc_id'] for slot in slots], [f'DOC-{i:04}' for i in range(1, 61)])
        self.assertEqual(Counter(slot['kind'] for slot in slots), COUNTS)
        self.assertEqual(len({slot['title'].casefold() for slot in slots}), 60)
        known = {ident for name in ('aircraft', 'issues', 'tasks', 'teams') for ident in rows[name]}
        for slot in slots:
            with self.subTest(doc_id=slot['doc_id']):
                self.assertEqual(slot['word_range'], word_range(slot['kind']))
                self.assertTrue(slot['title'].strip())
                day = date.fromisoformat(slot['date'])
                self.assertEqual(day.year, 2026)
                self.assertGreaterEqual(slot['date'], config['date_start_utc'][:10])
                self.assertLessEqual(slot['date'], config['date_end_utc'][:10])
                self.assertEqual(rows['people'][slot['author']]['team_id'], slot['team'])
                self.assertIn(slot['team'], rows['teams'])
                related = slot['related_ids']
                self.assertTrue(1 <= len(related) <= 6)
                self.assertEqual(len(related), len(set(related)))
                self.assertLessEqual(set(related), known)
                aircraft = set(related) & rows['aircraft'].keys()
                issues = set(related) & rows['issues'].keys()
                for ident in aircraft:
                    for field in ('station', 'program'):
                        if field in slot:
                            self.assertEqual(slot[field], rows['aircraft'][ident][field])
                for ident in issues:
                    if aircraft:
                        self.assertIn(rows['issues'][ident]['aircraft_id'], aircraft)
                    self.assertGreaterEqual(slot['date'], rows['issues'][ident]['opened_at'][:10])
                for ident in set(related) & rows['tasks'].keys():
                    self.assertIn(rows['tasks'][ident]['issue_id'], issues)
                    self.assertGreaterEqual(slot['date'], rows['tasks'][ident]['created_at'][:10])
                if slot['kind'] == 'handover':
                    self.assertIn(slot['station'], config['stations'])
                if slot['kind'] == 'memo':
                    self.assertIn(slot['program'], config['programs'])
        for name in ('issue_statuses', 'task_statuses', 'priorities', 'subsystems', 'task_kinds'):
            self.assertEqual(spec['vocabulary'][name], config[name])

    def test_readme_documents_every_banned_pattern(self):
        import ast

        path = ROOT / 'data/documents/README.md'
        self.assertTrue(path.is_file(), 'Document the file format and banned patterns')
        readme = path.read_text(encoding='utf-8')
        tree = ast.parse((ROOT / 'scripts/validate_documents.py').read_text(encoding='utf-8'))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == 'BANNED_PATTERNS' for target in node.targets
            ):
                for category, pattern in ast.literal_eval(node.value):
                    self.assertIn(f'{category}: {pattern}', readme)
                break
        else:
            self.fail('Banned pattern list is missing')


class DocumentValidatorTests(unittest.TestCase):
    def setUp(self):
        self.script = ROOT / 'scripts/validate_documents.py'
        self.assertTrue(self.script.is_file(), 'Implement the document validator')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.spec_path = self.directory / 'spec.json'
        self.slots: list[dict[str, Any]] = []
        for kind, count in COUNTS.items():
            for _ in range(count):
                self.slots.append(
                    {
                        'doc_id': f'DOC-{len(self.slots) + 1:04}',
                        'kind': kind,
                        'title': f'Fictional queue context {len(self.slots) + 1}',
                        'date': '2026-10-27',
                        'station': 'DEMO-STATION-01',
                        'program': 'DEMO-PROGRAM-ALPHA',
                        'author': 'PERSON-0001',
                        'team': 'TEAM-0001',
                        'related_ids': ['DEMO-AIR-0001', 'ISSUE-0001', 'TASK-0001', 'TEAM-0001'],
                        'word_range': word_range(kind),
                    }
                )
        self.spec = {'version': VERSION, 'seed': 20261108, 'slots': self.slots}
        self.save_spec()

    def save_spec(self):
        self.spec_path.write_text(json.dumps(self.spec), encoding='utf-8')

    def write_document(self, slot=None, *, fields=None, omit=(), body=None, name=None):
        slot = self.slots[0] if slot is None else slot
        header = {key: value for key, value in slot.items() if key != 'word_range'}
        header['related_ids'] = ', '.join(slot['related_ids'])
        header.update(authored_by='fixture-tool / fixture-model', spec_version=VERSION)
        header.update(fields or {})
        if body is None:
            body = 'Queue context remains fictional. ' * ((slot['word_range'][0] + 20) // 4)
            body = ('Request: queue discussion\nReason: shared context\n' if slot['kind'] == 'form' else '') + body
        text = '---\n' + ''.join(f'{key}: {value}\n' for key, value in header.items() if key not in omit)
        path = self.directory / (name or f'{slot["doc_id"]}.md')
        path.write_text(text + '---\n' + body + '\n', encoding='utf-8')
        return path

    def validate(self, *, allow_missing=True):
        args = [
            sys.executable,
            '-B',
            str(self.script),
            '--documents',
            str(self.directory),
            '--spec',
            str(self.spec_path),
            '--data',
            str(DATA),
        ]
        if allow_missing:
            args.append('--allow-missing')
        return subprocess.run(args, capture_output=True, text=True, timeout=30, cwd=ROOT)

    def assert_invalid(self, rule, *, allow_missing=True):
        result = self.validate(allow_missing=allow_missing)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(rule, result.stdout)
        self.assertEqual(result.stderr, '')
        self.assertTrue(all(line.startswith(('DOC-', 'SPEC:')) for line in result.stdout.splitlines()))
        return result

    def test_valid_fixture_for_every_kind(self):
        for kind in COUNTS:
            slot = next(slot for slot in self.slots if slot['kind'] == kind)
            self.write_document(slot)
        result = self.validate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, 'Validated 7 documents (53 missing slots allowed).\n')
        self.assertEqual(result.stderr, '')

    def test_complete_sixty_file_corpus_passes(self):
        for slot in self.slots:
            self.write_document(slot)
        result = self.validate(allow_missing=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, 'Validated 60 documents.\n')

    def test_missing_files_fail_unless_allowed(self):
        result = self.assert_invalid('DOC-0001: missing document', allow_missing=False)
        self.assertEqual(len(result.stdout.splitlines()), 60)
        self.assertEqual(self.validate().returncode, 0)

    def test_extra_files_fail_even_when_missing_is_allowed(self):
        self.write_document(name='DOC-0061.md')
        self.assert_invalid('DOC-0061: unexpected document')

    def test_each_required_field_is_required(self):
        for field in (
            'doc_id',
            'kind',
            'title',
            'date',
            'author',
            'team',
            'related_ids',
            'authored_by',
            'spec_version',
        ):
            with self.subTest(field=field):
                self.write_document(omit=(field,))
                self.assert_invalid(f'missing field {field}')

    def test_slot_fields_must_match(self):
        values = {
            'doc_id': 'DOC-0002',
            'kind': 'memo',
            'date': '2026-01-01',
            'author': 'PERSON-0002',
            'team': 'TEAM-0002',
            'related_ids': 'DEMO-AIR-0001',
            'spec_version': 'different-version',
            'station': 'DEMO-STATION-02',
            'program': 'DEMO-PROGRAM-BETA',
        }
        for field, value in values.items():
            with self.subTest(field=field):
                self.write_document(fields={field: value})
                self.assert_invalid(f'field {field} differs from slot')

    def test_scope_fields_required_when_in_slot(self):
        for field in ('station', 'program'):
            with self.subTest(field=field):
                self.write_document(omit=(field,))
                self.assert_invalid(f'missing field {field}')

    def test_scope_fields_optional_when_not_in_slot(self):
        del self.slots[0]['station'], self.slots[0]['program']
        self.save_spec()
        self.write_document()
        self.assertEqual(self.validate().returncode, 0)

    def test_word_limits_include_boundaries_for_each_kind(self):
        for kind in COUNTS:
            slot = next(slot for slot in self.slots if slot['kind'] == kind)
            low, high = slot['word_range']
            for count in (low - 1, low, high, high + 1):
                with self.subTest(kind=kind, count=count):
                    self.write_document(slot, body='Field: ' + 'queue ' * (count - 1))
                    result = self.validate()
                    self.assertEqual(result.returncode, 0 if low <= count <= high else 1, result.stdout)
                    if result.returncode:
                        self.assertIn('body word count outside', result.stdout)
            (self.directory / f'{slot["doc_id"]}.md').unlink()

    def test_every_id_namespace_checked_for_existence(self):
        for prefix in ('DEMO-AIR-', 'ISSUE-', 'TASK-', 'EVT-', 'TEAM-', 'PERSON-', 'SHIFT-', 'DOC-'):
            with self.subTest(prefix=prefix):
                self.write_document(body='queue ' * 190 + prefix + '9999')
                self.assert_invalid('unknown body ID')

    def test_existing_but_unrelated_ids_are_rejected(self):
        for ident in (
            'DEMO-AIR-0002',
            'ISSUE-0002',
            'TASK-0002',
            'EVT-0001',
            'TEAM-0002',
            'PERSON-0002',
            'SHIFT-0001',
            'DOC-0001',
        ):
            with self.subTest(ident=ident):
                self.write_document(body='queue ' * 190 + ident)
                self.assert_invalid('body ID not allowed by slot')

    def test_allowed_ids_and_author_team_pass(self):
        self.write_document(body='queue ' * 190 + ' '.join([*self.slots[0]['related_ids'], 'PERSON-0001']))
        self.assertEqual(self.validate().returncode, 0)

    def test_malformed_id_tokens_cannot_match_an_allowed_prefix(self):
        for token in ('ISSUE-0001-extra', 'TASK-00010', 'ISSUE-abc', 'issue-0001', 'TEAM-', 'DEMO-AIR-0001_foo'):
            with self.subTest(token=token):
                self.write_document(body='queue ' * 190 + token)
                self.assert_invalid('unknown body ID')

    def test_each_banned_category_and_case_insensitivity(self):
        cases = {
            'maintenance': (
                'torque',
                'rivet',
                'inspect the',
                'replace the',
                'remove the',
                'tighten',
                'lubricate',
                'borescope',
                'NDT',
                'part number',
                'P/N',
                'service bulletin',
                'airworthiness directive',
            ),
            'readiness': ('airworthy', 'cleared for flight', 'safe to fly', 'fit for flight', 'return to service'),
            'certification': (
                'sign off',
                'signed off',
                'sign-off',
                'certify',
                'certified',
                'release to service',
                'approved for flight',
            ),
            'real-world': (
                'FAA',
                'EASA',
                'Transport Canada',
                'Boeing',
                'Airbus',
                'Embraer',
                'Bombardier',
                'ATA chapter',
                'Air Canada',
                'WestJet',
                'Delta Air Lines',
                'United Airlines',
                'American Airlines',
                'Southwest Airlines',
                'British Airways',
                'Lufthansa',
                'Emirates',
                'Qatar Airways',
                'Singapore Airlines',
                'Air France',
                'KLM',
                'Ryanair',
                'easyJet',
                'Qantas',
                'JetBlue',
            ),
            'url': (
                'https://fiction.invalid/a',
                'www.fiction.invalid',
                'ftp://fiction.invalid',
                'fiction.invalid/path',
            ),
            'email': ('worker@fiction.invalid',),
            'phone': ('+1 (000) 555-0100', '000-555-0100', '000.555.0100', '0005550100', '555-0100', '+44 0000 000000'),
        }
        for category, examples in cases.items():
            for example in examples:
                with self.subTest(category=category, example=example):
                    self.write_document(body='queue ' * 190 + example.swapcase())
                    self.assert_invalid(f'banned {category}')

    def test_banned_patterns_cover_metadata_too(self):
        self.write_document(fields={'title': 'FAA context'})
        self.assert_invalid('banned real-world')

    def test_document_text_is_never_printed_even_in_errors(self):
        marker = 'PRIVATE_BODY_SENTINEL'
        self.write_document(body=marker + ' torque https://fiction.invalid/' + marker + ' ISSUE-' + marker)
        result = self.assert_invalid('banned maintenance')
        self.assertNotIn(marker, result.stdout + result.stderr)
        self.assertNotIn('torque', result.stdout + result.stderr)
        self.assertNotIn('https://', result.stdout + result.stderr)

    def test_duplicate_final_titles_are_case_and_whitespace_insensitive(self):
        self.write_document(fields={'title': 'Shared queue context'})
        self.write_document(self.slots[1], fields={'title': ' SHARED   QUEUE context '})
        result = self.assert_invalid('duplicate title')
        self.assertIn('DOC-0001: duplicate title', result.stdout)
        self.assertIn('DOC-0002: duplicate title', result.stdout)

    def test_plain_markdown_and_fictional_labels_are_not_false_positives(self):
        body = (
            '# Queue context\n\nDemo Worker 0001 and Demo Team 01 met on 2026-10-27.\n'
            'DEMO-STATION-01 uses DEMO-PROGRAM-ALPHA.\n'
            '| Status | Meaning |\n| --- | --- |\n| awaiting_review | Queue label |\n'
            '- inspection and rectification are task labels only.\n'
            '- blocked in_progress low hydraulics structures\n' + 'queue ' * 190
        )
        self.write_document(body=body)
        self.assertEqual(self.validate().returncode, 0, self.validate().stdout)

    def test_front_matter_must_be_simple_and_delimited(self):
        for text in (
            'no header\n',
            '---\nkind: policy\n',
            '---\nprivate: [nested]\n---\n',
            '---\nkind: policy\nkind: memo\n---\n',
        ):
            with self.subTest(text=text):
                (self.directory / 'DOC-0001.md').write_text(text, encoding='utf-8')
                self.assert_invalid('front matter')

    def test_authorship_must_name_tool_and_model(self):
        for value in ('', 'only-a-tool', 'tool / '):
            with self.subTest(value=value):
                self.write_document(fields={'authored_by': value})
                self.assert_invalid('authored_by must name tool / model')

    def test_invalid_utf8_reports_only_a_rule(self):
        (self.directory / 'DOC-0001.md').write_bytes(b'\xffPRIVATE_BODY_SENTINEL')
        result = self.assert_invalid('cannot read UTF-8 document')
        self.assertNotIn('PRIVATE_BODY_SENTINEL', result.stdout + result.stderr)

    def test_spec_is_checked_against_csvs_not_trusted_blindly(self):
        cases = (
            ('author', 'PERSON-9999', 'slot author'),
            ('team', 'TEAM-0002', 'author team'),
            ('related_ids', ['DEMO-AIR-0002', 'ISSUE-0001'], 'issue aircraft'),
            ('related_ids', ['ISSUE-9999'], 'related IDs'),
            ('related_ids', ['EVT-0001'], 'related IDs'),
            ('word_range', [1, 9999], 'word range'),
            ('date', '2025-10-27', 'slot date'),
            ('station', 'DEMO-STATION-99', 'slot station'),
        )
        original = self.slots[0].copy()
        for key, value, rule in cases:
            with self.subTest(key=key, value=value):
                self.slots[0] = {**original, key: value}
                self.save_spec()
                self.assert_invalid(rule)

    def test_invalid_json_and_incomplete_spec_fail_even_with_allow_missing(self):
        for text in ('PRIVATE_BODY_SENTINEL', '{}', '[]', '{"slots": [null]}'):
            with self.subTest(text=text):
                self.spec_path.write_text(text, encoding='utf-8')
                result = self.assert_invalid('invalid specification')
                self.assertNotIn('PRIVATE_BODY_SENTINEL', result.stdout + result.stderr)

    def test_invalid_spec_counts_and_duplicate_document_ids_fail(self):
        self.slots[0]['kind'] = 'memo'
        self.save_spec()
        self.assert_invalid('invalid specification kind counts')
        self.slots[0]['kind'] = 'policy'
        self.slots[1]['doc_id'] = 'DOC-0001'
        self.save_spec()
        self.assert_invalid('invalid specification')

    def test_related_ids_accept_separator_whitespace_but_reject_duplicates(self):
        self.write_document(fields={'related_ids': ','.join(self.slots[0]['related_ids'])})
        self.assertEqual(self.validate().returncode, 0)
        self.write_document(fields={'related_ids': ','.join([*self.slots[0]['related_ids'], 'ISSUE-0001'])})
        self.assert_invalid('field related_ids differs from slot')

    def test_id_only_counts_as_one_word_and_front_matter_does_not_count(self):
        self.write_document(body='queue ' * 178 + 'DEMO-AIR-0001')
        self.assert_invalid('body word count outside')
        self.write_document(body='queue ' * 179 + 'DEMO-AIR-0001')
        self.assertEqual(self.validate().returncode, 0)

    def test_symlink_document_is_not_read(self):
        target = self.directory / 'fixture.txt'
        target.write_text('PRIVATE_BODY_SENTINEL', encoding='utf-8')
        (self.directory / 'DOC-0001.md').symlink_to(target)
        result = self.assert_invalid('document must not be a symbolic link')
        self.assertNotIn('PRIVATE_BODY_SENTINEL', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
