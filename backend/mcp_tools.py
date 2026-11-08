"""Tool discovery and calls over streamable HTTP with bearer or SigV4 authentication.

Only registry tools are discovered or called; arguments are checked against the
registry before any request; every result is validated against the registry's
result model. Failures of any kind become a fixed error for the model and the
user, never upstream text.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
import json
import logging
from typing import Any, Protocol

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from backend.aws_auth import SigV4Auth
from backend.settings import ChatSettings, SettingsError
from preflight_core.tools import TOOLS

logger = logging.getLogger('preflight.tools')

TOOL_UNAVAILABLE = 'Tool unavailable'
# The MCP service's own fixed messages; anything else is reported as unavailable.
SERVICE_ERRORS = frozenset({'Record not found', 'Database unavailable'})
DEFAULT_TIMEOUT = 10.0
ClientFactory = Callable[[str, dict[str, str]], AbstractAsyncContextManager[Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    name: str
    call_id: str
    ok: bool
    payload: dict[str, Any] | None = None
    error: str | None = None


class ToolService(Protocol):
    """What the loop needs from a tool service; McpTools and test fakes both satisfy it."""

    async def discover(self) -> list[ToolSpec]: ...

    async def call(self, call_id: str, name: str, arguments: dict[str, Any]) -> ToolResult: ...


def default_client_factory(timeout: float, auth: httpx2.Auth | None = None) -> ClientFactory:
    @asynccontextmanager
    async def factory(url: str, headers: dict[str, str]) -> AsyncIterator[Any]:
        # The HTTP client is ours to close: the transport only closes clients it created itself.
        async with httpx2.AsyncClient(headers=headers, timeout=timeout, auth=auth) as http:  # noqa: SIM117
            async with Client(streamable_http_client(url, http_client=http)) as client:
                yield client

    return factory


def anthropic_tools(specs: list[ToolSpec]) -> list[dict[str, Any]]:
    """The discovered tool definitions for answer turns through the Messages API."""
    return [{'name': spec.name, 'description': spec.description, 'input_schema': spec.input_schema} for spec in specs]


def leaf_exception_types(error: BaseException) -> set[str]:
    """Only exception type names are safe to log; transport messages may contain credentials."""
    if isinstance(error, BaseExceptionGroup):
        return {name for child in error.exceptions for name in leaf_exception_types(child)}
    return {type(error).__name__}


class McpTools:
    def __init__(
        self,
        url: str,
        token: str | None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        client_factory: ClientFactory | None = None,
        auth: httpx2.Auth | None = None,
        tool_prefix: str = '',
    ) -> None:
        self.url = url
        self.timeout = timeout
        self.tool_prefix = tool_prefix
        self._headers = {'Authorization': 'Bearer ' + token} if token is not None else {}
        self._factory = client_factory or default_client_factory(timeout, auth)

    def _client(self) -> AbstractAsyncContextManager[Any]:
        return self._factory(self.url, dict(self._headers))

    async def discover(self) -> list[ToolSpec]:
        """The registry tools the service currently offers, in registry order, with their schemas."""
        async with asyncio.timeout(self.timeout), self._client() as client:
            listing = await client.list_tools()
        offered: dict[str, ToolSpec] = {}
        for tool in listing.tools:
            if not tool.name.startswith(self.tool_prefix):
                continue
            name = tool.name.removeprefix(self.tool_prefix)
            contract = TOOLS.get(name)
            if contract is None:
                continue
            schema = dict(tool.input_schema or {})
            properties = set(schema.get('properties') or {})
            if not properties <= contract.arguments:
                logger.warning('MCP tool %s offers arguments outside its contract; dropped', tool.name)
                continue
            offered[name] = ToolSpec(name, tool.description or '', schema)
        return [offered[name] for name in TOOLS if name in offered]

    async def call(self, call_id: str, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Call one registry tool; any refusal or failure is a ToolResult with the fixed error."""
        contract = TOOLS.get(name)
        if contract is None or not isinstance(arguments, dict) or set(arguments) - contract.arguments:
            return ToolResult(name, call_id, False, error=TOOL_UNAVAILABLE)
        try:
            async with asyncio.timeout(self.timeout), self._client() as client:
                result = await client.call_tool(self.tool_prefix + name, arguments)
            if getattr(result, 'is_error', False):
                text = getattr(result.content[0], 'text', '') if result.content else ''
                known = next((message for message in SERVICE_ERRORS if text.endswith(message)), TOOL_UNAVAILABLE)
                return ToolResult(name, call_id, False, error=known)
            payload = result.structured_content
            if payload is None:
                payload = json.loads(result.content[0].text)
            validated = contract.result.model_validate(payload).model_dump(mode='json')
        except Exception as error:
            # Transport, authentication, timeout, tool and validation failures all look the same to the model.
            logger.warning('Tool %s failed: %s', name, ','.join(sorted(leaf_exception_types(error))))
            return ToolResult(name, call_id, False, error=TOOL_UNAVAILABLE)
        return ToolResult(name, call_id, True, payload=validated)


def build_tool_service(settings: ChatSettings) -> McpTools:
    """Use the configured authentication and namespace without exposing them to the model loop."""
    auth = None
    token = settings.mcp_token
    if settings.mcp_auth == 'sigv4':
        if settings.aws_region is None:
            raise SettingsError('AWS_REGION is not set')
        auth = SigV4Auth(settings.aws_region)
        token = None
    return McpTools(settings.mcp_url, token, auth=auth, tool_prefix=settings.mcp_tool_prefix)
