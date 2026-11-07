-- Cross-table histories cannot be expressed as ordinary row CHECK constraints.
DO $$
BEGIN
    IF EXISTS (
        SELECT FROM preflight.issues i JOIN preflight.aircraft a USING (aircraft_id)
        WHERE i.opened_at < a.record_created_at
    ) OR EXISTS (
        SELECT FROM preflight.tasks t JOIN preflight.issues i USING (issue_id)
        WHERE t.created_at < i.opened_at
           OR (i.status = 'closed' AND (t.status <> 'completed' OR t.completed_at > i.closed_at))
    ) OR EXISTS (
        SELECT FROM preflight.activity_events e
        JOIN preflight.issues i USING (issue_id)
        LEFT JOIN preflight.tasks t ON e.task_id = t.task_id
        WHERE e.occurred_at < i.opened_at OR e.occurred_at < t.created_at
           OR (i.status = 'closed' AND e.occurred_at > i.closed_at)
    ) THEN
        RAISE EXCEPTION 'Inconsistent issue/task/event chronology';
    END IF;
    IF EXISTS (
        SELECT FROM preflight.issues i WHERE i.status = 'blocked' AND NOT EXISTS (
            SELECT FROM preflight.tasks t WHERE t.issue_id = i.issue_id AND t.status <> 'completed'
        )
    ) THEN RAISE EXCEPTION 'Blocked issue has no unfinished task'; END IF;
    IF EXISTS (
        SELECT FROM preflight.issues i WHERE i.status = 'reopened' AND NOT EXISTS (
            SELECT FROM preflight.activity_events c JOIN preflight.activity_events r USING (issue_id)
            WHERE c.issue_id = i.issue_id AND c.event_type = 'issue_closed'
              AND r.event_type = 'issue_reopened' AND c.occurred_at < r.occurred_at
        )
    ) THEN RAISE EXCEPTION 'Reopened issue lacks closure/reopening history'; END IF;
    IF EXISTS (
        SELECT FROM preflight.tasks t WHERE t.status = 'completed' AND NOT EXISTS (
            SELECT FROM preflight.activity_events e WHERE e.task_id = t.task_id
              AND e.event_type = 'task_completed' AND e.occurred_at = t.completed_at
        )
    ) THEN RAISE EXCEPTION 'Completed task lacks matching completion event'; END IF;
    IF EXISTS (
        SELECT FROM preflight.shifts a JOIN preflight.shifts b
          ON a.person_id = b.person_id AND a.shift_id < b.shift_id
        WHERE a.shift_start_at < b.shift_end_at AND b.shift_start_at < a.shift_end_at
    ) THEN RAISE EXCEPTION 'Overlapping shifts for a worker'; END IF;
    IF NOT EXISTS (
        SELECT FROM preflight.issues i JOIN preflight.tasks t USING (issue_id)
        WHERE i.status = 'open' AND t.status = 'completed'
    ) THEN RAISE EXCEPTION 'Missing open issue with completed task'; END IF;
END $$;
