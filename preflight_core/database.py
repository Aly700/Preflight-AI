"""Short-lived, read-only PostgreSQL connections; secrets stay outside source.

Each service names itself through `application_name` so `pg_stat_activity`
attributes sessions to the right process.
"""

from collections.abc import Iterator
from contextlib import contextmanager
import os
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row


@contextmanager
def open_database(*, application_name: str) -> Iterator[psycopg.Connection[dict[str, Any]]]:
    password = Path(os.environ['PGPASSWORD_FILE']).read_text().strip()
    if not password:
        raise ValueError('Empty database password file')
    with psycopg.connect(
        host=os.environ['PGHOST'],
        port=int(os.environ.get('PGPORT', '5432')),
        dbname=os.environ['PGDATABASE'],
        user=os.environ['PGUSER'],
        password=password,
        connect_timeout=5,
        options='-c statement_timeout=5000 -c timezone=UTC',
        application_name=application_name,
        row_factory=dict_row,
    ) as connection:
        connection.read_only = True
        yield connection
