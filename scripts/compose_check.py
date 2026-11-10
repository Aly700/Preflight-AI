#!/usr/bin/env python3
"""Check the running local stack: health, loaded rows, eight tools against SQL, and optionally one answer.

Standard library only. Without `--ask` no chat model is called: the checks are
container health, row counts from an independent SQL query, the chat status
route, and the eight MCP tools called through their real HTTP transport and
authentication, compared with SQL. `--ask` sends one fixed question through the
web entry point and compares the answer's tool evidence with the same SQL facts.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable, Iterator
import http.client
import json
from pathlib import Path
import subprocess
import sys
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
PROJECT = 'preflight-oss'
QUESTION = 'List the first five unresolved issues and the total unresolved count.'
TOTAL_ROWS = 1784
AIRCRAFT = 'DEMO-AIR-0001'
ISSUE = 'ISSUE-0001'

FACTS_SQL = f"""
SELECT json_build_object(
  'counts', json_build_object(
    'aircraft', (SELECT count(*) FROM preflight.aircraft),
    'issues', (SELECT count(*) FROM preflight.issues),
    'tasks', (SELECT count(*) FROM preflight.tasks),
    'activity_events', (SELECT count(*) FROM preflight.activity_events),
    'teams', (SELECT count(*) FROM preflight.teams),
    'people', (SELECT count(*) FROM preflight.people),
    'shifts', (SELECT count(*) FROM preflight.shifts)),
  'document_counts', json_build_object(
    'documents', (SELECT count(*) FROM preflight.documents),
    'passages', (SELECT count(*) FROM preflight.passages)),
  'passage_ids', (SELECT coalesce(json_agg(passage_id ORDER BY passage_id), '[]'::json) FROM preflight.passages),
  'unresolved_total', (SELECT count(*) FROM preflight.issues WHERE status <> 'closed'),
  'first_unresolved', (SELECT coalesce(json_agg(issue_id ORDER BY issue_id), '[]'::json) FROM
    (SELECT issue_id FROM preflight.issues WHERE status <> 'closed' ORDER BY issue_id LIMIT 5) first_five),
  'aircraft_0001', (SELECT json_build_object(
    'issue_count', count(*),
    'unresolved_issue_count', count(*) FILTER (WHERE status <> 'closed'),
    'issue_status_counts', json_build_object(
      'open', count(*) FILTER (WHERE status = 'open'),
      'in_progress', count(*) FILTER (WHERE status = 'in_progress'),
      'blocked', count(*) FILTER (WHERE status = 'blocked'),
      'reopened', count(*) FILTER (WHERE status = 'reopened'),
      'closed', count(*) FILTER (WHERE status = 'closed')),
    'task_count', (SELECT count(*) FROM preflight.tasks t JOIN preflight.issues i USING (issue_id)
                   WHERE i.aircraft_id = '{AIRCRAFT}'),
    'task_status_counts', (SELECT json_build_object(
      'todo', count(*) FILTER (WHERE t.status = 'todo'),
      'in_progress', count(*) FILTER (WHERE t.status = 'in_progress'),
      'awaiting_review', count(*) FILTER (WHERE t.status = 'awaiting_review'),
      'completed', count(*) FILTER (WHERE t.status = 'completed'),
      'blocked', count(*) FILTER (WHERE t.status = 'blocked'))
      FROM preflight.tasks t JOIN preflight.issues i USING (issue_id) WHERE i.aircraft_id = '{AIRCRAFT}'))
    FROM preflight.issues WHERE aircraft_id = '{AIRCRAFT}'),
  'issue_0001_events', (SELECT count(*) FROM preflight.activity_events WHERE issue_id = '{ISSUE}'),
  'unresolved_by_subsystem', (SELECT coalesce(json_object_agg(subsystem, n ORDER BY subsystem), '{{}}'::json) FROM
    (SELECT subsystem, count(*) AS n FROM preflight.issues WHERE status <> 'closed' GROUP BY subsystem) by_subsystem),
  'dated_unresolved_by_subsystem', (SELECT coalesce(json_object_agg(subsystem, n ORDER BY subsystem), '{{}}'::json) FROM
    (SELECT subsystem, count(*) AS n FROM preflight.issues WHERE status <> 'closed'
     AND opened_at >= '2026-03-01T00:00:00Z' AND opened_at <= '2026-04-01T00:00:00Z'
     GROUP BY subsystem) dated_by_subsystem),
  'aircraft_0002', (SELECT json_build_object(
    'issue_count', count(*),
    'unresolved_issue_count', count(*) FILTER (WHERE status <> 'closed'))
    FROM preflight.issues WHERE aircraft_id = 'DEMO-AIR-0002'),
  'review_tasks_total', (SELECT count(*) FROM preflight.tasks
    WHERE status = 'awaiting_review' AND assigned_team_id = 'TEAM-0001'),
  'first_review_tasks', (SELECT coalesce(json_agg(task_id ORDER BY task_id), '[]'::json) FROM
    (SELECT task_id FROM preflight.tasks WHERE status = 'awaiting_review' AND assigned_team_id = 'TEAM-0001'
     ORDER BY task_id LIMIT 5) first_five_tasks),
  'closed_total', (SELECT count(*) FROM preflight.issues WHERE status = 'closed'),
  'first_closed', (SELECT coalesce(json_agg(issue_id ORDER BY issue_id), '[]'::json) FROM
    (SELECT issue_id FROM preflight.issues WHERE status = 'closed' ORDER BY issue_id LIMIT 5) first_five)
);
"""

# Runs inside the MCP container: all tools through the real HTTP transport and bearer authentication.
TOOL_CLIENT = r"""
import json, sys, urllib.request
token = open('/run/secrets/mcp-token').read().strip()
headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
           'Authorization': 'Bearer ' + token}
