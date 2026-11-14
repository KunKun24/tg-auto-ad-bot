CREATE TABLE users (
 id BIGINT PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT, language_code TEXT,
 status TEXT NOT NULL DEFAULT 'ACTIVE', blocked BOOLEAN NOT NULL DEFAULT false,
 timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai', first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
 last_seen TIMESTAMPTZ NOT NULL DEFAULT now(), ban_reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE chats (
 chat_id BIGINT PRIMARY KEY, type TEXT NOT NULL, title TEXT NOT NULL, username TEXT,
 bot_status TEXT NOT NULL DEFAULT 'UNKNOWN', permissions JSONB NOT NULL DEFAULT '{}', updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE user_chats (
 id BIGSERIAL PRIMARY KEY, owner_id BIGINT NOT NULL REFERENCES users(id), chat_id BIGINT NOT NULL REFERENCES chats(chat_id),
 active BOOLEAN NOT NULL DEFAULT true, authorized_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(owner_id,chat_id)
);
CREATE INDEX ON user_chats(owner_id,active);
CREATE TABLE sources (
 id BIGSERIAL PRIMARY KEY, owner_id BIGINT NOT NULL REFERENCES users(id), chat_id BIGINT NOT NULL,
 title TEXT NOT NULL, username TEXT, active BOOLEAN NOT NULL DEFAULT true, authorized_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(owner_id,chat_id)
);
CREATE TABLE source_messages (
 id BIGSERIAL PRIMARY KEY, chat_id BIGINT NOT NULL, message_id BIGINT NOT NULL, payload JSONB NOT NULL,
 received_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(chat_id,message_id)
);
CREATE INDEX ON source_messages(chat_id,received_at DESC);
CREATE TABLE contents (
 id BIGSERIAL PRIMARY KEY, owner_id BIGINT NOT NULL REFERENCES users(id), title TEXT NOT NULL,
 current_version_id BIGINT, deleted BOOLEAN NOT NULL DEFAULT false, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON contents(owner_id,deleted);
CREATE TABLE content_versions (
 id BIGSERIAL PRIMARY KEY, content_id BIGINT NOT NULL REFERENCES contents(id), version_no INT NOT NULL,
 payload JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(content_id,version_no)
);
ALTER TABLE contents ADD CONSTRAINT contents_version_fk FOREIGN KEY(current_version_id) REFERENCES content_versions(id);
CREATE TABLE sessions (
 owner_id BIGINT PRIMARY KEY REFERENCES users(id), state TEXT NOT NULL, data JSONB NOT NULL,
 expires_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE tasks (
 id BIGSERIAL PRIMARY KEY, owner_id BIGINT NOT NULL REFERENCES users(id), name TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'DRAFT', interval_seconds INT NOT NULL CHECK(interval_seconds>=60),
 next_run_at TIMESTAMPTZ, remaining_seconds INT NOT NULL DEFAULT 0, current_index INT NOT NULL DEFAULT 0,
 delete_previous BOOLEAN NOT NULL DEFAULT false, instant_source_id BIGINT REFERENCES sources(id), source_mode TEXT NOT NULL DEFAULT 'copy',
 max_runs INT NOT NULL DEFAULT 0 CHECK(max_runs>=0), completed_runs INT NOT NULL DEFAULT 0,
 skip_failed BOOLEAN NOT NULL DEFAULT false, delete_failure_pause BOOLEAN NOT NULL DEFAULT false, deleted BOOLEAN NOT NULL DEFAULT false,
 last_error TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON tasks(next_run_at) WHERE status='RUNNING' AND NOT deleted;
CREATE INDEX ON tasks(owner_id,deleted);
CREATE INDEX ON tasks(instant_source_id) WHERE instant_source_id IS NOT NULL;
CREATE TABLE task_items (
 task_id BIGINT NOT NULL REFERENCES tasks(id), position INT NOT NULL, content_id BIGINT NOT NULL REFERENCES contents(id), PRIMARY KEY(task_id,position)
);
CREATE TABLE task_targets (
 id BIGSERIAL PRIMARY KEY, task_id BIGINT NOT NULL REFERENCES tasks(id), chat_id BIGINT NOT NULL,
 last_message_id BIGINT, last_sent_at TIMESTAMPTZ, active BOOLEAN NOT NULL DEFAULT true, UNIQUE(task_id,chat_id)
);
CREATE INDEX ON task_targets(chat_id);
CREATE TABLE publication_events (
 id BIGSERIAL PRIMARY KEY, task_id BIGINT NOT NULL REFERENCES tasks(id), kind TEXT NOT NULL,
 dedupe_key TEXT NOT NULL UNIQUE, payload JSONB NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
 scheduled_at TIMESTAMPTZ NOT NULL DEFAULT now(), started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ
);
CREATE INDEX ON publication_events(task_id,status,id);
CREATE TABLE deliveries (
 id BIGSERIAL PRIMARY KEY, event_id BIGINT NOT NULL REFERENCES publication_events(id), target_id BIGINT NOT NULL REFERENCES task_targets(id),
 chat_id BIGINT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING', attempts INT NOT NULL DEFAULT 0,
 next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(), message_id BIGINT, error TEXT NOT NULL DEFAULT '', degraded BOOLEAN NOT NULL DEFAULT false,
 delete_status TEXT, delete_error TEXT, started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ, UNIQUE(event_id,target_id)
);
CREATE INDEX ON deliveries(event_id,status,next_attempt_at);
CREATE TABLE message_logs (
 id BIGSERIAL PRIMARY KEY, owner_id BIGINT NOT NULL, task_id BIGINT, event_id BIGINT, chat_id BIGINT NOT NULL,
 message_id BIGINT, operation TEXT NOT NULL, status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', degraded BOOLEAN NOT NULL DEFAULT false,
 payload_version_id BIGINT, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON message_logs(owner_id,created_at DESC);
CREATE INDEX ON message_logs(created_at DESC);
CREATE INDEX ON message_logs(task_id,created_at DESC);
CREATE TABLE broadcast_jobs (
 id BIGSERIAL PRIMARY KEY, admin_id BIGINT NOT NULL, payload JSONB NOT NULL, status TEXT NOT NULL DEFAULT 'RUNNING',
 progress_chat_id BIGINT, progress_message_id BIGINT, last_progress_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE broadcast_recipients (
 id BIGSERIAL PRIMARY KEY, job_id BIGINT NOT NULL REFERENCES broadcast_jobs(id), user_id BIGINT NOT NULL,
 status TEXT NOT NULL DEFAULT 'PENDING', attempts INT NOT NULL DEFAULT 0, next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 message_id BIGINT, error TEXT NOT NULL DEFAULT '', degraded BOOLEAN NOT NULL DEFAULT false, started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ,
 UNIQUE(job_id,user_id)
);
CREATE INDEX ON broadcast_recipients(job_id,status,next_attempt_at);
CREATE TABLE audit_logs (
 id BIGSERIAL PRIMARY KEY, actor_id BIGINT NOT NULL, action TEXT NOT NULL, resource_type TEXT NOT NULL DEFAULT '',
 resource_id TEXT NOT NULL DEFAULT '', details JSONB NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON audit_logs(actor_id,created_at DESC);
CREATE TABLE app_settings (key TEXT PRIMARY KEY,value JSONB NOT NULL,updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE updates (
 update_id BIGINT PRIMARY KEY, payload JSONB NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING', attempts INT NOT NULL DEFAULT 0,
 error TEXT NOT NULL DEFAULT '', received_at TIMESTAMPTZ NOT NULL DEFAULT now(), processed_at TIMESTAMPTZ
);
CREATE INDEX ON updates(update_id) WHERE status='PENDING';
CREATE TABLE deferred_deletions (
 id BIGSERIAL PRIMARY KEY, owner_id BIGINT NOT NULL, chat_id BIGINT NOT NULL,message_id BIGINT NOT NULL,
 run_at TIMESTAMPTZ NOT NULL,status TEXT NOT NULL DEFAULT 'PENDING',error TEXT NOT NULL DEFAULT '', UNIQUE(chat_id,message_id)
);
CREATE INDEX ON deferred_deletions(run_at) WHERE status='PENDING';
INSERT INTO app_settings(key,value) VALUES
 ('min_interval_seconds','60'), ('max_tasks_per_user','100'), ('max_targets_per_task','1000'),
 ('max_bindings_per_user','2000'), ('instant_queue_limit','1000'), ('contact_admin','"@KunKun"'),
 ('test_message','"本消息由TG 自动广告机器人发布。权限测试成功，30秒后自动删除。"'),
 ('welcome','"欢迎使用TG 自动广告机器人！\n绑定频道和群组，保存广告内容，设置轮播或来源新帖同步。\n所有内容和发送记录长期保存在SQL，任务可暂停、恢复并在重启后继续。"');
