"""Offline UI tests: never use a token, a network or real recipients."""
import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest, RetryAfter

from adbot.ui import BotUI, csv_cell


class FakeDB:
    def __init__(self):
        self.sessions = {}
        self.users = {10: {'status': 'ACTIVE', 'ban_reason': '', 'timezone': 'Asia/Shanghai'}}
        self.resources = {}
        self.calls = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        if sql.startswith('INSERT INTO sessions'):
            self.sessions[args[0]] = dict(state=args[1], data=dict(args[2]), expires_at=args[3])
        elif sql.startswith('DELETE FROM sessions'):
            self.sessions.pop(args[0], None)
        return 'UPDATE 1'

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if sql.startswith('DELETE FROM sessions'):
            row = self.sessions.get(args[0])
            if row and row['state'] == args[1] and row['data']['nonce'] == args[2] and row['expires_at'] > datetime.now(timezone.utc):
                self.sessions.pop(args[0])
                return {'owner_id': args[0]}
            return None
        if 'FROM sessions' in sql:
            return self.sessions.get(args[0])
        if 'FROM users' in sql:
            return self.users.get(args[0])
        if sql.startswith('SELECT * FROM'):
            return self.resources.get((sql.split()[3], args[0]))
        return None

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        if 'app_settings' in sql:
            return None
        if 'SELECT id FROM contents' in sql:
            return args[1] if args[0] == 10 and args[1] in {1, 2} else None
        return 0

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if 'FROM contents' in sql:
            return [{'id': 1, 'title': '一'}, {'id': 2, 'title': '二'}]
        return []


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edit_message_text = AsyncMock(side_effect=self.send_message)

    async def send_message(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return NS(message_id=len(self.sent))

    async def get_me(self):
        return NS(id=999)

    async def get_chat(self, ref):
        return NS(id=-1001, type='channel', title='频道', username='test')

    async def get_chat_member(self, chat, user):
        return NS(status='administrator', can_post_messages=True, to_dict=lambda: {})


def update(uid=10, callback=None, text=None, private=True):
    user = NS(id=uid, username='tester', first_name='Test', last_name=None, language_code='zh')
    chat = NS(id=uid if private else -5, type='private' if private else 'group')
    query = NS(data=callback, answer=AsyncMock(), message=NS(message_id=1)) if callback is not None else None
    message = NS(text=text, caption=None) if text is not None else None
    return NS(my_chat_member=None, effective_user=user, effective_chat=chat, callback_query=query, message=message)


class UITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db, self.bot = FakeDB(), FakeBot()
        self.ui = BotUI(self.db, self.bot, NS(admin_ids=(100000001, 100000002), workers=1))
        self.ui.entitlements = NS(require=AsyncMock())
        self.ui.engine = NS(throttle=AsyncMock(), cooldown=AsyncMock(), create_broadcast=AsyncMock(return_value=8), create_task=AsyncMock(return_value=9),
                            resolve_unknown=AsyncMock(), start_task=AsyncMock(), pause_task=AsyncMock(),
                            schedule_test_delete=AsyncMock())

    async def test_foreign_callback_rejected(self):
        up = update(callback='u:11:admin')
        with patch.object(self.ui, 'callback', new_callable=AsyncMock) as call:
            await self.ui.handle_update(up)
            call.assert_not_awaited()
        self.assertTrue(up.callback_query.answer.call_args.kwargs['show_alert'])

    async def test_group_update_cannot_mutate_users(self):
        await self.ui.handle_update(update(text='/start', private=False))
        self.assertFalse(self.db.calls)

    async def test_banned_user_is_silent_and_audited(self):
        self.db.users[10]['status'] = 'BANNED'
        with patch('adbot.ui.audit', new_callable=AsyncMock) as audit:
            await self.ui.handle_update(update(text='/start'))
            audit.assert_awaited_once()
        self.assertFalse(self.bot.sent)

    async def test_owner_isolation(self):
        self.db.resources[('tasks', 1)] = {'id': 1, 'owner_id': 11, 'deleted': False}
        with self.assertRaises(PermissionError):
            await self.ui.resource('tasks', 1, 10)
        with patch('adbot.ui.audit', new_callable=AsyncMock) as audit:
            row = await self.ui.resource('tasks', 1, 100000001)
            self.assertEqual(row['owner_id'], 11)
            audit.assert_awaited_once()

    async def test_sessions_persist_and_expire(self):
        data = await self.ui.set_state(10, 'CONTENT_BODY', {'title': '标题'})
        another = BotUI(self.db, self.bot, NS(admin_ids=(), workers=1))
        self.assertEqual((await another.state(10))['data']['nonce'], data['nonce'])
        self.db.sessions[10]['expires_at'] = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertEqual((await another.state(10))['state'], 'EXPIRED')
        self.assertNotIn(10, self.db.sessions)

    async def test_stale_confirmation_and_cancel(self):
        data = await self.ui.set_state(10, 'CONTENT_CONFIRM', {'previewed': True})
        await self.ui.home(10)
        with self.assertRaises(ValueError):
            await self.ui.callback(10, f"w:{data['nonce']}:confirm", None)
        self.ui.engine.create_broadcast.assert_not_awaited()

    async def test_binding_checks_both_admins(self):
        original = self.bot.get_chat_member
        async def member(chat, user):
            if user == 10:
                return NS(status='member')
            return await original(chat, user)
        self.bot.get_chat_member = member
        with self.assertRaises(ValueError):
            await self.ui.verify_chat(10, '@test')
        self.assertFalse(any(sql.startswith('INSERT INTO chats') for sql, _ in self.db.calls))

    async def test_channel_post_permission_required(self):
        async def member(chat, user):
            return NS(status='administrator', can_post_messages=False)
        self.bot.get_chat_member = member
        with self.assertRaises(ValueError):
            await self.ui.verify_bot(-1001)

    async def test_private_source_requires_owner_join(self):
        with self.assertRaises(PermissionError):
            await self.ui.import_index(10, 77)
        self.assertTrue(any('s.owner_id=$2' in sql for sql, _ in self.db.calls))

    async def test_preview_required_and_real_broadcast_single_confirm(self):
        admin = 100000001
        data = await self.ui.set_state(admin, 'CONTENT_OPTIONS', {'broadcast': True, 'title': '广播', 'payload': {'type': 'text', 'text': '公告'}})
        with self.assertRaises(ValueError):
            await self.ui.callback(admin, f"w:{data['nonce']}:confirm", None)
        self.ui.engine.create_broadcast.assert_not_awaited()
        with patch('adbot.ui.deliver', new_callable=AsyncMock, return_value={'message_id': 1, 'degraded': False}):
            await self.ui.content_preview(admin, data)
        with patch.object(self.ui, 'broadcast_detail', new_callable=AsyncMock), patch('adbot.ui.audit', new_callable=AsyncMock):
            await self.ui.callback(admin, f"w:{data['nonce']}:confirm", None)
            with self.assertRaises(ValueError):
                await self.ui.callback(admin, f"w:{data['nonce']}:confirm", None)
        self.ui.engine.create_broadcast.assert_awaited_once()

    async def test_concurrent_broadcast_confirm_is_consumed_once(self):
        uid = 100000001
        data = await self.ui.set_state(uid, 'CONTENT_CONFIRM', {'broadcast': True, 'previewed': True, 'payload': {'type': 'text', 'text': '公告'}})
        real_state = self.ui.state
        barrier = asyncio.Event()
        count = 0
        async def raced_state(owner):
            nonlocal count
            session = await real_state(owner)
            count += 1
            if count == 2:
                barrier.set()
            await barrier.wait()
            return session
        with patch.object(self.ui, 'state', side_effect=raced_state), patch.object(self.ui, 'broadcast_detail', new_callable=AsyncMock), patch('adbot.ui.audit', new_callable=AsyncMock):
            results = await asyncio.gather(*(self.ui.callback(uid, f"w:{data['nonce']}:confirm", None) for _ in range(2)), return_exceptions=True)
        self.ui.engine.create_broadcast.assert_awaited_once()
        self.assertEqual(sum(isinstance(r, ValueError) for r in results), 1)

    async def test_new_body_discards_old_received_entities(self):
        await self.ui.set_state(10, 'CONTENT_BODY', {'title': '新版', 'received_entities': [{'type': 'bold', 'offset': 0, 'length': 99}]})
        with patch('adbot.ui.capture_message', return_value={'type': 'text', 'text': '新', 'entities': []}):
            await self.ui.message(10, NS(text='新', caption=None))
        session = await self.ui.state(10)
        self.assertNotIn('received_entities', session['data'])

    async def test_menu_custom_emoji_qualification_fallback_preserves_style(self):
        self.bot.send_message = AsyncMock(side_effect=[BadRequest('CUSTOM_EMOJI_USAGE_IS_NOT_ALLOWED'), NS(message_id=2)])
        with patch('adbot.ui.audit', new_callable=AsyncMock) as log:
            await self.ui.screen(10, '首页', [[('任务', 'list:tasks:0', 'primary', '5368324170671202286')]])
        markup = self.bot.send_message.call_args.kwargs['reply_markup'].to_dict()
        self.assertNotIn('icon_custom_emoji_id', markup['inline_keyboard'][0][0])
        self.assertEqual(markup['inline_keyboard'][0][0]['style'], 'primary')
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertEqual(self.ui.engine.throttle.await_count, 2)
        log.assert_awaited_once()

    async def test_menu_network_or_invalid_icon_not_retried(self):
        self.bot.send_message = AsyncMock(side_effect=BadRequest('CUSTOM_EMOJI_ID_INVALID'))
        with self.assertRaises(BadRequest):
            await self.ui.screen(10, '首页', [[('任务', 'list:tasks:0', 'primary', '5368324170671202286')]])
        self.bot.send_message.assert_awaited_once()

    async def test_private_chat_selector_and_return_are_verified(self):
        await self.ui.begin_binding(10, 'sources')
        session = await self.ui.state(10)
        keyboard = self.bot.sent[-1][1]['reply_markup'].to_dict()
        req = keyboard['keyboard'][0][0]['request_chat']
        self.assertTrue(req['bot_administrator_rights']['can_post_messages'])
        self.assertTrue(req['user_administrator_rights']['can_post_messages'])
        self.assertTrue(req['bot_is_member'])
        shared = NS(request_id=session['data']['request_id'] + 1, chat_id=-1001)
        with patch.object(self.ui, 'bind', new_callable=AsyncMock) as bind:
            await self.ui.message(10, NS(text=None, caption=None, chat_shared=shared))
            bind.assert_awaited_once_with(10, '-1001', 'sources')
        shared.request_id += 100
        with self.assertRaises(ValueError):
            await self.ui.message(10, NS(text=None, caption=None, chat_shared=shared))

    async def test_private_activity_unblocks_broadcast_recipient(self):
        await self.ui.handle_update(update(text='/start'))
        self.assertTrue(any('blocked=false' in sql for sql, _ in self.db.calls))

    async def test_screen_retry_after_sets_shared_cooldown_without_retry(self):
        self.bot.send_message = AsyncMock(side_effect=RetryAfter(7))
        with patch('adbot.ui.audit', new_callable=AsyncMock):
            with self.assertRaisesRegex(ValueError, '稍后重试'):
                await self.ui.screen(10, '首页')
        self.ui.engine.cooldown.assert_awaited_once_with(7.0)
        self.bot.send_message.assert_awaited_once()

    async def test_other_ui_retry_after_sets_shared_cooldown_and_reports(self):
        with patch.object(self.ui, 'message', new_callable=AsyncMock, side_effect=RetryAfter(9)), patch('adbot.ui.audit', new_callable=AsyncMock):
            await self.ui.handle_update(update(text='hello'))
        self.ui.engine.cooldown.assert_awaited_once_with(9.0)
        self.assertIn('稍后重试', self.bot.sent[-1][1]['text'])

    async def test_completed_task_is_editable(self):
        self.db.resources[('tasks', 3)] = dict(id=3, owner_id=10, status='COMPLETED', deleted=False, name='完成任务',
            interval_seconds=60, delete_previous=False, instant_source_id=None, source_mode='copy', max_runs=1,
            skip_failed=False, delete_failure_pause=False)
        with patch.object(self.ui, 'task_selection', new_callable=AsyncMock) as select:
            await self.ui.task_edit(10, 3)
        self.assertEqual(select.call_args.args[1]['edit_id'], 3)

    async def test_task_selection_order_and_owner_validation(self):
        data = dict(content_ids=[], chat_ids=[], name='轮播')
        await self.ui.task_selection(10, data, 'contents', 0)
        for rid in (2, 1):
            session = await self.ui.state(10)
            await self.ui.task_session_action(10, session['state'], session['data'], ['toggle', str(rid)], None)
        session = await self.ui.state(10)
        self.assertEqual(session['data']['content_ids'], [2, 1])
        with self.assertRaises(PermissionError):
            await self.ui.task_session_action(10, session['state'], session['data'], ['toggle', '3'], None)

    async def test_task_unknown_confirmation_delegates_owner(self):
        self.db.resources[('tasks', 3)] = {'id': 3, 'owner_id': 10, 'deleted': False, 'status': 'ERROR'}
        data = await self.ui.set_state(10, 'TASK_ACTION', {'id': 3, 'action': 'retry'})
        self.ui.engine.resolve_unknown.assert_not_awaited()
        with patch.object(self.ui, 'task_detail', new_callable=AsyncMock), patch('adbot.ui.audit', new_callable=AsyncMock):
            await self.ui.callback(10, f"w:{data['nonce']}:confirm", None)
        self.ui.engine.resolve_unknown.assert_awaited_once_with(3, 10, 'retry')

    async def test_admin_callback_authorization(self):
        with self.assertRaises(PermissionError):
            await self.ui.callback(10, 'config', None)

    async def test_unknown_format_cannot_be_injected(self):
        data = await self.ui.set_state(10, 'CONTENT_OPTIONS', {'payload': {'type': 'text', 'text': 'abc'}, 'title': 'x'})
        with self.assertRaises(ValueError):
            await self.ui.session_action(10, {'state': 'CONTENT_OPTIONS', 'data': data}, ['format', 'unsafe'], None)

    def test_callback_limit_and_formula_csv(self):
        with self.assertRaises(ValueError):
            self.ui.button(10, 'x', 'a' * 80)
        self.assertEqual(csv_cell('=SUM(A1:A2)'), "'=SUM(A1:A2)")
        self.assertEqual(csv_cell('普通文字'), '普通文字')


if __name__ == '__main__':
    unittest.main()
