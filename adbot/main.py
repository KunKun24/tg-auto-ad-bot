from __future__ import annotations

import asyncio
import json
import logging
import signal
from collections import defaultdict

from telegram import Bot, BotCommand, BotCommandScopeChat, MenuButtonCommands, Update
from telegram.error import Conflict, NetworkError, RetryAfter, TelegramError
from telegram.request import HTTPXRequest

from .config import configure_logging, load_settings
from .content import discover_icons
from .db import Database, audit
from .engine import Engine, safe_error
from .ui import BotUI
from .billing_ui import SECRET_REDACTED


def _command(text):
    return text.split()[0].split('@')[0].lower() if text and text.startswith('/') else ''


async def durable_update_payload(db, update, settings):
    """Private admin text is ephemeral; tokens cannot enter the durable inbox."""
    payload = json.loads(update.to_json())
    message = update.message or update.edited_message
    if message is None:
        return payload, False, False
    text = message.text or message.caption or ''
    uid = update.effective_user.id if update.effective_user else 0
    private_admin = message.chat.type == 'private' and uid in settings.admin_ids
    explicit_secret = _command(text) == '/paytoken'
    session_secret = False
    if private_admin:
        # Include expired sessions: a delayed secret must not become draft content.
        state = await db.fetchval('SELECT state FROM sessions WHERE owner_id=$1', uid)
        session_secret = state == 'BILLING_CONFIG_SECRET' and _command(text) not in {'/cancel', '/start', '/menu', '/help', '/admin'}
    transient = explicit_secret or session_secret or (private_admin and bool(text))
    force_secret = explicit_secret or session_secret
    if transient:
        key = 'message' if update.message else 'edited_message'
        original = payload[key]
        # Rebuild, rather than redact selected fields: quoted/replied messages can
        # contain the same secret, and entity URLs may also carry raw input.
        payload[key] = {name: original[name] for name in ('message_id', 'date', 'chat', 'from') if name in original}
        payload[key]['text'] = SECRET_REDACTED
        payload['_billing_ephemeral'] = True
        payload['_billing_force_secret'] = force_secret
    return payload, transient, force_secret

log=logging.getLogger(__name__)


async def process_inbox(db, bot, settings, stop, ephemeral=None):
    ui=BotUI(db,bot,settings)
    engine=Engine(db,bot,settings)
    locks=defaultdict(asyncio.Lock)
    semaphore=asyncio.Semaphore(16)
    ephemeral = ephemeral if ephemeral is not None else {}

    async def process(row):
        try:
            await process_inner(row)
        finally:
            ephemeral.pop(row['update_id'], None)

    async def process_inner(row):
        original = ephemeral.get(row['update_id'])
        update = original[0] if original else Update.de_json(row['payload'],bot)
        billing_secret = original[1] if original else bool(row['payload'].get('_billing_ephemeral'))
        source_update=bool(update.channel_post or (update.message and update.message.chat.type in ('group','supergroup')))
        actor=('source',update.effective_chat.id) if source_update else ('user',update.effective_user.id if update.effective_user else 0)
        async with locks[actor],semaphore:
            changed=await db.fetchval("UPDATE updates SET status='PROCESSING',attempts=attempts+1 WHERE update_id=$1 AND status='PENDING' RETURNING update_id",row['update_id'])
            if not changed:
                ephemeral.pop(row['update_id'], None)
                return
            try:
                if billing_secret:
                    await ui.handle_update(update, billing_secret=True)
                elif update.channel_post:
                    await engine.ingest_source(update.channel_post,update.update_id)
                elif update.message and update.message.chat.type in ('group','supergroup'):
                    await engine.ingest_source(update.message,update.update_id)
                else:
                    await ui.handle_update(update, billing_secret=billing_secret)
            except Exception as exc:
                # No exception text/traceback for ephemeral admin inputs: even
                # database validation errors can include submitted credentials.
                transient = bool(original or row['payload'].get('_billing_ephemeral'))
                if transient:
                    log.error('临时私聊处理失败 update_id=%s error_type=%s', update.update_id, type(exc).__name__)
                else:
                    log.exception('Update处理失败 update_id=%s',update.update_id)
                await db.execute("UPDATE updates SET status='ERROR',error=$2,processed_at=now() WHERE update_id=$1",update.update_id,type(exc).__name__ if transient else safe_error(exc))
                if update.effective_chat and update.effective_chat.type=='private':
                    try:
                        await bot.send_message(update.effective_chat.id,'操作未完成，已记录错误。请返回 /start 后重新操作；任务发送记录不会被清空。')
                    except TelegramError:
                        pass
            else:
                await db.execute("UPDATE updates SET status='DONE',processed_at=now() WHERE update_id=$1",update.update_id)
            finally:
                ephemeral.pop(row['update_id'], None)
    while not stop.is_set():
        rows=await db.fetch("SELECT * FROM updates WHERE status='PENDING' ORDER BY update_id LIMIT 100")
        if rows:
            await asyncio.gather(*(process(row) for row in rows))
        else:
            try:
                await asyncio.wait_for(stop.wait(),timeout=0.3)
            except TimeoutError:
                pass


