"""Claim verification: declared numbers and IDs against tool results, plus the prose scan."""

from typing import Any
import unittest

from backend.verify import FINAL_ANSWER_TOOL, Verification, record_ids, resolve_path, scan_prose, verify_claims

RESULTS = {'t1': {'total': 162, 'limit': 5, 'items': [{'issue_id': 'ISSUE-0001', 'priority': 'high'}]}}
SEEN = {'ISSUE-0001', 'ISSUE-0003'}


def number(value, path='total', call_id='t1', label='unresolved total'):
    return {'label': label, 'value': value, 'call_id': call_id, 'path': path}


def operand(call_id='t1', path='total'):
    return {'call_id': call_id, 'path': path}


def computed(value, op, operands, **options):
    return {
        'label': 'Unresolved structures issues',
        'value': value,
        'derived': {'op': op, 'operands': operands, **options},
    }


class DerivedClaimVerification(unittest.TestCase):
    def check(self, declaration, *, prose='', results=None, names=None):
        return verify_claims(
            prose=prose,
            claims={'cited_ids': [], 'numbers': [declaration]},
            results=results if results is not None else {'t1': {'total': 30, 'groups': [{'count': 2}]}},
            seen_ids=set(),
            names=names,
        )

    def test_schema_offers_direct_paths_or_three_derivations(self):
        item = FINAL_ANSWER_TOOL['input_schema']['properties']['numbers']['items']
        self.assertEqual(item['required'], ['label', 'value'])
        self.assertIn('call_id', item['properties'])
        self.assertIn('path', item['properties'])
        derived = item['properties']['derived']
        self.assertEqual(derived['type'], 'object')
        self.assertEqual(derived['required'], ['op', 'operands'])
        self.assertEqual(derived['properties']['op']['enum'], ['sum', 'difference', 'count'])
        operands = derived['properties']['operands']
        self.assertEqual(operands['type'], 'array')
        self.assertEqual(operands['items']['type'], 'object')
        self.assertEqual(operands['items']['required'], ['call_id', 'path'])
        for key in ('call_id', 'path'):
            self.assertEqual(operands['items']['properties'][key]['type'], 'string')
        where = derived['properties']['where']
        self.assertEqual(where['required'], ['field', 'equals'])
        self.assertEqual(where['properties']['field']['type'], 'string')
        self.assertEqual(where['properties']['equals']['type'], ['string', 'number', 'boolean'])
        self.assertIn('distinct', derived['properties'])
        distinct = derived['properties']['distinct']
        self.assertEqual(distinct['type'], 'string')
        self.assertIn('non-null', distinct['description'])
        self.assertIn('dotted', distinct['description'])
        self.assertNotIn('distinct', derived['required'])
        for description in (item['description'], FINAL_ANSWER_TOOL['description']):
            self.assertIn('call_id and path', description)
            self.assertIn('derived', description)

    def test_difference_within_one_result_verifies(self):
        result = self.check(computed(28, 'difference', [operand(), operand(path='groups[0].count')]))
        self.assertTrue(result.verified, result.note)
        self.assertEqual(result.note, '')

    def test_difference_operands_select_groups_by_field_value(self):
        result = self.check(
            computed(
                23, 'difference', [operand(path='groups[key=cabin].count'), operand(path='groups[key=other].count')]
            ),
            results={'t1': {'groups': [{'key': 'other', 'count': 2}, {'key': 'Cabin', 'count': 25}]}},
        )
        self.assertTrue(result.verified, result.note)

    def test_difference_accepts_signed_value_or_gap_in_either_order(self):
        results = {'a': {'total': 13}, 'b': {'total': 14}}
        for first, second, value in (('a', 'b', 1), ('b', 'a', 1), ('a', 'b', -1)):
            with self.subTest(first=first, second=second, value=value):
                result = self.check(computed(value, 'difference', [operand(first), operand(second)]), results=results)
                self.assertTrue(result.verified, result.note)

    def test_difference_mismatch_reports_computed_value(self):
        result = self.check(
            computed(28, 'difference', [operand(), operand(path='closed')]),
            results={'t1': {'total': 30, 'closed': 3}},
        )
        self.assertEqual(
            result.note, 'Unverified: Unresolved structures issues (28) does not match the derived value (27)'
        )

    def test_sum_across_calls_verifies_with_existing_numeric_tolerance(self):
        for a, b, value in ((13, 14, 27), (0.1, 0.2, 0.3)):
            with self.subTest(a=a, b=b):
                result = self.check(
                    computed(value, 'sum', [operand('a'), operand('b')]),
                    results={'a': {'total': a}, 'b': {'total': b}},
                )
                self.assertTrue(result.verified, result.note)

    def test_sum_accepts_twelve_operands(self):
        result = self.check(computed(360, 'sum', [operand()] * 12))
        self.assertTrue(result.verified, result.note)

    def test_sum_mismatch_reports_computed_value(self):
        result = self.check(
            computed(28, 'sum', [operand('a'), operand('b')]), results={'a': {'total': 13}, 'b': {'total': 14}}
        )
        self.assertFalse(result.verified)
        self.assertIn('does not match the derived value (27)', result.note)

    def test_count_without_where_is_list_length(self):
        for items in ([], [{'status': 'open'}, 'text', None, 7]):
            with self.subTest(items=items):
                result = self.check(
                    computed(len(items), 'count', [operand(path='items')]), results={'t1': {'items': items}}
                )
                self.assertTrue(result.verified, result.note)

    def test_count_where_matches_strings_case_insensitively(self):
        result = self.check(
            computed(2, 'count', [operand(path='items')], where={'field': 'status', 'equals': 'OpEn'}),
            results={'t1': {'items': [{'status': 'OPEN'}, {'status': 'open'}, {'status': 'closed'}, {}, 'open', None]}},
        )
        self.assertTrue(result.verified, result.note)

    def test_count_where_never_equates_booleans_and_numbers(self):
        items = [{'flag': value} for value in (True, 1, 1.0, False, 0, 0.0, '1', None)] + [{}]
        for equals, expected in ((True, 1), (1, 2), (False, 1), (0, 2), ('1', 1)):
            with self.subTest(equals=equals):
                result = self.check(
                    computed(expected, 'count', [operand(path='items')], where={'field': 'flag', 'equals': equals}),
                    results={'t1': {'items': items}},
                )
                self.assertTrue(result.verified, result.note)

    def test_count_distinct_ignores_case_nulls_and_missing_fields(self):
        items = [
            {'task_id': 'TASK-0001'},
            {'task_id': 'task-0001'},
            {'task_id': 'TASK-0002'},
            {'task_id': 'Straße'},
            {'task_id': 'STRASSE'},
            {'task_id': None},
            {},
            'TASK-0003',
            None,
        ]
        for values, expected in ((items, 3), ([], 0), ([{'task_id': None}, {}], 0)):
            with self.subTest(items=values):
                result = self.check(
                    computed(expected, 'count', [operand(path='items')], distinct='task_id'),
                    results={'t1': {'items': values}},
                )
                self.assertTrue(result.verified, result.note)

    def test_count_distinct_applies_where_before_counting(self):
        result = self.check(
            computed(
                2, 'count', [operand(path='items')], distinct='task_id', where={'field': 'status', 'equals': 'OPEN'}
            ),
            results={
                't1': {
                    'items': [
                        {'task_id': 'TASK-0001', 'status': 'closed'},
                        {'task_id': 'task-0001', 'status': 'open'},
                        {'task_id': 'TASK-0001', 'status': 'Open'},
                        {'task_id': 'TASK-0002', 'status': 'open'},
                        {'task_id': 'TASK-0003', 'status': 'closed'},
                        {'task_id': None, 'status': 'open'},
                        {'status': 'open'},
                    ]
                }
            },
        )
        self.assertTrue(result.verified, result.note)

    def test_count_distinct_reads_dotted_fields(self):
        result = self.check(
            computed(1, 'count', [operand(path='items')], distinct='task.task_id'),
            results={
                't1': {
                    'items': [
                        {'task': {'task_id': 'TASK-0001'}},
                        {'task': {'task_id': 'task-0001'}},
                        {'task': {'task_id': None}},
                        {'task': None},
                        {'task': {}},
                        {},
                    ]
                }
            },
        )
        self.assertTrue(result.verified, result.note)

    def test_distinct_is_only_allowed_for_count(self):
        for op, value in (('sum', 60), ('difference', 0)):
            with self.subTest(op=op):
                result = self.check(computed(value, op, [operand()] * 2, distinct='task_id'))
                self.assertFalse(result.verified)
                self.assertEqual(
                    result.note,
                    f'Unverified: Unresolved structures issues ({value}): distinct is only allowed for count',
                )

    def test_count_distinct_requires_non_empty_string(self):
        title = 'PRIVATE_DISTINCT_RECORD_TITLE'
        invalid_distinct: list[Any] = [None, 1, False, [], [title], {}, '', ' \t ']
        for distinct in invalid_distinct:
            with self.subTest(distinct=distinct):
                result = self.check(
                    computed(1, 'count', [operand(path='items')], distinct=distinct),
                    results={'t1': {'items': [{'title': title}]}},
                )
                self.assertFalse(result.verified)
                self.assertNotIn(title, result.note)
                self.assertEqual(
                    result.note, 'Unverified: Unresolved structures issues (1): distinct must be a non-empty string'
                )

    def test_count_mismatch_reports_computed_value(self):
        result = self.check(
            computed(2, 'count', [operand(path='items')]), results={'t1': {'items': [{'status': 'closed'}]}}
        )
        self.assertFalse(result.verified)
        self.assertIn('does not match the derived value (1)', result.note)

    def test_malformed_derivations_never_include_record_text(self):
        distinctive = 'RECORD_TEXT_MUST_STAY_PRIVATE'
        results = {'t1': {'total': 30, 'title': distinctive, 'items': [{'title': distinctive}], 'flag': True}}
        cases: list[tuple[str, Any, str]] = [
            ('non-object', 'sum', 'the derivation is malformed'),
            ('non-object-list', [], 'the derivation is malformed'),
            ('missing-op', {'operands': [operand()]}, 'the derivation has an unknown operation'),
            ('unknown-op', {'op': distinctive, 'operands': [operand()]}, 'the derivation has an unknown operation'),
            ('missing-operands', {'op': 'sum'}, 'the derivation operands must be a list'),
            ('non-list-operands', {'op': 'sum', 'operands': {}}, 'the derivation operands must be a list'),
            ('too-many', {'op': 'sum', 'operands': [operand()] * 13}, 'the derivation has more than 12 operands'),
            ('sum-empty', {'op': 'sum', 'operands': []}, 'sum requires 2 to 12 operands'),
            ('sum-one', {'op': 'sum', 'operands': [operand()]}, 'sum requires 2 to 12 operands'),
            ('difference-one', {'op': 'difference', 'operands': [operand()]}, 'difference requires exactly 2 operands'),
            (
                'difference-three',
                {'op': 'difference', 'operands': [operand()] * 3},
                'difference requires exactly 2 operands',
            ),
            ('count-empty', {'op': 'count', 'operands': []}, 'count requires exactly 1 operand'),
            ('count-two', {'op': 'count', 'operands': [operand()] * 2}, 'count requires exactly 1 operand'),
            ('operand-not-object', {'op': 'count', 'operands': [distinctive]}, 'a derivation operand is malformed'),
            (
                'missing-path',
                {'op': 'count', 'operands': [{'call_id': 't1'}]},
                'a derivation operand needs call_id and path',
            ),
            (
                'missing-call',
                {'op': 'count', 'operands': [{'path': 'items'}]},
                'a derivation operand needs call_id and path',
            ),
            (
                'invalid-path',
                {'op': 'count', 'operands': [operand(path=None)]},
                'a derivation operand needs call_id and path',
            ),
            (
                'count-non-list',
                {'op': 'count', 'operands': [operand(path='title')]},
                "the value at 'title' is not a list",
            ),
            (
                'sum-text',
                {'op': 'sum', 'operands': [operand(), operand(path='title')]},
                "the value at 'title' is not a number",
            ),
            (
                'sum-bool',
                {'op': 'sum', 'operands': [operand(), operand(path='flag')]},
                "the value at 'flag' is not a number",
            ),
        ]
        for op in ('sum', 'difference'):
            cases.append(
                (
                    f'where-on-{op}',
                    {'op': op, 'operands': [operand()] * 2, 'where': {}},
                    'where is only allowed for count',
                )
            )
        invalid_filters: list[Any] = [
            None,
            [],
            {},
            {'field': 'title'},
            {'equals': distinctive},
            {'field': 1, 'equals': True},
            {'field': 'title', 'equals': None},
            {'field': 'title', 'equals': [distinctive]},
        ]
        for where in invalid_filters:
            cases.append(
                (
                    'invalid-where',
                    {'op': 'count', 'operands': [operand(path='items')], 'where': where},
                    'the count filter is malformed',
                )
            )
        for case, derived, reason in cases:
            with self.subTest(case=case, derived=derived):
                result = self.check({'label': 'Count', 'value': 1, 'derived': derived}, results=results)
                self.assertFalse(result.verified)
                self.assertNotIn(distinctive, result.note)
                self.assertEqual(result.note, f'Unverified: Count (1): {reason}')

    def test_derived_operands_resolve_unique_tool_names(self):
        result = self.check(
            computed(28, 'difference', [operand('count_issues'), operand('count_issues', 'groups[0].count')]),
            names={'t1': 'count_issues'},
        )
        self.assertTrue(result.verified, result.note)

    def test_derived_operands_reject_ambiguous_tool_names(self):
        result = self.check(
            computed(27, 'sum', [operand('count_issues'), operand('b')]),
            results={'a': {'total': 13}, 'b': {'total': 14}},
            names={'a': 'count_issues', 'b': 'count_issues'},
        )
        self.assertFalse(result.verified)
        self.assertIn('names a tool that was called more than once; use the call id', result.note)

    def test_derived_operands_prefer_exact_call_ids(self):
        result = self.check(
            computed(27, 'sum', [operand('a'), operand('b')]),
            results={'a': {'total': 13}, 'b': {'total': 14}},
            names={'a': 'a', 'b': 'a'},
        )
        self.assertTrue(result.verified, result.note)

    def test_derived_operands_report_absent_calls_and_paths_safely(self):
        for reference, path, reason in (
            ('missing', 'total', 'refers to a tool call that returned nothing'),
            ('t1', 'missing', "has no value at 'missing' in that result"),
            ('t1', 'x' * 200, "has no value at '" + 'x' * 80 + "…' in that result"),
        ):
            with self.subTest(reference=reference, path=path):
                result = self.check(computed(1, 'sum', [operand(), operand(reference, path)]))
                self.assertFalse(result.verified)
                self.assertIn(reason, result.note)
                self.assertLess(len(result.note), 200)

    def test_neither_path_nor_derivation_is_unverified(self):
        result = self.check({'label': 'Count', 'value': 30, 'call_id': 't1'})
        self.assertFalse(result.verified)
        self.assertIn('declares neither a path nor a derivation', result.note)

    def test_both_path_and_derivation_are_unverified(self):
        declaration = number(30)
        declaration['derived'] = {'op': 'sum', 'operands': [operand()] * 2}
        result = self.check(declaration)
        self.assertFalse(result.verified)
        self.assertIn('declares both a path and a derivation', result.note)

    def test_null_or_blank_unused_fields_do_not_count_as_both_forms(self):
        # Models often fill an unused optional field with null or an empty string.
        direct = number(30) | {'derived': None}
        derivation = computed(28, 'difference', [operand(), operand(path='groups[0].count')]) | {
            'call_id': '',
            'path': ' ',
        }
        for name, declaration in (('direct with null derived', direct), ('derived with blank path', derivation)):
            with self.subTest(name):
                result = self.check(declaration)
                self.assertTrue(result.verified, result.note)

    def test_derived_values_count_as_declared_integers(self):
        for value in (28, 29):
            with self.subTest(value=value):
                result = self.check(
                    computed(value, 'difference', [operand(), operand(path='groups[0].count')]),
                    prose=f'There are {value} unresolved issues; {value} remain, including 3 high priority issues.',
                )
                self.assertEqual(result.undeclared, 1)
                if value == 28:
                    self.assertTrue(result.verified, result.note)
                else:
                    self.assertIn('does not match the derived value (28)', result.note)


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

    def test_selector_example_paths_resolve_and_verify_numeric_claims(self):
        payload = {
            'groups': [{'key': 'avionics', 'count': 30}, {'key': 'Cabin', 'count': 25}],
            'ranked_by_unresolved': [
                {'aircraft_id': 'DEMO-AIR-0001', 'unresolved_issue_count': 7},
                {'aircraft_id': 'DEMO-AIR-0012', 'unresolved_issue_count': 12},
            ],
        }
        for path, expected in (
            ('groups[key=cabin].count', 25),
            ("groups[?key=='Cabin'].count", 25),
            ('ranked_by_unresolved[aircraft_id=DEMO-AIR-0012].unresolved_issue_count', 12),
        ):
            with self.subTest(path=path):
                self.assertEqual(resolve_path(payload, path), expected)
                result = verify_claims(
                    prose='',
                    claims={'cited_ids': [], 'numbers': [number(expected, path)]},
                    results={'t1': payload},
                    seen_ids=set(),
                )
                self.assertTrue(result.verified, result.note)

    def test_selector_accepts_quotes_question_mark_equality_and_spaces(self):
        payload = {'groups': [{'key': 'Cabin', 'count': 25}]}
        for selector in (
            'key=cabin',
            "key='cabin'",
            'key="cabin"',
            '?key==cabin',
            "?key=='Cabin'",
            '?key=="Cabin"',
            ' key = cabin ',
            ' key = "Cabin" ',
            " ? key == 'Cabin' ",
            ' ? key == cabin ',
        ):
            with self.subTest(selector=selector):
                self.assertEqual(resolve_path(payload, f'groups[{selector}].count'), 25)

    def test_selector_requires_exactly_one_object_match_without_leaking_text(self):
        title = 'PRIVATE_SELECTOR_RECORD_TITLE'
        path = 'items[key=cabin].count'
        for matches in range(3):
            with self.subTest(matches=matches):
                payload = {
                    'items': [{'key': 'other', 'count': 9, 'title': title}, 'cabin', None]
                    + [{'key': 'Cabin', 'count': 25, 'title': title} for _ in range(matches)]
                }
                result = verify_claims(
                    prose='',
                    claims={'cited_ids': [], 'numbers': [number(25, path)]},
                    results={'t1': payload},
                    seen_ids=set(),
                )
                self.assertNotIn(title, result.note)
                if matches == 1:
                    self.assertEqual(resolve_path(payload, path), 25)
                    self.assertTrue(result.verified, result.note)
                else:
                    with self.assertRaises(LookupError):
                        resolve_path(payload, path)
                    self.assertFalse(result.verified)
                    self.assertIn(f'has no value at {path!r}', result.note)
        with self.assertRaises(LookupError):
            resolve_path({'items': {'key=cabin': {'count': 25}}}, path)

    def test_selector_numeric_values_parse_as_numbers_without_boolean_coercion(self):
        for actual, matching, missing in (
            (2, ('2', '2.0', '2e0'), ('two', 'true', 'false', '2.5')),
            (-1.5, ('-1.5', '-1.50'), ('-1', 'true')),
            (1, ('1', '1.0'), ('true', 'false')),
            (0, ('0', '0.0'), ('false', 'true')),
            (9007199254740993, ('9007199254740993',), ('9007199254740992',)),
        ):
            payload = {'items': [{'value': actual, 'count': 7}]}
            for selector in (*matching, *missing):
                with self.subTest(actual=actual, selector=selector):
                    path = f'items[value={selector}].count'
                    if selector in matching:
                        self.assertEqual(resolve_path(payload, path), 7)
                    else:
                        with self.assertRaises(LookupError):
                            resolve_path(payload, path)

    def test_selector_booleans_only_match_true_or_false(self):
        payload = {
            'items': [
                {'flag': True, 'count': 7},
                {'flag': False, 'count': 3},
                {'flag': 1},
                {'flag': 0},
                {'flag': 'true'},
                {'flag': 'false'},
            ]
        }
        for selector, expected in (('true', 7), ('TRUE', 7), ('false', 3), ('False', 3)):
            with self.subTest(selector=selector):
                self.assertEqual(resolve_path(payload, f'items[flag={selector}].count'), expected)
        for flag in (True, False):
            for selector in ('1', '0', 'yes', 'no'):
                with self.subTest(flag=flag, selector=selector), self.assertRaises(LookupError):
                    resolve_path({'items': [{'flag': flag}]}, f'items[flag={selector}]')

    def test_selector_nested_fields_and_list_length(self):
        payload = {
            'summaries': [
                {'aircraft': {'aircraft_id': 'DEMO-AIR-0002'}, 'unresolved_issue_count': 9},
                {
                    'aircraft': {'aircraft_id': 'DEMO-AIR-0001'},
                    'unresolved_issue_count': 4,
                    'task_status_counts': {'completed': 3},
                    'tasks': [{'task_id': 'TASK-0001'}, {'task_id': 'TASK-0002'}],
                },
                {'aircraft': None},
                {},
                'DEMO-AIR-0001',
            ]
        }
        prefix = 'summaries[aircraft.aircraft_id=DEMO-AIR-0001]'
        for suffix, expected in (
            ('.unresolved_issue_count', 4),
            ('.task_status_counts.completed', 3),
            ('.tasks.length', 2),
            ('.tasks[1].task_id', 'TASK-0002'),
        ):
            with self.subTest(suffix=suffix):
                self.assertEqual(resolve_path(payload, prefix + suffix), expected)

    def test_selector_non_numeric_field_is_unverified_without_record_text(self):
        title = 'PRIVATE_SELECTOR_NON_NUMERIC_TITLE'
        payload = {'items': [{'issue_id': 'ISSUE-0005', 'priority': 'high', 'title': title}]}
        path = 'items[issue_id=ISSUE-0005].priority'
        self.assertEqual(resolve_path(payload, path), 'high')
        for field in ('priority', 'title'):
            with self.subTest(field=field):
                result = verify_claims(
                    prose='',
                    claims={'cited_ids': [], 'numbers': [number(5, f'items[issue_id=ISSUE-0005].{field}')]},
                    results={'t1': payload},
                    seen_ids=set(),
                )
                self.assertFalse(result.verified)
                self.assertIn('is not a number', result.note)
                self.assertNotIn(title, result.note)
                self.assertNotIn('high', result.note)

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
