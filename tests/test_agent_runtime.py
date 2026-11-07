"""The native loop against a scripted model and fixture-backed tools. This is not live-model evidence."""

import asyncio
from typing import Any
import unittest

from test_mcp import FixtureRepository

from backend.agent_runtime import ANSWER_CHARS, stream_answer
from backend.mcp_tools import ToolResult, ToolSpec
from backend.model_client import ToolCall, Turn
from backend.settings import ChatSettings
from preflight_core.tools import TOOLS

SETTINGS = ChatSettings(
    'a' * 64,
    None,
    'c' * 64,
    mcp_url='http://127.0.0.1:8001/mcp',
    model='claude-sonnet-5-5',
    provider='anthropic',
    model_api_key='sk-ant-' + 'k' * 40,
    runtime='native',
)
METHODS = {
    'get_aircraft_summary': 'aircraft_summary',
    'list_open_issues': 'open_issues',
    'get_issue_history': 'issue_history',
}
DEFAULTS: dict[str, dict[str, Any]] = {
    'list_open_issues': {'aircraft_id': None, 'priority': None, 'limit': 50, 'offset': 0},
    'get_issue_history': {'limit': 100, 'offset': 0},
}


def final(cited=(), numbers=()):
    return ToolCall('final-1', 'final_answer', {'cited_ids': list(cited), 'numbers': list(numbers)})


def claims(cited=(), numbers=()):
    """The model's reply to the forced claims round."""
    return Turn('', [final(cited, numbers)], 'tool_use')


CLAIMS_CHOICE = {
    'type': 'auto'
}  # a forced tool choice is not supported by every model; the only tool offered is final_answer


class FakeModel:
    model = 'fake-model'

    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = []

    async def stream_turn(self, *, system, messages, tools, tool_choice, on_text):
        self.calls.append({'messages': [dict(m) for m in messages], 'tools': tools, 'tool_choice': tool_choice})
        turn = self.turns.pop(0)
        half = len(turn.text) // 2
        for chunk in (turn.text[:half], turn.text[half:]):
            if chunk:
                on_text(chunk)
        return turn


class FakeTools:
    def __init__(self, *, fail=(), barrier=0):
        self.repo = FixtureRepository()
        self.fail = set(fail)
        self.barrier = barrier
        self.active = 0
        self.max_active = 0
        self.calls = []

    async def discover(self):
        return [
            ToolSpec(name, 'd', {'type': 'object', 'properties': {a: {} for a in TOOLS[name].arguments}})
            for name in TOOLS
        ]

    async def call(self, call_id, name, arguments):
        self.calls.append((call_id, name, dict(arguments)))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.barrier:
                for _ in range(50):
                    if self.active >= self.barrier:
                        break
                    await asyncio.sleep(0.01)
            if name in self.fail or name not in TOOLS:
                return ToolResult(name, call_id, False, error='Tool unavailable')
            merged = {**DEFAULTS.get(name, {}), **arguments}
            method = getattr(self.repo, METHODS[name])
            payload = method(*[merged[a] for a in ARGUMENT_ORDER[name]])
            return ToolResult(
                name, call_id, True, payload=TOOLS[name].result.model_validate(payload).model_dump(mode='json')
            )
        finally:
            self.active -= 1


ARGUMENT_ORDER = {
    'get_aircraft_summary': ['aircraft_id'],
    'list_open_issues': ['aircraft_id', 'priority', 'limit', 'offset'],
    'get_issue_history': ['issue_id', 'limit', 'offset'],
}


async def collect(model, tools, question='Summary', settings=SETTINGS):
    return [event async for event in stream_answer(question, settings, model_client=model, tools=tools)]


