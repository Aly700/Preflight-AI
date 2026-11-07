#!/usr/bin/env python3
"""Generate original fictional Week 1 records using only the standard library.

The config is the only input file. No existing records or external sources are
read. CSV content depends only on that config and the seed; only the manifest's
generation timestamp uses the wall clock.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = "1.0.0"
ROOT = Path(__file__).resolve().parents[1]
FIELDS = {
    "aircraft": "aircraft_id program stage station record_created_at".split(),
    "issues": "issue_id aircraft_id title description subsystem priority status opened_at closed_at".split(),
    "tasks": "task_id issue_id task_kind status assigned_team_id assigned_person_id created_at completed_at".split(),
    "activity_events": "event_id issue_id task_id actor_id event_type occurred_at note".split(),
    "teams": "team_id team_name".split(),
    "people": "person_id display_name team_id".split(),
    "shifts": "shift_id person_id team_id shift_start_at shift_end_at".split(),
}
PREFIXES = dict(zip(FIELDS, (
    "DEMO-AIR-", "ISSUE-", "TASK-", "EVT-", "TEAM-", "PERSON-", "SHIFT-",
)))
ENUMS = {
    "issue_statuses": {"open", "in_progress", "blocked", "reopened", "closed"},
    "task_statuses": {"todo", "in_progress", "awaiting_review", "completed", "blocked"},
    "event_types": {
        "issue_opened", "issue_closed", "issue_reopened", "task_created",
        "task_assigned", "task_started", "task_completed", "task_review_requested",
        "task_blocked", "task_unblocked", "note_added",
    },
}
# Repeated workload profiles guarantee coverage without relying on chance.
PROFILES = (
    ("open", "completed"), ("in_progress", "review"), ("blocked", "blocked"),
    ("open", "todo"), ("reopened", "reopened"), ("in_progress", "active"),
    ("open", "todo"), ("closed", "completed"), ("in_progress", "active"),
    ("open", "todo"),
)
NOTES = {
    "issue_opened": "Fictional tracking item opened for demonstration.",
    "issue_closed": "Fictional tracking item administratively closed; no technical judgment.",
    "issue_reopened": "Fictional tracking item reopened for an additional record question.",
    "task_created": "Fictional work item added to the queue.",
    "task_assigned": "Fictional work item allocated to its recorded team and worker.",
    "task_started": "Fictional work item moved into progress.",
    "task_completed": "Fictional work item marked complete; issue status is independent.",
    "task_review_requested": "Fictional work item awaiting record review.",
    "task_blocked": "Fictional work item waiting for supporting information.",
    "task_unblocked": "Fictional information dependency cleared.",
    "note_added": "Fictional queue context recorded for demonstration.",
}


def utc(text: str) -> datetime:
    value = datetime.fromisoformat(text)
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("History bounds must be timezone-aware ISO 8601 UTC")
    return value


def validate_spec(spec: dict) -> tuple[datetime, datetime]:
    if spec["phase"] != "week1_structured_data":
        raise ValueError("Only week1_structured_data is supported")
    counts = spec["counts"]
    if set(counts) != set(FIELDS) or any(type(n) is not int or n <= 0 for n in counts.values()):
        raise ValueError("counts must contain exactly the seven tables with positive integer sizes")
    if spec["identifiers"] != PREFIXES:
        raise ValueError("Use only the Week 1 fictional ID prefixes")
    for name, allowed in ENUMS.items():
        if set(spec[name]) != allowed or len(spec[name]) != len(allowed):
            raise ValueError(f"{name} must contain the Week 1 contract values exactly once")
    for name in ("priorities", "subsystems", "task_kinds", "programs", "stages", "stations"):
        values = spec[name]
        if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v for v in values):
            raise ValueError(f"{name} must be a nonempty list of strings")
    if counts["issues"] < max(len(PROFILES), len(spec["priorities"]), len(spec["subsystems"])):
        raise ValueError("Not enough issues for the required lifecycle and filter coverage")
    if counts["tasks"] < 2 * counts["issues"]:
        raise ValueError("At least two tasks per issue are needed for the lifecycle profiles")
    if counts["people"] < counts["teams"]:
        raise ValueError("Each team needs at least one fictional worker")
    start, end = utc(spec["date_start_utc"]), utc(spec["date_end_utc"])
    if end - start < timedelta(days=15):
        raise ValueError("History range must span at least 15 days for the lifecycle profiles")
    return start, end


def balanced(values: list, count: int, rng: random.Random) -> list:
    result = [values[i % len(values)] for i in range(count)]
    rng.shuffle(result)
    return result


def generate(spec: dict, seed: int) -> dict[str, list[dict]]:
    start, end = validate_spec(spec)
    rng = random.Random(seed)
    counts = spec["counts"]
    rows = {name: [] for name in FIELDS}

    def identifier(table: str, index: int) -> str:
        width = max(4, len(str(counts[table])))
        return f"{spec['identifiers'][table]}{index + 1:0{width}d}"

    def event(issue_id: str, kind: str, when: datetime, task: dict | None = None) -> None:
        rows["activity_events"].append({
            "issue_id": issue_id, "task_id": task["task_id"] if task else "",
            "actor_id": task["assigned_person_id"] if task else "",
            "event_type": kind, "occurred_at": when.isoformat(), "note": NOTES[kind],
        })

    for i in range(counts["teams"]):
        rows["teams"].append({"team_id": identifier("teams", i), "team_name": f"Demo Team {i + 1:02d}"})
    members = {team["team_id"]: [] for team in rows["teams"]}
    for i in range(counts["people"]):
        team_id = rows["teams"][i % counts["teams"]]["team_id"]
        person = {"person_id": identifier("people", i), "display_name": f"Demo Worker {i + 1:04d}", "team_id": team_id}
        rows["people"].append(person)
        members[team_id].append(person["person_id"])
    for i in range(counts["aircraft"]):
        rows["aircraft"].append({
            "aircraft_id": identifier("aircraft", i),
            "program": spec["programs"][i % len(spec["programs"])],
            "stage": spec["stages"][i % len(spec["stages"])],
            "station": spec["stations"][(i // len(spec["stages"])) % len(spec["stations"])],
            "record_created_at": start.isoformat(),
        })

    profiles = balanced(list(PROFILES), counts["issues"], rng)
    priorities = balanced(spec["priorities"], counts["issues"], rng)
    subsystems = balanced(spec["subsystems"], counts["issues"], rng)
    aircraft_ids = balanced([r["aircraft_id"] for r in rows["aircraft"]], counts["issues"], rng)
    task_counts = [2] * counts["issues"]
    extra_order = list(range(counts["issues"]))
    rng.shuffle(extra_order)
    for i in range(counts["tasks"] - 2 * counts["issues"]):
        task_counts[extra_order[i % len(extra_order)]] += 1
    kinds = balanced(spec["task_kinds"], counts["tasks"], rng)
    # Reserve 14 days so even reopened histories stay inside the configured range.
    opening_span = int((end - start - timedelta(days=14)).total_seconds())
    for i, (status, profile) in enumerate(profiles):
        issue_id = identifier("issues", i)
        opened = start + timedelta(seconds=rng.randint(0, opening_span))
        closed = opened + timedelta(days=8)
        reopened = opened + timedelta(days=10)
        rows["issues"].append({
            "issue_id": issue_id, "aircraft_id": aircraft_ids[i],
            "title": f"Fictional {subsystems[i]} record question {i + 1:04d}",
            "description": "Invented queue item for record coordination; no physical work instructions or technical judgment.",
            "subsystem": subsystems[i], "priority": priorities[i], "status": status,
            "opened_at": opened.isoformat(), "closed_at": closed.isoformat() if status == "closed" else "",
        })
        event(issue_id, "issue_opened", opened)
        if status in ("closed", "reopened"):
            event(issue_id, "issue_closed", closed)
        if status == "reopened":
            event(issue_id, "issue_reopened", reopened)
        for j in range(task_counts[i]):
            task_index = len(rows["tasks"])
            team_id = rows["teams"][(i + j) % counts["teams"]]["team_id"]
            task_status = "todo"
            if status == "closed" or (j == 0 and profile in ("completed", "reopened")):
                task_status = "completed"
            elif j == 0 and status == "blocked":
                task_status = "blocked"
            elif j == 0 and status == "in_progress":
                task_status = "in_progress"
            elif j == 1 and profile == "review":
                task_status = "awaiting_review"
            base = reopened if status == "reopened" and j > 0 else opened
            created = base + timedelta(hours=rng.randint(1, 48))
            completed = created + timedelta(days=rng.randint(2, 4))
            # Only pending tasks can be team-only assignments.
            person_id = "" if task_status == "todo" and rng.randrange(4) == 0 else rng.choice(members[team_id])
            task = {
                "task_id": identifier("tasks", task_index), "issue_id": issue_id,
                "task_kind": kinds[task_index], "status": task_status,
                "assigned_team_id": team_id, "assigned_person_id": person_id,
                "created_at": created.isoformat(),
                "completed_at": completed.isoformat() if task_status == "completed" else "",
            }
            rows["tasks"].append(task)
            event(issue_id, "task_created", created, task)
            transition = {
                "completed": ("task_completed", completed),
                "in_progress": ("task_started", created + timedelta(hours=36)),
                "awaiting_review": ("task_review_requested", created + timedelta(days=2)),
                "blocked": ("task_blocked", created + timedelta(days=1)),
            }.get(task_status)
            if transition:
                event(issue_id, *transition, task)

    # Show a resolved information block before an active task started.
    active = next(t for t in rows["tasks"] if t["status"] == "in_progress")
    created = utc(active["created_at"])
    event(active["issue_id"], "task_blocked", created + timedelta(hours=12), active)
    event(active["issue_id"], "task_unblocked", created + timedelta(hours=24), active)
    remaining = counts["activity_events"] - len(rows["activity_events"])
    if remaining < 2:
        raise ValueError(f"activity_events needs at least {len(rows['activity_events']) + 2} rows for these lifecycle profiles")
    # The event table is a compact history: every creation and final transition,
    # plus selected assignment/context events, rather than a complete audit log.
    assigned = [t for t in rows["tasks"] if t["assigned_person_id"]]
    rng.shuffle(assigned)
    for task in assigned[:min(remaining // 2, len(assigned))]:
        event(task["issue_id"], "task_assigned", utc(task["created_at"]) + timedelta(minutes=30), task)
    remaining = counts["activity_events"] - len(rows["activity_events"])
    for i in range(remaining):
        issue = rows["issues"][i % counts["issues"]]
        event(issue["issue_id"], "note_added", utc(issue["opened_at"]) + timedelta(minutes=15 + i // counts["issues"]))
    rows["activity_events"].sort(key=lambda r: (r["occurred_at"], r["issue_id"], r["task_id"], r["event_type"]))
    for i, record in enumerate(rows["activity_events"]):
        record["event_id"] = identifier("activity_events", i)

    # Divide the history into disjoint eight-hour slot groups. A worker gets at
    # most one shift per group, preventing overlapping assignments for them.
    shift_rounds = (counts["shifts"] + counts["people"] - 1) // counts["people"]
    slots = int((end - start).total_seconds() // (8 * 3600)) + 1
    if shift_rounds > slots:
        raise ValueError("Too many shifts for non-overlapping eight-hour assignments")
    people_order = rows["people"].copy()
    rng.shuffle(people_order)
    for i in range(counts["shifts"]):
        person = people_order[i % counts["people"]]
        round_index = i // counts["people"]
        first_slot = round_index * slots // shift_rounds
        next_slot = (round_index + 1) * slots // shift_rounds
        began = start + timedelta(hours=8 * rng.randrange(first_slot, next_slot))
        rows["shifts"].append({
            "shift_id": identifier("shifts", i), "person_id": person["person_id"],
            "team_id": person["team_id"], "shift_start_at": began.isoformat(),
            "shift_end_at": (began + timedelta(hours=8)).isoformat(),
        })
    for table, records in rows.items():
        if len(records) != counts[table]:
            raise ValueError(f"Generated count for {table} differs from the requested count")
        records.sort(key=lambda r: r[FIELDS[table][0]])
    return rows


def write_output(output: Path, rows: dict, spec: dict, config_bytes: bytes, seed: int) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    files = {}
    for table, records in rows.items():
        filename = f"{table}.csv"
        path = output / filename
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS[table], lineterminator="\n")
            writer.writeheader()
            writer.writerows(records)
        files[filename] = {"rows": len(records), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    manifest = {
        "phase": spec["phase"], "seed": seed,
        "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "generator_version": VERSION, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "counts": {table: len(records) for table, records in rows.items()}, "files": files,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "synthetic_spec.json")
    parser.add_argument("--seed", type=int, help="Override the seed in the config")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "synthetic")
    args = parser.parse_args()
    try:
        config_bytes = args.config.read_bytes()
        spec = json.loads(config_bytes)
        seed = spec["seed"] if args.seed is None else args.seed
        if type(seed) is not int:
            raise ValueError("seed must be an integer")
        rows = generate(spec, seed)
        manifest = write_output(args.output, rows, spec, config_bytes, seed)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps({"seed": seed, "counts": manifest["counts"], "output": str(args.output)}, sort_keys=True))


if __name__ == "__main__":
    main()
