"""Billing UI tests: offline Bot, no real credentials or production SQL.

Run as adbot; the final tests reuse random schemas in auto_ad_test only.
"""
import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import pytest

from adbot.billing_ui import SECRET_REDACTED, TERMS, _pay_url
from adbot.ui import BotUI
from test_ui import FakeBot, FakeDB, update
from test_ui_sql import (ADMIN as SQL_ADMIN, OWNER as SQL_OWNER, CHAT_A, CHAT_B,
                         bind_targets, click, create_content, create_task, register, rig,
                         send, step)

ADMIN = 100000001
PROVIDER_ORDER_ID = '0123456789abcdef0123456789abcdef'
MERCHANT_ORDER_ID = 'ui-fixture-merchant-4'
CANONICAL_PAY_URL = f'https://t.me/OkayPayBot?start=shop_deposit--{PROVIDER_ORDER_ID}'


def snapshot(**changes):
    value = dict(enabled=True, exempt=False, active=True, plan_code='week',
                 starts_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                 expires_at=datetime(2026, 1, 8, tzinfo=timezone.utc),
                 queued_until=datetime(2026, 2, 7, tzinfo=timezone.utc),
                 extra_channels=10, channel_limit=30, used_channels=4)
    value.update(changes)
    return value


def order(**changes):
    value = dict(id=4, owner_id=10, status='PENDING', product_code='week',
                 amount='10', coin='USDT', pay_url=CANONICAL_PAY_URL,
                 unique_id=MERCHANT_ORDER_ID, provider_order_id=PROVIDER_ORDER_ID,
                 created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), paid_at=None)
    value.update(changes)
    return value


def message(text=None, caption=None):
    return NS(text=text, caption=caption, message_id=23)


class Context:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *args):
        return False


class BillingDB(FakeDB):
    def __init__(self):
        super().__init__()
        self.users[ADMIN] = {'status': 'ACTIVE', 'ban_reason': ''}
        self.orders = {4: order(), 5: order(id=5, owner_id=11)}
        self.pool = NS(acquire=lambda: Context(self))
        self.targets = []

    def transaction(self):
        return Context(self)

    async def fetchrow(self, sql, *args):
        if 'FROM billing_orders' in sql:
            self.calls.append((sql, args))
            row = self.orders.get(args[0])
            return row if row and (len(args) == 1 or row['owner_id'] == args[1]) else None
        return await super().fetchrow(sql, *args)

    async def fetch(self, sql, *args):
        if 'FROM task_targets' in sql:
            return [{'chat_id': value} for value in self.targets]
        return await super().fetch(sql, *args)


class BillingUITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db, self.bot = BillingDB(), FakeBot()
        self.bot.delete_message = AsyncMock()
        self.ui = BotUI(self.db, self.bot, NS(admin_ids=(ADMIN,), workers=1))
        self.ui.engine = NS(throttle=AsyncMock(), cooldown=AsyncMock(), edit_task=AsyncMock(),
                            create_task=AsyncMock(return_value=7), start_task=AsyncMock(),
                            stop_task=AsyncMock(), resolve_unknown=AsyncMock())
        self.ui.entitlements = NS(snapshot=AsyncMock(return_value=snapshot()), require=AsyncMock(),
                                  set_enabled=AsyncMock(), adjust=AsyncMock())
        self.ui.payments = NS(create_order=AsyncMock(return_value=order()),
                              check_order=AsyncMock(return_value=order()),
                              list_orders=AsyncMock(return_value=[]), configure=AsyncMock(),
                              config_summary=AsyncMock(return_value={'configured': True,
                                  'merchant_id': '123', 'masked_token': '********'}))

    def sent_text(self):
        return '\n'.join(kwargs.get('text', '') for _, kwargs in self.bot.sent)

    async def test_home_is_two_columns_with_billing_entries(self):
        await self.ui.home(10)
        keyboard = self.bot.sent[-1][1]['reply_markup'].inline_keyboard
        self.assertTrue(all(len(row) == 2 for row in keyboard))
        actions = {button.callback_data for row in keyboard for button in row}
        self.assertTrue({'u:10:bpay:member', 'u:10:bpay:shop', 'u:10:bpay:orders:0'} <= actions)

    async def test_membership_shows_usage_queue_and_no_auto_resume(self):
        await self.ui.billing_membership(10)
        text = self.sent_text()
        for expected in ('4 / 30', '永久附加目标：10', '2026-02-07', '手动启动', '来源不计算'):
            self.assertIn(expected, text)

    async def test_extra_packages_only_for_active_non_admin_week(self):
        for values, expected in (({}, True), ({'active': False}, False),
                                 ({'plan_code': 'month'}, False), ({'exempt': True}, False)):
            self.ui.entitlements.snapshot.return_value = snapshot(**values)
            await self.ui.billing_shop(10)
            keyboard = self.bot.sent[-1][1]['reply_markup'].inline_keyboard
            actions = {button.callback_data for row in keyboard for button in row}
            self.assertEqual('u:10:bpay:buy:extra10' in actions, expected)

    async def test_invalid_or_ineligible_purchase_rejected(self):
        with self.assertRaises(ValueError):
            await self.ui.billing_purchase(10, 'forged')
        self.ui.entitlements.snapshot.return_value = snapshot(active=False)
        with self.assertRaises(ValueError):
            await self.ui.billing_purchase(10, 'extra10')
        self.ui.entitlements.snapshot.return_value = snapshot(exempt=True)
        with self.assertRaises(ValueError):
            await self.ui.billing_purchase(ADMIN, 'week')
        self.ui.payments.create_order.assert_not_awaited()

    async def test_purchase_confirmation_contains_terms_and_fixed_key(self):
        await self.ui.billing_purchase(10, 'month')
        data = self.db.sessions[10]['data']
        self.assertEqual(data['product_code'], 'month')
        self.assertEqual(len(data['request_key']), 32)
        self.assertIn(TERMS, self.sent_text())
        self.assertIn('2026-02-07', self.sent_text())
        await asyncio.gather(*(self.ui.callback(10, f"bpay:confirm:{data['nonce']}", None) for _ in range(2)))
        self.assertEqual(self.ui.payments.create_order.await_count, 2)
        for call in self.ui.payments.create_order.await_args_list:
            self.assertEqual(call.args, (10, 'month', data['request_key']))

    async def test_foreign_and_stale_purchase_confirmation_rejected(self):
        await self.ui.billing_purchase(10, 'week')
        nonce = self.db.sessions[10]['data']['nonce']
        with self.assertRaises(ValueError):
            await self.ui.callback(11, f'bpay:confirm:{nonce}', None)
        with self.assertRaises(ValueError):
            await self.ui.callback(10, 'bpay:confirm:old', None)
        self.db.sessions[10]['expires_at'] = datetime.now(timezone.utc) - timedelta(seconds=1)
        with self.assertRaises(ValueError):
            await self.ui.callback(10, f'bpay:confirm:{nonce}', None)
        self.ui.payments.create_order.assert_not_awaited()

    async def test_foreign_callback_prefix_rejected_before_payments(self):
        up = update(callback='u:11:bpay:check:4')
        await self.ui.handle_update(up)
        self.ui.payments.check_order.assert_not_awaited()
        self.assertTrue(up.callback_query.answer.call_args.kwargs['show_alert'])

    async def test_order_queries_are_owner_scoped(self):
        with self.assertRaises(PermissionError):
            await self.ui.callback(10, 'bpay:detail:5', None)
        await self.ui.callback(10, 'bpay:check:4', None)
        self.ui.payments.check_order.assert_awaited_once_with(4, owner_id=10)
        self.ui.payments.create_order.assert_not_awaited()

    async def test_even_returned_foreign_order_is_not_displayed(self):
        self.ui.payments.check_order.return_value = order(owner_id=11)
        with self.assertRaises(PermissionError):
            await self.ui.callback(10, 'bpay:check:4', None)
        self.assertNotIn('所属用户：11', self.sent_text())

    async def test_admin_order_access_rechecks_dynamic_permission(self):
        await self.ui.callback(ADMIN, 'badmin:check:5', None)
        self.ui.payments.check_order.assert_awaited_once_with(5, admin_id=ADMIN)
        self.ui.settings.admin_ids = ()
        with self.assertRaises(PermissionError):
            await self.ui.callback(ADMIN, 'badmin:check:5', None)
        self.assertFalse(self.ui.admin(ADMIN))

    async def test_untrusted_url_or_evidence_not_rendered(self):
        await self.ui.billing_order(10, order(pay_url='https://evil.example/pay',
                                            error='PRIVATE_EVIDENCE', evidence='PRIVATE_EVIDENCE'))
        keyboard = self.bot.sent[-1][1]['reply_markup'].inline_keyboard
        self.assertFalse(any(button.url for row in keyboard for button in row))
        self.assertNotIn('PRIVATE_EVIDENCE', self.sent_text())

    async def test_allowed_payment_url_rendered_only_when_pending(self):
        await self.ui.billing_order(10, order())
        keyboard = self.bot.sent[-1][1]['reply_markup'].inline_keyboard
        self.assertEqual(keyboard[0][0].url, CANONICAL_PAY_URL)
        await self.ui.billing_order(10, order(status='PAID'))
        keyboard = self.bot.sent[-1][1]['reply_markup'].inline_keyboard
        self.assertFalse(any(button.url for row in keyboard for button in row))

    async def test_order_displays_distinct_local_merchant_and_platform_numbers(self):
        await self.ui.billing_order(10, order())
        text = self.sent_text()
        self.assertIn('订单 #4', text)
        self.assertIn(f'商户订单号：{MERCHANT_ORDER_ID}', text)
        self.assertIn(f'OKPay 平台订单号：{PROVIDER_ORDER_ID}', text)
        self.assertIn('付款后系统自动检测并开通', text)
        self.assertIn('不需管理员手动确认', text)

    async def test_order_pending_number_placeholders_are_explicit(self):
        await self.ui.billing_order(10, order(unique_id=None, provider_order_id=None))
        text = self.sent_text()
        self.assertIn('商户订单号：生成中', text)
        self.assertIn('OKPay 平台订单号：自动获取中', text)
        self.assertNotIn('None', text)

    async def test_link_review_recovery_keeps_original_order_automatically(self):
        await self.ui.billing_order(10, order(status='REVIEW', pay_url=None,
                                             error='支付链接异常，请联系管理员'))
        text = self.sent_text()
        self.assertIn('支付入口自动恢复中', text)
        self.assertIn('系统正在自动查单恢复原付款入口', text)
        self.assertIn('无需人工确认到账', text)
        self.assertIn('勿重复下单', text)
        self.assertNotIn('待人工核对', text)
        self.assertNotIn('支付链接异常，请联系管理员', text)
        buttons = [button for row in self.bot.sent[-1][1]['reply_markup'].inline_keyboard for button in row]
        self.assertFalse(any(button.url for button in buttons))
        self.assertIn('u:10:bpay:check:4', {button.callback_data for button in buttons})
        self.ui.payments.create_order.assert_not_awaited()

    async def test_non_link_review_remains_validation_exception_not_auto_recovery(self):
        await self.ui.billing_order(10, order(status='REVIEW', pay_url=None,
                                             error='PRIVATE_EVIDENCE', evidence={'token': 'PRIVATE_EVIDENCE'}))
        text = self.sent_text()
        self.assertIn('支付数据校验异常', text)
        self.assertNotIn('自动恢复中', text)
        self.assertNotIn('无需人工确认到账', text)
        self.assertNotIn('PRIVATE_EVIDENCE', text)
        buttons = [button for row in self.bot.sent[-1][1]['reply_markup'].inline_keyboard for button in row]
        self.assertIn('u:10:contact', {button.callback_data for button in buttons})
        self.assertFalse(any(button.url for button in buttons))

    async def test_order_pagination_scopes_owner(self):
        self.ui.payments.list_orders.return_value = [order(id=i) for i in range(1, 10)]
        await self.ui.billing_orders(10, 2)
        self.ui.payments.list_orders.assert_awaited_once_with(10, limit=9, offset=16)
        keyboard = self.bot.sent[-1][1]['reply_markup'].inline_keyboard
        actions = {button.callback_data for row in keyboard for button in row}
        self.assertIn('u:10:bpay:orders:3', actions)
        self.assertNotIn('u:10:bpay:detail:9', actions)

    async def test_billing_switch_confirmation_only_once(self):
        await self.ui.callback(ADMIN, 'badmin:switch:1', None)
        self.ui.entitlements.set_enabled.assert_not_awaited()
        nonce = self.db.sessions[ADMIN]['data']['nonce']
        results = await asyncio.gather(*(self.ui.callback(ADMIN, f'badmin:confirm:{nonce}', None)
                                         for _ in range(2)), return_exceptions=True)
        self.ui.entitlements.set_enabled.assert_awaited_once_with(ADMIN, True)
        self.assertEqual(sum(isinstance(item, ValueError) for item in results), 1)

    async def test_config_requires_merchant_and_warning_first(self):
        await self.ui.callback(ADMIN, 'badmin:config', None)
        await self.ui.message(ADMIN, message('123'))
        self.assertEqual(self.db.sessions[ADMIN]['state'], 'BILLING_CONFIG_WARNING')
        self.ui.payments.configure.assert_not_awaited()
        nonce = self.db.sessions[ADMIN]['data']['nonce']
        await self.ui.callback(ADMIN, f'badmin:confirm:{nonce}', None)
        self.assertEqual(self.db.sessions[ADMIN]['state'], 'BILLING_CONFIG_SECRET')
        self.assertEqual(set(self.db.sessions[ADMIN]['data']), {'merchant_id', 'nonce'})

    async def test_secret_session_drops_non_whitelisted_data(self):
        await self.ui.set_state(ADMIN, 'BILLING_CONFIG_SECRET',
                                {'merchant_id': '123', 'token': 'FIXTURE_SECRET', 'payload': {'text': 'FIXTURE_SECRET'}})
        self.assertEqual(set(self.db.sessions[ADMIN]['data']), {'merchant_id', 'nonce'})
        self.assertNotIn('FIXTURE_SECRET', repr(self.db.calls))

    async def test_secret_failure_is_deleted_not_echoed_or_persisted(self):
        await self.ui.set_state(ADMIN, 'BILLING_CONFIG_SECRET', {'merchant_id': '123'})
        self.ui.payments.configure.side_effect = RuntimeError('FIXTURE_SECRET_TOKEN')
        self.bot.delete_message.side_effect = RuntimeError('FIXTURE_SECRET_TOKEN')
        with patch('adbot.ui.audit', new_callable=AsyncMock) as audit:
            await self.ui.message(ADMIN, message('/paytoken FIXTURE_SECRET_TOKEN'))
            audit.assert_not_awaited()
        self.bot.delete_message.assert_awaited_once_with(chat_id=ADMIN, message_id=23)
        self.assertNotIn(ADMIN, self.db.sessions)
        self.assertNotIn('FIXTURE_SECRET_TOKEN', self.sent_text())
        self.assertNotIn('FIXTURE_SECRET_TOKEN', repr(self.db.calls))
        self.assertIn('旧配置未变', self.sent_text())

    async def test_secret_success_is_consumed_and_not_in_sql(self):
        await self.ui.set_state(ADMIN, 'BILLING_CONFIG_SECRET', {'merchant_id': '123'})
        await self.ui.message(ADMIN, message(caption='/paytoken FIXTURE_SECRET_TOKEN'))
        self.ui.payments.configure.assert_awaited_once_with(ADMIN, '123', 'FIXTURE_SECRET_TOKEN')
        self.assertNotIn(ADMIN, self.db.sessions)
        self.assertNotIn('FIXTURE_SECRET_TOKEN', self.sent_text() + repr(self.db.calls))

    async def test_duplicate_secret_cannot_reconfigure(self):
        await self.ui.set_state(ADMIN, 'BILLING_CONFIG_SECRET', {'merchant_id': '123'})
        session = await self.ui.state(ADMIN)
        results = await asyncio.gather(*(self.ui.billing_message(ADMIN, message('FIXTURE_SECRET_TOKEN'),
                                         session, force_secret=True) for _ in range(2)), return_exceptions=True)
        self.assertEqual(sum(isinstance(item, ValueError) for item in results), 1)
        self.ui.payments.configure.assert_awaited_once()
        self.assertNotIn('FIXTURE_SECRET_TOKEN', self.sent_text() + repr(self.db.calls))

    async def test_paytoken_cannot_enter_content_session(self):
        await self.ui.set_state(ADMIN, 'CONTENT_BODY', {'title': 'fixture'})
        with patch('adbot.ui.capture_message') as capture:
            await self.ui.message(ADMIN, message('/paytoken FIXTURE_SECRET_TOKEN'))
            capture.assert_not_called()
        self.ui.payments.configure.assert_not_awaited()
        self.assertEqual(self.db.sessions[ADMIN]['state'], 'CONTENT_BODY')
        self.assertNotIn('FIXTURE_SECRET_TOKEN', self.sent_text() + repr(self.db.calls))

    async def test_forced_secret_cannot_fall_through_after_cancel_race(self):
        await self.ui.set_state(ADMIN, 'CONTENT_BODY', {'title': 'fixture'})
        up = update(uid=ADMIN, text='FIXTURE_SECRET_TOKEN')
        up.message.message_id = 23
        with patch('adbot.ui.capture_message') as capture:
            await self.ui.handle_update(up, billing_secret=True)
            capture.assert_not_called()
        self.ui.payments.configure.assert_not_awaited()
        self.assertNotIn('FIXTURE_SECRET_TOKEN', self.sent_text() + repr(self.db.calls))

    async def test_redacted_replay_never_becomes_content(self):
        await self.ui.set_state(10, 'CONTENT_BODY', {'title': 'fixture'})
        with patch('adbot.ui.capture_message') as capture:
            await self.ui.message(10, message(SECRET_REDACTED))
            capture.assert_not_called()
        self.assertNotIn('payload', self.db.sessions[10]['data'])

    async def test_forced_secret_cancel_returns_home_without_configuring(self):
        await self.ui.set_state(ADMIN, 'BILLING_CONFIG_SECRET', {'merchant_id': '123'})
        up = update(uid=ADMIN, text='/cancel')
        up.message.message_id = 23
        await self.ui.handle_update(up, billing_secret=True)
        self.assertNotIn(ADMIN, self.db.sessions)
        self.ui.payments.configure.assert_not_awaited()
        self.assertIn('请选择操作', self.sent_text())

    async def test_banned_secret_sender_is_deleted_and_stays_silent(self):
        self.db.users[ADMIN]['status'] = 'BANNED'
        up = update(uid=ADMIN, text='/paytoken FIXTURE_SECRET_TOKEN')
        up.message.message_id = 23
        with patch('adbot.ui.audit', new_callable=AsyncMock):
            await self.ui.handle_update(up, billing_secret=True)
        self.bot.delete_message.assert_awaited_once()
        self.ui.payments.configure.assert_not_awaited()
        self.assertFalse(self.bot.sent)
        self.assertNotIn('FIXTURE_SECRET_TOKEN', repr(self.db.calls))

    async def test_admin_adjustment_requires_reason_and_second_confirmation(self):
        await self.ui.callback(ADMIN, 'badmin:adjust:10', None)
        with self.assertRaises(ValueError):
            await self.ui.message(ADMIN, message('7 10'))
        await self.ui.message(ADMIN, message('7 10 fixture compensation'))
        self.ui.entitlements.adjust.assert_not_awaited()
        nonce = self.db.sessions[ADMIN]['data']['nonce']
        await self.ui.callback(ADMIN, f'badmin:confirm:{nonce}', None)
        self.ui.entitlements.adjust.assert_awaited_once_with(
            ADMIN, 10, days=7, extra_channels=10, reason='fixture compensation')
        with self.assertRaises(ValueError):
            await self.ui.callback(ADMIN, f'badmin:confirm:{nonce}', None)

    async def test_admin_test_send_checks_target_owner_not_admin(self):
        self.db.resources[('user_chats', 3)] = {'id': 3, 'owner_id': 10, 'chat_id': -1001}
        self.ui.entitlements.require.side_effect = PermissionError('expired fixture')
        with patch('adbot.ui.audit', new_callable=AsyncMock), patch('adbot.ui.deliver', new_callable=AsyncMock) as deliver:
            with self.assertRaises(PermissionError):
                await self.ui.callback(ADMIN, 'test:3', None)
            deliver.assert_not_awaited()
        self.ui.entitlements.require.assert_awaited_once_with(10)

    async def test_expiry_during_throttle_blocks_test_send(self):
        self.db.resources[('user_chats', 3)] = {'id': 3, 'owner_id': 10, 'chat_id': -1001}
        self.ui.entitlements.require.side_effect = [snapshot(), PermissionError('expired during throttle')]
        with patch('adbot.ui.deliver', new_callable=AsyncMock) as deliver:
            with self.assertRaises(PermissionError):
                await self.ui.callback(10, 'test:3', None)
            deliver.assert_not_awaited()
        self.assertEqual(self.ui.entitlements.require.await_count, 2)

    async def test_admin_task_start_checks_task_owner(self):
        self.db.resources[('tasks', 3)] = {'id': 3, 'owner_id': 10, 'name': 'fixture'}
        self.ui.entitlements.require.side_effect = PermissionError('expired fixture')
        with patch('adbot.ui.audit', new_callable=AsyncMock):
            with self.assertRaises(PermissionError):
                await self.ui.callback(ADMIN, 'taskact:start:3', None)
        self.ui.entitlements.require.assert_awaited_once_with(10)
        self.ui.engine.start_task.assert_not_awaited()

    async def test_expired_owner_can_stop_task(self):
        self.db.resources[('tasks', 3)] = {'id': 3, 'owner_id': 10, 'name': 'fixture', 'status': 'PAUSED'}
        self.ui.entitlements.require.side_effect = PermissionError('expired fixture')
        data = await self.ui.set_state(10, 'TASK_ACTION', {'id': 3, 'action': 'stop'})
        with patch.object(self.ui, 'task_detail', new_callable=AsyncMock), patch('adbot.ui.audit', new_callable=AsyncMock):
            await self.ui.callback(10, f"w:{data['nonce']}:confirm", None)
        self.ui.entitlements.require.assert_not_awaited()
        self.ui.engine.stop_task.assert_awaited_once_with(3, 10)

    async def test_binding_uses_owner_lock_and_only_targets_consume_channels(self):
        for kind, expected in (('chats', [-1001]), ('sources', None)):
            self.ui.entitlements.require.reset_mock()
            await self.ui.bind(10, '@fixture', kind)
            self.ui.entitlements.require.assert_awaited_once_with(10, con=self.db, proposed_chat_ids=expected)
        self.assertIn(('SELECT pg_advisory_xact_lock($1)', (10,)), self.db.calls)

    async def test_expired_owner_cannot_start_binding(self):
        self.ui.entitlements.require.side_effect = PermissionError('expired fixture')
        with self.assertRaises(PermissionError):
            await self.ui.begin_binding(10, 'chats')
        self.assertNotIn(10, self.db.sessions)

    async def test_pending_purchase_text_does_not_require_admin(self):
        await self.ui.billing_purchase(10, 'week')
        await self.ui.message(10, message('fixture question'))
        self.assertIn('请使用购买确认按钮', self.sent_text())
        self.ui.payments.create_order.assert_not_awaited()

    async def test_expired_owner_can_remove_task_targets(self):
        self.db.resources[('tasks', 3)] = {'id': 3, 'owner_id': 10, 'status': 'PAUSED'}
        self.db.targets = [-1001, -1002]
        self.ui.entitlements.require.side_effect = PermissionError('expired fixture')
        data = dict(owner_id=10, edit_id=3, name='fixture', content_ids=[1], chat_ids=[-1001],
                    interval_seconds=60, delete_previous=False, instant_source_id=None,
                    source_mode='copy', max_runs=0, skip_failed=False, delete_failure_pause=False)
        with patch.object(self.ui, 'task_detail', new_callable=AsyncMock), patch('adbot.ui.audit', new_callable=AsyncMock):
            await self.ui.task_session_action(10, 'TASK_CONFIRM', data, ['confirm'], None)
        self.ui.entitlements.require.assert_not_awaited()
        self.ui.engine.edit_task.assert_awaited_once()

    async def test_admin_adding_task_target_checks_real_owner(self):
        self.db.resources[('tasks', 3)] = {'id': 3, 'owner_id': 10, 'status': 'PAUSED'}
        self.db.targets = [-1001]
        self.ui.entitlements.require.side_effect = PermissionError('expired fixture')
        data = dict(owner_id=10, edit_id=3, name='fixture', content_ids=[1], chat_ids=[-1001, -1002],
                    interval_seconds=60, delete_previous=False, instant_source_id=None,
                    source_mode='copy', max_runs=0, skip_failed=False, delete_failure_pause=False)
        with patch('adbot.ui.audit', new_callable=AsyncMock):
            with self.assertRaises(PermissionError):
                await self.ui.task_session_action(ADMIN, 'TASK_CONFIRM', data, ['confirm'], None)
        self.ui.entitlements.require.assert_awaited_once_with(10, proposed_chat_ids=[-1001, -1002], exclude_task_id=3)
        self.ui.engine.edit_task.assert_not_awaited()

    async def test_removed_admin_not_hardcoded(self):
        self.ui.settings.admin_ids = ()
        self.assertFalse(self.ui.admin(100000001))
        self.assertFalse(self.ui.admin(100000002))

    def test_payment_url_allowlist(self):
        for value in ('https://evil.example/pay', 'https://t.me.evil/okpay', 'https://t.me/okpay#evil',
                      'https://user@t.me/okpay', 'https://t.me/okpay/', 'tg://resolve/path?domain=okpay',
                      'tg://resolve?domain=other', 'https://t.me/okpay?start=bad value',
                      'https://t.me/okpay?start=fixture', 'tg://resolve?domain=okpay&start=fixture',
                      'https://t.me/okpay?start=shop_deposit----fixture&start=shop_deposit----other',
                      'https://t.me/okpay//?start=shop_deposit----fixture',
                      f'https://t.me/okpay?start=shop_deposit--{PROVIDER_ORDER_ID}',
                      f'https://t.me/OkayPayBot//?start=shop_deposit--{PROVIDER_ORDER_ID}', None):
            self.assertIsNone(_pay_url(value))
        self.assertEqual(_pay_url(f'tg://resolve?domain=okpay&start=shop_deposit----{PROVIDER_ORDER_ID}'),
                         CANONICAL_PAY_URL)
        self.assertEqual(_pay_url(f'tg://resolve?domain=OkayPayBot&start=shop_deposit--{PROVIDER_ORDER_ID}'),
                         CANONICAL_PAY_URL)


