"""Claim verification: declared numbers and IDs against tool results, plus the prose scan."""

import unittest

from backend.verify import Verification, record_ids, resolve_path, scan_prose, verify_claims

RESULTS = {'t1': {'total': 162, 'limit': 5, 'items': [{'issue_id': 'ISSUE-0001', 'priority': 'high'}]}}
SEEN = {'ISSUE-0001', 'ISSUE-0003'}


def number(value, path='total', call_id='t1', label='unresolved total'):
    return {'label': label, 'value': value, 'call_id': call_id, 'path': path}


class ClaimVerification(unittest.TestCase):
    def test_thousands_separators_are_one_number(self):
        for formatted, value in (('1,784', 1784), ('12,345', 12345), ('123,456,789', 123456789)):
            with self.subTest(formatted=formatted):
                prose = f'There are {formatted} records.'
                self.assertEqual(scan_prose(prose), (set(), [str(value)]))
                undeclared = verify_claims(prose=prose, claims=None, results={}, seen_ids=set())
                self.assertEqual(undeclared.undeclared, 1)
                self.assertFalse(undeclared.verified)
                declared = verify_claims(
                    prose=prose,
                    claims={'cited_ids': [], 'numbers': [number(value)]},
                    results={'t1': {'total': value}},
                    seen_ids=set(),
                )
                self.assertEqual(declared.undeclared, 0)
                self.assertTrue(declared.verified, declared.note)

    def test_record_ids_collects_aircraft_group_keys_only(self):
        for group_by, key, expected in (
            ('aircraft', 'DEMO-AIR-0001', ['DEMO-AIR-0001']),
            ('subsystem', 'avionics', []),
        ):
            with self.subTest(group_by=group_by):
                payload = {
                    'group_by': group_by,
                    'unresolved_only': True,
                    'filters': {'aircraft_id': None, 'status': None, 'subsystem': None, 'priority': None},
                    'total': 3,
                    'groups': [{'key': key, 'count': 3}],
                }
                self.assertEqual(record_ids(payload), expected)

    def test_resolve_path_handles_keys_and_list_indexes(self):
        self.assertEqual(resolve_path(RESULTS['t1'], 'items[0].issue_id'), 'ISSUE-0001')
        self.assertEqual(resolve_path(RESULTS['t1'], 'total'), 162)
        with self.assertRaises(LookupError):
            resolve_path(RESULTS['t1'], 'items[3].issue_id')
        with self.assertRaises(LookupError):
            resolve_path(RESULTS['t1'], 'totals')

    def test_length_suffix_resolves_list_size(self):
        self.assertEqual(resolve_path(RESULTS['t1'], 'items.length'), 1)
        self.assertEqual(resolve_path({'items': []}, 'items.length'), 0)
        self.assertEqual(resolve_path({'pages': [RESULTS['t1']]}, 'pages[0].items.length'), 1)
        for value in (None, 5, 'items', {'length': 3}):
            with self.subTest(value=value), self.assertRaises(LookupError):
                resolve_path({'items': value}, 'items.length')
        with self.assertRaises(LookupError):
            resolve_path(RESULTS['t1'], 'items.length.total')
        result = verify_claims(
            prose='This page shows 1 issue.',
            claims={'cited_ids': [], 'numbers': [number(1, 'items.length', label='page size')]},
            results=RESULTS,
            seen_ids=SEEN,
        )
        self.assertTrue(result.verified, result.note)
        self.assertEqual(result.undeclared, 0)

    def test_verifier_reads_new_result_shapes(self):
        counts = {
            'group_by': 'subsystem',
            'unresolved_only': True,
            'filters': {'aircraft_id': None, 'status': None, 'subsystem': None, 'priority': None},
            'total': 162,
            'groups': [{'key': 'avionics', 'count': 30}, {'key': 'cabin', 'count': 25}],
        }
        comparison = {
            'ranked_by_unresolved': [{'aircraft_id': 'DEMO-AIR-0001', 'unresolved_issue_count': 12}],
            'unresolved_spread': 0,
        }
        self.assertEqual(resolve_path(counts, 'groups[1].count'), 25)
        self.assertEqual(resolve_path(comparison, 'ranked_by_unresolved[0].unresolved_issue_count'), 12)
        claims = {
            'cited_ids': [],
            'numbers': [
                number(25, 'groups[1].count', 'c1', 'cabin unresolved'),
                number(0, 'unresolved_spread', 'c2', 'spread'),
            ],
        }
        result = verify_claims(
            prose='cabin 25, spread 0', claims=claims, results={'c1': counts, 'c2': comparison}, seen_ids=set()
        )
        self.assertTrue(result.verified)

    def test_declared_number_matching_its_path_verifies(self):
        result = verify_claims(
            prose='There are 162 unresolved issues; this page shows 5.',
            claims={'cited_ids': [], 'numbers': [number(162), number(5, 'limit', label='page size')]},
            results=RESULTS,
            seen_ids=SEEN,
        )
        self.assertIsInstance(result, Verification)
        self.assertTrue(result.verified)
        self.assertEqual(result.unverified, [])
        self.assertEqual(result.undeclared, 0)
        self.assertEqual(result.note, '')

    def test_declared_number_differing_from_its_path_is_unverified_with_label(self):
        result = verify_claims(
            prose='There are 161 unresolved issues.',
            claims={'cited_ids': [], 'numbers': [number(161)]},
            results=RESULTS,
            seen_ids=SEEN,
        )
        self.assertFalse(result.verified)
        self.assertEqual(len(result.unverified), 1)
        self.assertIn('unresolved total', result.unverified[0])
        self.assertIn('162', result.unverified[0])
        self.assertTrue(result.note.startswith('Unverified: '))

    def test_missing_path_is_unverified(self):
        for path, call_id in (('totals', 't1'), ('total', 'no-such-call'), ('items[9].priority', 't1')):
            with self.subTest(path=path, call_id=call_id):
                result = verify_claims(
                    prose='162',
                    claims={'cited_ids': [], 'numbers': [number(162, path, call_id)]},
                    results=RESULTS,
                    seen_ids=SEEN,
                )
                self.assertFalse(result.verified)
                self.assertIn('unresolved total', result.unverified[0])

    def test_prose_ids_must_be_cited(self):
        uncited = verify_claims(
            prose='See ISSUE-0001.', claims={'cited_ids': [], 'numbers': []}, results=RESULTS, seen_ids=SEEN
        )
        self.assertFalse(uncited.verified)
        self.assertIn('ISSUE-0001', uncited.unverified[0])
        never_seen = verify_claims(
            prose='See ISSUE-0042.', claims={'cited_ids': ['ISSUE-0042'], 'numbers': []}, results=RESULTS, seen_ids=SEEN
        )
        self.assertFalse(never_seen.verified)
        self.assertIn('ISSUE-0042', never_seen.unverified[0])
        cited = verify_claims(
            prose='See ISSUE-0001.', claims={'cited_ids': ['ISSUE-0001'], 'numbers': []}, results=RESULTS, seen_ids=SEEN
        )
        self.assertTrue(cited.verified)

    def test_non_record_values_in_cited_ids_are_ignored(self):
        result = verify_claims(
            prose='See ISSUE-0001 in DEMO-PROGRAM-ALPHA at DEMO-STATION-01.',
            claims={
                'cited_ids': [
                    'ISSUE-0001',
                    'DEMO-PROGRAM-ALPHA',
                    'DEMO-STATION-01',
                    'ISSUE-0001-extra',
                    'prefix-ISSUE-0001',
                    42,
                    None,
                ],
                'numbers': [],
            },
            results=RESULTS,
            seen_ids=SEEN,
        )
        self.assertTrue(result.verified, result.note)
        self.assertEqual(result.unverified, [])
        self.assertEqual(result.note, '')

    def test_number_may_reference_the_tool_name_when_the_value_is_found(self):
        by_name = {'cited_ids': [], 'numbers': [number(162, 'total', 'list_open_issues')]}
        names = {'t1': 'list_open_issues'}
        result = verify_claims(prose='162', claims=by_name, results=RESULTS, seen_ids=SEEN, names=names)
        self.assertTrue(result.verified)
        wrong = {'cited_ids': [], 'numbers': [number(7, 'total', 'list_open_issues')]}
        result = verify_claims(prose='7', claims=wrong, results=RESULTS, seen_ids=SEEN, names=names)
        self.assertFalse(result.verified)
        self.assertIn('162', result.unverified[0])

    def test_non_numeric_path_does_not_leak_the_payload(self):
        leaky = {'cited_ids': [], 'numbers': [number(162, 'items'), number(5, 'x' * 200)]}
        result = verify_claims(prose='162 and 5', claims=leaky, results=RESULTS, seen_ids=SEEN)
        self.assertFalse(result.verified)
        self.assertNotIn('high', result.note)  # no record field reaches the note
        self.assertNotIn('ISSUE-0001', result.note)
        self.assertIn('is not a number', result.unverified[0])
        self.assertLess(len(result.unverified[1]), 200)  # the long path is truncated

    def test_malformed_cited_ids_are_reported_not_iterated(self):
        result = verify_claims(
            prose='', claims={'cited_ids': 'ISSUE-0001', 'numbers': []}, results=RESULTS, seen_ids=SEEN
        )
        self.assertFalse(result.verified)
        self.assertEqual(result.unverified, ['cited_ids is malformed'])

    def test_tool_name_reference_is_refused_when_the_tool_was_called_twice(self):
        results = {'a': {'issue_count': 7}, 'b': {'issue_count': 12}}
        names = {'a': 'get_aircraft_summary', 'b': 'get_aircraft_summary'}
        swapped = {'cited_ids': [], 'numbers': [number(12, 'issue_count', 'get_aircraft_summary', 'issues')]}
        result = verify_claims(prose='12', claims=swapped, results=results, seen_ids=set(), names=names)
        self.assertFalse(result.verified)
        self.assertIn('more than once', result.unverified[0])

    def test_undeclared_integers_are_counted_not_shown(self):
        result = verify_claims(
            prose='There are 162 unresolved issues as of 2026-01-01 at 10:30, 3 of them high, opened in 2026.',
            claims={'cited_ids': [], 'numbers': [number(162)]},
            results=RESULTS,
            seen_ids=SEEN,
        )
        self.assertTrue(result.verified)
        self.assertEqual(
            result.undeclared, 1
        )  # the 3; the declared 162, the date, the time and the year are not counted
        self.assertEqual(result.note, '')

    def test_zero_claims_and_no_numbers_or_ids_is_verified(self):
        result = verify_claims(prose='Which aircraft do you mean?', claims=None, results={}, seen_ids=set())
        self.assertTrue(result.verified)
        self.assertEqual(result.note, '')

    def test_no_claims_with_numbers_in_prose_is_unverified(self):
        result = verify_claims(prose='There are 162 unresolved issues.', claims=None, results=RESULTS, seen_ids=SEEN)
        self.assertFalse(result.verified)
        self.assertEqual(result.undeclared, 1)
        self.assertTrue(result.note.startswith('Unverified: '))


if __name__ == '__main__':
    unittest.main()
