"""Telegram-only UI with SQL sessions and owner-scoped resource access."""
from __future__ import annotations

import csv
import io
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import (ChatAdministratorRights, InlineKeyboardMarkup, InputFile,
                      KeyboardButton, KeyboardButtonRequestChat, ReplyKeyboardMarkup, ReplyKeyboardRemove)
from telegram.error import BadRequest, RetryAfter, TelegramError

from .content import (capture_message, deliver, menu_button, parse_buttons, parse_chat_ref,
                      parse_interval, parse_source_link, _emoji_qualification_error)
from .db import audit
from .engine import Engine
from .billing_ui import BillingUIMixin

PAGE = 8
CONFIG = {
    'min_interval_seconds': (int, 60, 86400), 'max_tasks_per_user': (int, 1, 10000),
    'max_targets_per_task': (int, 1, 100000), 'max_bindings_per_user': (int, 1, 100000),
    'instant_queue_limit': (int, 1, 100000), 'test_message': (str, 1, 3500),
    'contact_admin': (str, 1, 1000), 'help_text': (str, 1, 3500), 'welcome': (str, 1, 3000),
}
HELP = ('使用教程\n1. 将机器人和自己设为频道/群组管理员，再绑定目标。\n'
        '2. 保存内容：发送文字、图片、视频、动画或文档，设置按钮并预览确认。\n'
        '3. 新建任务：选择多条内容、多目标，设置间隔和删除规则；保存后主动启动。\n'
        '4. 来源需单独绑定；索引仅含收到的消息。已知消息链接可尝试官方复制/转发，不能枚举任意历史。\n'
        '5. 暂停保留剩余等待。解绑或用户失去管理权不停止已有任务，请主动停止任务/移除来源。\n'
        '按钮：#p 蓝色、#r 红色、#g 绿色；文字 - https://example.com；同行用 &&；'
        '自定义表情可直接发送或写 {emoji:ID}。\n'
        'UNKNOWN 表示结果不确定，先人工核查，再确认重发（可能重复）或跳过。\n'
        '请仅向自己有权管理的目标发布，遵守 Telegram 平台规则。')


def obj(value):
    return json.loads(value) if isinstance(value, str) else dict(value or {})


def safe(value, limit=160):
    return re.sub(r'\b\d{6,}:[A-Za-z0-9_-]{20,}\b', '[REDACTED]', str(value))[:limit]


def ids(text):
    values = [int(v) for v in re.split(r'[\s,，]+', text.strip()) if v]
    if not values or len(values) > 10000 or 0 in values:
        raise ValueError('请输入有效 ID 列表，以空格或逗号分隔。')
    return list(dict.fromkeys(values))


def csv_cell(value):
    text = '' if value is None else str(value)
    return "'" + text if text.startswith(('=', '+', '-', '@', '\t', '\r')) else text


