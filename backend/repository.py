"""Explicit SELECT operations; no HTTP or fixture-file dependency."""

from typing import Any

import psycopg

Row = dict[str, Any]


class Repository:
    def __init__(self, connection: psycopg.Connection[Row]) -> None:
        self.connection = connection

    def healthy(self) -> bool:
        row = self.connection.execute('SELECT 1 AS ready').fetchone()
        return row is not None and row['ready'] == 1

    def aircraft(self) -> list[Row]:
        return self.connection.execute("""
            SELECT aircraft_id, program, stage, station, record_created_at
            FROM preflight.aircraft ORDER BY aircraft_id
        """).fetchall()

    def issues(self) -> list[Row]:
        return self.connection.execute("""
            SELECT issue_id, aircraft_id, title, description, subsystem,
                   priority, status, opened_at, closed_at
            FROM preflight.issues ORDER BY issue_id
        """).fetchall()