def rpc(method, params, ident):
    body = {'jsonrpc': '2.0', 'method': method, 'params': params}
    if ident is not None:
        body['id'] = ident
    request = urllib.request.Request('http://localhost:8001/mcp', data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read()
    return json.loads(raw) if raw else None
def call(name, arguments, ident):
    reply = rpc('tools/call', {'name': name, 'arguments': arguments}, ident)
    if 'error' in reply:
        message = str(reply['error'])
        if 'initializ' in message.lower():
            rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                               'clientInfo': {'name': 'compose-check', 'version': '0'}}, 100 + ident)
            rpc('notifications/initialized', {}, None)
            reply = rpc('tools/call', {'name': name, 'arguments': arguments}, ident)
        if 'error' in reply:
            raise SystemExit('tool call failed: ' + name + ': ' + str(reply['error']))
    result = reply['result']
    if result.get('isError'):
        raise SystemExit('tool error: ' + name + ': ' + json.dumps(result.get('content')))
    return result['structuredContent']
print(json.dumps({
    'get_aircraft_summary': call('get_aircraft_summary', {'aircraft_id': 'DEMO-AIR-0001'}, 1),
    'list_open_issues': call('list_open_issues', {'limit': 5}, 2),
    'get_issue_history': call('get_issue_history', {'issue_id': 'ISSUE-0001', 'limit': 1}, 3),
    'count_issues': call('count_issues', {'group_by': 'subsystem', 'unresolved_only': True}, 4),
    'count_issues_date_window': call('count_issues', {'group_by': 'subsystem', 'unresolved_only': True,
        'opened_after': '2026-03-01T00:00:00Z', 'opened_before': '2026-04-01T00:00:00Z'}, 9),
    'compare_aircraft': call('compare_aircraft', {'aircraft_ids': ['DEMO-AIR-0001', 'DEMO-AIR-0002']}, 5),
    'search_issues': call('search_issues', {'status': 'closed', 'limit': 5}, 6),
    'search_documents': call('search_documents', {'query': 'When does the Friday queue sweep start?', 'top_k': 3}, 7),
    'list_tasks': call('list_tasks', {'status': 'awaiting_review', 'assigned_team_id': 'TEAM-0001', 'limit': 5}, 8),
}))
"""


def run(command: list[str], *, timeout: float = 120) -> str:
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, cwd=ROOT)
    if result.returncode:
        raise RuntimeError(f'{command[0]} {command[1]} failed: {result.stderr.strip()[:400]}')
    return result.stdout


def service_health(project: str = PROJECT) -> dict[str, str]:
    """Each service's health (or state when it has no health check) from `docker compose ps`."""
    health: dict[str, str] = {}
    output = run(['docker', 'compose', '-p', project, 'ps', '--all', '--format', 'json']).strip()
    entries = (
        json.loads(output)
        if output.startswith('[')
        else [json.loads(line) for line in output.splitlines() if line.strip()]
    )
    for entry in entries:
        health[entry['Service']] = entry.get('Health') or entry.get('State') or 'unknown'
    return health