@pytest.mark.asyncio
@pytest.mark.parametrize('pay_url', [
    CANONICAL_PAY_URL,
    f'https://t.me/OkayPayBot/?start=shop_deposit--{PROVIDER_ORDER_ID}',
    f'https://T.ME/oKaYpAyBoT/?start=shop_deposit--{PROVIDER_ORDER_ID}',
    f'tg://resolve?domain=OkayPayBot&start=shop_deposit--{PROVIDER_ORDER_ID}',
    f'tg://RESOLVE?domain=oKaYpAyBoT&start=shop_deposit--{PROVIDER_ORDER_ID}',
    f'https://t.me/OKPay?start=shop_deposit----{PROVIDER_ORDER_ID}',
    f'https://t.me/OKPay/?start=shop_deposit----{PROVIDER_ORDER_ID}',
    f'https://T.ME/oKpAy/?start=shop_deposit----{PROVIDER_ORDER_ID}',
    f'tg://resolve?domain=OKPay&start=shop_deposit----{PROVIDER_ORDER_ID}',
    f'tg://RESOLVE?domain=oKpAy&start=shop_deposit----{PROVIDER_ORDER_ID}',
])
async def test_official_payment_link_variants_render_canonical_button(pay_url):
    bot = FakeBot()
    ui = BotUI(BillingDB(), bot, NS(admin_ids=(ADMIN,), workers=1))
    ui.engine = NS(throttle=AsyncMock(), cooldown=AsyncMock())

    await ui.billing_order(10, order(pay_url=pay_url))

    buttons = [button for row in bot.sent[-1][1]['reply_markup'].inline_keyboard for button in row]
    payment_buttons = [button for button in buttons if button.url]
    assert len(payment_buttons) == 1
    assert payment_buttons[0].text == '前往 OKPay 付款'
    assert payment_buttons[0].url == CANONICAL_PAY_URL


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['CREATING', 'CREATE_UNKNOWN', 'PENDING'])
@pytest.mark.parametrize('pay_url', [None, ''], ids=['none', 'empty'])
async def test_inflight_order_without_payment_url_keeps_original_order(status, pay_url):
    bot = FakeBot()
    ui = BotUI(BillingDB(), bot, NS(admin_ids=(ADMIN,), workers=1))
    ui.engine = NS(throttle=AsyncMock(), cooldown=AsyncMock())
    ui.payments = NS(create_order=AsyncMock())

    await ui.billing_order(10, order(status=status, pay_url=pay_url))

    rendered = bot.sent[-1][1]
    assert '订单 #4' in rendered['text']
    assert '原订单正在核实，支付链接可能遗失。请保留原单并联系管理员，勿重复下单或付款。' in rendered['text']
    buttons = [button for row in rendered['reply_markup'].inline_keyboard for button in row]
    assert not any(button.url for button in buttons)
    actions = {button.callback_data for button in buttons}
    assert {'u:10:bpay:check:4', 'u:10:contact'} <= actions
    assert not any(':buy:' in action or ':confirm:' in action for action in actions)
    ui.payments.create_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_sql_unpaid_user_keeps_content_and_management_not_new_binding_or_task(rig):
    await register(rig)
    await rig.db.execute("UPDATE app_settings SET value='true'::jsonb WHERE key='billing_enabled'")
    await click(rig, 'new:chats')
    assert await rig.ui.state(SQL_OWNER) is None
    await click(rig, 'new:tasks')
    assert await rig.ui.state(SQL_OWNER) is None
    assert await rig.db.fetchval('SELECT count(*) FROM user_chats') == 0
    assert await rig.db.fetchval('SELECT count(*) FROM tasks') == 0
    await create_content(rig, title='unpaid draft')
    assert await rig.db.fetchval('SELECT count(*) FROM contents WHERE owner_id=$1', SQL_OWNER) == 1
    await click(rig, 'bpay:member')
    assert '已到期 / 尚未购买' in rig.bot.edited[-1][1]
    await click(rig, 'contact')
    assert rig.bot.edited[-1][1] == await rig.ui.config('contact_admin', '联系管理员：@sad1881')
    await click(rig, 'new:tasks', SQL_ADMIN)
    assert (await rig.ui.state(SQL_ADMIN))['state'] == 'TASK_NAME'


