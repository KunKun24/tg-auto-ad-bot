"""Isolated SQL capacity smoke, not a network/load or production SLA benchmark.

Run as adbot: .venv/bin/python -m pytest -p no:cacheprovider tests/test_scale.py -q -s
Bulk SQL seeds 2,000 users, 10,000 distinct bound chats and 2,000 running
five-target tasks. Only auto_ad_test is used, with a per-test random schema.
"""
from __future__ import annotations

import asyncio
import json
from time import perf_counter
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from adbot.db import Database
from adbot.engine import Engine

TEST_DSN = "postgresql://adbot@/auto_ad_test?host=/var/run/postgresql"
USERS, TARGETS, TASKS = 2000, 10000, 2000
PAYLOAD = {"type": "text", "text": "offline scale smoke", "entities": [], "buttons": []}


class MockBot:
    id = 999000

    def __init__(self):
        self.sent = []

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status="administrator", can_post_messages=True)

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(chat_id)
        return SimpleNamespace(message_id=100 + len(self.sent))


async def schema_bytes(db):
    # Include heap, toast and indexes for every table in this schema only.
    return await db.fetchval("SELECT COALESCE(sum(pg_total_relation_size(c.oid)),0)::bigint "
                             "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                             "WHERE n.nspname=current_schema() AND c.relkind='r'")


@pytest_asyncio.fixture
async def rig():
    assert "/auto_ad_test?" in TEST_DSN and "/auto_ad?" not in TEST_DSN
    control = await asyncpg.connect(TEST_DSN)
    schema = "scale_" + uuid4().hex
    db = Database(TEST_DSN)
    try:
        assert await control.fetchval("SELECT current_database()") == "auto_ad_test"
        assert await control.fetchval("SELECT current_user") == "adbot"
        await control.execute(f'CREATE SCHEMA "{schema}"')

        async def init(con):
            for typ in ("json", "jsonb"):
                await con.set_type_codec(typ, schema="pg_catalog", encoder=json.dumps, decoder=json.loads)

        db.pool = await asyncpg.create_pool(TEST_DSN, min_size=2, max_size=8, command_timeout=30,
                                            server_settings={"search_path": schema}, init=init)
        await db.migrate()
        empty_bytes = await schema_bytes(db)
        db_before_bytes = await control.fetchval("SELECT pg_database_size(current_database())")
        seed_start = perf_counter()
        # Explicit, bounded fixture IDs simplify exact integrity checks; no user input.
        async with db.pool.acquire() as con:
            async with con.transaction():
                await con.execute("INSERT INTO users(id,first_name) SELECT n,'scale user '||n FROM generate_series(1,$1::int) n", USERS)
                await con.execute("INSERT INTO chats(chat_id,type,title,bot_status) "
                                  "SELECT -1000000000-n,'channel','scale chat '||n,'administrator' "
                                  "FROM generate_series(1,$1::int) n", TARGETS)
                await con.execute("INSERT INTO user_chats(owner_id,chat_id) "
                                  "SELECT ((n-1)/5)+1,-1000000000-n FROM generate_series(1,$1::int) n", TARGETS)
                await con.execute("INSERT INTO contents(id,owner_id,title) "
                                  "SELECT n,n,'scale content '||n FROM generate_series(1,$1::int) n", USERS)
                await con.execute("INSERT INTO content_versions(id,content_id,version_no,payload) "
                                  "SELECT n,n,1,$2 FROM generate_series(1,$1::int) n", USERS, PAYLOAD)
                await con.execute("UPDATE contents SET current_version_id=id")
                await con.execute("INSERT INTO tasks(id,owner_id,name,status,interval_seconds,next_run_at) "
                                  "SELECT n,n,'scale task '||n,'RUNNING',3600,now()-interval '1 second' "
                                  "FROM generate_series(1,$1::int) n", TASKS)
                await con.execute("INSERT INTO task_items(task_id,position,content_id) SELECT id,0,id FROM tasks")
                await con.execute("INSERT INTO task_targets(task_id,chat_id) SELECT owner_id,chat_id FROM user_chats")
                # Maintain serial sequences because a follow-up test appends a late task.
                await con.execute("SELECT setval(pg_get_serial_sequence('tasks','id'),$1,true)", TASKS)
        await db.execute("ANALYZE")
        seed_seconds = perf_counter() - seed_start
        bot = MockBot()
        engine = Engine(db, bot, SimpleNamespace(workers=8, admin_ids=(2000,)))

        async def no_wait(*_args, **_kwargs):
            pass

        engine.throttle = no_wait
        yield SimpleNamespace(db=db, engine=engine, bot=bot, seed_seconds=seed_seconds,
                              empty_bytes=empty_bytes, db_before_bytes=db_before_bytes, schema=schema)
    finally:
        await db.close()
        assert schema.startswith("scale_") and len(schema) == 38
        await control.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await control.close()


async def schedule_all(rig):
    counts = []
    for _ in range(20):
        before = await rig.db.fetchval("SELECT count(*) FROM publication_events")
        await rig.engine.schedule()
        after = await rig.db.fetchval("SELECT count(*) FROM publication_events")
        counts.append(after - before)
    assert counts == [100] * 20  # scheduler intentionally claims at most 100 tasks
    return counts


@pytest.mark.asyncio
async def test_2000_tasks_schedule_10000_deliveries_in_bounded_batches(rig):
    assert await rig.db.fetchval("SELECT count(*) FROM users") == USERS
    assert await rig.db.fetchval("SELECT count(*) FROM chats") == TARGETS
    assert await rig.db.fetchval("SELECT count(*) FROM user_chats WHERE active") == TARGETS
    assert await rig.db.fetchval("SELECT count(*) FROM tasks WHERE status='RUNNING'") == TASKS
    started = perf_counter()
    batches = await schedule_all(rig)
    schedule_seconds = perf_counter() - started
    assert await rig.db.fetchval("SELECT count(*) FROM publication_events") == TASKS
    assert await rig.db.fetchval("SELECT count(*) FROM deliveries") == TARGETS
    assert await rig.db.fetchval("SELECT count(*) FROM (SELECT task_id FROM publication_events GROUP BY task_id HAVING count(*)<>1) x") == 0
    assert await rig.db.fetchval("SELECT count(*) FROM (SELECT event_id FROM deliveries GROUP BY event_id HAVING count(*)<>5) x") == 0
    assert await rig.db.fetchval("SELECT count(*) FROM deliveries d JOIN publication_events e ON e.id=d.event_id "
                                "JOIN tasks t ON t.id=e.task_id JOIN user_chats uc ON uc.owner_id=t.owner_id AND uc.chat_id=d.chat_id") == TARGETS
    await rig.engine.schedule()
    assert await rig.db.fetchval("SELECT count(*) FROM deliveries") == TARGETS  # repeated schedule remains idempotent
    assert rig.bot.sent == []
    # Bounded id keyset walk checks every delivery exactly once at this volume.
    cursor, walked, pages = 0, 0, 0
    while True:
        rows = await rig.db.fetch("SELECT id FROM deliveries WHERE id>$1 ORDER BY id LIMIT 257", cursor)
        if not rows:
            break
        assert all(row["id"] > cursor for row in rows)
        cursor = rows[-1]["id"]
        walked += len(rows)
        pages += 1
    assert walked == TARGETS and pages == 39
    size = await schema_bytes(rig.db)
    db_size = await rig.db.fetchval("SELECT pg_database_size(current_database())")
    print("\nSCALE_SCHEDULE " + json.dumps({"users": USERS, "bindings": TARGETS, "running_tasks": TASKS,
          "events": TASKS, "deliveries": TARGETS, "schedule_batches": len(batches), "keyset_pages": pages,
          "seed_seconds": round(rig.seed_seconds, 3), "schedule_seconds": round(schedule_seconds, 3),
          "schema_total_bytes": size, "schema_growth_bytes": size-rig.empty_bytes,
          "test_database_total_bytes": db_size, "test_database_growth_bytes": db_size-rig.db_before_bytes}))


@pytest.mark.asyncio
async def test_future_retry_backlog_does_not_starve_late_task_or_due_rotation(rig):
    await schedule_all(rig)
    await rig.db.execute("UPDATE deliveries SET status='RETRY',next_attempt_at=now()+interval '1 day'")
    late = await rig.db.fetchval("INSERT INTO tasks(owner_id,name,status,interval_seconds,next_run_at) "
                                "VALUES(1,'late ready task','RUNNING',3600,now()-interval '1 second') RETURNING id")
    await rig.db.execute("INSERT INTO task_items(task_id,position,content_id) VALUES($1,0,1)", late)
    await rig.db.execute("INSERT INTO task_targets(task_id,chat_id) SELECT $1,chat_id FROM user_chats WHERE owner_id=1", late)
    started = perf_counter()
    await rig.engine._tasks_tick()
    late_first_seconds = perf_counter() - started
    assert await rig.db.fetchval("SELECT count(*) FROM deliveries d JOIN publication_events e ON e.id=d.event_id WHERE e.task_id=$1 AND d.status='SUCCESS'", late) == 1
    assert len(rig.bot.sent) == 1
    assert await rig.db.fetchval("SELECT count(*) FROM tasks WHERE id<=$1 AND last_served_at IS NOT NULL", TASKS) == 0
    assert await rig.db.fetchval("SELECT count(*) FROM deliveries WHERE status='RETRY' AND next_attempt_at>now()") == TARGETS
    # Return old work to due, preserving one outstanding batch per task.
    # last_served_at round-robin should serve every older task once before a second.
    await rig.db.execute("UPDATE deliveries SET status='PENDING',next_attempt_at=now() "
                         "WHERE event_id IN (SELECT id FROM publication_events WHERE task_id<=$1)", TASKS)
    rotation_start = perf_counter()
    tick_counts = []
    for tick in range(63):
        await rig.engine._tasks_tick()
        served = await rig.db.fetchval("SELECT count(*) FROM tasks WHERE id<=$1 AND last_served_at IS NOT NULL", TASKS)
        tick_counts.append(served)
        assert served == min((tick+1)*32, TASKS)
    rotation_seconds = perf_counter() - rotation_start
    assert await rig.db.fetchval("SELECT count(DISTINCT e.task_id) FROM deliveries d JOIN publication_events e ON e.id=d.event_id WHERE e.task_id<=$1 AND d.status='SUCCESS'", TASKS) == TASKS
    size = await schema_bytes(rig.db)
    print("\nSCALE_FAIRNESS " + json.dumps({"future_retry_rows_initial": TARGETS,
          "late_task_first_tick_success": 1, "late_task_tick_seconds": round(late_first_seconds, 3),
          "old_tasks_each_served": TASKS, "rotation_ticks": len(tick_counts),
          "rotation_seconds": round(rotation_seconds, 3), "mock_api_sends": len(rig.bot.sent),
          "schema_total_bytes": size, "seed_seconds": round(rig.seed_seconds, 3)}))
