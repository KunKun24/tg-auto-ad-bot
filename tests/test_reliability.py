"""Real-SQL reliability regressions; never connect to the production database.

Run as the PostgreSQL peer-authenticated adbot OS user:
  PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider tests/test_reliability.py -q
Every test creates a unique schema in auto_ad_test and removes only that schema.
All Telegram methods are implemented locally; no token or network is involved.
"""
from __future__ import annotations

import asyncio
import json
from collections import Counter, defaultdict, deque
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from telegram import Message
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

from adbot.db import Database
from adbot.engine import Engine

TEST_DSN = "postgresql://adbot@/auto_ad_test?host=/var/run/postgresql"
OWNER = 11001
OTHER = 11002
ADMIN = 11003
CHAT_A = -10010001
CHAT_B = -10010002
SOURCE = -10020001
PAYLOAD = {"type": "text", "text": "offline reliability fixture", "entities": [], "buttons": []}


class MockBot:
    """Small API-compatible bot that records all attempted external operations."""

    id = 999000

    def __init__(self):
        self.sent = []
        self.deleted = []
        self.edited = []
        self.errors = defaultdict(deque)
        self.delete_errors = {}
        self.before_send = None
        self.before_delete = None
        self.permission_error = None
        self.sequence = 100

    async def get_chat_member(self, chat_id, user_id):
        if self.permission_error:
            raise self.permission_error
        return SimpleNamespace(status="administrator", can_post_messages=True)

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        if self.before_send:
            await self.before_send(chat_id)
        if self.errors[chat_id]:
            raise self.errors[chat_id].popleft()
        self.sequence += 1
        return SimpleNamespace(message_id=self.sequence)

    async def copy_message(self, chat_id, from_chat_id, message_id, **kwargs):
        return await self.send_message(chat_id, f"copy:{from_chat_id}:{message_id}", **kwargs)

    async def forward_message(self, chat_id, from_chat_id, message_id, **kwargs):
        return await self.send_message(chat_id, f"forward:{from_chat_id}:{message_id}", **kwargs)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        if self.before_delete:
            await self.before_delete(chat_id)
        if chat_id in self.delete_errors:
            raise self.delete_errors[chat_id]
        return True

    async def edit_message_text(self, text, **kwargs):
        self.edited.append((text, kwargs))
        return SimpleNamespace(message_id=kwargs["message_id"])

    def count(self, chat_id):
        return sum(chat == chat_id for chat, _, _ in self.sent)


@pytest_asyncio.fixture
async def db():
    assert "/auto_ad_test?" in TEST_DSN and "/auto_ad?" not in TEST_DSN
    control = await asyncpg.connect(TEST_DSN)
    schema = "reliability_" + uuid4().hex
    database = Database(TEST_DSN)
    try:
        assert await control.fetchval("SELECT current_database()") == "auto_ad_test"
        assert await control.fetchval("SELECT current_user") == "adbot"
        await control.execute(f'CREATE SCHEMA "{schema}"')

        async def init(connection):
            for typ in ("json", "jsonb"):
                await connection.set_type_codec(typ, encoder=lambda obj: json.dumps(obj, ensure_ascii=False),
                                                decoder=json.loads, schema="pg_catalog")

        database.pool = await asyncpg.create_pool(TEST_DSN, min_size=2, max_size=6,
                                                  server_settings={"search_path": schema},
                                                  init=init, command_timeout=10)
        await database.migrate()
        yield database
    finally:
        await database.close()
        # This name is generated locally and never contains user input.
        assert schema.startswith("reliability_") and len(schema) == 44
        await control.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await control.close()


@pytest_asyncio.fixture
async def rig(db):
    for uid in (OWNER, OTHER, ADMIN):
        await db.execute("INSERT INTO users(id,first_name) VALUES($1,$2)", uid, f"User {uid}")
    for chat in (CHAT_A, CHAT_B, SOURCE):
        await db.execute("INSERT INTO chats(chat_id,type,title) VALUES($1,'channel',$2)", chat, str(chat))
        for uid in (OWNER, OTHER):
            await db.execute("INSERT INTO user_chats(owner_id,chat_id) VALUES($1,$2)", uid, chat)
    bot = MockBot()
    engine = Engine(db, bot, SimpleNamespace(workers=8, admin_ids=(ADMIN,)))

    async def no_wait(*_args, **_kwargs):
        pass

    engine.throttle = no_wait
    return SimpleNamespace(db=db, bot=bot, engine=engine)


