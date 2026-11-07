# Week 1 synthetic structured-data contract

**Status:** generated and validated November 2, 2026; PostgreSQL load verified November 3.  
**Scope:** aircraft, issues, tasks, activity events, teams, people, shifts.  
**Deferred to Week 2:** NCR documents, NCR index, document retrieval, embeddings, vector search.

This is an independent fictional schema, not an employer schema or a copy of an operational export. Its purpose is to support first-week API, dashboard, and future read-only MCP tool development.

## Files and columns

CSV encoding: UTF-8, header row, deterministic sort order, blank for optional fields, timezone-aware ISO 8601 UTC timestamps.

| File | Primary key | Required columns |
|---|---|---|
| `aircraft.csv` | `aircraft_id` | `aircraft_id,program,stage,station,record_created_at` |
| `issues.csv` | `issue_id` | `issue_id,aircraft_id,title,description,subsystem,priority,status,opened_at,closed_at` |
| `tasks.csv` | `task_id` | `task_id,issue_id,task_kind,status,assigned_team_id,assigned_person_id,created_at,completed_at` |
| `activity_events.csv` | `event_id` | `event_id,issue_id,task_id,actor_id,event_type,occurred_at,note` |
| `teams.csv` | `team_id` | `team_id,team_name` |
| `people.csv` | `person_id` | `person_id,display_name,team_id` |
| `shifts.csv` | `shift_id` | `shift_id,person_id,team_id,shift_start_at,shift_end_at` |

All IDs have strictly fictional prefixes from the JSON spec. A team can have many people, tasks and shifts. A task belongs to exactly one issue. An activity event belongs to an issue and may refer to a task or an actor; if it has a task, the issue IDs must match.

## Current-status rules

- **Issue** status: `open`, `in_progress`, `blocked`, `reopened`, `closed`.
- **Task** status: `todo`, `in_progress`, `awaiting_review`, `completed`, `blocked`.
- An issue's `closed_at` is filled only when its **current** status is `closed`.
- A task's `completed_at` is filled only when its **current** status is `completed`.
- A completed task **does not** close its issue automatically.
- A `reopened` issue must have an `issue_closed` event followed in time by an `issue_reopened` event, and `closed_at` must now be empty.
- A `blocked` issue must have at least one unfinished associated task.
- An `awaiting_review` task has an empty completion timestamp.
- Event types are constrained to the configured `event_types` array.
- Times must lie inside the spec's fictional history range, except that a shift may end shortly after the range's final date if it starts before it.
- Every present person assignment to a task must belong to that task's assigned team. Shift team must match the person's team.

## Plausible fictional coverage

- Mix priorities (`low`, `medium`, `high`) and all six configured subsystems.
- Include multiple tasks and activity events per issue and repeatable workload patterns.
- Provide enough high-priority, blocked, reopened and awaiting-review cases to exercise queries.
- Keep descriptions short, generic and non-actionable; no accurate aircraft maintenance procedures, real aircraft serials or real employee names.

## Queries this dataset should support

1. Count currently unresolved issues by aircraft, priority and subsystem (unresolved = anything other than `closed`).
2. List high-priority issues that are currently `blocked` or `reopened`.
3. Display an issue's ordered task/activity history.
4. Compare task completion counts to closed issue counts (not equal by design).
5. Summarize team workload and assigned workers/shifts.

## Reproducibility and provenance

`scripts/generate_synthetic.py` must accept `--config`, `--seed`, `--output`. The same seed and config produce byte-identical CSVs. `manifest.json` contains the seed, spec SHA-256, generator version, current UTC generation time, exact row counts and per-file SHA-256 hashes. Record actual commands and test outcomes in `evidence/validation-summary.md`.

Required manifest shape (example only; placeholder hash strings are not real outputs):

```json
{
  "phase": "week1_structured_data",
  "seed": 20261102,
  "config_sha256": "<actual SHA-256 of config/synthetic_spec.json>",
  "generator_version": "1.0.0",
  "generated_at_utc": "<actual timezone-aware UTC timestamp>",
  "counts": {"aircraft": 12, "issues": 180, "tasks": 480, "activity_events": 920, "teams": 6, "people": 36, "shifts": 150},
  "files": {
    "aircraft.csv": {"rows": 12, "sha256": "<actual SHA-256 of file>"}
  }
}
```

The `files` mapping must contain an entry for **every** generated CSV, not just `aircraft.csv` as illustrated. The timestamps in `generated_at_utc` may change between runs; deterministic CSV content must not.

**Week 2 extension boundary:** A separate task can generate fictional NCRs linked by existing `issue_id`. Do not generate those files, preemptively modify this schema for documents, or claim that RAG is implemented during Week 1.
