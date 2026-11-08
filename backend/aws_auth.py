"""HTTP request signing for IAM-authenticated MCP endpoints."""

import asyncio
from collections.abc import AsyncGenerator, Callable, Generator
from threading import Lock
from typing import Any

import botocore.auth  # type: ignore[import-untyped]
from botocore.awsrequest import AWSRequest  # type: ignore[import-untyped]
import botocore.session  # type: ignore[import-untyped]
import httpx2


class SigV4Auth(httpx2.Auth):
    requires_request_body = True

    def __init__(
        self, region: str, *, service: str = 'bedrock-agentcore', credentials: Callable[[], Any] | None = None
    ) -> None:
        self.region = region
        self.service = service
        self._credentials = credentials if credentials is not None else self._frozen_credentials
        self._credential_lock = Lock()
        self._session: Any = None
        self._provider: Any = None

    def _frozen_credentials(self) -> Any:
        # One lazy chain, including on concurrent first requests; refresh its frozen snapshot each time.
        with self._credential_lock:
            if self._session is None:
                self._session = botocore.session.get_session()
                self._provider = self._session.get_credentials()
            return self._provider.get_frozen_credentials() if self._provider is not None else None

    def sign(self, request: httpx2.Request, credentials: Any) -> None:
        if credentials is None:
            raise RuntimeError('AWS credentials are unavailable')
        headers = {name: request.headers[name] for name in ('host', 'content-type') if name in request.headers}
        signed = AWSRequest(method=request.method, url=str(request.url), data=request.content, headers=headers)
        botocore.auth.SigV4Auth(credentials, self.service, self.region).add_auth(signed)
        for name in ('Authorization', 'X-Amz-Date', 'X-Amz-Security-Token'):
            if name in signed.headers:
                request.headers[name] = signed.headers[name]
            else:
                request.headers.pop(name, None)

    def sync_auth_flow(self, request: httpx2.Request) -> Generator[httpx2.Request, httpx2.Response]:
        request.read()
        self.sign(request, self._credentials())
        yield request

    async def async_auth_flow(self, request: httpx2.Request) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        await request.aread()
        credentials = await asyncio.to_thread(self._credentials)
        self.sign(request, credentials)
        yield request
