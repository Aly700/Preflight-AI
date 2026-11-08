"""Chat transport configuration: file-based credentials, one explicit MCP target, one model provider.

The MCP URL has no default on purpose. A new deployment must say which MCP
service it talks to; inheriting another stack's service alias is the failure
mode this prevents (see docs/SETUP.md, "Local stack").

The model provider is explicit too. `copilot` (the default) needs the GitHub
Copilot token; `anthropic` and `openai` are bring-your-own-key providers that
need the developer's API key file and a model name instead. `bedrock` uses the
standard AWS credential chain with an explicit model and region.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
import os
import re
from urllib.parse import urlsplit

from preflight_core.settings import SettingsError, read_mcp_token, read_secret_file, required

__all__ = ['ChatSettings', 'SettingsError']

MODEL_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$')
AWS_REGION = re.compile(r'^[a-z]{2}(?:-[a-z]+)+-[0-9]$')
PROVIDERS = ('copilot', 'anthropic', 'openai', 'bedrock')
RUNTIMES = ('copilot', 'native', 'langgraph')
NATIVE_RUNTIMES = ('native', 'langgraph')
NATIVE_PROVIDERS = ('anthropic', 'bedrock')
WORKSPACE_ID = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
MCP_TOOL_PREFIX = re.compile(r'[A-Za-z0-9][A-Za-z0-9-]{0,99}___')


def mcp_url(environ: Mapping[str, str]) -> str:
    value = required(environ, 'PREFLIGHT_MCP_URL')
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
    except ValueError:
        hostname = None
        parts = None
    if (
        parts is None
        or parts.scheme not in ('http', 'https')
        or not hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise SettingsError('PREFLIGHT_MCP_URL must be a plain http(s) URL without credentials or fragment')
    return value


def model_name(environ: Mapping[str, str]) -> str | None:
    value = environ.get('COPILOT_MODEL', '').strip()
    if not value:
        return None
    if MODEL_NAME.fullmatch(value) is None:
        raise SettingsError('COPILOT_MODEL has an invalid format')
    return value


def workspace_id(environ: Mapping[str, str]) -> str | None:
    """Anthropic workspace for a user-scoped key; sent as the anthropic-workspace-id header when set."""
    value = environ.get('ANTHROPIC_WORKSPACE_ID', '').strip()
    if not value:
        return None
    if WORKSPACE_ID.fullmatch(value) is None:
        raise SettingsError('ANTHROPIC_WORKSPACE_ID has an invalid format')
    return value


def chat_runtime(environ: Mapping[str, str], provider: str) -> str:
    """The Copilot SDK loop, or the Messages API loop driven by native code or LangGraph."""
    value = environ.get('CHAT_RUNTIME', '').strip() or 'copilot'
    if value not in RUNTIMES:
        raise SettingsError('CHAT_RUNTIME must be one of ' + ', '.join(RUNTIMES))
    if value in NATIVE_RUNTIMES and provider not in NATIVE_PROVIDERS:
        raise SettingsError('CHAT_RUNTIME=native or langgraph requires MODEL_PROVIDER=' + ' or '.join(NATIVE_PROVIDERS))
    if provider == 'bedrock' and value not in NATIVE_RUNTIMES:
        raise SettingsError('CHAT_RUNTIME must be native or langgraph when MODEL_PROVIDER is bedrock')
    return value


def bounded_int(environ: Mapping[str, str], name: str, default: int, *, low: int = 1, high: int = 20) -> int:
    value = environ.get(name, '').strip()
    if not value:
        return default
    if not value.isdigit() or not low <= int(value) <= high:
        raise SettingsError(f'{name} must be an integer from {low} to {high}')
    return int(value)


def model_provider(environ: Mapping[str, str]) -> str:
    value = environ.get('MODEL_PROVIDER', '').strip() or 'copilot'
    if value not in PROVIDERS:
        raise SettingsError('MODEL_PROVIDER must be one of ' + ', '.join(PROVIDERS))
    return value


@dataclass(frozen=True)
class ChatSettings:
    access_token: str = field(repr=False)
    copilot_token: str | None = field(repr=False)
    mcp_token: str | None = field(repr=False)
    mcp_url: str
    model: str | None = None
    provider: str = 'copilot'
    model_api_key: str | None = field(default=None, repr=False)
    workspace_id: str | None = None
    runtime: str = 'copilot'
    max_tool_rounds: int = 6
    max_tool_calls: int = 12
    aws_region: str | None = None
    mcp_auth: str = 'bearer'
    mcp_tool_prefix: str = ''

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> ChatSettings:
        """Load and validate every setting; raise SettingsError naming the first problem."""
        source = os.environ if environ is None else environ
        provider = model_provider(source)
        model = model_name(source)
        auth = source.get('MCP_AUTH', 'bearer')
        if auth not in ('bearer', 'sigv4'):
            raise SettingsError('MCP_AUTH must be bearer or sigv4')
        prefix = source.get('MCP_TOOL_PREFIX', '')
        if prefix and MCP_TOOL_PREFIX.fullmatch(prefix) is None:
            raise SettingsError('MCP_TOOL_PREFIX has an invalid format')
        if auth == 'sigv4' and source.get('CHAT_RUNTIME', '').strip() not in NATIVE_RUNTIMES:
            raise SettingsError('MCP_AUTH=sigv4 requires CHAT_RUNTIME=native or langgraph')
        runtime = chat_runtime(source, provider)
        url = mcp_url(source)
        if auth == 'sigv4' and urlsplit(url).scheme != 'https':
            raise SettingsError('PREFLIGHT_MCP_URL must use https when MCP_AUTH=sigv4')
        aws_region = None
        if provider == 'copilot':
            copilot_token: str | None = read_secret_file(source, 'COPILOT_TOKEN_FILE')
            model_api_key: str | None = None
        elif provider == 'bedrock':
            copilot_token = None
            model_api_key = None
            if model is None:
                raise SettingsError('COPILOT_MODEL is required when MODEL_PROVIDER is bedrock')
        else:
            copilot_token = None
            model_api_key = read_secret_file(source, 'MODEL_API_KEY_FILE', minimum=20)
            if model is None:
                raise SettingsError('COPILOT_MODEL is required when MODEL_PROVIDER is ' + provider)
        if provider == 'bedrock' or auth == 'sigv4':
            aws_region = required(source, 'AWS_REGION')
            if AWS_REGION.fullmatch(aws_region) is None:
                raise SettingsError('AWS_REGION has an invalid format')
        return cls(
            access_token=read_secret_file(source, 'CHAT_TOKEN_FILE'),
            copilot_token=copilot_token,
            mcp_token=read_mcp_token(source) if auth == 'bearer' else None,
            mcp_url=url,
            mcp_auth=auth,
            mcp_tool_prefix=prefix,
            model=model,
            provider=provider,
            model_api_key=model_api_key,
            aws_region=aws_region,
            workspace_id=workspace_id(source),
            runtime=runtime,
            max_tool_rounds=bounded_int(source, 'MAX_TOOL_ROUNDS', 6),
            max_tool_calls=bounded_int(source, 'MAX_TOOL_CALLS', 12),
        )
