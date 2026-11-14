from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError

from .content import capture_message, deliver
from .db import audit
from .entitlements import AccessDenied, Entitlements

log = logging.getLogger(__name__)
UTC = timezone.utc


def now():
    return datetime.now(UTC)


def safe_error(exc):
    import re
    return re.sub(r'\d{6,}:[\w-]{25,}', '[TOKEN]', str(exc))[:700]


class Engine:
    def __init__(self, db, bot, settings):
        self.db, self.bot, self.settings = db, bot, settings
        self.entitlements = Entitlements(db, settings)
        self._billing_last_enforce = 0.0
        self.global_gate = asyncio.Lock()
        self.chat_gates = {}
        self.global_next = 0.0
        self.chat_next = {}
        self.api_not_before = 0.0
        self.semaphore = asyncio.Semaphore(settings.workers)

    async def setting(self, key, default):
        value = await self.db.fetchval('SELECT value FROM app_settings WHERE key=$1', key)
        return default if value is None else value

    async def throttle(self, chat_id, chat_type='channel'):
        if self.db is not None:
            await self._shared_throttle(chat_id, chat_type)
            return
        lock = self.chat_gates.setdefault(chat_id, asyncio.Lock())
        async with lock:
            loop = asyncio.get_running_loop()
            delay = self.chat_next.get(chat_id, 0) - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            # Wait for chat eligibility before claiming a global token. Cooldown
            # waiters re-enter this loop so they cannot all burst on wakeup.
            while True:
                async with self.global_gate:
                    moment = loop.time()
                    delay = max(self.global_next, self.api_not_before) - moment
                    if delay <= 0:
                        self.global_next = moment + 0.05
                        self.chat_next[chat_id] = moment + (3.1 if chat_type in ('group','supergroup') else 1.05)
                        return
                await asyncio.sleep(delay)

    async def _shared_throttle(self, chat_id, chat_type):
        # The receiver and Worker share the same free Bot API budget. Never
        # reserve a global slot while a slow group's own bucket is still closed.
        bucket=f'chat:{chat_id}'
        spacing=3.1 if chat_type in ('group','supergroup') else 1.05
        while True:
            async with self.db.pool.acquire() as con:
                async with con.transaction():
                    await con.execute("INSERT INTO api_rate_limits(bucket) VALUES($1) ON CONFLICT DO NOTHING",bucket)
                    chat_next=await con.fetchval('SELECT next_at FROM api_rate_limits WHERE bucket=$1 FOR UPDATE',bucket)
                    moment=await con.fetchval('SELECT clock_timestamp()')
                    delay=(chat_next-moment).total_seconds()
                    if delay<=0:
                        global_next=await con.fetchval("SELECT next_at FROM api_rate_limits WHERE bucket='global' FOR UPDATE")
                        moment=await con.fetchval('SELECT clock_timestamp()')
                        delay=(global_next-moment).total_seconds()
                        if delay<=0:
                            await con.execute("UPDATE api_rate_limits SET next_at=$2 WHERE bucket=$1",bucket,moment+timedelta(seconds=spacing))
                            await con.execute("UPDATE api_rate_limits SET next_at=$1 WHERE bucket='global'",moment+timedelta(seconds=0.05))
                            return
            await asyncio.sleep(max(0.005,delay))

    async def cooldown(self, seconds):
        self.api_not_before=asyncio.get_running_loop().time()+seconds+1
        if self.db is not None:
            await self.db.execute("UPDATE api_rate_limits SET next_at=greatest(next_at,$1) WHERE bucket='global'",now()+timedelta(seconds=seconds+1))

    async def check_bot(self, chat_id):
        member = await self.bot.get_chat_member(chat_id, self.bot.id)
        if member.status not in ('administrator','creator'):
            raise ValueError('机器人不是目标管理员，请重新添加并授权')
        chat_type = await self.db.fetchval('SELECT type FROM chats WHERE chat_id=$1', chat_id)
        if chat_type == 'channel' and member.status != 'creator' and not getattr(member, 'can_post_messages', False):
            raise ValueError('机器人没有频道发布权限')

    async def _owned(self, con, task_id, owner_id, lock=True):
        row = await con.fetchrow('SELECT * FROM tasks WHERE id=$1 AND owner_id=$2 AND NOT deleted' + (' FOR UPDATE' if lock else ''), task_id, owner_id)
        if not row:
            raise ValueError('任务不存在或不属于你')
        return row

    async def _validate(self, con, owner_id, content_ids, chat_ids, interval_seconds, source_id, exclude_task_id=0):
        await con.execute('SELECT pg_advisory_xact_lock(7150000004)')
        if not content_ids or not chat_ids:
            raise ValueError('至少选择一条内容和一个目标')
        if len(set(content_ids)) != len(content_ids):
            raise ValueError('轮播内容重复')
        minimum = await con.fetchval("SELECT value FROM app_settings WHERE key='min_interval_seconds'") or 60
        limit = await con.fetchval("SELECT value FROM app_settings WHERE key='max_targets_per_task'") or 1000
        if interval_seconds < int(minimum):
            raise ValueError(f'间隔不能小于 {minimum} 秒')
        if len(chat_ids) > int(limit):
            raise ValueError(f'每任务最多 {limit} 个目标')
        count = await con.fetchval('SELECT count(*) FROM contents WHERE id=ANY($1::bigint[]) AND owner_id=$2 AND NOT deleted AND current_version_id IS NOT NULL', content_ids, owner_id)
        if count != len(content_ids):
            raise ValueError('内容已被删除、无版本或不属于你')
        count = await con.fetchval('SELECT count(*) FROM user_chats WHERE owner_id=$1 AND chat_id=ANY($2::bigint[]) AND active', owner_id, chat_ids)
        if count != len(set(chat_ids)):
            raise ValueError('目标尚未绑定或已解绑，请重新绑定')
        if source_id is not None:
            source = await con.fetchrow('SELECT * FROM sources WHERE id=$1 AND owner_id=$2 AND active', source_id, owner_id)
            if not source:
                raise ValueError('来源尚未授权或已解绑')
            if source['chat_id'] in chat_ids:
                raise ValueError('来源不能同时作为目标，避免触发回环')
            edges = await con.fetch('SELECT s.chat_id src,tt.chat_id dst FROM tasks t JOIN sources s ON s.id=t.instant_source_id JOIN task_targets tt ON tt.task_id=t.id WHERE NOT t.deleted AND tt.active AND t.id<>$1', exclude_task_id)
            graph = {}
            for edge in edges:
                graph.setdefault(edge['src'], set()).add(edge['dst'])
            for dest in chat_ids:
                todo, seen = [dest], set()
                while todo:
                    node = todo.pop()
                    if node == source['chat_id']:
                        raise ValueError('此来源与目标会形成跨任务循环，不能启用')
                    if node not in seen:
                        seen.add(node)
                        todo.extend(graph.get(node, ()))

    async def create_task(self, owner_id, name, content_ids, chat_ids, interval_seconds, delete_previous=False, instant_source_id=None, source_mode='copy', max_runs=0, skip_failed=False, delete_failure_pause=False):
        if source_mode not in ('copy','forward') or max_runs < 0:
            raise ValueError('任务参数不正确')
        async with self.db.pool.acquire() as con:
            async with con.transaction():
                await con.execute('SELECT pg_advisory_xact_lock($1)', owner_id)
                user = await con.fetchrow('SELECT status FROM users WHERE id=$1', owner_id)
                if not user or user['status'] != 'ACTIVE':
                    raise ValueError('用户不可使用任务')
                limit = await con.fetchval("SELECT value FROM app_settings WHERE key='max_tasks_per_user'") or 100
                if await con.fetchval('SELECT count(*) FROM tasks WHERE owner_id=$1 AND NOT deleted', owner_id) >= int(limit):
                    raise ValueError(f'任务数量达到上限 {limit}')
                await self.entitlements.require(owner_id, con=con, proposed_chat_ids=chat_ids)
                await self._validate(con, owner_id, content_ids, chat_ids, interval_seconds, instant_source_id)
                task_id = await con.fetchval('INSERT INTO tasks(owner_id,name,interval_seconds,delete_previous,instant_source_id,source_mode,max_runs,skip_failed,delete_failure_pause) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id', owner_id, name[:120], interval_seconds, delete_previous, instant_source_id, source_mode, max_runs, skip_failed, delete_failure_pause)
                await con.executemany('INSERT INTO task_items(task_id,position,content_id) VALUES($1,$2,$3)', [(task_id,i,c) for i,c in enumerate(content_ids)])
                await con.executemany('INSERT INTO task_targets(task_id,chat_id) VALUES($1,$2)', [(task_id,c) for c in set(chat_ids)])
        await audit(self.db, owner_id, 'TASK_CREATE', 'task', task_id)
        return task_id

    async def start_task(self, task_id, owner_id):
        await self.entitlements.require(owner_id)
        task = await self.db.fetchrow('SELECT * FROM tasks WHERE id=$1 AND owner_id=$2 AND NOT deleted', task_id, owner_id)
        if not task:
            raise ValueError('任务不存在')
        user = await self.db.fetchrow('SELECT * FROM users WHERE id=$1', owner_id)
        if not user or user['status'] != 'ACTIVE':
            raise ValueError('用户已被封禁')
        targets = await self.db.fetch('SELECT chat_id FROM task_targets WHERE task_id=$1 AND active', task_id)
        for target in targets:
            await self.check_bot(target['chat_id'])
        async with self.db.pool.acquire() as con:
            async with con.transaction():
                await con.execute('SELECT pg_advisory_xact_lock($1)', owner_id)
                await self.entitlements.require(owner_id, con=con)
                task = await self._owned(con,task_id,owner_id)
                if await con.fetchval("SELECT count(*) FROM deliveries d JOIN publication_events e ON e.id=d.event_id WHERE e.task_id=$1 AND d.status='UNKNOWN'", task_id):
                    raise ValueError('请先处理发送结果未知的记录')
                if task['status'] == 'RUNNING':
                    return
                if task['status'] == 'COMPLETED' or (task['max_runs'] > 0 and task['completed_runs'] >= task['max_runs']):
                    raise ValueError('有限轮次任务已完成，请新建或修改任务')
                # Explicit user resume authorizes retry of known failures, never unknown.
                await con.execute("UPDATE deliveries d SET status='RETRY',attempts=0,next_attempt_at=now() FROM publication_events e WHERE e.id=d.event_id AND e.task_id=$1 AND e.status IN ('PENDING','RUNNING','ERROR') AND d.status='FAILED'", task_id)
                await con.execute("UPDATE publication_events SET status='PENDING' WHERE task_id=$1 AND status='ERROR'", task_id)
                wait = task['remaining_seconds'] if task['status']=='PAUSED' else 0
                await con.execute("UPDATE tasks SET status='RUNNING',next_run_at=$2,last_error='',updated_at=now() WHERE id=$1", task_id, now()+timedelta(seconds=wait))
        await audit(self.db,owner_id,'TASK_START','task',task_id)

    async def pause_task(self, task_id, owner_id):
        async with self.db.pool.acquire() as con:
            async with con.transaction():
                task=await self._owned(con,task_id,owner_id)
                wait=max(0,int((task['next_run_at']-now()).total_seconds())) if task['next_run_at'] else 0
                await con.execute("UPDATE tasks SET status='PAUSED',remaining_seconds=$2,next_run_at=NULL,updated_at=now() WHERE id=$1",task_id,wait)
                await con.execute("UPDATE publication_events SET status='CANCELLED',finished_at=now() WHERE task_id=$1 AND kind='INSTANT' AND status='PENDING' AND started_at IS NULL",task_id)
        await audit(self.db,owner_id,'TASK_PAUSE','task',task_id)

    async def stop_task(self, task_id, owner_id):
        async with self.db.pool.acquire() as con:
            async with con.transaction():
                await self._owned(con,task_id,owner_id)
                if await con.fetchval("SELECT 1 FROM deliveries d JOIN publication_events e ON e.id=d.event_id WHERE e.task_id=$1 AND d.status IN ('SENDING','UNKNOWN') LIMIT 1", task_id):
                    raise ValueError('存在在途或结果未知记录，请先暂停，等待返回并核对后再停止')
                await con.execute("UPDATE tasks SET status='STOPPED',next_run_at=NULL,remaining_seconds=0,updated_at=now() WHERE id=$1",task_id)
                await con.execute("UPDATE publication_events SET status='CANCELLED',finished_at=now() WHERE task_id=$1 AND status IN ('PENDING','RUNNING','ERROR')",task_id)
        await audit(self.db,owner_id,'TASK_STOP','task',task_id)

    async def delete_task(self, task_id, owner_id):
        await self.stop_task(task_id,owner_id)
        await self.db.execute('UPDATE tasks SET deleted=true,updated_at=now() WHERE id=$1 AND owner_id=$2',task_id,owner_id)
        await audit(self.db,owner_id,'TASK_DELETE','task',task_id)

    async def remove_source(self, task_id, owner_id):
        await self.db.execute('UPDATE tasks SET instant_source_id=NULL,updated_at=now() WHERE id=$1 AND owner_id=$2 AND NOT deleted',task_id,owner_id)
        await audit(self.db,owner_id,'TASK_SOURCE_REMOVE','task',task_id)

    async def edit_task(self, task_id, owner_id, **fields):
        allowed={'name','interval_seconds','delete_previous','instant_source_id','source_mode','max_runs','skip_failed','delete_failure_pause','content_ids','chat_ids'}
        if set(fields)-allowed:
            raise ValueError('不支持的任务配置')
        async with self.db.pool.acquire() as con:
            async with con.transaction():
                await con.execute('SELECT pg_advisory_xact_lock($1)', owner_id)
                task=await self._owned(con,task_id,owner_id)
                if task['status'] not in ('DRAFT','PAUSED','STOPPED','COMPLETED'):
                    raise ValueError('请先暂停或停止任务再修改')
                if await con.fetchval("SELECT 1 FROM deliveries d JOIN publication_events e ON e.id=d.event_id WHERE e.task_id=$1 AND d.status IN ('SENDING','UNKNOWN') LIMIT 1",task_id):
                    raise ValueError('请先等待在途结果并处理结果未知记录后修改')
                if await con.fetchval("SELECT count(*) FROM publication_events WHERE task_id=$1 AND status IN ('PENDING','RUNNING','ERROR')",task_id):
                    raise ValueError('当前批次尚未结束，请先停止任务后修改')
                contents=fields.get('content_ids') or [r['content_id'] for r in await con.fetch('SELECT content_id FROM task_items WHERE task_id=$1 ORDER BY position',task_id)]
                targets=fields['chat_ids'] if 'chat_ids' in fields else [r['chat_id'] for r in await con.fetch('SELECT chat_id FROM task_targets WHERE task_id=$1 AND active',task_id)]
                old_targets = {r['chat_id'] for r in await con.fetch('SELECT chat_id FROM task_targets WHERE task_id=$1 AND active', task_id)}
                if 'chat_ids' in fields and set(targets) - old_targets:
                    await self.entitlements.require(owner_id, con=con, proposed_chat_ids=targets, exclude_task_id=task_id)
                # Unbinding does not invalidate existing targets; only require active binding for newly selected targets.
                target_cleanup = set(fields) <= {'chat_ids'} and set(targets) <= old_targets
                if not target_cleanup and ('chat_ids' in fields or 'content_ids' in fields or 'instant_source_id' in fields):
                    await self._validate(con,owner_id,contents,targets,fields.get('interval_seconds',task['interval_seconds']),fields.get('instant_source_id',task['instant_source_id']), exclude_task_id=task_id)
                if 'interval_seconds' in fields and fields['interval_seconds'] < int(await con.fetchval("SELECT value FROM app_settings WHERE key='min_interval_seconds'") or 60):
                    raise ValueError('发布间隔小于系统最小值')
                if 'source_mode' in fields and fields['source_mode'] not in ('copy','forward'):
                    raise ValueError('来源模式无效')
                if 'max_runs' in fields and fields['max_runs']<0:
                    raise ValueError('轮次不能为负数')
                if 'content_ids' in fields:
                    await con.execute('DELETE FROM task_items WHERE task_id=$1',task_id)
                    await con.executemany('INSERT INTO task_items(task_id,position,content_id) VALUES($1,$2,$3)',[(task_id,i,c) for i,c in enumerate(contents)])
                    await con.execute('UPDATE tasks SET current_index=0 WHERE id=$1',task_id)
                if 'chat_ids' in fields:
                    await con.execute('UPDATE task_targets SET active=false WHERE task_id=$1',task_id)
                    await con.executemany('INSERT INTO task_targets(task_id,chat_id) VALUES($1,$2) ON CONFLICT(task_id,chat_id) DO UPDATE SET active=true',[(task_id,c) for c in set(targets)])
                for key,value in fields.items():
                    if key not in ('content_ids','chat_ids'):
                        await con.execute(f'UPDATE tasks SET {key}=$2,updated_at=now() WHERE id=$1',task_id,value)
                if task['status']=='COMPLETED':
                    await con.execute("UPDATE tasks SET status='STOPPED',completed_runs=0 WHERE id=$1",task_id)
        await audit(self.db,owner_id,'TASK_EDIT','task',task_id,{'fields':list(fields)})

    async def _new_event(self, con, task, kind, payload, dedupe):
        event_id=await con.fetchval('INSERT INTO publication_events(task_id,kind,payload,dedupe_key) VALUES($1,$2,$3,$4) ON CONFLICT(dedupe_key) DO NOTHING RETURNING id',task['id'],kind,payload,dedupe)
        if event_id:
            await con.execute('INSERT INTO deliveries(event_id,target_id,chat_id) SELECT $1,id,chat_id FROM task_targets WHERE task_id=$2 AND active',event_id,task['id'])
        return event_id

    async def _current_payload(self,con,task):
        items=await con.fetch('SELECT v.id version_id,v.payload FROM task_items i JOIN contents c ON c.id=i.content_id JOIN content_versions v ON v.id=c.current_version_id WHERE i.task_id=$1 ORDER BY i.position',task['id'])
        if not items:
            raise ValueError('任务没有可用内容版本')
        item=items[task['current_index']%len(items)]
        return {**item['payload'],'_version_id':item['version_id']}

    async def run_now(self,task_id,owner_id):
        async with self.db.pool.acquire() as con:
            async with con.transaction():
                await con.execute('SELECT pg_advisory_xact_lock($1)', owner_id)
                await self.entitlements.require(owner_id, con=con)
                task=await self._owned(con,task_id,owner_id)
                if task['status']!='RUNNING':
                    raise ValueError('请先启动任务')
                if await con.fetchval("SELECT count(*) FROM publication_events WHERE task_id=$1 AND status IN ('PENDING','RUNNING','ERROR')",task_id):
                    raise ValueError('已有未完成批次，请等待完成')
                await self._new_event(con,task,'MANUAL',await self._current_payload(con,task),f'manual:{task_id}:{uuid4()}')
                await con.execute('UPDATE tasks SET next_run_at=NULL WHERE id=$1',task_id)
        await audit(self.db,owner_id,'TASK_RUN_NOW','task',task_id)

    async def resolve_unknown(self,task_id,owner_id,action):
        if action not in ('retry','skip'):
            raise ValueError('未知处理方式')
        async with self.db.pool.acquire() as con:
            async with con.transaction():
                await self._owned(con,task_id,owner_id)
                await con.execute("UPDATE deliveries d SET status=$2,next_attempt_at=now(),attempts=0,error=error||$3 FROM publication_events e WHERE d.event_id=e.id AND e.task_id=$1 AND d.status='UNKNOWN'",task_id,'RETRY' if action=='retry' else 'SKIPPED','；用户确认'+action)
                await con.execute("UPDATE publication_events SET status='PENDING' WHERE task_id=$1 AND status='ERROR'",task_id)
                await con.execute("UPDATE tasks SET status='PAUSED',last_error='',remaining_seconds=0 WHERE id=$1",task_id)
        await audit(self.db,owner_id,'UNKNOWN_'+action.upper(),'task',task_id)

    async def ingest_source(self,message,update_id):
        if not await self.db.fetchval('SELECT 1 FROM sources WHERE chat_id=$1',message.chat_id):
            return
        try:
            payload=capture_message(message)
        except ValueError:
            return
        overflow=[]
        async with self.db.pool.acquire() as con:
            async with con.transaction():
                row=await con.fetchval('INSERT INTO source_messages(chat_id,message_id,payload) VALUES($1,$2,$3) ON CONFLICT(chat_id,message_id) DO NOTHING RETURNING id',message.chat_id,message.message_id,payload)
                if not row:
                    return
                tasks=await con.fetch("SELECT t.* FROM tasks t JOIN sources s ON s.id=t.instant_source_id JOIN users u ON u.id=t.owner_id WHERE s.chat_id=$1 AND t.status='RUNNING' AND NOT t.deleted AND u.status='ACTIVE' ORDER BY t.owner_id,t.id",message.chat_id)
                owners = sorted({task['owner_id'] for task in tasks})
                for owner in owners:
                    await con.execute('SELECT pg_advisory_xact_lock($1)', owner)
                limit=int(await con.fetchval("SELECT value FROM app_settings WHERE key='instant_queue_limit'") or 1000)
                for task in tasks:
                    task = await con.fetchrow("SELECT * FROM tasks WHERE id=$1 AND status='RUNNING' AND NOT deleted FOR UPDATE", task['id'])
                    if not task:
                        continue
                    try:
                        await self.entitlements.require(task['owner_id'], con=con)
                    except AccessDenied as exc:
                        await self.entitlements._pause(con, task, str(exc))
                        continue
                    pending=await con.fetchval("SELECT count(*) FROM publication_events WHERE task_id=$1 AND kind='INSTANT' AND status IN ('PENDING','RUNNING')",task['id'])
                    if pending>=limit:
                        await con.execute("UPDATE tasks SET status='ERROR',last_error='来源队列达到上限，新帖已保存来源库，请处理积压后手动导入',next_run_at=NULL WHERE id=$1",task['id'])
                        await con.execute("INSERT INTO audit_logs(actor_id,action,resource_type,resource_id,details) VALUES($1,'SOURCE_QUEUE_OVERFLOW','task',$2,$3)",task['owner_id'],str(task['id']),{'message_id':message.message_id})
                        overflow.append((task['owner_id'],task['id']))
                        continue
                    reference={'type':task['source_mode'],'source_chat_id':message.chat_id,'source_message_id':message.message_id,'buttons':[]}
                    await self._new_event(con,task,'INSTANT',reference,f'source:{task["id"]}:{message.chat_id}:{message.message_id}')
        for owner_id,task_id in overflow:
            await self.notify(owner_id,f'⚠️ 任务 #{task_id} 来源队列达到上限，已暂停。新消息已保存在来源库，处理积压后可手动导入。')

    async def schedule_test_delete(self,owner_id,chat_id,message_id,delay=30):
        await self.db.execute('INSERT INTO deferred_deletions(owner_id,chat_id,message_id,run_at) VALUES($1,$2,$3,$4) ON CONFLICT(chat_id,message_id) DO NOTHING',owner_id,chat_id,message_id,now()+timedelta(seconds=delay))

    async def create_broadcast(self,admin_id,payload):
        if admin_id not in self.settings.admin_ids:
            raise ValueError('仅管理员可广播')
        async with self.db.pool.acquire() as con:
            async with con.transaction():
                job=await con.fetchval('INSERT INTO broadcast_jobs(admin_id,payload) VALUES($1,$2) RETURNING id',admin_id,payload)
                await con.execute("INSERT INTO broadcast_recipients(job_id,user_id) SELECT $1,id FROM users WHERE status='ACTIVE' AND NOT blocked",job)
        await audit(self.db,admin_id,'BROADCAST_CREATE','broadcast',job)
        return job

    async def broadcast_action(self,job_id,admin_id,action):
        if admin_id not in self.settings.admin_ids:
            raise ValueError('仅管理员可管理广播')
        statuses={'pause':'PAUSED','resume':'RUNNING','cancel':'CANCELLED','retry_unknown':'PAUSED','skip_unknown':'PAUSED'}
        if action not in statuses:
            raise ValueError('未知广播动作')
        async with self.db.pool.acquire() as con:
            async with con.transaction():
                job=await con.fetchrow('SELECT * FROM broadcast_jobs WHERE id=$1 FOR UPDATE',job_id)
                if not job:
                    raise ValueError('广播不存在')
                if action=='resume' and await con.fetchval("SELECT 1 FROM broadcast_recipients WHERE job_id=$1 AND status='UNKNOWN'",job_id):
                    raise ValueError('请先选择重发或跳过结果未知记录')
                if action in ('retry_unknown','skip_unknown'):
                    await con.execute("UPDATE broadcast_recipients SET status=$2,next_attempt_at=now(),attempts=0 WHERE job_id=$1 AND status='UNKNOWN'",job_id,'RETRY' if action=='retry_unknown' else 'SKIPPED')
                await con.execute('UPDATE broadcast_jobs SET status=$2,updated_at=now() WHERE id=$1',job_id,statuses[action])
        await audit(self.db,admin_id,'BROADCAST_'+action.upper(),'broadcast',job_id)

    async def broadcast_stats(self,job_id):
        row=await self.db.fetchrow("SELECT count(*) total,count(*) FILTER(WHERE status='SUCCESS') success,count(*) FILTER(WHERE status='FAILED') failed,count(*) FILTER(WHERE status='UNKNOWN') unknown,count(*) FILTER(WHERE status='SKIPPED') skipped,count(*) FILTER(WHERE status IN ('PENDING','RETRY','SENDING')) pending FROM broadcast_recipients WHERE job_id=$1",job_id)
        return dict(row)

    async def notify(self,owner_id,text):
        try:
            await self.throttle(owner_id,'private')
            await self.bot.send_message(owner_id,text)
        except RetryAfter as exc:
            seconds=exc.retry_after.total_seconds() if isinstance(exc.retry_after,timedelta) else exc.retry_after
            await self.cooldown(seconds)
            log.warning('通知限流 owner=%s error=%s',owner_id,safe_error(exc))
        except TelegramError as exc:
            log.warning('通知失败 owner=%s error=%s',owner_id,safe_error(exc))

    async def recover(self):
        # Only called while the exclusive worker advisory lock is held.
        async with self.db.pool.acquire() as con:
            async with con.transaction():
                await con.execute("UPDATE deliveries SET status='UNKNOWN',error='进程中断，发送结果未知，请人工核对',finished_at=now() WHERE status='SENDING'")
                affected=await con.fetch("UPDATE tasks t SET status='ERROR',next_run_at=NULL,last_error='存在发送结果未知记录，请核对后重发或跳过' WHERE EXISTS(SELECT 1 FROM publication_events e JOIN deliveries d ON d.event_id=e.id WHERE e.task_id=t.id AND d.status='UNKNOWN') AND NOT t.deleted RETURNING t.owner_id,t.id")
                await con.execute("UPDATE publication_events e SET status='ERROR' WHERE EXISTS(SELECT 1 FROM deliveries d WHERE d.event_id=e.id AND d.status='UNKNOWN')")
                await con.execute("UPDATE broadcast_recipients SET status='UNKNOWN',error='进程中断，发送结果未知',finished_at=now() WHERE status='SENDING'")
                await con.execute("UPDATE broadcast_jobs b SET status='ERROR',updated_at=now() WHERE EXISTS(SELECT 1 FROM broadcast_recipients r WHERE r.job_id=b.id AND r.status='UNKNOWN')")
                await con.execute("UPDATE deferred_deletions SET status='PENDING' WHERE status='DELETING'")
        for row in affected:
            await self.notify(row['owner_id'],f'⚠️ 任务 #{row["id"]} 有发送结果未知的记录。请在任务详情核对并选择重发或跳过。')

    async def schedule(self):
        if asyncio.get_running_loop().time() - self._billing_last_enforce >= 5:
            await self.entitlements.enforce()
            self._billing_last_enforce = asyncio.get_running_loop().time()
        async with self.db.pool.acquire() as con:
            async with con.transaction():
                tasks=await con.fetch("SELECT t.* FROM tasks t JOIN users u ON u.id=t.owner_id WHERE t.status='RUNNING' AND NOT t.deleted AND u.status='ACTIVE' AND t.next_run_at<=now() AND NOT EXISTS(SELECT 1 FROM publication_events e WHERE e.task_id=t.id AND e.status IN ('PENDING','RUNNING','ERROR')) ORDER BY t.next_run_at LIMIT 100 FOR UPDATE OF t SKIP LOCKED")
                for task in tasks:
                    try:
                        payload=await self._current_payload(con,task)
                    except ValueError as exc:
                        await con.execute("UPDATE tasks SET status='ERROR',last_error=$2 WHERE id=$1",task['id'],str(exc))
                        continue
                    await self._new_event(con,task,'SCHEDULED',payload,f'scheduled:{task["id"]}:{task["next_run_at"].isoformat()}')
                    await con.execute('UPDATE tasks SET next_run_at=NULL WHERE id=$1',task['id'])

    async def tick(self):
        await asyncio.gather(self._tasks_tick(), self._broadcast_tick(), self._deletions_tick(), self._progress_tick())
        await self.db.execute("INSERT INTO app_settings(key,value) VALUES('worker_heartbeat',to_jsonb(now()::text)) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()")

    async def _tasks_tick(self):
        await self.schedule()
        tasks=await self.db.fetch("""SELECT t.* FROM tasks t JOIN users u ON u.id=t.owner_id
          WHERE t.status='RUNNING' AND NOT t.deleted AND u.status='ACTIVE'
          AND EXISTS(SELECT 1 FROM publication_events e WHERE e.task_id=t.id AND e.status IN ('PENDING','RUNNING')
           AND (EXISTS(SELECT 1 FROM deliveries d WHERE d.event_id=e.id AND d.status IN ('PENDING','RETRY') AND d.next_attempt_at<=now())
            OR NOT EXISTS(SELECT 1 FROM deliveries d WHERE d.event_id=e.id AND d.status IN ('PENDING','RETRY','SENDING'))))
          ORDER BY t.last_served_at NULLS FIRST,t.id LIMIT 32""")
        if tasks:
            await self.db.execute('UPDATE tasks SET last_served_at=now() WHERE id=ANY($1::bigint[])',[t['id'] for t in tasks])
            await asyncio.gather(*(self._guarded_task(task) for task in tasks))

    async def _guarded_task(self,task):
        async with self.semaphore:
            try:
                await self._process_task(task)
            except Exception as exc:
                log.exception('任务执行异常 task=%s',task['id'])
                await self.db.execute("UPDATE tasks SET status='ERROR',last_error=$2 WHERE id=$1",task['id'],safe_error(exc))
                await self.notify(task['owner_id'],f'⚠️ 任务 #{task["id"]} 已暂停：{safe_error(exc)}')

    async def _process_task(self,task):
        event=await self.db.fetchrow("SELECT * FROM publication_events WHERE task_id=$1 AND status IN ('PENDING','RUNNING') ORDER BY id LIMIT 1",task['id'])
        if not event:
            return
        await self.db.execute("UPDATE publication_events SET status='RUNNING',started_at=coalesce(started_at,now()) WHERE id=$1 AND status IN ('PENDING','RUNNING')",event['id'])
        deliveries=await self.db.fetch("SELECT d.*,tt.last_message_id,tt.last_sent_at,c.type chat_type FROM deliveries d JOIN task_targets tt ON tt.id=d.target_id LEFT JOIN chats c ON c.chat_id=d.chat_id WHERE d.event_id=$1 AND d.status IN ('PENDING','RETRY') AND d.next_attempt_at<=now() ORDER BY d.id LIMIT 1",event['id'])
        for delivery in deliveries:
            if await self.db.fetchval('SELECT status FROM tasks WHERE id=$1',task['id']) != 'RUNNING':
                break
            await self._send_delivery(task,event,delivery)
        await self._finish_event(task,event)

    async def _record(self,task,event,delivery,operation,status,*,message_id=None,error='',degraded=False):
        await self.db.execute('INSERT INTO message_logs(owner_id,task_id,event_id,chat_id,message_id,operation,status,error,degraded,payload_version_id) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)',task['owner_id'],task['id'],event['id'],delivery['chat_id'],message_id,operation,status,error,degraded,event['payload'].get('_version_id'))

    async def _billing_delivery_gate(self, task, delivery_id=None):
        async with self.db.pool.acquire() as con:
            async with con.transaction():
                await con.execute('SELECT pg_advisory_xact_lock($1)', task['owner_id'])
                current = await self._owned(con, task['id'], task['owner_id'])
                try:
                    await self.entitlements.require(task['owner_id'], con=con)
                except AccessDenied as exc:
                    if current['status'] == 'RUNNING':
                        await self.entitlements._pause(con, current, str(exc))
                    if delivery_id is not None:
                        await con.execute("UPDATE deliveries SET status='PENDING' WHERE id=$1 AND status='SENDING'", delivery_id)
                    return False
                if current['status'] != 'RUNNING':
                    if delivery_id is not None:
                        await con.execute("UPDATE deliveries SET status='PENDING' WHERE id=$1 AND status='SENDING'", delivery_id)
                    return False
                return True

    async def _send_delivery(self,task,event,d):
        if not await self._billing_delivery_gate(task):
            return
        try:
            await self.check_bot(d['chat_id'])
        except RetryAfter as exc:
            seconds=exc.retry_after.total_seconds() if isinstance(exc.retry_after,timedelta) else exc.retry_after
            await self.cooldown(seconds)
            await self.db.execute("UPDATE deliveries SET status='RETRY',next_attempt_at=$2,error=$3 WHERE id=$1",d['id'],now()+timedelta(seconds=seconds+1),safe_error(exc))
            return
        except (BadRequest, Forbidden, ValueError) as exc:
            await self.db.execute("UPDATE deliveries SET status='FAILED',error=$2,finished_at=now() WHERE id=$1",d['id'],safe_error(exc))
            await self._record(task,event,d,'SEND','FAILED',error=safe_error(exc))
            return
        except NetworkError as exc:
            # Permission query has no sending side effect; safe to retry.
            await self.db.execute("UPDATE deliveries SET attempts=attempts+1,status=CASE WHEN attempts>=4 THEN 'FAILED' ELSE 'RETRY' END,next_attempt_at=$2,error=$3 WHERE id=$1",d['id'],now()+timedelta(seconds=30),safe_error(exc))
            return
        except (ValueError,TelegramError) as exc:
            await self.db.execute("UPDATE deliveries SET status='FAILED',error=$2,finished_at=now() WHERE id=$1",d['id'],safe_error(exc))
            await self._record(task,event,d,'SEND','FAILED',error=safe_error(exc))
            return
        await self.throttle(d['chat_id'],d['chat_type'])
        if not await self._billing_delivery_gate(task):
            return
        async with self.db.pool.acquire() as con:
            async with con.transaction():
                current=await con.fetchval('SELECT status FROM tasks WHERE id=$1 FOR UPDATE',task['id'])
                event_status=await con.fetchval('SELECT status FROM publication_events WHERE id=$1 FOR UPDATE',event['id'])
                if current!='RUNNING' or event_status not in ('PENDING','RUNNING'):
                    return
                changed=await con.fetchval("UPDATE deliveries SET status='SENDING',attempts=attempts+1,started_at=now() WHERE id=$1 AND status IN ('PENDING','RETRY') RETURNING id",d['id'])
                if not changed:
                    return
        if task['delete_previous'] and d['last_message_id'] and d['delete_status'] != 'SUCCESS':
            try:
                await self.bot.delete_message(d['chat_id'],d['last_message_id'])
                await self.db.execute("UPDATE deliveries SET delete_status='SUCCESS',delete_error='' WHERE id=$1",d['id'])
                await self._record(task,event,d,'DELETE','SUCCESS',message_id=d['last_message_id'])
            except TelegramError as exc:
                if isinstance(exc,RetryAfter):
                    seconds=exc.retry_after.total_seconds() if isinstance(exc.retry_after,timedelta) else exc.retry_after
                    await self.cooldown(seconds)
                await self.db.execute("UPDATE deliveries SET delete_status='FAILED',delete_error=$2 WHERE id=$1",d['id'],safe_error(exc))
                await self._record(task,event,d,'DELETE','FAILED',message_id=d['last_message_id'],error=safe_error(exc))
                if task['delete_failure_pause']:
                    await self.db.execute("UPDATE deliveries SET status='FAILED',error=$2 WHERE id=$1",d['id'],'删除旧帖失败：'+safe_error(exc))
                    await self.db.execute("UPDATE tasks SET status='ERROR',last_error=$2 WHERE id=$1 AND status='RUNNING'",task['id'],'删除旧帖失败：'+safe_error(exc))
                    await self.db.execute("UPDATE publication_events SET status='ERROR' WHERE id=$1 AND status IN ('PENDING','RUNNING')",event['id'])
                    await self.notify(task['owner_id'],f'⚠️ 任务 #{task["id"]} 删除旧帖失败，已按设置暂停。')
                    return
            await self.throttle(d['chat_id'],d['chat_type'])
        # After a deletion, pause may have arrived. Keep its saved result but
        # release this unsent delivery back to PENDING without sending a new post.
        async with self.db.pool.acquire() as con:
            async with con.transaction():
                current=await con.fetchval('SELECT status FROM tasks WHERE id=$1 FOR UPDATE',task['id'])
                event_status=await con.fetchval('SELECT status FROM publication_events WHERE id=$1 FOR UPDATE',event['id'])
                if current!='RUNNING' or event_status not in ('PENDING','RUNNING'):
                    await con.execute("UPDATE deliveries SET status='PENDING' WHERE id=$1 AND status='SENDING'",d['id'])
                    return
        if not await self._billing_delivery_gate(task, d['id']):
            return
        try:
            result=await deliver(self.bot,d['chat_id'],event['payload'])
        except RetryAfter as exc:
            seconds=exc.retry_after.total_seconds() if isinstance(exc.retry_after,timedelta) else exc.retry_after
            await self.cooldown(seconds)
            await self.db.execute("UPDATE deliveries SET status='RETRY',next_attempt_at=$2,error=$3 WHERE id=$1",d['id'],now()+timedelta(seconds=seconds+1),safe_error(exc))
        except (BadRequest, Forbidden, ValueError) as exc:
            await self.db.execute("UPDATE deliveries SET status='FAILED',error=$2,finished_at=now() WHERE id=$1",d['id'],safe_error(exc))
            await self._record(task,event,d,'SEND','FAILED',error=safe_error(exc))
        except NetworkError as exc:
            await self.db.execute("UPDATE deliveries SET status='UNKNOWN',error=$2,finished_at=now() WHERE id=$1",d['id'],safe_error(exc))
            await self._record(task,event,d,'SEND','UNKNOWN',error=safe_error(exc))
        except TelegramError as exc:
            await self.db.execute("UPDATE deliveries SET status='FAILED',error=$2,finished_at=now() WHERE id=$1",d['id'],safe_error(exc))
            await self._record(task,event,d,'SEND','FAILED',error=safe_error(exc))
        else:
            # Crash before this commit deliberately leaves SENDING for UNKNOWN recovery.
            async with self.db.pool.acquire() as con:
                async with con.transaction():
                    await con.execute("UPDATE deliveries SET status='SUCCESS',message_id=$2,degraded=$3,error='',finished_at=now() WHERE id=$1",d['id'],result['message_id'],result.get('degraded',False))
                    await con.execute('UPDATE task_targets SET last_message_id=$2,last_sent_at=now() WHERE id=$1',d['target_id'],result['message_id'])
                    await con.execute("INSERT INTO message_logs(owner_id,task_id,event_id,chat_id,message_id,operation,status,degraded,payload_version_id) VALUES($1,$2,$3,$4,$5,'SEND','SUCCESS',$6,$7)",task['owner_id'],task['id'],event['id'],d['chat_id'],result['message_id'],result.get('degraded',False),event['payload'].get('_version_id'))

    async def _finish_event(self,task,event):
        rows=await self.db.fetch('SELECT status,count(*) n FROM deliveries WHERE event_id=$1 GROUP BY status',event['id'])
        counts={r['status']:r['n'] for r in rows}
        if counts.get('UNKNOWN') or (counts.get('FAILED') and not task['skip_failed']):
            if await self.db.fetchval('SELECT status FROM tasks WHERE id=$1',task['id']) in ('STOPPED','PAUSED'):
                return
            text='发送结果未知，请核对后选择重发或跳过' if counts.get('UNKNOWN') else '部分目标发送失败，成功目标不会重发；排除问题后恢复，或修改失败跳过策略'
            await self.db.execute("UPDATE tasks SET status='ERROR',last_error=$2,remaining_seconds=0 WHERE id=$1 AND status='RUNNING'",task['id'],text)
            await self.db.execute("UPDATE publication_events SET status='ERROR' WHERE id=$1 AND status='RUNNING'",event['id'])
            await self.notify(task['owner_id'],f'⚠️ 任务 #{task["id"]}：{text}')
            return
        if any(counts.get(s) for s in ('PENDING','RETRY','SENDING')):
            return
        async with self.db.pool.acquire() as con:
            async with con.transaction():
                current=await con.fetchrow('SELECT * FROM tasks WHERE id=$1 FOR UPDATE',task['id'])
                status=await con.fetchval('SELECT status FROM publication_events WHERE id=$1 FOR UPDATE',event['id'])
                if status not in ('RUNNING','PENDING'):
                    return
                await con.execute("UPDATE publication_events SET status='COMPLETED',finished_at=now() WHERE id=$1",event['id'])
                if event['kind']!='INSTANT':
                    items=await con.fetchval('SELECT count(*) FROM task_items WHERE task_id=$1',task['id'])
                    total=current['completed_runs']+1
                    done=current['max_runs']>0 and total>=current['max_runs']
                    new_status='COMPLETED' if done else current['status']
                    await con.execute('UPDATE tasks SET current_index=$2,completed_runs=$3,status=$4,next_run_at=$5,remaining_seconds=$6,updated_at=now() WHERE id=$1',task['id'],(current['current_index']+1)%max(1,items),total,new_status,now()+timedelta(seconds=current['interval_seconds']) if new_status=='RUNNING' else None,current['interval_seconds'])
                else:
                    await con.execute('UPDATE tasks SET updated_at=now() WHERE id=$1',task['id'])

    async def _broadcast_tick(self):
        rows=await self.db.fetch("SELECT r.*,b.payload FROM broadcast_recipients r JOIN broadcast_jobs b ON b.id=r.job_id JOIN users u ON u.id=r.user_id WHERE b.status='RUNNING' AND r.status IN ('PENDING','RETRY') AND r.next_attempt_at<=now() AND u.status='ACTIVE' ORDER BY r.id LIMIT 50")
        await asyncio.gather(*(self._send_broadcast(row) for row in rows))
        await self.db.execute("UPDATE broadcast_recipients r SET status='SKIPPED',error='用户已封禁',finished_at=now() FROM users u WHERE r.user_id=u.id AND u.status<>'ACTIVE' AND r.status IN ('PENDING','RETRY')")
        await self.db.execute("UPDATE broadcast_jobs b SET status='COMPLETED',updated_at=now() WHERE b.status='RUNNING' AND NOT EXISTS(SELECT 1 FROM broadcast_recipients r WHERE r.job_id=b.id AND r.status IN ('PENDING','RETRY','SENDING','UNKNOWN'))")

    async def _send_broadcast(self,row):
        async with self.semaphore:
            if await self.db.fetchval('SELECT status FROM broadcast_jobs WHERE id=$1',row['job_id'])!='RUNNING':
                return
            await self.throttle(row['user_id'],'private')
            if await self.db.fetchval('SELECT status FROM broadcast_jobs WHERE id=$1',row['job_id'])!='RUNNING':
                return
            if not await self.db.fetchval("UPDATE broadcast_recipients SET status='SENDING',started_at=now(),attempts=attempts+1 WHERE id=$1 AND status IN ('PENDING','RETRY') RETURNING id",row['id']):
                return
            try:
                result=await deliver(self.bot,row['user_id'],row['payload'])
            except RetryAfter as exc:
                seconds=exc.retry_after.total_seconds() if isinstance(exc.retry_after,timedelta) else exc.retry_after
                await self.cooldown(seconds)
                await self.db.execute("UPDATE broadcast_recipients SET status='RETRY',next_attempt_at=$2,error=$3 WHERE id=$1",row['id'],now()+timedelta(seconds=seconds+1),safe_error(exc))
            except BadRequest as exc:
                await self.db.execute("UPDATE broadcast_recipients SET status='FAILED',error=$2,finished_at=now() WHERE id=$1",row['id'],safe_error(exc))
            except NetworkError as exc:
                await self.db.execute("UPDATE broadcast_recipients SET status='UNKNOWN',error=$2,finished_at=now() WHERE id=$1",row['id'],safe_error(exc))
                await self.db.execute("UPDATE broadcast_jobs SET status='ERROR',updated_at=now() WHERE id=$1",row['job_id'])
            except Forbidden as exc:
                await self.db.execute("UPDATE broadcast_recipients SET status='FAILED',error=$2,finished_at=now() WHERE id=$1",row['id'],safe_error(exc))
                await self.db.execute('UPDATE users SET blocked=true WHERE id=$1',row['user_id'])
            except TelegramError as exc:
                await self.db.execute("UPDATE broadcast_recipients SET status='FAILED',error=$2,finished_at=now() WHERE id=$1",row['id'],safe_error(exc))
            except Exception as exc:
                log.exception('广播异常 recipient=%s',row['id'])
                await self.db.execute("UPDATE broadcast_recipients SET status='UNKNOWN',error=$2 WHERE id=$1",row['id'],safe_error(exc))
                await self.db.execute("UPDATE broadcast_jobs SET status='ERROR' WHERE id=$1",row['job_id'])
            else:
                await self.db.execute("UPDATE broadcast_recipients SET status='SUCCESS',message_id=$2,degraded=$3,error='',finished_at=now() WHERE id=$1",row['id'],result['message_id'],result.get('degraded',False))

    async def _deletions_tick(self):
        rows=await self.db.fetch("SELECT * FROM deferred_deletions WHERE status='PENDING' AND run_at<=now() ORDER BY run_at LIMIT 20")
        for row in rows:
            if not await self.db.fetchval("UPDATE deferred_deletions SET status='DELETING' WHERE id=$1 AND status='PENDING' RETURNING id",row['id']):
                continue
            try:
                await self.throttle(row['chat_id'])
                await self.bot.delete_message(row['chat_id'],row['message_id'])
            except RetryAfter as exc:
                seconds=exc.retry_after.total_seconds() if isinstance(exc.retry_after,timedelta) else exc.retry_after
                await self.cooldown(seconds)
                await self.db.execute("UPDATE deferred_deletions SET status='PENDING',run_at=$2,error=$3 WHERE id=$1",row['id'],now()+timedelta(seconds=seconds+1),safe_error(exc))
            except TelegramError as exc:
                await self.db.execute("UPDATE deferred_deletions SET status='FAILED',error=$2 WHERE id=$1",row['id'],safe_error(exc))
                await self.db.execute("INSERT INTO message_logs(owner_id,chat_id,message_id,operation,status,error) VALUES($1,$2,$3,'DELETE','FAILED',$4)",row['owner_id'],row['chat_id'],row['message_id'],safe_error(exc))
            else:
                await self.db.execute("UPDATE deferred_deletions SET status='SUCCESS',error='' WHERE id=$1",row['id'])
                await self.db.execute("INSERT INTO message_logs(owner_id,chat_id,message_id,operation,status) VALUES($1,$2,$3,'DELETE','SUCCESS')",row['owner_id'],row['chat_id'],row['message_id'])

    async def _progress_tick(self):
        jobs=await self.db.fetch("SELECT * FROM broadcast_jobs WHERE progress_message_id IS NOT NULL AND (last_progress_at IS NULL OR last_progress_at<updated_at OR (status IN ('RUNNING','ERROR') AND last_progress_at<now()-interval '5 seconds')) ORDER BY id DESC LIMIT 10")
        for job in jobs:
            stat=await self.broadcast_stats(job['id'])
            done=stat['success']+stat['failed']+stat['skipped']
            ratio=done/max(1,stat['total'])
            bar='█'*int(ratio*20)+'░'*(20-int(ratio*20))
            text=f'📣 广播 #{job["id"]} · {job["status"]}\n{bar} {ratio:.0%}\n总目标：{stat["total"]}\n成功：{stat["success"]} · 失败：{stat["failed"]}\n未知：{stat["unknown"]} · 跳过：{stat["skipped"]}\n待处理：{stat["pending"]}\n进度来自SQL实际发送记录。'
            try:
                await self.throttle(job['progress_chat_id'],'private')
                await self.bot.edit_message_text(text,chat_id=job['progress_chat_id'],message_id=job['progress_message_id'],reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('📣 广播详情',callback_data=f'u:{job["admin_id"]}:broadcast:{job["id"]}')]]))
            except RetryAfter as exc:
                seconds=exc.retry_after.total_seconds() if isinstance(exc.retry_after,timedelta) else exc.retry_after
                await self.cooldown(seconds)
                log.warning('广播进度限流 job=%s',job['id'])
            except BadRequest as exc:
                if 'not modified' not in str(exc).lower():
                    log.warning('广播进度编辑失败 job=%s %s',job['id'],safe_error(exc))
            except TelegramError as exc:
                log.warning('广播进度更新失败 job=%s %s',job['id'],safe_error(exc))
            await self.db.execute('UPDATE broadcast_jobs SET last_progress_at=now() WHERE id=$1',job['id'])
