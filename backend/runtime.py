"""Pick the chat runtime by configuration; all runtimes share one event contract."""

from collections.abc import AsyncGenerator
from typing import Any

from backend.settings import ChatSettings


async def stream_answer(
    question: str, settings: ChatSettings, *, scope: dict[str, str] | None = None
) -> AsyncGenerator[dict[str, Any]]:
    # Runtime modules load lazily so a missing SDK cannot break the record API.
    if settings.runtime == 'native':
        from backend.agent_runtime import stream_answer as native_answer

        async for event in native_answer(question, settings, scope=scope):
            yield event
        return
    if settings.runtime == 'langgraph':
        from backend.graph_runtime import stream_answer as graph_answer

        async for event in graph_answer(question, settings, scope=scope):
            yield event
        return
    from backend.copilot_chat import stream_answer as copilot_answer

    async for event in copilot_answer(question, settings, scope=scope):
        yield event
