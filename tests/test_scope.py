"""Structured scope validation and pure tool argument enforcement; no live services."""

from copy import deepcopy
import csv
import json
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.chat import parse_payload
from backend.settings import ChatSettings
from preflight_core.tools import TOOLS

VALID = {
    'entity': 'issues',
    'aircraft_id': 'DEMO-AIR-0001',
    'subsystem': 'cabin',
    'priority': 'high',
    'issue_status': 'unresolved',
    'opened_after': '2026-01-01',
    'opened_before': '2026-11-10',
    'task_status': 'awaiting_review',
    'task_kind': 'review',
    'team_id': 'TEAM-0001',
    'document_kind': 'memo',
    'related_id': 'DOC-0001#2',
}
INVALID = {
    'entity': 'people',
    'aircraft_id': 'DEMO-AIR-1',
    'subsystem': 'engine',
    'priority': 'urgent',
    'issue_status': 'completed',
    'opened_after': '2026-02-30',
    'opened_before': '2026-1-01',
    'task_status': 'closed',
    'task_kind': 'repair',
    'team_id': 'TEAM-1',
    'document_kind': 'manual',
    'related_id': '../private',
}


class ScopeValidation(unittest.TestCase):
    def test_each_field_and_every_enum_value_and_shared_json(self):
        from preflight_core.scope import SCOPE_OPTIONS, ui_options, validate_scope

        self.assertEqual(validate_scope({}), {})
        self.assertEqual(validate_scope(VALID), VALID)
        for key, value in VALID.items():
            with self.subTest(key=key):
                self.assertEqual(
                    parse_payload(json.dumps({'question': ' Q ', 'scope': {key: value}}).encode()),
                    ('Q', [], {key: value}),
                )
        for key, spec in SCOPE_OPTIONS.items():
            for value in spec.get('enum', []):
                self.assertEqual(validate_scope({key: value}), {key: value})
        path = Path(__file__).resolve().parents[1] / 'frontend/src/scope-options.json'
        self.assertEqual(json.loads(path.read_text()), ui_options())
        self.assertEqual({key: ui_options()[key] for key in SCOPE_OPTIONS}, SCOPE_OPTIONS)

    def test_ui_options_include_sorted_csv_choices_and_are_deterministic(self):
        from preflight_core.scope import ui_options

        root = Path(__file__).resolve().parents[1]
        options = ui_options()
        for filename, key, fields in (
            ('aircraft.csv', 'aircraft', {'id': 'aircraft_id', 'program': 'program', 'station': 'station'}),
            ('teams.csv', 'teams', {'id': 'team_id', 'name': 'team_name'}),
        ):
            with (root / 'data/synthetic' / filename).open(newline='') as source:
                expected = [
                    {target: row[column] for target, column in fields.items()} for row in csv.DictReader(source)
                ]
            self.assertEqual(options[key], sorted(expected, key=lambda row: row['id']))
        self.assertEqual(json.dumps(options), json.dumps(ui_options()))

    def test_invalid_fields_types_sizes_unknowns_and_dates_return_422(self):
        cases: list[Any] = [
            None,
            [],
            'issues',
            True,
            {'unknown': 'x'},
            {'opened_after': '2026-11-11', 'opened_before': '2026-11-10'},
        ]
        for key in VALID:
            invalid_values: list[Any] = [INVALID[key], '', None, [], [VALID[key]], {}, 1, True, 'x' * 40000]
            cases += [{key: value} for value in invalid_values]
        cases += [{'opened_after': value} for value in ('20260101', '2026-01-01T00:00:00Z', ' 2026-01-01')]
        cases += [{'related_id': 'ISSUE-' + '1' * 65}, {'team_id': 'TEAM-0001\n'}]
        for scope in cases:
            with self.subTest(scope=str(scope)[:100]), self.assertRaises(HTTPException) as caught:
                parse_payload(json.dumps({'question': 'Q', 'scope': scope}).encode())
            self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(parse_payload(b'{"question":"Q"}'), ('Q', [], {}))
        self.assertEqual(parse_payload(b'{"question":"Q","scope":{}}'), ('Q', [], {}))
        for body in ({'question': 'Q', 'other': {}}, {'question': ''}, {'question': 'Q', 'history': None}):
            with self.assertRaises(HTTPException) as caught:
                parse_payload(json.dumps(body).encode())
            self.assertEqual(caught.exception.status_code, 422)

    def test_duplicate_scope_selections_are_rejected(self):
        for body in (
            b'{"question":"Q","scope":{"priority":"high","priority":"low"}}',
            b'{"question":"Q","scope":{},"scope":{"priority":"high"}}',
        ):
            with self.subTest(body=body), self.assertRaises(HTTPException) as caught:
                parse_payload(body)
            self.assertEqual(caught.exception.status_code, 422)

    def test_scope_reaches_runtime_separately_from_question_and_history(self):
        settings = ChatSettings('a' * 64, 'b' * 64, 'c' * 64, mcp_url='http://127.0.0.1/mcp')
        received = []

        async def answer(question, actual_settings, *, scope=None):
            received.append((question, actual_settings, scope))
            yield {'event': 'done', 'data': {'scope': scope}}

        with (
            patch('backend.chat.ChatSettings.from_environment', return_value=settings),
            patch('backend.chat.stream_answer', answer),
            TestClient(create_app(allowed_hosts=['testserver'])) as client,
        ):
            response = client.post(
                '/api/chat',
                headers={'Authorization': 'Bearer ' + settings.access_token},
                json={'question': 'Q', 'scope': VALID},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(received, [('Q', settings, VALID)])
            self.assertIn('event: done', response.text)
            invalid = client.post(
                '/api/chat',
                headers={'Authorization': 'Bearer ' + settings.access_token},
                json={'question': 'Q', 'scope': {'priority': ['high', 'low']}},
            )
            self.assertEqual(invalid.status_code, 422)
            large = client.post(
                '/api/chat',
                headers={'Authorization': 'Bearer ' + settings.access_token},
                json={'question': 'Q', 'scope': {'related_id': 'x' * 33000}},
            )
            self.assertEqual(large.status_code, 413)


class ApplyScope(unittest.TestCase):
    def apply(self, tool, args, scope, schema=None):
        from backend.scope import apply_scope

        schema = schema if schema is not None else {'properties': {key: {} for key in TOOLS[tool].arguments}}
        before = deepcopy((args, scope, schema))
        try:
            return apply_scope(tool, args, scope, schema)
        finally:
            self.assertEqual((args, scope, schema), before, 'scope enforcement must be pure')

    def test_count_scope_injects_inclusive_days_and_refuses_wider_windows(self):
        scope = {'opened_after': '2026-01-01', 'opened_before': '2026-01-31', 'issue_status': 'unresolved'}
        self.assertEqual(
            self.apply('count_issues', {'group_by': 'subsystem'}, scope),
            {
                'group_by': 'subsystem',
                'unresolved_only': True,
                'opened_after': '2026-01-01T00:00:00+00:00',
                'opened_before': '2026-01-31T23:59:59.999999+00:00',
            },
        )
        from backend.scope import ScopeConflict

        for arguments in ({'opened_after': '2025-12-31T23:59:59Z'}, {'opened_before': '2026-02-01T00:00:00Z'}):
            with self.subTest(arguments=arguments), self.assertRaises(ScopeConflict):
                self.apply('count_issues', {'group_by': 'status', **arguments}, scope)

    def test_mapping_table(self):
        cases = [
            (
                'search_issues',
                {},
                {'aircraft_id': 'DEMO-AIR-0001', 'subsystem': 'cabin', 'priority': 'high', 'issue_status': 'blocked'},
                {'aircraft_id': 'DEMO-AIR-0001', 'subsystem': 'cabin', 'priority': 'high', 'status': 'blocked'},
            ),
            ('get_aircraft_summary', {}, {'aircraft_id': 'DEMO-AIR-0001'}, {'aircraft_id': 'DEMO-AIR-0001'}),
            (
                'list_tasks',
                {'limit': 3},
                {
                    'task_status': 'awaiting_review',
                    'task_kind': 'review',
                    'team_id': 'TEAM-0001',
                    'aircraft_id': 'DEMO-AIR-0001',
                    'issue_status': 'open',
                },
                {
                    'limit': 3,
                    'status': 'awaiting_review',
                    'task_kind': 'review',
                    'assigned_team_id': 'TEAM-0001',
                    'aircraft_id': 'DEMO-AIR-0001',
                },
            ),
            (
                'count_issues',
                {'group_by': 'status'},
                {'issue_status': 'unresolved'},
                {'group_by': 'status', 'unresolved_only': True},
            ),
            ('search_issues', {}, {'issue_status': 'unresolved'}, {'unresolved_only': True}),
            (
                'count_issues',
                {'status': 'open'},
                {'issue_status': 'unresolved'},
                {'status': 'open', 'unresolved_only': True},
            ),
            ('list_open_issues', {}, {'issue_status': 'unresolved'}, {}),
            (
                'search_issues',
                {},
                {'opened_after': '2026-01-01', 'opened_before': '2026-01-01'},
                {'opened_after': '2026-01-01T00:00:00+00:00', 'opened_before': '2026-01-01T23:59:59.999999+00:00'},
            ),
            (
                'search_documents',
                {'query': 'queue sweep'},
                {'document_kind': 'memo', 'related_id': 'ISSUE-0001'},
                {'query': 'queue sweep', 'kind': 'memo', 'related_id': 'ISSUE-0001'},
            ),
            (
                'search_documents',
                {},
                {'entity': 'documents', 'aircraft_id': 'DEMO-AIR-0001'},
                {'related_id': 'DEMO-AIR-0001'},
            ),
            (
                'search_documents',
                {},
                {'entity': 'documents', 'aircraft_id': 'DEMO-AIR-0001', 'related_id': 'TASK-0001'},
                {'related_id': 'TASK-0001'},
            ),
            ('search_documents', {}, {'entity': 'issues', 'aircraft_id': 'DEMO-AIR-0001'}, {}),
            ('get_issue_history', {'issue_id': 'ISSUE-0001'}, VALID, {'issue_id': 'ISSUE-0001'}),
            (
                'compare_aircraft',
                {'aircraft_ids': ['DEMO-AIR-0001']},
                {'aircraft_id': 'DEMO-AIR-0001'},
                {'aircraft_ids': ['DEMO-AIR-0001']},
            ),
            ('search_issues', {'priority': 'high'}, {'priority': 'high'}, {'priority': 'high'}),
            ('list_tasks', {'status': 'completed'}, {}, {'status': 'completed'}),
        ]
        for tool, args, scope, expected in cases:
            with self.subTest(tool=tool, scope=scope):
                self.assertEqual(self.apply(tool, args, scope), expected)
        self.assertEqual(self.apply('search_issues', {}, VALID, {'properties': {}}), {})
        self.assertEqual(self.apply('unknown_tool', {'x': 1}, VALID, {'properties': {}}), {'x': 1})

    def test_conflicts_are_field_specific_errors(self):
        cases = [
            ('list_open_issues', {'aircraft_id': 'DEMO-AIR-0002'}, {'aircraft_id': 'DEMO-AIR-0001'}, 'aircraft_id'),
            ('search_issues', {'subsystem': 'cabin'}, {'subsystem': 'controls'}, 'subsystem'),
            ('search_issues', {'priority': None}, {'priority': 'high'}, 'priority'),
            ('search_issues', {'status': 'open'}, {'issue_status': 'closed'}, 'status'),
            ('count_issues', {'status': 'closed'}, {'issue_status': 'unresolved'}, 'status'),
            ('search_issues', {'unresolved_only': False}, {'issue_status': 'unresolved'}, 'unresolved_only'),
            ('count_issues', {'unresolved_only': True}, {'issue_status': 'closed'}, 'unresolved_only'),
            ('list_tasks', {'status': 'completed'}, {'task_status': 'todo'}, 'status'),
            ('list_tasks', {'task_kind': 'review'}, {'task_kind': 'documentation'}, 'task_kind'),
            ('list_tasks', {'assigned_team_id': 'TEAM-0002'}, {'team_id': 'TEAM-0001'}, 'assigned_team_id'),
            ('search_issues', {'opened_after': '2025-12-01T00:00:00Z'}, {'opened_after': '2026-01-01'}, 'opened_after'),
            ('search_documents', {'kind': 'policy'}, {'document_kind': 'memo'}, 'kind'),
            (
                'search_documents',
                {'related_id': 'TASK-0001'},
                {'entity': 'documents', 'aircraft_id': 'DEMO-AIR-0001'},
                'related_id',
            ),
            (
                'compare_aircraft',
                {'aircraft_ids': ['DEMO-AIR-0001', 'DEMO-AIR-0002']},
                {'aircraft_id': 'DEMO-AIR-0001'},
                'aircraft_ids',
            ),
        ]
        for tool, args, scope, field in cases:
            with (
                self.subTest(tool=tool, field=field),
                self.assertRaisesRegex(ValueError, '^Outside the selected scope: ' + field + '$'),
            ):
                self.apply(tool, args, scope)

    def test_date_equivalence_and_unsupported_unresolved_fail_closed(self):
        args = {'opened_after': '2026-01-01T00:00:00Z'}
        self.assertEqual(self.apply('search_issues', args, {'opened_after': '2026-01-01'}), args)
        with self.assertRaisesRegex(ValueError, 'Outside the selected scope: status'):
            self.apply('search_issues', {}, {'issue_status': 'unresolved'}, {'properties': {'status': {}}})

    def test_dates_allow_narrowing_and_refuse_widening_or_ambiguous_timestamps(self):
        scope = {'opened_after': '2026-01-01', 'opened_before': '2026-02-01'}
        for args in (
            {'opened_after': '2026-01-10T08:00:00Z', 'opened_before': '2026-01-20T09:00:00Z'},
            {'opened_after': '2026-01-01T01:00:00+01:00', 'opened_before': '2026-02-01T18:59:59.999999-05:00'},
        ):
            self.assertEqual(self.apply('search_issues', args, scope), args)
        self.assertEqual(
            self.apply('search_issues', {'opened_after': '2026-01-10'}, {'opened_after': '2026-01-01'}),
            {'opened_after': '2026-01-10T00:00:00+00:00'},
        )
        for field, value in (
            ('opened_after', '2025-12-31T23:59:59Z'),
            ('opened_before', '2026-02-02T00:00:00Z'),
            ('opened_before', '2026-02-02'),
            ('opened_after', '2026-01-10T00:00:00'),
            ('opened_after', 'invalid'),
        ):
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, field):
                self.apply('search_issues', {field: value}, scope)

    def test_enums_canonicalize_case_but_ids_remain_exact(self):
        for tool, field, source, canonical in (
            ('search_issues', 'subsystem', 'subsystem', 'cabin'),
            ('search_issues', 'priority', 'priority', 'high'),
            ('search_issues', 'status', 'issue_status', 'blocked'),
            ('list_tasks', 'status', 'task_status', 'awaiting_review'),
            ('list_tasks', 'task_kind', 'task_kind', 'review'),
            ('search_documents', 'kind', 'document_kind', 'memo'),
        ):
            with self.subTest(field=field):
                self.assertEqual(self.apply(tool, {field: canonical.upper()}, {source: canonical}), {field: canonical})
        self.assertEqual(
            self.apply('search_issues', {'status': 'OPEN'}, {'issue_status': 'unresolved'}),
            {'status': 'open', 'unresolved_only': True},
        )
        for tool, field, source, value in (
            ('search_issues', 'aircraft_id', 'aircraft_id', 'demo-air-0001'),
            ('list_tasks', 'assigned_team_id', 'team_id', 'team-0001'),
            ('search_documents', 'related_id', 'related_id', 'issue-0001'),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                self.apply(tool, {field: value}, {source: value.upper()})

    def test_closed_cannot_reach_unresolved_only_tools(self):
        for tool, args, scope in (
            ('list_open_issues', {}, {'issue_status': 'closed'}),
            ('search_issues', {'status': 'CLOSED'}, {'issue_status': 'unresolved'}),
        ):
            with self.subTest(tool=tool), self.assertRaisesRegex(ValueError, '^Outside the selected scope: status$'):
                self.apply(tool, args, scope)


class ResultScope(unittest.TestCase):
    def check(self, payload, scope):
        from backend.scope import result_within_scope

        before = deepcopy((payload, scope))
        result = result_within_scope(payload, scope)
        self.assertEqual((payload, scope), before)
        return result

    def test_nested_issue_records_match_all_selected_fields(self):
        issue = {
            'issue_id': 'ISSUE-0001',
            'aircraft_id': 'DEMO-AIR-0001',
            'subsystem': 'cabin',
            'priority': 'high',
            'status': 'blocked',
        }
        scope = {'aircraft_id': 'DEMO-AIR-0001', 'subsystem': 'cabin', 'priority': 'high', 'issue_status': 'unresolved'}
        self.assertTrue(self.check({'issue': issue}, scope))
        self.assertTrue(self.check({'items': [{**issue, 'status': 'OPEN', 'priority': 'HIGH'}]}, scope))
        for field, value in (
            ('aircraft_id', 'demo-air-0001'),
            ('subsystem', 'avionics'),
            ('priority', 'low'),
            ('status', 'closed'),
            ('status', None),
        ):
            with self.subTest(field=field):
                self.assertFalse(self.check({'nested': [{'items': [issue, {**issue, field: value}]}]}, scope))
        self.assertFalse(self.check({'issue': issue}, {'issue_status': 'open'}))
        self.assertFalse(self.check({'issue_id': 'ISSUE-0001'}, scope))
        self.assertTrue(self.check({'issue': issue}, {}))

    def test_tasks_ignore_issue_fields_and_check_task_fields(self):
        task = {
            'task_id': 'TASK-0001',
            'issue_id': 'ISSUE-0001',
            'status': 'awaiting_review',
            'task_kind': 'review',
            'assigned_team_id': 'TEAM-0001',
        }
        scope = {
            'aircraft_id': 'DEMO-AIR-0001',
            'task_status': 'awaiting_review',
            'task_kind': 'review',
            'team_id': 'TEAM-0001',
            'issue_status': 'closed',
            'priority': 'low',
        }
        self.assertTrue(self.check({'items': [task]}, scope))
        self.assertTrue(self.check({**task, 'aircraft_id': 'DEMO-AIR-0001', 'status': 'AWAITING_REVIEW'}, scope))
        for field, value in (
            ('aircraft_id', 'DEMO-AIR-0002'),
            ('status', 'completed'),
            ('task_kind', 'documentation'),
            ('assigned_team_id', 'team-0001'),
        ):
            with self.subTest(field=field):
                self.assertFalse(self.check({**task, field: value}, scope))
        self.assertFalse(self.check({'task_id': 'TASK-0001'}, scope))

    def test_events_documents_aggregates_and_empty_results_are_not_records(self):
        payloads: tuple[Any, ...] = (
            {'events': [{'event_id': 'EVT-0001', 'issue_id': 'ISSUE-0001', 'task_id': 'TASK-0001'}]},
            {'results': [{'passage_id': 'DOC-0001#1', 'doc_id': 'DOC-0001', 'related_ids': ['DEMO-AIR-0002']}]},
            {'total': 2, 'groups': [{'key': 'low', 'count': 2}]},
            {'items': []},
            None,
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertTrue(self.check(payload, VALID))
