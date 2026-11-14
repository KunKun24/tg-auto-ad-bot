"""Offline membership regressions using random schemas in auto_ad_test only."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio

from adbot.engine import Engine
from adbot.entitlements import ADDONS, PLANS, AccessDenied
from test_reliability import (
    ADMIN, CHAT_A, CHAT_B, OTHER, OWNER, SOURCE, MockBot, content, db,
    process, source, source_message, task,
)

ADMIN_TWO = 11004


@pytest_asyncio.fixture
async def rig(db):
    for uid in (OWNER, OTHER, ADMIN, ADMIN_TWO):
        await db.execute("INSERT INTO users(id,first_name) VALUES($1,$2)", uid, f"User {uid}")
    for chat in (CHAT_A, CHAT_B, SOURCE):
        await db.execute("INSERT INTO chats(chat_id,type,title) VALUES($1,'channel',$2)", chat, str(chat))
        for uid in (OWNER, OTHER):
            await db.execute("INSERT INTO user_chats(owner_id,chat_id) VALUES($1,$2)", uid, chat)
    bot = MockBot()
    engine = Engine(db, bot, SimpleNamespace(workers=8, admin_ids=(ADMIN, ADMIN_TWO)))

    async def no_wait(*_args, **_kwargs):
        pass

    engine.throttle = no_wait
    credential = await db.fetchval(
        "INSERT INTO payment_credentials(merchant_id,token_encrypted,created_by) "
        "VALUES('offline','not-a-real-credential',$1) RETURNING id", ADMIN)
    return SimpleNamespace(db=db, bot=bot, engine=engine, ent=engine.entitlements, credential=credential)


async def enabled(rig, value=True):
    await rig.db.execute("UPDATE app_settings SET value=$1 WHERE key='billing_enabled'", value)


async def segment(rig, plan='week', owner=OWNER):
    return await rig.db.fetchrow(
        "INSERT INTO membership_segments(owner_id,plan_code,starts_at,ends_at,source_key) "
        "VALUES($1,$2,clock_timestamp()-interval '1 second',clock_timestamp()+interval '1 day',$3) RETURNING *",
        owner, plan, 'test:' + uuid4().hex)


async def expire(rig, owner=OWNER):
    await rig.db.execute(
        "UPDATE membership_segments SET starts_at=now()-interval '2 days',ends_at=now()-interval '1 day' WHERE owner_id=$1", owner)


async def billing_order(rig, product='week', owner=OWNER):
    amount = (PLANS if product in PLANS else ADDONS)[product][1]
    return await rig.db.fetchrow(
        "INSERT INTO billing_orders(owner_id,unique_id,product_code,amount,status,credential_id,request_key) "
        "VALUES($1,$2,$3,$4,'PENDING',$5,$6) RETURNING *",
        owner, 'offline-' + uuid4().hex, product, amount, rig.credential, uuid4().hex)


async def grant(rig, order):
    async with rig.db.pool.acquire() as con:
        async with con.transaction():
            await con.execute('SELECT pg_advisory_xact_lock($1)', order['owner_id'])
            current = await con.fetchrow('SELECT * FROM billing_orders WHERE id=$1 FOR UPDATE', order['id'])
            return await rig.ent.grant_order(con, current)


async def extra_destinations(rig, count, owner=OWNER, bind=True):
    chats = [-10050000 - index for index in range(count)]
    for chat in chats:
        await rig.db.execute("INSERT INTO chats(chat_id,type,title) VALUES($1,'channel','quota') ON CONFLICT DO NOTHING", chat)
        if bind:
            await rig.db.execute('INSERT INTO user_chats(owner_id,chat_id) VALUES($1,$2) ON CONFLICT DO NOTHING', owner, chat)
    return chats


@pytest.mark.asyncio
async def test_grants_queue_7_30_90_day_segments_without_overlap_or_auto_resume(rig):
    tid = await task(rig, start=False)
    await rig.db.execute("UPDATE tasks SET status='PAUSED',remaining_seconds=99 WHERE id=$1", tid)
    orders = [await billing_order(rig, product) for product in ('week', 'month', 'quarter')]
    results = [await grant(rig, order) for order in orders]
    rows = await rig.db.fetch('SELECT * FROM membership_segments ORDER BY starts_at,id')
    assert [row['plan_code'] for row in rows] == ['week', 'month', 'quarter']
    assert [row['ends_at'] - row['starts_at'] for row in rows] == [timedelta(days=days) for days in (7, 30, 90)]
    assert rows[0]['ends_at'] == rows[1]['starts_at'] and rows[1]['ends_at'] == rows[2]['starts_at']
    assert not results[0]['queued'] and results[1]['queued'] and results[2]['queued']
    assert all(result['manual_resume'] for result in results)
    assert [row['source_key'] for row in rows] == ['order:' + order['unique_id'] for order in orders]
    info = await rig.ent.snapshot(OWNER)
    assert info['plan_code'] == 'week' and info['queued_until'] == rows[-1]['ends_at']
    state = await rig.db.fetchrow('SELECT status,remaining_seconds FROM tasks WHERE id=$1', tid)
    assert state['status'] == 'PAUSED' and state['remaining_seconds'] == 99


@pytest.mark.asyncio
async def test_expired_renewal_starts_now_and_repeat_grant_is_idempotent(rig):
    await segment(rig)
    await expire(rig)
    before = await rig.db.fetchval('SELECT clock_timestamp()')
    order = await billing_order(rig, 'month')
    first = await grant(rig, order)
    second = await grant(rig, order)
    row = await rig.db.fetchrow("SELECT * FROM membership_segments WHERE source_key=$1", 'order:' + order['unique_id'])
    assert row['starts_at'] >= before and row['ends_at'] - row['starts_at'] == timedelta(days=30)
    assert first == second and not first['queued']
    assert await rig.db.fetchval('SELECT count(*) FROM payment_grants') == 1
    assert await rig.db.fetchval("SELECT count(*) FROM audit_logs WHERE action='PAYMENT_GRANT'") == 1


@pytest.mark.asyncio
async def test_permanent_addons_remain_after_expiry_and_only_grant_once(rig):
    await enabled(rig)
    await segment(rig)
    orders = [await billing_order(rig, product) for product in ('extra10', 'extra50')]
    for order in orders:
        first = await grant(rig, order)
        assert first['permanent'] and first['manual_resume']
        assert await grant(rig, order) == first
    assert (await rig.ent.snapshot(OWNER))['channel_limit'] == 80
    await expire(rig)
    info = await rig.ent.snapshot(OWNER)
    assert info['extra_channels'] == 60 and not info['active']
    with pytest.raises(AccessDenied):
        await rig.ent.require(OWNER)
    await grant(rig, await billing_order(rig, 'week'))
    assert (await rig.ent.require(OWNER))['channel_limit'] == 80


@pytest.mark.asyncio
async def test_destination_union_counts_unbound_draft_stopped_but_not_deleted_or_inactive(rig):
    # Existing bindings contribute three distinct destinations.
    chats = await extra_destinations(rig, 4, bind=False)
    for status, targets, deleted in (
        ('DRAFT', (CHAT_A, chats[0], chats[1]), False),
        ('STOPPED', (chats[1], chats[2]), False),
        ('STOPPED', (chats[3],), True),
    ):
        tid = await rig.db.fetchval("INSERT INTO tasks(owner_id,name,interval_seconds,status,deleted) VALUES($1,'count',60,$2,$3) RETURNING id", OWNER, status, deleted)
        for chat in targets:
            await rig.db.execute('INSERT INTO task_targets(task_id,chat_id) VALUES($1,$2)', tid, chat)
    await rig.db.execute('INSERT INTO user_chats(owner_id,chat_id,active) VALUES($1,$2,false)', OWNER, chats[3])
    assert (await rig.ent.snapshot(OWNER))['used_channels'] == 6
    await rig.db.execute('UPDATE task_targets SET active=false WHERE chat_id=$1', chats[2])
    assert (await rig.ent.snapshot(OWNER))['used_channels'] == 5
    async with rig.db.pool.acquire() as con:
        unique = await rig.ent._destinations(con, OWNER, proposed_chat_ids=[CHAT_A, chats[0], chats[3], chats[3]])
    assert len(unique) == 6


@pytest.mark.asyncio
async def test_only_adding_targets_checks_quota_cleanup_and_metadata_stay_available(rig):
    tid = await task(rig, chats=(CHAT_A, CHAT_B), start=False)
    await enabled(rig)
    await segment(rig)
    await extra_destinations(rig, 18)
    assert (await rig.ent.snapshot(OWNER))['used_channels'] == 21
    await rig.engine.edit_task(tid, OWNER, name='cleanup still possible')
    await rig.db.execute('UPDATE user_chats SET active=false WHERE owner_id=$1 AND chat_id=$2', OWNER, CHAT_B)
    await rig.engine.edit_task(tid, OWNER, chat_ids=[CHAT_A])
    assert await rig.db.fetchval('SELECT count(*) FROM task_targets WHERE task_id=$1 AND active', tid) == 1
    with pytest.raises(AccessDenied):
        await rig.engine.edit_task(tid, OWNER, chat_ids=[CHAT_A, CHAT_B])
    await expire(rig)
    await rig.engine.edit_task(tid, OWNER, name='expired cleanup')
    await rig.engine.edit_task(tid, OWNER, chat_ids=[])
    assert not await rig.db.fetchval('SELECT 1 FROM task_targets WHERE task_id=$1 AND active', tid)
    with pytest.raises(AccessDenied):
        await rig.engine.edit_task(tid, OWNER, chat_ids=[CHAT_A])


@pytest.mark.asyncio
async def test_month_to_week_downgrade_over_limit_blocks_all_sending_without_truncation(rig):
    chats = await extra_destinations(rig, 18)
    tid = await task(rig, chats=(CHAT_A, CHAT_B), delete_previous=True)
    await rig.db.execute('UPDATE task_targets SET last_message_id=88 WHERE task_id=$1', tid)
    await enabled(rig)
    await segment(rig, 'month')
    assert (await rig.ent.require(OWNER))['channel_limit'] is None
    # Simulate the scheduled unlimited-to-week transition, not a partial target truncation.
    await expire(rig)
    await segment(rig, 'week')
    with pytest.raises(AccessDenied):
        await rig.ent.require(OWNER)
    await process(rig, tid)
    assert not rig.bot.sent and not rig.bot.deleted
    assert await rig.db.fetchval('SELECT status FROM tasks WHERE id=$1', tid) == 'PAUSED'
    assert await rig.db.fetchval('SELECT count(*) FROM user_chats WHERE owner_id=$1 AND active', OWNER) == len(chats) + 3
    assert await rig.db.fetchval('SELECT count(*) FROM task_targets WHERE task_id=$1 AND active', tid) == 2
    assert await rig.db.fetchval('SELECT count(*) FROM deliveries WHERE status<>\'PENDING\'') == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('admin', [ADMIN, ADMIN_TWO])
async def test_both_admins_exempt_but_ban_still_denies_and_free_mode_does_not_bypass_ban(rig, admin):
    await enabled(rig)
    info = await rig.ent.require(admin)
    assert info['active'] and info['exempt'] and info['channel_limit'] is None
    await rig.db.execute("UPDATE users SET status='BANNED' WHERE id=$1", admin)
    with pytest.raises(AccessDenied):
        await rig.ent.require(admin)
    await enabled(rig, False)
    with pytest.raises(AccessDenied):
        await rig.ent.require(admin)


@pytest.mark.asyncio
async def test_enable_pauses_all_ordinary_tasks_even_paid_and_preserves_admins(rig):
    tids = {}
    for owner in (OWNER, OTHER, ADMIN, ADMIN_TWO):
        tids[owner] = await rig.db.fetchval(
            "INSERT INTO tasks(owner_id,name,interval_seconds,status,next_run_at) VALUES($1,'switch',60,'RUNNING',now()+interval '120 seconds') RETURNING id", owner)
    await segment(rig, 'month', OWNER)
    result = await rig.ent.set_enabled(ADMIN, True)
    assert result == {'enabled': True, 'paused_tasks': 2}
    rows = {row['owner_id']: row for row in await rig.db.fetch('SELECT * FROM tasks')}
    for owner in (OWNER, OTHER):
        assert rows[owner]['status'] == 'PAUSED' and rows[owner]['next_run_at'] is None
        assert 115 <= rows[owner]['remaining_seconds'] <= 120
    for admin in (ADMIN, ADMIN_TWO):
        assert rows[admin]['status'] == 'RUNNING'
    await rig.ent.set_enabled(ADMIN_TWO, False)
    assert await rig.db.fetchval("SELECT count(*) FROM tasks WHERE status='PAUSED'") == 2
    assert (await rig.ent.require(OTHER))['active']


@pytest.mark.asyncio
async def test_enable_switch_and_task_pauses_rollback_together(rig, monkeypatch):
    for owner in (OWNER, OTHER):
        await rig.db.execute("INSERT INTO tasks(owner_id,name,interval_seconds,status) VALUES($1,'rollback',60,'RUNNING')", owner)
    original = rig.ent._pause
    paused = 0

    async def fail_second(con, current, reason):
        nonlocal paused
        paused += 1
        await original(con, current, reason)
        if paused == 2:
            raise RuntimeError('offline rollback injection')

    monkeypatch.setattr(rig.ent, '_pause', fail_second)
    with pytest.raises(RuntimeError, match='offline rollback injection'):
        await rig.ent.set_enabled(ADMIN, True)
    assert not await rig.db.fetchval("SELECT value FROM app_settings WHERE key='billing_enabled'")
    assert await rig.db.fetchval("SELECT count(*) FROM tasks WHERE status='RUNNING'") == 2
    assert await rig.db.fetchval("SELECT count(*) FROM audit_logs WHERE action='MEMBERSHIP_TASK_PAUSE'") == 0


@pytest.mark.asyncio
async def test_expiry_cancels_unstarted_instant_and_never_auto_resumes_after_renewal(rig):
    sid = await source(rig)
    tid = await task(rig, instant_source_id=sid)
    await rig.engine.ingest_source(source_message(), 1)
    await enabled(rig)
    await segment(rig)
    await expire(rig)
    assert await rig.ent.enforce(OWNER) == 1
    row = await rig.db.fetchrow("SELECT * FROM publication_events WHERE task_id=$1 AND kind='INSTANT'", tid)
    assert row['status'] == 'CANCELLED' and row['finished_at'] is not None
    assert await rig.db.fetchval('SELECT count(*) FROM task_items WHERE task_id=$1', tid) == 1
    assert await rig.db.fetchval('SELECT count(*) FROM deliveries') == 2
    assert await rig.ent.enforce(OWNER) == 0
    await grant(rig, await billing_order(rig, 'month'))
    assert await rig.ent.enforce(OWNER) == 0
    assert await rig.db.fetchval('SELECT status FROM tasks WHERE id=$1', tid) == 'PAUSED'
    await rig.engine.start_task(tid, OWNER)
    assert await rig.db.fetchval('SELECT status FROM tasks WHERE id=$1', tid) == 'RUNNING'


@pytest.mark.asyncio
@pytest.mark.parametrize('with_previous', [False, True])
async def test_expiry_while_waiting_for_first_throttle_prevents_delete_and_send(rig, with_previous):
    tid = await task(rig, delete_previous=with_previous)
    await enabled(rig)
    await segment(rig)
    if with_previous:
        await rig.db.execute('UPDATE task_targets SET last_message_id=77 WHERE task_id=$1', tid)

    async def expire_during_wait(*_args):
        await expire(rig)

    rig.engine.throttle = expire_during_wait
    await process(rig, tid)
    assert not rig.bot.sent and not rig.bot.deleted
    assert await rig.db.fetchval('SELECT status FROM tasks WHERE id=$1', tid) == 'PAUSED'
    row = await rig.db.fetchrow('SELECT status,attempts FROM deliveries')
    assert row['status'] == 'PENDING' and row['attempts'] == 0


@pytest.mark.asyncio
async def test_expiry_after_successful_delete_preserves_delete_evidence_but_never_sends(rig):
    tid = await task(rig, delete_previous=True)
    await enabled(rig)
    await segment(rig)
    await rig.db.execute('UPDATE task_targets SET last_message_id=77 WHERE task_id=$1', tid)
    count = 0

    async def expire_after_delete(*_args):
        nonlocal count
        count += 1
        if count == 2:
            await expire(rig)

    rig.engine.throttle = expire_after_delete
    await process(rig, tid)
    assert rig.bot.deleted == [(CHAT_A, 77)] and not rig.bot.sent
    row = await rig.db.fetchrow('SELECT status,delete_status FROM deliveries')
    assert row['status'] == 'PENDING' and row['delete_status'] == 'SUCCESS'
    assert await rig.db.fetchval('SELECT status FROM tasks WHERE id=$1', tid) == 'PAUSED'
    assert await rig.db.fetchval("SELECT count(*) FROM message_logs WHERE operation='DELETE' AND status='SUCCESS'") == 1


@pytest.mark.asyncio
async def test_owner_lock_serializes_grants_and_task_creation(rig):
    cid = await content(rig)
    async with rig.db.pool.acquire() as con:
        transaction = con.transaction()
        await transaction.start()
        await con.execute('SELECT pg_advisory_xact_lock($1)', OWNER)
        creating = asyncio.create_task(rig.engine.create_task(OWNER, 'locked', [cid], [CHAT_A], 60))
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(creating), timeout=0.05)
            assert await rig.db.fetchval('SELECT count(*) FROM tasks') == 0
        finally:
            await transaction.rollback()
        assert await creating
    orders = [await billing_order(rig, code) for code in ('week', 'month', 'quarter')]
    results = await asyncio.gather(*(grant(rig, order) for order in orders))
    assert len(results) == 3
    rows = await rig.db.fetch('SELECT starts_at,ends_at FROM membership_segments ORDER BY starts_at')
    assert len(rows) == 3 and all(a['ends_at'] == b['starts_at'] for a, b in zip(rows, rows[1:]))
    assert sum((row['ends_at'] - row['starts_at']).days for row in rows) == 127


@pytest.mark.asyncio
async def test_owner_lock_prevents_membership_enforcement_racing_grant(rig):
    tid = await task(rig)
    await enabled(rig)
    async with rig.db.pool.acquire() as con:
        transaction = con.transaction()
        await transaction.start()
        await con.execute('SELECT pg_advisory_xact_lock($1)', OWNER)
        enforcing = asyncio.create_task(rig.ent.enforce(OWNER))
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(enforcing), timeout=0.05)
            await con.execute(
                "INSERT INTO membership_segments(owner_id,plan_code,starts_at,ends_at,source_key) "
                "VALUES($1,'month',now()-interval '1 second',now()+interval '30 days',$2)", OWNER, 'locked:' + uuid4().hex)
        finally:
            await transaction.commit()
        assert await enforcing == 0
    assert await rig.db.fetchval('SELECT status FROM tasks WHERE id=$1', tid) == 'RUNNING'
