"""Chat transport configuration: file-based credentials, one explicit MCP target, one model provider.

The MCP URL has no default on purpose. A new deployment must say which MCP
service it talks to; inheriting another stack's service alias is the failure
mode this prevents (see docs/SETUP.md, "Local stack").

The model provider is explicit too. `copilot` (the default) needs the GitHub
Copilot token; `anthropic` and `openai` are bring-your-own-key providers that
need the developer's API key file and a model name instead.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
import os
import re
from urllib.parse import urlsplit

from preflight_core.settings import SettingsError, read_mcp_token, read_secret_file, required

__all__ = ['ChatSettings', 'SettingsError']

MODEL_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$')
PROVIDERS = ('copilot', 'anthropic', 'openai')
WORKSPACE_ID = re.compile(r'^[A-Za-z0-9_-]{1,128}$')


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


def model_provider(environ: Mapping[str, str]) -> str:
    value = environ.get('MODEL_PROVIDER', '').strip() or 'copilot'
    if value not in PROVIDERS:
        raise SettingsError('MODEL_PROVIDER must be one of ' + ', '.join(PROVIDERS))
    return value


@dataclass(frozen=True)
class ChatSettings:
    access_token: str = field(repr=False)
    copilot_token: str | None = field(repr=False)
    mcp_token: str = field(repr=False)
    mcp_url: str
    model: str | None = None
    provider: str = 'copilot'
    model_api_key: str | None = field(default=None, repr=False)
    workspace_id: str | None = None

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> ChatSettings:
        """Load and validate every setting; raise SettingsError naming the first problem."""
        source = os.environ if environ is None else environ
        provider = model_provider(source)
        model = model_name(source)
        if provider == 'copilot':
            copilot_token: str | None = read_secret_file(source, 'COPILOT_TOKEN_FILE')
            model_api_key: str | None = None
        else:
            copilot_token = None
            model_api_key = read_secret_file(source, 'MODEL_API_KEY_FILE', minimum=20)
            if model is None:
                raise SettingsError('COPILOT_MODEL is required when MODEL_PROVIDER is ' + provider)
        return cls(
            access_token=read_secret_file(source, 'CHAT_TOKEN_FILE'),
            copilot_token=copilot_token,
            mcp_token=read_mcp_token(source),
            mcp_url=mcp_url(source),
            model=model,
            provider=provider,
            model_api_key=model_api_key,
            workspace_id=workspace_id(source),
        )