class BotUI(BillingUIMixin):
    def __init__(self, db, bot, settings):
        self.db, self.bot, self.settings = db, bot, settings
        self.engine = Engine(db, bot, settings)
        self.icons = {}
        self.init_billing()

    def admin(self, uid):
        return uid in self.settings.admin_ids

    def require_admin(self, uid):
        if not self.admin(uid):
            raise PermissionError('仅管理员可操作。')

    def button(self, uid, label, action, style=None, icon=None):
        data = f'u:{uid}:{action}'
        if len(data.encode()) > 64:
            raise ValueError('回调数据过长')
        if icon is None:
            section=action.split(':',1)[0]
            category=('broadcast' if section in ('broadcast','bact','bexport') or 'broadcasts' in action else
                      'tasks' if section.startswith('task') or section in ('tact','tedit') or 'tasks' in action else
                      'content' if section.startswith('content') or section in ('cpreview','cversions','cedit') or 'contents' in action else
                      'chats' if section in ('bind','chat','source') or 'chats' in action or 'sources' in action else
                      'stats' if section in ('stats','logs','export') or (section == 'bpay' and any(v in action for v in ('member', 'orders', 'detail', 'check'))) else
                      'admin' if section in ('admin','user','ban','audit','badmin') else
                      'settings' if section.startswith('config') or section=='settings' else
                      'help' if section in ('help','contact') else 'menu')
            icon=self.icons.get(category)
        return menu_button(label, callback_data=data, style=style, icon_id=icon)

    def markup(self, uid, rows=(), back='home'):
        buttons = [[self.button(uid, *item) for item in row] for row in rows]
        if back is not None:
            buttons.append([self.button(uid, '返回', back), self.button(uid, '取消', 'home')])
        return InlineKeyboardMarkup(buttons)

    async def screen(self, uid, text, rows=(), query=None, back='home', markup=None):
        self.icons = obj(await self.config('icons', {}))
        markup = markup if markup is not None else self.markup(uid, rows, back)
        editing = bool(query and query.message)
        while True:
            try:
                await self.engine.throttle(uid, 'private')
                if editing:
                    return await self.bot.edit_message_text(chat_id=uid, message_id=query.message.message_id,
                                                            text=text[:4000], reply_markup=markup)
                return await self.bot.send_message(chat_id=uid, text=text[:4000], reply_markup=markup)
            except RetryAfter as exc:
                await self.rate_limited(uid, exc)
                raise ValueError('Telegram已限流，请稍后重试；原操作未自动重发。') from exc
            except BadRequest as exc:
                buttons = markup.to_dict()
                if _emoji_qualification_error(exc) and any('icon_custom_emoji_id' in b for row in buttons['inline_keyboard'] for b in row):
                    for row in buttons['inline_keyboard']:
                        for button in row:
                            button.pop('icon_custom_emoji_id', None)
                    markup = InlineKeyboardMarkup.de_json(buttons, self.bot)
                    await audit(self.db, uid, 'menu_icon_degraded', 'users', str(uid))
                    continue
                error = str(exc).lower()
                if editing and 'message is not modified' in error:
                    return query.message
                if editing and any(s in error for s in ('message to edit', "can't be edited", 'no text')):
                    editing = False
                    continue
                raise

    async def rate_limited(self, uid, exc):
        retry = exc.retry_after
        seconds = retry.total_seconds() if isinstance(retry, timedelta) else float(retry)
        await self.engine.cooldown(seconds)
        await audit(self.db, uid, 'ui_rate_limit', 'users', str(uid), {'retry_after': seconds})
        return seconds

    async def begin_binding(self, uid, kind, query=None):
        await self.entitlements.require(uid)
        request_id = secrets.randbelow(2_147_483_646)
        await self.prompt(uid, 'BIND', '每行一个 @用户名、t.me/用户名 或数字 Chat ID。也可点击底部选择器选择私密频道/群组。\n'
                          '必须验证你与机器人均为管理员；频道机器人还需发帖权限。',
                          {'kind': kind, 'request_id': request_id}, query)
        buttons = []
        for channel in (True, False):
            rights = dict(is_anonymous=False, can_manage_chat=True, can_delete_messages=False,
                          can_manage_video_chats=False, can_restrict_members=False, can_promote_members=False,
                          can_change_info=False, can_invite_users=False, can_post_stories=False,
                          can_edit_stories=False, can_delete_stories=False)
            if channel:
                rights['can_post_messages'] = True
            permissions = ChatAdministratorRights(**rights)
            request = KeyboardButtonRequestChat(request_id=request_id + int(channel), chat_is_channel=channel,
                                                user_administrator_rights=permissions, bot_administrator_rights=permissions,
                                                bot_is_member=True, request_title=True, request_username=True)
            buttons.append([KeyboardButton('选择频道' if channel else '选择群组', request_chat=request)])
        await self.engine.throttle(uid, 'private')
        await self.bot.send_message(chat_id=uid, text='选择已授权频道或群组',
                                    reply_markup=ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=True))

    async def state(self, uid):
        row = await self.db.fetchrow('SELECT * FROM sessions WHERE owner_id=$1', uid)
        if not row:
            return None
        if row['expires_at'] <= datetime.now(timezone.utc):
            await self.clear(uid)
            return {'state': 'EXPIRED', 'data': {}}
        return {'state': row['state'], 'data': obj(row['data'])}

    async def set_state(self, uid, state, data=None):
        data = dict(data or {})
        if state == 'BILLING_CONFIG_SECRET':
            data = {key: data[key] for key in ('merchant_id', 'nonce') if key in data}
        data.setdefault('nonce', secrets.token_hex(4))
        await self.db.execute('INSERT INTO sessions(owner_id,state,data,expires_at) VALUES($1,$2,$3,$4) '
                              'ON CONFLICT(owner_id) DO UPDATE SET state=EXCLUDED.state,data=EXCLUDED.data,'
                              'expires_at=EXCLUDED.expires_at,updated_at=now()',
                              uid, state, data, datetime.now(timezone.utc) + timedelta(minutes=15))
        return data

    async def clear(self, uid):
        await self.db.execute('DELETE FROM sessions WHERE owner_id=$1', uid)

    async def config(self, key, default=None):
        result = await self.db.fetchval('SELECT value FROM app_settings WHERE key=$1', key)
        return default if result is None else result

    async def resource(self, table, rid, uid):
        if table not in {'tasks', 'contents', 'user_chats', 'sources'}:
            raise ValueError('无效资源')
        row = await self.db.fetchrow(f'SELECT * FROM {table} WHERE id=$1', rid)
        if not row or row.get('deleted', False):
            raise ValueError('记录不存在或已删除。')
        if row['owner_id'] != uid:
            self.require_admin(uid)
            await audit(self.db, uid, 'admin_view', table, str(rid), {'owner_id': row['owner_id']})
        return row

    async def handle_update(self, update, *, billing_secret=False):
        if update.my_chat_member:
            member, chat = update.my_chat_member, update.my_chat_member.chat
            await self.db.execute('INSERT INTO chats(chat_id,type,title,username,bot_status,permissions) '
                                  'VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(chat_id) DO UPDATE SET '
                                  'type=EXCLUDED.type,title=EXCLUDED.title,username=EXCLUDED.username,'
                                  'bot_status=EXCLUDED.bot_status,permissions=EXCLUDED.permissions,updated_at=now()',
                                  chat.id, chat.type, chat.title or '', chat.username,
                                  str(member.new_chat_member.status), member.new_chat_member.to_dict())
            return
        user, chat, query = update.effective_user, update.effective_chat, update.callback_query
        if not user or not chat or chat.type != 'private' or chat.id != user.id:
            if query:
                await query.answer('请在机器人私聊内操作。', show_alert=True)
            return
        uid = user.id
        if update.message:
            incoming_text = update.message.text or update.message.caption or ''
            command = incoming_text.split()[0].split('@')[0].lower() if incoming_text.startswith('/') else ''
            secret_session = await self.state(uid) if not billing_secret and command != '/paytoken' else None
            if billing_secret or command == '/paytoken' or (secret_session and secret_session['state'] == 'BILLING_CONFIG_SECRET'):
                await self.billing_delete_secret(uid, update.message)
        await self.db.execute('INSERT INTO users(id,username,first_name,last_name,language_code) VALUES($1,$2,$3,$4,$5) '
                              'ON CONFLICT(id) DO UPDATE SET username=EXCLUDED.username,first_name=EXCLUDED.first_name,'
                              'last_name=EXCLUDED.last_name,language_code=EXCLUDED.language_code,last_seen=now(),blocked=false',
                              uid, user.username, user.first_name, user.last_name, user.language_code)
        status = await self.db.fetchrow('SELECT status,ban_reason FROM users WHERE id=$1', uid)
        if status and status['status'] == 'BANNED':
            await audit(self.db, uid, 'banned_request', 'users', str(uid))
            if query:
                await query.answer()
            return
        try:
            if query:
                prefix = f'u:{uid}:'
                if not query.data or not query.data.startswith(prefix):
                    await query.answer('此菜单不属于你，请重新 /start。', show_alert=True)
                    return
                await query.answer()
                await self.callback(uid, query.data[len(prefix):], query)
            elif update.message:
                if billing_secret:
                    handled = await self.billing_message(uid, update.message, await self.state(uid), force_secret=True)
                    if not handled:
                        await self.message(uid, update.message)
                else:
                    await self.message(uid, update.message)
        except (ValueError, PermissionError, ZoneInfoNotFoundError) as exc:
            await self.screen(uid, safe(exc, 1600), query=query)
        except RetryAfter as exc:
            seconds = await self.rate_limited(uid, exc)
            await self.screen(uid, f'Telegram已限流，需冷却{seconds:g}秒。请稍后重试；原操作未自动重发。')
        except TelegramError as exc:
            await self.screen(uid, 'Telegram 操作失败：' + safe(exc, 500) + '\n未自动重试，请检查状态。')

    async def home(self, uid, query=None):
        session = await self.state(uid)
        if session and session['state'] == 'BIND':
            await self.engine.throttle(uid, 'private')
            await self.bot.send_message(chat_id=uid, text='已取消选择', reply_markup=ReplyKeyboardRemove())
        await self.clear(uid)
        icons = obj(await self.config('icons', {}))
        entries = [('目标频道 / 群组', 'list:chats:0', 'chats'), ('发布内容', 'list:contents:0', 'content'),
                   ('轮播任务', 'list:tasks:0', 'tasks'), ('消息来源', 'list:sources:0', 'chats'),
                   ('发送统计', 'stats', 'stats'), ('个人设置', 'settings', 'settings'),
                   ('会员中心', 'bpay:member', 'stats'), ('购买 / 续费', 'bpay:shop', 'menu'),
                   ('我的订单', 'bpay:orders:0', 'stats'), ('使用教程', 'help', 'help'),
                   ('联系管理员', 'contact', 'help'), ('刷新菜单', 'home', 'menu')]
        styled = [(label, action, 'success' if action == 'bpay:shop' else 'primary' if action.startswith('bpay:') else None, icons.get(icon))
                  for label, action, icon in entries]
        rows = [styled[i:i + 2] for i in range(0, len(styled), 2)]
        if self.admin(uid):
            rows.append([('管理员', 'admin', 'primary', icons.get('admin'))])
        await self.screen(uid, await self.config('welcome', 'TG 自动广告机器人\n请选择操作'), rows, query, back=None)

    async def prompt(self, uid, state, text, data=None, query=None):
        await self.set_state(uid, state, data)
        await self.screen(uid, text, query=query)

    async def confirm(self, uid, state, data, text, query=None):
        data = await self.set_state(uid, state, data)
        await self.screen(uid, text, [[('确认', f"w:{data['nonce']}:confirm", 'danger')]], query)

    async def callback(self, uid, action, query):
        p = action.split(':')
        cmd = p[0]
        if await self.billing_callback(uid, p, query):
            return
        if cmd == 'home':
            return await self.home(uid, query)
        if cmd == 'w':
            session = await self.state(uid)
            if not session or session['state'] == 'EXPIRED' or session['data'].get('nonce') != p[1]:
                raise ValueError('此确认已过期或已使用，请重新开始。')
            if p[2:] == ['confirm']:
                # SQL compare-and-delete prevents simultaneous confirmations across workers.
                # Content commits consume the session in the version transaction instead.
                if session['state'] != 'CONTENT_CONFIRM' or session['data'].get('broadcast'):
                    consumed = await self.db.fetchrow('DELETE FROM sessions WHERE owner_id=$1 AND state=$2 '
                                                      "AND data->>'nonce'=$3 AND expires_at>now() RETURNING owner_id",
                                                      uid, session['state'], p[1])
                    if not consumed:
                        raise ValueError('此确认已过期或已使用，请重新开始。')
            else:
                await self.set_state(uid, session['state'], session['data'])
            return await self.session_action(uid, session, p[2:], query)
        await self.clear(uid)
        if cmd == 'list':
            return await self.listing(uid, p[1], int(p[2]), query, int(p[3]) if len(p) > 3 else None)
        if cmd == 'new':
            kind = p[1]
            if kind in {'chats', 'sources'}:
                return await self.begin_binding(uid, kind, query)
            if kind == 'contents':
                return await self.prompt(uid, 'CONTENT_TITLE', '请输入内容标题（最多80字）。', {}, query)
            if kind == 'tasks':
                await self.entitlements.require(uid)
                return await self.prompt(uid, 'TASK_NAME', '请输入任务名称（最多80字）。', {}, query)
            if kind == 'broadcasts':
                self.require_admin(uid)
                return await self.prompt(uid, 'CONTENT_BODY', '发送广播正文或媒体。仅私聊预览，确认后才广播。',
                                         {'broadcast': True, 'title': '全站广播'}, query)
        if cmd == 'chat':
            return await self.chat_detail(uid, int(p[1]), query)
        if cmd in {'check', 'test', 'unbind'}:
            row = await self.resource('user_chats', int(p[1]), uid)
            if cmd == 'check':
                chat = await self.verify_chat(row['owner_id'], row['chat_id'])
                return await self.screen(uid, f'{chat.title}\n用户与机器人权限验证通过。', query=query)
            if cmd == 'test':
                await self.entitlements.require(row['owner_id'])
                target, _ = await self.verify_bot(row['chat_id'])
                await self.engine.throttle(row['chat_id'], target.type)
                await self.entitlements.require(row['owner_id'])
                result = await deliver(self.bot, row['chat_id'], {'type': 'text', 'text': await self.config('test_message', '权限测试，30秒后自动删除。')})
                await self.db.execute("INSERT INTO message_logs(owner_id,chat_id,message_id,operation,status) VALUES($1,$2,$3,'TEST','SUCCESS')",
                                      row['owner_id'], row['chat_id'], result['message_id'])
                await self.engine.schedule_test_delete(row['owner_id'], row['chat_id'], result['message_id'], delay=30)
                return await self.screen(uid, '测试已发送，已保存30秒删除计划；实际删除结果见日志。', query=query)
            return await self.confirm(uid, 'UNBIND', {'id': row['id'], 'kind': 'chats'}, '仅解绑目标，不停止已有任务。确定？', query)
        if cmd == 'source':
            return await self.source_detail(uid, int(p[1]), query)
        if cmd == 'sunbind':
            row = await self.resource('sources', int(p[1]), uid)
            return await self.confirm(uid, 'UNBIND', {'id': row['id'], 'kind': 'sources'},
                                      '仅从新选择中隐藏，已有任务继续。真正停止需在任务中移除即时来源。确定？', query)
        if cmd == 'index':
            return await self.source_index(uid, int(p[1]), int(p[2]), query)
        if cmd == 'import':
            return await self.prompt(uid, 'SOURCE_LINK', '发送已知来源消息链接。来源须先绑定；将尝试官方复制/转发，不保证历史消息可用。', {}, query)
        if cmd == 'indexed':
            return await self.import_index(uid, int(p[1]), query)
        if cmd == 'content':
            return await self.content_detail(uid, int(p[1]), query)
        if cmd == 'cpreview':
            row = await self.resource('contents', int(p[1]), uid)
            version = await self.db.fetchrow('SELECT * FROM content_versions WHERE id=$1 AND content_id=$2', int(p[2]), row['id'])
            if not version:
                raise ValueError('版本不存在。')
            await self.engine.throttle(uid, 'private')
            await deliver(self.bot, uid, obj(version['payload']))
            return await self.content_detail(uid, row['id'], query)
        if cmd == 'versions':
            return await self.versions(uid, int(p[1]), int(p[2]), query)
        if cmd == 'cedit':
            row = await self.resource('contents', int(p[1]), uid)
            return await self.prompt(uid, 'CONTENT_BODY', '发送新版正文或媒体，确认后生成新版本。',
                                     {'content_id': row['id'], 'owner_id': row['owner_id'], 'title': row['title']}, query)
        if cmd == 'cdelete':
            row = await self.resource('contents', int(p[1]), uid)
            return await self.confirm(uid, 'CONTENT_DELETE', {'id': row['id']},
                                      '删除将禁止新选择，已有任务仍保留引用与历史；请先检查关联任务。确定删除？', query)
        if cmd == 'task':
            return await self.task_detail(uid, int(p[1]), query)
        if cmd == 'taskact':
            row = await self.resource('tasks', int(p[2]), uid)
            labels = {'start': '启动/恢复（保留暂停剩余等待）', 'pause': '暂停', 'stop': '停止',
                      'delete': '删除任务（保留历史）', 'run_now': '立即发布', 'retry': '重发UNKNOWN（可能重复）',
                      'skip': '跳过UNKNOWN（可能漏发）', 'remove_source': '永久移除即时来源'}
            if p[1] not in labels:
                raise ValueError('未知任务操作')
            if p[1] in {'start', 'run_now', 'retry'}:
                await self.entitlements.require(row['owner_id'])
            return await self.confirm(uid, 'TASK_ACTION', {'id': row['id'], 'action': p[1]}, f"确认{labels[p[1]]}：{row['name']}？", query)
        if cmd == 'tedit':
            return await self.task_edit(uid, int(p[1]), query)
        if cmd == 'stats':
            return await self.stats(uid, query)
        if cmd == 'logs':
            return await self.logs(uid, int(p[1]), query, int(p[2]) if len(p) > 2 else uid)
        if cmd == 'export':
            return await self.export_logs(uid, int(p[1]) if len(p) > 1 else uid)
        if cmd == 'settings':
            user = await self.db.fetchrow('SELECT timezone FROM users WHERE id=$1', uid)
            return await self.screen(uid, f"个人设置\n时区：{user['timezone']}", [[('修改时区', 'timezone')]], query)
        if cmd == 'timezone':
            return await self.prompt(uid, 'TIMEZONE', '输入 IANA 时区，例如 Asia/Shanghai 或 Europe/London。', {}, query)
        if cmd == 'help':
            return await self.screen(uid, await self.config('help_text', HELP), query=query)
        if cmd == 'contact':
            contact = await self.config('contact_admin', '联系管理员：@sad1881')
            return await self.screen(uid, contact, query=query)
        if cmd in {'admin', 'search', 'user', 'ban', 'config', 'configkey', 'broadcast', 'bact', 'bexport', 'audit'}:
            self.require_admin(uid)
            return await self.admin_callback(uid, p, query)
        raise ValueError('此菜单已失效，请 /start。')

    async def verify_bot(self, ref):
        chat = await self.bot.get_chat(ref)
        if chat.type not in {'channel', 'group', 'supergroup'}:
            raise ValueError('只支持频道、群组和超级群组，不接受普通用户。')
        me = await self.bot.get_me()
        member = await self.bot.get_chat_member(chat.id, me.id)
        if member.status not in {'administrator', 'creator'}:
            raise ValueError('机器人必须是管理员。')
        if chat.type == 'channel' and member.status != 'creator' and not member.can_post_messages:
            raise ValueError('机器人缺少频道发帖权限。')
        return chat, member

    async def verify_chat(self, owner, ref):
        chat, member = await self.verify_bot(ref)
        user = await self.bot.get_chat_member(chat.id, owner)
        if user.status not in {'administrator', 'creator'}:
            raise ValueError('你不是此频道/群组管理员，无法首次绑定。')
        await self.db.execute('INSERT INTO chats(chat_id,type,title,username,bot_status,permissions) VALUES($1,$2,$3,$4,$5,$6) '
                              'ON CONFLICT(chat_id) DO UPDATE SET type=EXCLUDED.type,title=EXCLUDED.title,'
                              'username=EXCLUDED.username,bot_status=EXCLUDED.bot_status,permissions=EXCLUDED.permissions,updated_at=now()',
                              chat.id, chat.type, chat.title or '', chat.username, member.status, member.to_dict())
        return chat

    async def bind(self, uid, text, kind):
        lines = list(dict.fromkeys(v.strip() for v in text.splitlines() if v.strip()))
        if not lines or len(lines) > 100:
            raise ValueError('每批请输入1到100个目标，可多次提交。')
        results, seen = [], set()
        counts = {'channel': 0, 'group': 0, 'failed': 0}
        for line in lines:
            try:
                chat = await self.verify_chat(uid, parse_chat_ref(line))
                if chat.id in seen:
                    results.append(f'{line}：重复，已忽略')
                    continue
                seen.add(chat.id)
                table = 'user_chats' if kind == 'chats' else 'sources'
                maximum = int(await self.config('max_bindings_per_user', 2000))
                async with self.db.pool.acquire() as con:
                    async with con.transaction():
                        await con.execute('SELECT pg_advisory_xact_lock($1)', uid)
                        await self.entitlements.require(uid, con=con, proposed_chat_ids=[chat.id] if kind == 'chats' else None)
                        exists = await con.fetchval(f'SELECT id FROM {table} WHERE owner_id=$1 AND chat_id=$2 AND active', uid, chat.id)
                        count = await con.fetchval(f'SELECT count(*) FROM {table} WHERE owner_id=$1 AND active', uid)
                        if not exists and count >= maximum:
                            raise ValueError('已达到个人绑定配额。')
                        if kind == 'chats':
                            await con.execute('INSERT INTO user_chats(owner_id,chat_id) VALUES($1,$2) ON CONFLICT(owner_id,chat_id) '
                                              'DO UPDATE SET active=true,authorized_at=now()', uid, chat.id)
                        else:
                            await con.execute('INSERT INTO sources(owner_id,chat_id,title,username) VALUES($1,$2,$3,$4) '
                                              'ON CONFLICT(owner_id,chat_id) DO UPDATE SET active=true,title=EXCLUDED.title,'
                                              'username=EXCLUDED.username,authorized_at=now()', uid, chat.id, chat.title or '', chat.username)
                counts['channel' if chat.type == 'channel' else 'group'] += 1
                results.append(f'{safe(chat.title, 70)} ({chat.id})：绑定成功')
            except RetryAfter:
                raise
            except (ValueError, TelegramError) as exc:
                counts['failed'] += 1
                results.append(f'{safe(line, 80)}：失败 {safe(exc, 150)}')
        await self.clear(uid)
        await self.engine.throttle(uid, 'private')
        await self.bot.send_message(chat_id=uid, text='选择器已关闭', reply_markup=ReplyKeyboardRemove())
        summary = f"检测完成：频道{counts['channel']} / 群组{counts['group']} / 失败{counts['failed']}\n"
        for start in range(0, len(results), 12):
            await self.screen(uid, summary + '\n'.join(results[start:start + 12]), [[('查看列表', f'list:{kind}:0')]])

    async def listing(self, uid, kind, page, query=None, owner=None):
        page, scope = max(0, page), uid if owner is None else owner
        global_kinds = {'users', 'broadcasts', 'alltasks', 'allcontents', 'allchats'}
        if scope != uid or kind in global_kinds:
            self.require_admin(uid)
        specs = {
            'chats': ('SELECT uc.id,c.title,c.chat_id FROM user_chats uc JOIN chats c USING(chat_id) WHERE uc.owner_id=$1 AND uc.active ORDER BY uc.id DESC LIMIT $2 OFFSET $3', 'chat', '目标频道 / 群组'),
            'sources': ('SELECT id,title,chat_id FROM sources WHERE owner_id=$1 AND active ORDER BY id DESC LIMIT $2 OFFSET $3', 'source', '消息来源'),
            'contents': ('SELECT id,title FROM contents WHERE owner_id=$1 AND NOT deleted ORDER BY id DESC LIMIT $2 OFFSET $3', 'content', '发布内容'),
            'tasks': ('SELECT id,name AS title,status FROM tasks WHERE owner_id=$1 AND NOT deleted ORDER BY id DESC LIMIT $2 OFFSET $3', 'task', '轮播任务'),
            'users': ("SELECT id,COALESCE(username,first_name,'') AS title,status FROM users WHERE $1::bigint=$1 ORDER BY id LIMIT $2 OFFSET $3", 'user', '用户列表'),
            'broadcasts': ('SELECT id,status AS title FROM broadcast_jobs WHERE $1::bigint=$1 ORDER BY id DESC LIMIT $2 OFFSET $3', 'broadcast', '广播列表'),
            'alltasks': ('SELECT id,name AS title,status FROM tasks WHERE $1::bigint=$1 AND NOT deleted ORDER BY id DESC LIMIT $2 OFFSET $3', 'task', '全部任务'),
            'allcontents': ('SELECT id,title FROM contents WHERE $1::bigint=$1 AND NOT deleted ORDER BY id DESC LIMIT $2 OFFSET $3', 'content', '全部内容'),
            'allchats': ('SELECT uc.id,c.title,c.chat_id FROM user_chats uc JOIN chats c USING(chat_id) WHERE $1::bigint=$1 AND uc.active ORDER BY uc.id DESC LIMIT $2 OFFSET $3', 'chat', '全部用户目标'),
        }
        if kind not in specs:
            raise ValueError('未知列表')
        sql, detail, title = specs[kind]
        records = await self.db.fetch(sql, scope, PAGE + 1, page * PAGE)
        rows = [[(f"#{r['id']} {safe(r['title'], 42)} {r.get('status', '')}", f"{detail}:{r['id']}")] for r in records[:PAGE]]
        suffix = f':{scope}' if owner is not None else ''
        nav = []
        if page:
            nav.append(('上一页', f'list:{kind}:{page - 1}{suffix}'))
        if len(records) > PAGE:
            nav.append(('下一页', f'list:{kind}:{page + 1}{suffix}'))
        if nav:
            rows.append(nav)
        if scope == uid and kind in {'chats', 'sources', 'contents', 'tasks', 'broadcasts'}:
            rows.append([('新增', f'new:{kind}', 'primary')])
        if kind == 'contents':
            rows.append([('导入来源链接', 'import')])
        if kind == 'users':
            rows.append([('搜索用户', 'search')])
        if kind in global_kinds or scope != uid:
            await audit(self.db, uid, 'admin_list', kind, str(scope))
        await self.screen(uid, f'{title} · 第{page + 1}页' + ('\n暂无记录。' if not records else ''), rows, query,
                          back='admin' if kind in global_kinds else 'home')

    async def chat_detail(self, uid, rid, query=None):
        row = await self.resource('user_chats', rid, uid)
        chat = await self.db.fetchrow('SELECT * FROM chats WHERE chat_id=$1', row['chat_id'])
        await self.screen(uid, f"{chat['title']}\nChat ID：{row['chat_id']}\n所属用户：{row['owner_id']}\n"
                          f"类型：{chat['type']}\n机器人：{chat['bot_status']}\n绑定：{row['active']}\n解绑或用户失去管理权后，已有任务继续。",
                          [[('重新检测', f'check:{rid}'), ('测试并30秒删除', f'test:{rid}')], [('解绑', f'unbind:{rid}', 'danger')]], query, 'list:chats:0')

    async def source_detail(self, uid, rid, query=None):
        row = await self.resource('sources', rid, uid)
        await self.screen(uid, f"来源 #{rid} {row['title']}\nChat ID：{row['chat_id']}\n绑定：{row['active']}\n仅展示已接收消息；解绑不停止已有任务。",
                          [[('浏览消息索引', f'index:{rid}:0')], [('解绑来源', f'sunbind:{rid}', 'danger')]], query, 'list:sources:0')

    async def source_index(self, uid, rid, page, query=None):
        source = await self.resource('sources', rid, uid)
        records = await self.db.fetch('SELECT sm.id,sm.message_id,sm.received_at FROM source_messages sm JOIN sources s '
                                      'ON s.chat_id=sm.chat_id WHERE s.id=$1 AND s.owner_id=$2 ORDER BY sm.message_id DESC LIMIT $3 OFFSET $4',
                                      rid, source['owner_id'], PAGE + 1, max(0, page) * PAGE)
        rows = [[(f"消息 {r['message_id']} · {str(r['received_at'])[:16]}", f"indexed:{r['id']}")] for r in records[:PAGE]]
        nav = []
        if page > 0:
            nav.append(('上一页', f'index:{rid}:{page - 1}'))
        if len(records) > PAGE:
            nav.append(('下一页', f'index:{rid}:{page + 1}'))
        if nav:
            rows.append(nav)
        await self.screen(uid, '来源消息索引' + ('\n暂无消息，不能回溯加入前历史。' if not records else ''), rows, query, f'source:{rid}')

    async def import_index(self, uid, rid, query=None):
        row = await self.db.fetchrow('SELECT sm.* FROM source_messages sm WHERE sm.id=$1 AND EXISTS'
                                     '(SELECT 1 FROM sources s WHERE s.chat_id=sm.chat_id AND s.owner_id=$2)', rid, uid)
        if not row and self.admin(uid):
            row = await self.db.fetchrow('SELECT * FROM source_messages WHERE id=$1', rid)
            if row:
                await audit(self.db, uid, 'admin_source_content', 'source_messages', str(rid))
        if not row:
            raise PermissionError('来源不存在或未曾授权，无法读取。')
        data = await self.set_state(uid, 'SOURCE_MODE', {'title': f"来源消息{row['message_id']}",
                                    'source_chat_id': row['chat_id'], 'source_message_id': row['message_id']})
        await self.screen(uid, '选择发布模式，再私聊预览。复制可加按钮；转发保留来源，不支持自定义按钮。',
                          [[('复制', f"w:{data['nonce']}:mode:copy"), ('转发', f"w:{data['nonce']}:mode:forward")]], query)

    async def message(self, uid, message):
        session = await self.state(uid)
        if await self.billing_message(uid, message, session):
            return
        text = message.text or message.caption or ''
        command = text.split()[0].split('@')[0].lower() if text.startswith('/') else ''
        if command in {'/start', '/menu', '/cancel'}:
            return await self.home(uid)
        if command == '/help':
            await self.clear(uid)
            return await self.screen(uid, await self.config('help_text', HELP))
        if command == '/admin':
            self.require_admin(uid)
            await self.clear(uid)
            return await self.admin_callback(uid, ['admin'], None)
        session = await self.state(uid)
        if not session:
            return await self.screen(uid, '请通过菜单选择操作，/start 返回主菜单。')
        state, data = session['state'], session['data']
        if state == 'EXPIRED':
            return await self.screen(uid, '会话已超过15分钟未操作，请重新开始。')
        await self.set_state(uid, state, data)
        if state == 'BIND':
            shared = getattr(message, 'chat_shared', None)
            if shared:
                request_id = data.get('request_id')
                if request_id is None or shared.request_id not in {request_id, request_id + 1}:
                    raise ValueError('聊天选择已失效，请重新打开绑定菜单。')
                # Never trust client-supplied title/username; re-fetch and verify both admins.
                text = str(shared.chat_id)
            return await self.bind(uid, text, data['kind'])
        if state == 'CONTENT_TITLE':
            if not 1 <= len(text.strip()) <= 80:
                raise ValueError('标题需要1到80字。')
            data['title'] = text.strip()
            return await self.prompt(uid, 'CONTENT_BODY', '发送正文或单条媒体，保留收到的格式与自定义表情。', data)
        if state == 'CONTENT_BODY':
            data.pop('received_entities', None)
            data['payload'] = capture_message(message)
            return await self.content_options(uid, data)
        if state == 'CONTENT_BUTTONS':
            entities = [e.to_dict() for e in (message.entities or message.caption_entities or [])]
            data['payload']['buttons'] = [] if text.strip() == '-' else parse_buttons(text, entities)
            data.pop('previewed', None)
            return await self.content_options(uid, data)
        if state == 'SOURCE_LINK':
            ref, mid = parse_source_link(text.strip())
            chat = await self.bot.get_chat(ref)
            source = await self.db.fetchrow('SELECT id FROM sources WHERE chat_id=$1 AND owner_id=$2', chat.id, uid)
            if not source:
                raise PermissionError('来源未授权，请先绑定来源。')
            data = await self.set_state(uid, 'SOURCE_MODE', {'title': f'来源消息{mid}',
                                       'source_chat_id': chat.id, 'source_message_id': mid})
            return await self.screen(uid, '已知消息链接可尝试官方复制/转发，成功与否以私聊预览为准。不能枚举或读取任意历史。',
                                     [[('复制', f"w:{data['nonce']}:mode:copy"), ('转发', f"w:{data['nonce']}:mode:forward")]])
        if state == 'TASK_NAME':
            if not 1 <= len(text.strip()) <= 80:
                raise ValueError('任务名需要1到80字。')
            data.update(name=text.strip(), content_ids=[], chat_ids=[], delete_previous=False, instant_source_id=None,
                        source_mode='copy', max_runs=0, skip_failed=False, delete_failure_pause=False)
            return await self.task_selection(uid, data, 'contents', 0)
        if state == 'TASK_INTERVAL':
            value = parse_interval(text.strip())
            minimum = int(await self.config('min_interval_seconds', 60))
            if value < minimum:
                raise ValueError(f'当前最小间隔为{minimum}秒。')
            data['interval_seconds'] = value
            return await self.task_flags(uid, data)
        if state == 'TASK_RUNS':
            if not text.strip().isdigit() or len(text.strip()) > 9:
                raise ValueError('请输入0到999999999，0表示无限轮次。')
            data['max_runs'] = int(text.strip())
            return await self.task_preview(uid, data)
        if state == 'TIMEZONE':
            ZoneInfo(text.strip())
            await self.db.execute('UPDATE users SET timezone=$2 WHERE id=$1', uid, text.strip())
            await self.clear(uid)
            return await self.screen(uid, '时区已保存。')
        if state == 'USER_SEARCH':
            self.require_admin(uid)
            return await self.search_users(uid, text)
        if state == 'BAN_REASON':
            self.require_admin(uid)
            if not 1 <= len(text.strip()) <= 500:
                raise ValueError('封禁原因需要1到500字。')
            data['reason'] = text.strip()
            return await self.confirm(uid, 'BAN_CONFIRM', data, f"确认封禁用户 {data['user_id']}？\n原因：{text.strip()}\n将暂停其运行任务，解封不自动启动。")
        if state == 'CONFIG_VALUE':
            self.require_admin(uid)
            key = data['key']
            typ, low, high = CONFIG[key]
            value = int(text.strip()) if typ is int else text
            size = value if typ is int else len(value)
            if not low <= size <= high:
                raise ValueError(f'允许范围/长度为 {low} 到 {high}。')
            data['value'] = value
            return await self.confirm(uid, 'CONFIG_CONFIRM', data, f'确认更新 {key}？\n{safe(value, 2800)}')
        raise ValueError('请使用当前步骤的按钮，或 /cancel 取消。')

    async def content_options(self, uid, data, query=None):
        data.pop('previewed', None)
        data = await self.set_state(uid, 'CONTENT_OPTIONS', data)
        p, nonce = data['payload'], data['nonce']
        rows = [[('收到的格式', f'w:{nonce}:format:received'), ('纯文本', f'w:{nonce}:format:plain')],
                [('HTML', f'w:{nonce}:format:HTML'), ('MarkdownV2', f'w:{nonce}:format:MarkdownV2')]]
        if p['type'] != 'forward':
            rows.append([('编辑按钮', f'w:{nonce}:buttons')])
        rows += [[('切换保护内容', f'w:{nonce}:protect')], [('预览', f'w:{nonce}:preview', 'primary')],
                 [('重发正文', f'w:{nonce}:body')]]
        await self.screen(uid, f"{data['title']}\n类型：{p['type']}\n格式：{p.get('parse_mode', '原生实体')}\n"
                          f"保护内容：{p.get('protect_content', False)}\n按钮行：{len(p.get('buttons', []))}\n预览成功后才能确认。", rows, query)

    async def content_preview(self, uid, data, query=None):
        if data.get('broadcast'):
            self.require_admin(uid)
        await self.engine.throttle(uid, 'private')
        result = await deliver(self.bot, uid, data['payload'])
        data['previewed'] = True
        data = await self.set_state(uid, 'CONTENT_CONFIRM', data)
        suffix = '\n自定义表情已降级为原文字。' if result.get('degraded') else ''
        await self.screen(uid, ('广播预览：确认将向所有符合条件的真实用户广播！' if data.get('broadcast') else '内容预览成功，确认保存？') + suffix,
                          [[('确认广播' if data.get('broadcast') else '确认保存', f"w:{data['nonce']}:confirm", 'danger' if data.get('broadcast') else 'success')],
                           [('继续编辑', f"w:{data['nonce']}:options")]], query)

    async def save_content(self, uid, data):
        owner = data.get('owner_id', uid)
        if owner != uid:
            self.require_admin(uid)
        async with self.db.pool.acquire() as con:
            async with con.transaction():
                session = await con.fetchrow('SELECT state,data FROM sessions WHERE owner_id=$1 FOR UPDATE', uid)
                if not session or session['state'] != 'CONTENT_CONFIRM' or obj(session['data']).get('nonce') != data['nonce']:
                    raise ValueError('确认已使用，请返回内容列表。')
                cid = data.get('content_id')
                if cid:
                    row = await con.fetchrow('SELECT * FROM contents WHERE id=$1 AND owner_id=$2 AND NOT deleted FOR UPDATE', cid, owner)
                    if not row:
                        raise PermissionError('内容不存在或无权编辑。')
                else:
                    cid = await con.fetchval('INSERT INTO contents(owner_id,title) VALUES($1,$2) RETURNING id', owner, data['title'])
                number = await con.fetchval('SELECT COALESCE(max(version_no),0)+1 FROM content_versions WHERE content_id=$1', cid)
                vid = await con.fetchval('INSERT INTO content_versions(content_id,version_no,payload) VALUES($1,$2,$3) RETURNING id', cid, number, data['payload'])
                await con.execute('UPDATE contents SET current_version_id=$2,updated_at=now() WHERE id=$1', cid, vid)
                await con.execute('DELETE FROM sessions WHERE owner_id=$1', uid)
        if owner != uid:
            await audit(self.db, uid, 'admin_edit_content', 'contents', str(cid), {'owner_id': owner, 'version': number})
        return cid

    async def content_detail(self, uid, cid, query=None):
        row = await self.resource('contents', cid, uid)
        version = await self.db.fetchrow('SELECT * FROM content_versions WHERE id=$1', row['current_version_id'])
        await self.screen(uid, f"内容 #{cid} {row['title']}\n所属用户：{row['owner_id']}\n当前版本：{version['version_no'] if version else '无'}\n新版本不会修改历史事件快照。",
                          [[('预览', f"cpreview:{cid}:{row['current_version_id']}")],
                           [('编辑新版本', f'cedit:{cid}'), ('历史版本', f'versions:{cid}:0')], [('删除', f'cdelete:{cid}', 'danger')]], query, 'list:contents:0')

    async def versions(self, uid, cid, page, query=None):
        await self.resource('contents', cid, uid)
        records = await self.db.fetch('SELECT id,version_no,created_at FROM content_versions WHERE content_id=$1 ORDER BY version_no DESC LIMIT $2 OFFSET $3', cid, PAGE + 1, max(0, page) * PAGE)
        rows = [[(f"版本 {r['version_no']} · {str(r['created_at'])[:16]}", f"cpreview:{cid}:{r['id']}")] for r in records[:PAGE]]
        nav = []
        if page:
            nav.append(('上一页', f'versions:{cid}:{page - 1}'))
        if len(records) > PAGE:
            nav.append(('下一页', f'versions:{cid}:{page + 1}'))
        if nav:
            rows.append(nav)
        await self.screen(uid, '历史版本（只读）' + ('\n暂无版本。' if not records else ''), rows, query, f'content:{cid}')

    async def session_action(self, uid, session, p, query):
        state, data, verb = session['state'], session['data'], p[0]
        if state in {'CONTENT_OPTIONS', 'CONTENT_CONFIRM'}:
            if verb == 'options':
                return await self.content_options(uid, data, query)
            if verb == 'body':
                return await self.prompt(uid, 'CONTENT_BODY', '重新发送正文或媒体。', data, query)
            if state == 'CONTENT_OPTIONS':
                payload = data['payload']
                if verb == 'format':
                    if payload['type'] in {'forward', 'copy'}:
                        raise ValueError('来源引用不支持修改正文格式；请发送正文/媒体创建快照。')
                    mode = p[1]
                    if mode not in {'received', 'plain', 'HTML', 'MarkdownV2'}:
                        raise ValueError('无效格式')
                    data.setdefault('received_entities', payload.get('entities', []))
                    payload.pop('parse_mode', None)
                    payload['entities'] = data['received_entities'] if mode == 'received' else []
                    if mode in {'HTML', 'MarkdownV2'}:
                        payload['parse_mode'] = mode
                    return await self.content_options(uid, data, query)
                if verb == 'buttons':
                    if payload['type'] == 'forward':
                        raise ValueError('转发不支持按钮。')
                    return await self.prompt(uid, 'CONTENT_BUTTONS', '输入按钮，同行用 &&，换行新行。例：\n#p 官网 - https://example.com\n发送 - 清空按钮。\n可直接发送会员表情或 {emoji:ID}。', data, query)
                if verb == 'protect':
                    payload['protect_content'] = not payload.get('protect_content', False)
                    return await self.content_options(uid, data, query)
                if verb == 'preview':
                    return await self.content_preview(uid, data, query)
            if state == 'CONTENT_CONFIRM' and verb == 'confirm' and data.get('previewed'):
                if data.get('broadcast'):
                    self.require_admin(uid)
                    # Consume confirmation before creating a real broadcast: stale clicks never send twice.
                    await self.clear(uid)
                    job = await self.engine.create_broadcast(uid, data['payload'])
                    await audit(self.db, uid, 'create_broadcast', 'broadcast_jobs', str(job))
                    return await self.broadcast_detail(uid, job)
                cid = await self.save_content(uid, data)
                return await self.content_detail(uid, cid, query)
        if state == 'SOURCE_MODE' and verb == 'mode' and p[1] in {'copy', 'forward'}:
            data['payload'] = {'type': p[1], 'source_chat_id': data['source_chat_id'], 'source_message_id': data['source_message_id'], 'buttons': []}
            return await self.content_options(uid, data, query)
        if state == 'UNBIND' and verb == 'confirm':
            table = 'user_chats' if data['kind'] == 'chats' else 'sources'
            row = await self.resource(table, data['id'], uid)
            await self.db.execute(f'UPDATE {table} SET active=false WHERE id=$1 AND owner_id=$2', row['id'], row['owner_id'])
            await audit(self.db, uid, 'unbind', table, str(row['id']), {'owner_id': row['owner_id'], 'tasks_continue': True})
            await self.clear(uid)
            return await self.screen(uid, '已解绑，已有任务未被停止。', query=query)
        if state == 'CONTENT_DELETE' and verb == 'confirm':
            row = await self.resource('contents', data['id'], uid)
            await self.db.execute('UPDATE contents SET deleted=true,updated_at=now() WHERE id=$1 AND owner_id=$2', row['id'], row['owner_id'])
            await audit(self.db, uid, 'delete_content', 'contents', str(row['id']))
            await self.clear(uid)
            return await self.listing(uid, 'contents', 0, query)
        if state.startswith('TASK_'):
            return await self.task_session_action(uid, state, data, p, query)
        if state == 'BAN_CONFIRM' and verb == 'confirm':
            self.require_admin(uid)
            await self.set_ban(uid, data)
            await self.clear(uid)
            return await self.user_detail(uid, data['user_id'], query)
        if state == 'CONFIG_CONFIRM' and verb == 'confirm':
            self.require_admin(uid)
            old = await self.config(data['key'])
            await self.db.execute('INSERT INTO app_settings(key,value) VALUES($1,$2) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()', data['key'], data['value'])
            await audit(self.db, uid, 'config_update', 'app_settings', data['key'], {'before': old, 'after': data['value']})
            await self.clear(uid)
            return await self.admin_callback(uid, ['config'], query)
        if state == 'BROADCAST_ACTION' and verb == 'confirm':
            self.require_admin(uid)
            await self.engine.broadcast_action(data['id'], uid, data['action'])
            await audit(self.db, uid, 'broadcast_' + data['action'], 'broadcast_jobs', str(data['id']))
            await self.clear(uid)
            return await self.broadcast_detail(uid, data['id'], query)
        raise ValueError('步骤已改变，请使用最新菜单。')

    async def task_selection(self, uid, data, kind, page, query=None):
        owner = data.get('owner_id', uid)
        if owner != uid:
            self.require_admin(uid)
        data.update(selection_kind=kind, selection_page=max(0, page))
        data = await self.set_state(uid, 'TASK_SELECTION', data)
        nonce = data['nonce']
        if kind == 'contents':
            sql = 'SELECT id,title FROM contents WHERE owner_id=$1 AND NOT deleted ORDER BY id LIMIT $2 OFFSET $3'
            selected = data['content_ids']
        else:
            sql = 'SELECT c.chat_id AS id,c.title FROM user_chats uc JOIN chats c USING(chat_id) WHERE uc.owner_id=$1 AND uc.active ORDER BY uc.id LIMIT $2 OFFSET $3'
            selected = data['chat_ids']
        records = await self.db.fetch(sql, owner, PAGE + 1, data['selection_page'] * PAGE)
        rows = [[(f"{'✓' if r['id'] in selected else '○'} {r['id']} {safe(r['title'], 32)}", f"w:{nonce}:toggle:{r['id']}")] for r in records[:PAGE]]
        nav = []
        if page:
            nav.append(('上一页', f'w:{nonce}:page:{page - 1}'))
        if len(records) > PAGE:
            nav.append(('下一页', f'w:{nonce}:page:{page + 1}'))
        if nav:
            rows.append(nav)
        rows += [[('全选', f'w:{nonce}:all'), ('清空', f'w:{nonce}:none')], [('下一步', f'w:{nonce}:next', 'primary')]]
        await self.screen(uid, ('选择轮播内容，按点击顺序轮播。' if kind == 'contents' else '选择发布目标。') +
                          f'\n已选 {len(selected)} 项 · 第{page + 1}页' + ('\n暂无可选记录，请先创建。' if not records else ''), rows, query)

    async def task_flags(self, uid, data, query=None):
        data = await self.set_state(uid, 'TASK_FLAGS', data)
        n = data['nonce']
        rows = [[(f"{'开启' if data[key] else '关闭'} · {label}", f'w:{n}:flag:{key}')]
                for key, label in [('delete_previous', '发送前删旧'), ('skip_failed', '失败跳过'), ('delete_failure_pause', '删除失败暂停')]]
        rows.append([('选择即时来源', f'w:{n}:sources:0'), ('不启用即时来源', f'w:{n}:source:0')])
        await self.screen(uid, f"发送间隔：{data['interval_seconds']}秒\n设置发送策略。删除失败默认继续并记录。", rows, query)

    async def task_sources(self, uid, data, page, query=None):
        owner = data.get('owner_id', uid)
        records = await self.db.fetch('SELECT id,title FROM sources WHERE owner_id=$1 AND active ORDER BY id LIMIT $2 OFFSET $3', owner, PAGE + 1, max(0, page) * PAGE)
        data = await self.set_state(uid, 'TASK_SOURCES', data)
        n = data['nonce']
        rows = [[(f"#{r['id']} {safe(r['title'], 45)}", f"w:{n}:source:{r['id']}")] for r in records[:PAGE]]
        nav = []
        if page:
            nav.append(('上一页', f'w:{n}:sources:{page - 1}'))
        if len(records) > PAGE:
            nav.append(('下一页', f'w:{n}:sources:{page + 1}'))
        if nav:
            rows.append(nav)
        rows.append([('不启用即时来源', f'w:{n}:source:0')])
        await self.screen(uid, '选择新消息即时触发来源' + ('\n暂无已绑定来源。' if not records else ''), rows, query)

    async def task_preview(self, uid, data, query=None):
        data = await self.set_state(uid, 'TASK_CONFIRM', data)
        text = (f"任务预览：{data['name']}\n内容（顺序）：{', '.join(map(str, data['content_ids']))}\n"
                f"目标：{len(data['chat_ids'])}个\n间隔：{data['interval_seconds']}秒\n"
                f"删旧：{data['delete_previous']}\n失败跳过：{data['skip_failed']}\n删除失败暂停：{data['delete_failure_pause']}\n"
                f"即时来源：{data.get('instant_source_id') or '无'} / {data['source_mode']}\n"
                f"有限轮次：{data['max_runs'] or '无限'}\n确认只保存，不自动启动。暂停恢复保留剩余等待。已完成任务编辑后转为停止并重置轮次。")
        await self.screen(uid, text, [[('确认保存', f"w:{data['nonce']}:confirm", 'success')]], query)

    async def task_edit(self, uid, tid, query=None):
        row = await self.resource('tasks', tid, uid)
        if row['status'] not in {'DRAFT', 'PAUSED', 'STOPPED', 'COMPLETED'}:
            raise ValueError('仅草稿、暂停、停止或已完成任务可编辑。存在未完成事件时请先停止。')
        contents = await self.db.fetch('SELECT content_id FROM task_items WHERE task_id=$1 ORDER BY position', tid)
        targets = await self.db.fetch('SELECT chat_id FROM task_targets WHERE task_id=$1 AND active ORDER BY id', tid)
        data = {key: row[key] for key in ('name', 'owner_id', 'interval_seconds', 'delete_previous', 'instant_source_id', 'source_mode', 'max_runs', 'skip_failed', 'delete_failure_pause')}
        data.update(edit_id=tid, content_ids=[r['content_id'] for r in contents], chat_ids=[r['chat_id'] for r in targets])
        await self.task_selection(uid, data, 'contents', 0, query)

    async def task_session_action(self, uid, state, data, p, query):
        verb, owner = p[0], data.get('owner_id', uid)
        if owner != uid:
            self.require_admin(uid)
        if state == 'TASK_SELECTION':
            kind, page = data['selection_kind'], data['selection_page']
            key = 'content_ids' if kind == 'contents' else 'chat_ids'
            if verb in {'toggle', 'all'}:
                if kind == 'contents':
                    sql = 'SELECT id FROM contents WHERE owner_id=$1 AND NOT deleted'
                else:
                    sql = 'SELECT chat_id AS id FROM user_chats WHERE owner_id=$1 AND active'
                if verb == 'toggle':
                    rid = int(p[1])
                    exists = await self.db.fetchval(sql + (' AND id=$2' if kind == 'contents' else ' AND chat_id=$2'), owner, rid)
                    if exists is None:
                        raise PermissionError('此项目不属于任务用户，或已解绑/删除。')
                    if rid in data[key]:
                        data[key].remove(rid)
                    else:
                        data[key].append(rid)
                else:
                    records = await self.db.fetch(sql + ' ORDER BY id', owner)
                    data[key] = [r['id'] for r in records]
                if kind == 'chats' and len(data[key]) > int(await self.config('max_targets_per_task', 1000)):
                    raise ValueError('超过单任务目标配额，请分批建立任务。')
                return await self.task_selection(uid, data, kind, page, query)
            if verb == 'none':
                data[key] = []
                return await self.task_selection(uid, data, kind, page, query)
            if verb == 'page':
                return await self.task_selection(uid, data, kind, max(0, int(p[1])), query)
            if verb == 'next':
                if not data[key]:
                    raise ValueError('至少选择一项。')
                if kind == 'contents':
                    return await self.task_selection(uid, data, 'chats', 0, query)
                return await self.prompt(uid, 'TASK_INTERVAL', '请输入间隔：50 = 50分钟，1:30 = 1小时30分钟，最少1分钟。', data, query)
        if state in {'TASK_FLAGS', 'TASK_SOURCES'}:
            if verb == 'flag' and state == 'TASK_FLAGS' and p[1] in {'delete_previous', 'skip_failed', 'delete_failure_pause'}:
                data[p[1]] = not data[p[1]]
                return await self.task_flags(uid, data, query)
            if verb == 'sources':
                return await self.task_sources(uid, data, max(0, int(p[1])), query)
            if verb == 'source':
                sid = int(p[1])
                if sid:
                    row = await self.resource('sources', sid, uid)
                    if row['owner_id'] != owner or not row['active']:
                        raise PermissionError('来源不属于任务用户或已解绑。')
                    data['instant_source_id'] = sid
                    data = await self.set_state(uid, 'TASK_MODE', data)
                    return await self.screen(uid, '选择即时来源发布模式。', [[('复制', f"w:{data['nonce']}:mode:copy"), ('转发', f"w:{data['nonce']}:mode:forward")]], query)
                data['instant_source_id'] = None
                return await self.prompt(uid, 'TASK_RUNS', '输入最多执行轮次，0为无限。', data, query)
        if state == 'TASK_MODE' and verb == 'mode' and p[1] in {'copy', 'forward'}:
            data['source_mode'] = p[1]
            return await self.prompt(uid, 'TASK_RUNS', '输入最多执行轮次，0为无限。', data, query)
        if state == 'TASK_CONFIRM' and verb == 'confirm':
            fields = {key: data[key] for key in ('name', 'content_ids', 'chat_ids', 'interval_seconds', 'delete_previous', 'instant_source_id', 'source_mode', 'max_runs', 'skip_failed', 'delete_failure_pause')}
            await self.clear(uid)
            if data.get('edit_id'):
                row = await self.resource('tasks', data['edit_id'], uid)
                targets = await self.db.fetch('SELECT chat_id FROM task_targets WHERE task_id=$1 AND active', row['id'])
                if set(fields['chat_ids']) - {r['chat_id'] for r in targets}:
                    await self.entitlements.require(row['owner_id'], proposed_chat_ids=fields['chat_ids'], exclude_task_id=row['id'])
                await self.engine.edit_task(row['id'], row['owner_id'], **fields)
                tid = row['id']
            else:
                await self.entitlements.require(owner, proposed_chat_ids=fields['chat_ids'])
                tid = await self.engine.create_task(owner, **fields)
            await audit(self.db, uid, 'save_task', 'tasks', str(tid), {'owner_id': owner, 'edited': bool(data.get('edit_id'))})
            return await self.task_detail(uid, tid, query)
        if state == 'TASK_ACTION' and verb == 'confirm':
            row = await self.resource('tasks', data['id'], uid)
            action = data['action']
            if action in {'start', 'run_now', 'retry'}:
                await self.entitlements.require(row['owner_id'])
            await self.clear(uid)
            if action in {'retry', 'skip'}:
                await self.engine.resolve_unknown(row['id'], row['owner_id'], action)
            else:
                method = {'start': 'start_task', 'pause': 'pause_task', 'stop': 'stop_task', 'delete': 'delete_task', 'run_now': 'run_now', 'remove_source': 'remove_source'}[action]
                await getattr(self.engine, method)(row['id'], row['owner_id'])
            await audit(self.db, uid, 'task_' + action, 'tasks', str(row['id']), {'owner_id': row['owner_id'], 'before': row['status']})
            if action == 'delete':
                return await self.listing(uid, 'tasks', 0, query)
            return await self.task_detail(uid, row['id'], query)
        raise ValueError('任务步骤已改变，请重新开始。')

    async def task_detail(self, uid, tid, query=None):
        row = await self.resource('tasks', tid, uid)
        counts = await self.db.fetchrow('SELECT count(*) AS total FROM task_targets WHERE task_id=$1 AND active', tid)
        unknown = await self.db.fetchval("SELECT count(*) FROM deliveries d JOIN publication_events e ON e.id=d.event_id WHERE e.task_id=$1 AND d.status='UNKNOWN'", tid)
        items = await self.db.fetch('SELECT content_id FROM task_items WHERE task_id=$1 ORDER BY position', tid)
        rows = [[('启动 / 恢复', f'taskact:start:{tid}', 'success'), ('暂停', f'taskact:pause:{tid}')],
                [('停止', f'taskact:stop:{tid}', 'danger'), ('立即发布', f'taskact:run_now:{tid}')],
                [('编辑配置', f'tedit:{tid}'), ('删除任务', f'taskact:delete:{tid}', 'danger')]]
        if row['instant_source_id']:
            rows.append([('确认移除即时来源', f'taskact:remove_source:{tid}', 'danger')])
        if unknown:
            rows.append([('UNKNOWN重发', f'taskact:retry:{tid}', 'danger'), ('UNKNOWN跳过', f'taskact:skip:{tid}')])
        text = (f"任务 #{tid} {row['name']}\n所属用户：{row['owner_id']}\n状态：{row['status']}\n"
                f"目标：{counts['total']}\n内容顺序：{','.join(str(r['content_id']) for r in items)}\n"
                f"间隔：{row['interval_seconds']}秒\n完成：{row['completed_runs']} / {row['max_runs'] or '无限'}\n"
                f"下次：{row['next_run_at']}\n暂停剩余：{row['remaining_seconds']}秒\n"
                f"即时来源：{row['instant_source_id'] or '无'}\nUNKNOWN：{unknown}\n"
                f"最近错误：{safe(row['last_error'], 500)}\n暂停恢复保留剩余等待；解绑不停止既有任务。")
        await self.screen(uid, text, rows, query, 'list:tasks:0')

    async def stats(self, uid, query=None):
        user = await self.db.fetchrow('SELECT timezone FROM users WHERE id=$1', uid)
        tz = user['timezone'] if user else 'Asia/Shanghai'
        daily = await self.db.fetch('SELECT operation,status,count(*) AS n FROM message_logs WHERE owner_id=$1 '
                                    'AND (created_at AT TIME ZONE $2)::date=(now() AT TIME ZONE $2)::date GROUP BY operation,status', uid, tz)
        tasks = await self.db.fetch('SELECT status,count(*) AS n FROM tasks WHERE owner_id=$1 AND NOT deleted GROUP BY status', uid)
        targets = await self.db.fetchval('SELECT count(*) FROM user_chats WHERE owner_id=$1 AND active', uid)
        last = await self.db.fetchrow('SELECT created_at,status,error FROM message_logs WHERE owner_id=$1 ORDER BY id DESC LIMIT 1', uid)
        text = '今日统计 · ' + tz + '\n' + ('\n'.join(f"{r['operation']} / {r['status']}：{r['n']}" for r in daily) or '暂无发送/删除记录。')
        text += '\n任务：' + (', '.join(f"{r['status']} {r['n']}" for r in tasks) or '无') + f'\n已绑定目标：{targets}'
        if last:
            text += f"\n最近记录：{last['created_at']} {last['status']} {safe(last['error'], 300)}"
        await self.screen(uid, text, [[('发送与删除记录', 'logs:0'), ('导出CSV', 'export')]], query)

    async def logs(self, uid, page, query=None, owner=None):
        owner = uid if owner is None else owner
        if owner != uid:
            self.require_admin(uid)
            await audit(self.db, uid, 'admin_logs', 'users', str(owner))
        rows = await self.db.fetch('SELECT l.*,c.username FROM message_logs l LEFT JOIN chats c USING(chat_id) '
                                   'WHERE l.owner_id=$1 ORDER BY l.id DESC LIMIT $2 OFFSET $3', owner, PAGE + 1, max(0, page) * PAGE)
        text = ['消息记录' + ('\n暂无记录。' if not rows else '')]
        for row in rows[:PAGE]:
            link = f"https://t.me/{row['username']}/{row['message_id']}" if row['username'] and row['message_id'] else '暂无公开消息链接'
            text.append(f"#{row['id']} {str(row['created_at'])[:19]}\n{row['operation']} {row['status']} Chat {row['chat_id']}\n"
                        f"{safe(row['error'], 130)}\n{link}")
        nav = []
        if page:
            nav.append(('上一页', f'logs:{page - 1}:{owner}'))
        if len(rows) > PAGE:
            nav.append(('下一页', f'logs:{page + 1}:{owner}'))
        buttons = [nav] if nav else []
        buttons.append([('导出CSV', f'export:{owner}')])
        await self.screen(uid, '\n\n'.join(text), buttons, query)

    async def send_csv(self, uid, records, name):
        stream = io.StringIO(newline='')
        writer = csv.writer(stream)
        if records:
            writer.writerow(list(records[0].keys()))
            writer.writerows([[csv_cell(safe(v, 4000)) for v in r.values()] for r in records])
        else:
            writer.writerow(['暂无记录'])
        await self.engine.throttle(uid, 'private')
        await self.bot.send_document(chat_id=uid, document=InputFile(io.BytesIO(stream.getvalue().encode('utf-8-sig')), filename=name))

    async def export_logs(self, uid, owner):
        if owner != uid:
            self.require_admin(uid)
            await audit(self.db, uid, 'admin_export_logs', 'users', str(owner))
        # Bounded keyset chunks avoid pulling lifetime history into memory.
        cursor, part = 0, 1
        while True:
            records = await self.db.fetch('SELECT id,task_id,chat_id,message_id,operation,status,error,degraded,payload_version_id,created_at '
                                          'FROM message_logs WHERE owner_id=$1 AND id>$2 ORDER BY id LIMIT 10000', owner, cursor)
            if not records:
                if part == 1:
                    await self.send_csv(uid, [], 'logs.csv')
                return
            await self.send_csv(uid, records, f'logs-{owner}-{part}.csv')
            cursor, part = records[-1]['id'], part + 1
            if len(records) < 10000:
                return

    async def admin_callback(self, uid, p, query):
        self.require_admin(uid)
        cmd = p[0]
        if cmd == 'admin':
            totals = await self.db.fetchrow("SELECT count(*) AS total,count(*) FILTER(WHERE status='BANNED') banned,"
                                            "count(*) FILTER(WHERE first_seen::date=current_date) new,"
                                            "count(*) FILTER(WHERE last_seen>now()-interval '1 day') active FROM users")
            queued = await self.db.fetchval("SELECT count(*) FROM deliveries WHERE status IN ('PENDING','RETRY','SENDING','UNKNOWN')")
            tasks = await self.db.fetchval("SELECT count(*) FROM tasks WHERE status='RUNNING' AND NOT deleted")
            chats = await self.db.fetch('SELECT c.type,count(*) AS n FROM user_chats uc JOIN chats c USING(chat_id) WHERE uc.active GROUP BY c.type')
            today = await self.db.fetch('SELECT operation,status,count(*) AS n FROM message_logs WHERE created_at::date=current_date GROUP BY operation,status')
            text = (f"管理员总览（服务器当日）\n用户：{totals['total']} · 今日新增：{totals['new']} · 24h活跃：{totals['active']}\n"
                    f"封禁：{totals['banned']} · 运行任务：{tasks} · 待处理/未知：{queued}\n")
            text += '\n'.join(f"{r['type']}绑定：{r['n']}" for r in chats) + '\n'
            text += '\n'.join(f"{r['operation']} {r['status']}：{r['n']}" for r in today)
            return await self.screen(uid, text, [[('用户管理', 'list:users:0'), ('用户搜索', 'search')],
                    [('全部任务', 'list:alltasks:0'), ('全部内容', 'list:allcontents:0')], [('全部目标', 'list:allchats:0')],
                    [('广播', 'list:broadcasts:0'), ('系统配置', 'config')],
                    [('审计日志', 'audit:0'), ('收费管理', 'badmin:home', 'primary')]], query)
        if cmd == 'search':
            return await self.prompt(uid, 'USER_SEARCH', '输入用户ID、@用户名、名称、ACTIVE/BANNED，或 seen:YYYY-MM-DD:YYYY-MM-DD 查询最近活跃日期范围。', {}, query)
        if cmd == 'user':
            return await self.user_detail(uid, int(p[1]), query)
        if cmd == 'ban':
            target = int(p[1])
            row = await self.db.fetchrow('SELECT * FROM users WHERE id=$1', target)
            if not row:
                raise ValueError('用户不存在。')
            if target in self.settings.admin_ids:
                raise ValueError('不能通过UI封禁系统管理员。')
            if row['status'] == 'BANNED':
                return await self.confirm(uid, 'BAN_CONFIRM', {'user_id': target, 'unban': True, 'reason': ''}, f'确认解封用户 {target}？不会自动启动任务。', query)
            return await self.prompt(uid, 'BAN_REASON', '输入封禁原因，随后再次确认。', {'user_id': target, 'unban': False}, query)
        if cmd == 'config':
            rows = [[(key, 'configkey:' + key)] for key in CONFIG]
            return await self.screen(uid, '系统配置（仅允许白名单，不可读取Token/数据库密码）', rows, query, 'admin')
        if cmd == 'configkey':
            key = p[1]
            if key not in CONFIG:
                raise ValueError('不允许修改此配置。')
            return await self.prompt(uid, 'CONFIG_VALUE', f'配置 {key}\n当前：{safe(await self.config(key), 2800)}\n输入新值：', {'key': key}, query)
        if cmd == 'broadcast':
            return await self.broadcast_detail(uid, int(p[1]), query)
        if cmd == 'bact':
            labels = {'pause': '暂停', 'resume': '继续', 'cancel': '取消未发送项',
                      'retry_unknown': '重发UNKNOWN（可能重复）', 'skip_unknown': '跳过UNKNOWN（可能漏发）'}
            if p[1] not in labels:
                raise ValueError('未知广播操作')
            return await self.confirm(uid, 'BROADCAST_ACTION', {'id': int(p[2]), 'action': p[1]},
                                      f'确认{labels[p[1]]} 广播 #{p[2]}？已在途发送可能返回结果。', query)
        if cmd == 'bexport':
            return await self.broadcast_export(uid, int(p[1]))
        if cmd == 'audit':
            page = max(0, int(p[1]))
            records = await self.db.fetch('SELECT id,actor_id,action,resource_type,resource_id,details,created_at FROM audit_logs ORDER BY id DESC LIMIT $1 OFFSET $2', PAGE + 1, page * PAGE)
            nav = []
            if page:
                nav.append(('上一页', f'audit:{page - 1}'))
            if len(records) > PAGE:
                nav.append(('下一页', f'audit:{page + 1}'))
            text = '\n\n'.join(f"#{r['id']} {str(r['created_at'])[:19]}\n{r['actor_id']} {r['action']} {r['resource_type']}:{r['resource_id']}\n{safe(r['details'], 180)}" for r in records[:PAGE])
            return await self.screen(uid, text or '暂无审计记录。', [nav] if nav else [], query, 'admin')

    async def search_users(self, uid, term):
        self.require_admin(uid)
        term = term.strip()
        if term.startswith('seen:'):
            values = term.split(':')
            if len(values) != 3:
                raise ValueError('格式 seen:YYYY-MM-DD:YYYY-MM-DD')
            start, end = [datetime.strptime(v, '%Y-%m-%d').replace(tzinfo=timezone.utc) for v in values[1:]]
            if end < start:
                raise ValueError('结束日期不能早于开始日期。')
            records = await self.db.fetch('SELECT id,username,first_name,status FROM users WHERE last_seen >= $1 AND last_seen < $2 ORDER BY id LIMIT 51', start, end + timedelta(days=1))
        else:
            records = await self.db.fetch("SELECT id,username,first_name,status FROM users WHERE id::text=$1 OR username ILIKE $2 OR first_name ILIKE $2 OR status=$3 ORDER BY id LIMIT 51",
                                          term, '%' + term.lstrip('@')[:100] + '%', term.upper())
        rows = [[(f"{r['id']} @{r['username'] or ''} {safe(r['first_name'], 30)} {r['status']}", f"user:{r['id']}")] for r in records[:50]]
        await audit(self.db, uid, 'user_search', 'users', '', {'query': safe(term, 100)})
        await self.clear(uid)
        await self.screen(uid, '搜索结果' + ('\n暂无匹配用户。' if not records else '') + ('\n仅显示前50条，请缩小条件。' if len(records) > 50 else ''), rows, back='admin')

    async def user_detail(self, uid, target, query=None):
        self.require_admin(uid)
        row = await self.db.fetchrow('SELECT * FROM users WHERE id=$1', target)
        if not row:
            raise ValueError('用户不存在。')
        counts = await self.db.fetchrow('SELECT (SELECT count(*) FROM user_chats WHERE owner_id=$1 AND active) chats,'
                                        '(SELECT count(*) FROM tasks WHERE owner_id=$1 AND NOT deleted) tasks,'
                                        '(SELECT count(*) FROM message_logs WHERE owner_id=$1) logs', target)
        await audit(self.db, uid, 'user_detail', 'users', str(target))
        await self.screen(uid, f"用户 {target} @{row['username'] or ''}\n{row['first_name'] or ''} {row['last_name'] or ''}\n"
                          f"首次：{row['first_seen']}\n最近：{row['last_seen']}\n状态：{row['status']}\n封禁原因：{safe(row['ban_reason'], 500)}\n"
                          f"目标：{counts['chats']} · 任务：{counts['tasks']} · 记录：{counts['logs']}",
                          [[('目标', f'list:chats:0:{target}'), ('来源', f'list:sources:0:{target}')],
                           [('任务', f'list:tasks:0:{target}'), ('内容', f'list:contents:0:{target}')],
                           [('消息日志', f'logs:0:{target}'), ('封禁 / 解封', f'ban:{target}', 'danger')]], query, 'list:users:0')

    async def set_ban(self, uid, data):
        self.require_admin(uid)
        target = data['user_id']
        if target in self.settings.admin_ids:
            raise ValueError('不能封禁系统管理员。')
        before = await self.db.fetchrow('SELECT status FROM users WHERE id=$1', target)
        if not before:
            raise ValueError('用户不存在。')
        unban = data.get('unban', False)
        # Engine pause preserves remaining time and cancels queued instant work.
        await self.db.execute('UPDATE users SET status=$2,ban_reason=$3 WHERE id=$1', target, 'ACTIVE' if unban else 'BANNED', data['reason'])
        if not unban:
            rows = await self.db.fetch("SELECT id FROM tasks WHERE owner_id=$1 AND status='RUNNING' AND NOT deleted", target)
            for row in rows:
                await self.engine.pause_task(row['id'], target)
            await self.db.execute('DELETE FROM sessions WHERE owner_id=$1', target)
        await audit(self.db, uid, 'unban' if unban else 'ban', 'users', str(target), {'before': before['status'], 'after': 'ACTIVE' if unban else 'BANNED', 'reason': data['reason']})

    async def broadcast_detail(self, uid, job_id, query=None):
        self.require_admin(uid)
        job = await self.db.fetchrow('SELECT * FROM broadcast_jobs WHERE id=$1', job_id)
        if not job:
            raise ValueError('广播不存在。')
        stat = await self.engine.broadcast_stats(job_id)
        total = stat['total']
        done = stat['success'] + stat['failed'] + stat['skipped']
        ratio = done / total if total else 1
        bar = '█' * int(ratio * 20) + '░' * (20 - int(ratio * 20))
        failures = await self.db.fetch("SELECT user_id,status,error FROM broadcast_recipients WHERE job_id=$1 AND status IN ('FAILED','UNKNOWN') ORDER BY id LIMIT 5", job_id)
        text = (f"广播 #{job_id} · {job['status']}\n{bar} {ratio:.0%}\n总目标：{total} · 完成：{done}\n"
                f"成功：{stat['success']} · 失败：{stat['failed']} · 跳过：{stat['skipped']}\n"
                f"待处理：{stat['pending']} · UNKNOWN：{stat['unknown']}")
        text += '\n' + '\n'.join(f"{r['user_id']} {r['status']} {safe(r['error'], 120)}" for r in failures)
        rows = [[('刷新', f'broadcast:{job_id}'), ('暂停', f'bact:pause:{job_id}')],
                [('继续', f'bact:resume:{job_id}'), ('取消', f'bact:cancel:{job_id}', 'danger')], [('失败/未知导出', f'bexport:{job_id}')]]
        if stat['unknown']:
            rows.append([('UNKNOWN重发', f'bact:retry_unknown:{job_id}', 'danger'), ('UNKNOWN跳过', f'bact:skip_unknown:{job_id}')])
        message = await self.screen(uid, text, rows, query, 'list:broadcasts:0')
        await self.db.execute('UPDATE broadcast_jobs SET progress_chat_id=$2,progress_message_id=$3,last_progress_at=now() WHERE id=$1', job_id, uid, message.message_id)
        await audit(self.db, uid, 'broadcast_detail', 'broadcast_jobs', str(job_id))

    async def broadcast_export(self, uid, job_id):
        self.require_admin(uid)
        cursor, part = 0, 1
        while True:
            rows = await self.db.fetch("SELECT id,user_id,status,attempts,error,finished_at FROM broadcast_recipients WHERE job_id=$1 AND id>$2 AND status IN ('FAILED','UNKNOWN') ORDER BY id LIMIT 10000", job_id, cursor)
            if not rows:
                if part == 1:
                    await self.send_csv(uid, [], f'broadcast-{job_id}-failures.csv')
                break
            await self.send_csv(uid, rows, f'broadcast-{job_id}-failures-{part}.csv')
            cursor, part = rows[-1]['id'], part + 1
            if len(rows) < 10000:
                break
        await audit(self.db, uid, 'broadcast_export', 'broadcast_jobs', str(job_id))