async def content(rig, owner=OWNER):
    cid = await rig.db.fetchval("INSERT INTO contents(owner_id,title) VALUES($1,'fixture') RETURNING id", owner)
    vid = await rig.db.fetchval("INSERT INTO content_versions(content_id,version_no,payload) VALUES($1,1,$2) RETURNING id", cid, PAYLOAD)
    await rig.db.execute("UPDATE contents SET current_version_id=$2 WHERE id=$1", cid, vid)
    return cid


async def task(rig, chats=(CHAT_A,), owner=OWNER, start=True, **options):
    cid = await content(rig, owner)
    tid = await rig.engine.create_task(owner, "reliability", [cid], list(chats), 60, **options)
    if start:
        await rig.engine.start_task(tid, owner)
        await rig.engine.schedule()
    return tid


async def snapshot(rig, tid):
    current = await rig.db.fetchrow("SELECT * FROM tasks WHERE id=$1", tid)
    event = await rig.db.fetchrow("SELECT * FROM publication_events WHERE task_id=$1 ORDER BY id LIMIT 1", tid)
    return current, event


async def process(rig, tid):
    # Production deliberately serves one delivery per turn for fair scheduling.
    # Drain only currently eligible rows; never fast-forward a retry deadline.
    for _ in range(20):
        current = await rig.db.fetchrow("SELECT * FROM tasks WHERE id=$1", tid)
        if current["status"] != "RUNNING":
            return
        await rig.engine._process_task(current)
        due = await rig.db.fetchval("SELECT count(*) FROM deliveries d JOIN publication_events e ON e.id=d.event_id "
                                     "WHERE e.task_id=$1 AND e.status IN ('PENDING','RUNNING') "
                                     "AND d.status IN ('PENDING','RETRY') AND d.next_attempt_at<=now()", tid)
        if not due:
            return
    raise AssertionError("eligible deliveries did not drain")


async def delivery(rig, event_id):
    return await rig.db.fetchrow("SELECT d.*,tt.last_message_id,tt.last_sent_at,c.type chat_type FROM deliveries d "
                                 "JOIN task_targets tt ON tt.id=d.target_id LEFT JOIN chats c ON c.chat_id=d.chat_id "
                                 "WHERE d.event_id=$1 ORDER BY d.id LIMIT 1", event_id)


async def source(rig, chat=SOURCE, owner=OWNER):
    return await rig.db.fetchval("INSERT INTO sources(owner_id,chat_id,title) VALUES($1,$2,'source') RETURNING id", owner, chat)


def source_message(mid=500):
    return Message.de_json({"message_id": mid, "date": 1, "chat": {"id": SOURCE, "type": "channel", "title": "Source"},
                            "text": "indexed original"}, None)


@pytest.mark.asyncio
async def test_bad_request_is_definitive_failure_not_unknown(rig):
    tid = await task(rig)
    rig.bot.errors[CHAT_A].append(BadRequest("message is too long"))
    await process(rig, tid)
    row = await rig.db.fetchrow("SELECT status,attempts FROM deliveries")
    assert row["status"] == "FAILED" and row["attempts"] == 1
    assert await rig.db.fetchval("SELECT status FROM tasks WHERE id=$1", tid) == "ERROR"
    assert rig.bot.count(CHAT_A) == 1


@pytest.mark.asyncio
async def test_permission_bad_request_never_attempts_send(rig):
    tid = await task(rig)
    rig.bot.permission_error = BadRequest("chat not found")
    await process(rig, tid)
    assert await rig.db.fetchval("SELECT status FROM deliveries") == "FAILED"
    assert rig.bot.count(CHAT_A) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimedOut(), NetworkError("connection reset")])
