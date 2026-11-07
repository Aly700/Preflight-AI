"""Three internal read-only HTTP endpoints backed by PostgreSQL."""

from collections.abc import Awaitable, Callable, Iterator
import os
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
import psycopg
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend.models import AircraftList, Health, IssueList
from backend.repository import Repository
from preflight_core.database import open_database
from preflight_core.settings import api_hosts_from_environment

DEFAULT_ALLOWED_HOSTS = ['localhost', '127.0.0.1']
APPLICATION_NAME = 'preflight-oss-api'


def get_repository() -> Iterator[Repository]:
    try:
        with open_database(application_name=APPLICATION_NAME) as connection:
            yield Repository(connection)
    except psycopg.Error, OSError, KeyError, ValueError:
        # Never put connection details or credentials in an HTTP response.
        raise HTTPException(status_code=503, detail='Database unavailable') from None


Database = Annotated[Repository, Depends(get_repository, scope='function')]


def default_static_directory() -> Path:
    """The built frontend when present, otherwise a (normally absent) backend/static folder."""
    built = Path(__file__).resolve().parents[1] / 'frontend/dist'
    return built if built.exists() else Path(__file__).parent / 'static'


def create_app(*, allowed_hosts: list[str] | None = None, static_directory: Path | None = None) -> FastAPI:
    """Build the API.

    `allowed_hosts` defaults to PREFLIGHT_ALLOWED_HOSTS, then to loopback names only.
    `static_directory` holds index.html, icon.svg and assets/; when its index.html is
    absent, `/` answers 404 because the frontend is hosted separately.
    """
    if allowed_hosts is None:
        allowed_hosts = api_hosts_from_environment(os.environ, 'PREFLIGHT_ALLOWED_HOSTS', DEFAULT_ALLOWED_HOSTS)
    app = FastAPI(title='Preflight AI structured-data API', version='0.1.0', docs_url=None, redoc_url=None)
    from backend.chat import create_router

    app.include_router(create_router())
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))
    static = default_static_directory() if static_directory is None else static_directory
    app.mount('/assets', StaticFiles(directory=static / 'assets', check_dir=False), name='assets')

    @app.get('/icon.svg', include_in_schema=False)
    def icon() -> FileResponse:
        return FileResponse(static / 'icon.svg')

    @app.middleware('http')
    async def browser_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.get('/', include_in_schema=False)
    def index() -> FileResponse:
        if not (static / 'index.html').exists():
            raise HTTPException(404, 'The frontend is hosted separately')
        return FileResponse(static / 'index.html', headers={'Cache-Control': 'no-store'})

    @app.get('/health', response_model=Health)
    def health(repository: Database) -> dict[str, str]:
        if not repository.healthy():
            raise HTTPException(status_code=503, detail='Database unavailable')
        return {'status': 'ok', 'database': 'ok'}

    @app.get('/api/aircraft', response_model=AircraftList)
    def aircraft(repository: Database) -> dict[str, Any]:
        records = repository.aircraft()
        return {'count': len(records), 'items': records}

    @app.get('/api/issues', response_model=IssueList)
    def issues(repository: Database) -> dict[str, Any]:
        records = repository.issues()
        return {'count': len(records), 'items': records}

    return app


app = create_app()