def sql_facts(container: str, database: str) -> dict[str, Any]:
    """Independent facts from one SQL query run with psql inside the database container."""
    command = [
        'docker',
        'exec',
        '-i',
        container,
        'psql',
        '-X',
        '-At',
        '-v',
        'ON_ERROR_STOP=1',
        '-U',
        'postgres',
        '-d',
        database,
    ]
    result = subprocess.run(command, input=FACTS_SQL, capture_output=True, text=True, timeout=60, cwd=ROOT)
    if result.returncode:
        raise RuntimeError('SQL facts query failed: ' + result.stderr.strip()[:400])
    facts: dict[str, Any] = json.loads(result.stdout.strip())
    return facts


def tool_results(service: str = 'preflight-oss-mcp', project: str = PROJECT) -> dict[str, Any]:
    """All tools called inside the MCP container through its own HTTP transport."""
    output = run(['docker', 'compose', '-p', project, 'exec', '-T', service, 'python', '-c', TOOL_CLIENT])
    results: dict[str, Any] = json.loads(output)
    return results


def chat_status(base: str) -> dict[str, Any]:
    with urlopen(Request(base + '/api/chat/status'), timeout=30) as response:
        status: dict[str, Any] = json.load(response)
        return status


def parse_sse(stream: Iterable[bytes]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield (event, data) pairs from a server-sent-event byte stream; stop after `done` or `error`."""
    name = ''
    data: list[str] = []
    for raw in stream:
        line = raw.decode('utf-8').rstrip('\r\n')
        if line.startswith('event:'):
            name = line[6:].strip()
        elif line.startswith('data:'):
            data.append(line[5:].strip())
        elif line == '':
            if name:
                yield name, json.loads('\n'.join(data) or '{}')
                if name in ('done', 'error'):
                    return
            name, data = '', []


def ask(
    base: str,
    question: str,
    *,
    timeout: float = 150,
    history: list[dict[str, str]] | None = None,
    scope: dict[str, str] | None = None,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
    token: str | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """One question through the web entry point; a socket silent for `timeout` seconds raises TimeoutError.

    `token` is the chat access key, needed only when calling the API directly (on a host
    without the web proxy); it is sent as a bearer header and never printed.
    """
    parts = urlsplit(base)
    connection = http.client.HTTPConnection(parts.hostname or '127.0.0.1', parts.port or 80, timeout=timeout)
    body = json.dumps(
        {'question': question, 'history': history or [], **({'scope': scope} if scope is not None else {})}
    )
    try:
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        connection.request('POST', '/api/chat', body=body, headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f'chat answered {response.status}: {response.read(400).decode("utf-8", "replace")}')
        events = []
        for name, data in parse_sse(response):
            if on_event is not None:
                on_event(name, data)
            events.append((name, data))
        return events
    finally:
        connection.close()


def compare_summary(result: dict[str, Any], facts: dict[str, Any], tool: str) -> list[str]:
    expected = facts['aircraft_0001']
    mismatches = []
    for key in ('issue_count', 'unresolved_issue_count', 'issue_status_counts', 'task_count', 'task_status_counts'):
        if result.get(key) != expected[key]:
            mismatches.append(f'{tool}: {key} {result.get(key)!r} differs from SQL {expected[key]!r}')
    return mismatches


def compare_page(result: dict[str, Any], facts: dict[str, Any], tool: str) -> list[str]:
    mismatches = []
    if result.get('total') != facts['unresolved_total']:
        mismatches.append(f'{tool}: total {result.get("total")!r} differs from SQL {facts["unresolved_total"]!r}')
    first = [item.get('issue_id') for item in result.get('items', [])[:5]]
    if first != facts['first_unresolved']:
        mismatches.append(f'{tool}: first issues {first!r} differ from SQL {facts["first_unresolved"]!r}')
    return mismatches


def compare_evidence(events: Iterable[tuple[str, dict[str, Any]]], facts: dict[str, Any]) -> list[str]:
    """Mismatches between the answer's evidence event and the SQL facts; an error event is a mismatch."""
    mismatches: list[str] = []
    for name, data in events:
        if name == 'error':
            mismatches.append('answer ended with error: ' + str(data.get('message')))
        if name != 'evidence':
            continue
        for entry in data.get('results', []):
            tool, result = entry.get('tool'), entry.get('result') or {}
            if tool == 'list_open_issues':
                mismatches += compare_page(result, facts, tool)
            elif tool == 'get_aircraft_summary' and result.get('aircraft', {}).get('aircraft_id') == AIRCRAFT:
                mismatches += compare_summary(result, facts, tool)
    return mismatches


def compare_tools(results: dict[str, Any], facts: dict[str, Any]) -> list[str]:
    """Mismatches between direct tool calls and the SQL facts; a missing tool is a mismatch."""
    mismatches: list[str] = []
    for tool in (
        'get_aircraft_summary',
        'list_open_issues',
        'get_issue_history',
        'count_issues',
        'count_issues_date_window',
        'compare_aircraft',
        'search_issues',
        'search_documents',
        'list_tasks',
    ):
        if tool not in results:
            mismatches.append(f'{tool}: no result')
    if 'get_aircraft_summary' in results:
        mismatches += compare_summary(results['get_aircraft_summary'], facts, 'get_aircraft_summary')
    if 'list_open_issues' in results:
        mismatches += compare_page(results['list_open_issues'], facts, 'list_open_issues')
    if 'get_issue_history' in results:
        history = results['get_issue_history']
        if history.get('issue', {}).get('issue_id') != ISSUE:
            mismatches.append(f'get_issue_history: issue {history.get("issue")!r} is not {ISSUE}')
        if history.get('total_events') != facts['issue_0001_events']:
            observed, expected = history.get('total_events'), facts['issue_0001_events']
            mismatches.append(f'get_issue_history: total_events {observed!r} differs from SQL {expected!r}')
    for tool, fact in (
        ('count_issues', 'unresolved_by_subsystem'),
        ('count_issues_date_window', 'dated_unresolved_by_subsystem'),
    ):
        if tool in results:
            counts = results[tool]
            groups = {g.get('key'): g.get('count') for g in counts.get('groups', [])}
            total = facts['unresolved_total'] if tool == 'count_issues' else sum(facts[fact].values())
            if groups != facts[fact] or counts.get('total') != total:
                mismatches.append(f'{tool}: {groups!r} (total {counts.get("total")!r}) differs from SQL')
    if 'compare_aircraft' in results:
        comparison = results['compare_aircraft']
        summaries = comparison.get('summaries', [])
        aircraft_ids = [AIRCRAFT, 'DEMO-AIR-0002']
        wanted = [facts['aircraft_0001'], facts['aircraft_0002']]
        if [s.get('aircraft', {}).get('aircraft_id') for s in summaries] != aircraft_ids or any(
            s.get(k) != w[k]
            for s, w in zip(summaries, wanted, strict=False)
            for k in ('issue_count', 'unresolved_issue_count')
        ):
            mismatches.append('compare_aircraft: summaries differ from SQL')
        ranking = sorted(
            [
                {
                    'aircraft_id': aircraft_id,
                    'unresolved_issue_count': fact['unresolved_issue_count'],
                    'issue_count': fact['issue_count'],
                }
                for aircraft_id, fact in zip(aircraft_ids, wanted, strict=True)
            ],
            key=lambda entry: (-entry['unresolved_issue_count'], entry['aircraft_id']),
        )
        if comparison.get('ranked_by_unresolved') != ranking:
            mismatches.append('compare_aircraft: ranking differs from SQL')
        spread = abs(
            facts['aircraft_0001']['unresolved_issue_count'] - facts['aircraft_0002']['unresolved_issue_count']
        )
        if comparison.get('unresolved_spread') != spread:
            spread_seen = comparison.get('unresolved_spread')
            mismatches.append(f'compare_aircraft: unresolved_spread {spread_seen!r} differs from SQL {spread!r}')
    if 'search_issues' in results:
        closed = results['search_issues']
        items = closed.get('items', [])
        if (
            closed.get('limit') != 5
            or closed.get('offset') != 0
            or closed.get('total') != facts['closed_total']
            or [i.get('issue_id') for i in items] != facts['first_closed']
            or any(i.get('status') != 'closed' for i in items)
        ):
            mismatches.append('search_issues: page differs from SQL or an item is not closed')
    if 'list_tasks' in results:
        tasks = results['list_tasks']
        items = tasks.get('items', [])
        if (
            tasks.get('limit') != 5
            or tasks.get('offset') != 0
            or tasks.get('total') != facts['review_tasks_total']
            or [item.get('task_id') for item in items] != facts['first_review_tasks']
            or any(
                item.get('status') != 'awaiting_review' or item.get('assigned_team_id') != 'TEAM-0001' for item in items
            )
        ):
            mismatches.append('list_tasks: page differs from SQL or status/team filters')
    if 'search_documents' in results:
        passages = results['search_documents'].get('results', [])
        if not any(p.get('doc_id') == 'DOC-0008' for p in passages[:3]):
            mismatches.append('search_documents: expected a DOC-0008 passage in the top three')
        known = set(facts['passage_ids'])
        for passage in passages:
            if passage.get('passage_id') not in known:
                mismatches.append(f'search_documents: passage {passage.get("passage_id")!r} is absent from SQL')
    return mismatches


def answer_mentions(events: Iterable[tuple[str, dict[str, Any]]], facts: dict[str, Any]) -> bool:
    text = ''.join(str(data.get('text', '')) for name, data in events if name == 'delta')
    return str(facts['unresolved_total']) in text


def compare_document_counts(
    counts: dict[str, int], passages: Path = ROOT / 'data/documents/passages.jsonl'
) -> list[str]:
    rows = [json.loads(line) for line in passages.read_text(encoding='utf-8').splitlines()]
    if not rows:
        raise ValueError('Passage fixture is empty')
    expected = {'documents': len({row['doc_id'] for row in rows}), 'passages': len(rows)}
    return [
        f'{name} count {counts.get(name)!r} differs from passage fixture {count}'
        for name, count in expected.items()
        if counts.get(name) != count
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='http://127.0.0.1:8080')
    parser.add_argument('--project', default=PROJECT)
    parser.add_argument('--container', default='preflight-oss-postgres')
    parser.add_argument('--database', default='preflight_oss')
    parser.add_argument('--ask', action='store_true', help='Send one real question (uses the configured model)')
    parser.add_argument('--output', type=Path, help='Write the JSON report here as well')
    args = parser.parse_args()
    failures: list[str] = []
    report: dict[str, Any] = {'question': QUESTION if args.ask else None}
    try:
        report['services'] = service_health(args.project)
        unhealthy = {name: state for name, state in report['services'].items() if state != 'healthy'}
        if unhealthy:
            failures.append(f'services not healthy: {unhealthy}')
        facts = sql_facts(args.container, args.database)
        report['counts'] = facts['counts']
        report['total_rows'] = sum(facts['counts'].values())
        if report['total_rows'] != TOTAL_ROWS:
            failures.append(f'total rows {report["total_rows"]} differ from {TOTAL_ROWS}')
        report['document_counts'] = facts['document_counts']
        failures += compare_document_counts(facts['document_counts'])
        report['tool_mismatches'] = compare_tools(tool_results(project=args.project), facts)
        failures += report['tool_mismatches']
        report['status'] = chat_status(args.base)
        if args.ask:
            events = ask(args.base, QUESTION)
            counts: dict[str, int] = {}
            for name, _ in events:
                counts[name] = counts.get(name, 0) + 1
            report['events'] = counts
            report['answer_excerpt'] = ''.join(str(d.get('text', '')) for n, d in events if n == 'delta')[:400]
            report['mismatches'] = compare_evidence(events, facts)
            report['mentions_total'] = answer_mentions(events, facts)
            failures += report['mismatches']
            if not report['mentions_total']:
                failures.append('answer does not mention the unresolved total')
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired, TimeoutError) as error:
        failures.append(f'{type(error).__name__}: {error}')
    report['failures'] = failures
    report['result'] = 'passed' if not failures else 'failed'
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        args.output.write_text(rendered + '\n', encoding='utf-8')
    sys.exit(0 if not failures else 1)


if __name__ == '__main__':
    main()
