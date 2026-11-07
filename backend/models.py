"""HTTP response shapes retain the existing fictional data contract."""

from typing import Literal

from pydantic import BaseModel, Field

from preflight_core.models import Aircraft, Issue

__all__ = ['Aircraft', 'AircraftList', 'Health', 'Issue', 'IssueList']


class Health(BaseModel):
    status: Literal['ok']
    database: Literal['ok']


class AircraftList(BaseModel):
    count: int = Field(ge=0)
    items: list[Aircraft]


class IssueList(BaseModel):
    count: int = Field(ge=0)
    items: list[Issue]