class NativeLoop(unittest.IsolatedAsyncioTestCase):
    async def test_single_tool_round_streams_text_then_sources_evidence_done(self):
        model = FakeModel(
            [
                Turn('', [ToolCall('t1', 'list_open_issues', {'limit': 5})], 'tool_use'),
                Turn('Five unresolved issues, starting with ISSUE-0001.', [], 'end_turn'),
                claims(['ISSUE-0001']),
            ]
        )
        tools = FakeTools()
        events = await collect(model, tools)
        self.assertEqual(len(model.calls), 3)
        self.assertNotIn('final_answer', [t['name'] for t in model.calls[0]['tools']])
        self.assertEqual([t['name'] for t in model.calls[2]['tools']], ['final_answer'])
        self.assertEqual(model.calls[2]['tool_choice'], CLAIMS_CHOICE)
        self.assertIn('t1', model.calls[2]['messages'][-1]['content'])
        self.assertIn('list_open_issues', model.calls[2]['messages'][-1]['content'])
        kinds = [e['event'] for e in events]
        self.assertEqual(kinds[:2], ['tool', 'tool'])
        self.assertEqual(events[0]['data'], {'name': 'list_open_issues', 'status': 'running', 'call_id': 't1'})
        self.assertEqual(events[1]['data']['status'], 'complete')
        self.assertEqual(
            ''.join(e['data']['text'] for e in events if e['event'] == 'delta'),
            'Five unresolved issues, starting with ISSUE-0001.',
        )
        self.assertEqual(kinds[-3:], ['sources', 'evidence', 'done'])
        self.assertIn('ISSUE-0001', events[-3]['data']['records'])
        self.assertEqual(events[-3]['data']['tools'], ['list_open_issues'])
        self.assertEqual(events[-2]['data']['results'][0]['tool'], 'list_open_issues')
        self.assertEqual(events[-2]['data']['results'][0]['call_id'], 't1')
        self.assertEqual(events[-1]['data']['tool_calls'], 1)
        self.assertEqual(events[-1]['data']['rounds'], 2)
        self.assertIsInstance(events[-1]['data']['verified'], bool)
        tool_result = model.calls[1]['messages'][-1]['content'][0]
        self.assertEqual((tool_result['type'], tool_result['tool_use_id']), ('tool_result', 't1'))
        self.assertIn('"total"', tool_result['content'])

    async def test_parallel_tool_calls_run_concurrently(self):
        model = FakeModel(
            [
                Turn(
                    '',
                    [
                        ToolCall('a', 'get_aircraft_summary', {'aircraft_id': 'DEMO-AIR-0001'}),
                        ToolCall('b', 'get_aircraft_summary', {'aircraft_id': 'DEMO-AIR-0002'}),
                    ],
                    'tool_use',
                ),
                Turn('Both compared.', [], 'end_turn'),
                claims(['DEMO-AIR-0001', 'DEMO-AIR-0002']),
            ]
        )
        tools = FakeTools(barrier=2)
        events = await collect(model, tools)
        self.assertEqual(tools.max_active, 2)
        running = [e['data']['call_id'] for e in events if e['event'] == 'tool' and e['data']['status'] == 'running']
        self.assertEqual(sorted(running), ['a', 'b'])
        self.assertEqual(events[-1]['data']['tool_calls'], 2)

    async def test_unknown_tool_is_refused_without_calling_mcp(self):
        model = FakeModel(
            [Turn('', [ToolCall('x', 'drop_table', {})], 'tool_use'), Turn('Nothing.', [], 'end_turn'), claims()]
        )
        tools = FakeTools()
        events = await collect(model, tools)
        self.assertEqual(tools.calls, [])
        failed = [e for e in events if e['event'] == 'tool' and e['data']['status'] == 'failed']
        self.assertEqual(failed[0]['data']['call_id'], 'x')
        reply = model.calls[1]['messages'][-1]['content'][0]
        self.assertEqual((reply['type'], reply['tool_use_id'], reply['is_error']), ('tool_result', 'x', True))

    async def test_failed_tool_result_reaches_the_model_as_error(self):
        model = FakeModel(
            [
                Turn('', [ToolCall('h', 'get_issue_history', {'issue_id': 'ISSUE-0001'})], 'tool_use'),
                Turn('Unknown.', [], 'end_turn'),
                claims(),
            ]
        )
        tools = FakeTools(fail={'get_issue_history'})
        events = await collect(model, tools)
        self.assertEqual([e['data']['status'] for e in events if e['event'] == 'tool'], ['running', 'failed'])
        reply = model.calls[1]['messages'][-1]['content'][0]
        self.assertTrue(reply['is_error'])
        self.assertNotIn('Traceback', reply['content'])
        self.assertEqual(events[-2]['data']['results'], [])

    async def test_round_and_call_limits_are_enforced(self):
        tool_turns = [Turn('', [ToolCall(f't{i}', 'list_open_issues', {'limit': 1})], 'tool_use') for i in range(6)]
        model = FakeModel([*tool_turns, Turn('Done from what I have.', [], 'end_turn'), claims()])
        tools = FakeTools()
        events = await collect(model, tools)
        self.assertEqual(len(model.calls), 8)  # six rounds, a wrap-up round without tools, the claims round
        self.assertEqual(model.calls[6]['tool_choice'], {'type': 'none'})
        self.assertEqual(model.calls[7]['tool_choice'], CLAIMS_CHOICE)
        self.assertEqual(events[-1]['data']['rounds'], 6)
        self.assertIn('Done from what I have.', ''.join(e['data']['text'] for e in events if e['event'] == 'delta'))
        many = Turn('', [ToolCall(f'c{i}', 'list_open_issues', {'limit': 1}) for i in range(13)], 'tool_use')
        model = FakeModel([many, Turn('Done.', [], 'end_turn'), claims()])
        tools = FakeTools()
        events = await collect(model, tools)
        self.assertEqual(len(tools.calls), 12)
        results = model.calls[1]['messages'][-1]['content']
        self.assertEqual(len(results), 13)
        self.assertTrue(results[-1]['is_error'])
        self.assertEqual(events[-1]['data']['tool_calls'], 12)

    async def test_prose_is_followed_by_one_claims_round_whose_text_is_not_streamed(self):
        model = FakeModel(
            [Turn('Plain text answer.', [], 'end_turn'), Turn('Here are the claims.', [final()], 'tool_use')]
        )
        events = await collect(model, FakeTools())
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(model.calls[1]['tool_choice'], CLAIMS_CHOICE)
        self.assertEqual(events[-1]['data']['rounds'], 1)
        self.assertEqual(''.join(e['data']['text'] for e in events if e['event'] == 'delta'), 'Plain text answer.')

    async def test_fabricated_number_marks_the_answer_unverified(self):
        model = FakeModel(
            [
                Turn('', [ToolCall('t1', 'list_open_issues', {'limit': 5})], 'tool_use'),
                Turn('There are 999 unresolved issues.', [], 'end_turn'),
                claims([], [{'label': 'unresolved total', 'value': 999, 'call_id': 't1', 'path': 'total'}]),
            ]
        )
        events = await collect(model, FakeTools())
        done = events[-1]['data']
        self.assertFalse(done['verified'])
        self.assertEqual(done['unverified'], 1)
        text = ''.join(e['data']['text'] for e in events if e['event'] == 'delta')
        self.assertIn('Unverified: unresolved total (999) does not match the tool result', text)

    async def test_consumer_going_away_cancels_in_flight_tools(self):
        started = asyncio.Event()
        outcome = {}

        class HangingTools(FakeTools):
            async def call(self, call_id, name, arguments):
                started.set()
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    outcome['cancelled'] = True
                    raise
                outcome['finished'] = True
                return ToolResult(name, call_id, False, error='Tool unavailable')

        model = FakeModel([Turn('', [ToolCall('t1', 'list_open_issues', {'limit': 1})], 'tool_use')])
        stream = stream_answer('Summary', SETTINGS, model_client=model, tools=HangingTools())
        first = await stream.__anext__()
        self.assertEqual(first['data']['status'], 'running')
        await asyncio.wait_for(started.wait(), 1)
        await stream.aclose()
        await asyncio.sleep(0)
        self.assertEqual(outcome, {'cancelled': True})

    async def test_answer_text_cap_is_enforced(self):
        model = FakeModel([Turn('x' * (ANSWER_CHARS + 1), [], 'end_turn')])
        with self.assertRaisesRegex(RuntimeError, 'limit'):
            await collect(model, FakeTools())


if __name__ == '__main__':
    unittest.main()