async def test_ambiguous_send_blocks_start_stop_edit_until_resolved(rig, error):
    tid = await task(rig)
    rig.bot.errors[CHAT_A].append(error)
    await process(rig, tid)
    assert await rig.db.fetchval("SELECT status FROM deliveries") == "UNKNOWN"
    for operation in (lambda: rig.engine.start_task(tid, OWNER), lambda: rig.engine.stop_task(tid, OWNER),
                      lambda: rig.engine.edit_task(tid, OWNER, name="bypass")):
        with pytest.raises(ValueError):
            await operation()
    await rig.engine.pause_task(tid, OWNER)
    with pytest.raises(ValueError):
        await rig.engine.edit_task(tid, OWNER, name="paused bypass")
    assert rig.bot.count(CHAT_A) == 1
    await rig.engine.resolve_unknown(tid, OWNER, "retry")
    await rig.engine.start_task(tid, OWNER)
    await process(rig, tid)
    assert rig.bot.count(CHAT_A) == 2
    assert await rig.db.fetchval("SELECT status FROM deliveries") == "SUCCESS"
    assert await rig.db.fetchval("SELECT count(*) FROM publication_events") == 1


@pytest.mark.asyncio
async def test_partial_failure_resume_does_not_resend_successful_target(rig):
    tid = await task(rig, chats=(CHAT_A, CHAT_B))
    rows = await rig.db.fetch("SELECT chat_id FROM deliveries ORDER BY id")
    successful, failed = rows[0]["chat_id"], rows[1]["chat_id"]
    rig.bot.errors[failed].append(BadRequest("not enough rights to send text messages"))
    await process(rig, tid)
    counts = dict(await rig.db.fetch("SELECT status,count(*) FROM deliveries GROUP BY status"))
    assert counts == {"SUCCESS": 1, "FAILED": 1}
    assert await rig.db.fetchval("SELECT completed_runs FROM tasks WHERE id=$1", tid) == 0
    await rig.engine.start_task(tid, OWNER)
    await process(rig, tid)
    assert rig.bot.count(successful) == 1
    assert rig.bot.count(failed) == 2
    assert await rig.db.fetchval("SELECT count(*) FROM deliveries WHERE status='SUCCESS'") == 2
    assert await rig.db.fetchval("SELECT completed_runs FROM tasks WHERE id=$1", tid) == 1
    assert await rig.db.fetchval("SELECT count(*) FROM publication_events") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("with_previous", [False, True])
async def test_pause_during_throttle_prevents_delete_and_send(rig, with_previous):
    tid = await task(rig, delete_previous=with_previous)
    if with_previous:
        await rig.db.execute("UPDATE task_targets SET last_message_id=77 WHERE task_id=$1", tid)
    current, event = await snapshot(rig, tid)
    d = await delivery(rig, event["id"])
    paused = False

    async def pause_before_api(*_args):
        nonlocal paused
        if not paused:
            paused = True
            await rig.engine.pause_task(tid, OWNER)

    rig.engine.throttle = pause_before_api
    await rig.engine._send_delivery(current, event, d)
    assert rig.bot.deleted == []
    assert rig.bot.count(CHAT_A) == 0
    assert await rig.db.fetchval("SELECT status FROM tasks WHERE id=$1", tid) == "PAUSED"


@pytest.mark.asyncio
async def test_pause_after_delete_saves_delete_and_resumes_without_deleting_twice(rig):
    tid = await task(rig, delete_previous=True)
    await rig.db.execute("UPDATE task_targets SET last_message_id=77 WHERE task_id=$1", tid)

    async def pause_after_delete(_chat_id):
        await rig.engine.pause_task(tid, OWNER)

    rig.bot.before_delete = pause_after_delete
    await process(rig, tid)
    assert rig.bot.deleted == [(CHAT_A, 77)] and rig.bot.count(CHAT_A) == 0
    assert await rig.db.fetchval("SELECT status FROM deliveries") == "PENDING"
    assert await rig.db.fetchval("SELECT delete_status FROM deliveries") == "SUCCESS"
    rig.bot.before_delete = None
    await rig.engine.start_task(tid, OWNER)
    await process(rig, tid)
    assert rig.bot.deleted == [(CHAT_A, 77)] and rig.bot.count(CHAT_A) == 1
    assert await rig.db.fetchval("SELECT status FROM deliveries") == "SUCCESS"


