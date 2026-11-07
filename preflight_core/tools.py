"""The single declaration of the three read-only tools: names, argument sets and result schemas.

The MCP service uses this to reject unknown tools and unexpected arguments
before any handler runs. The Copilot adapter uses it to recognise permitted
tools and to validate their results. Adding a tool here widens both sides at
once, which is why the list is short and every change to it is reviewed.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from preflight_core.models import AircraftSummary, IssueHistory, IssuePage, ResultModel

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
)
TOOLS: Mapping[str, ToolContract] = {contract.name: contract for contract in TOOL_CONTRACTS}
QUALIFIED_NAMES: Mapping[str, str] = {contract.qualified_name: contract.name for contract in TOOL_CONTRACTS}
