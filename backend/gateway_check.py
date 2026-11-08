"""Compare direct MCP and configured Gateway results and per-call latency.

Run inside the API container with --direct-url and optional --repeats/--output.
Reports contain registry names, matches and timings, never URLs or credentials.
Each timing includes the MCP client's connection and session setup.
"""

import argparse
import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
from statistics import median
import sys
import time
from typing import Any

from backend.mcp_tools import McpTools, ToolResult, ToolService, build_tool_service, leaf_exception_types
from backend.settings import ChatSettings, mcp_url
from preflight_core.settings import read_mcp_token
from preflight_core.tools import TOOLS


@dataclass(frozen=True)
class Case:
    name: str
    tool: str
    arguments: dict[str, Any]
    error: str | None = None


CASES = (
    Case('aircraft_summary', 'get_aircraft_summary', {'aircraft_id': 'DEMO-AIR-0001'}),
    Case('open_issues', 'list_open_issues', {'aircraft_id': 'DEMO-AIR-0002', 'limit': 3}),
    Case('issue_history', 'get_issue_history', {'issue_id': 'ISSUE-0001', 'limit': 3}),
    Case('issue_counts', 'count_issues', {'group_by': 'priority', 'aircraft_id': 'DEMO-AIR-0001'}),
    Case('aircraft_comparison', 'compare_aircraft', {'aircraft_ids': ['DEMO-AIR-0001', 'DEMO-AIR-0002']}),
    Case('issue_search', 'search_issues', {'aircraft_id': 'DEMO-AIR-0002', 'limit': 3}),
    Case('missing_aircraft', 'get_aircraft_summary', {'aircraft_id': 'DEMO-AIR-9999'}, 'Record not found'),
)


async def timed_call(
    service: ToolService, case: Case, call_id: str, clock: Callable[[], float]
) -> tuple[ToolResult, float]:
    start = clock()
    result = await service.call(call_id, case.tool, dict(case.arguments))
    return result, (clock() - start) * 1000


async def run_check(
    direct: ToolService,
    gateway: ToolService,
    unsigned: ToolService,
    *,
    repeats: int = 3,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    """Compare each repetition; successful cases must succeed and the missing record must be refused."""
    if repeats < 1:
        raise ValueError('repeats must be positive')
    discovered = {
        'direct': [spec.name for spec in await direct.discover()],
        'gateway': [spec.name for spec in await gateway.discover()],
    }
    cases = []
    all_direct: list[float] = []
    all_gateway: list[float] = []
    for case in CASES:
        direct_ms: list[float] = []
        gateway_ms: list[float] = []
        match = True
        for repeat in range(repeats):
            call_id = f'{case.name}-{repeat}'
            left, elapsed_direct = await timed_call(direct, case, call_id, clock)
            right, elapsed_gateway = await timed_call(gateway, case, call_id, clock)
            direct_ms.append(elapsed_direct)
            gateway_ms.append(elapsed_gateway)
            match = (
                match
                and (left.ok, left.payload, left.error) == (right.ok, right.payload, right.error)
                and left.ok == (case.error is None)
                and left.error == case.error
            )
        all_direct.extend(direct_ms)
        all_gateway.extend(gateway_ms)
        cases.append(
            {
                'name': case.name,
                'tool': case.tool,
                'match': match,
                'direct_ms': median(direct_ms),
                'gateway_ms': median(gateway_ms),
            }
        )
    unsigned_result, _ = await timed_call(unsigned, CASES[0], 'unsigned', clock)
    unsigned_refused = not unsigned_result.ok
    return {
        'discovered': discovered,
        'cases': cases,
        'unsigned_refused': unsigned_refused,
        'median_ms': {'direct': median(all_direct), 'gateway': median(all_gateway)},
        'passed': (
            discovered['direct'] == discovered['gateway'] == list(TOOLS)
            and all(case['match'] for case in cases)
            and unsigned_refused
        ),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--direct-url', required=True)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        settings = ChatSettings.from_environment()
        direct_url = mcp_url({'PREFLIGHT_MCP_URL': args.direct_url})
        direct = McpTools(direct_url, read_mcp_token(os.environ))
        gateway = build_tool_service(settings)
        unsigned = McpTools(settings.mcp_url, None, tool_prefix=settings.mcp_tool_prefix)
        report = asyncio.run(run_check(direct, gateway, unsigned, repeats=args.repeats))
    except Exception as error:
        # Configuration and transport exceptions may contain credentials or URLs: report only the exception types.
        report = {
            'error': ','.join(sorted(leaf_exception_types(error))),
            'discovered': {'direct': [], 'gateway': []},
            'cases': [],
            'unsigned_refused': False,
            'median_ms': {'direct': None, 'gateway': None},
            'passed': False,
        }
    text = json.dumps(report) + '\n'
    if args.output is not None:
        args.output.write_text(text, encoding='utf-8')
    sys.stdout.write(text)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
