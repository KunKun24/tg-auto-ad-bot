#!/usr/bin/env python3
"""Explicit live check confined to configured admins in an isolated TEST schema.

Private admin chats cannot be business channel targets; this diagnostic replaces
only check_bot for that known private recipient. Production SQL is never used.
"""
from __future__ import annotations
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import asyncpg
from telegram import Bot
from adbot.config import load_settings, configure_logging
from adbot.content import discover_icons
from adbot.db import Database
from adbot.engine import Engine

TEST_DSN='postgresql://adbot@/auto_ad_test?host=/var/run/postgresql'


async def main():
    configure_logging()
    settings=load_settings()
    uid=settings.admin_ids[0]
    schema='live_admin_'+uuid4().hex
    control=await asyncpg.connect(TEST_DSN)
    db=Database(TEST_DSN)
    report={'checked_at':datetime.now(timezone.utc).isoformat(),'recipient_admin':uid,'database':'auto_ad_test','business_channel_permissions_tested':False}
    try:
        assert await control.fetchval('SELECT current_database()')=='auto_ad_test'
        await control.execute(f'CREATE SCHEMA "{schema}"')
        async def init(con):
            for kind in ('json','jsonb'):
                await con.set_type_codec(kind,encoder=lambda value:json.dumps(value,ensure_ascii=False),decoder=json.loads,schema='pg_catalog')
        db.pool=await asyncpg.create_pool(TEST_DSN,min_size=2,max_size=6,server_settings={'search_path':schema},init=init)
        await db.migrate()
        async with Bot(settings.token) as bot:
            icons=await discover_icons(bot)
            icon=icons.get('tasks') or icons.get('menu')
            payload={'type':'text','text':'✅ 管理员专用Worker实发验收 A：下一条将删除本条。','entities':[],'buttons':[[{'text':'机器人菜单','url':f'https://t.me/{bot.username}','style':'success',**({'icon_custom_emoji_id':icon} if icon else {})}]]}
            await db.execute("INSERT INTO users(id,first_name) VALUES($1,'Admin live diagnostic')",uid)
            await db.execute("INSERT INTO chats(chat_id,type,title) VALUES($1,'private','Admin-only diagnostic')",uid)
            await db.execute('INSERT INTO user_chats(owner_id,chat_id) VALUES($1,$1)',uid)
            cid=await db.fetchval("INSERT INTO contents(owner_id,title) VALUES($1,'Live diagnostic') RETURNING id",uid)
            vid=await db.fetchval('INSERT INTO content_versions(content_id,version_no,payload) VALUES($1,1,$2) RETURNING id',cid,payload)
            await db.execute('UPDATE contents SET current_version_id=$2 WHERE id=$1',cid,vid)
            engine=Engine(db,bot,settings)
            async def admin_private_only(chat_id):
                if chat_id!=uid or chat_id not in settings.admin_ids:
                    raise ValueError('诊断仅限预设管理员私聊')
            engine.check_bot=admin_private_only
            task=await engine.create_task(uid,'Live diagnostic',[cid],[uid],60,delete_previous=True,max_runs=2)
            await engine.start_task(task,uid)
            await engine.tick()
            vid2=await db.fetchval('INSERT INTO content_versions(content_id,version_no,payload) VALUES($1,2,$2) RETURNING id',cid,{**payload,'text':'✅ 管理员专用Worker实发验收 B：上一条已由Worker删除，有限轮次已完成。'})
            await db.execute('UPDATE contents SET current_version_id=$2 WHERE id=$1',cid,vid2)
            await db.execute('UPDATE tasks SET next_run_at=now() WHERE id=$1',task)
            await engine.tick()
            report['task']=dict(await db.fetchrow('SELECT status,completed_runs,current_index FROM tasks WHERE id=$1',task))
            report['deliveries']=[dict(row) for row in await db.fetch('SELECT status,message_id,delete_status,degraded FROM deliveries ORDER BY id')]
            report['logs']=[dict(row) for row in await db.fetch('SELECT operation,status,message_id FROM message_logs ORDER BY id')]
            assert report['task']['status']=='COMPLETED'
            assert len(report['deliveries'])==2 and all(row['status']=='SUCCESS' for row in report['deliveries'])
            assert report['deliveries'][1]['delete_status']=='SUCCESS'
            report['icons']=icons
            job=await engine.create_broadcast(uid,{**payload,'text':'📣 管理员专用广播实发验收：本测试数据库仅包含你一位接收者，进度将更新。'})
            await engine.throttle(uid,'private')
            progress=await bot.send_message(uid,'广播验收进度：等待Worker处理（本次仅管理员1人）')
            await db.execute('UPDATE broadcast_jobs SET progress_chat_id=$2,progress_message_id=$3 WHERE id=$1',job,uid,progress.message_id)
            await engine._broadcast_tick()
            await engine._progress_tick()
            report['broadcast']={'status':await db.fetchval('SELECT status FROM broadcast_jobs WHERE id=$1',job),'stats':await engine.broadcast_stats(job),'progress_message_id':progress.message_id,'last_progress_updated':bool(await db.fetchval('SELECT last_progress_at FROM broadcast_jobs WHERE id=$1',job))}
            assert report['broadcast']['status']=='COMPLETED'
            assert report['broadcast']['stats']['success']==1
            assert report['broadcast']['last_progress_updated']
        print(json.dumps(report,ensure_ascii=False,indent=2))
        (ROOT/'data'/'live-worker-smoke.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    finally:
        await db.close()
        assert schema.startswith('live_admin_') and len(schema)==43
        await control.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await control.close()


if __name__=='__main__':
    asyncio.run(main())
