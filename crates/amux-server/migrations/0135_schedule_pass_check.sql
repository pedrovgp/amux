-- Pass/fail schedules (runtime_jobs/schedule_check.rs). `pass_check` is a
-- shell command whose exit code is the verdict: 0 passes, anything else fails.
-- NULL means the schedule has no pass condition. The rest is the sweep's state:
-- pass_state probe|pending|pass|fail|unrunnable, when the sweep acts next, how
-- many reconcile re-fires the current failing stretch has used, and the last
-- check's exit code and output tail.
ALTER TABLE schedules ADD COLUMN pass_check TEXT;
ALTER TABLE schedules ADD COLUMN pass_state TEXT;
ALTER TABLE schedules ADD COLUMN pass_next_at INTEGER;
ALTER TABLE schedules ADD COLUMN pass_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE schedules ADD COLUMN pass_checked_at INTEGER;
ALTER TABLE schedules ADD COLUMN pass_detail TEXT;
CREATE INDEX IF NOT EXISTS idx_schedules_pass_due ON schedules (pass_next_at) WHERE pass_check IS NOT NULL;
