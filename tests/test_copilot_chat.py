"""Adapter orchestration against a fake Copilot client. This is not live-model evidence."""

import asyncio
import dataclasses
import time
from types import SimpleNamespace
from typing import Any
import unittest

from backend.copilot_chat import provider_config, stream_answer
from backend.settings import ChatSettings
from preflight_core.tools import TOOLS

SETTINGS = ChatSettings('a' * 64, 'b' * 64, 'c' * 64, mcp_url='http://127.0.0.1:8001/mcp')
BYOK = ChatSettings(
    'a' * 64,
    None,
    'c' * 64,
    mcp_url='http://127.0.0.1:8001/mcp',
    model='claude-sonnet-5-5',
    provider='anthropic',
    model_api_key='sk-ant-' + 'k' * 40,
)
PAGE = {
    'total': 1,
    'limit': 50,
    'offset': 0,
    'items': [
        {
            'issue_id': 'ISSUE-0001',
            'aircraft_id': 'DEMO-AIR-0001',
            'title': 'Fictional cabin record question 0001',
            'description': 'Invented queue item.',
            'subsystem': 'cabin',
            'priority': 'low',
            'status': 'open',
            'opened_at': '2026-01-02T00:00:00+00:00',
            'closed_at': None,
        }
    ],
}
HANG = object()


def event(kind, **data):
    return SimpleNamespace(type=kind, data=SimpleNamespace(**data), agent_id=None)


def tool_call(server='preflight', success=True):
    return [
        event('tool.execution_start', mcp_server_name=server, mcp_tool_name='list_open_issues', tool_call_id='call-1'),
        event(
            'tool.execution_complete',
            tool_call_id='call-1',
            success=success,
            result=SimpleNamespace(structured_content=PAGE, content=''),
        ),
    ]


ANSWER = [event('assistant.message_delta', delta_content='One unresolved issue: ISSUE-0001.')]


class FakeSession:
    def __init__(self, options, script, *, abort_hangs=False, abort_events=(), detach_raises=False):
        self.options = options
        self.script = script
        self.abort_hangs = abort_hangs
        self.abort_events = abort_events
        self.detach_raises = detach_raises
        self.aborted = False
        self.detached = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.detached = True
        if self.detach_raises:
            raise RuntimeError('Failed to detach session')

    async def send_and_wait(self, prompt, **options):
        for item in self.script:
            if item is HANG:
                await asyncio.Future()
            self.options['on_event'](item)
        return None

    async def abort(self):
        self.aborted = True
        for item in self.abort_events:
            self.options['on_event'](item)
        if self.abort_hangs:
            await asyncio.Future()


def fake_runtime(
    script, *, authenticated=True, start_raises=False, stop_hangs=False, stop_raises=False, **session_kwargs
):
    """A client factory with the surface `stream_answer` uses, recording what it was given."""
    seen: dict[str, Any] = {'stop_started': asyncio.Event()}

    class Client:
        def __init__(self, **kwargs):
            seen['client'] = kwargs

        async def __aenter__(self):
            if start_raises:
                raise RuntimeError('runtime failed to start')
            return self

        async def __aexit__(self, *exc):
            seen['stopped'] = True
            seen['stop_started'].set()
            if stop_hangs:
                await asyncio.Future()
            if stop_raises:
                raise RuntimeError('runtime stop failed')

        async def force_stop(self):
            seen['force_stopped'] = True

        async def get_auth_status(self):
            seen['auth_checked'] = True
            return SimpleNamespace(isAuthenticated=authenticated)

        async def create_session(self, **options):
            seen['options'] = options
            seen['session'] = FakeSession(options, script, **session_kwargs)
            return seen['session']

    return Client, seen


async def collect(factory, **kwargs):
    events = []
    async for item in stream_answer('Summary', SETTINGS, client_factory=factory, **kwargs):
        events.append(item)
    return events