@pytest.mark.asyncio
async def test_inflight_send_prevents_stop_and_edit_even_after_pause(rig):
    tid = await task(rig)
    await rig.db.execute("UPDATE deliveries SET status='SENDING'")
    with pytest.raises(ValueError):
        await rig.engine.stop_task(tid, OWNER)
    await rig.engine.pause_task(tid, OWNER)
    with pytest.raises(ValueError):
        await rig.engine.edit_task(tid, OWNER, name="unsafe edit")
    assert await rig.db.fetchval("SELECT status FROM deliveries") == "SENDING"


@pytest.mark.asyncio
async def test_recover_preserves_pending_deliveries_without_inventing_unknown(rig):
    tid = await task(rig)
    await rig.engine.recover()
    assert await rig.db.fetchval("SELECT status FROM tasks WHERE id=$1", tid) == "RUNNING"
    assert await rig.db.fetchval("SELECT status FROM deliveries") == "PENDING"
    assert rig.bot.sent == []
    await process(rig, tid)
    assert rig.bot.count(CHAT_A) == 1


@pytest.mark.asyncio
async def test_cancelled_old_event_cannot_send_after_stop_start(rig):
    tid = await task(rig)
    current, event = await snapshot(rig, tid)
    d = await delivery(rig, event["id"])
    await rig.engine.stop_task(tid, OWNER)
    await rig.engine.start_task(tid, OWNER)
    await rig.engine._send_delivery(current, event, d)
    assert await rig.db.fetchval("SELECT status FROM publication_events WHERE id=$1", event["id"]) == "CANCELLED"
    assert rig.bot.count(CHAT_A) == 0


@pytest.mark.asyncio
async def test_delete_failure_pause_overrides_skip_failed(rig):
    tid = await task(rig, delete_previous=True, skip_failed=True, delete_failure_pause=True)
    await rig.db.execute("UPDATE task_targets SET last_message_id=77 WHERE task_id=$1", tid)
    rig.bot.delete_errors[CHAT_A] = BadRequest("message can't be deleted")
    await process(rig, tid)
    assert rig.bot.deleted == [(CHAT_A, 77)]
    assert rig.bot.count(CHAT_A) == 0
    assert await rig.db.fetchval("SELECT status FROM tasks WHERE id=$1", tid) == "ERROR"
    assert await rig.db.fetchval("SELECT completed_runs FROM tasks WHERE id=$1", tid) == 0
    assert await rig.db.fetchval("SELECT status FROM publication_events") == "ERROR"


@pytest.mark.asyncio
async def test_final_finite_run_paused_in_flight_cannot_add_another_run(rig):
    tid = await task(rig, max_runs=1)

    async def pause_during_send(chat_id):
        if chat_id == CHAT_A:
            await rig.engine.pause_task(tid, OWNER)

    rig.bot.before_send = pause_during_send
    await process(rig, tid)
    assert await rig.db.fetchval("SELECT completed_runs FROM tasks WHERE id=$1", tid) == 1
    with pytest.raises(ValueError, match="完成"):
        await rig.engine.start_task(tid, OWNER)
    assert rig.bot.count(CHAT_A) == 1


@pytest.mark.asyncio
async def test_source_ingestion_is_idempotent_and_pause_does_not_backlog(rig):
    sid = await source(rig)
    tid = await task(rig, instant_source_id=sid)
    msg = source_message()
    await asyncio.gather(rig.engine.ingest_source(msg, 1), rig.engine.ingest_source(msg, 1))
    assert await rig.db.fetchval("SELECT count(*) FROM source_messages") == 1
    assert await rig.db.fetchval("SELECT count(*) FROM publication_events WHERE kind='INSTANT'") == 1
    await rig.engine.pause_task(tid, OWNER)
    await rig.engine.ingest_source(source_message(501), 2)
    assert await rig.db.fetchval("SELECT count(*) FROM source_messages") == 2
    assert await rig.db.fetchval("SELECT count(*) FROM publication_events WHERE kind='INSTANT' AND status<>'CANCELLED'") == 0


