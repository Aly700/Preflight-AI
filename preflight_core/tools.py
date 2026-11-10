"""The single declaration of the eight read-only tools: names, argument sets and result schemas.

The MCP service uses this to reject unknown tools and unexpected arguments
before any handler runs. The Copilot adapter uses it to recognise permitted
tools and to validate their results. Adding a tool here widens both sides at
once, which is why the list is short and every change to it is reviewed.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from preflight_core.models import (
    AircraftComparison,
    AircraftSummary,
    DocumentSearchResult,
    IssueCounts,
    IssueHistory,
    IssuePage,
    ResultModel,
    TaskPage,
)

MCP_SERVER_NAME = 'preflight'


@dataclass(frozen=True)
class ToolContract:
    name: str
    arguments: frozenset[str]
    result: type[ResultModel]

    @property
    def qualified_name(self) -> str:
        """The name the Copilot runtime gives an MCP tool: `<server>-<tool>`."""
        return f'{MCP_SERVER_NAME}-{self.name}'


TOOL_CONTRACTS: tuple[ToolContract, ...] = (
    ToolContract('get_aircraft_summary', frozenset({'aircraft_id'}), AircraftSummary),
    ToolContract('list_open_issues', frozenset({'aircraft_id', 'priority', 'limit', 'offset'}), IssuePage),
    ToolContract('get_issue_history', frozenset({'issue_id', 'limit', 'offset'}), IssueHistory),
    ToolContract(
        'count_issues',
        frozenset(
            {
                'group_by',
                'aircraft_id',
                'status',
                'subsystem',
                'priority',
                'unresolved_only',
                'opened_after',
                'opened_before',
            }
        ),
        IssueCounts,
    ),
    ToolContract('compare_aircraft', frozenset({'aircraft_ids'}), AircraftComparison),
    ToolContract(
        'search_issues',
        frozenset(
            {
                'status',
                'subsystem',
                'priority',
                'aircraft_id',
                'opened_after',
                'opened_before',
                'limit',
                'offset',
                'unresolved_only',
            }
        ),
        IssuePage,
    ),
    ToolContract('search_documents', frozenset({'query', 'top_k', 'kind', 'related_id'}), DocumentSearchResult),
    ToolContract(
        'list_tasks',
        frozenset(
            {
                'status',
                'task_kind',
                'assigned_team_id',
                'assigned_person_id',
                'aircraft_id',
                'issue_id',
                'limit',
                'offset',
            }
        ),
        TaskPage,
    ),
)
TOOLS: Mapping[str, ToolContract] = {contract.name: contract for contract in TOOL_CONTRACTS}
QUALIFIED_NAMES: Mapping[str, str] = {contract.qualified_name: contract.name for contract in TOOL_CONTRACTS}
