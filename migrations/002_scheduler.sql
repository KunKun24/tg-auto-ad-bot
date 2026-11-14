ALTER TABLE tasks ADD COLUMN last_served_at TIMESTAMPTZ;
CREATE INDEX tasks_fair_queue ON tasks(last_served_at NULLS FIRST,id) WHERE status='RUNNING' AND NOT deleted;
CREATE INDEX deliveries_due ON deliveries(next_attempt_at,event_id) WHERE status IN ('PENDING','RETRY');
CREATE INDEX broadcast_recipients_due ON broadcast_recipients(next_attempt_at,id) WHERE status IN ('PENDING','RETRY');