class AdapterOrchestration(unittest.IsolatedAsyncioTestCase):
    async def test_success_forwards_tool_activity_text_then_sources_evidence_done(self):
        factory, seen = fake_runtime(tool_call() + ANSWER)
        events = await collect(factory)
        self.assertEqual([e['event'] for e in events], ['tool', 'tool', 'delta', 'sources', 'evidence', 'done'])
        self.assertEqual(events[0]['data'], {'name': 'list_open_issues', 'status': 'running', 'call_id': 'call-1'})
        self.assertEqual(events[3]['data'], {'records': ['DEMO-AIR-0001', 'ISSUE-0001'], 'tools': ['list_open_issues']})
        self.assertEqual(events[4]['data']['results'][0]['tool'], 'list_open_issues')
        self.assertEqual(events[5]['data'], {'tool_calls': 1})
        self.assertTrue(seen['session'].aborted and seen['session'].detached and seen['stopped'])

    async def test_runtime_receives_only_the_copilot_token_and_a_throwaway_home(self):
        factory, seen = fake_runtime(tool_call() + ANSWER)
        await collect(factory)
        client = seen['client']
        self.assertEqual(client['github_token'], 'b' * 64)
        self.assertFalse(client['use_logged_in_user'])
        self.assertLessEqual(
            set(client['env']), {'PATH', 'LANG', 'COPILOT_CLI_PATH', 'HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME'}
        )
        for value in client['env'].values():
            for secret in ('a' * 64, 'b' * 64, 'c' * 64):
                self.assertNotIn(secret, value)
        self.assertTrue(client['env']['HOME'].startswith(client['base_directory'][:4]))
        options = seen['options']
        self.assertEqual(
            options['available_tools'],
            ['mcp:preflight-' + name for name in TOOLS],
        )
        self.assertEqual(options['mcp_servers']['preflight']['url'], SETTINGS.mcp_url)
        self.assertEqual(options['mcp_servers']['preflight']['headers'], {'Authorization': 'Bearer ' + 'c' * 64})
        self.assertFalse(
            options['enable_skills'] or options['enable_file_hooks'] or options['enable_host_git_operations']
        )

    async def test_failure_recorded_during_cleanup_cannot_become_success(self):
        late = [event('session.error', message='injected late session failure')]
        factory, _ = fake_runtime(tool_call() + ANSWER, abort_events=late)
        events = []
        with self.assertRaises(RuntimeError):
            async for item in stream_answer('Summary', SETTINGS, client_factory=factory):
                events.append(item['event'])
        self.assertNotIn('done', events)
        self.assertNotIn('sources', events)

    async def test_unexpected_tool_or_failed_result_stops_the_answer(self):
        for script in (tool_call(server='other') + ANSWER, tool_call(success=False) + ANSWER, ANSWER):
            with self.subTest(script=[getattr(e.type, 'value', e.type) for e in script]):
                factory, seen = fake_runtime(script)
                events = []
                with self.assertRaises(RuntimeError):
                    async for item in stream_answer('Summary', SETTINGS, client_factory=factory):
                        events.append(item['event'])
                self.assertNotIn('done', events)
                self.assertTrue(seen['session'].aborted and seen['stopped'])

    async def test_unauthenticated_runtime_fails_closed(self):
        factory, seen = fake_runtime(tool_call() + ANSWER, authenticated=False)
        with self.assertRaisesRegex(RuntimeError, 'authentication'):
            await collect(factory)
        self.assertNotIn('session', seen)
        self.assertTrue(seen['stopped'])

    async def test_stalled_runtime_cleanup_is_bounded_after_the_transport_deadline(self):
        factory, seen = fake_runtime([*tool_call(), HANG], abort_hangs=True)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            async with asyncio.timeout(0.3):
                await collect(factory, cleanup_seconds=0.1)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(seen['session'].aborted and seen['session'].detached and seen['stopped'])

    async def test_cleanup_failure_is_not_a_successful_answer(self):
        for kwargs in ({'detach_raises': True}, {'stop_raises': True}):
            with self.subTest(**kwargs):
                factory, seen = fake_runtime(tool_call() + ANSWER, **kwargs)
                events = []
                with self.assertRaisesRegex(RuntimeError, 'cleanup failed'):
                    async for item in stream_answer('Summary', SETTINGS, client_factory=factory):
                        events.append(item['event'])
                self.assertEqual(events, ['tool', 'tool', 'delta'])
                self.assertTrue(seen['session'].detached and seen['stopped'])

    async def test_stalled_runtime_stop_is_forced_and_reported(self):
        factory, seen = fake_runtime(tool_call() + ANSWER, stop_hangs=True)
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, 'runtime stop'):
            await collect(factory, cleanup_seconds=0.1)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(seen['force_stopped'])

    async def test_consumer_going_away_during_a_stalled_stop_still_forces_the_runtime_down(self):
        factory, seen = fake_runtime([*tool_call(), *ANSWER], stop_hangs=True)
        stream = stream_answer('Summary', SETTINGS, client_factory=factory, cleanup_seconds=0.2)
        for _ in range(3):
            await anext(stream)
        await asyncio.wait_for(seen['stop_started'].wait(), 2)  # the graceful stop is in progress and never returns
        await stream.aclose()  # the browser disconnected while the stop was in progress
        self.assertTrue(seen['force_stopped'])

    async def test_failed_runtime_start_still_kills_what_was_spawned(self):
        factory, seen = fake_runtime(tool_call() + ANSWER, start_raises=True)
        with self.assertRaisesRegex(RuntimeError, 'failed to start'):
            await collect(factory)
        self.assertTrue(seen['force_stopped'])
        self.assertNotIn('session', seen)

    async def test_abandoned_cleanup_failures_are_logged_not_lost(self):
        from backend.copilot_chat import ABANDONED, finish_abandoned

        async def late_failure():
            raise RuntimeError('late cleanup failure')

        task = asyncio.create_task(late_failure())
        ABANDONED.add(task)
        task.add_done_callback(finish_abandoned)
        with self.assertLogs('preflight.copilot', 'WARNING') as logs:
            await asyncio.wait({task})
            await asyncio.sleep(0)  # let the done callback run
        self.assertIn('RuntimeError', logs.output[0])
        self.assertNotIn(task, ABANDONED)

    async def test_consumer_going_away_cleans_up_the_session(self):
        factory, seen = fake_runtime([*tool_call(), *ANSWER, HANG])
        stream = stream_answer('Summary', SETTINGS, client_factory=factory)
        received = [(await anext(stream))['event'] for _ in range(3)]
        await stream.aclose()  # the browser disconnected; the transport closes the generator
        self.assertEqual(received, ['tool', 'tool', 'delta'])
        self.assertTrue(seen['session'].aborted and seen['session'].detached and seen['stopped'])


