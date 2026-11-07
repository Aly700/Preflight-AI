"""Week 1 acceptance tests for *future* fictional operational data.

The starter deliberately has no generated CSVs or generator; missing-file
failures are expected until the generator has been run.

Run: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "synthetic"
SPEC_PATH = ROOT / "config" / "synthetic_spec.json"
SPEC = json.loads(SPEC_PATH.read_text(encoding="utf-8"))

FILES = {
    "aircraft": ("aircraft.csv", "aircraft_id"),
    "issues": ("issues.csv", "issue_id"),
    "tasks": ("tasks.csv", "task_id"),
    "activity_events": ("activity_events.csv", "event_id"),
    "teams": ("teams.csv", "team_id"),
    "people": ("people.csv", "person_id"),
    "shifts": ("shifts.csv", "shift_id"),
}

FIELDS = {
    "aircraft": "aircraft_id program stage station record_created_at".split(),
    "issues": "issue_id aircraft_id title description subsystem priority status opened_at closed_at".split(),
    "tasks": "task_id issue_id task_kind status assigned_team_id assigned_person_id created_at completed_at".split(),
    "activity_events": "event_id issue_id task_id actor_id event_type occurred_at note".split(),
    "teams": "team_id team_name".split(),
    "people": "person_id display_name team_id".split(),
    "shifts": "shift_id person_id team_id shift_start_at shift_end_at".split(),
}


def read_rows(path: Path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def timestamp(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"Expected timezone-aware ISO 8601 time: {text}")
    if moment.utcoffset() != timedelta(0):
        raise ValueError(f"Expected UTC offset +00:00: {text}")
    return moment


class Week1SyntheticContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = {}
        cls.columns = {}
        for name, (file_name, _) in FILES.items():
            path = OUTPUT / file_name
            if not path.is_file():
                raise FileNotFoundError(
                    f"Missing {path}. Run scripts/generate_synthetic.py to generate the Week 1 dataset first."
                )
            columns, records = read_rows(path)
            cls.columns[name], cls.rows[name] = columns, records
        cls.start = timestamp(SPEC["date_start_utc"])
        cls.end = timestamp(SPEC["date_end_utc"])

    def assert_time_in_range(self, value: str, *, shift_end: bool = False):
        ts = timestamp(value)
        self.assertGreaterEqual(ts, self.start)
        limit = self.end + (timedelta(days=1) if shift_end else timedelta())
        self.assertLessEqual(ts, limit)
        return ts

    def test_01_exact_counts_schemas_and_unique_fictional_ids(self):
        self.assertEqual(set(SPEC["counts"]), set(FILES))
        self.assertEqual(SPEC["phase"], "week1_structured_data")
        for name, (_, key) in FILES.items():
            with self.subTest(table=name):
                rows = self.rows[name]
                self.assertEqual(len(rows), SPEC["counts"][name])
                self.assertTrue(set(FIELDS[name]).issubset(self.columns[name]))
                ids = [row[key] for row in rows]
                self.assertTrue(all(ids))
                self.assertEqual(len(ids), len(set(ids)))
                self.assertTrue(all(x.startswith(SPEC["identifiers"][name]) for x in ids))

    def test_02_referential_integrity_and_assignments(self):
        idx = {name: {r[key]: r for r in self.rows[name]} for name, (_, key) in FILES.items()}
        for r in self.rows["aircraft"]:
            self.assertIn(r["program"], SPEC["programs"])
            self.assertIn(r["stage"], SPEC["stages"])
            self.assertIn(r["station"], SPEC["stations"])
        for r in self.rows["issues"]:
            self.assertIn(r["aircraft_id"], idx["aircraft"])
        for r in self.rows["people"]:
            self.assertIn(r["team_id"], idx["teams"])
            self.assertTrue(r["display_name"].startswith("Demo Worker "))
        for r in self.rows["tasks"]:
            self.assertIn(r["issue_id"], idx["issues"])
            self.assertIn(r["assigned_team_id"], idx["teams"])
            if r["assigned_person_id"]:
                person = idx["people"][r["assigned_person_id"]]
                self.assertEqual(person["team_id"], r["assigned_team_id"])
        for r in self.rows["activity_events"]:
            self.assertIn(r["issue_id"], idx["issues"])
            if r["task_id"]:
                self.assertEqual(idx["tasks"][r["task_id"]]["issue_id"], r["issue_id"])
            if r["actor_id"]:
                self.assertIn(r["actor_id"], idx["people"])
        for r in self.rows["shifts"]:
            self.assertIn(r["person_id"], idx["people"])
            self.assertEqual(r["team_id"], idx["people"][r["person_id"]]["team_id"])

    def test_03_status_dates_events_are_consistent(self):
        issues = {r["issue_id"]: r for r in self.rows["issues"]}
        tasks = {r["task_id"]: r for r in self.rows["tasks"]}
        for issue in issues.values():
            self.assertIn(issue["status"], SPEC["issue_statuses"])
            self.assertIn(issue["priority"], SPEC["priorities"])
            self.assertIn(issue["subsystem"], SPEC["subsystems"])
            opened = self.assert_time_in_range(issue["opened_at"])
            if issue["status"] == "closed":
                self.assertTrue(issue["closed_at"])
                self.assertGreaterEqual(self.assert_time_in_range(issue["closed_at"]), opened)
            else:
                self.assertFalse(issue["closed_at"])
        for task in tasks.values():
            self.assertIn(task["status"], SPEC["task_statuses"])
            self.assertIn(task["task_kind"], SPEC["task_kinds"])
            created = self.assert_time_in_range(task["created_at"])
            self.assertGreaterEqual(created, timestamp(issues[task["issue_id"]]["opened_at"]))
            if task["status"] == "completed":
                self.assertTrue(task["completed_at"])
                self.assertGreaterEqual(self.assert_time_in_range(task["completed_at"]), created)
            else:
                self.assertFalse(task["completed_at"])
        for event in self.rows["activity_events"]:
            self.assertIn(event["event_type"], SPEC["event_types"])
            occurred = self.assert_time_in_range(event["occurred_at"])
            self.assertGreaterEqual(occurred, timestamp(issues[event["issue_id"]]["opened_at"]))
            if event["task_id"]:
                self.assertGreaterEqual(occurred, timestamp(tasks[event["task_id"]]["created_at"]))
        for r in self.rows["shifts"]:
            start = self.assert_time_in_range(r["shift_start_at"])
            finish = self.assert_time_in_range(r["shift_end_at"], shift_end=True)
            self.assertLess(start, finish)

    def test_04_week1_lifecycle_edge_cases(self):
        issues = {r["issue_id"]: r for r in self.rows["issues"]}
        tasks = self.rows["tasks"]
        events = self.rows["activity_events"]
        self.assertTrue(all(any(r["status"] == x for r in issues.values()) for x in SPEC["issue_statuses"]))
        self.assertTrue(all(any(r["status"] == x for r in tasks) for x in SPEC["task_statuses"]))
        self.assertTrue(all(any(r["subsystem"] == x for r in issues.values()) for x in SPEC["subsystems"]))
        self.assertTrue(all(any(r["priority"] == x for r in issues.values()) for x in SPEC["priorities"]))
        self.assertTrue(any(t["status"] == "completed" and issues[t["issue_id"]]["status"] != "closed" for t in tasks))
        self.assertTrue(any(i["status"] == "blocked" and any(t["issue_id"] == i["issue_id"] and t["status"] != "completed" for t in tasks) for i in issues.values()))
        self.assertTrue(any(t["status"] == "awaiting_review" and not t["completed_at"] for t in tasks))
        self.assertTrue(any(sum(t["issue_id"] == issue_id for t in tasks) >= 2 for issue_id in issues))
        self.assertTrue(any(sum(e["issue_id"] == issue_id for e in events) >= 2 for issue_id in issues))
        self.assertNotEqual(sum(t["status"] == "completed" for t in tasks), sum(i["status"] == "closed" for i in issues.values()))
        reopened = [i for i in issues.values() if i["status"] == "reopened"]
        self.assertTrue(reopened)
        for issue in reopened:
            history = sorted((e for e in events if e["issue_id"] == issue["issue_id"]), key=lambda e: e["occurred_at"])
            closed_at = [timestamp(e["occurred_at"]) for e in history if e["event_type"] == "issue_closed"]
            reopened_at = [timestamp(e["occurred_at"]) for e in history if e["event_type"] == "issue_reopened"]
            self.assertTrue(closed_at, issue["issue_id"])
            self.assertTrue(reopened_at, issue["issue_id"])
            self.assertTrue(any(c < r for c in closed_at for r in reopened_at), issue["issue_id"])

    def test_05_manifest_and_hash_integrity(self):
        manifest_path = OUTPUT / "manifest.json"
        self.assertTrue(manifest_path.is_file())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["phase"], SPEC["phase"])
        self.assertEqual(manifest["seed"], SPEC["seed"])
        self.assertEqual(manifest["counts"], SPEC["counts"])
        self.assertEqual(manifest["config_sha256"], hashlib.sha256(SPEC_PATH.read_bytes()).hexdigest())
        self.assertTrue(str(manifest["generator_version"]).strip())
        timestamp(manifest["generated_at_utc"])
        for name, (filename, _) in FILES.items():
            self.assertEqual(manifest["files"][filename]["rows"], SPEC["counts"][name])
            self.assertEqual(
                manifest["files"][filename]["sha256"],
                hashlib.sha256((OUTPUT / filename).read_bytes()).hexdigest(),
            )

    def test_06_reproducible_with_same_seed_and_config(self):
        script = ROOT / "scripts" / "generate_synthetic.py"
        self.assertTrue(script.is_file(), "First implement scripts/generate_synthetic.py")
        with tempfile.TemporaryDirectory() as directory:
            command = [sys.executable, str(script), "--config", str(SPEC_PATH), "--seed", str(SPEC["seed"]), "--output", directory]
            subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True, timeout=120)
            for name, (filename, _) in FILES.items():
                with self.subTest(table=name):
                    self.assertEqual((OUTPUT / filename).read_bytes(), (Path(directory) / filename).read_bytes())

    def test_07_no_week2_documents_or_embeddings(self):
        self.assertFalse((OUTPUT / "ncr_index.csv").exists())
        self.assertFalse((OUTPUT / "ncr_docs").exists())
        self.assertFalse((OUTPUT / "embeddings.json").exists())
        self.assertFalse((OUTPUT / "vectors.csv").exists())


if __name__ == "__main__":
    unittest.main()