@pytest.mark.asyncio
async def test_sql_overquota_binding_blocked_sources_free_and_admin_owner_gate(rig):
    await register(rig)
    await rig.db.execute("UPDATE app_settings SET value='true'::jsonb WHERE key='billing_enabled'")
    await rig.db.execute("INSERT INTO membership_segments(owner_id,plan_code,starts_at,ends_at,source_key) "
                         "VALUES($1,'week',now()-interval '1 day',now()+interval '6 days','ui-fixture')", SQL_OWNER)
    ids = [-10050000 - i for i in range(20)]
    for chat_id in ids:
        await rig.db.execute("INSERT INTO chats(chat_id,type,title,bot_status) VALUES($1,'channel','fixture','administrator')", chat_id)
        await rig.db.execute('INSERT INTO user_chats(owner_id,chat_id) VALUES($1,$2)', SQL_OWNER, chat_id)
    await rig.ui.bind(SQL_OWNER, str(CHAT_A), 'chats')
    assert await rig.db.fetchval('SELECT count(*) FROM user_chats WHERE owner_id=$1 AND active', SQL_OWNER) == 20
    await rig.ui.bind(SQL_OWNER, '@sourceone', 'sources')
    assert await rig.db.fetchval('SELECT count(*) FROM sources WHERE owner_id=$1 AND active', SQL_OWNER) == 1
    assert (await rig.ui.entitlements.snapshot(SQL_OWNER))['used_channels'] == 20
    await rig.db.execute("UPDATE membership_segments SET ends_at=now()-interval '1 hour' WHERE owner_id=$1", SQL_OWNER)
    binding = await rig.db.fetchval('SELECT id FROM user_chats WHERE owner_id=$1 LIMIT 1', SQL_OWNER)
    with patch('adbot.ui.deliver', new_callable=AsyncMock) as deliver:
        await click(rig, f'test:{binding}', SQL_ADMIN)
        deliver.assert_not_awaited()
    assert '会员未开通或已到期' in rig.bot.edited[-1][1]


@pytest.mark.asyncio
async def test_sql_expired_owner_removes_targets_and_stops_without_publish_access(rig):
    await register(rig)
    await bind_targets(rig)
    content_id = await create_content(rig)
    task_id = await create_task(rig, [content_id])
    await rig.db.execute("UPDATE app_settings SET value='true'::jsonb WHERE key='billing_enabled'")
    await click(rig, f'tedit:{task_id}')
    await step(rig, 'next')
    await step(rig, f'toggle:{CHAT_B}')
    await step(rig, 'next')
    await send(rig, '90')
    await step(rig, 'source:0')
    await send(rig, '2')
    await step(rig, 'confirm')
    assert await rig.db.fetchval('SELECT count(*) FROM task_targets WHERE task_id=$1 AND active', task_id) == 1
    await click(rig, f'taskact:start:{task_id}', SQL_ADMIN)
    assert '会员未开通或已到期' in rig.bot.edited[-1][1]
    assert await rig.ui.state(SQL_ADMIN) is None
    await click(rig, f'taskact:stop:{task_id}')
    await step(rig, 'confirm')
    assert await rig.db.fetchval('SELECT status FROM tasks WHERE id=$1', task_id) == 'STOPPED'