@pytest.mark.asyncio
async def test_cross_owner_concurrent_source_cycle_is_rejected(rig):
    source_a = await source(rig, CHAT_A, OWNER)
    source_b = await source(rig, CHAT_B, OTHER)
    content_a, content_b = await content(rig, OWNER), await content(rig, OTHER)
    outcomes = await asyncio.gather(
        rig.engine.create_task(OWNER, "A to B", [content_a], [CHAT_B], 60, instant_source_id=source_a),
        rig.engine.create_task(OTHER, "B to A", [content_b], [CHAT_A], 60, instant_source_id=source_b),
        return_exceptions=True)
    assert sum(type(result) is int for result in outcomes) == 1
    errors = [result for result in outcomes if isinstance(result, Exception)]
    assert len(errors) == 1 and isinstance(errors[0], ValueError) and "循环" in str(errors[0])
    assert await rig.db.fetchval("SELECT count(*) FROM tasks") == 1


@pytest.mark.asyncio
async def test_source_self_target_and_foreign_owner_are_rejected(rig):
    sid = await source(rig, CHAT_A)
    cid = await content(rig)
    with pytest.raises(ValueError, match="来源"):
        await rig.engine.create_task(OWNER, "self", [cid], [CHAT_A], 60, instant_source_id=sid)
    foreign = await source(rig, SOURCE, OTHER)
    with pytest.raises(ValueError, match="来源"):
        await rig.engine.create_task(OWNER, "foreign", [cid], [CHAT_B], 60, instant_source_id=foreign)
    assert await rig.db.fetchval("SELECT count(*) FROM tasks") == 0


@pytest.mark.asyncio
async def test_recover_marks_only_inflight_unknown_and_keeps_success(rig):
    tid = await task(rig, chats=(CHAT_A, CHAT_B))
    await rig.db.execute("UPDATE deliveries SET status='SUCCESS',message_id=99 WHERE chat_id=$1", CHAT_A)
    await rig.db.execute("UPDATE deliveries SET status='SENDING' WHERE chat_id=$1", CHAT_B)
    await rig.db.execute("UPDATE publication_events SET status='RUNNING'")
    await rig.engine.schedule_test_delete(OWNER, CHAT_A, 90)
    await rig.db.execute("UPDATE deferred_deletions SET status='DELETING'")
    await rig.engine.recover()
    assert await rig.db.fetchval("SELECT status FROM tasks WHERE id=$1", tid) == "ERROR"
    assert await rig.db.fetchval("SELECT status FROM publication_events") == "ERROR"
    assert dict(await rig.db.fetch("SELECT chat_id,status FROM deliveries")) == {CHAT_A: "SUCCESS", CHAT_B: "UNKNOWN"}
    assert await rig.db.fetchval("SELECT status FROM deferred_deletions") == "PENDING"
    assert rig.bot.count(CHAT_A) == rig.bot.count(CHAT_B) == 0
    await rig.engine.resolve_unknown(tid, OWNER, "skip")
    await rig.engine.start_task(tid, OWNER)
    await process(rig, tid)
    assert await rig.db.fetchval("SELECT status FROM publication_events") == "COMPLETED"
    assert rig.bot.count(CHAT_A) == rig.bot.count(CHAT_B) == 0


@pytest.mark.asyncio
async def test_retry_after_is_known_rejection_and_preserves_success(rig):
    tid = await task(rig, chats=(CHAT_A, CHAT_B))
    rig.bot.errors[CHAT_B].append(RetryAfter(2))
    await process(rig, tid)
    assert dict(await rig.db.fetch("SELECT chat_id,status FROM deliveries")) == {CHAT_A: "SUCCESS", CHAT_B: "RETRY"}
    assert await rig.db.fetchval("SELECT next_attempt_at>now() FROM deliveries WHERE chat_id=$1", CHAT_B)
    await rig.db.execute("UPDATE deliveries SET next_attempt_at=now() WHERE status='RETRY'")
    await process(rig, tid)
    assert rig.bot.count(CHAT_A) == 1 and rig.bot.count(CHAT_B) == 2


@pytest.mark.asyncio
async def test_broadcast_bad_request_is_failed_not_unknown(rig):
    job = await rig.engine.create_broadcast(ADMIN, PAYLOAD)
    rig.bot.errors[OWNER].append(BadRequest("message is too long"))
    await rig.engine._broadcast_tick()
    assert await rig.db.fetchval("SELECT status FROM broadcast_recipients WHERE job_id=$1 AND user_id=$2", job, OWNER) == "FAILED"
    assert await rig.db.fetchval("SELECT status FROM broadcast_jobs WHERE id=$1", job) == "COMPLETED"
    stats = await rig.engine.broadcast_stats(job)
    assert stats["failed"] == 1 and stats["unknown"] == 0 and stats["success"] == 2


