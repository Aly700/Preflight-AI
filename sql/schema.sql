-- Original fictional Week 1 schema. Apply through load_postgres.py.
CREATE SCHEMA IF NOT EXISTS preflight;
REVOKE ALL ON SCHEMA preflight FROM PUBLIC;

CREATE TABLE IF NOT EXISTS preflight.teams (
    team_id text PRIMARY KEY CHECK (team_id ~ '^TEAM-[0-9]+$'),
    team_name text NOT NULL
);
CREATE TABLE IF NOT EXISTS preflight.people (
    person_id text PRIMARY KEY CHECK (person_id ~ '^PERSON-[0-9]+$'),
    display_name text NOT NULL CHECK (display_name ~ '^Demo Worker [0-9]+$'),
    team_id text NOT NULL REFERENCES preflight.teams,
    UNIQUE (person_id, team_id)
);
CREATE TABLE IF NOT EXISTS preflight.aircraft (
    aircraft_id text PRIMARY KEY CHECK (aircraft_id ~ '^DEMO-AIR-[0-9]+$'),
    program text NOT NULL CHECK (program IN ('DEMO-PROGRAM-ALPHA', 'DEMO-PROGRAM-BETA')),
    stage text NOT NULL CHECK (stage IN ('assembly', 'testing', 'review')),
    station text NOT NULL CHECK (station IN ('DEMO-STATION-01', 'DEMO-STATION-02', 'DEMO-STATION-03')),
    record_created_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS preflight.issues (
    issue_id text PRIMARY KEY CHECK (issue_id ~ '^ISSUE-[0-9]+$'),
    aircraft_id text NOT NULL REFERENCES preflight.aircraft,
    title text NOT NULL,
    description text NOT NULL,
    subsystem text NOT NULL CHECK (subsystem IN ('hydraulics', 'avionics', 'electrical', 'cabin', 'structures', 'controls')),
    priority text NOT NULL CHECK (priority IN ('low', 'medium', 'high')),
    status text NOT NULL CHECK (status IN ('open', 'in_progress', 'blocked', 'reopened', 'closed')),
    opened_at timestamptz NOT NULL,
    closed_at timestamptz,
    CHECK ((status = 'closed') = (closed_at IS NOT NULL)),
    CHECK (closed_at >= opened_at)
);
CREATE TABLE IF NOT EXISTS preflight.tasks (
    task_id text PRIMARY KEY CHECK (task_id ~ '^TASK-[0-9]+$'),
    issue_id text NOT NULL REFERENCES preflight.issues,
    task_kind text NOT NULL CHECK (task_kind IN ('review', 'investigation', 'documentation', 'inspection', 'rectification')),
    status text NOT NULL CHECK (status IN ('todo', 'in_progress', 'awaiting_review', 'completed', 'blocked')),
    assigned_team_id text NOT NULL REFERENCES preflight.teams,
    assigned_person_id text,
    created_at timestamptz NOT NULL,
    completed_at timestamptz,
    FOREIGN KEY (assigned_person_id, assigned_team_id) REFERENCES preflight.people (person_id, team_id),
    UNIQUE (task_id, issue_id),
    CHECK ((status = 'completed') = (completed_at IS NOT NULL)),
    CHECK (completed_at >= created_at)
);
CREATE TABLE IF NOT EXISTS preflight.activity_events (
    event_id text PRIMARY KEY CHECK (event_id ~ '^EVT-[0-9]+$'),
    issue_id text NOT NULL REFERENCES preflight.issues,
    task_id text,
    actor_id text REFERENCES preflight.people,
    event_type text NOT NULL CHECK (event_type IN (
        'issue_opened', 'issue_closed', 'issue_reopened', 'task_created',
        'task_assigned', 'task_started', 'task_completed', 'task_review_requested',
        'task_blocked', 'task_unblocked', 'note_added'
    )),
    occurred_at timestamptz NOT NULL,
    note text NOT NULL,
    FOREIGN KEY (task_id, issue_id) REFERENCES preflight.tasks (task_id, issue_id)
);
CREATE TABLE IF NOT EXISTS preflight.shifts (
    shift_id text PRIMARY KEY CHECK (shift_id ~ '^SHIFT-[0-9]+$'),
    person_id text NOT NULL,
    team_id text NOT NULL,
    shift_start_at timestamptz NOT NULL,
    shift_end_at timestamptz NOT NULL,
    FOREIGN KEY (person_id, team_id) REFERENCES preflight.people (person_id, team_id),
    CHECK (shift_end_at > shift_start_at)
);

CREATE INDEX IF NOT EXISTS issues_aircraft_idx ON preflight.issues (aircraft_id);
CREATE INDEX IF NOT EXISTS tasks_issue_idx ON preflight.tasks (issue_id);
CREATE INDEX IF NOT EXISTS events_issue_time_idx ON preflight.activity_events (issue_id, occurred_at);

-- A group role, not a credential or an application login.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'preflight_reader') THEN
        CREATE ROLE preflight_reader NOLOGIN;
    END IF;
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'preflight_reader'
               AND (rolsuper OR rolcreaterole OR rolcreatedb OR rolcanlogin)) THEN
        RAISE EXCEPTION 'Existing preflight_reader has unexpected privileges';
    END IF;
END $$;
GRANT USAGE ON SCHEMA preflight TO preflight_reader;
REVOKE ALL ON ALL TABLES IN SCHEMA preflight FROM preflight_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA preflight TO preflight_reader;
