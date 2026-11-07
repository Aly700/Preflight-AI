"""The model client wrapper against a fake SDK; no provider is contacted."""

from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from anthropic import APIConnectionError, CredentialsError
import httpx2

from backend.model_client import AnthropicClient, ModelError, build_model_client
from backend.settings import ChatSettings, SettingsError

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
BEDROCK_SETTINGS = replace(
    SETTINGS,
    provider='bedrock',
    model='us.anthropic.claude-sonnet-4-5-20250929-v1:0',
    model_api_key=None,
    aws_region='us-east-1',
)


def block(**fields):
    return SimpleNamespace(**fields)


class FakeStream:
    def __init__(self, events, final, *, raises=None):
        self.events = events
        self.final = final
        self.raises = raises

    async def __aenter__(self):
        if self.raises:
            raise self.raises
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            for event in self.events:
                yield event

        return gen()

    async def get_final_message(self):
        return self.final


def fake_sdk(stream):
    """A client factory recording its constructor kwargs and the stream kwargs."""
    seen = {}

    class Client:
        def __init__(self, **kwargs):
            seen['client'] = kwargs
            self.messages = SimpleNamespace(stream=self.stream)

        def stream(self, **kwargs):
            seen['stream'] = kwargs
            return stream

    return Client, seen


class ModelClientContract(unittest.IsolatedAsyncioTestCase):
    async def test_stream_turn_forwards_text_and_returns_tool_calls(self):
        final = block(
            stop_reason='tool_use',
            content=[
                block(type='text', text='Hello'),
                block(type='tool_use', id='toolu_1', name='list_open_issues', input={'limit': 5}),
            ],
        )
        stream = FakeStream([block(type='text', text='Hel'), block(type='text', text='lo')], final)
        factory, seen = fake_sdk(stream)
        client = AnthropicClient(api_key='sk-ant-' + 'k' * 40, model='claude-sonnet-5-5', client_factory=factory)
        chunks: list[str] = []
        turn = await client.stream_turn(
            system='sys',
            messages=[{'role': 'user', 'content': 'q'}],
            tools=[{'name': 'list_open_issues', 'description': 'd', 'input_schema': {'type': 'object'}}],
            tool_choice={'type': 'auto'},
            on_text=chunks.append,
        )
        self.assertEqual(chunks, ['Hel', 'lo'])
        self.assertEqual(turn.text, 'Hello')
        self.assertEqual(turn.stop_reason, 'tool_use')
        self.assertEqual(
            [(c.id, c.name, c.input) for c in turn.tool_calls], [('toolu_1', 'list_open_issues', {'limit': 5})]
        )
        self.assertEqual(seen['stream']['model'], 'claude-sonnet-5-5')
        self.assertEqual(seen['stream']['system'], 'sys')
        self.assertEqual(seen['stream']['tool_choice'], {'type': 'auto'})
        self.assertEqual(seen['stream']['tools'][0]['name'], 'list_open_issues')
        self.assertEqual(seen['client']['api_key'], 'sk-ant-' + 'k' * 40)

    async def test_provider_error_becomes_model_error_without_details(self):
        stream = FakeStream([], None, raises=RuntimeError('https://api.anthropic.com/v1/messages key=sk-ant-secret'))
        factory, _ = fake_sdk(stream)
        client = AnthropicClient(api_key='sk-ant-' + 'k' * 40, model='m', client_factory=factory)
        with self.assertRaises(ModelError) as caught:
            await client.stream_turn(system='s', messages=[], tools=[], tool_choice=None, on_text=lambda _: None)
        self.assertNotIn('sk-ant', str(caught.exception))
        self.assertNotIn('http', str(caught.exception))
        self.assertIn('RuntimeError', str(caught.exception))

    def test_build_model_client_rejects_other_providers(self):
        client = build_model_client(SETTINGS)
        self.assertIsInstance(client, AnthropicClient)
        self.assertEqual(client.model, 'claude-sonnet-5-5')
        for provider in ('openai', 'copilot', 'unknown'):
            with self.subTest(provider=provider), self.assertRaisesRegex(SettingsError, 'MODEL_PROVIDER'):
                build_model_client(
                    ChatSettings('a' * 64, 'b' * 64, 'c' * 64, mcp_url='http://x/mcp', provider=provider)
                )

    async def test_build_model_client_uses_bedrock_with_the_region_and_no_keys(self):
        final = block(stop_reason='end_turn', content=[block(type='text', text='Hello')])
        factory, seen = fake_sdk(FakeStream([block(type='text', text='Hello')], final))
        with patch('backend.model_client.AsyncAnthropicBedrock', factory):
            client = build_model_client(BEDROCK_SETTINGS)
        self.assertIsInstance(client, AnthropicClient)
        self.assertEqual(client.model, BEDROCK_SETTINGS.model)
        self.assertEqual(seen['client'], {'aws_region': 'us-east-1'})
        chunks: list[str] = []
        turn = await client.stream_turn(system='s', messages=[], tools=[], tool_choice=None, on_text=chunks.append)
        self.assertEqual(chunks, ['Hello'])
        self.assertEqual(turn.text, 'Hello')
        self.assertEqual(seen['stream']['model'], BEDROCK_SETTINGS.model)

    async def test_bedrock_sdk_error_becomes_model_error_without_details(self):
        errors = (
            CredentialsError('upstream-secret in credential resolution'),
            APIConnectionError(
                message='upstream-secret at https://bedrock.invalid',
                request=httpx2.Request('POST', 'https://bedrock.invalid'),
            ),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                factory, _ = fake_sdk(FakeStream([], None, raises=error))
                with patch('backend.model_client.AsyncAnthropicBedrock', factory):
                    client = build_model_client(BEDROCK_SETTINGS)
                with self.assertRaises(ModelError) as caught:
                    await client.stream_turn(
                        system='s', messages=[], tools=[], tool_choice=None, on_text=lambda _: None
                    )
                self.assertEqual(str(caught.exception), f'Model request failed: {type(error).__name__}')
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__)

    def test_workspace_header_is_set_when_configured(self):
        factory, seen = fake_sdk(FakeStream([], None))
        AnthropicClient(api_key='k' * 40, model='m', workspace_id='wrkspc_01ABC', client_factory=factory)
        self.assertEqual(seen['client']['default_headers'], {'anthropic-workspace-id': 'wrkspc_01ABC'})
        AnthropicClient(api_key='k' * 40, model='m', client_factory=factory)
        self.assertNotIn('default_headers', seen['client'])


if __name__ == '__main__':
    unittest.main()
