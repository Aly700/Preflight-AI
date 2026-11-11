"""Copilot SDK adapter: fresh sessions, three read-only MCP tools, real provenance.

Responsibilities, in order of appearance: the permission and tool policies the
runtime consults, validation of tool results into source IDs, the event tracker
that turns runtime events into public SSE payloads, the session options that
disable everything except the three MCP tools, and the orchestration that runs
one answer with bounded cleanup.
"""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
import json
import logging
import os
import tempfile
from typing import Any

from copilot import CopilotClient, RuntimeConnection
from copilot.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject

from backend.prompts import COPILOT_SYSTEM
from backend.settings import ChatSettings, SettingsError
from backend.verify import record_ids
from preflight_core.scope import scope_note, validate_scope
from preflight_core.tools import MCP_SERVER_NAME, QUALIFIED_NAMES, TOOLS

logger = logging.getLogger('preflight.copilot')

ANSWER_CHARS = 32000
MODEL_WAIT_SECONDS = 100
CLEANUP_SECONDS = 5.0
SYSTEM = COPILOT_SYSTEM

Event = dict[str, Any]
Payload = Mapping[str, Any]


def permission(request: Any, invocation: Any) -> PermissionDecisionApproveOnce | PermissionDecisionReject:
    """Approve exactly one read-only call to one of the three Preflight MCP tools; reject everything else."""
    if (
        getattr(request, 'kind', None) == 'mcp'
        and getattr(request, 'server_name', None) == MCP_SERVER_NAME
        and getattr(request, 'tool_name', None) in TOOLS
        and getattr(request, 'read_only', False)
        and not getattr(request, 'managed_approval_required', False)
    ):
        return PermissionDecisionApproveOnce()
    return PermissionDecisionReject(feedback='Only the three read-only Preflight MCP tools are allowed')


class ToolPolicy:
    """Pre-tool-use hook: only qualified Preflight tool names, at most `max_calls` per answer."""

    def __init__(self, max_calls: int = 8) -> None:
        self.max_calls = max_calls
        self.calls = 0

    def before(self, event: Mapping[str, Any], context: Any) -> dict[str, str]:
        if event['toolName'] not in QUALIFIED_NAMES or self.calls >= self.max_calls:
            return {'permissionDecision': 'deny', 'permissionDecisionReason': 'Tool unavailable or call limit reached'}
        self.calls += 1
        return {'permissionDecision': 'allow'}


def validate_result(name: str, payload: Any) -> dict[str, Any]:
    """Parse a tool result against its declared schema; ValueError (pydantic) on any mismatch."""
    return TOOLS[name].result.model_validate(payload).model_dump(mode='json')


class EventTracker:
    """Forward only public answer text and schema-validated MCP results."""

    def __init__(self, emit: Callable[[Event], None]) -> None:
        self.emit = emit
        self.started: dict[str, str] = {}
        self.results: list[dict[str, Any]] = []
        self.records: set[str] = set()
        self.text_length = 0
        self.failure: RuntimeError | None = None

    def on_event(self, event: Any) -> None:
        if getattr(event, 'agent_id', None):
            return
        kind = getattr(event.type, 'value', event.type)
        data = event.data
        try:
            if kind == 'assistant.message_delta':
                text = data.delta_content
                self.text_length += len(text)
                if self.text_length > ANSWER_CHARS:
                    raise ValueError('Answer limit exceeded')
                self.emit({'event': 'delta', 'data': {'text': text}})
            elif kind == 'tool.execution_start':
                server = getattr(data, 'mcp_server_name', None)
                name = getattr(data, 'mcp_tool_name', None)
                if server != MCP_SERVER_NAME or not isinstance(name, str) or name not in TOOLS:
                    raise ValueError('Unexpected tool execution')
                self.started[data.tool_call_id] = name
                self.emit({'event': 'tool', 'data': {'name': name, 'status': 'running', 'call_id': data.tool_call_id}})
            elif kind == 'tool.execution_complete':
                name = self.started.pop(data.tool_call_id, None)
                if not name or not data.success or data.result is None:
                    raise ValueError('Tool did not return a successful result')
                payload = data.result.structured_content
                if payload is None:
                    payload = json.loads(data.result.content)
                result = validate_result(name, payload)
                self.records.update(record_ids(result))
                self.results.append({'tool': name, 'result': result})
                self.emit({'event': 'tool', 'data': {'name': name, 'status': 'complete', 'call_id': data.tool_call_id}})
            elif kind == 'session.error':
                raise ValueError('Model session failed')
        except ValueError, KeyError, TypeError, AttributeError, asyncio.QueueFull:
            self.failure = RuntimeError('Invalid or failed model/tool event')


# The runtime requires a base URL for every bring-your-own-key provider; these are the public endpoints.
PROVIDER_BASE_URLS = {'anthropic': 'https://api.anthropic.com', 'openai': 'https://api.openai.com/v1'}


