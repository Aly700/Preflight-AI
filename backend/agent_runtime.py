"""The native agent loop: Claude with native tool use over the MCP service, verified claims.

One answer is one loop: discover the tools, let the model take up to
`max_tool_rounds` turns in which it writes prose and calls tools, run each
turn's tool calls concurrently (bounded), validate every result and feed it
back, then request a claims round when the answer includes record IDs or bare
integers so they can be checked against what the tools returned.
The events emitted are the same the UI already reads from the Copilot runtime.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import aclosing
import json
import logging
from typing import Any

from opentelemetry import trace

from backend.mcp_tools import ToolResult, ToolService, anthropic_tools, build_tool_service
from backend.model_client import USAGE_KEYS, ModelClient, ModelError, ToolCall, Turn, build_model_client
from backend.prompts import CLAIMS_INSTRUCTIONS, NATIVE_SYSTEM
from backend.scope import ScopeConflict, apply_scope, result_within_scope
from backend.settings import ChatSettings
from backend.telemetry import get_tracer
from backend.verify import FINAL_ANSWER_TOOL, ID_PATTERN, record_ids, scan_prose, verify_claims
from preflight_core.scope import scope_note, validate_scope
from preflight_core.tools import TOOLS

logger = logging.getLogger('preflight.native')

ANSWER_CHARS = 32000
PARALLEL_CALLS = 4
FINAL = str(FINAL_ANSWER_TOOL['name'])
LIMIT_REACHED = 'Tool call limit reached'
NO_MORE_TOOLS = 'No more tool calls are available; answer from the results you already have.'
TOOL_UNAVAILABLE = 'Tool unavailable'
SYSTEM = NATIVE_SYSTEM

Event = dict[str, Any]


class AnswerLimitExceeded(RuntimeError):
    """The streamed answer passed ANSWER_CHARS; the loop stops rather than stream on."""


def assistant_blocks(turn: Turn) -> list[dict[str, Any]]:
    """The assistant turn to send back: the model's own blocks when kept, else text and tool calls."""
    if turn.content:
        return list(turn.content)
    blocks: list[dict[str, Any]] = []
    if turn.text:
        blocks.append({'type': 'text', 'text': turn.text})
    for call in turn.tool_calls:
        blocks.append({'type': 'tool_use', 'id': call.id, 'name': call.name, 'input': call.input})
    if not blocks:
        blocks.append({'type': 'text', 'text': '(no content)'})
    return blocks


