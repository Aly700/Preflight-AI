"""Claim verification: declared numbers and IDs against tool results, plus the prose scan."""

import unittest

from backend.verify import Verification, resolve_path, verify_claims

RESULTS = {'t1': {'total': 162, 'limit': 5, 'items': [{'issue_id': 'ISSUE-0001', 'priority': 'high'}]}}
SEEN = {'ISSUE-0001', 'ISSUE-0003'}


def number(value, path='total', call_id='t1', label='unresolved total'):
    return {'label': label, 'value': value, 'call_id': call_id, 'path': path}


class ClaimVerification(unittest.TestCase):
    def test_resolve_path_handles_keys_and_list_indexes(self):
        self.assertEqual(resolve_path(RESULTS['t1'], 'items[0].issue_id'), 'ISSUE-0001')
        self.assertEqual(resolve_path(RESULTS['t1'], 'total'), 162)
        with self.assertRaises(LookupError):
            resolve_path(RESULTS['t1'], 'items[3].issue_id')
        with self.assertRaises(LookupError):
            resolve_path(RESULTS['t1'], 'totals')

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