def provider_config(settings: ChatSettings) -> dict[str, Any]:
    """The bring-your-own-key provider for the runtime; the key travels in this option, never the environment."""
    config: dict[str, Any] = {
        'type': settings.provider,
        'base_url': PROVIDER_BASE_URLS[settings.provider],
        'api_key': settings.model_api_key,
        'model_id': settings.model,
        'wire_model': settings.model,
    }
    if settings.provider == 'openai':
        config['wire_api'] = 'responses'
    if settings.workspace_id:
        config['headers'] = {'anthropic-workspace-id': settings.workspace_id}
    return config


def session_options(
    settings: ChatSettings, directory: str, tracker: EventTracker, policy: ToolPolicy
) -> dict[str, Any]:
    """Session options that leave the model nothing but the three MCP tools."""
    if settings.mcp_token is None:
        raise SettingsError('Copilot runtime requires an MCP bearer token')
    options: dict[str, Any] = {
        'on_permission_request': permission,
        'streaming': True,
        'available_tools': ['mcp:' + name for name in QUALIFIED_NAMES],
        'system_message': {'mode': 'replace', 'content': SYSTEM},
        'working_directory': directory,
        'config_directory': directory,
        'enable_config_discovery': False,
        'skip_custom_instructions': True,
        'enable_skills': False,
        'enable_file_hooks': False,
        'enable_host_git_operations': False,
        'enable_session_store': False,
        'enable_session_telemetry': False,
        'enable_file_change_tracking': False,
        'manage_schedule_enabled': False,
        'custom_agents': [],
        'plugin_directories': [],
        'skill_directories': [],
        'instruction_directories': [],
        'hook_directories': [],
        'agent_directories': [],
        'hooks': {'on_pre_tool_use': policy.before},
        'on_event': tracker.on_event,
        'mcp_servers': {
            MCP_SERVER_NAME: {
                'type': 'http',
                'url': settings.mcp_url,
                'headers': {'Authorization': 'Bearer ' + settings.mcp_token},
                'tools': list(TOOLS),
                'timeout': 10000,
            }
        },
    }
    if settings.model:
        options['model'] = settings.model
    if settings.provider != 'copilot':
        options['provider'] = provider_config(settings)
    return options


class Cleanup:
    """Bounded cleanup steps that record failures instead of swallowing them.

    Every step gets a deadline so a stalled runtime cannot hold the answer slot,
    and every recorded failure is raised after cleanup has finished so that a
    failed detach or stop never ends in a successful `done` frame.
    """

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.failures: list[str] = []

    async def step(self, awaitable: Awaitable[object], what: str, *, record: bool = True) -> bool:
        try:
            async with asyncio.timeout(self.seconds):
                await awaitable
            return True
        except Exception as error:
            logger.warning('Copilot runtime cleanup step "%s" did not finish cleanly: %s', what, type(error).__name__)
            if record:
                self.failures.append(what)
            return False


def exit_arguments(error: BaseException | None) -> tuple[type[BaseException] | None, BaseException | None, Any]:
    return (type(error), error, error.__traceback__) if error is not None else (None, None, None)


def child_environment(directory: str) -> dict[str, str]:
    """The runtime inherits no GitHub, AWS or database credentials; its home is a throwaway directory."""
    environment = {key: os.environ[key] for key in ('PATH', 'LANG', 'COPILOT_CLI_PATH') if key in os.environ}
    environment.update(HOME=directory, XDG_CONFIG_HOME=directory, XDG_CACHE_HOME=directory)
    return environment


ABANDONED: set[asyncio.Task[None]] = set()


def finish_abandoned(task: asyncio.Task[None]) -> None:
    """Retrieve an abandoned cleanup task's outcome so asyncio never reports an unretrieved exception."""
    ABANDONED.discard(task)
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.warning('Abandoned Copilot runtime cleanup finished with %s', type(error).__name__)


async def stop_runtime(client: Any, cleanup: Cleanup, error: BaseException | None) -> None:
    """Stop the runtime gracefully; on failure, timeout or cancellation kill the owned process."""
    try:
        stopped = await cleanup.step(client.__aexit__(*exit_arguments(error)), 'runtime stop')
    except asyncio.CancelledError:
        # The consumer went away during a graceful stop: still kill the owned runtime, then honour it.
        await cleanup.step(client.force_stop(), 'runtime force stop')
        raise
    if not stopped:
        # Graceful stop stalled or failed: kill the owned runtime process rather than leak it.
        await cleanup.step(client.force_stop(), 'runtime force stop')