def result_block(call_id: str, content: str, *, error: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {'type': 'tool_result', 'tool_use_id': call_id, 'content': content}
    if error:
        block['is_error'] = True
    return block


def claims_prompt(calls: list[tuple[str, str, dict[str, Any]]]) -> str:
    """The declaration request, with short ids numbered in successful call log order."""
    lines = [CLAIMS_INSTRUCTIONS]
    if calls:
        lines.append('Tool calls in this conversation and their short ids; use these short ids as call_id:')
        lines += [
            f'- c{i}: {name}({json.dumps(arguments, ensure_ascii=True)})'
            for i, (_, name, arguments) in enumerate(calls, 1)
        ]
    else:
        lines.append(
            'No tool call succeeded, so declare no numbers and no record IDs unless they came from the question.'
        )
    return '\n'.join(lines)


class Loop:
    def __init__(
        self,
        settings: ChatSettings,
        model: ModelClient,
        tools: ToolService,
        emit: Callable[[Event], None] = lambda _: None,
        *,
        scope: dict[str, str] | None = None,
    ) -> None:
        self.scope = validate_scope({} if scope is None else scope)
        self.settings = settings
        self.model = model
        self.tools = tools
        self.emit = emit
        self.rounds = 0
        self.calls_made = 0
        self.calls_refused = 0
        self.model_calls = 0
        self.usage = dict.fromkeys(USAGE_KEYS, 0)
        self.text_parts: list[str] = []
        self.text_length = 0
        self.results: dict[str, dict[str, Any]] = {}
        self.names: dict[str, str] = {}
        self.calls_log: list[tuple[str, str, dict[str, Any]]] = []
        self.evidence: list[dict[str, Any]] = []
        self.seen: set[str] = set()
        self.claims: dict[str, Any] | None = None
        self.turn_has_text = False
        self.truncated = False
        self.question_ids: set[str] = set()
        self.tool_defs: list[dict[str, Any]] = []

    def on_text(self, text: str) -> None:
        if text and not self.turn_has_text and self.text_parts and not self.text_parts[-1].endswith('\n'):
            # A new turn's text would otherwise run straight on from the previous turn's last sentence.
            text = '\n\n' + text
        if text:
            self.turn_has_text = True
        self.text_length += len(text)
        if self.text_length > ANSWER_CHARS:
            raise AnswerLimitExceeded('Answer limit exceeded')
        self.text_parts.append(text)
        self.emit({'event': 'delta', 'data': {'text': text}})

    async def model_turn(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: dict[str, Any],
        on_text: Callable[[str], None],
    ) -> Turn:
        self.model_calls += 1
        turn = await self.model.stream_turn(
            system=SYSTEM, messages=messages, tools=tools, tool_choice=tool_choice, on_text=on_text
        )
        for key in self.usage:
            self.usage[key] += turn.usage.get(key, 0)
        return turn

    async def start(self, question: str) -> list[dict[str, Any]]:
        self.tool_defs = anthropic_tools(await self.tools.discover())
        # IDs the user supplied (question, browser history or selected scope) are context, not claims to verify.
        self.question_ids = set(ID_PATTERN.findall(question)) | set(ID_PATTERN.findall(' '.join(self.scope.values())))
        if self.scope:
            return [
                {
                    'role': 'user',
                    'content': [
                        {'type': 'text', 'text': scope_note(self.scope)},
                        {'type': 'text', 'text': question},
                    ],
                }
            ]
        return [{'role': 'user', 'content': question}]

    async def model_step(self, messages: list[dict[str, Any]]) -> Turn:
        self.turn_has_text = False
        turn = await self.model_turn(
            messages=messages, tools=self.tool_defs, tool_choice={'type': 'auto'}, on_text=self.on_text
        )
        self.truncated |= turn.stop_reason == 'max_tokens'
        self.rounds += 1
        messages.append({'role': 'assistant', 'content': assistant_blocks(turn)})
        return turn

    async def tool_step(self, messages: list[dict[str, Any]], turn: Turn) -> None:
        messages.append({'role': 'user', 'content': await self.execute(turn.tool_calls)})

    async def wrap_up_step(self, messages: list[dict[str, Any]]) -> None:
        # Wrap-up: the model answers from what it has; no further tools are offered.
        messages.append({'role': 'user', 'content': NO_MORE_TOOLS})
        try:
            self.turn_has_text = False
            turn = await self.model_turn(
                messages=messages,
                tools=self.tool_defs,
                tool_choice={'type': 'none'},
                on_text=self.on_text,
            )
        except ModelError:
            # Not every model accepts tool_choice none; the instruction alone then has to do.
            self.turn_has_text = False
            turn = await self.model_turn(
                messages=messages,
                tools=self.tool_defs,
                tool_choice={'type': 'auto'},
                on_text=self.on_text,
            )
        self.truncated |= turn.stop_reason == 'max_tokens'
        messages.append({'role': 'assistant', 'content': assistant_blocks(turn)})
        if turn.tool_calls:
            self.calls_refused += len(turn.tool_calls)
            refused = [result_block(c.id, LIMIT_REACHED, error=True) for c in turn.tool_calls]
            messages.append({'role': 'user', 'content': refused})

    def finish_step(self) -> bool:
        if self.truncated:
            self.emit({'event': 'delta', 'data': {'text': '\n\n(The answer was cut short at the length limit.)'}})
        prose_ids, integers = scan_prose(''.join(self.text_parts))
        return bool(prose_ids - self.question_ids or integers)

    async def claims_step(self, messages: list[dict[str, Any]]) -> None:
        # Claims round: its text is not part of the answer.
        messages.append({'role': 'user', 'content': claims_prompt(self.calls_log)})
        # Not every model accepts a forced tool choice, so the claims tool is simply the only tool offered.
        self.turn_has_text = False
        turn = await self.model_turn(
            messages=messages,
            tools=[FINAL_ANSWER_TOOL],
            tool_choice={'type': 'auto'},
            on_text=lambda _: None,
        )
        final_call = next((c for c in turn.tool_calls if c.name == FINAL), None)
        self.claims = dict(final_call.input) if final_call is not None else None

    async def run(self, question: str) -> None:
        messages = await self.start(question)
        while True:
            turn = await self.model_step(messages)
            if not turn.tool_calls:
                break
            await self.tool_step(messages, turn)
            if self.rounds >= self.settings.max_tool_rounds:
                await self.wrap_up_step(messages)
                break
        if self.finish_step():
            await self.claims_step(messages)

    async def execute(self, calls: list[ToolCall]) -> list[dict[str, Any]]:
        """Run one turn's tool calls concurrently (bounded) and return their result blocks in order."""
        blocks: dict[str, dict[str, Any]] = {}
        runnable: list[ToolCall] = []
        for call in calls:
            if call.name not in TOOLS:
                self.calls_refused += 1
                self.calls_made += 1
                if call.name != FINAL:
                    self.emit({'event': 'tool', 'data': {'name': call.name, 'status': 'failed', 'call_id': call.id}})
                blocks[call.id] = result_block(call.id, TOOL_UNAVAILABLE, error=True)
            elif self.calls_made >= self.settings.max_tool_calls:
                self.calls_refused += 1
                self.emit({'event': 'tool', 'data': {'name': call.name, 'status': 'failed', 'call_id': call.id}})
                blocks[call.id] = result_block(call.id, LIMIT_REACHED, error=True)
            else:
                self.calls_made += 1
                schema: dict[str, Any] = next(
                    (tool['input_schema'] for tool in self.tool_defs if tool['name'] == call.name), {}
                )
                try:
                    arguments = apply_scope(call.name, call.input, self.scope, schema)
                except ScopeConflict as error:
                    self.calls_refused += 1
                    self.emit(
                        {
                            'event': 'tool',
                            'data': {'name': call.name, 'status': 'refused', 'reason': 'scope', 'call_id': call.id},
                        }
                    )
                    blocks[call.id] = result_block(call.id, str(error), error=True)
                    continue
                runnable.append(ToolCall(call.id, call.name, arguments))
                self.emit({'event': 'tool', 'data': {'name': call.name, 'status': 'running', 'call_id': call.id}})
        scope_refusals: set[str] = set()
        semaphore = asyncio.Semaphore(PARALLEL_CALLS)

        async def bounded(call: ToolCall) -> ToolResult:
            async with semaphore:
                with get_tracer().start_as_current_span(
                    f'execute_tool {call.name}',
                    kind=trace.SpanKind.INTERNAL,
                    attributes={
                        'gen_ai.operation.name': 'execute_tool',
                        'gen_ai.tool.name': call.name,
                        'gen_ai.tool.call.id': call.id,
                    },
                    record_exception=False,
                    set_status_on_exception=False,
                ) as span:
                    try:
                        result = await self.tools.call(call.id, call.name, dict(call.input))
                    except BaseException as error:
                        span.set_attribute('preflight.tool.outcome', 'failed')
                        span.set_attribute('error.type', type(error).__name__)
                        span.set_status(trace.StatusCode.ERROR)
                        raise
                    complete = result.ok and result.payload is not None
                    if complete and not result_within_scope(result.payload, self.scope):
                        scope_refusals.add(call.id)
                        self.calls_refused += 1
                        result = ToolResult(call.name, call.id, False, error='The result is outside the selected scope')
                        complete = False
                    span.set_attribute('preflight.tool.outcome', 'complete' if complete else 'failed')
                    if not complete:
                        span.set_attribute('error.type', 'ToolError')
                        span.set_status(trace.StatusCode.ERROR)
                    return result

        tasks = [asyncio.create_task(bounded(call)) for call in runnable]
        try:
            results = await asyncio.gather(*tasks)
        finally:
            # gather propagates the first failure without cancelling siblings. Join
            # them here so no tool execution (or span) can outlive its answer.
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        for call, result in zip(runnable, results, strict=True):
            if result.ok and result.payload is not None:
                self.results[call.id] = result.payload
                self.names[call.id] = call.name
                self.calls_log.append((call.id, call.name, dict(call.input)))
                self.evidence.append({'tool': call.name, 'call_id': call.id, 'result': result.payload})
                self.seen.update(record_ids(result.payload))
                self.emit({'event': 'tool', 'data': {'name': call.name, 'status': 'complete', 'call_id': call.id}})
                blocks[call.id] = result_block(call.id, json.dumps(result.payload, ensure_ascii=True))
            else:
                outcome = (
                    {'status': 'refused', 'reason': 'scope'} if call.id in scope_refusals else {'status': 'failed'}
                )
                self.emit({'event': 'tool', 'data': {'name': call.name, 'call_id': call.id, **outcome}})
                blocks[call.id] = result_block(call.id, result.error or TOOL_UNAVAILABLE, error=True)
        return [blocks[call.id] for call in calls]

    def final_events(self) -> list[Event]:
        verification = verify_claims(
            prose=''.join(self.text_parts),
            claims=self.claims,
            results=self.results,
            seen_ids=self.seen,
            names=self.names,
            aliases={f'c{i}': call_id for i, (call_id, _, _) in enumerate(self.calls_log, 1)},
            question_ids=self.question_ids,
        )
        events: list[Event] = []
        if verification.note:
            events.append({'event': 'delta', 'data': {'text': '\n\n' + verification.note}})
        events.append(
            {
                'event': 'sources',
                'data': {'records': sorted(self.seen), 'tools': sorted({e['tool'] for e in self.evidence})},
            }
        )
        events.append({'event': 'evidence', 'data': {'results': self.evidence}})
        events.append(
            {
                'event': 'done',
                'data': {
                    'scope': dict(self.scope),
                    'tool_calls': len(self.evidence),
                    'rounds': self.rounds,
                    'usage': dict(self.usage),
                    'model_calls': self.model_calls,
                    'verified': verification.verified,
                    'truncated': self.truncated,
                    'unverified': len(verification.unverified),
                    'undeclared': verification.undeclared,
                },
            }
        )
        return events


async def stream_answer(
    question: str,
    settings: ChatSettings,
    *,
    scope: dict[str, str] | None = None,
    model_client: ModelClient | None = None,
    tools: ToolService | None = None,
) -> AsyncGenerator[Event]:
    """Yield delta and tool events while the loop runs, then sources, evidence and done.

    The loop runs in its own task so a consumer that goes away cancels every
    in-flight tool call; failures of the model or the transport propagate to the
    caller, which turns them into the fixed error frame.
    """
    model = model_client if model_client is not None else build_model_client(settings)
    service = tools if tools is not None else build_tool_service(settings)
    loop = Loop(settings, model, service, scope=scope)
    async with aclosing(
        stream_loop(loop, loop.run(question), owned_model=model if model_client is None else None)
    ) as stream:
        async for event in stream:
            yield event


async def stream_loop(
    loop: Loop, drive: Coroutine[Any, Any, None], *, owned_model: ModelClient | None
) -> AsyncGenerator[Event]:
    """Stream either driver with identical error propagation, cancellation and client ownership."""
    queue: asyncio.Queue[Event | None] = asyncio.Queue()
    loop.emit = queue.put_nowait
    span = get_tracer().start_span(
        'invoke_agent preflight',
        kind=trace.SpanKind.INTERNAL,
        attributes={
            'gen_ai.operation.name': 'invoke_agent',
            'gen_ai.agent.name': 'preflight',
            'gen_ai.provider.name': 'aws.bedrock' if loop.settings.provider == 'bedrock' else 'anthropic',
            'gen_ai.request.model': loop.model.model,
            'preflight.runtime': loop.settings.runtime,
            'preflight.scope_fields': sorted(loop.scope),
        },
    )

    async def run() -> None:
        try:
            # Context lives in the driver task, never across a yield to the consumer.
            # asyncio children and LangGraph nodes inherit it, including SDK model spans.
            with trace.use_span(span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
                await drive
        finally:
            queue.put_nowait(None)

    task = asyncio.create_task(run())
    try:
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
            await task  # propagate model, transport and limit failures; never turn them into done
            events = loop.final_events()
            done = events[-1]['data']
            usage = done['usage']
            # As in the SDK's model spans, input tokens include the cached ones.
            span.set_attribute(
                'gen_ai.usage.input_tokens',
                usage['input_tokens'] + usage['cache_read_input_tokens'] + usage['cache_creation_input_tokens'],
            )
            span.set_attribute('gen_ai.usage.output_tokens', usage['output_tokens'])
            span.set_attribute('gen_ai.usage.cache_read.input_tokens', usage['cache_read_input_tokens'])
            span.set_attribute('gen_ai.usage.cache_write.input_tokens', usage['cache_creation_input_tokens'])
            for key in ('tool_calls', 'rounds', 'model_calls', 'unverified', 'truncated'):
                span.set_attribute(f'preflight.{key}', done[key])
            if isinstance(done['verified'], bool):
                span.set_attribute('preflight.verified', done['verified'])
            for event in events:
                yield event
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if owned_model is not None:
                await owned_model.aclose()  # an injected client belongs to its caller
    except BaseException as error:
        span.set_attribute('error.type', type(error).__name__)
        span.set_status(trace.StatusCode.ERROR)
        raise
    finally:
        span.set_attribute('preflight.tool_calls_refused', loop.calls_refused)
        span.end()