async def run():
    configure_logging()
    settings=load_settings()
    db=Database(settings.database_url)
    await db.open()
    await db.migrate()
    stop=asyncio.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig,stop.set)
    async with db.pool.acquire() as guard:
        if not await guard.fetchval('SELECT pg_try_advisory_lock(7150000002)'):
            raise RuntimeError('接收服务已运行，不允许重复getUpdates')
        # UI operations in progress may have external side effects. Do not blindly
        # replay confirmation callbacks after a crash; user explicitly retries.
        await db.execute("""UPDATE updates SET status='PENDING',error='来源摄取中断，将幂等恢复'
          WHERE status='PROCESSING' AND (payload ? 'channel_post' OR payload->'message'->'chat'->>'type' IN ('group','supergroup'))""")
        interrupted=await db.execute("UPDATE updates SET status='ERROR',error='接收进程中断，需用户重新操作',processed_at=now() WHERE status='PROCESSING'")
        log.info('持久化收件箱恢复 %s',interrupted)
        request=HTTPXRequest(connection_pool_size=40,connect_timeout=10,read_timeout=35,write_timeout=35,pool_timeout=30)
        poll_request=HTTPXRequest(connection_pool_size=2,connect_timeout=10,read_timeout=40)
        async with Bot(settings.token,request=request,get_updates_request=poll_request) as bot:
            me=await bot.get_me()
            await bot.delete_webhook(drop_pending_updates=False)
            commands=[BotCommand('start','打开主菜单'),BotCommand('help','功能和使用教程'),BotCommand('cancel','取消当前操作')]
            await bot.set_my_commands(commands)
            for admin_id in settings.admin_ids:
                try:
                    await bot.set_my_commands(commands+[BotCommand('admin','管理员后台')],scope=BotCommandScopeChat(admin_id))
                except TelegramError as exc:
                    log.warning('管理员尚未启动机器人 admin=%s error=%s',admin_id,safe_error(exc))
            await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
            await bot.set_my_description('TG 自动广告机器人：频道/群组管理、广告编辑、定时轮播、来源新帖同步、发送统计。会员订阅与永久目标增量，支持彩色按钮和会员表情；所有操作在Telegram内完成。')
            await bot.set_my_short_description('频道群组广告管理 · 定时轮播 · 来源同步 · 彩色按钮与会员表情')
            icons=await discover_icons(bot)
            await db.execute("INSERT INTO app_settings(key,value) VALUES('icons',$1) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()",icons)
            await db.execute("INSERT INTO app_settings(key,value) VALUES('bot_username',$1) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()",me.username)
            log.info('Bot @%s 已启动 id=%s 图标类别=%s',me.username,me.id,len(icons))
            await audit(db,0,'BOT_START','system',str(me.id),{'username':me.username,'icons':len(icons)})
            startup_engine=Engine(db,bot,settings)
            await db.execute("INSERT INTO app_settings(key,value) VALUES('bot_heartbeat',to_jsonb(now()::text)) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()")
            for admin_id in settings.admin_ids:
                try:
                    await startup_engine.throttle(admin_id,'private')
                    await bot.send_message(admin_id,f'✅ TG 自动广告机器人已启动\n@{me.username}\n\n接收服务已连接，SQL持久化已就绪。\n/start 打开主菜单\n/admin 管理员后台\n\n仅向管理员发送本启动通知；不会自动向用户或频道发布广告。')
                except TelegramError as exc:
                    log.warning('启动通知未送达 admin=%s error=%s',admin_id,safe_error(exc))
            ephemeral = {}
            consumer=asyncio.create_task(process_inbox(db,bot,settings,stop,ephemeral),name='persistent-inbox')
            try:
                offset=int(await db.fetchval("SELECT value FROM app_settings WHERE key='poll_offset'") or 0)
                while not stop.is_set():
                    if consumer.done():
                        await consumer
                    try:
                        updates=await bot.get_updates(offset=offset,timeout=settings.poll_timeout,allowed_updates=['message','callback_query','channel_post','my_chat_member','chat_member'],limit=100)
                        if updates:
                            prepared = [(update, *await durable_update_payload(db, update, settings)) for update in updates]
                            added = []
                            try:
                                async with db.pool.acquire() as con:
                                    async with con.transaction():
                                        for update, payload, transient, force_secret in prepared:
                                            inserted = await con.fetchval('INSERT INTO updates(update_id,payload) VALUES($1,$2) ON CONFLICT(update_id) DO NOTHING RETURNING update_id', update.update_id, payload)
                                            if inserted and transient:
                                                ephemeral[update.update_id] = (update, force_secret)
                                                added.append(update.update_id)
                                        new_offset=max(u.update_id for u in updates)+1
                                        await con.execute("INSERT INTO app_settings(key,value) VALUES('poll_offset',$1) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()",new_offset)
                            except BaseException:
                                for update_id in added:
                                    ephemeral.pop(update_id, None)
                                raise
                            offset=new_offset
                        await db.execute("INSERT INTO app_settings(key,value) VALUES('bot_heartbeat',to_jsonb(now()::text)) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()")
                    except Conflict:
                        log.error('检测到另一处getUpdates实例，停止本服务以避免抢占')
                        raise
                    except RetryAfter as exc:
                        delay=exc.retry_after.total_seconds() if hasattr(exc.retry_after,'total_seconds') else exc.retry_after
                        try:
                            await asyncio.wait_for(stop.wait(),timeout=delay+1)
                        except TimeoutError:
                            pass
                    except NetworkError as exc:
                        log.warning('轮询网络异常，将安全重试：%s',safe_error(exc))
                        try:
                            await asyncio.wait_for(stop.wait(),timeout=3)
                        except TimeoutError:
                            pass
            finally:
                stop.set()
                try:
                    await asyncio.wait_for(consumer,timeout=40)
                except TimeoutError:
                    consumer.cancel()
                    await asyncio.gather(consumer,return_exceptions=True)
    await db.close()


if __name__=='__main__':
    asyncio.run(run())
