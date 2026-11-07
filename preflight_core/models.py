"""Record shapes shared by the API and the MCP tools, and the tool result schemas.

Everything here describes wholly fictional demonstration records. Identifier
patterns are the fictional namespaces from the data contract; nothing resembles
a real aircraft, person or organization identifier.
"""

import re
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

RECORD_ID = re.compile(r'^(?:DEMO-AIR|ISSUE|TASK|EVT|TEAM|PERSON|SHIFT)-[0-9]+$')

AircraftId = Annotated[str, Field(pattern=r'^DEMO-AIR-[0-9]+$', max_length=64, strict=True)]
IssueId = Annotated[str, Field(pattern=r'^ISSUE-[0-9]+$', max_length=64, strict=True)]
PageLimit = Annotated[int, Field(ge=1, le=100, strict=True)]
PageOffset = Annotated[int, Field(ge=0, le=10000, strict=True)]
Priority = Literal['low', 'medium', 'high']
Subsystem = Literal['hydraulics', 'avionics', 'electrical', 'cabin', 'structures', 'controls']
GroupBy = Literal['subsystem', 'priority', 'status', 'aircraft']
IssueStatus = Literal['open', 'in_progress', 'blocked', 'reopened', 'closed']
TaskStatus = Literal['todo', 'in_progress', 'awaiting_review', 'completed', 'blocked']
EventType = Literal[
    'issue_opened',
    'issue_closed',
    'issue_reopened',
    'task_created',
    'task_assigned',
    'task_started',
    'task_completed',
    'task_review_requested',
    'task_blocked',
    'task_unblocked',
    'note_added',
]
Count = Annotated[int, Field(ge=0)]
Flag = Annotated[bool, Field(strict=True)]


class Aircraft(BaseModel):
    aircraft_id: str = Field(pattern=r'^DEMO-AIR-[0-9]+$')
    program: str
    stage: str
    station: str
    record_created_at: AwareDatetime


class Issue(BaseModel):
    issue_id: str = Field(pattern=r'^ISSUE-[0-9]+$')
    aircraft_id: str = Field(pattern=r'^DEMO-AIR-[0-9]+$')
    title: str
    description: str
    subsystem: str
    priority: Priority
    status: IssueStatus
    opened_at: AwareDatetime
    closed_at: AwareDatetime | None


class ResultModel(BaseModel):
    """Tool results reject unknown fields so an unexpected payload shape fails validation."""

    model_config = ConfigDict(extra='forbid')


class AircraftSummary(ResultModel):
    aircraft: Aircraft
    issue_count: Count
    unresolved_issue_count: Count
    issue_status_counts: dict[IssueStatus, Count]
    task_count: Count
    task_status_counts: dict[TaskStatus, Count]


class IssuePage(ResultModel):
    total: Count
    limit: PageLimit
    offset: PageOffset
    items: list[Issue]


class ActivityEvent(ResultModel):
    event_id: str = Field(pattern=r'^EVT-[0-9]+$')
    issue_id: IssueId
    task_id: str | None = Field(pattern=r'^TASK-[0-9]+$')
    actor_id: str | None = Field(pattern=r'^PERSON-[0-9]+$')
    event_type: EventType
    occurred_at: AwareDatetime
    note: str


class IssueHistory(ResultModel):
    issue: Issue
    total_events: Count
    limit: PageLimit
    offset: PageOffset
    events: list[ActivityEvent]


class CountGroup(ResultModel):
    key: str
    count: Count


class CountFilters(ResultModel):
    aircraft_id: AircraftId | None
    status: IssueStatus | None
    subsystem: Subsystem | None
    priority: Priority | None


class IssueCounts(ResultModel):
    """Issue counts grouped by one column; SQL does the counting, keys with no issues are absent."""

    group_by: GroupBy
    unresolved_only: bool
    filters: CountFilters
    total: Count
    groups: list[CountGroup]


class AircraftRank(ResultModel):
    aircraft_id: AircraftId
    unresolved_issue_count: Count
    issue_count: Count


class AircraftComparison(ResultModel):
    """Summaries for several aircraft in request order, ranked by unresolved issues, with the spread."""

    summaries: list[AircraftSummary]
    ranked_by_unresolved: list[AircraftRank]
    unresolved_spread: Count
