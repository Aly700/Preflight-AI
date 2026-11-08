"""The native agent steps orchestrated by one compiled, stateless LangGraph."""

from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Any, Literal, TypedDict, cast
import warnings

from langchain_core.runnables import RunnableConfig

from backend.agent_runtime import Event, Loop, stream_loop
from backend.mcp_tools import ToolService, build_tool_service
from backend.model_client import ModelClient, ToolCall, Turn, build_model_client
from backend.settings import ChatSettings

# The pinned transitive LangSmith dependency uses this deprecated predicate in
# import-time decorators on Python 3.14. Limit the compatibility filter to that
# exact warning and module during import; application warnings still fail normally.
with warnings.catch_warnings():
    warnings.filterwarnings(
        'ignore',
        message=r"'asyncio\.iscoroutinefunction' is deprecated.*",
        category=DeprecationWarning,
        module=r'langsmith\._internal\._beta_decorator',
    )
    from langgraph.graph import END, START, StateGraph
    from langsmith import tracing_context


class State(TypedDict, total=False):
    messages: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    claims_needed: bool


def request_loop(config: RunnableConfig) -> Loop:
    return cast(Loop, config['configurable']['loop'])


async def start(state: State, config: RunnableConfig) -> State:
    messages = await request_loop(config).start(state['messages'][0]['content'])
    return {'messages': messages}


async def model(state: State, config: RunnableConfig) -> State:
    messages = state['messages']
    turn = await request_loop(config).model_step(messages)
    return {
        'messages': messages,
        'tool_calls': [{'id': call.id, 'name': call.name, 'input': call.input} for call in turn.tool_calls],
    }


def after_model(state: State) -> Literal['tools', 'finish']:
    return 'tools' if state['tool_calls'] else 'finish'


async def tools(state: State, config: RunnableConfig) -> State:
    messages = state['messages']
    turn = Turn('', [ToolCall(call['id'], call['name'], call['input']) for call in state['tool_calls']])
    await request_loop(config).tool_step(messages, turn)
    return {'messages': messages, 'tool_calls': []}


def after_tools(state: State, config: RunnableConfig) -> Literal['wrap_up', 'model']:
    loop = request_loop(config)
    return 'wrap_up' if loop.rounds >= loop.settings.max_tool_rounds else 'model'


async def wrap_up(state: State, config: RunnableConfig) -> State:
    messages = state['messages']
    await request_loop(config).wrap_up_step(messages)
    return {'messages': messages}


async def finish(state: State, config: RunnableConfig) -> State:
    return {'claims_needed': request_loop(config).finish_step()}


def after_finish(state: State) -> Literal['claims', '__end__']:
    return 'claims' if state['claims_needed'] else '__end__'


async def claims(state: State, config: RunnableConfig) -> State:
    messages = state['messages']
    await request_loop(config).claims_step(messages)
    return {'messages': messages}


builder = StateGraph(State)
builder.add_node('start', start)
builder.add_node('model', model)
builder.add_node('tools', tools)
builder.add_node('wrap_up', wrap_up)
builder.add_node('finish', finish)
builder.add_node('claims', claims)
builder.add_edge(START, 'start')
builder.add_edge('start', 'model')
builder.add_conditional_edges('model', after_model)
builder.add_conditional_edges('tools', after_tools)
builder.add_edge('wrap_up', 'finish')
builder.add_conditional_edges('finish', after_finish)
builder.add_edge('claims', END)
graph = builder.compile()


def mermaid() -> str:
    return graph.get_graph().draw_mermaid()


async def run(loop: Loop, question: str) -> None:
    # Two nodes per tool round, plus start, wrap_up, finish, claims, and termination headroom.
    with tracing_context(enabled=False):
        await graph.ainvoke(
            {'messages': [{'role': 'user', 'content': question}]},
            config={'configurable': {'loop': loop}, 'recursion_limit': 2 * loop.settings.max_tool_rounds + 5},
        )


async def stream_answer(
    question: str,
    settings: ChatSettings,
    *,
    model_client: ModelClient | None = None,
    tools: ToolService | None = None,
) -> AsyncGenerator[Event]:
    model = model_client if model_client is not None else build_model_client(settings)
    service = tools if tools is not None else build_tool_service(settings)
    loop = Loop(settings, model, service)
    async with aclosing(
        stream_loop(loop, run(loop, question), owned_model=model if model_client is None else None)
    ) as stream:
        async for event in stream:
            yield event
