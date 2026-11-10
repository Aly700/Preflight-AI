"""Record shapes shared by the API and the MCP tools, and the tool result schemas.

Everything here describes wholly fictional demonstration records. Identifier
patterns are the fictional namespaces from the data contract; nothing resembles
a real aircraft, person or organization identifier.
"""

from datetime import date as Date
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

# Explicit re-exports retain the existing model imports while scope and tools share one vocabulary.
from preflight_core.scope import RECORD_ID as RECORD_ID
from preflight_core.scope import DocumentKind as DocumentKind
from preflight_core.scope import IssueStatus as IssueStatus
from preflight_core.scope import Priority as Priority
from preflight_core.scope import Subsystem as Subsystem
from preflight_core.scope import TaskKind as TaskKind
from preflight_core.scope import TaskStatus as TaskStatus

AircraftId = Annotated[str, Field(pattern=r'^DEMO-AIR-[0-9]+$', max_length=64, strict=True)]
IssueId = Annotated[str, Field(pattern=r'^ISSUE-[0-9]+$', max_length=64, strict=True)]
TeamId = Annotated[str, Field(pattern=r'^TEAM-[0-9]+$', max_length=64, strict=True)]
PersonId = Annotated[str, Field(pattern=r'^PERSON-[0-9]+$', max_length=64, strict=True)]
TaskPageLimit = Annotated[int, Field(ge=1, le=50, strict=True)]
PageLimit = Annotated[int, Field(ge=1, le=100, strict=True)]
PageOffset = Annotated[int, Field(ge=0, le=10000, strict=True)]
GroupBy = Literal['subsystem', 'priority', 'status', 'aircraft']
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
RecordId = Annotated[str, Field(pattern=RECORD_ID.pattern, max_length=64, strict=True)]
DocumentQuery = Annotated[str, Field(min_length=3, max_length=300, strict=True)]
DocumentTopK = Annotated[int, Field(ge=1, le=8, strict=True)]
CandidateRank = Annotated[int, Field(ge=1, le=20, strict=True)]


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


class Task(ResultModel):
    task_id: str = Field(pattern=r'^TASK-[0-9]+$', max_length=64, strict=True)
    issue_id: IssueId
    aircraft_id: AircraftId
    task_kind: TaskKind
    status: TaskStatus
    assigned_team_id: TeamId
    assigned_person_id: PersonId | None
    created_at: AwareDatetime
    completed_at: AwareDatetime | None


class TaskPage(ResultModel):
    total: Count
    limit: TaskPageLimit
    offset: PageOffset
    items: list[Task]


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


class DocumentPassage(ResultModel):
    passage_id: str = Field(pattern=r'^DOC-[0-9]{4}#[0-9]+$')
    doc_id: str = Field(pattern=r'^DOC-[0-9]{4}$')
    title: str
    kind: DocumentKind
    date: Date
    station: str | None
    program: str | None
    related_ids: list[RecordId]
    # The document's author and team, so an answer that names them verifies.
    author_id: RecordId
    team_id: RecordId
    text: str
    score: float = Field(ge=0, allow_inf_nan=False)
    keyword_rank: CandidateRank | None
    vector_rank: CandidateRank | None


class DocumentSearchResult(ResultModel):
    results: list[DocumentPassage] = Field(max_length=8)
