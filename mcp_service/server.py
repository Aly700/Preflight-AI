"""Three read-only MCP tools, authenticated HTTP, and database-backed health."""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from functools import partial
import os
from typing import Any

import anyio
from mcp import MCPError
from mcp.server import MCPServer
from mcp.server.context import ServerRequestContext
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
import psycopg
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp_service.auth import BearerAuthentication, load_token
from mcp_service.repository import Repository
from preflight_core.database import open_database
from preflight_core.models import (
    AircraftId,
    AircraftSummary,
    IssueHistory,
    IssueId,
    IssuePage,
    PageLimit,
    PageOffset,
    Priority,
)
from preflight_core.settings import mcp_hosts_from_environment
from preflight_core.tools import TOOLS

DEFAULT_ALLOWED_HOSTS = ['localhost:*', '127.0.0.1:*']
APPLICATION_NAME = 'preflight-oss-mcp'
RepositoryFactory = Callable[[], AbstractContextManager[Any]]
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


@contextmanager
def open_repository() -> Iterator[Repository]:
    with open_database(application_name=APPLICATION_NAME) as connection:
        # A tool can issue several SELECTs; all must see the same database snapshot.
        connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        yield Repository(connection)


Context = ServerRequestContext[Any, Any]
MiddlewareResult = BaseModel | dict[str, Any] | None


async def strict_arguments(
    ctx: Context, call_next: Callable[[Context], Awaitable[MiddlewareResult]]
) -> MiddlewareResult:
    """Reject unknown tools and unexpected argument names before any handler runs."""
    if ctx.method == 'tools/call':
        params = ctx.params or {}
        name = params.get('name')
        contract = TOOLS.get(name) if isinstance(name, str) else None
        arguments = params.get('arguments') or {}
        if contract is None or not isinstance(arguments, dict) or set(arguments) - contract.arguments:
            raise MCPError(code=-32602, message='Unknown tool or unexpected arguments')
    return await call_next(ctx)


def query(repository_factory: RepositoryFactory, method: str, *arguments: Any) -> Any:
    try:
        with repository_factory() as repository:
            return getattr(repository, method)(*arguments)
    except psycopg.Error, OSError, KeyError, ValueError:
        raise ToolError('Database unavailable') from None
    except LookupError:
        raise ToolError('Record not found') from None


def create_server(repository_factory: RepositoryFactory = open_repository) -> MCPServer:
    server = MCPServer(
        'Preflight AI',
        version='0.2.0',
        log_level='WARNING',
        instructions='Read-only fictional demonstration records. Cite returned source IDs. '
        'Completed tasks do not imply closed issues. No maintenance or airworthiness judgments.',
        middleware=[strict_arguments],
    )
    limiter = anyio.CapacityLimiter(4)

    async def read(method: str, *arguments: Any) -> Any:
        return await anyio.to_thread.run_sync(partial(query, repository_factory, method, *arguments), limiter=limiter)

    @server.tool(annotations=READ_ONLY)
    async def get_aircraft_summary(aircraft_id: AircraftId) -> AircraftSummary:
        """Get a fictional aircraft and separate issue/task status counts by source aircraft ID."""
        return AircraftSummary.model_validate(await read('aircraft_summary', aircraft_id))

    @server.tool(annotations=READ_ONLY)
    async def list_open_issues(
        aircraft_id: AircraftId | None = None,
        priority: Priority | None = None,
        limit: PageLimit = 50,
        offset: PageOffset = 0,
    ) -> IssuePage:
        """List non-closed fictional issues: open, in_progress, blocked and reopened.

        Optional aircraft/priority filters; total counts all matches. Pages are ordered
        by issue ID. Source IDs are retained. This tool cannot change any records.
        """
        return IssuePage.model_validate(await read('open_issues', aircraft_id, priority, limit, offset))

    @server.tool(annotations=READ_ONLY)
    async def get_issue_history(issue_id: IssueId, limit: PageLimit = 100, offset: PageOffset = 0) -> IssueHistory:
        """Get a fictional issue and a chronological page of events, including prior closure/reopening.

        Events retain source issue/task/person IDs. Ordering is by timestamp then event ID.
        The event total counts all matching events, not just the current page.
        """
        return IssueHistory.model_validate(await read('issue_history', issue_id, limit, offset))

    @server.custom_route('/health', methods=['GET'])
    async def health(request: Request) -> JSONResponse:
        try:
            if await read('healthy'):
                return JSONResponse({'status': 'ok', 'database': 'ok'})
        except ToolError:
            pass
        return JSONResponse({'error': 'Database unavailable'}, status_code=503)

    return server


def create_http_app(
    repository_factory: RepositoryFactory = open_repository,
    *,
    token: str | None = None,
    allowed_hosts: list[str] | None = None,
) -> Starlette:
    """Build the HTTP app. `allowed_hosts` defaults to MCP_ALLOWED_HOSTS, then to loopback names only."""
    token = load_token() if token is None else token
    if allowed_hosts is None:
        allowed_hosts = mcp_hosts_from_environment(os.environ, 'MCP_ALLOWED_HOSTS', DEFAULT_ALLOWED_HOSTS)
    server = create_server(repository_factory)
    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        max_request_body_size=65536,
        transport_security=TransportSecuritySettings(
            allowed_hosts=list(allowed_hosts),
            allowed_origins=[],
        ),
    )
    app.add_middleware(BearerAuthentication, token=token)
    return app
