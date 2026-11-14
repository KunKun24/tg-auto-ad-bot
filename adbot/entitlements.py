"""Subscription segments and permanent destination credits; no network effects."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4


UTC = timezone.utc
PLANS = {'week': (7, Decimal('10')), 'month': (30, Decimal('30')), 'quarter': (90, Decimal('66'))}
ADDONS = {'extra10': (10, Decimal('3')), 'extra50': (50, Decimal('10'))}


class AccessDenied(ValueError):
    pass


class Entitlements:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    def _admin(self, actor_id):
        if actor_id not in self.settings.admin_ids:
            raise PermissionError('仅管理员可管理会员。')

    async def _destinations(self, con, owner_id, proposed_chat_ids=None, exclude_task_id=0):
        rows = await con.fetch('''SELECT chat_id FROM user_chats WHERE owner_id=$1 AND active
          UNION SELECT tt.chat_id FROM task_targets tt JOIN tasks t ON t.id=tt.task_id
          WHERE t.owner_id=$1 AND NOT t.deleted AND tt.active AND t.id<>$2''', owner_id, exclude_task_id)
        return {r['chat_id'] for r in rows} | set(proposed_chat_ids or ())

    async def snapshot(self, owner_id, con=None):
        if con is None:
            async with self.db.pool.acquire() as connection:
                return await self.snapshot(owner_id, connection)
        enabled = bool(await con.fetchval("SELECT value FROM app_settings WHERE key='billing_enabled'"))
        moment = await con.fetchval('SELECT clock_timestamp()')
        segment = await con.fetchrow('''SELECT * FROM membership_segments WHERE owner_id=$1
          AND starts_at<=$2 AND ends_at>$2 ORDER BY starts_at DESC,id DESC LIMIT 1''', owner_id, moment)
        last_end = await con.fetchval('SELECT max(ends_at) FROM membership_segments WHERE owner_id=$1 AND ends_at>$2', owner_id, moment)
        credits = int(await con.fetchval('SELECT extra_channels FROM channel_credits WHERE owner_id=$1', owner_id) or 0)
        exempt = owner_id in self.settings.admin_ids
        active = not enabled or exempt or bool(segment)
        plan = segment['plan_code'] if segment else None
        limit = 20 + credits if enabled and not exempt and plan == 'week' else None
        return {'enabled': enabled, 'exempt': exempt, 'active': active, 'plan_code': plan,
                'starts_at': segment['starts_at'] if segment else None,
                'expires_at': segment['ends_at'] if segment else None, 'queued_until': last_end,
                'extra_channels': credits, 'channel_limit': limit,
                'used_channels': len(await self._destinations(con, owner_id))}

    async def require(self, owner_id, *, con=None, proposed_chat_ids=None, exclude_task_id=0):
        if con is None:
            async with self.db.pool.acquire() as connection:
                return await self.require(owner_id, con=connection, proposed_chat_ids=proposed_chat_ids, exclude_task_id=exclude_task_id)
        status = await con.fetchval('SELECT status FROM users WHERE id=$1', owner_id)
        if status != 'ACTIVE':
            raise AccessDenied('账号不存在或已被封禁，无法发布。')
        info = await self.snapshot(owner_id, con)
        if not info['active']:
            raise AccessDenied('会员未开通或已到期。请购买会员后手动恢复任务；内容与发送历史仍保留。')
        if info['channel_limit'] is not None:
            used = len(await self._destinations(con, owner_id, proposed_chat_ids, exclude_task_id))
            if used > info['channel_limit']:
                raise AccessDenied(f"周会员额度不足：去重目标 {used} 个，上限 {info['channel_limit']} 个。请永久移除多余绑定和任务目标，或购买增量包。")
        return info

    async def grant_order(self, con, order):
        owner_id, product = order['owner_id'], order['product_code']
        catalog = PLANS if product in PLANS else ADDONS
        if product not in catalog or Decimal(str(order['amount'])) != catalog[product][1] or order['coin'] != 'USDT':
            raise ValueError('订单商品或金额与固定套餐不一致，禁止开通。')
        inserted = await con.fetchval('''INSERT INTO payment_grants(order_id,owner_id,product_code)
          VALUES($1,$2,$3) ON CONFLICT(order_id) DO NOTHING RETURNING order_id''', order['id'], owner_id, product)
        if not inserted:
            return await con.fetchval('SELECT grant_result FROM billing_orders WHERE id=$1', order['id']) or {}
        if product in PLANS:
            moment = await con.fetchval('SELECT clock_timestamp()')
            last_end = await con.fetchval('SELECT max(ends_at) FROM membership_segments WHERE owner_id=$1', owner_id)
            starts = max(moment, last_end) if last_end else moment
            ends = starts + timedelta(days=PLANS[product][0])
            await con.execute('''INSERT INTO membership_segments(owner_id,plan_code,starts_at,ends_at,source_key)
              VALUES($1,$2,$3,$4,$5)''', owner_id, product, starts, ends, 'order:' + order['unique_id'])
            result = {'plan_code': product, 'starts_at': starts.isoformat(), 'ends_at': ends.isoformat(),
                      'queued': starts > moment, 'manual_resume': True}
        else:
            extra = await con.fetchval('''INSERT INTO channel_credits(owner_id,extra_channels) VALUES($1,$2)
              ON CONFLICT(owner_id) DO UPDATE SET extra_channels=channel_credits.extra_channels+EXCLUDED.extra_channels,
              updated_at=now() RETURNING extra_channels''', owner_id, ADDONS[product][0])
            result = {'extra_channels': extra, 'added': ADDONS[product][0], 'permanent': True, 'manual_resume': True}
        await con.execute('UPDATE billing_orders SET grant_result=$2 WHERE id=$1', order['id'], result)
        await con.execute("INSERT INTO audit_logs(actor_id,action,resource_type,resource_id,details) VALUES($1,'PAYMENT_GRANT','billing_order',$2,$3)", owner_id, str(order['id']), result)
        return result

    async def adjust(self, admin_id, owner_id, days=0, extra_channels=0, reason=''):
        self._admin(admin_id)
        if not reason.strip() or len(reason) > 300 or not isinstance(days, int) or not isinstance(extra_channels, int):
            raise ValueError('请输入明确调整原因与整数天数/额度。')
        if abs(days) > 3650 or abs(extra_channels) > 100000 or (days == 0 and extra_channels == 0):
            raise ValueError('调整范围不正确。')
        async with self.db.pool.acquire() as con:
            async with con.transaction():
                await con.execute('SELECT pg_advisory_xact_lock($1)', owner_id)
                if not await con.fetchval('SELECT 1 FROM users WHERE id=$1', owner_id):
                    raise ValueError('该用户尚未启动机器人。')
                moment = await con.fetchval('SELECT clock_timestamp()')
                if days > 0:
                    last_end = await con.fetchval('SELECT max(ends_at) FROM membership_segments WHERE owner_id=$1', owner_id)
                    starts = max(moment, last_end) if last_end else moment
                    await con.execute('''INSERT INTO membership_segments(owner_id,plan_code,starts_at,ends_at,source_key)
                      VALUES($1,'month',$2,$3,$4)''', owner_id, starts, starts + timedelta(days=days), 'admin:' + uuid4().hex)
                elif days < 0:
                    remaining = -days * 86400
                    segments = await con.fetch('''SELECT * FROM membership_segments WHERE owner_id=$1 AND ends_at>$2
                      ORDER BY ends_at DESC,id DESC FOR UPDATE''', owner_id, moment)
                    for segment in segments:
                        floor = max(moment, segment['starts_at'])
                        available = (segment['ends_at'] - floor).total_seconds()
                        removed = min(available, remaining)
                        end = segment['ends_at'] - timedelta(seconds=removed)
                        if end <= segment['starts_at']:
                            await con.execute('DELETE FROM membership_segments WHERE id=$1', segment['id'])
                        else:
                            await con.execute('UPDATE membership_segments SET ends_at=$2 WHERE id=$1', segment['id'], end)
                        remaining -= removed
                        if remaining <= 0:
                            break
                if extra_channels:
                    old = int(await con.fetchval('SELECT extra_channels FROM channel_credits WHERE owner_id=$1', owner_id) or 0)
                    if old + extra_channels < 0:
                        raise ValueError('永久增量额度不能小于0。')
                    await con.execute('''INSERT INTO channel_credits(owner_id,extra_channels) VALUES($1,$2)
                      ON CONFLICT(owner_id) DO UPDATE SET extra_channels=EXCLUDED.extra_channels,updated_at=now()''', owner_id, old + extra_channels)
                await con.execute("INSERT INTO audit_logs(actor_id,action,resource_type,resource_id,details) VALUES($1,'MEMBERSHIP_ADJUST','user',$2,$3)", admin_id, str(owner_id), {'days': days, 'extra_channels': extra_channels, 'reason': reason})
                result = await self.snapshot(owner_id, con)
        await self.enforce(owner_id)
        return result

    async def _pause(self, con, task, reason):
        wait = max(0, int((task['next_run_at'] - datetime.now(UTC)).total_seconds())) if task['next_run_at'] else task['remaining_seconds']
        await con.execute("UPDATE tasks SET status='PAUSED',next_run_at=NULL,remaining_seconds=$2,last_error=$3,updated_at=now() WHERE id=$1", task['id'], wait, reason[:700])
        # Instant posts from an expired subscription are not accumulated for later catch-up.
        await con.execute("UPDATE publication_events SET status='CANCELLED',finished_at=now() WHERE task_id=$1 AND kind='INSTANT' AND status='PENDING' AND started_at IS NULL", task['id'])
        await con.execute("INSERT INTO audit_logs(actor_id,action,resource_type,resource_id,details) VALUES($1,'MEMBERSHIP_TASK_PAUSE','task',$2,$3)", task['owner_id'], str(task['id']), {'reason': reason[:700]})

    async def set_enabled(self, admin_id, enabled):
        self._admin(admin_id)
        if not isinstance(enabled, bool):
            raise ValueError('收费开关必须为布尔值。')
        async with self.db.pool.acquire() as con:
            async with con.transaction():
                await con.execute('SELECT pg_advisory_xact_lock(7150000005)')
                owners = await con.fetch("SELECT DISTINCT owner_id FROM tasks WHERE status='RUNNING' AND NOT deleted AND NOT(owner_id=ANY($1::bigint[])) ORDER BY owner_id", list(self.settings.admin_ids)) if enabled else []
                for owner in owners:
                    await con.execute('SELECT pg_advisory_xact_lock($1)', owner['owner_id'])
                await con.execute("INSERT INTO app_settings(key,value) VALUES('billing_enabled',$1) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()", enabled)
                tasks = await con.fetch("SELECT * FROM tasks WHERE status='RUNNING' AND NOT deleted AND NOT(owner_id=ANY($1::bigint[])) ORDER BY owner_id,id FOR UPDATE", list(self.settings.admin_ids)) if enabled else []
                for task in tasks:
                    await self._pause(con, task, '收费模式已启用，原免费任务暂停。付款开通后请主动检查并恢复任务。')
                await con.execute("INSERT INTO audit_logs(actor_id,action,resource_type,details) VALUES($1,'BILLING_SWITCH','system',$2)", admin_id, {'enabled': enabled, 'paused_tasks': len(tasks)})
        return {'enabled': enabled, 'paused_tasks': len(tasks)}

    async def enforce(self, owner_id=None):
        # Owner lock precedes task locks everywhere, including task creation/start.
        owners = [owner_id] if owner_id is not None else [r['owner_id'] for r in await self.db.fetch("SELECT DISTINCT owner_id FROM tasks WHERE status='RUNNING' AND NOT deleted")]
        paused = 0
        for owner in owners:
            async with self.db.pool.acquire() as con:
                async with con.transaction():
                    await con.execute('SELECT pg_advisory_xact_lock($1)', owner)
                    try:
                        await self.require(owner, con=con)
                    except AccessDenied as exc:
                        tasks = await con.fetch("SELECT * FROM tasks WHERE owner_id=$1 AND status='RUNNING' AND NOT deleted ORDER BY id FOR UPDATE", owner)
                        for task in tasks:
                            await self._pause(con, task, str(exc))
                            paused += 1
        return paused
