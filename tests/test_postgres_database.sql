-- Run with psql -X -v ON_ERROR_STOP=1 -U postgres -d preflight.
-- Every negative test rolls its attempted mutation back; the outer transaction
-- also rolls back so the fictional fixture remains unchanged.
BEGIN;
SET LOCAL TIME ZONE 'UTC';
DO $$
DECLARE
    task_row preflight.tasks%ROWTYPE;
    event_row preflight.activity_events%ROWTYPE;
    worker text;
    other_task text;
BEGIN
    BEGIN
        INSERT INTO preflight.issues
        VALUES ('ISSUE-9999', 'DEMO-AIR-9999', 'Fictional test', 'Fictional test',
                'cabin', 'low', 'open', '2026-01-01T00:00:00+00:00', NULL);
        RAISE EXCEPTION 'FAIL: orphan aircraft reference accepted';
    EXCEPTION WHEN foreign_key_violation THEN
        RAISE NOTICE 'PASS: orphan aircraft reference rejected';
    END;

    SELECT * INTO STRICT task_row FROM preflight.tasks ORDER BY task_id LIMIT 1;
    SELECT person_id INTO STRICT worker FROM preflight.people
        WHERE team_id <> task_row.assigned_team_id ORDER BY person_id LIMIT 1;
    BEGIN
        UPDATE preflight.tasks SET assigned_person_id = worker WHERE task_id = task_row.task_id;
        RAISE EXCEPTION 'FAIL: cross-team worker assignment accepted';
    EXCEPTION WHEN foreign_key_violation THEN
        RAISE NOTICE 'PASS: cross-team worker assignment rejected';
    END;

    SELECT * INTO STRICT event_row FROM preflight.activity_events
        WHERE task_id IS NOT NULL ORDER BY event_id LIMIT 1;
    SELECT task_id INTO STRICT other_task FROM preflight.tasks
        WHERE issue_id <> event_row.issue_id ORDER BY task_id LIMIT 1;
    BEGIN
        UPDATE preflight.activity_events SET task_id = other_task WHERE event_id = event_row.event_id;
        RAISE EXCEPTION 'FAIL: cross-issue event task accepted';
    EXCEPTION WHEN foreign_key_violation THEN
        RAISE NOTICE 'PASS: cross-issue event task rejected';
    END;

    BEGIN
        UPDATE preflight.tasks SET completed_at = created_at WHERE status = 'awaiting_review';
        RAISE EXCEPTION 'FAIL: review task completion date accepted';
    EXCEPTION WHEN check_violation THEN
        RAISE NOTICE 'PASS: review task completion date rejected';
    END;
    BEGIN
        UPDATE preflight.tasks SET completed_at = created_at - interval '1 second' WHERE status = 'completed';
        RAISE EXCEPTION 'FAIL: completion before creation accepted';
    EXCEPTION WHEN check_violation THEN
        RAISE NOTICE 'PASS: completion before creation rejected';
    END;
    BEGIN
        INSERT INTO preflight.teams SELECT * FROM preflight.teams LIMIT 1;
        RAISE EXCEPTION 'FAIL: duplicate primary key accepted';
    EXCEPTION WHEN unique_violation THEN
        RAISE NOTICE 'PASS: duplicate primary key rejected';
    END;
END $$;

SET LOCAL ROLE preflight_reader;
DO $$
DECLARE n integer;
BEGIN
    SELECT count(*) INTO n FROM preflight.issues;
    IF n <> 180 THEN RAISE EXCEPTION 'FAIL: reader cannot see all issues'; END IF;
    RAISE NOTICE 'PASS: reader can query issues';
    BEGIN
        UPDATE preflight.issues SET title = title;
        RAISE EXCEPTION 'FAIL: reader has write permission';
    EXCEPTION WHEN insufficient_privilege THEN
        RAISE NOTICE 'PASS: reader write rejected';
    END;
END $$;
ROLLBACK;