class ProviderSelection(unittest.IsolatedAsyncioTestCase):
    async def test_byok_session_carries_the_provider_and_skips_copilot_authentication(self):
        factory, seen = fake_runtime(tool_call() + ANSWER, authenticated=False)
        events = [item async for item in stream_answer('Summary', BYOK, client_factory=factory)]
        self.assertIsNone(seen['client']['github_token'])
        self.assertNotIn('auth_checked', seen)
        provider = seen['options']['provider']
        self.assertEqual(provider['type'], 'anthropic')
        self.assertEqual(provider['model_id'], 'claude-sonnet-5-5')
        self.assertEqual(provider['base_url'], 'https://api.anthropic.com')  # the runtime requires baseUrl
        self.assertEqual(seen['options']['model'], 'claude-sonnet-5-5')
        self.assertEqual(events[-1]['event'], 'done')

    async def test_copilot_provider_still_requires_authentication(self):
        factory, seen = fake_runtime(tool_call() + ANSWER, authenticated=False)
        with self.assertRaisesRegex(RuntimeError, 'authentication'):
            await collect(factory)
        self.assertTrue(seen['auth_checked'])

    def test_anthropic_workspace_header_is_sent_only_when_configured(self):
        self.assertNotIn('headers', provider_config(BYOK))
        scoped = dataclasses.replace(BYOK, workspace_id='wrkspc_01ABC')
        self.assertEqual(provider_config(scoped)['headers'], {'anthropic-workspace-id': 'wrkspc_01ABC'})

    def test_openai_provider_uses_the_responses_wire_api(self):
        settings = dataclasses.replace(BYOK, provider='openai', model='gpt-5.5')
        self.assertEqual(provider_config(settings)['wire_api'], 'responses')
        self.assertEqual(provider_config(settings)['base_url'], 'https://api.openai.com/v1')
        self.assertNotIn('wire_api', provider_config(BYOK))


if __name__ == '__main__':
    unittest.main()
