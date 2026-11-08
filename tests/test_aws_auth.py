"""SigV4 signing with fixed fake credentials and no AWS credential-chain access."""

import asyncio
from datetime import UTC, datetime
import threading
import unittest
from unittest.mock import Mock, patch

from botocore.credentials import ReadOnlyCredentials  # type: ignore[import-untyped]
import httpx2

from backend.aws_auth import SigV4Auth

FAKE = ReadOnlyCredentials('AKIDEXAMPLE', 'fake-secret-for-tests', 'fake-session-token')
URL = 'https://gateway.example.test/mcp'


class SigV4Contract(unittest.TestCase):
    def test_sync_flow_signs_only_required_headers(self):
        auth = SigV4Auth('ca-central-1', credentials=lambda: FAKE)
        request = httpx2.Request(
            'POST',
            URL,
            content=b'{"method":"tools/list"}',
            headers={'content-type': 'application/json', 'mcp-session-id': 'session', 'accept': 'application/json'},
        )
        self.assertTrue(auth.requires_request_body)
        self.assertEqual(list(auth.sync_auth_flow(request)), [request])
        authorization = request.headers['Authorization']
        self.assertIn('AWS4-HMAC-SHA256', authorization)
        self.assertRegex(authorization, r'Credential=AKIDEXAMPLE/\d{8}/ca-central-1/bedrock-agentcore/aws4_request')
        self.assertIn('SignedHeaders=content-type;host;x-amz-date;x-amz-security-token', authorization)
        self.assertNotIn('mcp-session-id', authorization)
        self.assertEqual(request.headers['X-Amz-Security-Token'], FAKE.token)
        self.assertNotIn(FAKE.secret_key, str(request.headers))

    def test_sync_flow_buffers_and_signs_body_at_fixed_time(self):
        auth = SigV4Auth('ca-central-1', credentials=lambda: FAKE)
        requests = [httpx2.Request('POST', URL, content=iter([body])) for body in (b'first', b'second')]
        with patch('botocore.auth.get_current_datetime', return_value=datetime(2026, 1, 2, tzinfo=UTC)):
            for request in requests:
                list(auth.sync_auth_flow(request))
        self.assertEqual(requests[0].headers['X-Amz-Date'], '20260102T000000Z')
        self.assertEqual(requests[0].headers['X-Amz-Date'], requests[1].headers['X-Amz-Date'])
        self.assertNotEqual(requests[0].headers['Authorization'], requests[1].headers['Authorization'])

    def test_default_chain_is_lazy_and_freezes_credentials_per_request(self):
        provider = Mock()
        provider.get_frozen_credentials.side_effect = [FAKE, FAKE._replace(access_key='AKIDROTATED')]
        with patch('botocore.session.get_session') as session:
            session.return_value.get_credentials.return_value = provider
            auth = SigV4Auth('ca-central-1')
            session.assert_not_called()
            requests = [httpx2.Request('POST', URL), httpx2.Request('POST', URL)]
            for request in requests:
                list(auth.sync_auth_flow(request))
            session.assert_called_once_with()
            session.return_value.get_credentials.assert_called_once_with()
            self.assertEqual(provider.get_frozen_credentials.call_count, 2)
        self.assertIn('Credential=AKIDROTATED/', requests[1].headers['Authorization'])

    def test_no_credentials_has_a_fixed_safe_error(self):
        with patch('botocore.session.get_session') as session:
            session.return_value.get_credentials.return_value = None
            for default, auth in enumerate(
                (SigV4Auth('ca-central-1'), SigV4Auth('ca-central-1', credentials=lambda: None))
            ):
                with self.subTest(default=default == 0):
                    with self.assertRaisesRegex(RuntimeError, '^AWS credentials are unavailable$') as caught:
                        list(auth.sync_auth_flow(httpx2.Request('POST', URL)))
                    for value in (FAKE.access_key, FAKE.secret_key, FAKE.token, URL):
                        self.assertNotIn(value, str(caught.exception))

    def test_sign_removes_stale_session_token_for_long_lived_credentials(self):
        request = httpx2.Request('GET', URL, headers={'X-Amz-Security-Token': 'stale-token'})
        auth = SigV4Auth('ca-central-1', credentials=lambda: FAKE._replace(token=None))
        list(auth.sync_auth_flow(request))
        self.assertNotIn('X-Amz-Security-Token', request.headers)
        self.assertIn('SignedHeaders=host;x-amz-date,', request.headers['Authorization'])


class AsyncSigV4Contract(unittest.IsolatedAsyncioTestCase):
    async def test_async_flow_buffers_body_and_fetches_credentials_in_a_thread(self):
        threads = []

        def credentials():
            threads.append(threading.get_ident())
            return FAKE

        async def body():
            yield b'{"method":'
            yield b'"tools/list"}'

        auth = SigV4Auth('ca-central-1', credentials=credentials)
        request = httpx2.Request('POST', URL, content=body(), headers={'content-type': 'application/json'})
        with patch('asyncio.to_thread', wraps=asyncio.to_thread) as to_thread:
            signed = [item async for item in auth.async_auth_flow(request)]
            to_thread.assert_awaited_once_with(credentials)
        self.assertEqual(signed, [request])
        self.assertEqual(request.content, b'{"method":"tools/list"}')
        self.assertNotEqual(threads, [threading.get_ident()])
        self.assertIn('AWS4-HMAC-SHA256', request.headers['Authorization'])
        self.assertNotIn(FAKE.secret_key, str(request.headers))


if __name__ == '__main__':
    unittest.main()
