"""One small interface over the model provider so the loop and its tests never see an SDK.

`AnthropicClient` streams one assistant turn through the Messages API and
reports text as it arrives; the returned `Turn` carries the tool calls the model
wants made. Provider failures surface as `ModelError` naming only the exception
type: no key, no URL, no upstream text reaches a log or a client.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from anthropic import AsyncAnthropic, AsyncAnthropicBedrock

from backend.settings import ChatSettings, SettingsError

DEFAULT_MAX_TOKENS = 4096
USAGE_KEYS = ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')


class ModelError(RuntimeError):
    """The provider failed; the message names the failure type only."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class Turn:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = 'end_turn'
    usage: dict[str, int] = field(default_factory=dict)


class ModelClient(Protocol):
    model: str

    async def aclose(self) -> None: ...

    async def stream_turn(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: dict[str, Any] | None,
        on_text: Callable[[str], None],
    ) -> Turn: ...


class AnthropicClient:
    """Claude through the Anthropic Messages API, streaming, with native tool use."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        workspace_id: str | None = None,
        client_factory: Callable[..., Any] = AsyncAnthropic,
        client: Any | None = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        if client is not None:
            self._client = client
            return
        options: dict[str, Any] = {'api_key': api_key}
        if workspace_id:
            options['default_headers'] = {'anthropic-workspace-id': workspace_id}
        self._client = client_factory(**options)

    async def aclose(self) -> None:
        close = getattr(self._client, 'close', None)
        if close is not None:
            await close()

    async def stream_turn(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: dict[str, Any] | None,
        on_text: Callable[[str], None],
    ) -> Turn:
        cached_tools = list(tools)
        if cached_tools:
            cached_tools[-1] = {**cached_tools[-1], 'cache_control': {'type': 'ephemeral'}}
        options: dict[str, Any] = {
            'model': self.model,
            'max_tokens': self.max_tokens,
            'system': [{'type': 'text', 'text': system, 'cache_control': {'type': 'ephemeral'}}],
            'messages': messages,
            'tools': cached_tools,
        }
        if tool_choice is not None:
            options['tool_choice'] = tool_choice
        callback_error: Exception | None = None
        try:
            async with self._client.messages.stream(**options) as stream:
                async for event in stream:
                    if getattr(event, 'type', None) == 'text':
                        try:
                            on_text(event.text)
                        except Exception as error:
                            callback_error = error
                            raise
                final = await stream.get_final_message()
        except Exception as error:
            if error is callback_error:
                raise
            # The SDK's message may carry the URL or the request; keep only the type.
            raise ModelError(f'Model request failed: {type(error).__name__}') from None
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for item in final.content:
            kind = getattr(item, 'type', None)
            if kind == 'text':
                text_parts.append(item.text)
            elif kind == 'tool_use':
                calls.append(ToolCall(id=str(item.id), name=str(item.name), input=dict(item.input or {})))
        usage = getattr(final, 'usage', None)
        return Turn(
            text=''.join(text_parts),
            tool_calls=calls,
            stop_reason=str(final.stop_reason or 'end_turn'),
            usage={key: int(getattr(usage, key, None) or 0) for key in USAGE_KEYS},
        )


def build_model_client(settings: ChatSettings) -> ModelClient:
    """Claude through the Anthropic API or Amazon Bedrock for the native runtime."""
    if settings.provider == 'bedrock' and settings.model and settings.aws_region:
        return AnthropicClient(model=settings.model, client=AsyncAnthropicBedrock(aws_region=settings.aws_region))
    if settings.provider != 'anthropic' or not settings.model_api_key or not settings.model:
        raise SettingsError('MODEL_PROVIDER must be anthropic or bedrock for the native runtime')
    return AnthropicClient(api_key=settings.model_api_key, model=settings.model, workspace_id=settings.workspace_id)
