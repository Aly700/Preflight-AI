"""Configuration fails closed with a named reason and never echoes a secret value."""

from pathlib import Path
import tempfile
import unittest

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

    def test_complete_configuration_loads_and_hides_secrets(self):
        settings = ChatSettings.from_environment(self.environ)
        self.assertEqual(settings.access_token, 'a' * 64)
        self.assertEqual(settings.copilot_token, 'b' * 64)
        self.assertEqual(settings.mcp_token, 'c' * 64)
        self.assertEqual(settings.mcp_url, 'http://127.0.0.1:8001/mcp')
        self.assertIsNone(settings.model)
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
            ChatSettings.from_environment({**environ, 'MODEL_PROVIDER': 'bedrock'})

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
