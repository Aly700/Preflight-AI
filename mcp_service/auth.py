"""Internal service authentication; tokens are mounted files, never tool inputs."""

from collections.abc import Mapping
import hmac
import os

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from preflight_core.settings import read_mcp_token


def load_token(environ: Mapping[str, str] | None = None) -> str:
    """Read MCP_TOKEN_FILE with the shared rule; raises SettingsError naming the variable."""
    return read_mcp_token(os.environ if environ is None else environ)


class BearerAuthentication:
    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.expected = b'Bearer ' + token.encode('ascii')

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope['type'] == 'http':
            values = [v for k, v in scope['headers'] if k.lower() == b'authorization']
            if len(values) != 1 or not hmac.compare_digest(values[0], self.expected):
                response = JSONResponse(
                    {'error': 'Unauthorized'}, status_code=401, headers={'WWW-Authenticate': 'Bearer'}
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