@pytest.mark.asyncio
async def test_broadcast_recover_unknown_requires_explicit_resolution(rig):
    job = await rig.engine.create_broadcast(ADMIN, PAYLOAD)
    await rig.db.execute("UPDATE broadcast_recipients SET status=CASE WHEN user_id=$2 THEN 'SENDING' ELSE 'SUCCESS' END WHERE job_id=$1", job, OWNER)
    await rig.engine.recover()
    assert await rig.db.fetchval("SELECT status FROM broadcast_jobs WHERE id=$1", job) == "ERROR"
    with pytest.raises(ValueError, match="未知"):
        await rig.engine.broadcast_action(job, ADMIN, "resume")
    await rig.engine.broadcast_action(job, ADMIN, "retry_unknown")
    await rig.engine.broadcast_action(job, ADMIN, "resume")
    await rig.engine._broadcast_tick()
    assert Counter(chat for chat, _, _ in rig.bot.sent) == {OWNER: 1}
    assert await rig.db.fetchval("SELECT status FROM broadcast_jobs WHERE id=$1", job) == "COMPLETED"
    stats = await rig.engine.broadcast_stats(job)
    assert stats["success"] == 3 and stats["pending"] == stats["unknown"] == 0


@pytest.mark.asyncio
async def test_broadcast_network_error_is_unknown_and_never_auto_retried(rig):
    job = await rig.engine.create_broadcast(ADMIN, PAYLOAD)
    rig.bot.errors[OWNER].append(TimedOut())
    row = await rig.db.fetchrow("SELECT r.*,b.payload FROM broadcast_recipients r JOIN broadcast_jobs b ON b.id=r.job_id WHERE r.job_id=$1 AND r.user_id=$2", job, OWNER)
    await rig.engine._send_broadcast(row)
    await rig.engine._broadcast_tick()
    assert rig.bot.count(OWNER) == 1
    assert await rig.db.fetchval("SELECT status FROM broadcast_recipients WHERE id=$1", row["id"]) == "UNKNOWN"
    assert await rig.db.fetchval("SELECT status FROM broadcast_jobs WHERE id=$1", job) == "ERROR"


@pytest.mark.asyncio
async def test_broadcast_progress_can_be_rebuilt_from_database(rig):
    job = await rig.engine.create_broadcast(ADMIN, PAYLOAD)
    await rig.db.execute("UPDATE broadcast_jobs SET progress_chat_id=$2,progress_message_id=700 WHERE id=$1", job, ADMIN)
    await rig.db.execute("UPDATE broadcast_recipients SET status='SUCCESS' WHERE job_id=$1", job)
    await rig.engine._broadcast_tick()
    replacement = Engine(rig.db, rig.bot, SimpleNamespace(workers=8, admin_ids=(ADMIN,)))
    replacement.throttle = rig.engine.throttle
    await replacement._progress_tick()
    assert len(rig.bot.edited) == 1
    text, kwargs = rig.bot.edited[0]
    assert "100%" in text and "COMPLETED" in text and "成功：3" in text
    assert kwargs["message_id"] == 700
    assert await rig.db.fetchval("SELECT last_progress_at IS NOT NULL FROM broadcast_jobs WHERE id=$1", job)


@pytest.mark.asyncio
async def test_task_operations_cannot_cross_owner_boundary(rig):
    tid = await task(rig)
    for operation in (lambda: rig.engine.pause_task(tid, OTHER), lambda: rig.engine.stop_task(tid, OTHER),
                      lambda: rig.engine.start_task(tid, OTHER), lambda: rig.engine.resolve_unknown(tid, OTHER, "retry"),
                      lambda: rig.engine.edit_task(tid, OTHER, name="foreign")):
        with pytest.raises(ValueError):
            await operation()
    assert await rig.db.fetchval("SELECT status FROM tasks WHERE id=$1", tid) == "RUNNING"


