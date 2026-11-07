"""Configuration helpers shared by the services.

Every failure is a SettingsError whose message names the environment variable
and the rule it broke. Messages never include the value that was rejected, so
they are safe to log and to turn into a generic HTTP error.
"""

from collections.abc import Mapping
from pathlib import Path
import re

# A host name, optionally wildcard-prefixed, optionally with a port or a "*" port.
LABEL = r'[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?'
# Starlette's TrustedHostMiddleware compares host names without a port and supports a leading "*." wildcard.
API_HOST = re.compile(rf'^(?:\*\.)?{LABEL}(?:\.{LABEL})*$')
# MCP transport security matches "host", "host:port" or "host:*"; it has no domain wildcard.
MCP_HOST = re.compile(rf'^{LABEL}(?:\.{LABEL})*(?::(?:\*|[0-9]{{1,5}}))?$')


class SettingsError(ValueError):
    """Configuration is missing or invalid; the message names the variable, never its value."""


def required(environ: Mapping[str, str], name: str) -> str:
    """Return a non-blank variable or fail with its name."""
    value = environ.get(name, '').strip()
    if not value:
        raise SettingsError(f'{name} is not set')
    return value


def read_secret_file(environ: Mapping[str, str], name: str, *, minimum: int = 32, maximum: int = 512) -> str:
    """Read a credential from the file named by `name`; the credential itself never enters the environment."""
    path = Path(required(environ, name))
    try:
        secret = path.read_text(encoding='utf-8').strip()
    except OSError, UnicodeDecodeError:
        raise SettingsError(f'{name} does not name a readable credential file') from None
    if not minimum <= len(secret) <= maximum or not secret.isascii() or any(c.isspace() for c in secret):
        raise SettingsError(f'{name} names a file whose credential has an invalid format')
    return secret


MCP_TOKEN_FORMAT = re.compile(r'^[a-f0-9]{64}$')


def read_mcp_token(environ: Mapping[str, str]) -> str:
    """The MCP bearer token: exactly 64 lowercase hex characters, the rule the MCP service enforces.

    The chat API validates with the same function so "configured" on the chat
    side cannot describe a token the MCP service would reject.
    """
    token = read_secret_file(environ, 'MCP_TOKEN_FILE', minimum=64, maximum=64)
    if MCP_TOKEN_FORMAT.fullmatch(token) is None:
        raise SettingsError('MCP_TOKEN_FILE names a file whose token is not 64 lowercase hexadecimal characters')
    return token


def hosts_from_environment(
    environ: Mapping[str, str], name: str, default: list[str], *, pattern: re.Pattern[str], accepted: str
) -> list[str]:
    """Parse a comma-separated host allowlist, or return the loopback default when the variable is absent."""
    if name not in environ:
        return list(default)
    hosts = [entry.strip() for entry in environ[name].split(',') if entry.strip()]
    if not hosts or any(pattern.fullmatch(host) is None for host in hosts):
        raise SettingsError(f'{name} must be a comma-separated list of {accepted}')
    return hosts


def api_hosts_from_environment(environ: Mapping[str, str], name: str, default: list[str]) -> list[str]:
    """Host names for the API's TrustedHost check: `example.test` or `*.example.test`, never a port."""
    return hosts_from_environment(
        environ, name, default, pattern=API_HOST, accepted='host names, optionally *.domain, without ports'
    )


def mcp_hosts_from_environment(environ: Mapping[str, str], name: str, default: list[str]) -> list[str]:
    """Host entries for MCP transport security: `host`, `host:port` or `host:*`, never a domain wildcard."""
    return hosts_from_environment(
        environ, name, default, pattern=MCP_HOST, accepted='host, host:port or host:* entries'
    )