async def stream_answer(
    question: str,
    settings: ChatSettings,
    *,
    scope: dict[str, str] | None = None,
    client_factory: Callable[..., Any] = CopilotClient,
    cleanup_seconds: float = CLEANUP_SECONDS,
) -> AsyncGenerator[Event]:
    """Yield delta/tool events while the model works, then sources, evidence and done.

    Any runtime, authentication, tool, validation or cleanup failure raises
    instead of completing: the caller turns that into an `error` frame. Cleanup
    of the session and runtime is bounded so a stalled runtime cannot hold the
    answer slot past the transport deadline.
    """
    scope = validate_scope({} if scope is None else scope)
    # Copilot owns tool execution: scope is prompt-only here, with no argument enforcement.
    prompt = scope_note(scope) + '\nCurrent question:\n' + question if scope else question
    queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=512)
    tracker = EventTracker(queue.put_nowait)

    async def answer(session: Any) -> None:
        response = await session.send_and_wait(prompt, timeout=MODEL_WAIT_SECONDS)
        if tracker.failure:
            raise tracker.failure
        if not tracker.results:
            raise RuntimeError('No successful MCP result; answer is unverified')
        if tracker.started:
            raise RuntimeError('Tool execution incomplete')
        if not tracker.text_length and response is not None:
            content = getattr(response.data, 'content', '')
            if content:
                tracker.on_event(
                    type(
                        'Event',
                        (),
                        {'type': 'assistant.message_delta', 'data': type('Data', (), {'delta_content': content})()},
                    )()
                )
        if tracker.failure or not tracker.text_length:
            raise RuntimeError('No usable model answer')

    async def run() -> None:
        cleanup = Cleanup(cleanup_seconds)
        with tempfile.TemporaryDirectory(prefix='preflight-chat-') as directory:
            client = client_factory(
                connection=RuntimeConnection.for_stdio(),
                mode='empty',
                github_token=settings.copilot_token,
                use_logged_in_user=False,
                base_directory=directory,
                working_directory=directory,
                log_level='error',
                env=child_environment(directory),
            )
            try:
                await client.__aenter__()
            except BaseException:
                # Start-up failed or was cancelled: kill any process the SDK already spawned, then report it.
                await cleanup.step(client.force_stop(), 'runtime force stop')
                raise
            error: BaseException | None = None
            try:
                # A bring-your-own-key provider bypasses Copilot API authentication by SDK contract.
                if settings.provider == 'copilot':
                    authentication = await client.get_auth_status()
                    if not authentication.isAuthenticated:
                        raise RuntimeError('Copilot authentication required')
                session = await client.create_session(**session_options(settings, directory, tracker, ToolPolicy()))
                await session.__aenter__()
                try:
                    await answer(session)
                except BaseException as caught:
                    error = caught
                    raise
                finally:
                    # Abort failures were always tolerated; detach and stop failures never were.
                    await cleanup.step(session.abort(), 'session abort', record=False)
                    await cleanup.step(session.__aexit__(*exit_arguments(error)), 'session detach')
            except BaseException as caught:
                error = caught
                raise
            finally:
                await stop_runtime(client, cleanup, error)
        if cleanup.failures:
            raise RuntimeError('Copilot runtime cleanup failed: ' + ', '.join(cleanup.failures))

    task = asyncio.create_task(run())
    propagated = False  # becomes True once the consumer itself awaits the task's outcome
    try:
        while not task.done() or not queue.empty():
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.25)
            except TimeoutError:
                if tracker.failure:
                    raise tracker.failure from None
                continue
            if tracker.failure:
                # Stop forwarding as soon as the runtime reports a failure; drained text is not an answer.
                raise tracker.failure from None
            yield event
        propagated = True
        await task  # Propagate runtime/auth/model/cleanup failures; never turn them into success.
        if tracker.failure:
            # A failure recorded during abort or detach must not become a completed answer.
            raise tracker.failure from None
        yield {
            'event': 'sources',
            'data': {'records': sorted(tracker.records), 'tools': sorted({r['tool'] for r in tracker.results})},
        }
        # These are actual MCP results, allowing independent SQL verification.
        yield {'event': 'evidence', 'data': {'results': tracker.results}}
        yield {'event': 'done', 'data': {'tool_calls': len(tracker.results), 'scope': scope}}
    finally:
        task.cancel()
        done, pending = await asyncio.wait({task}, timeout=cleanup_seconds * 3)
        for finished in done:
            # Retrieve the outcome so asyncio never reports it as unretrieved; the consumer's own outcome is unchanged.
            error = None if finished.cancelled() else finished.exception()
            if error is not None and not propagated:
                logger.warning(
                    'Copilot runtime task ended with %s after the answer was interrupted', type(error).__name__
                )
        if pending:
            # Keep a reference so the stalled cleanup can still finish; the answer slot is already released.
            ABANDONED.add(task)
            task.add_done_callback(finish_abandoned)
            logger.warning('Copilot runtime cleanup exceeded %.0fs and was abandoned', cleanup_seconds * 3)