@pytest.mark.asyncio
async def test_slow_group_does_not_reserve_global_tokens_for_other_chats():
    engine = Engine(None, None, SimpleNamespace(workers=8))
    loop = asyncio.get_running_loop()
    engine.chat_next[CHAT_A] = loop.time() + 0.25
    slow = asyncio.create_task(engine.throttle(CHAT_A, "supergroup"))
    try:
        await asyncio.sleep(0)
        await asyncio.wait_for(engine.throttle(CHAT_B, "private"), timeout=0.12)
    finally:
        slow.cancel()
        await asyncio.gather(slow, return_exceptions=True)


@pytest.mark.asyncio
async def test_cooldown_waiters_reacquire_global_tokens_instead_of_bursting():
    engine = Engine(None, None, SimpleNamespace(workers=8))
    loop = asyncio.get_running_loop()
    engine.api_not_before = loop.time() + 0.04
    released = []

    async def wait(chat):
        await engine.throttle(chat, "private")
        released.append(loop.time())

    await asyncio.gather(*(wait(chat) for chat in range(5)))
    intervals = [b - a for a, b in zip(released, released[1:])]
    assert min(intervals) >= 0.04


@pytest_asyncio.fixture
async def rate_engines(db):
    # Separate pools model receiver/worker processes, with only SQL shared.
    schema = await db.fetchval("SELECT current_schema()")
    peer = Database(TEST_DSN)
    peer.pool = await asyncpg.create_pool(TEST_DSN, min_size=1, max_size=2,
                                         server_settings={"search_path": schema}, command_timeout=10)
    try:
        settings = SimpleNamespace(workers=8)
        yield Engine(db, None, settings), Engine(peer, None, settings)
    finally:
        await peer.close()


@pytest.mark.asyncio
async def test_sql_two_engines_share_global_spacing(rate_engines):
    engines = rate_engines
    released = []
    loop = asyncio.get_running_loop()

    async def wait(index):
        await engines[index % 2].throttle(90000 + index, "private")
        released.append(loop.time())

    await asyncio.wait_for(asyncio.gather(*(wait(i) for i in range(8))), timeout=3)
    released.sort()
    assert len(released) == 8
    assert min(b - a for a, b in zip(released, released[1:])) >= 0.04


@pytest.mark.asyncio
async def test_sql_two_engines_share_same_chat_spacing(rate_engines):
    first, second = rate_engines
    loop = asyncio.get_running_loop()
    await first.throttle(CHAT_A, "channel")
    started = loop.time()
    await asyncio.wait_for(second.throttle(CHAT_A, "channel"), timeout=3)
    assert loop.time() - started >= 1.0
    assert await first.db.fetchval("SELECT count(*) FROM api_rate_limits WHERE bucket=$1", f"chat:{CHAT_A}") == 1


@pytest.mark.asyncio
async def test_sql_group_wait_does_not_block_other_instance_chat(rate_engines):
    first, second = rate_engines
    await first.throttle(CHAT_A, "supergroup")
    slow = asyncio.create_task(second.throttle(CHAT_A, "supergroup"))
    try:
        await asyncio.sleep(0)
        await asyncio.wait_for(first.throttle(CHAT_B, "channel"), timeout=0.4)
        assert not slow.done()
    finally:
        slow.cancel()
        await asyncio.gather(slow, return_exceptions=True)


@pytest.mark.asyncio
async def test_sql_cooldown_is_shared_and_waiters_do_not_burst(rate_engines):
    first, second = rate_engines
    loop = asyncio.get_running_loop()
    await first.cooldown(0.1)  # API retry interval plus one-second safety margin.
    started = loop.time()
    released = []

    async def wait(engine, chat):
        await engine.throttle(chat, "private")
        released.append(loop.time())

    await asyncio.wait_for(asyncio.gather(wait(first, CHAT_A), wait(second, CHAT_B)), timeout=3)
    released.sort()
    assert released[0] - started >= 1.05
    assert released[1] - released[0] >= 0.04


@pytest.mark.asyncio
async def test_sql_later_shorter_cooldown_does_not_erase_existing_deadline(rate_engines):
    first, second = rate_engines
    await first.cooldown(0.4)
    deadline = await first.db.fetchval("SELECT next_at FROM api_rate_limits WHERE bucket='global'")
    await second.cooldown(0)
    assert await second.db.fetchval("SELECT next_at FROM api_rate_limits WHERE bucket='global'") == deadline
