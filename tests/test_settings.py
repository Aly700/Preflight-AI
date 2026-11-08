"""Configuration fails closed with a named reason and never echoes a secret value."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.copilot_chat import EventTracker, ToolPolicy, session_options
from backend.settings import ChatSettings, SettingsError
from preflight_core.settings import api_hosts_from_environment, mcp_hosts_from_environment


class ChatSettingsContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.environ = {'PREFLIGHT_MCP_URL': 'http://127.0.0.1:8001/mcp'}
        for name, token in (
            ('CHAT_TOKEN_FILE', 'a' * 64),
            ('COPILOT_TOKEN_FILE', 'b' * 64),
            ('MCP_TOKEN_FILE', 'c' * 64),
        ):
            path = Path(self.temp.name) / name
            path.write_text(token + '\n')
            self.environ[name] = str(path)

    def gateway_environment(self):
        key = Path(self.temp.name) / 'model-key'
        key.write_text('k' * 40)
        return {
            **self.environ,
            'MODEL_PROVIDER': 'anthropic',
            'MODEL_API_KEY_FILE': str(key),
            'COPILOT_MODEL': 'demo-model',
            'CHAT_RUNTIME': 'native',
            'MCP_AUTH': 'sigv4',
            'PREFLIGHT_MCP_URL': 'https://gateway.example.test/mcp',
            'AWS_REGION': 'ca-central-1',
        }

    def test_mcp_auth_defaults_to_bearer_and_rejects_unknown_modes(self):
        settings = ChatSettings.from_environment(self.environ)
        with self.subTest(mode='default'):
            self.assertEqual(getattr(settings, 'mcp_auth', None), 'bearer')
        self.assertEqual(settings.mcp_token, 'c' * 64)
        for mode in ('', 'iam', 'SIGV4'):
            with self.subTest(mode=mode), self.assertRaisesRegex(SettingsError, 'MCP_AUTH'):
                ChatSettings.from_environment({**self.environ, 'MCP_AUTH': mode})

    def test_sigv4_uses_region_without_reading_mcp_token(self):
        environ = self.gateway_environment()
        for token_path in (None, str(Path(self.temp.name) / 'absent')):
            with self.subTest(token_path=token_path):
                environ.pop('MCP_TOKEN_FILE', None)
                if token_path is not None:
                    environ['MCP_TOKEN_FILE'] = token_path
                with patch('backend.settings.read_mcp_token', side_effect=AssertionError('must not read token')):
                    settings = ChatSettings.from_environment(environ)
                self.assertEqual(settings.mcp_auth, 'sigv4')
                self.assertEqual(settings.aws_region, 'ca-central-1')
                self.assertIsNone(settings.mcp_token)

    def test_sigv4_requires_native_or_langgraph_runtime(self):
        environ = self.gateway_environment()
        for provider in ('anthropic', 'bedrock'):
            for runtime in ('', 'copilot'):
                with (
                    self.subTest(provider=provider, runtime=runtime),
                    self.assertRaisesRegex(SettingsError, '^MCP_AUTH=sigv4 requires CHAT_RUNTIME=native or langgraph$'),
                ):
                    ChatSettings.from_environment({**environ, 'CHAT_RUNTIME': runtime, 'MODEL_PROVIDER': provider})

    def test_sigv4_requires_https(self):
        with self.assertRaisesRegex(SettingsError, 'PREFLIGHT_MCP_URL.*https'):
            ChatSettings.from_environment(
                {**self.gateway_environment(), 'PREFLIGHT_MCP_URL': 'http://gateway.example.test/mcp'}
            )

    def test_sigv4_requires_valid_region_for_anthropic(self):
        environ = self.gateway_environment()
        for region in (None, '', 'US-east-1', 'us_east_1', 'us-east', 'us-east-12', 'us--east-1'):
            candidate = {k: v for k, v in environ.items() if k != 'AWS_REGION'}
            if region is not None:
                candidate['AWS_REGION'] = region
            message = 'AWS_REGION is not set' if not region else 'AWS_REGION has an invalid format'
            with self.subTest(region=region), self.assertRaisesRegex(SettingsError, '^' + message + '$'):
                ChatSettings.from_environment(candidate)

    def test_mcp_prefix_is_optional_and_allowed_with_either_auth_mode(self):
        self.assertEqual(getattr(ChatSettings.from_environment(self.environ), 'mcp_tool_prefix', None), '')
        for environ in (self.environ, self.gateway_environment()):
            for prefix in ('', 'target___', '0-Mixed-case___', 'a' * 100 + '___'):
                with self.subTest(mode=environ.get('MCP_AUTH'), prefix=prefix):
                    settings = ChatSettings.from_environment({**environ, 'MCP_TOOL_PREFIX': prefix})
                    self.assertEqual(settings.mcp_tool_prefix, prefix)

    def test_mcp_prefix_rejects_invalid_names_without_echoing_them(self):
        for prefix in ('target', 'target__', 'target____', '-target___', 'a_b___', 'a' * 101 + '___', 'x___\n', ' '):
            with self.subTest(prefix=prefix):
                with self.assertRaisesRegex(SettingsError, 'MCP_TOOL_PREFIX') as caught:
                    ChatSettings.from_environment({**self.environ, 'MCP_TOOL_PREFIX': prefix})
                if prefix.strip():
                    self.assertNotIn(prefix, str(caught.exception))

    def test_copilot_session_requires_a_bearer_token(self):
        settings = ChatSettings('a' * 64, 'b' * 64, None, 'http://example.test/mcp')
        with self.assertRaisesRegex(SettingsError, 'Copilot runtime requires an MCP bearer token'):
            session_options(settings, self.temp.name, EventTracker(lambda event: None), ToolPolicy())

    def test_complete_configuration_loads_and_hides_secrets(self):
        settings = ChatSettings.from_environment(self.environ)
        self.assertEqual(settings.access_token, 'a' * 64)
        self.assertEqual(settings.copilot_token, 'b' * 64)
        self.assertEqual(settings.mcp_token, 'c' * 64)
        self.assertEqual(settings.mcp_url, 'http://127.0.0.1:8001/mcp')
        self.assertIsNone(settings.model)
        self.assertIsNone(settings.aws_region)
        for secret in ('aaaa', 'bbbb', 'cccc'):
            self.assertNotIn(secret, repr(settings))

    def test_model_is_optional_but_validated(self):
        self.assertIsNone(ChatSettings.from_environment({**self.environ, 'COPILOT_MODEL': ''}).model)
        self.assertEqual(
            ChatSettings.from_environment({**self.environ, 'COPILOT_MODEL': 'demo-model-1'}).model, 'demo-model-1'
        )
        with self.assertRaisesRegex(SettingsError, 'COPILOT_MODEL'):
            ChatSettings.from_environment({**self.environ, 'COPILOT_MODEL': 'bad model\n'})

    def test_mcp_url_is_required_and_must_be_a_plain_http_url(self):
        del self.environ['PREFLIGHT_MCP_URL']
        with self.assertRaisesRegex(SettingsError, 'PREFLIGHT_MCP_URL'):
            ChatSettings.from_environment(self.environ)
        for url in (
            'preflight-mcp:8001/mcp',
            'ftp://127.0.0.1/mcp',
            'http://user:secret-value@127.0.0.1:8001/mcp',
            'http:///mcp',
            'http://127.0.0.1/mcp#fragment',
            'http://[bad/mcp',
        ):
            with self.subTest(url=url), self.assertRaisesRegex(SettingsError, 'PREFLIGHT_MCP_URL') as caught:
                ChatSettings.from_environment({**self.environ, 'PREFLIGHT_MCP_URL': url})
            self.assertNotIn('secret-value', str(caught.exception))

    def test_each_credential_file_is_required_and_validated(self):
        for name in ('CHAT_TOKEN_FILE', 'COPILOT_TOKEN_FILE', 'MCP_TOKEN_FILE'):
            with self.subTest(variable=name):
                with self.assertRaisesRegex(SettingsError, name):
                    ChatSettings.from_environment({k: v for k, v in self.environ.items() if k != name})
                with self.assertRaisesRegex(SettingsError, name):
                    ChatSettings.from_environment({**self.environ, name: str(Path(self.temp.name) / 'absent')})
                for bad in ('short', 'x' * 513, 'a' * 40 + ' ' + 'b' * 40, 'é' * 64):
                    Path(self.environ[name]).write_text(bad)
                    with self.assertRaisesRegex(SettingsError, name) as caught:
                        ChatSettings.from_environment(self.environ)
                    self.assertNotIn(bad, str(caught.exception))
                Path(self.environ[name]).write_text('a' * 64)

    def test_default_provider_is_copilot_and_needs_the_copilot_token(self):
        settings = ChatSettings.from_environment(self.environ)
        self.assertEqual(settings.provider, 'copilot')
        self.assertIsNone(settings.model_api_key)
        del self.environ['COPILOT_TOKEN_FILE']
        with self.assertRaisesRegex(SettingsError, 'COPILOT_TOKEN_FILE'):
            ChatSettings.from_environment(self.environ)

    def test_byok_provider_needs_a_key_file_and_a_model_and_not_the_copilot_token(self):
        key = Path(self.temp.name) / 'MODEL_API_KEY_FILE'
        key.write_text('sk-ant-' + 'k' * 40 + '\n')
        environ = {
            **self.environ,
            'MODEL_PROVIDER': 'anthropic',
            'MODEL_API_KEY_FILE': str(key),
            'COPILOT_MODEL': 'claude-sonnet-5-5',
        }
        del environ['COPILOT_TOKEN_FILE']
        settings = ChatSettings.from_environment(environ)
        self.assertEqual(settings.provider, 'anthropic')
        self.assertEqual(settings.model, 'claude-sonnet-5-5')
        self.assertIsNone(settings.copilot_token)
        self.assertEqual(settings.model_api_key, 'sk-ant-' + 'k' * 40)
        self.assertNotIn('kkkk', repr(settings))
        with self.assertRaisesRegex(SettingsError, 'COPILOT_MODEL'):
            ChatSettings.from_environment({**environ, 'COPILOT_MODEL': ''})
        with self.assertRaisesRegex(SettingsError, 'MODEL_API_KEY_FILE'):
            ChatSettings.from_environment({k: v for k, v in environ.items() if k != 'MODEL_API_KEY_FILE'})
        with self.assertRaisesRegex(SettingsError, 'MODEL_PROVIDER'):
            ChatSettings.from_environment({**environ, 'MODEL_PROVIDER': 'unknown'})

    def test_bedrock_needs_a_model_and_a_region_and_no_key_file(self):
        environ = {
            **self.environ,
            'MODEL_PROVIDER': 'bedrock',
            'CHAT_RUNTIME': 'native',
            'COPILOT_MODEL': 'us.anthropic.claude-sonnet-4-5-20250929-v1:0',
            'AWS_REGION': 'us-east-1',
        }
        del environ['COPILOT_TOKEN_FILE']
        for region in ('us-east-1', 'ca-central-1', 'us-gov-west-1'):
            with self.subTest(region=region):
                settings = ChatSettings.from_environment({**environ, 'AWS_REGION': region})
                self.assertEqual(settings.provider, 'bedrock')
                self.assertEqual(settings.runtime, 'native')
                self.assertEqual(settings.model, environ['COPILOT_MODEL'])
                self.assertEqual(settings.aws_region, region)
                self.assertIsNone(settings.model_api_key)
                self.assertIsNone(settings.copilot_token)
        # Even configured key paths must be ignored for this provider.
        unused = str(Path(self.temp.name) / 'absent')
        settings = ChatSettings.from_environment(
            {**environ, 'COPILOT_TOKEN_FILE': unused, 'MODEL_API_KEY_FILE': unused}
        )
        self.assertIsNone(settings.model_api_key)
        self.assertIsNone(settings.copilot_token)
        for name in ('COPILOT_MODEL', 'AWS_REGION'):
            with self.subTest(missing=name), self.assertRaisesRegex(SettingsError, name):
                ChatSettings.from_environment({k: v for k, v in environ.items() if k != name})
            with self.subTest(blank=name), self.assertRaisesRegex(SettingsError, name):
                ChatSettings.from_environment({**environ, name: ' '})
        for region in ('US-east-1', 'us_east_1', 'us-east', 'us-east-12', 'usa-east-1', 'us--east-1', 'us-east-1/path'):
            with self.subTest(invalid_region=region), self.assertRaisesRegex(SettingsError, 'AWS_REGION'):
                ChatSettings.from_environment({**environ, 'AWS_REGION': region})

    def test_bedrock_requires_the_native_runtime(self):
        environ = {
            **self.environ,
            'MODEL_PROVIDER': 'bedrock',
            'COPILOT_MODEL': 'us.anthropic.claude-sonnet-4-5-20250929-v1:0',
            'AWS_REGION': 'us-east-1',
        }
        for runtime in (None, '', 'copilot'):
            candidate = environ if runtime is None else {**environ, 'CHAT_RUNTIME': runtime}
            with self.subTest(runtime=runtime), self.assertRaisesRegex(SettingsError, 'CHAT_RUNTIME'):
                ChatSettings.from_environment(candidate)
        self.assertEqual(ChatSettings.from_environment({**environ, 'CHAT_RUNTIME': 'native'}).runtime, 'native')

    def test_anthropic_workspace_id_is_optional_and_validated(self):
        key = Path(self.temp.name) / 'MODEL_API_KEY_FILE'
        key.write_text('sk-ant-' + 'k' * 40 + '\n')
        environ = {
            **self.environ,
            'MODEL_PROVIDER': 'anthropic',
            'MODEL_API_KEY_FILE': str(key),
            'COPILOT_MODEL': 'claude-sonnet-5-5',
        }
        self.assertIsNone(ChatSettings.from_environment(environ).workspace_id)
        self.assertIsNone(ChatSettings.from_environment({**environ, 'ANTHROPIC_WORKSPACE_ID': ' '}).workspace_id)
        settings = ChatSettings.from_environment({**environ, 'ANTHROPIC_WORKSPACE_ID': 'wrkspc_01ABC'})
        self.assertEqual(settings.workspace_id, 'wrkspc_01ABC')
        with self.assertRaisesRegex(SettingsError, 'ANTHROPIC_WORKSPACE_ID'):
            ChatSettings.from_environment({**environ, 'ANTHROPIC_WORKSPACE_ID': 'bad id\n'})

    def test_runtime_defaults_to_copilot_and_native_requires_anthropic(self):
        self.assertEqual(ChatSettings.from_environment(self.environ).runtime, 'copilot')
        key = Path(self.temp.name) / 'MODEL_API_KEY_FILE'
        key.write_text('sk-ant-' + 'k' * 40 + '\n')
        native = {
            **self.environ,
            'CHAT_RUNTIME': 'native',
            'MODEL_PROVIDER': 'anthropic',
            'MODEL_API_KEY_FILE': str(key),
            'COPILOT_MODEL': 'claude-sonnet-5-5',
        }
        del native['COPILOT_TOKEN_FILE']
        settings = ChatSettings.from_environment(native)
        self.assertEqual((settings.runtime, settings.max_tool_rounds, settings.max_tool_calls), ('native', 6, 12))
        self.assertEqual(ChatSettings.from_environment({**native, 'MAX_TOOL_ROUNDS': '3'}).max_tool_rounds, 3)
        with self.assertRaisesRegex(SettingsError, 'CHAT_RUNTIME'):
            ChatSettings.from_environment({**self.environ, 'CHAT_RUNTIME': 'native'})
        with self.assertRaisesRegex(SettingsError, 'CHAT_RUNTIME'):
            ChatSettings.from_environment({**native, 'CHAT_RUNTIME': 'langchain'})
        with self.assertRaisesRegex(SettingsError, 'MAX_TOOL_ROUNDS'):
            ChatSettings.from_environment({**native, 'MAX_TOOL_ROUNDS': '0'})
        with self.assertRaisesRegex(SettingsError, 'MAX_TOOL_CALLS'):
            ChatSettings.from_environment({**native, 'MAX_TOOL_CALLS': 'many'})

    def test_langgraph_accepts_native_providers_and_auth_modes(self):
        environ = self.gateway_environment()
        for provider in ('anthropic', 'bedrock'):
            for auth in ('bearer', 'sigv4'):
                with self.subTest(provider=provider, auth=auth):
                    settings = ChatSettings.from_environment(
                        {
                            **environ,
                            'CHAT_RUNTIME': 'langgraph',
                            'MODEL_PROVIDER': provider,
                            'MCP_AUTH': auth,
                        }
                    )
                    self.assertEqual(settings.runtime, 'langgraph')
                    self.assertEqual(settings.provider, provider)
                    self.assertEqual(settings.mcp_auth, auth)
                    self.assertEqual((settings.max_tool_rounds, settings.max_tool_calls), (6, 12))
                    if provider == 'bedrock':
                        self.assertIsNone(settings.model_api_key)
                    if auth == 'sigv4':
                        self.assertIsNone(settings.mcp_token)

    def test_both_native_runtimes_reject_other_providers_and_name_the_options(self):
        for runtime in ('native', 'langgraph'):
            for provider in ('copilot', 'openai'):
                with (
                    self.subTest(runtime=runtime, provider=provider),
                    self.assertRaisesRegex(
                        SettingsError, 'CHAT_RUNTIME=native or langgraph requires MODEL_PROVIDER=anthropic or bedrock'
                    ),
                ):
                    ChatSettings.from_environment(
                        {
                            **self.environ,
                            'CHAT_RUNTIME': runtime,
                            'MODEL_PROVIDER': provider,
                        }
                    )

    def test_bedrock_runtime_error_names_both_allowed_runtimes(self):
        with self.assertRaisesRegex(SettingsError, 'CHAT_RUNTIME must be native or langgraph'):
            ChatSettings.from_environment({**self.environ, 'MODEL_PROVIDER': 'bedrock'})

    def test_empty_key_file_is_not_configured(self):
        key = Path(self.temp.name) / 'MODEL_API_KEY_FILE'
        key.write_text('')
        with self.assertRaisesRegex(SettingsError, 'MODEL_API_KEY_FILE'):
            ChatSettings.from_environment(
                {**self.environ, 'MODEL_PROVIDER': 'openai', 'MODEL_API_KEY_FILE': str(key), 'COPILOT_MODEL': 'gpt-5.5'}
            )

    def test_mcp_token_follows_the_mcp_service_rule(self):
        from mcp_service.auth import load_token

        Path(self.environ['MCP_TOKEN_FILE']).write_text('g' * 64)  # right length, not hexadecimal
        with self.assertRaisesRegex(SettingsError, 'MCP_TOKEN_FILE'):
            ChatSettings.from_environment(self.environ)
        with self.assertRaisesRegex(SettingsError, 'MCP_TOKEN_FILE'):
            load_token(self.environ)
        Path(self.environ['MCP_TOKEN_FILE']).write_text('0123456789abcdef' * 4)
        self.assertEqual(ChatSettings.from_environment(self.environ).mcp_token, load_token(self.environ))


class HostAllowlist(unittest.TestCase):
    def test_api_hosts_follow_trusted_host_rules(self):
        default = ['localhost', '127.0.0.1']
        self.assertEqual(api_hosts_from_environment({}, 'PREFLIGHT_ALLOWED_HOSTS', default), default)
        value = ' demo.example , localhost , *.demo.example '
        self.assertEqual(
            api_hosts_from_environment({'PREFLIGHT_ALLOWED_HOSTS': value}, 'PREFLIGHT_ALLOWED_HOSTS', default),
            ['demo.example', 'localhost', '*.demo.example'],
        )
        # TrustedHost compares names without ports and rejects port wildcards, so these must fail configuration.
        for value in ('', ' , ', 'a b', 'http://demo.example', 'demo.example/path', 'localhost:8000', 'localhost:*'):
            with self.subTest(value=value), self.assertRaisesRegex(SettingsError, 'PREFLIGHT_ALLOWED_HOSTS'):
                api_hosts_from_environment({'PREFLIGHT_ALLOWED_HOSTS': value}, 'PREFLIGHT_ALLOWED_HOSTS', default)

    def test_mcp_hosts_follow_transport_security_rules(self):
        default = ['localhost:*', '127.0.0.1:*']
        self.assertEqual(mcp_hosts_from_environment({}, 'MCP_ALLOWED_HOSTS', default), default)
        value = 'preflight-oss-mcp:*,127.0.0.1:8001,mcp.demo.example'
        self.assertEqual(
            mcp_hosts_from_environment({'MCP_ALLOWED_HOSTS': value}, 'MCP_ALLOWED_HOSTS', default),
            ['preflight-oss-mcp:*', '127.0.0.1:8001', 'mcp.demo.example'],
        )
        # MCP transport security has no domain wildcard, so a "*." entry must fail configuration.
        for value in ('', '*.demo.example:*', 'http://mcp.demo.example', 'mcp:abc'):
            with self.subTest(value=value), self.assertRaisesRegex(SettingsError, 'MCP_ALLOWED_HOSTS'):
                mcp_hosts_from_environment({'MCP_ALLOWED_HOSTS': value}, 'MCP_ALLOWED_HOSTS', default)


if __name__ == '__main__':
    unittest.main()
