"""Eight read-only MCP tools, authenticated HTTP, and database-backed health."""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from functools import partial
import os
from typing import Annotated, Any

import anyio
from mcp import MCPError
from mcp.server import MCPServer
from mcp.server.context import ServerRequestContext
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
import psycopg
from pydantic import AwareDatetime, BaseModel, BeforeValidator
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp_service.auth import BearerAuthentication, load_token
from mcp_service.repository import Repository
from preflight_core.database import open_database
from preflight_core.models import (
    AircraftComparison,
    AircraftId,
    AircraftSummary,
    DocumentKind,
    DocumentQuery,
    DocumentSearchResult,
    DocumentTopK,
    Flag,
    GroupBy,
    IssueCounts,
    IssueHistory,
    IssueId,
    IssuePage,
    IssueStatus,
    PageLimit,
    PageOffset,
    PersonId,
    Priority,
    RecordId,
    Subsystem,
    TaskKind,
    TaskPage,
    TaskPageLimit,
    TaskStatus,
    TeamId,
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


def blank_filter(value: Any) -> Any:
    """Optional task filters treat blank strings like omitted values, before typed validation."""
    return None if isinstance(value, str) and not value.strip() else value


type OptionalTaskFilter[T] = Annotated[T | None, BeforeValidator(blank_filter)]


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

    @server.tool(annotations=READ_ONLY)
    async def count_issues(
        group_by: GroupBy,
        aircraft_id: AircraftId | None = None,
        status: IssueStatus | None = None,
        subsystem: Subsystem | None = None,
        priority: Priority | None = None,
        unresolved_only: Flag = False,
        opened_after: AwareDatetime | None = None,
        opened_before: AwareDatetime | None = None,
    ) -> IssueCounts:
        """Count fictional issues grouped by subsystem, priority, status or aircraft, with optional filters.

        `opened_after` and `opened_before` are inclusive timestamps with a timezone, as in search_issues.
        SQL does the counting. `unresolved_only` excludes closed issues. Every value of the grouped
        field that the filters allow is listed, with count 0 when no issue matches, so a zero can be
        cited as groups[key=blocked].count. `total` is the sum of the groups. This tool cannot change any records.
        """
        return IssueCounts.model_validate(
            await read(
                'issue_counts',
                group_by,
                aircraft_id,
                status,
                subsystem,
                priority,
                unresolved_only,
                opened_after,
                opened_before,
            )
        )

    @server.tool(annotations=READ_ONLY)
    async def compare_aircraft(aircraft_ids: list[AircraftId]) -> AircraftComparison:
        """Summaries for two to six fictional aircraft in one call, ranked by unresolved issues.

        `summaries` follow the request order; `ranked_by_unresolved` is descending with ties by ID;
        `unresolved_spread` is the difference between the highest and lowest unresolved counts.
        """
        distinct = list(dict.fromkeys(aircraft_ids))
        if not 2 <= len(distinct) <= 6:
            raise ToolError('Give two to six distinct aircraft IDs')
        return AircraftComparison.model_validate(await read('compare_aircraft', distinct))

    @server.tool(annotations=READ_ONLY)
    async def search_issues(
        status: IssueStatus | None = None,
        subsystem: Subsystem | None = None,
        priority: Priority | None = None,
        aircraft_id: AircraftId | None = None,
        opened_after: AwareDatetime | None = None,
        opened_before: AwareDatetime | None = None,
        limit: PageLimit = 50,
        offset: PageOffset = 0,
        unresolved_only: Flag = False,
    ) -> IssuePage:
        """Search fictional issues of any status with optional filters; paginated, ordered by issue ID.

        Unlike list_open_issues this includes closed issues unless a status filter says otherwise.
        `opened_after` and `opened_before` are inclusive timestamps with a timezone. The total counts
        all matches. `unresolved_only` excludes closed issues. This tool cannot change any records.
        """
        return IssuePage.model_validate(
            await read(
                'search_issues',
                status,
                subsystem,
                priority,
                aircraft_id,
                opened_after,
                opened_before,
                limit,
                offset,
                unresolved_only,
            )
        )

    @server.tool(annotations=READ_ONLY)
    async def search_documents(
        query: DocumentQuery,
        top_k: DocumentTopK = 5,
        kind: DocumentKind | None = None,
        related_id: RecordId | None = None,
    ) -> DocumentSearchResult:
        """Search fictional passages for policies, procedures of the coordination process,
        decisions, handovers, meeting outcomes and definitions.

        Use the record tools for counts, statuses and current values. Hybrid keyword/vector
        retrieval returns passage IDs to cite, with optional kind and related record filters.
        This tool cannot change any records.
        """
        return DocumentSearchResult.model_validate(await read('search_documents', query, top_k, kind, related_id))

    @server.tool(annotations=READ_ONLY)
    async def list_tasks(
        status: OptionalTaskFilter[TaskStatus] = None,
        task_kind: OptionalTaskFilter[TaskKind] = None,
        assigned_team_id: OptionalTaskFilter[TeamId] = None,
        assigned_person_id: OptionalTaskFilter[PersonId] = None,
        aircraft_id: OptionalTaskFilter[AircraftId] = None,
        issue_id: OptionalTaskFilter[IssueId] = None,
        limit: TaskPageLimit = 20,
        offset: PageOffset = 0,
    ) -> TaskPage:
        """List fictional tasks joined to their issue for aircraft ID, with optional AND-combined filters.

        Blank filters are ignored. Pages are ordered by task ID; total counts all matches.
        For task counts by status on whole aircraft, get_aircraft_summary or compare_aircraft gives
        every status in one call; use this tool to list tasks or to count with a kind, team, person
        or issue filter. Completed tasks do not imply closed issues. This tool cannot change records.
        """
        return TaskPage.model_validate(
            await read(
                'list_tasks',
                status,
                task_kind,
                assigned_team_id,
                assigned_person_id,
                aircraft_id,
                issue_id,
                limit,
                offset,
            )
        )

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
